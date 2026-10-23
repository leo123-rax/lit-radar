"""The unattended reading queue, the facts format it commits, and the hosted import of those facts.

Nothing here touches a publisher, a browser or a real session: the queue's reader is a stub, the
session check is injected, the desktop notification is mocked, and the only database is a temporary
one. The two claims that matter are asserted directly: a committed record carries no article text,
and an imported record changes the hosted database exactly where it should.
"""
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from litradar import reader
from litradar.commands import read_queue as rq_cmd
from litradar.commands import reads_import as ri_cmd
from litradar.config import Settings
from litradar.models import Mention, Paper
from litradar.store import Store

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


# ---------------------------------------------------------------------------- fixtures and fakes
INDEX = {
    "window": {"start": "2026-04-26", "end": "2026-10-23"},
    "records": [
        {"id": "doi:10.1038/s41586-026-11250-8", "doi": "10.1038/s41586-026-11250-8", "t": "Antiviral immunity",
         "j": "Nature", "d": "2026-10-21", "s": 6.25, "tier": "tier1"},
        {"id": "doi:10.1126/science.aeh2982", "doi": "10.1126/science.aeh2982", "t": "Atomic scaffolding",
         "j": "Science", "d": "2026-10-20", "s": 5.94},
        {"id": "doi:10.1038/s41592-026-03273-w", "doi": "10.1038/s41592-026-03273-w", "t": "Causal cascades",
         "j": "Nature Methods", "d": "2026-10-21", "s": 9.0},          # not in the reading list
        {"id": "doi:10.1038/s41586-026-10001-1", "doi": "10.1038/s41586-026-10001-1", "t": "Old news",
         "j": "Nature", "d": "2026-09-01", "s": 8.0},                   # outside the window
        {"id": "doi:10.1016/j.cell.2026.01.001", "doi": "10.1016/j.cell.2026.01.001", "t": "Elsewhere",
         "j": "Cell", "d": "2026-10-21", "s": 7.0},                     # no subscription
        {"id": "biorxiv:1", "t": "No DOI", "j": "Nature", "d": "2026-10-22", "s": 9.9},  # unusable
    ],
}
TODAY = date(2026, 10, 23)


def settings_for(tmp_path, **radar):
    s = Settings(cache_dir=tmp_path / ".cache", db_path=tmp_path / "radar.sqlite",
                 config_dir=ROOT / "config", radar=radar)
    return s


class FakeResult:
    """What `subscription.read_one` hands back, with only the attributes the queue reads."""

    def __init__(self, doi, publisher="nature", methods_chars=20000, error="", **kw):
        self.doi, self.publisher, self.error = doi, publisher, error
        self.paper_id = f"doi:{doi}"
        self.title, self.journal, self.pub_date = kw.get("title", "A paper"), kw.get("journal", "Nature"), "2026-10-21"
        self.methods_chars = methods_chars
        self.url = kw.get("url", f"https://www.nature.com/articles/{doi.split('/')[-1]}")
        for k, v in kw.items():
            setattr(self, k, v)


class FakeStore:
    def __init__(self, mentions=None):
        self._mentions = mentions if mentions is not None else [
            {"kind": "instrument", "entity_id": "ilmn-novaseq-x", "entity_name": "NovaSeq X Plus",
             "section": "methods", "count": 3, "alias": "NovaSeq X", "snippet": "x" * 900},
            {"kind": "company", "entity_id": "illumina", "entity_name": "Illumina",
             "section": "coi", "count": 1, "alias": "", "snippet": "short"},
        ]
        self.closed = False

    def mentions_for(self, paper_id):
        return list(self._mentions)

    def close(self):
        self.closed = True


class FakePipe:
    def __init__(self, mentions=None):
        self.store = FakeStore(mentions)


