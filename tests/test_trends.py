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


def _add_instrument_papers(st: Store, entity: str, name: str, weeks_back: int, n: int, first_seen_day: str = None, prefix: str = "") -> None:
    for i in range(n):
        pid = f"doi:10.1000/{prefix}{entity}-w{weeks_back}-{i}"
        st.upsert_paper(_paper(pid, _day(weeks_back, i % 7)))
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
