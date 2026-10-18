"""The client-side search index, vocabulary and page, built from a seeded temporary store (no network).

Cases: record fields and ordering, the data-policy gate on abstract excerpts (no full text ever,
no excerpt at level 'none'), markup stripping and escaping, the window, the record cap and the
byte budget, the entity vocabulary with card-aware page links, JSON validity, the page shell,
the dashboard build hook and the `search-index` command.
"""
import json
from datetime import date

from litradar import cli, dashboard
from litradar import dashboard_search as ds
from litradar.models import Author, Mention, Paper
from litradar.store import Store

TODAY = date(2026, 10, 17)
INJECTION = "</script><img src=x onerror=alert(1)>"


def _paper(i, **kw):
    base = dict(
        id=f"doi:10.1000/s{i}",
        source="biorxiv",
        kind="preprint",
        title=f"Paper {i} on spatial profiling",
        doi=f"10.1000/s{i}",
        abstract=f"Abstract {i}.",
        journal="Journal of Examples",
        pub_date="2026-10-13",
        url=f"https://example.org/{i}",
        license="cc_by",
        authors=[Author(name="A. Researcher", affiliation="Example Institute")],
        affiliations=["Example Institute"],
    )
    base.update(kw)
    return Paper(**base)


def _seed(tmp_path):
    st = Store(tmp_path / "radar.sqlite")
    # 1: open licence, long abstract, full text stored -> excerpt from the abstract only
    st.upsert_paper(_paper(
        1, kind="article", journal="Nature Methods", source="crossref", pub_date="2026-10-14",
        abstract=("Spatial profiling of tumour sections with an in situ platform resolves cell states at scale. " * 6),
        sections={"methods": "Sections were imaged on the SECRETMETHODS instrument.", "body": "SECRETBODY paragraph."},
    ))
    # 2: all-rights-reserved preprint recorded at 'facts_only' -> excerpt still allowed; title carries markup + injection
    st.upsert_paper(_paper(
        2, title=f"Gate-tunable WSe<sub>2</sub> moiré {INJECTION}", license="cc_no",
        extra={"fulltext_policy": "facts_only"}, abstract=f"Abstract two. {INJECTION}",
    ))
    # 3: recorded at 'none' -> title-only, no excerpt
    st.upsert_paper(_paper(3, extra={"fulltext_policy": "none"}, abstract="Hidden abstract."))
    # 4: outside the window
    st.upsert_paper(_paper(4, pub_date="2025-01-01"))
    # 5: no abstract, URL is the DOI resolver (omitted), countries and a keyword subgroup, RSS source
    st.upsert_paper(_paper(
        5, kind="article", source="rss:Some Journal", abstract="", url="https://doi.org/10.1000/s5",
        extra={"countries": ["us", "GB", "us"], "subgroups": ["spatial"]},
    ))
    st.replace_mentions("doi:10.1000/s1", [
        Mention("doi:10.1000/s1", "instrument", "inst-a", "Instrument A", "Vendor One", "VONE", "spatial", "methods", 2, "…"),
        Mention("doi:10.1000/s1", "instrument", "inst-a", "Instrument A", "Vendor One", "VONE", "spatial", "abstract", 1, "…"),
        Mention("doi:10.1000/s1", "company", "vendor-one", "Vendor One", "", "VONE", "", "affiliations", 1, "…"),
    ])
    st.replace_mentions("doi:10.1000/s2", [
        Mention("doi:10.1000/s2", "instrument", "inst-b", "Instrument B", "Vendor Two", "n/a", "", "abstract", 1, "…"),
    ])
    st.add_tracked_author_hit("doi:10.1000/s1", "A. Researcher", "long_read", "name")
    st.save_score("doi:10.1000/s1", 5.0, None, 5.0, "journal tier A; instruments named")
    st.save_score("doi:10.1000/s2", 3.0, None, 3.0, "preprint")
    st.save_score("doi:10.1000/s3", 1.0, None, 1.0, "preprint")
    st.save_score("doi:10.1000/s5", 4.0, None, 4.0, "journal tier ?")
    st.conn.execute("UPDATE papers SET first_seen='2026-10-13T00:00:00Z'")
    st.commit()
    st.close()
    return tmp_path / "radar.sqlite"


def _collect(db, **kw):
    conn, tmp = dashboard.connect_readonly(db)
    try:
        return ds.collect_index(conn, today=TODAY, **kw)
    finally:
        conn.close()


