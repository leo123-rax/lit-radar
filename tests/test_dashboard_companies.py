"""Company profile pages and the co-mention graph page of the static dashboard, built from a seeded store.

A company with COMPANY_PAGE_MIN_PAPERS distinct papers in the window gets companies/<slug>.html; one below the
threshold does not. companies.html links to every profile and to companies/graph.html; the graph page pins d3
with its SRI hash and ships the same pairs as a table payload. Names are HTML-escaped on the server side and the
inline JSON payload can never close its own script tag. An empty store still writes the graph page.
"""
import json
from datetime import date

from litradar import dashboard
from litradar.models import Author, Mention, NewsItem, Paper
from litradar.store import Store

TODAY = date(2026, 10, 17)
NASTY = 'Acme <b>Bio</b> & "Co"'


def _paper(i, pub_date):
    return Paper(id=f"doi:10.1000/dc{i}", source="biorxiv", kind="preprint", title=f"Paper {i}", doi=f"10.1000/dc{i}",
                 abstract="A.", journal="J", pub_date=pub_date, url=f"https://example.org/dc{i}",
                 authors=[Author(name="A. R.", affiliation="Inst")], affiliations=["Inst"], sections={"methods": "M."})


def _m(pid, kind, eid, name, section, ticker=""):
    return Mention(pid, kind, eid, name, "", ticker, "", section, 1, "...")


def _seed(tmp_path, mentions=True):
    db = tmp_path / "radar.sqlite"
    st = Store(db)
    for i, d in enumerate(("2026-10-13", "2026-10-14", "2026-10-06", "2026-09-29"), start=1):
        st.upsert_paper(_paper(i, d))
    if mentions:
        st.replace_mentions("doi:10.1000/dc1", [
            _m("doi:10.1000/dc1", "company", "acme-bio", NASTY, "affiliations", "ACME"),
            _m("doi:10.1000/dc1", "company", "beta-tools", "Beta Tools", "methods"),
            _m("doi:10.1000/dc1", "instrument", "inst-x", "Instrument X", "methods"),
        ])
        st.replace_mentions("doi:10.1000/dc2", [_m("doi:10.1000/dc2", "company", "acme-bio", NASTY, "methods", "ACME")])
        st.replace_mentions("doi:10.1000/dc3", [_m("doi:10.1000/dc3", "company", "acme-bio", NASTY, "coi", "ACME")])
        st.upsert_news([NewsItem("n1", "feed", "Acme news", "https://example.org/n1", "2026-10-13", "", ["acme-bio"])])
    st.conn.execute("UPDATE papers SET first_seen='2026-10-13T00:00:00Z'")
    st.commit()
    st.close()
    return db


def _blob(text):
    blob = text.split('<script type="application/json" id="page-data">', 1)[1].split("</script>", 1)[0]
    assert "<" not in blob and ">" not in blob
    return json.loads(blob)