def queue(tmp_path, monkeypatch, reads=None, status=None, **kw):
    """Run the queue with everything injected; returns the report."""
    monkeypatch.delenv("LITRADAR_READ_CAP", raising=False)
    monkeypatch.delenv("LITRADAR_READ_INTERVAL", raising=False)
    settings = kw.pop("settings", None) or settings_for(tmp_path)
    calls = []

    def read_fn(pipe, s, target, keep_text=False, store=True):
        calls.append(target)
        return (reads or {}).get(target) or FakeResult(target)

    rep = reader.run_queue(
        settings, today=TODAY, url="https://example.test/search-index.json",
        opener=lambda url: json.dumps(INDEX), status_fn=lambda s: status or {"nature": True, "science": True},
        read_fn=read_fn, pipe_factory=FakePipe, notifier=lambda msg: True, log_line=lambda line: None,
        out_dir=kw.pop("out_dir", None) or tmp_path / "reads", **kw)
    rep.calls = calls
    return rep


# ---------------------------------------------------------------------------- selection
def test_journal_map_routes_only_the_configured_titles():
    s = Settings()
    jmap = reader.journal_map(s)
    assert reader.publisher_for_journal("Nature", jmap) == "nature"
    assert reader.publisher_for_journal("science", jmap) == "science"   # case-insensitive
    assert reader.publisher_for_journal("Nature Methods", jmap) == ""   # a separate subscription
    assert reader.publisher_for_journal("", jmap) == ""
    # the documented opt-in lists exist for the reader to copy from, and are NOT on by default
    assert "Nature Methods" in reader.NATURE_RESEARCH_TITLES and "Science Advances" in reader.SCIENCE_FAMILY_TITLES
    assert "nature methods" not in jmap

    custom = Settings(radar={"reading": {"journals": {"nature": ["Nature", "Nature Methods"]}}})
    assert reader.publisher_for_journal("Nature Methods", reader.journal_map(custom)) == "nature"


def test_candidates_are_in_window_subscribed_and_ordered_by_score():
    cands = reader.candidates_from_index(INDEX, reader.journal_map(Settings()), days=7, today=TODAY)
    assert [c.doi for c in cands] == ["10.1038/s41586-026-11250-8", "10.1126/science.aeh2982"]
    assert [c.publisher for c in cands] == ["nature", "science"]
    assert cands[0].score == 6.25 and cands[0].pub_date == "2026-10-21"


def test_candidates_from_the_local_database_match_the_index_path(tmp_path):
    db = tmp_path / "r.sqlite"
    st = Store(db)
    for doi, journal, pub_date, score in (("10.1038/a", "Nature", "2026-10-21", 5.0),
                                          ("10.1126/b", "Science", "2026-10-20", 7.0),
                                          ("10.1038/c", "Nature Methods", "2026-10-21", 9.0)):
        pid = f"doi:{doi}"
        st.upsert_paper(Paper(id=pid, source="x", kind="article", title="t", doi=doi, journal=journal, pub_date=pub_date))
        st.save_score(pid, score, None, score, "")
    st.commit()
    st.close()
    cands = reader.candidates_from_db(db, reader.journal_map(Settings()), days=7, today=TODAY)
    assert [(c.doi, c.score) for c in cands] == [("10.1126/b", 7.0), ("10.1038/a", 5.0)]


def test_the_done_set_covers_the_cache_file_and_the_committed_records(tmp_path):
    reads = tmp_path / "reads"
    reads.mkdir()
    (reads / "2026-10-22.jsonl").write_text(
        json.dumps({"doi": "10.1038/s41586-026-11250-8", "status": "ok"}) + "\n"
        + json.dumps({"doi": "10.1038/preview-1", "status": "preview"}) + "\n", encoding="utf-8")
    cache = tmp_path / "done.json"
    cache.write_text(json.dumps({"dois": ["10.1126/science.aeh2982"]}), encoding="utf-8")
    done = reader.DoneSet(cache, reads)
    assert "10.1038/S41586-026-11250-8" in done and "10.1126/science.aeh2982" in done
    assert "10.1038/preview-1" not in done      # a preview is not finished
    assert done.ready("10.1038/preview-1", TODAY) is True
    assert done.ready("10.1126/science.aeh2982", TODAY) is False