def test_text_helpers():
    assert ds.clean_text("WSe<sub>2</sub>\n   moiré &amp; more") == "WSe 2 moiré & more"
    assert ds.clean_text("p < 0.05 and n > 30") == "p < 0.05 and n > 30"      # comparisons are not tags
    long = "word " * 100
    cut = ds.excerpt(long, 50)
    assert len(cut) <= 50 and cut.endswith("…") and not cut.endswith(" …")
    assert ds.excerpt("short", 50) == "short"
    assert ds.abstract_excerpt("text", "full") == "text" and ds.abstract_excerpt("text", "facts_only") == "text"
    assert ds.abstract_excerpt("text", "none") == "" and ds.abstract_excerpt("", "full") == ""
    assert ds.policy_level("cc_by", {}) == "full" and ds.policy_level("cc_no", {}) == "facts_only"
    assert ds.policy_level("cc_by", {"fulltext_policy": "none"}) == "none"       # the recorded level wins
    assert ds.tier_of("journal tier A; instruments", "article") == "A"
    assert ds.tier_of("journal tier ?", "article") == "other" and ds.tier_of(None, "preprint") == "preprint"
    assert ds.source_kind("rss:Some Journal") == "rss" and ds.source_kind("Crossref") == "crossref" and ds.source_kind("") == "unknown"


def test_records_fields_ordering_and_policy(tmp_path):
    db = _seed(tmp_path)
    result = _collect(db)
    records = result["records"]
    assert [r["id"] for r in records] == ["doi:10.1000/s1", "doi:10.1000/s5", "doi:10.1000/s2", "doi:10.1000/s3"]
    assert result["stats"]["total_in_window"] == 4 and result["stats"]["records"] == 4   # s4 is outside the window
    dump = json.dumps(records, ensure_ascii=False)
    assert "SECRETMETHODS" not in dump and "SECRETBODY" not in dump                 # never full text
    assert "Hidden abstract" not in dump                                             # level 'none': no excerpt

    r1 = records[0]
    assert r1["t"] == "Paper 1 on spatial profiling" and r1["j"] == "Nature Methods" and r1["d"] == "2026-10-14"
    assert r1["doi"] == "10.1000/s1" and r1["u"] == "https://example.org/1"
    assert r1["k"] == "article" and r1["src"] == "crossref" and r1["tier"] == "A" and r1["s"] == 5.0
    assert r1["a"].startswith("Spatial profiling") and len(r1["a"]) <= ds.SNIPPET_CHARS and r1["a"].endswith("…")
    assert r1["in"] == ["inst-a"] and r1["co"] == ["vendor-one"] and r1["sg"] == ["long_read"]
    assert "c" not in r1                                                             # no citation count stored

    r2 = records[2]
    assert "<" not in r2["t"] and ">" not in r2["t"] and r2["t"].startswith("Gate-tunable WSe 2 moiré")
    assert r2["a"] == "Abstract two." and r2["tier"] == "preprint" and r2["in"] == ["inst-b"]

    r3 = records[3]
    assert "a" not in r3 and r3["s"] == 1.0

    r5 = records[1]
    assert "a" not in r5 and "u" not in r5                                           # URL == DOI resolver: omitted
    assert r5["cc"] == ["US", "GB"] and r5["sg"] == ["spatial"] and r5["src"] == "rss" and r5["tier"] == "other"


def test_cap_and_byte_budget(tmp_path):
    db = _seed(tmp_path)
    capped = _collect(db, max_records=2)
    assert [r["id"] for r in capped["records"]] == ["doi:10.1000/s1", "doi:10.1000/s5"]
    assert capped["stats"]["dropped_by_cap"] == 2 and capped["stats"]["dropped_by_budget"] == 0
    # the vocabulary counts only what is indexed, so facets and results agree
    assert {e["id"] for e in capped["entities"]["instruments"]} == {"inst-a"}

    everything = _collect(db, max_bytes=0)
    assert len(everything["records"]) == 4 and everything["stats"]["dropped_by_budget"] == 0
    full_size = ds.write_files(everything, tmp_path / "full")[ds.INDEX_FILE]

    # one byte short of the full index: the lowest-ranked record goes, the file fits the budget
    budget = full_size - 1
    tight = _collect(db, max_bytes=budget)
    assert [r["id"] for r in tight["records"]] == ["doi:10.1000/s1", "doi:10.1000/s5", "doi:10.1000/s2"]
    assert tight["stats"]["dropped_by_budget"] == 1 and tight["stats"]["dropped_by_cap"] == 0
    assert ds.write_files(tight, tmp_path / "tight")[ds.INDEX_FILE] <= budget

    # a budget too small for any record yields an empty, still valid index
    nothing = _collect(db, max_bytes=200)
    assert nothing["records"] == [] and nothing["stats"]["dropped_by_budget"] == 4


