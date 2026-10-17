"""Static dashboard built from a seeded temporary SQLite store (no network, no live database).

Cases: a normal store, an empty one, malformed JSON in extra_json / news companies, a store that
carries the optional `timeseries` table, and a store with no week overlap (pagination-style stop,
where every bucket is zero but the site must still build).
"""
import json
import sqlite3
from datetime import date

from litradar import dashboard
from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.store import Store

TODAY = date(2026, 10, 17)          # a Saturday; its ISO week starts Monday 2026-10-12
WEEK = "2026-10-12"
PRIOR_WEEK = "2026-10-05"


def _paper(i, pub_date="2026-10-13", **kw):
    base = dict(
        id=f"doi:10.1000/dash{i}",
        source="biorxiv",
        kind="preprint",
        title=f"Paper {i} on a profiling platform",
        doi=f"10.1000/dash{i}",
        abstract="Abstract text.",
        journal="Journal of Examples",
        pub_date=pub_date,
        url=f"https://example.org/{i}",
        authors=[Author(name="A. Researcher", affiliation="Example Institute")],
        affiliations=["Example Institute"],
        sections={"methods": "Methods text."},
    )
    base.update(kw)
    return Paper(**base)


def _seed(tmp_path, name="radar.sqlite"):
    st = Store(tmp_path / name)
    st.upsert_paper(_paper(1))
    st.upsert_paper(_paper(2, pub_date="2026-10-14", kind="article"))
    st.upsert_paper(_paper(3, pub_date="2026-10-06"))          # the week before
    st.upsert_paper(_paper(4, pub_date="", extra={"country": "GB", "subgroups": ["spatial"]}))
    st.replace_mentions("doi:10.1000/dash1", [
        Mention("doi:10.1000/dash1", "instrument", "inst-a", "Instrument A", "Vendor One", "VONE", "sequencer", "methods", 2, "…"),
        Mention("doi:10.1000/dash1", "company", "vendor-one", "Vendor One", "", "VONE", "", "affiliations", 1, "…"),
        Mention("doi:10.1000/dash1", "company", "vendor-one", "Vendor One", "", "VONE", "", "coi", 1, "…"),
    ])
    st.replace_mentions("doi:10.1000/dash2", [
        Mention("doi:10.1000/dash2", "instrument", "inst-b", "Instrument B", "Vendor Two", "VTWO", "spatial", "abstract", 1, "…"),
        Mention("doi:10.1000/dash2", "company", "vendor-two", "Vendor Two", "", "VTWO", "", "methods", 1, "…"),
    ])
    st.replace_mentions("doi:10.1000/dash3", [
        Mention("doi:10.1000/dash3", "instrument", "inst-a", "Instrument A", "Vendor One", "VONE", "sequencer", "methods", 1, "…"),
    ])
    st.upsert_archive_records([
        ArchiveRecord("sra", f"XRX{i}", "SRP1", "Instrument A model", "PLATFORM", "WGS", "2026-10-13",
                      "centre", "study", "Homo sapiens", "inst-a", "Vendor One", "VONE")
        for i in range(5)
    ])
    st.upsert_archive_records([
        ArchiveRecord("ena", "ERX1", "ERP1", "Instrument B model", "PLATFORM", "RNA-Seq", "2026-10-07",
                      "centre", "study", "Mus musculus", "inst-b", "Vendor Two", "VTWO"),
        # no gazetteer match: the archive's own upper-case platform name stands in for the vendor
        ArchiveRecord("ena", "ERX2", "ERP2", "Unmatched model", "VENDOR ONE", "RNA-Seq", "2026-10-13",
                      "centre", "study", "Mus musculus", "", "", ""),
    ])
    st.upsert_news([
        NewsItem("news-1", "feed", "A headline", "https://example.org/n1", "2026-10-13", "summary", ["Vendor One"]),
    ])
    st.add_tracked_author_hit("doi:10.1000/dash1", "A. Researcher", "spatial", "name")
    st.save_score("doi:10.1000/dash1", 4.0, None, 4.0, "reasons")
    # pin first_seen so the dateless paper buckets into a known week whatever day the suite runs
    st.conn.execute("UPDATE papers SET first_seen='2026-10-13T00:00:00Z'")
    st.commit()
    st.close()
    return tmp_path / name


def _collect(db_path, weeks=4, today=TODAY):
    conn, tmp = dashboard.connect_readonly(db_path)
    try:
        return dashboard.collect(conn, weeks=weeks, today=today)
    finally:
        conn.close()


