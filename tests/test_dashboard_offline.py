"""Static dashboard built from a seeded temporary SQLite store (no network, no live database).

Cases: a normal store, an empty one, malformed JSON in extra_json / news companies, a store that
carries the optional `timeseries` table, a store with no week overlap (pagination-style stop,
where every bucket is zero but the site must still build), the instrument card cap, the
complete-weeks rule for movers, optional-versus-fatal database errors, archive platform-code
merging and subgroup folding; and the coverage rules - weeks before the recorded ingestion start
are n/a rather than zero, movers compare only windows ingested alike, and first-of-month dates
from a padding source are kept out of the weekly series.
"""
import json
import sqlite3
from datetime import date

import pytest

from litradar import backfill, dashboard
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
    # the run that built the store covered the whole charted axis, so an empty week is a measured zero
    st.record_run("2026-10-17T00:00:00Z", "2026-09-21", "2026-10-17", {"papers": 4})
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

    # TODAY is a Saturday, so the current ISO week (2026-10-12) is unfinished and left out of the
    # comparison: of the remaining three buckets one is "recent" (2026-10-05) and one "previous"
    movers = {m["id"]: m for m in data["movers"]["instrument_papers"]}
    window = dict(data["movers_window"])
    coverage = window.pop("coverage")
    assert window == {"span": 1, "offset": 1, "recent": [PRIOR_WEEK], "previous": ["2026-09-28"],
                      "excludes_current_week": True}
    # the recorded run covers both compared weeks, so every table is comparable
    assert coverage == {k: {"comparable": True, "reason": ""} for k in ("instrument_papers", "archive_experiments", "companies")}
    assert data["coverage"] == {"runs_since": "2026-09-21", "backfill_from": None, "backfill_to": None,
                                "archive_since": "2026-09-21", "papers_since": "2026-09-21"}
    assert movers["inst-a"]["recent"] == 1 and movers["inst-a"]["previous"] == 0
    assert movers["inst-a"]["pct"] is None          # no baseline: reported as n/a, never as infinity
    assert "inst-b" not in movers                   # its only paper is in the unfinished week

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
    assert report["counts"] == {"instruments": 0, "instruments_total": 0, "companies": 0, "subgroups": 0,
                                "countries": 0, "papers_indexed": 0, "timeseries_months": 0,
                                "timeseries_series": 0, "timeseries_vendors": 0}
    assert report["skipped_queries"] == []           # a complete schema skips nothing
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
    # a week no folded series covers stays n/a in 'Other'; one that any of them covers is summed
    with_gaps = dashboard._fold_tail({"a": [None, 5], "b": [None, 4], "c": [None, 1], "d": [None, 0]}, 2)
    assert with_gaps["Other"] == [None, 1]


def test_build_does_not_modify_the_source_database(tmp_path):
    db = _seed(tmp_path, "readonly.sqlite")
    before = db.read_bytes()
    dashboard.build(db, tmp_path / "site-ro", weeks=4, today=TODAY)
    assert db.read_bytes() == before


SUNDAY = date(2026, 10, 18)         # the ISO week of 2026-10-12 is complete, so it counts as "recent"


