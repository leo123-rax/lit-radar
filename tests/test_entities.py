"""Entity dossiers and the watchlist, exercised against a temporary SQLite store (no network)."""
import json
import os
import sqlite3
import stat
from datetime import date, datetime, timedelta, timezone

import pytest

from litradar import entities
from litradar.extract.gazetteer import Gazetteer
from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.store import Store

INSTRUMENTS = Gazetteer.from_yaml({"entries": [
    {"id": "pacb-revio", "name": "Revio", "vendor": "Pacific Biosciences", "ticker": "PACB", "category": "sequencer.long_read", "launched": 2022,
     "aliases": ["Revio system", "PacBio Revio"], "case_sensitive_aliases": ["Revio"]},
    {"id": "ilmn-novaseq-x", "name": "NovaSeq X", "vendor": "Illumina", "ticker": "ILMN", "category": "sequencer.short_read", "launched": 2023, "aliases": ["NovaSeq X Plus"]},
    {"id": "ilmn-miseq", "name": "MiSeq", "vendor": "Illumina", "ticker": "ILMN", "category": "sequencer.short_read", "launched": 2011, "aliases": []},
    {"id": "txg-xenium", "name": "Xenium", "vendor": "10x Genomics", "ticker": "TXG", "category": "spatial", "launched": 2022, "aliases": ["Xenium Analyzer"]},
]}, "instrument")
COMPANIES = Gazetteer.from_yaml({"entries": [
    {"id": "illumina", "name": "Illumina", "ticker": "ILMN", "domains": ["illumina.com"], "aliases": ["Illumina Inc"]},
    {"id": "pacbio", "name": "Pacific Biosciences", "ticker": "PACB", "domains": ["pacb.com"], "aliases": ["PacBio"]},
    {"id": "10x-genomics", "name": "10x Genomics", "ticker": "TXG", "aliases": ["10X Genomics"]},
    {"id": "element-biosciences", "name": "Element Biosciences", "ticker": "private", "aliases": ["Element Bio"]},
    {"id": "fluent-biosciences", "name": "Fluent BioSciences", "ticker": "ILMN", "parent": "Illumina", "aliases": ["Fluent Bio"]},
]}, "company")
SUBGROUPS = [
    {"id": "long_read", "tracked_authors": [{"name": "Evan E. Eichler", "openalex_id": "A1", "institution": "Example University"}]},
    {"id": "gene_editing", "tracked_authors": [{"name": "Feng Zhang", "openalex_id": "", "institution": "Example Institute"}]},
]


def _paper(i, pub_date, authors=(), title=None, kind="preprint", journal="biorxiv", first_seen=None, source="biorxiv"):
    return Paper(
        id=f"doi:10.1000/e{i}", source=source, kind=kind, title=title or f"Paper {i} on long-read genome assembly",
        doi=f"10.1000/e{i}", abstract=f"Abstract {i} mentions methylation and ctDNA." if i % 2 else f"Abstract {i} about spatial maps.",
        journal=journal, pub_date=pub_date, url=f"https://example.org/{i}",
        authors=[Author(name=a) for a in authors], sections={"methods": f"Sequenced on a PacBio Revio instrument ({i})."},
    )


