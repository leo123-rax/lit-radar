"""Company intelligence (litradar.companies) and the `litradar companies` command on a seeded temporary store.

Covers window / month helpers, role counting, listing filters, the detail profile (windows, monthly series,
co-mentions, recent papers, news, linked discovery candidates), entry resolution, CSV export, the co-mention
graph, the SEC exact-brand ticker proposals with a canned ticker list, the vetting table renderer, and the
four CLI actions. Nothing touches the network.
"""
import csv
import json
from datetime import date

import pytest
import yaml

from litradar import cli, discovery
from litradar import companies as C
from litradar.models import Author, Mention, NewsItem, Paper
from litradar.store import Store

TODAY = date(2026, 10, 17)

ENTRIES = [
    {"id": "acme-bio", "name": "Acme Bio", "ticker": "ACME", "type": "tools", "watch_news": False,
     "domains": ["acmebio.com"], "aliases": ["Acme Bio Inc", "Acme Biosciences"]},
    {"id": "beta-tools", "name": "Beta Tools", "ticker": "private", "type": "cro", "watch_news": False, "domains": [], "aliases": []},
    {"id": "gamma-dx", "name": "Gamma Dx", "ticker": "n/a", "type": "diagnostics", "watch_news": False, "domains": [], "aliases": []},
]


def _paper(i, pub_date, **kw):
    base = dict(id=f"doi:10.1000/co{i}", source="europepmc", kind="article", title=f"Paper {i} about things",
                doi=f"10.1000/co{i}", abstract="Abstract.", journal="J Example", pub_date=pub_date,
                url=f"https://example.org/co{i}", authors=[Author(name="A. Researcher", affiliation="Example Institute")],
                affiliations=["Example Institute"], sections={"methods": "Methods."})
    base.update(kw)
    return Paper(**base)


def _m(pid, kind, eid, name, section, ticker="", count=1):
    return Mention(pid, kind, eid, name, "", ticker, "", section, count, "...")


def seed(tmp_path):
    db = tmp_path / "radar.sqlite"
    st = Store(db)
    st.upsert_paper(_paper(1, "2026-10-13"))
    st.upsert_paper(_paper(2, "2026-10-14"))
    st.upsert_paper(_paper(3, "2026-10-06"))
    st.upsert_paper(_paper(4, "2026-03-01"))          # inside 365 days, outside 90
    st.upsert_paper(_paper(5, ""))                    # undated: bucketed by first_seen
    st.replace_mentions("doi:10.1000/co1", [
        _m("doi:10.1000/co1", "company", "acme-bio", "Acme Bio", "affiliations", "ACME"),
        _m("doi:10.1000/co1", "company", "beta-tools", "Beta Tools", "methods"),
        _m("doi:10.1000/co1", "company", "unlisted:foo-corp", "Foo Corp", "affiliations"),
        _m("doi:10.1000/co1", "instrument", "inst-x", "Instrument X", "methods"),
    ])
    st.replace_mentions("doi:10.1000/co2", [
        _m("doi:10.1000/co2", "company", "acme-bio", "Acme Bio", "coi", "ACME"),
        _m("doi:10.1000/co2", "company", "acme-bio", "Acme Bio", "methods", "ACME", count=3),
        _m("doi:10.1000/co2", "company", "beta-tools", "Beta Tools", "methods"),
        _m("doi:10.1000/co2", "instrument", "inst-x", "Instrument X", "methods"),
    ])
    st.replace_mentions("doi:10.1000/co3", [_m("doi:10.1000/co3", "company", "acme-bio", "Acme Bio", "abstract", "ACME")])
    st.replace_mentions("doi:10.1000/co4", [_m("doi:10.1000/co4", "company", "acme-bio", "Acme Bio", "methods", "ACME")])
    st.replace_mentions("doi:10.1000/co5", [_m("doi:10.1000/co5", "company", "beta-tools", "Beta Tools", "funding")])
    st.upsert_news([
        NewsItem("n1", "feed", "Acme Bio launches a thing", "https://example.org/n1", "2026-10-13", "", ["acme-bio"]),
        NewsItem("n2", "feed", "Acme Bio earlier news", "https://example.org/n2", "2026-01-10", "", ["Acme Bio"]),
        NewsItem("n3", "feed", "Unrelated", "https://example.org/n3", "2026-10-12", "", ["someone-else"]),
    ])
    st.conn.execute("UPDATE papers SET first_seen='2026-10-15T00:00:00Z'")
    discovery.ensure_schema(st.conn)
    st.conn.execute(
        "INSERT INTO candidate_companies (name_norm, display_name, evidence_json, first_seen, last_seen, papers, score, status) VALUES (?,?,?,?,?,?,?,?)",
        (discovery.normalize_name("Acme Biosciences Ltd"), "Acme Biosciences Ltd",
         json.dumps({"evidence": [], "domains": ["acmebio.com"], "variants": ["Acme Biosciences"]}), "2026-10-13", "2026-10-13", 1, 2.5, "new"),
    )
    st.conn.execute(
        "INSERT INTO candidate_companies (name_norm, display_name, evidence_json, first_seen, last_seen, papers, score, status) VALUES (?,?,?,?,?,?,?,?)",
        ("othercorp", "Other Corp", json.dumps({"evidence": [], "domains": ["other.com"], "variants": []}), "2026-10-13", "2026-10-13", 1, 2.5, "new"),
    )
    st.commit()
    st.close()
    return db