def test_archive_heavy_instrument_survives_the_card_cap(tmp_path, monkeypatch):
    """An instrument with no paper mentions but many archive records must keep its card and its
    place in the archive movers, however many paper-heavy instruments outrank it."""
    db = _seed(tmp_path, "cap.sqlite")
    st = Store(db)
    st.upsert_archive_records([
        ArchiveRecord("sra", f"SRX9{i}", "SRP9", "Instrument C model", "PLATFORM", "WGS", "2026-10-06",
                      "centre", "study", "Homo sapiens", "inst-c", "Vendor Three", "VTHR")
        for i in range(40)
    ])
    st.commit()
    st.close()
    monkeypatch.setattr(dashboard, "MAX_INSTRUMENT_CARDS", 1)   # a cap tighter than the data

    data = _collect(db, today=SUNDAY)
    assert data["instruments_total"] == 3
    cards = {e["id"]: e for e in data["instruments"]}
    assert set(cards) == {"inst-a", "inst-c"}        # top-1 by papers UNION top-1 by experiments
    assert cards["inst-c"]["papers_total"] == 0 and cards["inst-c"]["experiments_total"] == 40
    assert cards["inst-c"]["recent_papers"] == []
    assert cards["inst-a"]["recent_papers"]          # fetched only for the carded entities

    archive_movers = {m["id"]: m for m in data["movers"]["archive_experiments"]}
    assert archive_movers["inst-c"]["recent"] == 40 and archive_movers["inst-c"]["previous"] == 0
    assert archive_movers["inst-c"]["name"] == "Instrument C model"
    # inst-b lost its card to the cap but is still ranked among the movers
    paper_movers = {m["id"]: m for m in data["movers"]["instrument_papers"]}
    assert "inst-b" in paper_movers and "inst-b" not in cards

    out = tmp_path / "site-cap"
    report = dashboard.build(db, out, weeks=4, today=SUNDAY)
    assert report["counts"]["instruments"] == 2 and report["counts"]["instruments_total"] == 3
    assert "2 of 3 instruments" in (out / "instruments.html").read_text(encoding="utf-8")


def test_movers_skip_the_unfinished_week_unless_today_is_sunday(tmp_path):
    db = _seed(tmp_path, "movers.sqlite")
    axis = dashboard.week_axis(TODAY, 4)

    saturday = dashboard._movers_window(axis, TODAY)
    assert saturday["excludes_current_week"] is True and saturday["recent"] == [PRIOR_WEEK]
    sunday = dashboard._movers_window(axis, SUNDAY)
    assert sunday["excludes_current_week"] is False
    assert sunday["span"] == 2 and sunday["recent"] == [PRIOR_WEEK, WEEK]
    assert sunday["previous"] == ["2026-09-21", "2026-09-28"]
    assert dashboard._movers_window(["2026-10-12"], TODAY)["span"] == 0    # nothing complete to compare

    on_sunday = {m["id"]: m for m in _collect(db, today=SUNDAY)["movers"]["instrument_papers"]}
    assert on_sunday["inst-a"]["recent"] == 2 and on_sunday["inst-b"]["recent"] == 1
    on_saturday = {m["id"]: m for m in _collect(db, today=TODAY)["movers"]["instrument_papers"]}
    assert on_saturday["inst-a"]["recent"] == 1 and "inst-b" not in on_saturday

    index = (tmp_path / "site-movers" / "index.html")
    dashboard.build(db, index.parent, weeks=4, today=TODAY)
    text = index.read_text(encoding="utf-8")
    assert "The current, unfinished week is left out." in text
    assert "The last one complete week (2026-10-05 to 2026-10-05)" in text


def test_missing_columns_are_optional_but_other_errors_are_fatal(tmp_path):
    # an older store: a papers table without kind / journal / url / extra_json
    old = tmp_path / "old.sqlite"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT, pub_date TEXT, first_seen TEXT)")
    conn.execute("INSERT INTO papers VALUES ('x:1', 'Old row', '2026-10-13', '2026-10-13T00:00:00Z')")
    conn.commit()
    conn.close()
    report = dashboard.build(old, tmp_path / "site-old", weeks=4, today=TODAY)
    assert report["skipped_queries"]                                   # counted, not silent
    assert all(m.startswith("no such column") for m in report["skipped_queries"])
    assert report["counts"]["papers_indexed"] == 0                    # the section is empty, the build is fine

    # a query that is wrong for any other reason propagates
    conn, _ = dashboard.connect_readonly(old)
    try:
        with pytest.raises(sqlite3.OperationalError):
            dashboard._q(conn, "SELECT * FROM")
        assert dashboard._q(conn, "SELECT missing_col FROM papers") == []
        assert conn.skipped_queries == ["no such column: missing_col"]
    finally:
        conn.close()

    # a corrupt file never yields an all-zero site
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"this is not a database " * 512)
    with pytest.raises(sqlite3.DatabaseError):
        dashboard.build(junk, tmp_path / "site-junk", weeks=4, today=TODAY)
    assert not (tmp_path / "site-junk" / "index.html").exists()