def test_entities_vocabulary_and_card_links(tmp_path):
    db = _seed(tmp_path)
    plain = _collect(db)["entities"]
    inst = {e["id"]: e for e in plain["instruments"]}
    assert inst["inst-a"] == {"id": "inst-a", "name": "Instrument A", "papers": 1, "vendor": "Vendor One", "ticker": "VONE", "category": "spatial"}
    assert inst["inst-b"]["papers"] == 1 and "ticker" not in inst["inst-b"]          # 'n/a' placeholder dropped
    assert "page" not in inst["inst-a"]                                               # no card list given: no link
    assert plain["companies"] == [{"id": "vendor-one", "name": "Vendor One", "papers": 1, "ticker": "VONE"}]
    assert plain["subgroups"] == [{"id": "long_read", "papers": 1}, {"id": "spatial", "papers": 1}]
    assert plain["sources"][0] == {"id": "biorxiv", "papers": 2}
    assert {t["id"]: t["papers"] for t in plain["tiers"]} == {"A": 1, "preprint": 2, "other": 1}
    assert {k["id"]: k["papers"] for k in plain["kinds"]} == {"article": 2, "preprint": 2}
    assert plain["countries"] == [{"id": "GB", "papers": 1}, {"id": "US", "papers": 1}]

    carded = _collect(db, carded={"instrument": {"inst-a"}, "company": set()})["entities"]
    pages = {e["id"]: e.get("page") for e in carded["instruments"]}
    assert pages == {"inst-a": "instruments.html", "inst-b": None}


def test_write_files_json_and_page(tmp_path):
    db = _seed(tmp_path)
    out = tmp_path / "site"
    files = ds.write_search_site(db, out, today=TODAY)
    assert set(files) == {ds.INDEX_FILE, ds.ENTITIES_FILE, ds.PAGE_FILE}

    index = json.loads((out / ds.INDEX_FILE).read_text(encoding="utf-8"))
    assert index["count"] == 4 and index["total_in_window"] == 4 and index["dropped"] == {"by_cap": 0, "by_budget": 0}
    assert index["schema"]["a"] == "abstract excerpt" and len(index["records"]) == 4
    entities = json.loads((out / ds.ENTITIES_FILE).read_text(encoding="utf-8"))
    assert entities["count"] == 4 and entities["instruments"][0]["id"] == "inst-a"

    html = (out / ds.PAGE_FILE).read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert dashboard.CHARTJS_URL in html and 'href="search.html" aria-current="page"' in html   # the shared shell
    assert 'id="sq"' in html and ds.INDEX_FILE in html and ds.ENTITIES_FILE in html
    assert "4 papers from 2026-04-20 to 2026-10-17 (4 in the window)" in html
    blob = html.split('<script type="application/json" id="page-data">', 1)[1].split("</script>", 1)[0]
    payload = json.loads(blob)
    assert payload["index"] == ds.INDEX_FILE and payload["count"] == 4
    assert "<" not in blob and ">" not in blob
    # records are fetched, never embedded: the injected title cannot reach the page as markup
    assert "onerror" not in html and "SECRETBODY" not in html
    # the page links out only through validated URLs and writes data with textContent
    assert "innerHTML" not in html and 'rel: "noopener noreferrer"' in html


def test_dashboard_build_emits_search_files(tmp_path):
    db = _seed(tmp_path)
    out = tmp_path / "site"
    report = dashboard.build(db, out, weeks=4, today=TODAY)
    for name in (ds.INDEX_FILE, ds.ENTITIES_FILE, ds.PAGE_FILE):
        assert (out / name).exists() and report["files"][name] == (out / name).stat().st_size
    assert report["bytes"] == sum(report["files"].values())
    html = (out / "search.html").read_text(encoding="utf-8")
    assert 'id="sq"' in html                                                      # the search page is the new one
    assert (out / "data" / "papers.json").exists()                               # the older payload is still written
    entities = json.loads((out / ds.ENTITIES_FILE).read_text(encoding="utf-8"))
    # built through the dashboard: entities with a card link to their page
    assert {e["id"]: e.get("page") for e in entities["instruments"]} == {"inst-a": "instruments.html", "inst-b": "instruments.html"}
    assert entities["companies"][0]["page"] == "companies.html"


def test_empty_and_missing_tables(tmp_path):
    st = Store(tmp_path / "empty.sqlite")
    st.commit()
    st.close()
    result = _collect(tmp_path / "empty.sqlite")
    assert result["records"] == [] and result["stats"]["records"] == 0
    out = tmp_path / "site"
    files = ds.write_files(result, out)
    assert json.loads((out / ds.INDEX_FILE).read_text(encoding="utf-8"))["records"] == []
    assert files[ds.PAGE_FILE] > 0


def test_cli_search_index(tmp_path, capsys):
    db = _seed(tmp_path)
    out = tmp_path / "cli-site"
    # a window wide enough to hold the 2025 paper whatever day the suite runs
    rc = cli.main(["search-index", "--db", str(db), "--out", str(out), "--days", "36500", "--max", "3"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "indexed 3 of 5 papers" in text and ds.INDEX_FILE in text
    index = json.loads((out / ds.INDEX_FILE).read_text(encoding="utf-8"))
    assert index["count"] == 3 and index["dropped"]["by_cap"] == 2
    assert (out / ds.PAGE_FILE).exists() and (out / ds.ENTITIES_FILE).exists()

    rc = cli.main(["search-index", "--db", str(tmp_path / "missing.sqlite"), "--out", str(out)])
    assert rc == 1 and "cannot read" in capsys.readouterr().err
