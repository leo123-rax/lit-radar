"""Offline tests for litradar.trends on a temporary SQLite store with synthetic weekly data."""
import json
import statistics
from datetime import date, timedelta

import pytest

from litradar import trends
from litradar.models import ArchiveRecord, Mention, Paper
from litradar.store import Store

UNTIL = "2026-10-17"
END = date.fromisoformat(UNTIL)


def _day(weeks_back: int, offset: int = 0) -> str:
    """A date inside week `weeks_back` (0 = current week): the week's last day minus `offset` days (0..6)."""
    return (END - timedelta(days=7 * weeks_back + offset)).isoformat()


def _paper(pid: str, pub_date: str, journal: str = "Nature", kind: str = "article", title: str = "Paper", **kw) -> Paper:
    base = dict(id=pid, source="europepmc", kind=kind, title=title, doi=pid[4:] if pid.startswith("doi:") else None,
                abstract="abstract text", journal=journal, pub_date=pub_date, url=f"https://example.org/{pid}")
    base.update(kw)
    return Paper(**base)


def _set_first_seen(st: Store, pid: str, day: str) -> None:
    st.conn.execute("UPDATE papers SET first_seen=?, updated_at=? WHERE id=?", (f"{day}T12:00:00Z", f"{day}T12:00:00Z", pid))


def _add_instrument_papers(st: Store, entity: str, name: str, weeks_back: int, n: int, first_seen_day: str = None, prefix: str = "", **kw) -> None:
    for i in range(n):
        pid = f"doi:10.1000/{prefix}{entity}-w{weeks_back}-{i}"
        st.upsert_paper(_paper(pid, _day(weeks_back, i % 7), **kw))
        st.replace_mentions(pid, [Mention(pid, "instrument", entity, name, "Vendor", "TCK", "sequencer", "methods", 1, "...")])
        _set_first_seen(st, pid, first_seen_day or _day(weeks_back, i % 7))