def test_archive_platform_codes_merge_into_the_gazetteer_vendor(tmp_path):
    db = _seed(tmp_path, "platform.sqlite")
    st = Store(db)
    st.upsert_archive_records([
        # gazetteer match: vendor spelled as the gazetteer does
        ArchiveRecord("ena", "ERX10", "ERP10", "DNBSEQ-G400", "DNBSEQ", "WGS", "2026-10-13",
                      "centre", "study", "Homo sapiens", "inst-g400", "MGI Tech", "300676.SZ"),
        # no gazetteer match: only the archive's platform code is known
        ArchiveRecord("ena", "ERX11", "ERP11", "Some unmatched BGI model", "BGISEQ", "WGS", "2026-10-13",
                      "centre", "study", "Homo sapiens", "", "", ""),
        ArchiveRecord("ena", "ERX12", "ERP12", "Some unmatched PacBio model", "PACBIO_SMRT", "WGS", "2026-10-13",
                      "centre", "study", "Homo sapiens", "", "", ""),
        # a code with no gazetteer vendor is left as it is, never guessed
        ArchiveRecord("ena", "ERX13", "ERP13", "GenoLab M", "GENEMIND", "WGS", "2026-10-13",
                      "centre", "study", "Homo sapiens", "", "", ""),
    ])
    st.commit()
    st.close()
    series = _collect(db)["archive_by_vendor"]["series"]
    assert series["MGI Tech"] == [0, 0, 0, 2]              # DNBSEQ (matched) + BGISEQ (code only) = one series
    assert series["Pacific Biosciences"] == [0, 0, 0, 1]
    assert series["GENEMIND"] == [0, 0, 0, 1]
    assert "BGISEQ" not in series and "PACBIO_SMRT" not in series


def test_subgroups_list_every_group_and_fold_only_the_chart(tmp_path):
    db = _seed(tmp_path, "groups.sqlite")
    st = Store(db)
    for i, group in enumerate(["g1", "g2", "g3", "g4", "g5", "g6", "g7", "g8", "g9"]):
        st.add_tracked_author_hit("doi:10.1000/dash1", f"Author {i}", group, "name")
    st.commit()
    st.close()
    sub = _collect(db)["subgroups"]
    ids = [g["id"] for g in sub["groups"]]
    assert len(ids) == 10 and "spatial" in ids and "g9" in ids       # nothing dropped from the table
    assert sub["folded"] is True
    assert len(sub["series"]) == dashboard.MAX_STACK_SERIES + 1 and "Other" in sub["series"]
    assert sum(sub["series"]["Other"]) == 10 - dashboard.MAX_STACK_SERIES
    out = tmp_path / "site-groups"
    dashboard.build(db, out, weeks=4, today=TODAY)
    assert "folds the rest into 'Other'" in (out / "subgroups.html").read_text(encoding="utf-8")


def test_page_script_links_only_http_urls():
    """The client script must refuse to turn a stored javascript:/data: URL into a link."""
    assert "function isWebUrl" in dashboard.APP_JS
    assert "isWebUrl(cell.href)" in dashboard.APP_JS
    assert r"/^https?:\/\//i" in dashboard.APP_JS