def _seed(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    # five Revio papers across three months, one of them also naming NovaSeq X and Illumina as a company
    dates = ["2026-08-05", "2026-09-10", "2026-09-20", "2026-10-02", "2026-10-11"]
    for i, d in enumerate(dates):
        authors = ["Zhang, F.", "Doe, J."] if i in (1, 4) else ["Smith, A."]
        st.upsert_paper(_paper(i, d, authors=authors))
        ms = [Mention(f"doi:10.1000/e{i}", "instrument", "pacb-revio", "Revio", "Pacific Biosciences", "PACB", "sequencer.long_read", "methods", 1, f"snippet {i}")]
        if i == 4:
            ms.append(Mention(f"doi:10.1000/e{i}", "instrument", "pacb-revio", "Revio", "Pacific Biosciences", "PACB", "sequencer.long_read", "abstract", 1, "abs snippet"))
            ms.append(Mention(f"doi:10.1000/e{i}", "instrument", "ilmn-novaseq-x", "NovaSeq X", "Illumina", "ILMN", "sequencer.short_read", "methods", 2, "nova"))
            ms.append(Mention(f"doi:10.1000/e{i}", "company", "illumina", "Illumina", "", "ILMN", "", "methods", 1, "illumina co"))
        if i in (3, 4):
            ms.append(Mention(f"doi:10.1000/e{i}", "company", "pacbio", "Pacific Biosciences", "", "PACB", "", "coi", 1, "coi snippet"))
        st.replace_mentions(f"doi:10.1000/e{i}", ms)
    # an Illumina-only paper (company as author affiliation) with a MiSeq
    st.upsert_paper(_paper(9, "2026-10-12", authors=["Eichler, E. E.", "Lee, K."], title="A MiSeq amplicon study", kind="article", journal="Nature Methods"))
    st.replace_mentions("doi:10.1000/e9", [
        Mention("doi:10.1000/e9", "company", "illumina", "Illumina", "", "ILMN", "", "affiliations", 1, "Illumina, San Diego"),
        Mention("doi:10.1000/e9", "instrument", "ilmn-miseq", "MiSeq", "Illumina", "ILMN", "sequencer.short_read", "methods", 1, "MiSeq snippet"),
    ])
    st.add_tracked_author_hit("doi:10.1000/e9", "Evan E. Eichler", "long_read", "openalex_id")
    st.add_tracked_author_hit("doi:10.1000/e4", "Feng Zhang", "gene_editing", "name")
    # a paper with a CJK title and an undated paper (first_seen only)
    st.upsert_paper(_paper(20, "2026-10-13", authors=["Wang, L."], title="甲基化 ctDNA 检测 in plasma"))
    st.upsert_paper(_paper(21, "", authors=["Undated, U."], title="Undated feed item mentioning Xenium"))
    # author-name forms from other sources: Europe PMC "Surname I", a CJK-only name, romanised surname-first order
    st.upsert_paper(_paper(30, "2026-07-15", authors=["Zhang F", "Doe J"], kind="article", journal="Example Journal", source="europepmc"))
    st.upsert_paper(_paper(32, "2026-10-14", authors=["张锋", "Wang, L."], kind="article", journal="Example Journal", source="openalex"))
    st.upsert_paper(_paper(33, "2026-06-20", authors=["Zhang Feng", "Li Wei"], kind="article", journal="Example Journal", source="openalex"))
    # archive records: Revio unmatched by id (as SRA writes it), Sequel-like rows matched, Illumina rows by ticker
    recs = [ArchiveRecord("sra", f"SRX{i}", f"SRP{i % 3}", "Revio", "PACBIO_SMRT", "WGS", "2026-10-1" + str(i % 7), "c", "t", "Homo sapiens", "", "", "") for i in range(5)]
    recs += [ArchiveRecord("sra", f"SRXI{i}", "SRP9", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", "2026-09-15", "c", "t", "Homo sapiens", "ilmn-novaseq-x", "Illumina", "ILMN") for i in range(3)]
    recs += [ArchiveRecord("ena", "ERX1", "ERP1", "Illumina MiSeq", "ILLUMINA", "AMPLICON", "2026-10-01", "c", "t", "soil", "ilmn-miseq", "Illumina", "ILMN")]
    st.upsert_archive_records(recs)
    st.upsert_news([
        NewsItem("n1", "news:Illumina", "Illumina launches a thing", "https://news.example/1", "2026-10-10T10:00:00Z", "", ["illumina"]),
        NewsItem("n2", "news:PacBio", "PacBio quarter", "https://news.example/2", "2026-10-09T10:00:00Z", "", ["pacbio"]),
        NewsItem("n3", "news:old", "Old Illumina news", "https://news.example/3", "2025-01-01T10:00:00Z", "", ["illumina"]),
    ])
    st.conn.execute("UPDATE news_items SET first_seen='2025-01-01T10:00:00Z' WHERE id='n3'")  # genuinely old, not just old-dated
    st.commit()
    return st


# ---------------------------------------------------------------------------------- helpers
def test_sparkline_and_month_range():
    assert entities.sparkline([]) == ""
    assert entities.sparkline([0, 0]) == "__"
    s = entities.sparkline([0, 1, 50, 100])
    assert s[0] == "_" and s[-1] == "@" and s[1] == "." and s[2] == "+" and len(s) == 4
    assert entities.sparkline([3]) == "@"
    assert entities.month_range("2025-11-20", "2026-02-03") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert entities.month_range("2026-03-01", "2026-01-01") == []
    assert entities.month_range("garbage", "2026-01-01") == []


def test_name_keys_and_matching():
    assert entities.name_key("Feng Zhang") == ("feng", "zhang")
    assert entities.name_key("Zhang, F.") == ("f", "zhang")
    assert entities.name_key("van Oudenaarden, A.") == ("a", "oudenaarden")
    assert entities.name_key("Alexander van Oudenaarden") == ("alexander", "oudenaarden")
    assert entities.name_key("Müller, Jörg") == ("jorg", "muller")
    assert entities.name_key("") is None and entities.name_key(", ") is None
    # PubMed / Europe PMC trailing-initials form, leading initials, all-caps surnames
    assert entities.name_key("Krokidis M") == ("m", "krokidis")
    assert entities.name_key("Zhang FJ") == ("f", "zhang")
    assert entities.name_key("van der Berg J") == ("j", "berg")
    assert entities.name_key("Wenjin Gu") == ("wenjin", "gu")
    assert entities.name_key("F.J. Zhang") == ("f", "zhang") and entities.name_key("FJ Zhang") == ("f", "zhang")
    assert entities.name_key("WANG J") == ("j", "wang") and entities.name_key("Evan E. Eichler") == ("evan", "eichler")
    # non-Latin names: exact compact key; a Latin name with a CJK gloss keys on the Latin part
    assert entities.name_key("张锋") == ("*", "张锋") and entities.name_key("张 锋") == ("*", "张锋")
    assert entities.name_key("Иванов, А.") == ("*", "ИвановА")
    assert entities.name_key("Wenxin Hu (胡文欣)") == ("wenxin", "hu")
    k = entities.name_key("Feng Zhang")
    assert entities.names_match(k, entities.name_key("Zhang, F."))
    assert entities.names_match(k, entities.name_key("Zhang, Feng"))
    assert entities.names_match(k, entities.name_key("Zhang F"))  # Europe PMC form
    assert entities.names_match(entities.name_key("Zhang Feng"), k)  # romanised surname-first order, both parts spelled out
    assert not entities.names_match(entities.name_key("Zhang F"), entities.name_key("Feng Z"))  # initials never swap
    assert not entities.names_match(k, entities.name_key("Fan Zhang"))  # a full first name must agree
    assert not entities.names_match(k, entities.name_key("Zhang"))  # surname-only on one side never matches
    assert not entities.names_match(k, None)
    assert entities.names_match(entities.name_key("张锋"), entities.name_key("张 锋"))
    assert not entities.names_match(entities.name_key("张锋"), k)
    assert entities._name_matches(entities.name_key("胡文欣"), "Wenxin Hu (胡文欣)")
    assert entities._name_matches(entities.name_key("Wenxin Hu"), "Wenxin Hu (胡文欣)")
    assert not entities._name_matches(entities.name_key("胡文"), "Wenxin Hu (胡文欣)")  # no CJK substring matching
    assert entities._like_needle(("*", "张锋")) == "张" and entities._like_needle(("feng", "zhang")) == "zhang"


# ---------------------------------------------------------------------------------- resolution
def test_resolve_by_id_name_alias_ticker_and_mentions(tmp_path):
    st = _seed(tmp_path)
    r = entities.resolve(st, "pacb-revio", INSTRUMENTS, COMPANIES, SUBGROUPS)
    assert (r.kind, r.entity_id, r.matched_by) == ("instrument", "pacb-revio", "id")
    assert r.identity["vendor"] == "Pacific Biosciences" and "PacBio Revio" in r.identity["aliases"]
    r = entities.resolve(st, "revio", INSTRUMENTS, COMPANIES)
    assert r.entity_id == "pacb-revio" and r.matched_by == "name"
    r = entities.resolve(st, "novaseq x plus", INSTRUMENTS, COMPANIES)
    assert r.entity_id == "ilmn-novaseq-x" and r.matched_by == "alias"
    r = entities.resolve(st, "ILMN", INSTRUMENTS, COMPANIES)  # parent wins over a subsidiary sharing the ticker
    assert (r.kind, r.entity_id, r.matched_by) == ("company", "illumina", "ticker")
    assert [c["id"] for c in r.candidates] == ["fluent-biosciences"]
    r = entities.resolve(st, "PacBio", INSTRUMENTS, COMPANIES)  # company alias beats the instrument alias "PacBio Revio"
    assert (r.kind, r.entity_id) == ("company", "pacbio")
    # a company-type institution recorded only in the mentions table
    st.replace_mentions("doi:10.1000/e9", [
        Mention("doi:10.1000/e9", "company", "unlisted:acme-genomics", "Acme Genomics", "", "", "openalex-company", "affiliations", 1, "Acme Genomics")
    ])
    st.commit()
    r = entities.resolve(st, "unlisted:acme-genomics", INSTRUMENTS, COMPANIES)
    assert r.kind == "company" and r.matched_by == "mentions" and r.name == "Acme Genomics"


def test_resolve_people_and_unknown(tmp_path):
    st = _seed(tmp_path)
    r = entities.resolve(st, "Evan Eichler", INSTRUMENTS, COMPANIES, SUBGROUPS)  # tracked in config and in hits
    assert r.kind == "person" and r.matched_by == "tracked_author" and r.identity["tracked"]
    assert r.identity["subgroups"][0]["subgroup"] == "long_read" and r.name == "Evan E. Eichler"
    r = entities.resolve(st, "J. Doe", INSTRUMENTS, COMPANIES, SUBGROUPS)  # only in author lists
    assert r.kind == "person" and r.matched_by == "authors" and "Doe, J." in r.identity["name_variants"]
    r = entities.resolve(st, "Nobody Here", INSTRUMENTS, COMPANIES, SUBGROUPS)
    assert r.kind == "unknown" and r.candidates == []
    r = entities.resolve(st, "Zhang", INSTRUMENTS, COMPANIES, SUBGROUPS)  # surname only -> not a person
    assert r.kind == "unknown"
    r = entities.resolve(st, "nova", INSTRUMENTS, COMPANIES)
    assert r.kind == "unknown" and r.candidates[0]["id"] == "ilmn-novaseq-x" and r.candidates[0]["matched_by"] == "substring"
    assert entities.resolve(st, "   ", INSTRUMENTS, COMPANIES).kind == "unknown"


def test_resolve_without_gazetteers_falls_back_to_mentions(tmp_path):
    st = _seed(tmp_path)
    r = entities.resolve(st, "pacb-revio")
    assert r.kind == "instrument" and r.matched_by == "mentions" and r.identity["vendor"] == "Pacific Biosciences"


# ---------------------------------------------------------------------------------- dossiers
def test_instrument_dossier(tmp_path):
    st = _seed(tmp_path)
    res = entities.resolve(st, "pacb-revio", INSTRUMENTS, COMPANIES, SUBGROUPS)
    d = entities.dossier(st, res, "2026-07-01", "2026-10-17", limit=3, instruments=INSTRUMENTS)
    assert d["papers_total"] == 5
    assert [m["month"] for m in d["papers_per_month"]] == ["2026-07", "2026-08", "2026-09", "2026-10"]
    assert [m["papers"] for m in d["papers_per_month"]] == [0, 1, 2, 2]
    assert d["sparkline"] == "_" + entities.SPARK_LEVELS[5] + "@@"
    # recent = 2026-09-18..10-17 (e2, e3, e4); prior = 2026-08-19..09-17 (e1 only)
    assert d["trend"]["recent_30d"] == 3 and d["trend"]["prior_30d"] == 1 and d["trend"]["change_pct"] == 200.0
    assert {s["section"]: s["papers"] for s in d["sections"]} == {"methods": 5, "abstract": 1}
    assert len(d["recent_papers"]) == 3 and d["recent_papers"][0]["id"] == "doi:10.1000/e4"
    assert d["recent_papers"][0]["sections"] == ["abstract", "methods"] and d["recent_papers"][0]["venue"] == "biorxiv (preprint)"
    co = d["co_mentioned"]
    assert [c["entity_id"] for c in co["instruments"]] == ["ilmn-novaseq-x"]
    assert [(c["entity_id"], c["papers"]) for c in co["companies"]] == [("pacbio", 2), ("illumina", 1)]
    # archive fell back to the model-name substring because SRA's "Revio" never resolved to the id
    assert d["archive"]["matched_by"] == "model_name" and d["archive"]["experiments_total"] == 5
    assert d["archive"]["models"][0]["instrument_model"] == "Revio" and d["archive"]["models"][0]["studies"] == 3
    text = entities.render_dossier(d)
    assert "Revio  [instrument pacb-revio]  matched by id" in text and "matched by model_name" in text
    assert "Pacific Biosciences (2), Illumina (1)" in text
    assert "trend: last 30 days 3 vs prior 30 days 1 (+200.0%)" in text
    assert d["papers_out_of_range"] == 0 and d["archive"]["experiments_out_of_range"] == 0
    assert not st.conn.in_transaction  # the scratch-table work must not leave a transaction open on the caller's connection
    json.dumps(d)  # serialisable


def test_company_dossier_news_portfolio_archive(tmp_path):
    st = _seed(tmp_path)
    res = entities.resolve(st, "illumina", INSTRUMENTS, COMPANIES, SUBGROUPS)
    d = entities.dossier(st, res, "2026-09-01", "2026-10-17", limit=10, instruments=INSTRUMENTS)
    assert d["papers_total"] == 2
    assert {s["section"] for s in d["sections"]} == {"methods", "affiliations"}
    assert [n["id"] for n in d["news"]] == ["n1"]  # the 2025 item is outside the window; pacbio's is not tagged
    port = {p["entity_id"]: p for p in d["portfolio"]}
    assert port["ilmn-novaseq-x"]["papers"] == 1 and port["ilmn-novaseq-x"]["experiments"] == 3
    assert port["ilmn-miseq"]["papers"] == 1 and port["ilmn-miseq"]["experiments"] == 1
    assert d["archive"]["matched_by"] == "ticker" and d["archive"]["experiments_total"] == 4
    co_inst = d["co_mentioned"]["instruments"]
    assert {c["entity_id"] for c in co_inst} == {"ilmn-miseq", "ilmn-novaseq-x", "pacb-revio"}
    assert co_inst[0]["entity_id"] == "ilmn-novaseq-x" and co_inst[0]["mentions"] == 2  # ties on papers break on mention count
    text = entities.render_dossier(d)
    assert "News items tagged with this company (1)" in text and "This vendor's instruments" in text


def test_company_dossier_private_ticker_uses_vendor_name(tmp_path):
    st = _seed(tmp_path)
    st.upsert_archive_records([ArchiveRecord("sra", "SRXE1", "SRPE", "Element AVITI", "ELEMENT", "WGS", "2026-10-05", "c", "t", "x", "", "Element Biosciences", "private")])
    st.commit()
    res = entities.resolve(st, "Element Bio", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, res, "2026-09-01", "2026-10-17", instruments=INSTRUMENTS)
    assert d["papers_total"] == 0 and d["archive"]["matched_by"] == "vendor" and d["archive"]["experiments_total"] == 1
    assert d["recent_papers"] == [] and d["portfolio"] == [] and d["trend"]["change_pct"] is None
    assert "none in the window" in entities.render_dossier(d)


def test_person_dossier(tmp_path):
    st = _seed(tmp_path)
    res = entities.resolve(st, "Feng Zhang", INSTRUMENTS, COMPANIES, SUBGROUPS)
    assert res.kind == "person" and res.identity["hit_names"] == ["Feng Zhang"]
    d = entities.dossier(st, res, "2026-08-01", "2026-10-17", limit=10, subgroups=SUBGROUPS)
    assert d["papers_total"] == 2 and {p["id"] for p in d["recent_papers"]} == {"doi:10.1000/e1", "doi:10.1000/e4"}
    assert d["recent_papers"][0]["snippet"] == "author: Zhang, F." and d["recent_papers"][0]["sections"] == ["authors"]
    assert [h["id"] for h in d["tracked_hits"]] == ["doi:10.1000/e4"] and d["tracked_hits"][0]["matched_by"] == "name"
    assert [c["entity_id"] for c in d["co_mentioned"]["instruments"]] == ["pacb-revio", "ilmn-novaseq-x"]
    text = entities.render_dossier(d)
    assert "tracked in: gene_editing (Example Institute)" in text and "Tracked-author hits (1)" in text


def test_dossier_for_unknown_and_undated_papers(tmp_path):
    st = _seed(tmp_path)
    res = entities.resolve(st, "nothing-at-all", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, res, "2026-01-01", "2026-10-17")
    assert d["kind"] == "unknown" and d["papers_total"] == 0 and "papers_per_month" not in d
    assert "0 papers" in entities.render_dossier(d)
    # undated papers count through first_seen, in the current month
    st.replace_mentions("doi:10.1000/e21", [Mention("doi:10.1000/e21", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "title", 1, "x")])
    st.commit()
    res = entities.resolve(st, "txg-xenium", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, res, "2026-01-01")
    assert d["papers_total"] == 1 and d["recent_papers"][0]["pub_date"] == "" and sum(m["papers"] for m in d["papers_per_month"]) == 1


def test_malformed_rows_do_not_crash(tmp_path):
    st = _seed(tmp_path)
    st.conn.execute("UPDATE papers SET authors_json='{not json' WHERE id='doi:10.1000/e1'")
    st.conn.execute("UPDATE papers SET authors_json=NULL WHERE id='doi:10.1000/e2'")
    st.conn.execute("UPDATE papers SET authors_json='[\"Zhang, F.\", 5, null]' WHERE id='doi:10.1000/e3'")  # odd but tolerated
    st.conn.execute("UPDATE news_items SET companies_json='garbage' WHERE id='n1'")
    st.commit()
    res = entities.resolve(st, "Feng Zhang", INSTRUMENTS, COMPANIES, SUBGROUPS)
    d = entities.dossier(st, res, "2026-01-01", "2026-10-17")
    assert {p["id"] for p in d["recent_papers"]} == {"doi:10.1000/e3", "doi:10.1000/e4", "doi:10.1000/e30", "doi:10.1000/e33"}
    res = entities.resolve(st, "illumina", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, res, "2026-01-01", "2026-10-17", instruments=INSTRUMENTS)
    assert d["news"] == []
    assert entities._authors_match("{bad", ("f", "zhang")) is None and entities._authors_match('{"a": 1}', ("f", "zhang")) is None


# ---------------------------------------------------------------------------------- watchlist
def test_watchlist_add_list_remove_and_kinds(tmp_path):
    st = _seed(tmp_path)
    assert entities.watch_list(st) == []  # table created lazily
    a = entities.watch_add(st, "methylation ctDNA", instruments=INSTRUMENTS, companies=COMPANIES, subgroups=SUBGROUPS)
    assert a["created"] and a["kind"] == "term" and a["term"] == "methylation ctDNA"
    b = entities.watch_add(st, "Revio", instruments=INSTRUMENTS, companies=COMPANIES)
    assert b["kind"] == "entity" and b["term"] == "pacb-revio" and b["resolved"]["matched_by"] == "name"
    c = entities.watch_add(st, "Feng Zhang", instruments=INSTRUMENTS, companies=COMPANIES, subgroups=SUBGROUPS)
    assert c["kind"] == "author" and c["term"] == "Feng Zhang"
    d = entities.watch_add(st, "Xenium", kind="term")  # forced kind keeps the text as typed
    assert d["kind"] == "term" and d["term"] == "Xenium"
    again = entities.watch_add(st, "METHYLATION ctdna")
    assert again["created"] is False and again["term"] == "methylation ctDNA"  # case-insensitive primary key
    rows = entities.watch_list(st)
    assert [(r["term"], r["kind"]) for r in rows] == [("methylation ctDNA", "term"), ("pacb-revio", "entity"), ("Feng Zhang", "author"), ("Xenium", "term")]
    assert all(r["added_at"].endswith("Z") for r in rows)
    assert entities.watch_remove(st, "Xenium") == "Xenium" and entities.watch_remove(st, "Xenium") is None
    # remove accepts the spelling add resolved: the name "Revio" was stored as the id pacb-revio
    assert entities.watch_remove(st, "Revio") is None  # without gazetteers there is nothing to resolve against
    assert entities.watch_remove(st, "Revio", instruments=INSTRUMENTS, companies=COMPANIES) == "pacb-revio"
    assert [r["term"] for r in entities.watch_list(st)] == ["methylation ctDNA", "Feng Zhang"]
    with pytest.raises(ValueError):
        entities.watch_add(st, "  ")
    with pytest.raises(ValueError):
        entities.watch_add(st, "x", kind="bogus")
    assert "watchlist" in {r[0] for r in st.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_watch_run_reports_matches_per_kind(tmp_path):
    st = _seed(tmp_path)
    entities.watch_add(st, "methylation AND ctDNA")  # explicit FTS5 operator: AND of the two words
    entities.watch_add(st, "methylation ctDNA")  # plain multi-word term: matched as a PHRASE (the abstracts say "methylation and ctDNA")
    entities.watch_add(st, "pacb-revio", instruments=INSTRUMENTS, companies=COMPANIES)
    entities.watch_add(st, "Feng Zhang", instruments=INSTRUMENTS, companies=COMPANIES, subgroups=SUBGROUPS)
    entities.watch_add(st, "甲基化", kind="term")  # CJK: exact substring path
    entities.watch_add(st, "ghost-entity", kind="entity")  # unresolvable entity id -> free-text fallback
    entities.watch_add(st, 'weird "quote', kind="term")  # invalid FTS syntax -> phrase fallback, no crash
    rep = entities.watch_run(st, "2026-10-01", "2026-10-17", limit=10, instruments=INSTRUMENTS, companies=COMPANIES, subgroups=SUBGROUPS)
    by = {t["term"]: t for t in rep["terms"]}
    # odd-numbered seed papers carry "methylation and ctDNA" in the abstract: e3 (Oct 2), e9 (Oct 12), e21 (undated, first seen now)
    assert by["methylation AND ctDNA"]["kind"] == "term"
    assert {i["id"] for i in by["methylation AND ctDNA"]["items"]} == {"doi:10.1000/e3", "doi:10.1000/e9", "doi:10.1000/e21"}
    assert by["methylation ctDNA"]["matches"] == 0 and by["methylation ctDNA"]["items"] == []  # phrase: "and" sits between the words
    assert by["pacb-revio"]["resolved"]["id"] == "pacb-revio" and [i["id"] for i in by["pacb-revio"]["items"]] == ["doi:10.1000/e4", "doi:10.1000/e3"]
    assert "methods" in by["pacb-revio"]["items"][0]["sections"]
    assert [i["id"] for i in by["Feng Zhang"]["items"]] == ["doi:10.1000/e4"] and by["Feng Zhang"]["items"][0]["snippet"] == "author: Zhang, F."
    assert [i["id"] for i in by["甲基化"]["items"]] == ["doi:10.1000/e20"] and by["甲基化"]["items"][0]["snippet"] == "substring match in title"
    assert by["ghost-entity"]["matches"] == 0 and "searched as free text" in by["ghost-entity"]["note"]
    assert "error" not in by['weird "quote'] and by['weird "quote']["matches"] == 0
    assert rep["total_matches"] == sum(t["matches"] for t in rep["terms"])
    text = entities.render_watch_report(rep)
    assert "## pacb-revio (entity -> pacb-revio): 2 matches" in text and "## 甲基化 (term): 1 matches" in text
    json.dumps(rep, default=str)
    # the window matters: September keeps e1 (2026-09-10) plus the undated e21, which counts through first_seen (store rule)
    rep2 = entities.watch_run(st, "2026-09-01", "2026-09-30")
    assert {i["id"] for t in rep2["terms"] if t["term"] == "methylation AND ctDNA" for i in t["items"]} == {"doi:10.1000/e1", "doi:10.1000/e21"}
    assert "empty watchlist" in entities.render_watch_report({"since": "a", "until": "b", "terms": [], "total_matches": 0})


def test_europepmc_name_form_is_found(tmp_path):
    """Europe PMC stores authors as "Surname I"; a Western-order query must still find those papers."""
    st = _seed(tmp_path)
    res = entities.resolve(st, "Feng Zhang", INSTRUMENTS, COMPANIES, SUBGROUPS)
    d = entities.dossier(st, res, "2026-07-01", "2026-07-31", subgroups=SUBGROUPS)
    assert d["papers_total"] == 1 and d["recent_papers"][0]["id"] == "doi:10.1000/e30" and d["recent_papers"][0]["snippet"] == "author: Zhang F"
    for q in ("Zhang F", "F. Zhang", "Zhang, Feng", "Feng Zhang"):
        assert entities.resolve(st, q, INSTRUMENTS, COMPANIES).kind == "person", q
    # a name that exists only in author lists, stored in three forms
    r = entities.resolve(st, "J. Doe", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, r, "2026-01-01", "2026-12-31")
    assert {p["id"] for p in d["recent_papers"]} == {"doi:10.1000/e1", "doi:10.1000/e4", "doi:10.1000/e30"}
    entities.watch_add(st, "Feng Zhang", kind="author")
    rep = entities.watch_run(st, "2026-07-01", "2026-07-31")
    assert rep["terms"][0]["matches"] == 1 and rep["terms"][0]["items"][0]["id"] == "doi:10.1000/e30"


def test_cjk_author_and_romanised_order(tmp_path):
    st = _seed(tmp_path)
    r = entities.resolve(st, "张锋", INSTRUMENTS, COMPANIES, SUBGROUPS)
    assert r.kind == "person" and r.matched_by == "authors" and r.identity["name_variants"] == ["张锋"]
    d = entities.dossier(st, r, "2026-10-01", "2026-10-17")
    assert d["papers_total"] == 1 and d["recent_papers"][0]["id"] == "doi:10.1000/e32" and d["recent_papers"][0]["snippet"] == "author: 张锋"
    assert "张锋  [person]  matched by authors" in entities.render_dossier(d)
    a = entities.watch_add(st, "张锋", instruments=INSTRUMENTS, companies=COMPANIES, subgroups=SUBGROUPS)
    assert a["kind"] == "author" and a["term"] == "张锋"
    rep = entities.watch_run(st, "2026-10-01", "2026-10-17")
    assert rep["terms"][0]["matches"] == 1 and [i["id"] for i in rep["terms"][0]["items"]] == ["doi:10.1000/e32"]
    # romanised surname-first order: "Zhang Feng" finds the tracked "Feng Zhang" and the stored "Zhang Feng"
    r = entities.resolve(st, "Zhang Feng", INSTRUMENTS, COMPANIES, SUBGROUPS)
    assert r.kind == "person" and r.identity["tracked"]
    d = entities.dossier(st, r, "2026-06-01", "2026-10-17", subgroups=SUBGROUPS)
    assert {p["id"] for p in d["recent_papers"]} == {"doi:10.1000/e4", "doi:10.1000/e33"}  # "Zhang, F." / "Zhang F" (initials) do not swap
    r = entities.resolve(st, "Feng Zhang", INSTRUMENTS, COMPANIES, SUBGROUPS)
    d = entities.dossier(st, r, "2026-06-01", "2026-10-17", subgroups=SUBGROUPS)
    assert {p["id"] for p in d["recent_papers"]} == {"doi:10.1000/e1", "doi:10.1000/e4", "doi:10.1000/e30", "doi:10.1000/e33"}


def test_trend_is_measured_inside_the_window_only(tmp_path):
    st = _seed(tmp_path)
    res = entities.resolve(st, "pacb-revio", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, res, "2026-10-11", "2026-10-17", instruments=INSTRUMENTS)
    t = d["trend"]
    assert t["recent_30d"] == 1 and t["recent_from"] == "2026-10-11" and t["recent_days"] == 7
    assert t["prior_30d"] is None and t["change_pct"] is None and "before the window" in t["note"]
    assert "trend: last 7 days 1 vs prior 30 days n/a (window starts 2026-10-11)" in entities.render_dossier(d)
    assert not st.conn.in_transaction
    # a 60-day window measures both periods
    d = entities.dossier(st, res, "2026-08-19", "2026-10-17", instruments=INSTRUMENTS)
    assert d["trend"]["prior_30d"] == 1 and d["trend"]["recent_30d"] == 3 and d["trend"]["recent_days"] == 30


def test_out_of_window_months_are_counted_not_plotted(tmp_path):
    st = _seed(tmp_path)
    st.replace_mentions("doi:10.1000/e21", [Mention("doi:10.1000/e21", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "title", 1, "x")])
    st.conn.execute("UPDATE papers SET first_seen='2026-11-03T00:00:00Z' WHERE id='doi:10.1000/e21'")  # undated, first seen after --until
    st.upsert_archive_records([ArchiveRecord("sra", "SRXX1", "SRPX", "Xenium", "OTHER", "OTHER", "2026-1-5", "c", "t", "x", "txg-xenium", "10x Genomics", "TXG")])  # malformed date
    st.commit()
    res = entities.resolve(st, "txg-xenium", INSTRUMENTS, COMPANIES)
    d = entities.dossier(st, res, "2026-01-01", "2026-10-17", instruments=INSTRUMENTS)
    assert d["papers_total"] == 1 and d["papers_out_of_range"] == 1 and sum(m["papers"] for m in d["papers_per_month"]) == 0
    assert [m["month"] for m in d["papers_per_month"]] == entities.month_range("2026-01-01", "2026-10-17")
    assert d["archive"]["experiments_total"] == 1 and d["archive"]["experiments_out_of_range"] == 1
    assert sum(m["experiments"] for m in d["archive"]["per_month"]) == 0 and len(d["archive"]["per_month"]) == 10
    text = entities.render_dossier(d)
    assert "1 paper(s) dated outside the window months" in text and "1 experiment(s) carry a first_public outside" in text
    series, spark, extra = entities._zero_filled(["2026-08", "2026-09"], {"2026-09": 2, "2027-01": 5}, "papers")
    assert [x["papers"] for x in series] == [0, 2] and spark == "_@" and extra == 5


def test_watch_run_counts_every_match_and_shows_the_newest(tmp_path):
    st = Store(tmp_path / "w.sqlite")
    days = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(25)]
    for i, d in enumerate(days):
        st.upsert_paper(_paper(100 + i, d, authors=["Doe J"], title=f"Zebrafish 斑马鱼 study {i}"))
        st.replace_mentions(f"doi:10.1000/e{100 + i}", [Mention(f"doi:10.1000/e{100 + i}", "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "methods", 1, "x")])
    st.commit()
    entities.watch_add(st, "zebrafish", kind="term")
    entities.watch_add(st, "斑马鱼", kind="term")
    entities.watch_add(st, "txg-xenium", kind="entity", instruments=INSTRUMENTS, companies=COMPANIES)
    entities.watch_add(st, "Doe J", kind="author")
    rep = entities.watch_run(st, "2026-09-01", "2026-09-30", limit=10, instruments=INSTRUMENTS, companies=COMPANIES)
    newest = sorted(days[15:], reverse=True)
    for t in rep["terms"]:
        assert t["matches"] == 25 and t["shown"] == 10 and len(t["items"]) == 10, t["term"]
        assert [i["pub_date"] for i in t["items"]] == newest, t["term"]
    assert rep["total_matches"] == 100
    text = entities.render_watch_report(rep)
    assert "## zebrafish (term): 25 matches (showing 10)" in text and "## txg-xenium (entity -> txg-xenium): 25 matches (showing 10)" in text
    # a wider limit shows everything and drops the suffix
    rep = entities.watch_run(st, "2026-09-01", "2026-09-30", limit=50)
    assert all(t["matches"] == t["shown"] == 25 for t in rep["terms"]) and "(showing" not in entities.render_watch_report(rep)


def test_read_only_database_keeps_show_working(tmp_path, monkeypatch, capsys):
    from litradar import cli

    st = _seed(tmp_path)
    st.close()
    path = tmp_path / "e.sqlite"
    for p in (path, tmp_path / "e.sqlite-wal", tmp_path / "e.sqlite-shm"):
        if p.exists():
            os.chmod(p, stat.S_IRUSR)
    if os.access(path, os.W_OK):
        pytest.skip("filesystem ignores read-only bits")
    ro = Store(path)
    assert entities.ensure_watchlist_schema(ro) is False
    assert entities.watch_list(ro) == []
    with pytest.raises(sqlite3.OperationalError):
        entities.watch_add(ro, "anything")
    res = entities.resolve(ro, "pacb-revio", INSTRUMENTS, COMPANIES)
    d = entities.dossier(ro, res, "2026-07-01", "2026-10-17", instruments=INSTRUMENTS)  # TEMP tables are fine read-only
    assert d["papers_total"] == 5 and not ro.conn.in_transaction
    ro.conn.close()
    # the CLI turns the read-only error into one line and exit 1, and `show` still works
    monkeypatch.setenv("LITRADAR_DB", str(path))
    assert cli.main(["watch", "add", "foo"]) == 1
    assert "read-only" in capsys.readouterr().out
    assert cli.main(["watch", "remove", "foo"]) == 1
    assert "read-only" in capsys.readouterr().out
    assert cli.main(["watch", "list"]) == 0 and "watchlist is empty" in capsys.readouterr().out
    assert cli.main(["show", "pacb-revio", "--since", "2026-07-01", "--until", "2026-10-17"]) == 0
    assert "5 papers" in capsys.readouterr().out
    for p in (path, tmp_path / "e.sqlite-wal", tmp_path / "e.sqlite-shm"):
        if p.exists():
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------------- CLI
def test_cli_show_and_watch(tmp_path, monkeypatch, capsys):
    from litradar import cli

    st = _seed(tmp_path)
    st.close()
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "e.sqlite"))
    assert cli.main(["show", "pacb-revio", "--since", "2026-07-01", "--until", "2026-10-17", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["entity_id"] == "pacb-revio" and d["papers_total"] == 5 and d["window"] == {"since": "2026-07-01", "until": "2026-10-17"}
    assert cli.main(["show", "illumina", "--since", "90d"]) == 0
    assert "Illumina  [company illumina]  matched by id" in capsys.readouterr().out
    assert cli.main(["show", "no-such-thing"]) == 1
    assert "nothing in the gazetteers" in capsys.readouterr().out
    assert cli.main(["watch", "add", "methylation AND ctDNA"]) == 0
    assert "added: 'methylation AND ctDNA' as term" in capsys.readouterr().out
    assert cli.main(["watch", "add", "Revio", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["term"] == "pacb-revio"
    assert cli.main(["watch", "list"]) == 0
    out = capsys.readouterr().out
    assert "methylation AND ctDNA" in out and "pacb-revio" in out
    assert cli.main(["watch", "run", "--since", "2026-10-01", "--until", "2026-10-17", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert {t["term"]: t["matches"] for t in rep["terms"]} == {"methylation AND ctDNA": 3, "pacb-revio": 2}
    assert cli.main(["watch", "remove", "Revio"]) == 0  # the same spelling `add` accepted removes the stored id
    assert "removed 'pacb-revio' (given as 'Revio')" in capsys.readouterr().out
    assert cli.main(["watch", "remove", "pacb-revio"]) == 1
    assert "was not on the watchlist" in capsys.readouterr().out


def test_cli_errors_are_messages_not_tracebacks(tmp_path, monkeypatch, capsys):
    from litradar import cli

    st = _seed(tmp_path)
    st.close()
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "e.sqlite"))
    assert cli.main(["watch", "add", "   "]) == 1
    assert "watch: empty watch term" in capsys.readouterr().out
    # --since / --until: YYYY-MM-DD or Nd, validated by the parser itself (usage error, exit 2)
    with pytest.raises(SystemExit) as exc:
        cli.main(["show", "illumina", "--until", "garbage"])
    assert exc.value.code == 2 and "argument --until: must be YYYY-MM-DD or Nd (for example 90d), got 'garbage'" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["show", "illumina", "--since", "2026-13-01"])
    assert exc.value.code == 2 and "argument --since: is not a real calendar date: 2026-13-01" in capsys.readouterr().err
    assert cli.main(["show", "illumina", "--since", "2026-10-17", "--until", "2026-10-01"]) == 2
    assert "is after --until" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["watch", "run", "--since", "7x"])
    assert exc.value.code == 2 and "argument --since: must be YYYY-MM-DD or Nd" in capsys.readouterr().err
    assert cli.main(["show", "illumina", "--until", "30d", "--json"]) == 0  # Nd works for --until too (N days before today)
    d = json.loads(capsys.readouterr().out)
    assert d["window"]["until"] == (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
    assert cli.main(["watch", "run", "--since", "2026-10-01", "--until", "2026-10-17"]) == 0
    assert "empty watchlist" in capsys.readouterr().out