def test_the_queue_skips_what_is_already_read_and_writes_one_file_a_day(tmp_path, monkeypatch):
    out = tmp_path / "reads"
    out.mkdir()
    (out / "2026-10-20.jsonl").write_text(json.dumps({"doi": "10.1126/science.aeh2982", "status": "ok"}) + "\n",
                                          encoding="utf-8")
    rep = queue(tmp_path, monkeypatch, out_dir=out)
    assert rep.calls == ["10.1038/s41586-026-11250-8"]
    assert rep.out_file == str(out / "2026-10-23.jsonl")
    assert len(reader.iter_records([out / "2026-10-23.jsonl"])) == 1
    # a second run is a no-op: the DOI is in the done set now
    rep2 = queue(tmp_path, monkeypatch, out_dir=out)
    assert rep2.calls == [] and rep2.read == [] and not rep2.out_file


def test_dry_run_plans_without_reading_or_writing(tmp_path, monkeypatch):
    rep = queue(tmp_path, monkeypatch, dry_run=True)
    assert len(rep.planned) == 2 and rep.calls == [] and rep.read == []
    assert not (tmp_path / "reads").exists()
    assert any("dry run" in n for n in rep.notes)


def test_the_limit_and_the_daily_cap_both_bound_the_run(tmp_path, monkeypatch):
    rep = queue(tmp_path, monkeypatch, limit=1)
    assert len(rep.calls) == 1 and rep.cap["cap_per_day"] == 20

    # the ledger already used 19 of 20 today -> one read left, whatever --limit says
    monkeypatch.setenv("LITRADAR_READ_CAP", "20")
    other = tmp_path / "capped"
    settings = settings_for(other)
    ledger_path = settings.cache_dir / "read-ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    noon = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc).timestamp()
    ledger_path.write_text(json.dumps({TODAY.isoformat(): 19}), encoding="utf-8")
    rep = reader.run_queue(
        settings, today=TODAY, limit=10, url="u", opener=lambda url: json.dumps(INDEX),
        status_fn=lambda s: {"nature": True, "science": True}, clock=lambda: noon,
        read_fn=lambda pipe, s, target, keep_text=False, store=True: FakeResult(target),
        pipe_factory=FakePipe, notifier=lambda m: True, log_line=lambda line: None, out_dir=other / "reads")
    assert rep.cap["used_today"] == 19 and rep.cap["remaining"] == 1 and len(rep.planned) == 1


def test_a_full_ledger_reads_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("LITRADAR_READ_CAP", raising=False)
    settings = settings_for(tmp_path)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / "read-ledger.json").write_text(json.dumps({TODAY.isoformat(): 20}), encoding="utf-8")
    rep = queue(tmp_path, monkeypatch, settings=settings)
    assert rep.calls == [] and rep.planned == [] and rep.cap["remaining"] == 0


# ---------------------------------------------------------------------------- no session
def test_a_missing_session_skips_the_run_and_notifies_the_desktop(tmp_path, monkeypatch):
    seen = []
    rep = reader.run_queue(
        settings_for(tmp_path), today=TODAY, url="u", opener=lambda url: json.dumps(INDEX),
        status_fn=lambda s: {"nature": False, "science": True},
        read_fn=lambda *a, **k: pytest.fail("nothing may be read without a session"),
        pipe_factory=FakePipe, notifier=lambda msg: seen.append(msg) or True,
        log_line=lambda line: None, out_dir=tmp_path / "reads")
    assert rep.missing_sessions == ["nature"] and rep.read == []
    assert seen and "litradar login nature" in seen[0]
    assert any("litradar login nature" in n for n in rep.notes)
    assert not (tmp_path / "reads").exists()


def test_the_notification_shells_out_to_osascript_only_on_macos():
    calls = []
    assert reader.notify_macos("run litradar login nature", runner=lambda *a, **k: calls.append(a), platform="darwin")
    assert calls and calls[0][0][0] == "osascript"
    assert 'display notification "run litradar login nature" with title "lit-radar"' in calls[0][0][2]
    calls.clear()
    assert reader.notify_macos("x", runner=lambda *a, **k: calls.append(a), platform="linux") is False
    assert calls == []
    # quotes and newlines from any upstream string can never close the AppleScript literal
    reader.notify_macos('a" & do shell script "boom\n', runner=lambda *a, **k: calls.append(a), platform="darwin")
    script = calls[0][0][2]
    assert script.count('"') == 4 and "\n" not in script