@pytest.fixture
def db(tmp_path):
    return seed(tmp_path)


@pytest.fixture
def entries():
    return [C.normalize_entry(e) for e in ENTRIES]


# ---- helpers ----------------------------------------------------------------------------------
def test_window_month_axis_and_flags():
    assert C.window(90, TODAY) == ("2026-07-19", "2026-10-17")
    assert C.month_axis(TODAY, 3) == ["2026-08", "2026-09", "2026-10"]
    assert C.month_axis(date(2026, 1, 15), 2) == ["2025-12", "2026-01"]
    assert C.is_public("ILMN") and C.is_public("300676.SZ")
    assert not C.is_public("private") and not C.is_public("n/a") and not C.is_public("") and not C.is_public(None)
    assert C.page_slug("unlisted:Foo Corp") == "unlisted-foo-corp" and C.page_slug("10x-genomics") == "10x-genomics"
    e = C.normalize_entry({"id": "x", "name": "X Co", "ticker": "", "aliases": None, "domains": ["A.COM"]})
    assert e["ticker"] == "private" and e["public"] is False and e["domains"] == ["a.com"] and e["aliases"] == []


def test_resolve_entry(entries):
    assert C.resolve_entry(entries, "acme-bio")["id"] == "acme-bio"
    assert C.resolve_entry(entries, "ACME BIO")["id"] == "acme-bio"
    assert C.resolve_entry(entries, "acme bio inc")["id"] == "acme-bio"      # alias
    assert C.resolve_entry(entries, "acme")["id"] == "acme-bio"              # ticker, case-insensitive
    assert C.resolve_entry(entries, "private") is None                       # a placeholder is never a ticker match
    assert C.resolve_entry(entries, "gamma")["id"] == "gamma-dx"             # unique substring
    assert C.resolve_entry(entries, "zzz") is None
    with pytest.raises(LookupError):
        C.resolve_entry(entries + [C.normalize_entry({"id": "acme-dx", "name": "Acme Dx"})], "acm")


# ---- counts -----------------------------------------------------------------------------------
def test_paper_counts_roles_and_windows(db):
    st = Store(db)
    since, until = C.window(90, TODAY)
    counts = C.paper_counts(st.conn, since, until)
    acme = counts["acme-bio"]
    assert acme["papers"] == 3 and acme["mentions"] == 1 + 1 + 3 + 1
    assert acme["roles"] == {"author": 1, "funding_coi": 1, "subject": 1, "supplier": 1, "other": 0}
    assert acme["last_paper"] == "2026-10-14"
    beta = counts["beta-tools"]
    assert beta["papers"] == 3 and beta["roles"]["supplier"] == 2 and beta["roles"]["funding_coi"] == 1  # co5 via first_seen
    since, until = C.window(365, TODAY)
    assert C.paper_counts(st.conn, since, until)["acme-bio"]["papers"] == 4
    assert C.news_counts(st.conn, *C.window(90, TODAY)) == {"acme-bio": 1, "someone-else": 1}
    assert C.news_counts(st.conn, *C.window(365, TODAY))["Acme Bio"] == 1
    st.close()