def test_week_bucketing():
    assert dashboard.week_of("2026-10-17") == WEEK
    assert dashboard.week_of("2026-10-12T04:00:00Z") == WEEK
    assert dashboard.week_of("2026-10-11") == PRIOR_WEEK
    assert dashboard.week_of("") == "" and dashboard.week_of("not-a-date") == ""
    assert dashboard.week_axis(TODAY, 3) == ["2026-09-28", PRIOR_WEEK, WEEK]


def test_build_site_writes_pages_and_parsable_json(tmp_path):
    db = _seed(tmp_path)
    out = tmp_path / "site"
    report = dashboard.build(db, out, weeks=4, today=TODAY)

    for page in ("index.html", "instruments.html", "companies.html", "subgroups.html",
                 "countries.html", "search.html"):
        assert (out / page).exists(), page
        text = (out / page).read_text(encoding="utf-8")
        assert text.startswith("<!doctype html>")
        assert dashboard.CHARTJS_URL in text and dashboard.CHARTJS_SRI in text
        blob = text.split('<script type="application/json" id="page-data">', 1)[1].split("</script>", 1)[0]
        assert json.loads(blob) is not None          # the inline payload parses
        assert "<" not in blob and ">" not in blob   # and cannot close its own script tag
    assert (out / "assets" / "app.js").exists() and (out / "assets" / "styles.css").exists()
    assert (out / ".nojekyll").exists()

    for name in ("overview", "instruments", "companies", "subgroups", "countries", "papers"):
        payload = json.loads((out / "data" / f"{name}.json").read_text(encoding="utf-8"))
        assert isinstance(payload, dict)

    overview = json.loads((out / "data" / "overview.json").read_text(encoding="utf-8"))
    assert overview["weeks"][-1] == WEEK and len(overview["weeks"]) == 4
    # dash4 has no pub_date, so it is bucketed by first_seen (today) -> the current week
    assert overview["papers_per_week"]["total"] == [0, 0, 1, 3]
    assert overview["instruments_by_vendor"]["series"]["Vendor One"] == [0, 0, 1, 1]
    # the unmatched ERX2 record ("VENDOR ONE") merges into the mixed-case vendor rather than
    # standing up a second stacked series for the same vendor
    assert overview["archive_by_vendor"]["series"]["Vendor One"] == [0, 0, 0, 6]
    assert report["counts"]["instruments"] == 2 and report["counts"]["papers_indexed"] == 4
    assert report["files"]["index.html"] > 0 and report["bytes"] == sum(report["files"].values())


def test_entity_pages_carry_series_roles_and_recent_papers(tmp_path):
    data = _collect(_seed(tmp_path))
    inst = {e["id"]: e for e in data["instruments"]}
    assert inst["inst-a"]["papers_per_week"] == [0, 0, 1, 1]
    assert inst["inst-a"]["experiments_per_week"] == [0, 0, 0, 5]
    assert inst["inst-a"]["recent_papers"][0]["url"].startswith("https://example.org/")
    assert inst["inst-b"]["experiments_per_week"] == [0, 0, 1, 0]

    comp = {c["id"]: c for c in data["companies"]}
    assert comp["vendor-one"]["author"] == [0, 0, 0, 1]
    assert comp["vendor-one"]["funding_coi"] == [0, 0, 0, 1]
    assert comp["vendor-one"]["subject"] == [0, 0, 0, 0]
    assert comp["vendor-one"]["news"] == [0, 0, 0, 1]
    assert comp["vendor-two"]["subject"] == [0, 0, 0, 1]

    movers = {m["id"]: m for m in data["movers"]["instrument_papers"]}
    assert movers["inst-a"]["recent"] == 2 and movers["inst-a"]["previous"] == 0
    assert movers["inst-a"]["pct"] is None          # no baseline: reported as n/a, never as infinity

    assert data["subgroups"]["available"] is True
    groups = {g["id"]: g for g in data["subgroups"]["groups"]}
    assert groups["spatial"]["total"] == 2          # one tracked-author hit + one extra_json assignment
    assert data["countries"]["available"] is True and data["countries"]["source"] == "extra_json.country"
    assert data["countries"]["series"]["GB"] == [0, 0, 0, 1]

    titles = {p["title"]: p for p in data["papers"]}
    row = titles["Paper 1 on a profiling platform"]
    assert row["instruments"] == ["Instrument A"] and row["companies"] == ["Vendor One"]
    assert row["score"] == 4.0


