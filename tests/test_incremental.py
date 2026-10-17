"""Live mode: window computation, status health/exit codes, and the update command (offline)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from litradar import incremental
from litradar.cli import main
from litradar.models import Paper
from litradar.pipeline import Pipeline
from litradar.store import Store

TODAY = date(2026, 10, 17)  # a Saturday
NOW = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_run(store: Store, since: str, until: str, finished: datetime, counts=None, errors=None, started=None):
    report = {"since": since, "until": until, "counts": counts or {}, "errors": errors or [], "http": {"requests": 3}}
    store.conn.execute(
        "INSERT INTO runs (started, finished, since, until, counts_json) VALUES (?,?,?,?,?)",
        (_ts(started or (finished - timedelta(hours=1))), _ts(finished), since, until, json.dumps(report)),
    )
    store.conn.commit()


# ---------------------------------------------------------------- window ---------------------
def test_window_first_run_defaults_to_seven_days():
    w = incremental.compute_window(None, today=TODAY)
    assert (w["since"], w["until"], w["basis"], w["days"], w["capped"]) == ("2026-10-10", "2026-10-17", "first_run", 7, False)


def test_window_continues_from_last_run_with_overlap():
    w = incremental.compute_window("2026-10-16", today=TODAY, overlap_days=2)
    assert (w["since"], w["until"], w["basis"], w["days"]) == ("2026-10-14", "2026-10-17", "last_run", 3)
    w0 = incremental.compute_window("2026-10-16", today=TODAY, overlap_days=0)
    assert w0["since"] == "2026-10-16"
    w5 = incremental.compute_window("2026-10-16", today=TODAY, overlap_days=5)
    assert w5["since"] == "2026-10-11"


def test_window_is_capped_after_an_outage():
    w = incremental.compute_window("2026-09-01", today=TODAY, overlap_days=2, max_days=14)
    assert (w["since"], w["until"], w["capped"], w["days"]) == ("2026-10-03", "2026-10-17", True, 14)
    assert w["last_until"] == "2026-09-01"


def test_window_overrides():
    w = incremental.compute_window("2026-10-16", today=TODAY, since="2026-09-01")
    assert (w["since"], w["until"], w["basis"], w["capped"], w["days"]) == ("2026-09-01", "2026-10-17", "override", False, 46)  # explicit since: no cap
    w = incremental.compute_window("2026-10-16", today=TODAY, since="2026-08-01", until="2026-10-10")
    assert (w["since"], w["until"], w["basis"], w["capped"]) == ("2026-08-01", "2026-10-10", "override", False)
    w = incremental.compute_window("2026-10-10", today=TODAY, until="2026-10-12", overlap_days=1)
    assert (w["since"], w["until"], w["basis"]) == ("2026-10-09", "2026-10-12", "last_run")


def test_window_clamps_future_last_run_and_validates_input():
    w = incremental.compute_window("2026-10-30", today=TODAY, overlap_days=0)
    assert w["since"] == w["until"] == "2026-10-17"
    with pytest.raises(ValueError):
        incremental.compute_window(None, today=TODAY, since="17/10/2026")
    with pytest.raises(ValueError):
        incremental.compute_window(None, today=TODAY, overlap_days=-1)
    with pytest.raises(ValueError):
        incremental.compute_window(None, today=TODAY, max_days=0)


def test_error_sources_collapse_feed_names():
    errs = ["europepmc: boom", "rss Nature Methods: 404", "rss Cell: 404", "subject feed Genomics: 500", "sra: timeout", "europepmc: again"]
    assert incremental.error_sources(errs) == ["europepmc", "rss", "subject", "sra"]
    assert incremental.error_sources([]) == []


# ---------------------------------------------------------------- status ---------------------
def test_status_ok_on_fresh_clean_run(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _seed_run(st, "2026-10-14", "2026-10-17", NOW - timedelta(hours=2), counts={"papers.new": 5, "papers.unique": 9, "fetched.biorxiv": 4}, errors=["rss Nature: 404", "sra: 500"])
    rep = incremental.health(st.conn, st.path, now=NOW)
    assert rep["ok"] is True and rep["problems"] == []
    assert incremental.exit_code(rep) == 0
    assert rep["last_update"]["error_sources"] == ["rss", "sra"]  # two failing sources is tolerated
    assert rep["last_update"]["window_days"] == 3
    assert rep["db"]["bytes"] > 0


def test_status_exit_1_when_stale(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _seed_run(st, "2026-10-07", "2026-10-13", NOW - timedelta(days=3, hours=1))
    rep = incremental.health(st.conn, st.path, now=NOW)
    assert incremental.exit_code(rep) == 1
    assert any("days ago" in p for p in rep["problems"])
    # just inside the limit is fine
    st2 = Store(tmp_path / "t2.sqlite")
    _seed_run(st2, "2026-10-07", "2026-10-13", NOW - timedelta(days=2, hours=23))
    assert incremental.exit_code(incremental.health(st2.conn, st2.path, now=NOW)) == 0


def test_status_exit_1_when_more_than_two_sources_failed(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _seed_run(st, "2026-10-14", "2026-10-17", NOW - timedelta(hours=1), errors=["biorxiv: x", "europepmc: y", "openalex: z"])
    rep = incremental.health(st.conn, st.path, now=NOW)
    assert incremental.exit_code(rep) == 1
    assert any("errors on 3 sources" in p for p in rep["problems"])


def test_status_exit_1_without_any_run(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    rep = incremental.health(st.conn, st.path, now=NOW)
    assert rep["last_update"] is None and incremental.exit_code(rep) == 1
    assert "no runs recorded yet" in rep["problems"]
    assert rep["gaps"] == {"window_days": 30, "days_checked": 0, "coverage_start": None, "gaps": []}


def test_status_per_source_table_and_rates(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _seed_run(st, "2026-10-01", "2026-10-08", NOW - timedelta(days=9), counts={"fetched.biorxiv": 100, "archive.sra_new": 1000, "news.gnews": 7})
    _seed_run(st, "2026-10-06", "2026-10-15", NOW - timedelta(days=2), counts={"fetched.biorxiv": 120, "fetched.arxiv": 3, "archive.sra_new": 900, "news.excluded": 4})
    _seed_run(st, "2026-10-13", "2026-10-17", NOW - timedelta(hours=1), counts={"fetched.biorxiv": 150, "archive.sra_new": 950, "archive.ena_new": 20})
    for i, pub in enumerate(["2026-10-16", "2026-10-16", "2026-10-15", "2026-10-02"]):
        st.upsert_paper(Paper(id=f"doi:10.1/{i}", source="biorxiv", kind="preprint", title=f"p{i}", pub_date=pub))
    st.commit()
    rep = incremental.health(st.conn, st.path, now=NOW)
    rows = {r["source"]: r for r in rep["per_source"]["rows"]}
    assert rows["fetched.biorxiv"]["values"] == [150, 120, 100] and rows["fetched.biorxiv"]["delta"] == 30
    assert rows["archive.sra_new"]["delta"] == 50
    assert rows["archive.ena_new"]["values"] == [20, 0, 0]
    assert rows["fetched.arxiv"]["values"] == [0, 3, 0] and rows["fetched.arxiv"]["delta"] == -3
    assert "news.excluded" not in rows and rows["news.gnews"]["values"] == [0, 0, 7]
    assert rep["papers"]["total"] == 4 and rep["papers"]["per_day"] == round(4 / 14, 1)  # first_seen is today for all four
    assert rep["per_source"]["runs"][0]["since"] == "2026-10-13"
    text = incremental.format_status(rep)
    assert "fetched.biorxiv" in text and "(+30)" in text and "OK" in text


def test_status_gaps_respect_coverage_start(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _seed_run(st, "2026-10-10", "2026-10-17", NOW - timedelta(hours=1))
    for i, pub in enumerate(["2026-10-10", "2026-10-11", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17"]):
        st.upsert_paper(Paper(id=f"doi:10.1/{i}", source="biorxiv", kind="preprint", title=f"p{i}", pub_date=pub))
    st.commit()
    g = incremental.paper_gaps(st.conn, TODAY)
    assert g["coverage_start"] == "2026-10-10" and g["days_checked"] == 8
    assert g["gaps"] == ["2026-10-12"]


def test_next_cron_matches_documented_schedules():
    assert incremental.next_cron("20 6 * * *", NOW) == datetime(2026, 10, 18, 6, 20, tzinfo=timezone.utc)
    assert incremental.next_cron("15 11 * * 1", NOW) == datetime(2026, 10, 19, 11, 15, tzinfo=timezone.utc)  # next Monday
    assert incremental.next_cron("20 6 * * *", datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 17, 6, 20, tzinfo=timezone.utc)
    nxt = incremental.next_scheduled(NOW)
    assert [s["next_utc"] for s in nxt] == ["2026-10-18T06:20Z", "2026-10-19T11:15Z"]
    with pytest.raises(ValueError):
        incremental.next_cron("0 0 1 * *", NOW)


def test_disabled_sources_and_key_presence():
    cfg = {"sources": {"biorxiv": {"enabled": True}, "openalex": {"enabled": False}, "sec": {"enabled": False}, "ena": {}}}
    assert incremental.disabled_sources(cfg) == ["openalex", "sec"]

    class S:
        contact_email = "you@example.com"
        ncbi_api_key = ""
        anthropic_api_key = "sk-secret"

    keys = incremental.key_presence(S())
    assert keys["LITRADAR_CONTACT_EMAIL"] is True and keys["NCBI_API_KEY"] is False and keys["ANTHROPIC_API_KEY"] is True
    assert "sk-secret" not in json.dumps(keys)


# ---------------------------------------------------------------- CLI: status ----------------
@pytest.fixture
def live_env(tmp_path, monkeypatch):
    db = tmp_path / "data" / "radar.sqlite"
    out = tmp_path / "digests"
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(out))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setattr(incremental, "utc_now", lambda: NOW)
    return {"db": db, "out": out}


def test_status_cli_json_and_exit_codes(live_env, capsys):
    assert main(["status"]) == 1  # no database yet
    assert not live_env["db"].exists()  # status never creates the database
    st = Store(live_env["db"])
    _seed_run(st, "2026-10-14", "2026-10-17", NOW - timedelta(hours=1), counts={"papers.new": 2})
    st.close()
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] is True and rep["last_update"]["papers_new"] == 2
    assert set(rep["keys_configured"]) >= {"NCBI_API_KEY", "ANTHROPIC_API_KEY", "LITRADAR_CONTACT_EMAIL"}
    assert all(isinstance(v, bool) for v in rep["keys_configured"].values())
    assert rep["next_scheduled"][0]["cron"] == "20 6 * * *"
    assert main(["status", "--stale-days", "0"]) == 1


# ---------------------------------------------------------------- CLI: update ----------------
def test_update_dry_run_prints_plan_without_fetching(live_env, capsys, monkeypatch):
    def boom(self, *a, **k):
        raise AssertionError("Pipeline.run must not be called in a dry run")

    monkeypatch.setattr(Pipeline, "run", boom)
    assert main(["update", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert (plan["since"], plan["until"], plan["basis"]) == ("2026-10-10", "2026-10-17", "first_run")


def test_update_calls_pipeline_with_the_computed_window(live_env, capsys, monkeypatch):
    st = Store(live_env["db"])
    _seed_run(st, "2026-10-08", "2026-10-15", NOW - timedelta(days=2), counts={"papers.new": 1})
    st.close()
    calls = []

    def fake_run(self, since, until, use_llm=False, fulltext=True, only=None):
        calls.append({"since": since, "until": until, "use_llm": use_llm, "fulltext": fulltext, "only": only})
        self.counts.update({"papers.unique": 12, "papers.new": 7, "archive.sra_new": 300, "archive.ena_new": 4, "news.gnews": 2, "fetched.biorxiv": 12})
        self.errors.append("rss Example: 404")
        report = {"since": since, "until": until, "counts": dict(self.counts), "errors": self.errors, "http": {"requests": 5}}
        self.store.record_run("2026-10-17T06:20:00Z", since, until, report)
        return report

    monkeypatch.setattr(Pipeline, "run", fake_run)
    assert main(["update", "--overlap-days", "2"]) == 0
    assert calls == [{"since": "2026-10-13", "until": "2026-10-17", "use_llm": False, "fulltext": True, "only": None}]
    out = capsys.readouterr().out
    assert "12 papers fetched, 7 new" in out and "304 new archive records" in out and "1 error(s) on 1 source(s): rss" in out
    summary = json.loads((live_env["out"] / "_work" / "last_update.json").read_text())
    assert summary["since"] == "2026-10-13" and summary["papers_new"] == 7 and summary["archive_new_by_source"] == {"ena": 4, "sra": 300}
    assert summary["window"]["basis"] == "last_run" and summary["error_sources"] == ["rss"]
    # the run was recorded, so the next window continues from today
    assert main(["update", "--sources", "sra,ena", "--llm", "--no-fulltext", "--max-days", "3"]) == 0
    assert calls[-1] == {"since": "2026-10-15", "until": "2026-10-17", "use_llm": True, "fulltext": False, "only": ["sra", "ena"]}
    assert main(["update", "--since", "2026-09-01", "--until", "2026-09-10"]) == 0
    assert (calls[-1]["since"], calls[-1]["until"]) == ("2026-09-01", "2026-09-10")
    # status now sees a fresh run with a single failing source
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["last_update"]["error_sources"] == ["rss"] and rep["last_update_json"]["papers_new"] == 7


def test_update_failure_leaves_a_summary_and_records_nothing(live_env, capsys, monkeypatch):
    def fake_run(self, since, until, **kw):
        self.errors.append("biorxiv: 503")
        raise RuntimeError("network down")

    monkeypatch.setattr(Pipeline, "run", fake_run)
    assert main(["update"]) == 2
    assert "FAILED" in capsys.readouterr().out
    summary = json.loads((live_env["out"] / "_work" / "last_update.json").read_text())
    assert summary["failed"].startswith("RuntimeError") and summary["error_sources"] == ["biorxiv"]
    st = Store(live_env["db"])
    assert incremental.last_run(st.conn) is None