def test_profile_pages_graph_and_links(tmp_path):
    db = _seed(tmp_path)
    out = tmp_path / "site"
    report = dashboard.build(db, out, weeks=4, today=TODAY)
    assert report["company_pages"] == {"pages": 1, "min_papers": dashboard.COMPANY_PAGE_MIN_PAPERS, "graph_nodes": 2, "graph_links": 1}
    assert "company_pages" not in report["counts"]  # the existing counts block is unchanged for its consumers
    assert (out / "companies" / "acme-bio.html").exists()
    assert not (out / "companies" / "beta-tools.html").exists()      # one paper: below the threshold
    assert report["files"]["companies/acme-bio.html"] > 0 and report["files"]["companies/graph.html"] > 0

    companies = (out / "companies.html").read_text(encoding="utf-8")
    assert 'href="companies/acme-bio.html"' in companies and 'href="companies/graph.html"' in companies
    assert "Acme &lt;b&gt;Bio&lt;/b&gt; &amp; &quot;Co&quot;" in companies and "<b>Bio</b>" not in companies
    payload = _blob(companies)
    pages = {e["id"]: e.get("page") for e in payload["entities"]}
    assert pages["acme-bio"] == "companies/acme-bio.html" and pages["beta-tools"] == ""

    detail = (out / "companies" / "acme-bio.html").read_text(encoding="utf-8")
    assert detail.startswith("<!doctype html>")
    assert '<link rel="stylesheet" href="../assets/styles.css">' in detail and 'src="../assets/app.js"' in detail
    assert 'href="../companies.html" aria-current="page"' in detail and 'href="../index.html"' in detail
    assert "<title>Acme &lt;b&gt;Bio&lt;/b&gt; &amp; &quot;Co&quot;</title>" in detail and "<b>Bio</b>" not in detail
    assert "litradar.renderCompanyDetail(" in detail
    for slot in ("comp-months", "comp-roles", "comp-papers", "comp-instruments", "comp-companies", "comp-news"):
        assert f'id="{slot}"' in detail, slot
    d = _blob(detail)
    assert d["id"] == "acme-bio" and d["windows"]["90d"]["papers"] == 3 and d["windows"]["90d"]["roles"]["author"] == 1
    assert len(d["monthly"]) == dashboard.COMPANY_PAGE_MONTHS and d["monthly"][-1]["month"] == "2026-10"
    assert d["instruments"][0]["id"] == "inst-x" and d["companies"][0]["id"] == "beta-tools" and d["companies"][0]["page"] == ""
    assert d["news"][0]["title"] == "Acme news" and d["window_papers"] == 3

    graph_html = (out / "companies" / "graph.html").read_text(encoding="utf-8")
    assert dashboard.D3_URL in graph_html and dashboard.D3_SRI in graph_html and 'crossorigin="anonymous"' in graph_html
    assert dashboard.CHARTJS_URL in graph_html and "litradar.renderCompanyGraph(" in graph_html
    g = _blob(graph_html)
    assert g["links"] == [{"source": "acme-bio", "target": "beta-tools", "weight": 1}] and g["default_min"] == 1
    assert [n["id"] for n in g["nodes"]] == ["acme-bio", "beta-tools"] and g["nodes"][0]["page"] == "companies/acme-bio.html"
    assert "1 companies and 1 co-mention pairs" not in graph_html  # the lede counts nodes: 2 companies, 1 pair
    assert "2 companies and 1 co-mention pairs" in graph_html

    data = json.loads((out / "data" / "companies-graph.json").read_text(encoding="utf-8"))
    assert data["links"] == g["links"]
    assert "renderCompanyDetail" in (out / "assets" / "app.js").read_text(encoding="utf-8")
    assert ".graph svg" in (out / "assets" / "styles.css").read_text(encoding="utf-8")


def test_default_min_weight_caps_default_edges():
    links = [{"source": f"a{i}", "target": f"b{i}", "weight": w} for i, w in enumerate([9] * 10 + [5] * 500 + [1] * 1000)]
    assert dashboard._graph_default_min_weight({"links": links}) == 5
    assert dashboard._graph_default_min_weight({"links": links[:50]}) == 1
    assert dashboard._graph_default_min_weight({"links": []}) == 1


def test_store_without_mentions_still_writes_graph_page(tmp_path):
    db = _seed(tmp_path, mentions=False)
    out = tmp_path / "site"
    report = dashboard.build(db, out, weeks=4, today=TODAY)
    assert report["company_pages"]["pages"] == 0 and report["company_pages"]["graph_links"] == 0
    assert (out / "companies" / "graph.html").exists() and not list((out / "companies").glob("*-*.html"))
    g = _blob((out / "companies" / "graph.html").read_text(encoding="utf-8"))
    assert g["nodes"] == [] and g["links"] == []
    companies = (out / "companies.html").read_text(encoding="utf-8")
    assert "No company reaches the profile threshold" in companies and 'href="companies/graph.html"' in companies