def test_footer_wording_placeholder_tickers_and_layout_rules(tmp_path):
    db = _seed(tmp_path, "layout.sqlite")
    st = Store(db)
    # an instrument whose gazetteer ticker is the placeholder 'n/a' (open-source software) must not get a ticker pill
    st.replace_mentions("doi:10.1000/dash2", [
        Mention("doi:10.1000/dash2", "instrument", "oss-tool", "Open Tool", "community", "n/a", "software", "methods", 1, "…"),
        Mention("doi:10.1000/dash2", "company", "vendor-two", "Vendor Two", "", "VTWO", "", "methods", 1, "…"),
    ])
    st.commit()
    st.close()
    data = _collect(db)
    inst = {e["id"]: e for e in data["instruments"]}
    assert inst["oss-tool"]["ticker"] == "" and inst["inst-a"]["ticker"] == "VONE"
    assert dashboard._ticker("n/a") == "" and dashboard._ticker("N/A") == "" and dashboard._ticker(" VONE ") == "VONE" and dashboard._ticker(None) == ""
    out = tmp_path / "site-layout"
    dashboard.build(db, out, weeks=4, today=TODAY)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "from the local corpus" not in html and "from the radar database this site was built from" in html
    css = (out / "assets" / "styles.css").read_text(encoding="utf-8")
    # phone width: the lede wraps, cards and charts never widen the page
    assert ".lede { max-width: 72ch; overflow-wrap: anywhere; }" in css
    assert "min-width: 0; max-width: 100%; overflow: hidden" in css and ".chart canvas { max-width: 100% !important; }" in css
    # search results: the title wraps and the table fits the card, so the other columns stay in view
    assert ".table-scroll.fit table { width: 100%" in css and "td.wrap, th.wrap { white-space: normal" in css
    js = (out / "assets" / "app.js").read_text(encoding="utf-8")
    assert '{ label: "Title", wrap: true }' in js and "fit: true" in js and 'options.fit ? "table-scroll fit"' in js


# ---------------------------------------------------------------------------- coverage rules
def _run_since(db, since):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE runs SET since=?", (since,))
    conn.commit()
    conn.close()


def test_weeks_before_the_archive_census_are_not_zero(tmp_path):
    """DASH-03: a store whose first run (and so first census) starts mid-axis must not draw the earlier weeks as
    zero experiments - they were never fetched for. They are None, the payload says how many, the first covered
    week reports how many days of records it holds, and the page says all of it in words."""
    db = _seed(tmp_path, "census.sqlite")
    _run_since(db, "2026-10-10")                     # a Saturday: the week of 2026-10-05 holds two days of census
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM archive_records WHERE first_public < '2026-10-10'")   # ERX1 (2026-10-07) goes
    conn.commit()
    conn.close()

    data = _collect(db, today=SUNDAY)
    assert data["coverage"]["archive_since"] == "2026-10-10"
    arch = data["archive_by_vendor"]
    assert arch["series"]["Vendor One"] == [None, None, 0, 6]
    assert arch["coverage"] == {"since": "2026-10-10", "uncovered_weeks": 2, "first_week": "2026-10-05", "first_week_days": 2}
    inst = {e["id"]: e for e in data["instruments"]}
    assert inst["inst-a"]["experiments_per_week"] == [None, None, 0, 5]
    assert inst["inst-a"]["experiments_total"] == 5              # None never counts
    # papers: the run is the only paper layer, so the same lead is n/a; a week before it that holds a paper keeps it
    assert data["papers_per_week"]["coverage"]["since"] == "2026-10-10"
    assert data["papers_per_week"]["total"] == [None, None, 1, 3]
    # the archive movers would compare a window the census never saw: suppressed, with the dates
    assert data["movers"]["archive_experiments"] == []
    reason = data["movers_window"]["coverage"]["archive_experiments"]["reason"]
    assert "the archive census (records public from 2026-10-10)" in reason
    assert "covers the recent window (2026-10-05 through 2026-10-18) only in part" in reason
    assert "previous one (2026-09-21 through 2026-10-04) not at all" in reason

    out = tmp_path / "site-census"
    dashboard.build(db, out, weeks=4, today=SUNDAY)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert ("The archive census in this store starts 2026-10-10: the 2 earlier weeks (shaded) were never fetched for, "
            "and an empty one is shown as n/a, not as zero; the week of 2026-10-05 holds 2 days of records.") in index
    assert "Instruments by archive experiments: suppressed, because the archive census" in index
    overview = json.loads((out / "data" / "overview.json").read_text(encoding="utf-8"))
    assert overview["archive_by_vendor"]["series"]["Vendor One"] == [None, None, 0, 6]     # null in JSON, never 0
    instruments = json.loads((out / "data" / "instruments.json").read_text(encoding="utf-8"))
    assert instruments["archive_coverage"]["uncovered_weeks"] == 2 and instruments["papers_coverage"]["since"] == "2026-10-10"
    assert "the 2 earlier weeks (shaded)" in (out / "instruments.html").read_text(encoding="utf-8")