# ---------------------------------------------------------------------------- the facts record
def test_the_committed_record_carries_facts_and_no_article_text(tmp_path, monkeypatch):
    rep = queue(tmp_path, monkeypatch, limit=1)
    rec = reader.iter_records([Path(rep.out_file)])[0]
    assert rec["schema"] == reader.SCHEMA_NAME and rec["status"] == "ok"
    assert rec["doi"] == "10.1038/s41586-026-11250-8" and rec["journal"] == "Nature" and rec["publisher"] == "nature"
    assert rec["methods_chars"] == 20000 and rec["sections"] == ["coi", "methods"]
    assert rec["instruments"] == [{"id": "ilmn-novaseq-x", "name": "NovaSeq X Plus"}]
    assert rec["companies"] == [{"id": "illumina", "name": "Illumina"}]
    assert [(m["kind"], m["entity_id"], m["section"], m["count"], m["alias"]) for m in rec["mentions"]] == [
        ("company", "illumina", "coi", 1, ""), ("instrument", "ilmn-novaseq-x", "methods", 3, "NovaSeq X")]
    # no text: no section bodies, no snippets at all by default, nothing long anywhere
    assert reader.record_is_clean(rec)
    assert not any("snippet" in m for m in rec["mentions"])
    assert all(len(v) <= reader.SNIPPET_MAX for v in rec.values() if isinstance(v, str))
    assert "x" * 300 not in json.dumps(rec)


def test_snippets_are_opt_in_and_clipped_to_the_ceiling(tmp_path, monkeypatch):
    rep = queue(tmp_path, monkeypatch, limit=1, keep_snippets=True)
    rec = reader.iter_records([Path(rep.out_file)])[0]
    snippets = [m["snippet"] for m in rec["mentions"] if "snippet" in m]
    assert snippets and max(len(s) for s in snippets) == reader.SNIPPET_MAX
    assert reader.record_is_clean(rec)
    assert reader.record_is_clean({"mentions": [{"snippet": "y" * (reader.SNIPPET_MAX + 1)}]}) is False
    assert reader.record_is_clean({"methods": "the article"}) is False


# ---------------------------------------------------------------------------- previews and supplements
def test_an_accelerated_preview_is_recorded_deferred_and_re_queued(tmp_path, monkeypatch):
    doi = "10.1038/s41586-026-11250-8"
    preview = FakeResult(doi, methods_chars=0,
                         error="accelerated article preview: the edited version is not published yet")
    rep = queue(tmp_path, monkeypatch, limit=1, reads={doi: preview})
    assert rep.read == [] and rep.deferred and rep.deferred[0]["status"] == "preview"
    assert rep.deferred[0]["retry_after"] == "2026-10-30" and rep.deferred[0]["attempts"] == 1
    rec = reader.iter_records([Path(rep.out_file)])[0]
    assert rec["status"] == "preview" and rec["mentions"] == [] and rec["methods_chars"] == 0
    assert rec["instruments"] == [] and rec["sections"] == []

    # the same day: the preview is not read again (the next candidate is), and no second preview record
    again = queue(tmp_path, monkeypatch, limit=1, reads={doi: preview})
    assert doi not in again.calls and again.calls == ["10.1126/science.aeh2982"]
    assert [r["doi"] for r in reader.iter_records([Path(rep.out_file)])].count(doi) == 1
    # a week later the DOI is back in the queue, and the edited version finishes it
    done = reader.DoneSet(settings_for(tmp_path).cache_dir / "reads" / "done.json", tmp_path / "reads")
    assert done.ready(doi, date(2026, 10, 29)) is False and done.ready(doi, date(2026, 10, 30)) is True
    assert done.retry[doi]["reason"] == "preview"


def test_a_preview_stops_being_re_queued_after_three_sightings(tmp_path):
    done = reader.DoneSet(tmp_path / "done.json")
    for n in range(reader.PREVIEW_MAX_ATTEMPTS):
        done.defer("10.1038/x", days=7, today=TODAY + timedelta(days=7 * n))
    assert done.retry["10.1038/x"]["attempts"] == reader.PREVIEW_MAX_ATTEMPTS
    assert done.ready("10.1038/x", date(2027, 1, 1)) is False