def test_list_companies_filters_sort_and_news(db, entries):
    st = Store(db)
    since, until = C.window(90, TODAY)
    rows = C.list_companies(st, entries, since, until)
    assert [r["id"] for r in rows] == ["acme-bio", "beta-tools", "gamma-dx"]
    assert rows[0]["news"] == 1 and rows[0]["candidates"] == 1 and rows[0]["public"] is True
    assert rows[2]["papers"] == 0 and rows[2]["last_paper"] == "" and rows[2]["public"] is False
    assert [r["id"] for r in C.list_companies(st, entries, since, until, public=True)] == ["acme-bio"]
    assert [r["id"] for r in C.list_companies(st, entries, since, until, public=False)] == ["beta-tools", "gamma-dx"]
    assert [r["id"] for r in C.list_companies(st, entries, since, until, type_="cro")] == ["beta-tools"]
    rows365 = C.list_companies(st, entries, *C.window(365, TODAY))
    assert rows365[0]["news"] == 2  # the older item is tagged by display name
    st.close()


def test_company_detail(db, entries):
    st = Store(db)
    d = C.company_detail(st, entries[0], today=TODAY)
    assert d["id"] == "acme-bio" and d["slug"] == "acme-bio" and d["public"] is True
    assert d["windows"]["90d"]["papers"] == 3 and d["windows"]["365d"]["papers"] == 4
    assert d["windows"]["90d"]["news"] == 1 and d["windows"]["365d"]["news"] == 2
    assert len(d["monthly"]) == 24 and d["monthly"][-1] == {"month": "2026-10", "papers": 3, "mentions": 6, "subject": 1}
    assert d["coverage"] is None  # too few papers per month to judge the section mix
    assert {m["month"]: m["papers"] for m in d["monthly"]}["2026-03"] == 1
    assert d["instruments"] == [{"id": "inst-x", "name": "Instrument X", "vendor": "", "ticker": "", "papers": 2, "unlisted": False}]
    assert [(c["id"], c["papers"]) for c in d["companies"]] == [("beta-tools", 2), ("unlisted:foo-corp", 1)]
    assert d["companies"][1]["unlisted"] is True
    assert [p["date"] for p in d["recent_papers"]] == ["2026-10-14", "2026-10-13", "2026-10-06", "2026-03-01"]
    assert d["recent_papers"][0]["roles"] == ["funding_coi", "supplier"] and d["recent_papers"][0]["mentions"] == 4
    assert [n["title"] for n in d["news"]] == ["Acme Bio launches a thing", "Acme Bio earlier news"]
    assert [c["display_name"] for c in d["candidates"]] == ["Acme Biosciences Ltd"]  # alias / domain match, not Other Corp
    # a minimal entry (what the dashboard builds from the store) works too
    d2 = C.company_detail(st, {"id": "beta-tools", "name": "Beta Tools", "ticker": "private"}, today=TODAY, windows=(30,), months=2)
    assert d2["windows"] == {"30d": d2["windows"]["30d"]} and d2["windows"]["30d"]["papers"] == 3 and len(d2["monthly"]) == 2
    assert d2["candidates"] == []
    st.close()