def test_archive_records_before_the_first_run_extend_the_census_start(tmp_path):
    """Records that reached the store before the earliest run (an import) are data, not a gap: the census start
    moves back to the earliest record and those weeks are counted."""
    db = _seed(tmp_path, "import.sqlite")
    _run_since(db, "2026-10-10")                     # but ERX1 (2026-10-07) is still there
    data = _collect(db, today=SUNDAY)
    assert data["coverage"]["archive_since"] == "2026-10-07"
    assert data["archive_by_vendor"]["series"]["Vendor Two"] == [None, None, 1, 0]
    assert data["archive_by_vendor"]["coverage"]["uncovered_weeks"] == 2


def test_movers_are_suppressed_when_the_windows_were_not_ingested_alike(tmp_path):
    """DASH-01: the first weekly run falls inside the recent window, so the previous window holds only what
    arrived by other routes. Ranking the difference would rank the ingestion change; the tables are suppressed,
    the reason names the layer and the dates, and the page carries it without JavaScript."""
    db = _seed(tmp_path, "mixed.sqlite")
    _run_since(db, "2026-10-10")
    data = _collect(db, today=SUNDAY)                # recent 2026-10-05..18, previous 2026-09-21..10-04
    cov = data["movers_window"]["coverage"]
    assert cov["instrument_papers"]["comparable"] is False and cov["companies"]["comparable"] is False
    assert data["movers"]["instrument_papers"] == [] and data["movers"]["companies"] == []
    assert cov["instrument_papers"]["reason"].startswith(
        "the weekly runs (publication dates from 2026-10-10) cover the recent window (2026-10-05 through 2026-10-18) "
        "only in part and the previous one (2026-09-21 through 2026-10-04) not at all")
    out = tmp_path / "site-mixed"
    dashboard.build(db, out, weeks=4, today=SUNDAY)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Instruments by papers: suppressed, because the weekly runs (publication dates from 2026-10-10)" in index
    assert "Companies by mentions: suppressed, because" in index
    assert "A table appears only when both windows were ingested alike." in index

    # once the runs cover both windows the tables are back, with no suppression sentence
    _run_since(db, "2026-09-21")
    data = _collect(db, today=SUNDAY)
    assert all(v["comparable"] for v in data["movers_window"]["coverage"].values())
    assert {m["id"] for m in data["movers"]["instrument_papers"]} == {"inst-a", "inst-b"}
    dashboard.build(db, out, weeks=4, today=SUNDAY)
    assert "suppressed, because" not in (out / "index.html").read_text(encoding="utf-8")


def test_movers_coverage_rules_layer_by_layer():
    """The pure comparison: a layer must relate to both windows alike and fully or not at all."""
    rel = dashboard._layer_relation
    assert rel(None, None, "2026-09-21", "2026-10-04") == "unknown"
    assert rel("2026-09-01", None, "2026-09-21", "2026-10-04") == "full"
    assert rel("2026-09-25", None, "2026-09-21", "2026-10-04") == "partial"
    assert rel("2026-10-05", None, "2026-09-21", "2026-10-04") == "none"
    assert rel("2026-01-01", "2026-09-30", "2026-09-21", "2026-10-04") == "partial"
    assert rel("2026-01-01", "2026-09-20", "2026-09-21", "2026-10-04") == "none"

    window = {"span": 2, "offset": 0, "recent": ["2026-10-05", "2026-10-12"], "previous": ["2026-09-21", "2026-09-28"],
              "excludes_current_week": False}
    nothing = {"runs_since": None, "backfill_from": None, "backfill_to": None, "archive_since": None, "papers_since": None}
    # no recorded layer: compared as before (a store built outside the pipeline)
    assert all(v["comparable"] for v in dashboard._movers_coverage(window, nothing).values())
    # the backfill ends between the windows while the runs cover both: suppressed, and the backfill is named
    cov = dict(nothing, runs_since="2026-09-01", backfill_from="2026-01-01", backfill_to="2026-09-30", archive_since="2026-09-01")
    out = dashboard._movers_coverage(window, cov)
    assert out["instrument_papers"]["comparable"] is False
    assert out["instrument_papers"]["reason"].startswith("the backfilled months (2026-01-01 to 2026-09-30) cover the recent window")
    assert out["archive_experiments"]["comparable"] is True
    # a backfill that covers both windows and runs that cover both: comparable
    cov = dict(cov, backfill_to="2026-10-31")
    assert all(v["comparable"] for v in dashboard._movers_coverage(window, cov).values())
    # recorded layers that cover neither window: nothing to compare
    cov = dict(nothing, runs_since="2026-11-01", archive_since="2026-11-01")
    out = dashboard._movers_coverage(window, cov)
    assert out["companies"]["reason"].startswith("no run, backfill or census recorded in this store covers the compared weeks (2026-09-21 through 2026-10-18)")
    # too few complete weeks: nothing to compare and nothing to explain (the note already says so)
    assert dashboard._movers_coverage({"span": 0}, cov) == {k: {"comparable": False, "reason": ""} for k in dashboard.MOVERS_LAYERS}