def test_methods_in_the_supplement_finish_the_doi_and_keep_the_link(tmp_path, monkeypatch):
    doi = "10.1126/science.aeh2982"
    result = FakeResult(doi, publisher="science", journal="Science", methods_chars=450,
                        methods_in_supplement=True, supplement_url="https://www.science.org/doi/suppl/x/media")
    rep = queue(tmp_path, monkeypatch, limit=2, reads={doi: result})
    recs = {r["doi"]: r for r in reader.iter_records([Path(rep.out_file)])}
    rec = recs[doi]
    assert rec["status"] == "supplement_only" and rec["methods_in_supplement"] is True
    assert rec["supplement_url"] == "https://www.science.org/doi/suppl/x/media"
    assert rec["methods_chars"] == 450 and rec["mentions"]           # thin, but real facts
    done = reader.DoneSet(settings_for(tmp_path).cache_dir / "reads" / "done.json", tmp_path / "reads")
    assert doi in done                                               # finished: the supplement is not read here


SCIENCE_SUPPL_HTML = """<html><body><h1>Paper</h1>
<section id="sec-1"><h2>Materials and Methods</h2><p>See the supplement.</p></section>
<a href="/doi/10.1126/science.aeh2982#supplementary-materials">Supplementary Materials</a>
<a href="/doi/suppl/10.1126/science.aeh2982/suppl_file/sm.pdf">Download the PDF</a></body></html>"""
NATURE_SUPPL_HTML = """<html><body><h1 class="c-article-title">Paper</h1>
<a href="/articles/x#ref1">References</a>
<section data-title="Supplementary information"><a href="/articles/x/MOESM1_ESM.pdf">Supplementary Information</a></section>
</body></html>"""


def test_the_supplement_link_is_captured_from_either_publisher_without_fetching_it():
    url = reader.extract_supplement_url(SCIENCE_SUPPL_HTML, "science", base_url="https://www.science.org/doi/10.1126/science.aeh2982")
    assert url == "https://www.science.org/doi/10.1126/science.aeh2982#supplementary-materials"
    nat = reader.extract_supplement_url(NATURE_SUPPL_HTML, "nature", base_url="https://www.nature.com/articles/x")
    assert nat == "https://www.nature.com/articles/x/MOESM1_ESM.pdf"
    assert reader.extract_supplement_url("<html><body><a href='/x'>Figures</a></body></html>", "nature") == ""
    assert reader.extract_supplement_url("", "nature") == ""
    # the result's own field wins; page HTML is only a fallback, and nothing is ever downloaded
    assert reader.supplement_url_for(FakeResult("10.1038/a", supplement_url="https://example.org/s")) == "https://example.org/s"
    assert reader.supplement_url_for(FakeResult("10.1038/a", html=NATURE_SUPPL_HTML, url="https://www.nature.com/articles/x")) \
        == "https://www.nature.com/articles/x/MOESM1_ESM.pdf"
    assert reader.supplement_url_for(FakeResult("10.1038/a")) == ""


# ---------------------------------------------------------------------------- read --json -> facts
READ_JSON_OK = {
    "target": "10.1038/s41586-026-11143-w", "publisher": "nature", "doi": "10.1038/s41586-026-11143-w",
    "title": "De novo design of cysteine proteases", "journal": "Nature", "pub_date": "2026-10-14",
    "paper_id": "doi:10.1038/s41586-026-11143-w", "paywalled": False, "methods_chars": 29807,
    "instruments": ["NovaSeq X Plus", "Chromium"], "companies": ["Illumina"], "score": 5.62,
    "stored_as": "facts_only", "error": "",
    "methods": "Libraries were sequenced on a NovaSeq X Plus. " * 400,     # the text that must be dropped
}
READ_JSON_FAILED = {"target": "10.1038/x", "doi": "10.1038/x", "methods_chars": 0, "instruments": [], "companies": [],
                    "error": "no full text returned: no Methods section found. Run `litradar login nature` first, then retry."}