@pytest.fixture()
def synthetic(tmp_path):
    """Four baseline weeks + the current week.

    riser  : 8, 12, 8, 12 then 20  -> mean 10, sample std 2.309, z = +4.33, notable
    steady : 10 x4 then 11         -> std 0 -> floored to 1 -> z = +1.0, not notable
    tiny   : 0 x4 then 3           -> z = +3.0 but count 3 < 5 -> not notable; new this week
    faller : company 10 x4 then 0  -> z = -10, baseline mean 10 >= 5 -> notable faller
    """
    st = Store(tmp_path / "t.sqlite")
    for wk, n in zip([4, 3, 2, 1], [8, 12, 8, 12]):
        _add_instrument_papers(st, "riser", "Riser 3000", wk, n)
    _add_instrument_papers(st, "riser", "Riser 3000", 0, 20)
    for wk in [4, 3, 2, 1]:
        _add_instrument_papers(st, "steady", "Steady", wk, 10)
    _add_instrument_papers(st, "steady", "Steady", 0, 11)
    _add_instrument_papers(st, "tiny", "Tiny", 0, 3)  # first seen this week -> new entity
    # faller company: 10 papers/week in baseline, none this week; mixed roles
    for wk in [4, 3, 2, 1]:
        for i in range(10):
            pid = f"doi:10.1000/faller-w{wk}-{i}"
            st.upsert_paper(_paper(pid, _day(wk, i % 7), journal="Cell"))
            section = "affiliations" if i < 4 else ("coi" if i < 7 else "methods")
            st.replace_mentions(pid, [Mention(pid, "company", "faller-co", "Faller Co", "", "FALL", "", section, 1, "...")])
            _set_first_seen(st, pid, _day(wk, i % 7))
    # tracked-author hit in a subgroup, this week, plus a keyword paper for the same subgroup
    pid = "doi:10.1000/tracked-1"
    st.upsert_paper(_paper(pid, _day(0, 1), title="A spatial transcriptomics atlas"))
    st.add_tracked_author_hit(pid, "Some Author", "spatial", "name")
    _set_first_seen(st, pid, _day(0, 1))
    # preprint -> journal conversion: article published and first seen this week
    pre = "doi:10.1101/2026.07.01.1"
    st.upsert_paper(_paper(pre, _day(6, 2), journal="biorxiv", kind="preprint", title="Preprint P", published_doi="10.1038/S41586-026-9999-9"))
    _set_first_seen(st, pre, _day(6, 2))
    art = "doi:10.1038/s41586-026-9999-9"
    st.upsert_paper(_paper(art, _day(0, 3), journal="Nature", title="Journal version of P"))
    _set_first_seen(st, art, _day(0, 3))
    # malformed dates must be ignored, not crash
    bad = "doi:10.1000/bad-date"
    st.upsert_paper(_paper(bad, "2026-13-99"))
    st.replace_mentions(bad, [Mention(bad, "instrument", "riser", "Riser 3000", "Vendor", "TCK", "sequencer", "methods", 1, "...")])
    _set_first_seen(st, bad, _day(0))
    # archive records: NovaSeq-ish model 100/week baseline then 300; a brand-new model this week
    recs = []
    for wk in [4, 3, 2, 1]:
        recs += [ArchiveRecord("sra", f"SRX{wk}-{i}", f"SRP{wk}", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", _day(wk, i % 7), "c", "t", "Homo sapiens", "ilmn-novaseq-x", "Illumina", "ILMN") for i in range(100)]
    recs += [ArchiveRecord("sra", f"SRX0-{i}", "SRP0", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", _day(0, i % 7), "c", "t", "Homo sapiens", "ilmn-novaseq-x", "Illumina", "ILMN") for i in range(300)]
    recs += [ArchiveRecord("ena", f"ERX0-{i}", "ERP0", "Brand New Sequencer", "OTHER", "RNA-Seq", _day(0, 1), "c", "t", "Mus musculus") for i in range(6)]
    st.upsert_archive_records(recs)
    st.commit()
    return st


SUBGROUPS = [{"id": "spatial", "name": "Spatial biology", "keywords": ["spatial transcriptomics", "Xenium"], "openalex_topics": ["T11289"]}]


def test_zscore_and_notable_flags(synthetic):
    tr = trends.compute_trends(synthetic, UNTIL, weeks=4, subgroups=SUBGROUPS)
    by_id = {r["id"]: r for r in tr["instruments"]["papers"]}
    riser = by_id["riser"]
    assert riser["current"] == 20 and riser["previous"] == 12 and riser["series"] == [8, 12, 8, 12, 20]
    assert riser["mean"] == 10.0 and riser["baseline_weeks"] == 4
    assert riser["std"] == pytest.approx(statistics.stdev([8, 12, 8, 12]), abs=0.01) == pytest.approx(2.31, abs=0.01)
    assert riser["z"] == pytest.approx(10 / statistics.stdev([8, 12, 8, 12]), abs=0.01) == pytest.approx(4.33, abs=0.01)
    assert riser["notable"] is True and riser["direction"] == "up" and riser["pct_change"] == pytest.approx(66.7)
    steady = by_id["steady"]
    assert steady["std"] == 0.0 and steady["z"] == 1.0 and steady["notable"] is False  # std floored at 1.0
    tiny = by_id["tiny"]
    assert tiny["z"] == 3.0 and tiny["notable"] is False  # |z| >= 2 but count 3 < 5
    assert tr["history"]["paper_weeks_with_data"] == 4 and tr["history"]["z_available_papers"] is True
    # the malformed-date paper contributed nothing (riser current stays 20)
    assert tr["totals"]["papers"]["current"] == 20 + 11 + 3 + 1 + 1  # riser, steady, tiny, tracked, journal article


def test_faller_company_roles_and_notable_ordering(synthetic):
    tr = trends.compute_trends(synthetic, UNTIL, weeks=4, subgroups=SUBGROUPS)
    comp = {r["id"]: r for r in tr["companies"]}["faller-co"]
    assert comp["current"] == 0 and comp["previous"] == 10 and comp["mean"] == 10.0 and comp["z"] == -10.0
    assert comp["notable"] is True and comp["direction"] == "down" and comp["pct_change"] == -100.0
    assert comp["roles"]["as_author"]["previous"] == 4 and comp["roles"]["as_funder_or_coi"]["previous"] == 3 and comp["roles"]["as_subject"]["previous"] == 3
    notable_ids = [(r["group"], r["id"]) for r in tr["notable"]]
    # sorted by |z|: archive model (z = 200), then the company faller and its journal (both |z| = 10), then the paper riser (4.33)
    assert notable_ids[0] == ("instrument_experiments", "Illumina NovaSeq X Plus")
    assert set(notable_ids[1:3]) == {("company", "faller-co"), ("journal", "cell")}
    assert ("instrument_papers", "riser") in notable_ids and ("instrument_papers", "tiny") not in notable_ids
    zs = [abs(r["z"]) for r in tr["notable"]]
    assert zs == sorted(zs, reverse=True)  # notable list is ordered by |z|
    risers, fallers = trends.top_movers(tr, n=5)
    assert risers[0]["id"] == "Illumina NovaSeq X Plus" and "riser" in {r["id"] for r in risers}
    assert [r["z"] for r in risers] == sorted((r["z"] for r in risers), reverse=True)
    assert {r["id"] for r in fallers[:2]} == {"faller-co", "cell"}  # the company and the journal that carried its papers tie at z = -10
    assert all(r["id"] != "tiny" for r in risers)  # below the min_count gate


def test_archive_experiments_and_new_entities(synthetic):
    tr = trends.compute_trends(synthetic, UNTIL, weeks=4)
    exp = {r["id"]: r for r in tr["instruments"]["experiments"]}
    nova = exp["Illumina NovaSeq X Plus"]
    assert nova["series"] == [100, 100, 100, 100, 300] and nova["z"] == 200.0 and nova["notable"] is True
    assert nova["studies"]["current"] == 1 and nova["ticker"] == "ILMN"
    assert exp["Brand New Sequencer"]["current"] == 6 and exp["Brand New Sequencer"]["mean"] == 0.0
    assert tr["totals"]["archive_experiments"]["current"] == 306
    ne = tr["new_entities"]
    assert [r["id"] for r in ne["instrument"]] == ["tiny"]  # riser/steady were first seen in earlier weeks
    assert ne["company"] == []
    assert [r["id"] for r in ne["archive_instrument"]] == ["Brand New Sequencer"]
    assert tr["cold_start"] is False


def test_subgroups_from_keywords_and_tracked_hits(synthetic):
    tr = trends.compute_trends(synthetic, UNTIL, weeks=4, subgroups=SUBGROUPS)
    sg = {r["id"]: r for r in tr["subgroups"]}["spatial"]
    assert sg["name"] == "Spatial biology" and sg["metric"] == "papers"
    assert sg["current"] == 1 and sg["tracked_author_papers"]["current"] == 1
    # without definitions, subgroups come from tracked-author hits only
    tr2 = trends.compute_trends(synthetic, UNTIL, weeks=4)
    sg2 = {r["id"]: r for r in tr2["subgroups"]}["spatial"]
    assert sg2["metric"] == "tracked_author_papers" and sg2["current"] == 1


def test_journals_and_conversions(synthetic):
    tr = trends.compute_trends(synthetic, UNTIL, weeks=4)
    journals = {r["id"]: r for r in tr["journals"]}
    assert journals["cell"]["previous"] == 10 and journals["cell"]["current"] == 0
    assert journals["nature"]["current"] >= 1
    assert len(tr["conversions"]) == 1
    c = tr["conversions"][0]
    assert c["preprint_id"] == "doi:10.1101/2026.07.01.1" and c["article_id"] == "doi:10.1038/s41586-026-9999-9"
    assert c["journal"] == "Nature" and c["lag_days"] == 6 * 7 + 2 - 3  # posted week 6 (offset 2), published week 0 (offset 3)
    # a conversion whose journal version is older than the window is not reported again
    synthetic.conn.execute("UPDATE papers SET pub_date=?, first_seen=? WHERE id=?", (_day(3), f"{_day(3)}T00:00:00Z", c["article_id"]))
    synthetic.conn.execute("UPDATE papers SET updated_at=? WHERE id=?", (f"{_day(3)}T00:00:00Z", c["preprint_id"]))
    synthetic.commit()
    assert trends.compute_trends(synthetic, UNTIL, weeks=4)["conversions"] == []


def test_render_markdown_and_json_roundtrip(synthetic):
    tr = trends.compute_trends(synthetic, UNTIL, weeks=4, subgroups=SUBGROUPS)
    json.dumps(tr)  # JSON-serialisable (no inf/nan/dates)
    md = trends.render_trends_markdown(tr, top_n=5)
    assert md.startswith("## What changed: 2026-10-11 to 2026-10-17")
    assert "### Biggest risers" in md and "### Biggest fallers" in md and "### Preprints that became journal articles" in md
    assert "| **Riser 3000** | instrument · papers | 20 | 12 | 10.0 ± 2.3 | +4.3 | TCK |" in md
    assert "| **Faller Co** | company · papers | 0 | 10 | 10.0 ± 0.0 | -10.0 | author 0 · funding/COI 0 · subject 0 |" in md
    assert "Instruments (first appearance in the corpus): Tiny (3)" in md
    assert "Brand New Sequencer (6)" in md
    assert "Journal version of P" not in md and "[Preprint P](https://example.org/doi:10.1101/2026.07.01.1)" in md
    assert "median preprint-to-journal lag 41 days" in md
    assert trends.render_trends_markdown(tr, heading_level=3).startswith("### What changed")


def test_empty_store(tmp_path):
    st = Store(tmp_path / "empty.sqlite")
    tr = trends.compute_trends(st, UNTIL)
    assert tr["instruments"] == {"papers": [], "experiments": []} and tr["companies"] == [] and tr["journals"] == []
    assert tr["conversions"] == [] and tr["notable"] == [] and tr["cold_start"] is True
    assert tr["totals"]["papers"]["current"] == 0 and tr["totals"]["papers"]["z"] is None
    assert tr["baseline"]["since"] == "2026-07-12" and tr["previous_week"] == {"start": "2026-10-04", "end": "2026-10-10"}
    md = trends.render_trends_markdown(tr)
    assert "z-scores are suppressed" in md and "_nothing to show_" in md and "no preprint" in md
    json.dumps(tr)


def test_cold_start_single_week_suppresses_z(tmp_path):
    st = Store(tmp_path / "cold.sqlite")
    _add_instrument_papers(st, "x", "X", 0, 7, first_seen_day=_day(0))
    st.commit()
    tr = trends.compute_trends(st, UNTIL)
    row = tr["instruments"]["papers"][0]
    assert row["current"] == 7 and row["z"] is None and row["notable"] is False and row["mean"] is None
    assert tr["cold_start"] is True and tr["history"]["paper_weeks_with_data"] == 0
    risers, fallers = trends.top_movers(tr)
    assert {r["id"] for r in risers} == {"x", "nature"} and fallers == []  # ranked by week-over-week change; journal counts too
    assert all(r["z"] is None for r in risers)
    md = trends.render_trends_markdown(tr)
    assert "First week of data in this corpus: every entity is new (1 instruments" in md


def test_insufficient_history_threshold(tmp_path):
    st = Store(tmp_path / "two.sqlite")
    for wk in (2, 1):
        _add_instrument_papers(st, "x", "X", wk, 5)
    _add_instrument_papers(st, "x", "X", 0, 9)
    st.commit()
    assert trends.compute_trends(st, UNTIL)["instruments"]["papers"][0]["z"] is None  # 2 weeks < default 3
    row = trends.compute_trends(st, UNTIL, min_baseline_weeks=2)["instruments"]["papers"][0]
    assert row["z"] == 4.0 and row["notable"] is True  # (9 - 5) / max(0, 1)


def test_bad_arguments(tmp_path):
    st = Store(tmp_path / "args.sqlite")
    with pytest.raises(ValueError):
        trends.compute_trends(st, "17/10/2026")
    with pytest.raises(ValueError):
        trends.compute_trends(st, UNTIL, weeks=0)


def test_cli_trends_prints_and_writes(tmp_path, monkeypatch, capsys):
    from litradar import cli

    st = Store(tmp_path / "cli.sqlite")
    _add_instrument_papers(st, "x", "X", 0, 6, first_seen_day=_day(0))
    st.close()
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "cli.sqlite"))
    assert cli.main(["trends", "--until", UNTIL, "--print"]) == 0
    out = capsys.readouterr().out
    assert "## What changed: 2026-10-11 to 2026-10-17" in out
    assert cli.main(["trends", "--until", UNTIL, "--out", str(tmp_path / "digests")]) == 0
    assert (tmp_path / "digests" / f"trends-{UNTIL}.md").exists()
    assert "trends written to" in capsys.readouterr().out
    assert cli.main(["trends", "--until", UNTIL, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["until"] == UNTIL and data["instruments"]["papers"][0]["id"] == "x"


def _bulk_instrument_papers(st: Store, entity: str, weeks_back: int, n: int) -> None:
    """Like _add_instrument_papers but with an entity-specific id prefix so entities never collide."""
    _add_instrument_papers(st, entity, entity.title(), weeks_back, n, prefix=f"{entity}-")


@pytest.fixture()
def partial_week(tmp_path):
    """Three full baseline weeks (family total 50) + one 10%-size week (total 5) + the current week.

    x: 40, 40, 40, 4 then 60   y: 10, 10, 10, 1 then 0
    """
    st = Store(tmp_path / "partial.sqlite")
    for wk in (4, 3, 2):
        _bulk_instrument_papers(st, "x", wk, 40)
        _bulk_instrument_papers(st, "y", wk, 10)
    _bulk_instrument_papers(st, "x", 1, 4)
    _bulk_instrument_papers(st, "y", 1, 1)
    _bulk_instrument_papers(st, "x", 0, 60)
    st.commit()
    return st


def test_partial_week_is_dropped_from_baseline(partial_week):
    tr = trends.compute_trends(partial_week, UNTIL, weeks=4)
    hist = tr["history"]
    assert hist["paper_weeks_with_data"] == 3 and hist["z_available_papers"] is True
    assert [d["week"] for d in hist["paper_weeks_dropped_partial"]] == [1]
    d = hist["paper_weeks_dropped_partial"][0]
    assert (d["start"], d["end"], d["total"], d["median"]) == ("2026-10-04", "2026-10-10", 5, 50.0)  # median of {50, 50, 50, 5}
    assert hist["previous_week_partial_papers"] is True and hist["archive_weeks_dropped_partial"] == []
    x = {r["id"]: r for r in tr["instruments"]["papers"]}["x"]
    # baseline is the three full weeks only: mean 40, std 0 -> floored to 1 -> z = +20
    assert x["series"] == [40, 40, 40, 4, 60] and x["previous"] == 4
    assert x["baseline_weeks"] == 3 and x["mean"] == 40.0 and x["std"] == 0.0 and x["z"] == 20.0 and x["notable"] is True
    md = trends.render_trends_markdown(tr)
    assert "3 weeks with paper data" in md
    assert "Partial weeks excluded from the baseline (below 25% of the median weekly total): papers: 2026-10-04 to 2026-10-10 (5 papers vs median 50)" in md
    assert "'Last week' above is one of them." in md


def test_partial_week_filter_disabled_restores_damped_z(partial_week):
    tr = trends.compute_trends(partial_week, UNTIL, weeks=4, min_week_fraction=0)
    hist = tr["history"]
    assert hist["paper_weeks_with_data"] == 4 and hist["paper_weeks_dropped_partial"] == []
    x = {r["id"]: r for r in tr["instruments"]["papers"]}["x"]
    assert x["baseline_weeks"] == 4 and x["mean"] == 31.0
    assert x["std"] == pytest.approx(statistics.stdev([40, 40, 40, 4]), abs=0.01)
    assert x["z"] == pytest.approx((60 - 31) / statistics.stdev([40, 40, 40, 4]), abs=0.01) and x["z"] < 2 and x["notable"] is False
    assert "Partial weeks excluded" not in trends.render_trends_markdown(tr)
    with pytest.raises(ValueError):
        trends.compute_trends(partial_week, UNTIL, min_week_fraction=1.0)


def test_partial_week_rule_needs_two_weeks_to_compare(tmp_path):
    st = Store(tmp_path / "one.sqlite")
    _add_instrument_papers(st, "x", "X", 1, 2)  # a single tiny baseline week: nothing to compare against
    _add_instrument_papers(st, "x", "X", 0, 9)
    st.commit()
    tr = trends.compute_trends(st, UNTIL, weeks=4)
    assert tr["history"]["paper_weeks_with_data"] == 1 and tr["history"]["paper_weeks_dropped_partial"] == []
    # one-week baseline with min_baseline_weeks=1: std is undefined, rendered as n/a, z uses the floor
    tr1 = trends.compute_trends(st, UNTIL, weeks=4, min_baseline_weeks=1)
    x = {r["id"]: r for r in tr1["instruments"]["papers"]}["x"]
    assert x["mean"] == 2.0 and x["std"] is None and x["z"] == 7.0
    md = trends.render_trends_markdown(tr1)
    assert "1 week with paper data" in md and "| 9 | 2 | 2.0 ± n/a | +7.0 |" in md
    assert "baseline 2 ± n/a" in md  # corpus line


def test_conversion_not_rereported_on_preprint_reupsert(tmp_path):
    st = Store(tmp_path / "conv.sqlite")
    pre = "doi:10.1101/2026.01.01.1"
    art = "doi:10.1038/s41586-026-0001-1"
    st.upsert_paper(_paper(pre, _day(9), journal="biorxiv", kind="preprint", title="Old preprint", published_doi="10.1038/s41586-026-0001-1"))
    st.upsert_paper(_paper(art, _day(5), journal="Nature", title="Journal version"))
    _set_first_seen(st, pre, _day(9))
    _set_first_seen(st, art, _day(5))  # journal version published and first seen five weeks ago
    st.commit()
    assert trends.compute_trends(st, UNTIL, weeks=13)["conversions"] == []
    # the preprint is re-upserted this week (new version / overlap day): updated_at moves, published_doi unchanged
    st.upsert_paper(_paper(pre, _day(9), journal="biorxiv", kind="preprint", title="Old preprint v2", published_doi="10.1038/s41586-026-0001-1"))
    st.conn.execute("UPDATE papers SET updated_at=? WHERE id=?", (f"{_day(0)}T09:00:00Z", pre))
    st.commit()
    assert st.conn.execute("SELECT substr(updated_at,1,10) FROM papers WHERE id=?", (pre,)).fetchone()[0] == _day(0)
    assert trends.compute_trends(st, UNTIL, weeks=13)["conversions"] == []
    # but a journal version first seen this week is reported even if its pub_date is older
    st.conn.execute("UPDATE papers SET first_seen=? WHERE id=?", (f"{_day(0, 2)}T09:00:00Z", art))
    st.commit()
    conv = trends.compute_trends(st, UNTIL, weeks=13)["conversions"]
    assert [c["article_id"] for c in conv] == [art] and conv[0]["article_first_seen"] == _day(0, 2)


def test_keyword_query_failure_is_reported_as_warning(tmp_path):
    st = Store(tmp_path / "nofts.sqlite")
    _add_instrument_papers(st, "x", "X", 0, 6, first_seen_day=_day(0))
    st.conn.execute("DROP TABLE papers_fts")
    st.commit()
    tr = trends.compute_trends(st, UNTIL, subgroups=SUBGROUPS)
    assert len(tr["warnings"]) == 1 and tr["warnings"][0].startswith("subgroup spatial: keyword query failed")
    sg = {r["id"]: r for r in tr["subgroups"]}["spatial"]
    assert sg["current"] == 0  # keywords contributed nothing, topics matched nothing
    md = trends.render_trends_markdown(tr)
    assert "_Warnings:_\n- subgroup spatial: keyword query failed" in md
    json.dumps(tr)


def test_window_note_and_digest_window_note(synthetic):
    tr = trends.compute_trends(synthetic, UNTIL, weeks=4)
    md = trends.render_trends_markdown(tr)
    assert "Weeks are seven-day buckets ending 2026-10-17." in md and "digest tables" not in md
    note = trends.digest_window_note("2026-10-10", UNTIL)
    assert note == "The digest tables cover 2026-10-10 to 2026-10-17 (8 inclusive days), so their counts can differ from the seven-day figures in this section."
    md2 = trends.render_trends_markdown(tr, window_note=note)
    assert f"_{note}_" in md2
    assert "a window of its own" in trends.digest_window_note("bad", UNTIL)


def test_cli_rejects_bad_arguments_at_the_parser(tmp_path, monkeypatch, capsys):
    from litradar import cli

    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "unused.sqlite"))
    cases = [
        (["trends", "--weeks", "0"], "expected a positive integer, got '0'"),
        (["trends", "--until", "17/10/2026"], "expected YYYY-MM-DD, got '17/10/2026'"),
        (["trends", "--top", "x"], "expected a positive integer, got 'x'"),
    ]
    for argv, message in cases:
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "usage: litradar trends" in err and message in err and "Traceback" not in err


def test_partial_week_rule_with_two_weeks_uses_the_other_week(tmp_path):
    """Leave-one-out: with two baseline weeks a 20 % week is measured against the full one, not their average."""
    st = Store(tmp_path / "two-weeks.sqlite")
    _bulk_instrument_papers(st, "x", 2, 50)
    _bulk_instrument_papers(st, "x", 1, 10)  # 20 % of the other week -> partial
    _bulk_instrument_papers(st, "x", 0, 55)
    st.commit()
    hist = trends.compute_trends(st, UNTIL, weeks=4)["history"]
    assert hist["paper_weeks_with_data"] == 1 and [(d["week"], d["total"], d["median"]) for d in hist["paper_weeks_dropped_partial"]] == [(1, 10, 50.0)]
    assert hist["previous_week_partial_papers"] is True
    # 30 % of the other week is a slow week, not a partial one
    _add_instrument_papers(st, "x", "X", 1, 5, prefix="x-extra-")  # distinct ids: 10 + 5 = 15 papers
    st.commit()
    hist = trends.compute_trends(st, UNTIL, weeks=4)["history"]
    assert hist["paper_weeks_with_data"] == 2 and hist["paper_weeks_dropped_partial"] == []


def test_previous_week_only_partly_covered_by_the_runs_suppresses_week_over_week_movers(tmp_path):
    """Seen live: a corpus built by one `run --days 7` holds a single day of 'last week', and with no baseline the
    movers were ranked week-over-week against that bucket (every faller an artefact). The runs table says how much
    of the previous week was ever fetched; below seven days the tables wait."""
    st = Store(tmp_path / "cov.sqlite")
    _add_instrument_papers(st, "x", "X", 1, 1)  # the one covered day of 'last week' (2026-10-10)
    _add_instrument_papers(st, "x", "X", 0, 9)
    st.record_run("2026-10-17T00:00:00Z", "2026-10-10", "2026-10-17", {})
    st.commit()
    tr = trends.compute_trends(st, UNTIL, weeks=4)
    cov = tr["history"]["previous_week_coverage"]
    assert cov == {"covered_from": "2026-10-10", "days_covered": 1, "partial": True}
    assert tr["history"]["z_available_papers"] is False
    md = trends.render_trends_markdown(tr)
    assert "### Biggest risers and fallers" in md and "Suppressed: the runs that built this store cover 'last week'" in md
    assert "from 2026-10-10 only (1 of 7 days)" in md
    assert "### Biggest fallers" not in md and "| 9 | 1 |" not in md
    # an earlier run that covered the whole previous week lifts the suppression
    st.record_run("2026-10-10T00:00:00Z", "2026-09-01", "2026-10-10", {})
    st.commit()
    tr = trends.compute_trends(st, UNTIL, weeks=4)
    assert tr["history"]["previous_week_coverage"] == {"covered_from": "2026-09-01", "days_covered": 7, "partial": False}
    md = trends.render_trends_markdown(tr)
    assert "### Biggest risers" in md and "### Biggest fallers" in md and "Suppressed" not in md
    # no run recorded at all (a store built another way): nothing is assumed, the tables stay
    st2 = Store(tmp_path / "noruns.sqlite")
    _add_instrument_papers(st2, "x", "X", 1, 1)
    _add_instrument_papers(st2, "x", "X", 0, 9)
    st2.commit()
    tr2 = trends.compute_trends(st2, UNTIL, weeks=4)
    assert tr2["history"]["previous_week_coverage"] == {"covered_from": None, "days_covered": 7, "partial": False}
    assert "### Biggest fallers" in trends.render_trends_markdown(tr2)


def test_partial_coverage_with_a_baseline_keeps_the_tables_and_flags_last_week(synthetic):
    synthetic.record_run("2026-10-17T00:00:00Z", "2026-10-08", "2026-10-17", {})  # 3 of the 7 days of 2026-10-04..10
    synthetic.commit()
    tr = trends.compute_trends(synthetic, UNTIL)
    assert tr["history"]["previous_week_coverage"] == {"covered_from": "2026-10-08", "days_covered": 3, "partial": True}
    assert tr["history"]["z_available_papers"] is True
    md = trends.render_trends_markdown(tr)
    assert "### Biggest risers" in md and "### Biggest fallers" in md
    assert "'Last week' is only partly covered by the runs that built this store (from 2026-10-08, 3 of 7 days)" in md


def test_backfill_fills_the_baseline_by_pub_date_and_extends_coverage(tmp_path):
    """A backfill writes papers with old publication dates but today's first_seen: z-scores must come from the
    pub_date weeks, a recorded backfill must count as coverage of 'last week', and the new-entities list must say
    why it is omitted instead of claiming a first week of data."""
    st = Store(tmp_path / "bf.sqlite")
    today = f"{_day(0)}T12:00:00Z"
    for wk in (4, 3, 2, 1):  # baseline weeks by pub_date, 12 papers each naming the instrument ...
        for i in range(12):
            pid = f"doi:10.1000/bf-w{wk}-{i}"
            st.upsert_paper(_paper(pid, _day(wk, i % 7), journal="Backfill Journal", source="openalex"))
            st.replace_mentions(pid, [Mention(pid, "instrument", "bf-seq", "Backfilled Seq", "Vendor", "TCK", "sequencer", "methods", 1, "...")])
            st.conn.execute("UPDATE papers SET first_seen=? WHERE id=?", (today, pid))  # ... all ingested today
    # the live week comes from the same source as the backfill, so the baseline weeks are comparable (a live week fed
    # by other sources is the source-mix case, tested separately)
    _add_instrument_papers(st, "bf-seq", "Backfilled Seq", 0, 40, first_seen_day=_day(0), source="openalex")
    st.record_run(f"{_day(0)}T00:00:00Z", _day(0, 6), _day(0), {})  # the live run covers the current week only
    st.commit()
    tr = trends.compute_trends(st, UNTIL, weeks=4)
    row = {r["id"]: r for r in tr["instruments"]["papers"]}["bf-seq"]
    assert row["series"] == [12, 12, 12, 12, 40] and row["mean"] == 12.0 and row["z"] == 28.0 and row["notable"] is True
    hist = tr["history"]
    assert hist["paper_weeks_with_data"] == 4 and hist["z_available_papers"] is True and hist["paper_weeks_dropped_partial"] == []
    assert tr["cold_start"] is True and hist["ingested_this_week_only_with_history"] is True
    # no backfill recorded yet: the runs table alone says 'last week' was never fetched for
    assert hist["previous_week_coverage"]["partial"] is True and hist["coverage_sources"] == {"runs_since": _day(0, 6), "backfill_from": None}
    md = trends.render_trends_markdown(tr)
    assert "Every paper in this corpus was ingested this week although 4 earlier weeks hold publication-dated papers" in md
    assert "First week of data in this corpus" not in md and "| **Backfilled Seq** | instrument · papers | 40 | 12 |" in md
    assert "'Last week' is only partly covered" in md
    # a recorded backfill extends the coverage back to its first month
    st.conn.executescript(
        "CREATE TABLE IF NOT EXISTS backfill_progress (subgroup TEXT NOT NULL, month TEXT NOT NULL, country TEXT NOT NULL DEFAULT '', "
        "done_at TEXT, fetched INTEGER DEFAULT 0, cap INTEGER DEFAULT 0, PRIMARY KEY (subgroup, month, country));"
    )
    st.conn.execute("INSERT INTO backfill_progress (subgroup, month, country, done_at, fetched) VALUES ('spatial', '2026-08', '', 'now', 48)")
    st.commit()
    tr2 = trends.compute_trends(st, UNTIL, weeks=4)
    cov = tr2["history"]["previous_week_coverage"]
    assert cov == {"covered_from": "2026-08-01", "days_covered": 7, "partial": False}
    assert tr2["history"]["coverage_sources"] == {"runs_since": _day(0, 6), "backfill_from": "2026-08-01"}
    assert "'Last week' is only partly covered" not in trends.render_trends_markdown(tr2)
    assert trends._backfill_start(st.conn) == "2026-08-01"
    st.conn.execute("UPDATE backfill_progress SET month='garbage'")
    st.commit()
    assert trends._backfill_start(st.conn) is None  # an unusable month is ignored, not fatal
    json.dumps(tr2)
    st.close()


def _backfilled_baseline(st: Store, entity: str = "bf-seq", weeks: int = 4, per_week: int = 12) -> None:
    """OpenAlex-only baseline weeks by pub_date, all ingested today (a backfill), each naming one instrument."""
    today = f"{_day(0)}T12:00:00Z"
    for wk in range(weeks, 0, -1):
        for i in range(per_week):
            pid = f"doi:10.1000/mix-w{wk}-{i}"
            st.upsert_paper(_paper(pid, _day(wk, i % 7), journal="Backfill Journal", source="openalex"))
            st.replace_mentions(pid, [Mention(pid, "instrument", entity, "Backfilled Seq", "Vendor", "TCK", "sequencer", "methods", 1, "...")])
            st.conn.execute("UPDATE papers SET first_seen=? WHERE id=?", (today, pid))


def test_source_mix_step_suppresses_paper_z_and_movers_rank_by_z_only(tmp_path):
    """Seen live: a 13-week OpenAlex-only backfill under a multi-source live week printed 'z +13.9' for the corpus and 245
    notable movers, every one of them the step in coverage. Baseline weeks whose source mix does not cover the current
    week's are left out of the paper baseline; and with the paper z gone, the movers tables rank the archive family by z
    alone instead of putting its raw deltas above every paper row."""
    st = Store(tmp_path / "mix.sqlite")
    _backfilled_baseline(st)
    _add_instrument_papers(st, "bf-seq", "Backfilled Seq", 0, 10, first_seen_day=_day(0), source="openalex")  # 20 % of the live week
    _add_instrument_papers(st, "bf-seq", "Backfilled Seq", 0, 40, first_seen_day=_day(0), prefix="live-")  # europepmc: 80 %
    recs = [ArchiveRecord("sra", f"SRX{wk}-{i}", f"SRP{wk}", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", _day(wk, i % 7), "c", "t", "", "ilmn-novaseq-x", "Illumina", "ILMN")
            for wk in (4, 3, 2, 1) for i in range(100)]
    recs += [ArchiveRecord("sra", f"SRX0-{i}", "SRP0", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", _day(0, i % 7), "c", "t", "", "ilmn-novaseq-x", "Illumina", "ILMN") for i in range(300)]
    st.upsert_archive_records(recs)
    st.record_run(f"{_day(0)}T00:00:00Z", _day(0, 6), _day(0), {})
    st.commit()
    tr = trends.compute_trends(st, UNTIL, weeks=4)
    hist = tr["history"]
    dropped = hist["paper_weeks_dropped_source_mix"]
    assert [d["week"] for d in dropped] == [1, 2, 3, 4] and all(d["missing"] == ["europepmc"] for d in dropped)
    assert dropped[0]["covered_share"] == 0.2 and dropped[0]["total"] == 12
    assert hist["current_week_sources"] == {"europepmc": 40, "openalex": 10}
    assert hist["paper_weeks_with_data"] == 0 and hist["paper_weeks_holding_papers"] == 4
    assert hist["z_available_papers"] is False and hist["z_available_archive"] is True
    assert hist["previous_week_partial_papers"] is True  # 'last week' is one of the non-comparable weeks
    assert tr["cold_start"] is True and hist["ingested_this_week_only_with_history"] is True  # a backfill, not a first week
    row = {r["id"]: r for r in tr["instruments"]["papers"]}["bf-seq"]
    assert row["series"] == [12, 12, 12, 12, 50] and row["z"] is None and row["mean"] is None and row["notable"] is False
    assert tr["totals"]["papers"]["z"] is None and tr["totals"]["archive_experiments"]["z"] == 200.0
    assert [(r["group"], r["id"]) for r in tr["notable"]] == [("instrument_experiments", "Illumina NovaSeq X Plus")]
    risers, fallers = trends.top_movers(tr)
    assert [(r["group"], r["id"]) for r in risers] == [("instrument_experiments", "Illumina NovaSeq X Plus")] and fallers == []
    md = trends.render_trends_markdown(tr)
    assert "_Source mix: 4 baseline weeks (2026-09-13 to 2026-10-10) hold papers but their sources are not this week's (openalex supplies 20% of this week's 50 papers; europepmc is absent or far smaller there)" in md
    assert "z-scores for the paper family are suppressed until 3 comparable weeks exist" in md and "'Last week' is one of them" in md
    assert "Fewer than 3 comparable weeks of paper history: z-scores for that family are suppressed, and its rows are left out of the movers tables" in md
    assert "**Corpus:** 50 (last week 12) papers" in md  # no baseline figure, no z for the corpus line
    assert "| **Illumina NovaSeq X Plus** | instrument · experiments | 300 | 100 | 100.0 ± 0.0 | +200.0 |" in md
    assert "Backfilled Seq" not in md[md.index("### Biggest risers"): md.index("### New this week")]
    assert "Every paper in this corpus was ingested this week although 4 earlier weeks hold publication-dated papers" in md
    assert "are by publication date and are not affected by the ingestion day; the source-mix note under the header says which backfilled weeks are comparable" in md
    assert "First week of data in this corpus" not in md
    json.dumps(tr)
    # the rule is a parameter: 0 disables it and the old (coverage-measuring) z comes back
    tr0 = trends.compute_trends(st, UNTIL, weeks=4, min_source_coverage=0)
    assert tr0["history"]["paper_weeks_dropped_source_mix"] == [] and tr0["history"]["z_available_papers"] is True
    assert {r["id"]: r for r in tr0["instruments"]["papers"]}["bf-seq"]["z"] == 38.0
    assert "Source mix:" not in trends.render_trends_markdown(tr0)
    with pytest.raises(ValueError):
        trends.compute_trends(st, UNTIL, min_source_coverage=1.5)
    # a live week fed by the same source is comparable: nothing is dropped
    st2 = Store(tmp_path / "same.sqlite")
    _backfilled_baseline(st2)
    _add_instrument_papers(st2, "bf-seq", "Backfilled Seq", 0, 40, first_seen_day=_day(0), source="openalex")
    st2.commit()
    tr2 = trends.compute_trends(st2, UNTIL, weeks=4)
    assert tr2["history"]["paper_weeks_dropped_source_mix"] == [] and tr2["history"]["z_available_papers"] is True
    # a minor new source (below 25 % of the live week) does not make the baseline incomparable
    _add_instrument_papers(st2, "bf-seq", "Backfilled Seq", 0, 5, first_seen_day=_day(0), prefix="minor-")  # europepmc: 5 of 45
    st2.commit()
    tr3 = trends.compute_trends(st2, UNTIL, weeks=4)
    assert tr3["history"]["paper_weeks_dropped_source_mix"] == [] and tr3["history"]["z_available_papers"] is True
    st.close()
    st2.close()


def test_source_mix_z_returns_once_enough_comparable_weeks_exist(tmp_path):
    """Three multi-source weeks behind the live week are comparable; the OpenAlex-only weeks before them are not, and
    the z is computed against the comparable weeks only."""
    st = Store(tmp_path / "heal.sqlite")
    _backfilled_baseline(st, weeks=6)  # weeks 6..1: 12 openalex papers each
    for wk in (3, 2, 1):  # the last three baseline weeks also carry 36 europepmc papers (75 %) naming the instrument
        _add_instrument_papers(st, "bf-seq", "Backfilled Seq", wk, 36, prefix=f"live{wk}-")
    _add_instrument_papers(st, "bf-seq", "Backfilled Seq", 0, 12, first_seen_day=_day(0), source="openalex")
    _add_instrument_papers(st, "bf-seq", "Backfilled Seq", 0, 60, first_seen_day=_day(0), prefix="live-")
    st.commit()
    tr = trends.compute_trends(st, UNTIL, weeks=6)
    hist = tr["history"]
    assert [d["week"] for d in hist["paper_weeks_dropped_source_mix"]] == [4, 5, 6]
    assert hist["paper_weeks_with_data"] == 3 and hist["z_available_papers"] is True and hist["previous_week_partial_papers"] is False
    row = {r["id"]: r for r in tr["instruments"]["papers"]}["bf-seq"]
    assert row["series"] == [12, 12, 12, 48, 48, 48, 72] and row["baseline_weeks"] == 3 and row["mean"] == 48.0 and row["z"] == 24.0
    md = trends.render_trends_markdown(tr)
    assert "Source mix: 3 baseline weeks (2026-08-30 to 2026-09-19)" in md and "computed against the 3 comparable weeks only" in md
    assert "'Last week' is one of them" not in md
    st.close()


def test_archive_last_week_coverage_comes_from_the_runs_not_the_paper_backfill(tmp_path):
    """A recorded backfill covers papers, never archive records: with a backfill back to August and a first run three
    days into 'last week', the paper family is fully covered while the archive family holds 3 of 7 days."""
    st = Store(tmp_path / "arch.sqlite")
    for wk in (3, 2, 1):
        _add_instrument_papers(st, "x", "X", wk, 10)
    _add_instrument_papers(st, "x", "X", 0, 30, first_seen_day=_day(0))
    st.upsert_archive_records([ArchiveRecord("sra", f"SRX1-{i}", "SRP1", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", _day(1, i % 3), "c", "t", "") for i in range(20)]
                              + [ArchiveRecord("sra", f"SRX0-{i}", "SRP0", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", _day(0, i % 7), "c", "t", "") for i in range(50)])
    st.conn.executescript(
        "CREATE TABLE IF NOT EXISTS backfill_progress (subgroup TEXT NOT NULL, month TEXT NOT NULL, country TEXT NOT NULL DEFAULT '', "
        "done_at TEXT, fetched INTEGER DEFAULT 0, cap INTEGER DEFAULT 0, PRIMARY KEY (subgroup, month, country));"
        "INSERT INTO backfill_progress (subgroup, month, country, done_at, fetched) VALUES ('spatial', '2026-08', '', 'now', 30);"
    )
    st.record_run(f"{_day(0)}T00:00:00Z", _day(1, 2), _day(0), {})  # first run started 2026-10-08: 3 of the 7 days of 10-04..10-10
    st.commit()
    tr = trends.compute_trends(st, UNTIL, weeks=4)
    hist = tr["history"]
    assert hist["previous_week_coverage"] == {"covered_from": "2026-08-01", "days_covered": 7, "partial": False}
    assert hist["previous_week_coverage_archive"] == {"covered_from": "2026-10-08", "days_covered": 3, "partial": True}
    assert hist["z_available_papers"] is True and hist["z_available_archive"] is False
    assert trends.week_over_week_comparable(tr, "papers") is True and trends.week_over_week_comparable(tr, "archive") is False
    md = trends.render_trends_markdown(tr)
    assert "'Last week' is only partly covered by the runs that built this store (from 2026-10-08, 3 of 7 days) for archive records; read the 'Last week' column as a lower bound." in md
    assert "for papers; read the 'Last week'" not in md
    # the z-ranked tables hold the paper family only (archive z is suppressed)
    risers, _ = trends.top_movers(tr)
    assert {r["group"] for r in risers} == {"instrument_papers", "journal"} and "Illumina NovaSeq X Plus" not in md[md.index("### Biggest risers"):]
    # with no z anywhere, the week-over-week fallback keeps the paper rows and leaves the archive rows out, saying why
    tr1 = trends.compute_trends(st, UNTIL, weeks=4, min_baseline_weeks=5)
    assert tr1["history"]["z_available_papers"] is False and tr1["history"]["z_available_archive"] is False
    risers, fallers = trends.top_movers(tr1)
    assert {r["group"] for r in risers} == {"instrument_papers", "journal"} and all(r["z"] is None for r in risers)
    md1 = trends.render_trends_markdown(tr1)
    assert "### Biggest risers\n" in md1 and "archive rows are left out because the runs that built this store cover 'last week' (2026-10-04 to 2026-10-10) from 2026-10-08 only (3 of 7 days) for archive records" in md1
    # and when every family is incomplete the tables wait, naming each reason
    st.conn.execute("DELETE FROM backfill_progress")
    st.commit()
    md2 = trends.render_trends_markdown(trends.compute_trends(st, UNTIL, weeks=4, min_baseline_weeks=5))
    assert "### Biggest risers and fallers\n" in md2 and "Suppressed: the runs that built this store cover 'last week' (2026-10-04 to 2026-10-10) from 2026-10-08 only (3 of 7 days) for papers and archive records, and with no baseline" in md2
    assert "_'Last week' is only partly covered by the runs that built this store (from 2026-10-08, 3 of 7 days) for papers and archive records; read the 'Last week' column as a lower bound._" in md2
    st.close()


def test_conversions_note_replaces_the_table_inside_the_digest(synthetic):
    tr = trends.compute_trends(synthetic, UNTIL, weeks=4)
    assert len(tr["conversions"]) == 1
    md = trends.render_trends_markdown(tr, conversions_note="2 conversions in this digest's window, listed under the top innovations above.")
    section = md[md.index("### Preprints that became journal articles"):]
    assert "_2 conversions in this digest's window, listed under the top innovations above._" in section
    assert "| Preprint |" not in section and "Preprint P" not in section and "median preprint-to-journal lag" not in section
    assert "[Preprint P]" in trends.render_trends_markdown(tr)  # the standalone `litradar trends` output keeps its table


def test_mixed_history_ranks_z_scored_rows_above_rows_without_a_baseline(tmp_path):
    """Paper families with a baseline, archive family without one: the movers tables are z-ranked and the archive
    rows (z n/a, week-over-week deltas in the thousands) only fill the places left below the z-scored rows. Before
    this rule a store with 13 weeks of paper history and one week of archive history showed twenty archive rows,
    ordered by a delta the table does not print, and none of the notable paper movers it had just announced."""
    st = Store(tmp_path / "mixed.sqlite")
    for wk, n in zip([4, 3, 2, 1], [8, 12, 8, 12]):
        _add_instrument_papers(st, "riser", "Riser 3000", wk, n)
    _add_instrument_papers(st, "riser", "Riser 3000", 0, 20)  # z = +4.33, notable
    for wk in [4, 3, 2, 1]:
        _add_instrument_papers(st, "faller", "Faller 1000", wk, 10)  # 10 x4 then 0 -> z = -10, notable
    # archive records in last week and this week only: one week of archive history (< 3) -> z suppressed for the family
    big = [ArchiveRecord("sra", f"SRX1-{i}", "SRP1", "Big Box", "ILLUMINA", "WGS", _day(1, i % 7), "c", "t", "Homo sapiens", "big-box", "Vendor", "VND") for i in range(1000)]
    big += [ArchiveRecord("sra", f"SRX0-{i}", "SRP0", "Big Box", "ILLUMINA", "WGS", _day(0, i % 7), "c", "t", "Homo sapiens", "big-box", "Vendor", "VND") for i in range(5000)]
    old = [ArchiveRecord("sra", f"SRY1-{i}", "SRP2", "Old Box", "ILLUMINA", "WGS", _day(1, i % 7), "c", "t", "Homo sapiens", "old-box", "Vendor", "VND") for i in range(2000)]
    st.upsert_archive_records(big + old)
    st.commit()
    tr = trends.compute_trends(st, UNTIL, weeks=4)
    hist = tr["history"]
    assert hist["z_available_papers"] is True and hist["z_available_archive"] is False
    rows = {r["id"]: r for r in tr["instruments"]["experiments"]}
    assert rows["Big Box"]["z"] is None and rows["Big Box"]["delta"] == 4000  # the raw delta that used to outrank every z-score
    assert rows["Old Box"]["z"] is None and rows["Old Box"]["delta"] == -2000
    risers, fallers = trends.top_movers(tr, n=10)
    assert [r["id"] for r in risers] == ["riser", "Big Box"] and [r["id"] for r in fallers] == ["faller", "Old Box"]
    assert risers[0]["z"] == pytest.approx(4.33, abs=0.01) and fallers[0]["z"] == -10.0
    # no place left below the z-scored rows: the family without a baseline does not appear at all
    risers1, fallers1 = trends.top_movers(tr, n=1)
    assert [r["id"] for r in risers1] == ["riser"] and [r["id"] for r in fallers1] == ["faller"]
    md = trends.render_trends_markdown(tr, top_n=10)
    assert ("_Fewer than 3 weeks of archive history: z-scores for that family are suppressed. Its rows (z n/a) are listed "
            "after every z-scored row in the tables below, ordered by week-over-week change among themselves, so they appear "
            "only when fewer than 10 z-scored risers or fallers qualify._") in md
    assert md.index("| **Riser 3000** |") < md.index("| Big Box |") and md.index("| **Faller 1000** |") < md.index("| Old Box |")
    md1 = trends.render_trends_markdown(tr, top_n=1)
    assert "| **Riser 3000** |" in md1 and "| Big Box |" not in md1 and "| Old Box |" not in md1
    json.dumps(tr)
    st.close()