def test_backfill_layer_is_read_and_marks_the_papers_boundary(tmp_path):
    """A recorded backfill extends paper coverage back to its first month (those weeks are measured zeros, not
    n/a), the papers chart marks the week the weekly runs begin, and a backfill that stops between the movers'
    windows suppresses the paper tables while the archive table - fed by the runs alone - stays."""
    db = _seed(tmp_path, "backfill.sqlite")
    _run_since(db, "2026-08-24")
    st = Store(db)
    backfill.ensure_schema(st)
    st.conn.executemany("INSERT INTO backfill_progress (subgroup, month, country, fetched, done_at, cap) VALUES (?,?,?,?,?,?)",
                        [("spatial", m, "", 10, "2026-10-01T00:00:00Z", 0) for m in ("2026-07", "2026-08", "2026-09")])
    st.commit()
    st.close()
    data = _collect(db, weeks=10, today=SUNDAY)      # axis 2026-08-10 .. 2026-10-12
    assert data["coverage"] == {"runs_since": "2026-08-24", "backfill_from": "2026-07-01", "backfill_to": "2026-09-30",
                                "archive_since": "2026-08-24", "papers_since": "2026-07-01"}
    pcov = data["papers_per_week"]["coverage"]
    assert pcov["uncovered_weeks"] == 0 and pcov["runs_week_index"] == 2       # two backfill-only weeks lead the axis
    assert data["papers_per_week"]["total"] == [0, 0, 0, 0, 0, 0, 0, 0, 1, 3]  # backfilled weeks are measured zeros
    assert data["archive_by_vendor"]["coverage"]["uncovered_weeks"] == 2       # the census is the runs' alone
    window = data["movers_window"]                   # recent 2026-09-21..10-18, previous 2026-08-24..09-20
    assert window["span"] == 4 and window["previous"][0] == "2026-08-24"
    cov = window["coverage"]
    assert cov["instrument_papers"]["comparable"] is False and cov["companies"]["comparable"] is False
    assert cov["instrument_papers"]["reason"].startswith(
        "the backfilled months (2026-07-01 to 2026-09-30) cover the recent window (2026-09-21 through 2026-10-18) only in part "
        "and the previous one (2026-08-24 through 2026-09-20) fully")
    assert cov["archive_experiments"]["comparable"] is True
    assert data["movers"]["instrument_papers"] == [] and data["movers"]["archive_experiments"]
    out = tmp_path / "site-backfill"
    dashboard.build(db, out, weeks=10, today=SUNDAY)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert ("The dashed line marks 2026-08-24, the first publication date the weekly multi-source runs cover; earlier "
            "weeks hold backfilled publication metadata only, so a step there is a change of ingestion, not of "
            "publishing.") in index
    assert "Instruments by papers: suppressed, because the backfilled months (2026-07-01 to 2026-09-30)" in index