READ_JSON_PREVIEW = dict(READ_JSON_FAILED, doi="10.1038/y",
                         error="accelerated article preview: no Methods on the page yet")


def test_read_json_lines_convert_to_facts_without_any_text():
    names = reader.entity_ids_by_name(Settings(config_dir=ROOT / "config"))
    rec = reader.convert_read_json(READ_JSON_OK, names)
    assert reader.record_is_clean(rec) and "methods" not in rec and "NovaSeq X Plus." not in json.dumps(rec)
    assert rec["status"] == "ok" and rec["doi"] == "10.1038/s41586-026-11143-w" and rec["methods_chars"] == 29807
    assert {i["id"] for i in rec["instruments"]} == {"ilmn-novaseq-x-plus", "txg-chromium"}
    assert rec["companies"] == [{"id": "illumina", "name": "Illumina"}]
    assert {m["section"] for m in rec["mentions"]} == {"methods"}
    assert reader.convert_read_json(READ_JSON_FAILED, names) is None
    preview = reader.convert_read_json(READ_JSON_PREVIEW, names)
    assert preview["status"] == "preview" and preview["mentions"] == [] and preview["methods_chars"] == 0
    assert reader.convert_read_json({"doi": ""}, names) is None


def test_the_convert_command_writes_the_facts_file_and_is_idempotent(tmp_path, capsys, monkeypatch):
    src = tmp_path / "reads_in.jsonl"
    src.write_text("\n".join(json.dumps(o) for o in (READ_JSON_OK, READ_JSON_FAILED)) + "\n", encoding="utf-8")
    out = tmp_path / "reads" / "2026-10-23.jsonl"
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "radar.sqlite"))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    args = _args(paths=[], from_read_json=str(src), to=str(out), no_rank=True, json=False)
    assert ri_cmd.cmd_reads_import(args) == 0
    assert ri_cmd.cmd_reads_import(args) == 0                     # a second conversion adds nothing
    recs = reader.iter_records([out])
    assert len(recs) == 1 and recs[0]["doi"] == "10.1038/s41586-026-11143-w"
    assert "1 read" in capsys.readouterr().out


def _args(**kw):
    kw.setdefault("config_dir", str(ROOT / "config"))
    return type("Args", (), kw)()


# ---------------------------------------------------------------------------- hosted import
def _seeded_store(tmp_path):
    st = Store(tmp_path / "hosted.sqlite")
    p = Paper(id="doi:10.1038/s41586-026-11143-w", source="crossref", kind="article",
              title="De novo design of cysteine proteases", doi="10.1038/s41586-026-11143-w",
              abstract="An abstract.", journal="Nature", pub_date="2026-10-14", sections={})
    st.upsert_paper(p)
    st.replace_mentions(p.id, [Mention(p.id, "instrument", "stale-model", "Stale", section="methods", count=1),
                               Mention(p.id, "company", "illumina", "Illumina", section="abstract", count=1)])
    st.commit()
    return st


def _facts(**kw):
    rec = {
        "schema": reader.SCHEMA_NAME, "status": "ok", "doi": "10.1038/s41586-026-11143-w",
        "paper_id": "doi:10.1038/s41586-026-11143-w", "title": "De novo design of cysteine proteases",
        "journal": "Nature", "pub_date": "2026-10-14", "publisher": "nature",
        "read_at": "2026-10-23T07:31:02Z", "methods_chars": 29807, "sections": ["coi", "methods"],
        "instruments": [{"id": "ilmn-novaseq-x-plus", "name": "NovaSeq X Plus"}],
        "companies": [{"id": "illumina", "name": "Illumina"}],
        "mentions": [
            {"kind": "instrument", "entity_id": "ilmn-novaseq-x-plus", "entity_name": "NovaSeq X Plus",
             "section": "methods", "count": 4, "alias": "NovaSeq X"},
            {"kind": "company", "entity_id": "illumina", "entity_name": "Illumina", "section": "coi", "count": 1, "alias": ""},
        ],
    }
    rec.update(kw)
    return rec