def test_export_rows_and_csv(db, entries, tmp_path):
    st = Store(db)
    rows = C.export_rows(st, entries, *C.window(90, TODAY))
    assert [r["id"] for r in rows] == ["acme-bio", "beta-tools", "gamma-dx"]
    assert rows[0]["public"] == "yes" and rows[1]["public"] == "no"
    assert rows[0]["papers_90d"] == 3 and rows[0]["supplier_90d"] == 1 and rows[0]["candidates_linked"] == 1 and rows[0]["aliases"] == 2
    out = tmp_path / "companies.csv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        assert C.write_csv(rows, fh) == 3
    with open(out, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == list(C.EXPORT_COLUMNS)
        assert [r["id"] for r in reader] == ["acme-bio", "beta-tools", "gamma-dx"]
    st.close()


def test_comention_graph(db, entries):
    st = Store(db)
    since, until = C.window(90, TODAY)
    g = C.comention_graph(st, since, until, entries)
    assert g["links"] == [{"source": "acme-bio", "target": "beta-tools", "weight": 2}]
    assert [(n["id"], n["type"], n["papers"], n["degree"]) for n in g["nodes"]] == [("acme-bio", "tools", 3, 1), ("beta-tools", "cro", 3, 1)]
    assert g["nodes"][0]["ticker"] == "ACME" and g["nodes"][1]["ticker"] == ""
    assert C.comention_graph(st, since, until, entries, min_weight=3)["links"] == []
    g2 = C.comention_graph(st, since, until, entries, include_unlisted=True)
    assert len(g2["links"]) == 3 and any(n["id"] == "unlisted:foo-corp" and n["type"] == "unlisted" for n in g2["nodes"])
    g3 = C.comention_graph(st, since, until, entries, min_weight=3, keep_isolated=True)
    assert g3["links"] == [] and len(g3["nodes"]) == 2
    st.close()


# ---- vetting helpers (no network) ---------------------------------------------------------------
def test_sec_ticker_proposals_exact_brand_only(entries):
    tickers = [
        (discovery.normalize_name("Beta Tools Inc"), "BETA", "Beta Tools Inc"),
        (discovery.normalize_name("Gamma Diagnostics"), "GMA", "Gamma Diagnostics"),
        (discovery.normalize_name("Acme Bio"), "ACMX", "Acme Bio"),
    ]
    props = C.sec_ticker_proposals(entries, tickers)
    assert props == [{"id": "beta-tools", "name": "Beta Tools", "ticker": "BETA", "title": "Beta Tools Inc", "match": 1.0}]
    # a listed company is never re-proposed; a same-brand near miss is not an exact match
    assert discovery.sec_ticker_hint("Gamma Dx", tickers) is None


def test_render_vetting_table_escapes_pipes():
    md = C.render_vetting_table([{"candidate": "A | B", "verdict": "life_science", "name": "A B", "type": "tools",
                                   "ticker": "private", "hq": "US", "what": "does\nthings", "domain": "ab.com", "action": "added"}])
    lines = md.splitlines()
    assert lines[0].startswith("| candidate | verdict |") and lines[1].startswith("|---|")
    assert "A / B" in lines[2] and "does things" in lines[2] and "\n" not in lines[2]


# ---- CLI ----------------------------------------------------------------------------------------
@pytest.fixture
def cli_env(db, tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "companies.yaml").write_text(yaml.safe_dump({"entries": ENTRIES}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    return tmp_path


def test_cli_list_show_export_graph(cli_env, capsys):
    assert cli.main(["companies", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["acme-bio", "beta-tools", "gamma-dx"]

    assert cli.main(["companies", "list", "--public"]) == 0
    out = capsys.readouterr().out
    assert "acme-bio" in out and "beta-tools" not in out and "1 companies" in out

    assert cli.main(["companies", "show", "acme-bio"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Acme Bio  [acme-bio]  ACME  type=tools") and "last  90d: 3 papers" in out and "Instrument X (2)" in out
    assert "discovery candidates linked: Acme Biosciences Ltd [new, 2.5]" in out

    assert cli.main(["companies", "show", "ACME", "--json"]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["id"] == "acme-bio" and detail["windows"]["90d"]["papers"] == 3

    assert cli.main(["companies", "show", "no-such-company"]) == 1
    assert "no company matches" in capsys.readouterr().err
    assert cli.main(["companies", "show"]) == 2

    out_csv = cli_env / "out" / "companies.csv"
    assert cli.main(["companies", "export", "--csv", str(out_csv)]) == 0
    assert "3 companies written" in capsys.readouterr().out
    text = out_csv.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(C.EXPORT_COLUMNS) and len(text.splitlines()) == 4

    assert cli.main(["companies", "export"]) == 0  # stdout
    assert capsys.readouterr().out.splitlines()[0].startswith("id,name,ticker,public")

    out_json = cli_env / "out" / "companies.json"
    assert cli.main(["companies", "graph", "--out", str(out_json)]) == 0
    assert "2 nodes, 1 links" in capsys.readouterr().out
    graph = json.loads(out_json.read_text(encoding="utf-8"))
    assert graph["links"] == [{"source": "acme-bio", "target": "beta-tools", "weight": 2}]

    assert cli.main(["companies", "graph", "--min-weight", "5"]) == 0
    assert json.loads(capsys.readouterr().out)["links"] == []