def test_first_of_month_dates_from_a_padding_source_are_kept_out_of_weekly_series(tmp_path):
    """DASH-02: OpenAlex pads month- and year-precision dates to the 1st. Such papers have no publication week:
    they leave the headline chart, the vendor chart, the entity series and the movers, are counted apart per week,
    and the page says how many. The same date from a source that does not pad is an ordinary paper."""
    db = _seed(tmp_path, "padded.sqlite")
    st = Store(db)
    st.upsert_paper(_paper(10, pub_date="2026-10-01", source="openalex", kind="article"))    # week of 2026-09-28
    st.upsert_paper(_paper(11, pub_date="2026-10-01", source="openalex", kind="article"))
    st.upsert_paper(_paper(12, pub_date="2026-10-01", source="crossref", kind="article"))    # a real 1st: stays
    st.upsert_paper(_paper(13, pub_date="2026-10-02", source="openalex", kind="article"))    # day precision: stays
    st.replace_mentions("doi:10.1000/dash10", [
        Mention("doi:10.1000/dash10", "instrument", "inst-a", "Instrument A", "Vendor One", "VONE", "sequencer", "abstract", 1, "…"),
        Mention("doi:10.1000/dash10", "company", "vendor-one", "Vendor One", "", "VONE", "", "affiliations", 1, "…"),
    ])
    st.replace_mentions("doi:10.1000/dash13", [
        Mention("doi:10.1000/dash13", "instrument", "inst-a", "Instrument A", "Vendor One", "VONE", "sequencer", "abstract", 1, "…"),
    ])
    st.conn.execute("UPDATE papers SET first_seen='2026-10-13T00:00:00Z'")
    st.commit()
    st.close()

    data = _collect(db)
    pw = data["papers_per_week"]
    assert pw["total"] == [0, 2, 1, 3]                       # crossref 10-01 and openalex 10-02 count; the padded two do not
    assert pw["first_of_month"] == [0, 2, 0, 0] and pw["first_of_month_total"] == 2
    assert pw["padded_sources"] == ["openalex"]
    assert data["instruments_by_vendor"]["series"]["Vendor One"] == [0, 1, 1, 1]     # dash13 yes, dash10 no
    inst = {e["id"]: e for e in data["instruments"]}
    assert inst["inst-a"]["papers_per_week"] == [0, 1, 1, 1] and inst["inst-a"]["papers_total"] == 3
    comp = {c["id"]: c for c in data["companies"]}
    assert comp["vendor-one"]["author"] == [0, 0, 0, 1]      # the padded paper's affiliation mention is not a weekly count
    movers = {m["id"]: m for m in data["movers"]["instrument_papers"]}
    assert movers["inst-a"]["recent"] == 1 and movers["inst-a"]["previous"] == 1
    # the list surfaces keep every paper: the padded ones are still searchable
    assert {p["id"] for p in data["papers"]} >= {"doi:10.1000/dash10", "doi:10.1000/dash11"}

    out = tmp_path / "site-padded"
    dashboard.build(db, out, weeks=4, today=TODAY)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "with a day-precision publication date" in index
    assert ("2 papers dated the 1st of a month by a source that pads month- and year-precision dates to it (openalex) "
            "are left out of every weekly series on this site") in index
    assert '"first_of_month": [0, 2, 0, 0]' in index                   # the payload carries the uncharted column
    assert dashboard._week_known_sql("x") == "NOT (COALESCE(x.source,'') IN ('openalex') AND substr(x.pub_date, 9, 2) = '01')"


def test_page_script_renders_coverage_marks_and_na_cells():
    """The client renders a null week as n/a, shades uncovered weeks, marks the runs boundary and explains a
    suppressed movers table instead of showing an empty one."""
    js = dashboard.APP_JS
    assert 'return v === null ? "n/a" : (v || 0);' in js
    assert "function coveragePlugin(marks)" in js and 'id: "litradarCoverage"' in js
    assert 'coverageMarks(pw.coverage, "before any run or backfill")' in js
    assert 'coverageMarks((data.archive_by_vendor || {}).coverage, "before the archive census")' in js
    assert '"Dated to the 1st (not charted)"' in js
    assert "Suppressed for this window: the compared weeks were not ingested alike" in js
    assert "cell(e.papers_per_week[i]), cell(e.experiments_per_week[i])" in js