def test_import_marks_the_paper_replaces_its_methods_mentions_and_is_idempotent(tmp_path):
    st = _seeded_store(tmp_path)
    counts = reader.import_records(st, [_facts()])
    assert (counts["imported"], counts["unknown_doi"], counts["skipped"]) == (1, 0, 0)
    row = st.get_paper("doi:10.1038/s41586-026-11143-w")
    extra = json.loads(row["extra_json"])
    assert extra["fulltext_source"] == "personal_subscription" and extra["methods_chars"] == 29807
    assert extra["sections_present"] == ["coi", "methods"] and extra["read_at"] == "2026-10-23T07:31:02Z"
    sections = json.loads(row["sections_json"])
    assert sections == {"coi": "", "methods": ""}            # present, and empty: the text stayed on the Mac
    by_section = {(m["section"], m["entity_id"]): m for m in st.mentions_for(row["id"])}
    assert ("methods", "stale-model") not in by_section      # the Methods section was replaced wholesale
    assert by_section[("methods", "ilmn-novaseq-x-plus")]["count"] == 4
    assert by_section[("methods", "ilmn-novaseq-x-plus")]["alias"] == "NovaSeq X"
    assert ("abstract", "illumina") in by_section            # metadata-derived sections are left alone
    assert ("coi", "illumina") in by_section
    again = reader.import_records(st, [_facts()])
    assert (again["imported"], again["skipped"]) == (0, 1)
    st.close()


def test_import_counts_unknown_dois_previews_and_rubbish(tmp_path):
    st = _seeded_store(tmp_path)
    counts = reader.import_records(st, [
        _facts(doi="10.1038/not-in-the-store", paper_id=""),
        _facts(status="preview", mentions=[], methods_chars=0),
        {"schema": reader.SCHEMA_NAME},
        "not a record",
    ])
    assert counts == {"imported": 0, "skipped": 1, "unknown_doi": 1, "invalid": 2, "rescored": 0, "mentions": 0}
    st.close()


def test_import_records_the_supplement_link_and_rescores_through_the_pipeline(tmp_path):
    from litradar.pipeline import Pipeline

    st = _seeded_store(tmp_path)
    settings = Settings(config_dir=ROOT / "config", cache_dir=tmp_path / ".cache", db_path=tmp_path / "hosted.sqlite")
    pipe = Pipeline(settings, http=object(), store=st)
    counts = reader.import_records(st, [_facts(status="supplement_only", methods_in_supplement=True,
                                               methods_chars=450,
                                               supplement_url="https://www.science.org/doi/suppl/x")], pipe=pipe)
    assert counts["imported"] == 1 and counts["rescored"] == 1
    extra = json.loads(st.get_paper("doi:10.1038/s41586-026-11143-w")["extra_json"])
    assert extra["methods_in_supplement"] is True and extra["supplement_url"] == "https://www.science.org/doi/suppl/x"
    score = st.get_score("doi:10.1038/s41586-026-11143-w")
    assert score and score["final"] is not None
    st.close()


def test_the_import_command_reports_the_three_counts(tmp_path, capsys, monkeypatch):
    st = _seeded_store(tmp_path)
    st.close()
    facts = tmp_path / "reads" / "2026-10-23.jsonl"
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text(json.dumps(_facts()) + "\n" + json.dumps(_facts(doi="10.9999/nope", paper_id="")) + "\n",
                     encoding="utf-8")
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "hosted.sqlite"))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    assert ri_cmd.cmd_reads_import(_args(paths=[str(facts)], from_read_json=None, to=None, no_rank=True, json=False)) == 0
    out = capsys.readouterr().out
    assert "reads.imported=1" in out and "reads.unknown_doi=1" in out and "reads.skipped=0" in out


def test_the_import_command_refuses_a_missing_database(tmp_path, monkeypatch, capsys):
    facts = tmp_path / "f.jsonl"
    facts.write_text(json.dumps(_facts()) + "\n", encoding="utf-8")
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "nothing.sqlite"))
    assert ri_cmd.cmd_reads_import(_args(paths=[str(facts)], from_read_json=None, to=None, no_rank=True, json=False)) == 1
    assert "no database at" in capsys.readouterr().err