def test_empty_store_still_builds(tmp_path):
    db = tmp_path / "empty.sqlite"
    Store(db).close()
    out = tmp_path / "site-empty"
    report = dashboard.build(db, out, weeks=6, today=TODAY)
    assert report["counts"] == {"instruments": 0, "companies": 0, "subgroups": 0, "countries": 0,
                                "papers_indexed": 0, "timeseries_months": 0}
    overview = json.loads((out / "data" / "overview.json").read_text(encoding="utf-8"))
    assert overview["papers_per_week"]["total"] == [0] * 6
    assert overview["movers"]["instrument_papers"] == []
    countries = (out / "countries.html").read_text(encoding="utf-8")
    assert "No country signal in this store yet" in countries   # the placeholder explains how to enable it
    assert json.loads((out / "data" / "papers.json").read_text(encoding="utf-8"))["papers"] == []


def test_malformed_rows_are_skipped_not_fatal(tmp_path):
    db = _seed(tmp_path, "malformed.sqlite")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE papers SET extra_json='{not json' WHERE id='doi:10.1000/dash4'")
    conn.execute("UPDATE news_items SET companies_json='[\"Vendor One\"' WHERE id='news-1'")
    conn.execute(
        "INSERT INTO papers (id, title, pub_date, first_seen, extra_json, kind) VALUES "
        "('x:1','Broken dates','not-a-date','also-not-a-date','{\"country\": 17}','article')"
    )
    conn.commit()
    conn.close()

    data = _collect(db)
    assert data["papers_per_week"]["total"] == [0, 0, 1, 3]       # dash4 still counts; the junk-date row drops out
    assert data["countries"]["available"] is False                # 17 is not a country string
    comp = {c["id"]: c for c in data["companies"]}
    assert comp["vendor-one"]["news"] == [0, 0, 0, 0]             # unparseable companies_json -> no news counted
    groups = {g["id"]: g for g in data["subgroups"]["groups"]}
    assert groups["spatial"]["total"] == 1                        # only the tracked-author hit survives


def test_optional_timeseries_table_is_used_when_present(tmp_path):
    db = _seed(tmp_path, "ts.sqlite")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS timeseries (source TEXT, entity_id TEXT, model TEXT, month TEXT, count INTEGER)")
    conn.executemany(
        "INSERT INTO timeseries (source, entity_id, model, month, count) VALUES (?,?,?,?,?)",
        [("archive", "inst-a", "Instrument A model", "2026-09", 11),
         ("archive", "inst-a", "Instrument A model", "2026-10", 13),
         ("papers", "inst-b", "Instrument B model", "2026-10", 2)],
    )
    conn.commit()
    conn.close()

    out = tmp_path / "site-ts"
    report = dashboard.build(db, out, weeks=4, today=TODAY)
    assert report["counts"]["timeseries_months"] == 2
    overview = json.loads((out / "data" / "overview.json").read_text(encoding="utf-8"))
    ts = overview["timeseries"]
    assert ts["available"] is True and ts["months"] == ["2026-09", "2026-10"]
    assert ts["series"]["archive"] == [11, 13] and ts["series"]["papers"] == [0, 2]
    assert ts["by_entity"]["inst-a"]["2026-10"] == 13
    assert "Monthly series from the timeseries table" in (out / "index.html").read_text(encoding="utf-8")


def test_window_outside_the_data_yields_zero_filled_series(tmp_path):
    """The stop condition of the week walk: ask for weeks that contain nothing and every bucket is 0."""
    data = _collect(_seed(tmp_path, "window.sqlite"), weeks=3, today=date(2027, 3, 1))
    assert data["papers_per_week"]["total"] == [0, 0, 0]
    assert data["instruments"] == [] and data["companies"] == []
    assert data["movers"]["instrument_papers"] == []
    assert data["papers"] == []


def test_fold_tail_keeps_top_series_and_sums_the_rest():
    series = {"a": [5, 5], "b": [4, 4], "c": [1, 1], "d": [1, 0]}
    folded = dashboard._fold_tail(series, 2)
    assert list(folded) == ["a", "b", "Other"]
    assert folded["Other"] == [2, 1]
    unchanged = dashboard._fold_tail(series, 9)
    assert "Other" not in unchanged and list(unchanged) == ["a", "b", "c", "d"]


def test_build_does_not_modify_the_source_database(tmp_path):
    db = _seed(tmp_path, "readonly.sqlite")
    before = db.read_bytes()
    dashboard.build(db, tmp_path / "site-ro", weeks=4, today=TODAY)
    assert db.read_bytes() == before