def test_an_empty_queue_and_an_empty_import_both_exit_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "radar.sqlite"))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setattr(reader, "run_queue", lambda *a, **k: reader.QueueReport(notes=["nothing to read"]))
    assert rq_cmd.cmd_read_queue(_args(limit=20, days=7, source="index", index_url=None, snippets=False,
                                       dry_run=False, json=False)) == 0
    assert "nothing to read" in capsys.readouterr().out
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert ri_cmd.cmd_reads_import(_args(paths=[str(empty)], from_read_json=None, to=None, no_rank=True, json=False)) == 0
    assert "nothing to do" in capsys.readouterr().out


# ---------------------------------------------------------------------------- the workflows
@pytest.mark.parametrize("name,before", [("daily.yml", "Update the corpus"), ("weekly.yml", "Run the radar")])
def test_the_import_step_runs_after_the_restore_and_before_the_run(name, before):
    wf = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    job = wf["jobs"]["update" if name == "daily.yml" else "radar"]
    steps = job["steps"]
    names = [s.get("name", "") for s in steps]
    idx = next(i for i, s in enumerate(steps) if "reads-import" in s.get("run", ""))
    step = steps[idx]
    assert step["continue-on-error"] is True
    assert step["run"].strip() == "python -m litradar reads-import"
    assert "${{" not in step["run"]
    restore = next(i for i, n in enumerate(names) if n.startswith(("Download the database", "Restore the database")))
    assert restore < idx < names.index(before)
    assert job["env"]["LITRADAR_DB"] == "data/radar.sqlite"


def test_every_workflow_still_parses_and_every_action_is_still_pinned():
    pin = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
    seen = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in wf["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses:
                    assert pin.match(uses), f"{path.name}: unpinned action {uses!r}"
                    action, sha = uses.split("@")
                    assert seen.setdefault(action, sha) == sha, f"{action} pinned to two different SHAs"


# ---------------------------------------------------------------------------- the LaunchAgent
@pytest.mark.skipif(sys.platform == "win32", reason="the schedule script is a POSIX shell script")
def test_the_plist_renders_with_absolute_paths_and_the_daily_schedule(tmp_path):
    script = ROOT / "scripts" / "reader_schedule.sh"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LITRADAR_READER_PYTHON": "/opt/py/bin/python3"}
    out = subprocess.run(["bash", str(script), "render"], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    plist = out.stdout
    assert "@@" not in plist
    root = ET.fromstring(plist)
    pairs = dict(zip([e.text for e in root.find("dict")[::2]], root.find("dict")[1::2]))
    assert pairs["Label"].text == "com.lit-radar.reader"
    args = [e.text for e in pairs["ProgramArguments"]]
    assert args == ["/bin/bash", str(script), "run"]
    assert pairs["WorkingDirectory"].text == str(ROOT)
    cal = pairs["StartCalendarInterval"]
    assert dict(zip([e.text for e in cal[::2]], [e.text for e in cal[1::2]])) == {"Hour": "7", "Minute": "30"}
    assert pairs["StandardOutPath"].text == str(ROOT / ".cache" / "reader.log")
    assert "/opt/py/bin/python3" in plist
    body = script.read_text(encoding="utf-8")
    for fragment in ('pgrep -f "litradar login"', "mkdir \"$LOCK\"", "read-queue --limit",
                     "commit.gpgsign=false", "git -C \"$REPO\" pull --rebase", "nothing to commit"):
        assert fragment in body, fragment


@pytest.mark.skipif(sys.platform == "win32", reason="the schedule script is a POSIX shell script")
def test_the_schedule_script_never_installs_anything_by_itself(tmp_path):
    """`status` and a bare call must not write a plist: installing is the owner's explicit step."""
    script = ROOT / "scripts" / "reader_schedule.sh"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LITRADAR_READER_PLIST_DIR": str(tmp_path / "LaunchAgents")}
    for argv in (["status"], []):
        subprocess.run(["bash", str(script), *argv], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert not (tmp_path / "LaunchAgents").exists()
