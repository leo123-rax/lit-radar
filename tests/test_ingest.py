"""`litradar add`: identifier parsing and end-to-end adds against canned payloads (no network)."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from litradar import cli, ingest
from litradar.config import Settings
from litradar.pipeline import Pipeline

FIX = Path(__file__).parent / "fixtures" / "sample_jats.xml"

DOI = "10.1038/s41592-026-0001-1"
PMID = "40000001"
PMCID = "PMC9000001"
PRE_DOI = "10.1101/2026.01.02.600001"
ARXIV_ID = "2601.00001"


class FakeResponse:
    def __init__(self, status_code, content=b""):
        self.status_code, self.content, self.url = status_code, content, ""


class FakeHttp:
    """Answers by URL / query-parameter substring so resolution order and chaining can be asserted."""

    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls = []  # (method, url, params)

    def _lookup(self, url, params):
        key = url + " " + " ".join(str(v) for v in (params or {}).values())
        for needle, payload in self.routes:
            if needle in key:
                return payload
        return None

    def get_json(self, url, params=None, cache=False):
        self.calls.append(("json", url, params))
        return self._lookup(url, params)

    def get_text(self, url, params=None, cache=False):
        self.calls.append(("text", url, params))
        return self._lookup(url, params)

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,)):
        self.calls.append(("get", url, params))
        payload = self._lookup(url, params)
        return FakeResponse(200, payload) if payload is not None else FakeResponse(404)

    def keys(self):
        return [c[1] + " " + " ".join(str(v) for v in (c[2] or {}).values()) for c in self.calls]


def _settings(tmp_path, **overrides):
    s = Settings.from_env()
    s.db_path = tmp_path / "add.sqlite"
    s.cache_dir = tmp_path / "cache"
    s.radar = copy.deepcopy(s.radar)
    for dotted, value in overrides.items():
        node = s.radar
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return s


def _pipe(tmp_path, http, **overrides):
    return Pipeline(_settings(tmp_path, **overrides), http=http)


# ---- canned payloads ----------------------------------------------------------------------------
def epmc_record(**kw):
    rec = {
        "id": PMID, "source": "MED", "pmid": PMID, "pmcid": PMCID, "doi": DOI,
        "title": "Spatial profiling of tumours with a multiplexed assay.",
        "abstractText": "We present a single-cell and spatial transcriptomics method using Xenium.",
        "authorList": {"author": [{"fullName": "Doe J", "authorAffiliationDetailsList": {"authorAffiliation": [{"affiliation": "10x Genomics, Pleasanton, CA, USA"}]}}]},
        "journalInfo": {"journal": {"title": "Nature methods"}}, "firstPublicationDate": "2026-10-11",
        "isOpenAccess": "Y", "inEPMC": "Y", "license": "cc by", "grantsList": {"grant": [{"agency": "NIH"}]},
    }
    rec.update(kw)
    return {"hitCount": 1, "resultList": {"result": [rec]}}


def crossref_work(doi=DOI, type_="journal-article", **kw):
    msg = {
        "DOI": doi, "type": type_, "title": ["Spatial profiling of tumours with a multiplexed assay"],
        "container-title": ["Nature Methods"], "ISSN": ["1548-7091", "1548-7105"], "published": {"date-parts": [[2026, 10, 11]]},
        "URL": f"https://doi.org/{doi}", "author": [{"given": "Jane", "family": "Doe", "affiliation": [{"name": "10x Genomics"}]}],
        "funder": [{"name": "National Institutes of Health"}], "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
    }
    msg.update(kw)
    return {"status": "ok", "message-type": "work", "message": msg}


def epmc_ppr():
    return {"hitCount": 1, "resultList": {"result": [{
        "id": "PPR900001", "source": "PPR", "doi": PRE_DOI, "title": "A preprint about sequencing",
        "abstractText": "Sequenced on a NovaSeq X Plus.", "isOpenAccess": "N", "inEPMC": "N", "firstPublicationDate": "2026-01-03",
    }]}}


def biorxiv_details(server="bioRxiv", license="cc_by", versions=2):
    coll = []
    for i in range(versions):
        coll.append({
            "doi": PRE_DOI, "title": f"A preprint about sequencing (v{i + 1})", "authors": "Doe, J.; Roe, R.",
            "author_corresponding": "R Roe", "author_corresponding_institution": "Example University",
            "date": f"2026-01-0{2 + i}", "version": str(i + 1), "category": "genomics", "license": license,
            "jatsxml": f"https://www.{server.lower()}.org/content/early/2026/01/0{2 + i}/2026.01.02.600001.source.xml",
            "abstract": "Sequenced on a NovaSeq X Plus.", "published": "NA", "server": server, "type": "new results",
        })
    return {"messages": [{"status": "ok", "category": "all"}], "collection": coll}


NO_POSTS = {"messages": [{"status": "no posts found"}], "collection": []}

ARXIV_HEAD = ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom" '
              'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">')


def arxiv_feed(doi=None):
    doi_el = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    return (ARXIV_HEAD + "<opensearch:totalResults>1</opensearch:totalResults><entry>"
            f"<id>http://arxiv.org/abs/{ARXIV_ID}v1</id><title>Foundation models for single-cell atlases</title>"
            "<summary>We introduce a scalable method for single-cell data.</summary><published>2026-01-05T00:00:00Z</published>"
            f"<author><name>A Author</name></author>{doi_el}<arxiv:primary_category term=\"q-bio.GN\"/></entry></feed>")


ARXIV_EMPTY = ARXIV_HEAD + "<opensearch:totalResults>0</opensearch:totalResults></feed>"


def journal_routes():
    return [
        (f'DOI:"{DOI}"', epmc_record()),
        (f"EXT_ID:{PMID} AND SRC:MED", epmc_record()),
        (f"PMCID:{PMCID}", epmc_record()),
        (f"api.crossref.org/works/{DOI}", crossref_work()),
        (f"{PMCID}/fullTextXML", FIX.read_bytes()),
    ]


def preprint_routes(license="cc_by", medrxiv=False):
    details = biorxiv_details("medRxiv" if medrxiv else "bioRxiv", license=license)
    return [
        (f'DOI:"{PRE_DOI}"', epmc_ppr()),
        (f"api.crossref.org/works/{PRE_DOI}", crossref_work(PRE_DOI, "posted-content", title=["A preprint about sequencing"],
                                                             **{"container-title": [], "ISSN": [], "license": []})),
        (f"details/biorxiv/{PRE_DOI}", NO_POSTS if medrxiv else details),
        (f"details/medrxiv/{PRE_DOI}", details if medrxiv else NO_POSTS),
        ("2026.01.02.600001.source.xml", FIX.read_bytes()),
    ]


# ---- identifier parsing -------------------------------------------------------------------------
@pytest.mark.parametrize("raw, kind, value", [
    ("10.1038/s41592-026-0001-1", "doi", "10.1038/s41592-026-0001-1"),
    ("DOI:10.1038/S41592-026-0001-1", "doi", "10.1038/s41592-026-0001-1"),
    ("https://doi.org/10.1038/s41592-026-0001-1", "doi", "10.1038/s41592-026-0001-1"),
    ("http://dx.doi.org/10.1038/s41592-026-0001-1.", "doi", "10.1038/s41592-026-0001-1"),
    ("38409223", "pmid", "38409223"),
    ("PMID:38409223", "pmid", "38409223"),
    ("pmid: 38409223", "pmid", "38409223"),
    ("PMC10887192", "pmcid", "PMC10887192"),
    ("pmc10887192", "pmcid", "PMC10887192"),
    ("PMCID:PMC10887192", "pmcid", "PMC10887192"),
    ("PPR653043", "ppr", "PPR653043"),
    ("2510.12345", "arxiv", "2510.12345"),
    ("2510.12345v2", "arxiv", "2510.12345v2"),
    ("arXiv:2510.12345", "arxiv", "2510.12345"),
    ("q-bio/0601001", "arxiv", "q-bio/0601001"),
    ("https://arxiv.org/abs/2510.12345v1", "arxiv", "2510.12345v1"),
    ("https://arxiv.org/pdf/2510.12345.pdf", "arxiv", "2510.12345"),
    ("10.48550/arXiv.2301.08370", "arxiv", "2301.08370"),
    ("10.1101/2023.04.30.538439v2", "doi", "10.1101/2023.04.30.538439"),
    ("https://www.biorxiv.org/content/10.1101/2023.04.30.538439v2.full", "doi", "10.1101/2023.04.30.538439"),
    ("https://www.biorxiv.org/content/10.64898/2026.10.05.756623v1.full.pdf", "doi", "10.64898/2026.10.05.756623"),
    ("https://www.medrxiv.org/content/10.1101/2020.03.24.20042937v2", "doi", "10.1101/2020.03.24.20042937"),
    ("10.1101/gr.275193.120", "doi", "10.1101/gr.275193.120"),  # CSHL journal, same prefix: not a preprint, untouched
    ("https://europepmc.org/article/MED/38409223", "pmid", "38409223"),
    ("https://europepmc.org/article/PMC/PMC10887192", "pmcid", "PMC10887192"),
    ("https://europepmc.org/abstract/PPR/PPR653043", "ppr", "PPR653043"),
    ("https://pubmed.ncbi.nlm.nih.gov/38409223/", "pmid", "38409223"),
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC10887192/", "pmcid", "PMC10887192"),
    ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10887192/", "pmcid", "PMC10887192"),
    ("https://api.crossref.org/works/10.1038/s41592-026-0001-1", "doi", "10.1038/s41592-026-0001-1"),
    ("https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0300001", "doi", "10.1371/journal.pone.0300001"),
    ("https://onlinelibrary.wiley.com/doi/full/10.1002/advs.202400001", "doi", "10.1002/advs.202400001"),
    ("https://www.science.org/doi/10.1126/science.abc1234", "doi", "10.1126/science.abc1234"),
    ("<https://doi.org/10.1038/s41592-026-0001-1>", "doi", "10.1038/s41592-026-0001-1"),
])
def test_parse_identifier_forms(raw, kind, value):
    ident = ingest.parse_identifier(raw)
    assert (ident.kind, ident.value) == (kind, value)


def test_parse_identifier_keeps_server_hint():
    assert ingest.parse_identifier("https://www.medrxiv.org/content/10.1101/2020.03.24.20042937v2").server_hint == "medrxiv"
    assert ingest.parse_identifier("https://www.biorxiv.org/content/10.64898/2026.10.05.756623v1").server_hint == "biorxiv"
    assert ingest.parse_identifier("10.64898/2026.10.05.756623").server_hint == ""


@pytest.mark.parametrize("raw", [
    "", "   ", "not-an-id", "https://example.com/no/identifier/here", "PMC", "PMID:abc", "arXiv:nope",
    "https://www.biorxiv.org/content/early/2023/05/01/2023.04.30.538439",  # no DOI prefix in the URL
])
def test_parse_identifier_rejects_garbage(raw):
    with pytest.raises(ValueError):
        ingest.parse_identifier(raw)


def test_is_preprint_doi():
    assert ingest.is_preprint_doi("10.1101/2023.04.30.538439")
    assert ingest.is_preprint_doi("10.64898/2026.10.05.756623")
    assert ingest.is_preprint_doi("10.1101/000109")
    assert not ingest.is_preprint_doi("10.1101/gr.275193.120")
    assert not ingest.is_preprint_doi("10.1038/s41592-026-0001-1")


def test_read_id_file(tmp_path):
    f = tmp_path / "ids.txt"
    f.write_text("# reading list\n10.1038/s41592-026-0001-1   # why I care\n\nPMID:38409223\n  https://arxiv.org/abs/2510.12345  \n", encoding="utf-8")
    assert ingest.read_id_file(f) == ["10.1038/s41592-026-0001-1", "PMID:38409223", "https://arxiv.org/abs/2510.12345"]


# ---- end-to-end adds ----------------------------------------------------------------------------
def test_add_journal_article_with_open_access_full_text(tmp_path):
    http = FakeHttp(journal_routes())
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, [f"https://doi.org/{DOI}"], tags=["reading-list"])

    assert not r.error and r.new and r.paper_id == f"doi:{DOI}"
    assert r.resolved_via == [f"europepmc:MED:{PMID}", "crossref"]
    assert r.journal == "Nature methods" and r.pub_date == "2026-10-11" and r.kind == "article"
    assert sorted(r.sources) == ["crossref", "europepmc"]
    assert r.fulltext_level == "full" and PMCID in r.fulltext_note
    inst = {d["id"] for d in r.instruments}
    assert {"ilmn-novaseq-x", "txg-xenium", "thermo-orbitrap-astral"} <= inst
    comp = {d["id"]: d for d in r.companies}
    assert {"10x-genomics", "illumina", "twist"} <= set(comp)
    assert "coi" in comp["illumina"]["sections"]
    assert r.score is not None and r.score > 0 and "journal tier A" in r.reasons
    assert r.tags == ["reading-list"]
    # resolution order: Europe PMC before Crossref, no preprint or arXiv lookups for a journal DOI
    keys = http.keys()
    assert "europepmc" in keys[0] and "crossref" in keys[1]
    assert not any("biorxiv" in k or "arxiv" in k for k in keys)
    # stored like a pipeline paper: sections, FTS body, mentions, score, tags in extra_json
    row = pipe.store.get_paper(r.paper_id)
    sections = json.loads(row["sections_json"])
    assert "NovaSeq X Plus" in sections["methods"] and row["fulltext_available"] == 1
    extra = json.loads(row["extra_json"])
    assert extra["tags"] == ["reading-list"] and extra["fulltext_level"] == "full" and extra["issn"] == ["1548-7091", "1548-7105"]
    assert extra["added_ids"] == [f"https://doi.org/{DOI}"] and extra["added_at"]
    assert pipe.store.search("GSE000001")[0]["id"] == r.paper_id  # body landed in FTS
    assert {m["entity_id"] for m in pipe.store.mentions_for(r.paper_id)} >= {"ilmn-novaseq-x", "10x-genomics"}
    assert pipe.store.get_score(r.paper_id)["final"] == r.score
    assert "NEW" in ingest.format_result(r) and "NovaSeq X" in ingest.format_result(r)


def test_add_is_idempotent_and_unions_tags(tmp_path):
    pipe = _pipe(tmp_path, FakeHttp(journal_routes()))
    first = ingest.add_papers(pipe, [DOI], tags=["a"])[0]
    second = ingest.add_papers(pipe, [f"PMID:{PMID}"], tags=["b"])[0]
    assert first.new and not second.new and second.paper_id == first.paper_id
    assert second.tags == ["a", "b"]
    extra = json.loads(pipe.store.get_paper(first.paper_id)["extra_json"])
    assert extra["tags"] == ["a", "b"] and sorted(extra["added_ids"]) == [DOI, f"PMID:{PMID}"]
    assert extra["added_at"] == json.loads(pipe.store.get_paper(first.paper_id)["extra_json"])["added_at"]
    assert pipe.store.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
    n_mentions = pipe.store.conn.execute("SELECT COUNT(*) FROM mentions WHERE paper_id=?", (first.paper_id,)).fetchone()[0]
    assert n_mentions == len(pipe.store.mentions_for(first.paper_id))  # replaced, not appended
    assert "REFRESH" in ingest.format_result(second)


def test_pmid_and_pmcid_use_the_verified_field_syntax(tmp_path):
    http = FakeHttp(journal_routes())
    pipe = _pipe(tmp_path, http)
    results = ingest.add_papers(pipe, [PMID, f"https://europepmc.org/article/PMC/{PMCID}"], fulltext=False)
    assert all(not r.error for r in results)
    queries = [c[2]["query"] for c in http.calls if c[2] and "query" in c[2]]
    assert queries == [f"EXT_ID:{PMID} AND SRC:MED", f"PMCID:{PMCID}"]
    assert all(c[2]["resultType"] == "core" for c in http.calls if c[2] and "query" in c[2])
    # both chained to Crossref through the DOI the Europe PMC record carried
    assert sum(1 for k in http.keys() if f"crossref.org/works/{DOI}" in k) == 2
    assert results[0].fulltext_level == "none" and "disabled" in results[0].fulltext_note


def test_add_preprint_keeps_latest_version_and_fetches_jats_under_cc_licence(tmp_path):
    http = FakeHttp(preprint_routes(license="cc_by_nc_nd"))
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, [f"https://www.biorxiv.org/content/{PRE_DOI}v1.full"])
    assert not r.error and r.kind == "preprint" and r.journal == "biorxiv"
    assert r.resolved_via == ["europepmc:PPR:PPR900001", "crossref", "biorxiv v2"]
    assert r.title.endswith("(v2)") and r.url == f"https://doi.org/{PRE_DOI}"  # merge keeps the base record's URL
    assert r.fulltext_level == "full" and "cc_by_nc_nd" in r.fulltext_note
    assert {d["id"] for d in r.instruments} >= {"ilmn-novaseq-x", "txg-xenium"}
    assert any("2026.01.02.600001.source.xml" in k for k in http.keys())
    assert "preprint" in r.reasons
    row = pipe.store.get_paper(r.paper_id)
    assert row["license"] == "cc_by_nc_nd" and row["fulltext_available"] == 1
    assert json.loads(row["extra_json"])["version"] == "2"


def test_add_preprint_respects_radar_fulltext_flag(tmp_path):
    http = FakeHttp(preprint_routes())
    pipe = _pipe(tmp_path, http, **{"sources.biorxiv.fulltext": False})
    (r,) = ingest.add_papers(pipe, [PRE_DOI])
    assert not r.error and r.fulltext_level == "none" and "sources.biorxiv.fulltext" in r.fulltext_note
    assert not any("source.xml" in k for k in http.keys())
    # abstract-only annotation still happens
    assert {d["id"] for d in r.instruments} == {"ilmn-novaseq-x"}


def test_add_preprint_falls_back_to_medrxiv(tmp_path):
    http = FakeHttp(preprint_routes(medrxiv=True))
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, [PRE_DOI])
    assert not r.error and r.resolved_via[-1] == "medrxiv v2" and r.journal == "medrxiv"
    keys = http.keys()
    assert [k for k in keys if "details/" in k] == [f"https://api.biorxiv.org/details/biorxiv/{PRE_DOI} ", f"https://api.biorxiv.org/details/medrxiv/{PRE_DOI} "]


def test_medrxiv_url_hint_tries_medrxiv_first(tmp_path):
    http = FakeHttp(preprint_routes(medrxiv=True))
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, [f"https://www.medrxiv.org/content/{PRE_DOI}v2"])
    assert not r.error
    assert [k for k in http.keys() if "details/" in k] == [f"https://api.biorxiv.org/details/medrxiv/{PRE_DOI} "]


def test_restrictive_licence_keeps_mentions_but_stores_no_text(tmp_path):
    http = FakeHttp(preprint_routes(license="cc_no"))
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, [PRE_DOI])
    assert not r.error and r.fulltext_level == "facts_only"
    inst = {d["id"]: d for d in r.instruments}
    assert "thermo-orbitrap-astral" in inst and "methods" in inst["thermo-orbitrap-astral"]["sections"]  # found in the Methods we read
    row = pipe.store.get_paper(r.paper_id)
    sections = json.loads(row["sections_json"])
    assert "methods" not in sections and "coi" not in sections and row["fulltext_available"] == 0
    assert pipe.store.conn.execute("SELECT body FROM papers_fts WHERE id=?", (r.paper_id,)).fetchone()[0] == ""
    assert "emails" not in json.loads(row["extra_json"])
    assert {m["entity_id"] for m in pipe.store.mentions_for(r.paper_id)} >= {"thermo-orbitrap-astral", "illumina"}


def test_author_manuscript_in_europepmc_is_facts_only(tmp_path):
    routes = [(f'DOI:"{DOI}"', epmc_record(isOpenAccess="N", license=None)), (f"{PMCID}/fullTextXML", FIX.read_bytes())]
    pipe = _pipe(tmp_path, FakeHttp(routes))
    (r,) = ingest.add_papers(pipe, [DOI])
    assert not r.error and r.fulltext_level == "facts_only" and "crossref: no record" in r.notes
    assert "methods" not in json.loads(pipe.store.get_paper(r.paper_id)["sections_json"])
    assert "ilmn-novaseq-x" in {m["entity_id"] for m in pipe.store.mentions_for(r.paper_id)}


def test_add_arxiv_id(tmp_path):
    http = FakeHttp([(f"api/query {ARXIV_ID}", arxiv_feed())])
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, [f"arXiv:{ARXIV_ID}"])
    assert not r.error and r.paper_id == f"arxiv:{ARXIV_ID}v1" and r.kind == "preprint" and r.journal == "arXiv"
    assert r.resolved_via == [f"arxiv:{ARXIV_ID}v1"] and r.fulltext_level == "none"
    assert http.calls[0][2]["id_list"] == ARXIV_ID
    assert not any("europepmc" in k or "crossref" in k for k in http.keys())  # no DOI known: nothing to chain
    assert "watchlist" in r.reasons  # abstract names single-cell


def test_arxiv_with_journal_doi_chains_to_europepmc_and_crossref(tmp_path):
    http = FakeHttp([(f"api/query {ARXIV_ID}", arxiv_feed(doi=DOI))] + journal_routes())
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, [ARXIV_ID], fulltext=False)
    assert not r.error and r.paper_id == f"doi:{DOI}"
    assert r.resolved_via == [f"arxiv:{ARXIV_ID}v1", f"europepmc:MED:{PMID}", "crossref"]
    assert r.journal == "Nature methods" and r.kind == "article"  # the index record is the base; arXiv enriches
    assert sorted(r.sources) == ["arxiv", "crossref", "europepmc"]


def test_arxiv_unknown_id_is_reported(tmp_path):
    http = FakeHttp([("api/query 9999.99999", ARXIV_EMPTY)])
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, ["9999.99999"])
    assert r.error.startswith("no record found") and "arxiv: no record" in r.error
    assert "FAILED" in ingest.format_result(r)


def test_unknown_doi_is_reported_without_preprint_lookup(tmp_path):
    http = FakeHttp([])
    pipe = _pipe(tmp_path, http)
    (r,) = ingest.add_papers(pipe, ["10.9999/does-not-exist"])
    assert r.error == "no record found (europepmc: no record; crossref: no record)"
    assert len(http.calls) == 2


def test_malformed_identifier_makes_no_requests(tmp_path):
    http = FakeHttp(journal_routes())
    pipe = _pipe(tmp_path, http)
    results = ingest.add_papers(pipe, ["not-an-id", DOI], fulltext=False)
    assert results[0].error.startswith("unrecognised identifier") and results[0].identifier == ""
    assert not results[1].error
    assert all(DOI in k or "europepmc" in k for k in http.keys())


def test_refresh_without_fulltext_keeps_stored_text(tmp_path):
    pipe = _pipe(tmp_path, FakeHttp(journal_routes()))
    first = ingest.add_papers(pipe, [DOI])[0]
    assert first.fulltext_level == "full"
    second = ingest.add_papers(pipe, [DOI], fulltext=False)[0]
    assert second.fulltext_level == "full" and "earlier" in second.fulltext_note
    row = pipe.store.get_paper(first.paper_id)
    assert "NovaSeq X Plus" in json.loads(row["sections_json"])["methods"] and row["fulltext_available"] == 1
    assert pipe.store.search("GSE000001")[0]["id"] == first.paper_id
    assert {d["id"] for d in second.instruments} >= {"ilmn-novaseq-x", "thermo-orbitrap-astral"}


def test_tracked_author_hit_recorded_once(tmp_path):
    pipe = _pipe(tmp_path, FakeHttp(journal_routes()))
    tracked = next(a["name"] for g in pipe.subgroups for a in (g.get("tracked_authors") or []) if a.get("name"))
    routes = [(f'DOI:"{DOI}"', epmc_record(authorList={"author": [{"fullName": tracked}]}))]
    pipe = _pipe(tmp_path, FakeHttp(routes))
    r1 = ingest.add_papers(pipe, [DOI])[0]
    r2 = ingest.add_papers(pipe, [DOI])[0]
    assert r1.tracked_authors and r1.tracked_authors[0]["name"] == tracked and "tracked author" in r1.reasons
    assert r2.tracked_authors == r1.tracked_authors
    n = pipe.store.conn.execute("SELECT COUNT(*) FROM tracked_author_hits WHERE paper_id=?", (r1.paper_id,)).fetchone()[0]
    assert n == len(r1.tracked_authors)
    assert "tracked" in ingest.format_result(r2)


def test_tag_lookup_helpers(tmp_path):
    pipe = _pipe(tmp_path, FakeHttp(journal_routes() + preprint_routes()))
    ingest.add_papers(pipe, [DOI], fulltext=False, tags=["reading-list", "MRD"])
    ingest.add_papers(pipe, [PRE_DOI], fulltext=False, tags=["reading-list"])
    assert ingest.tag_counts(pipe.store) == {"reading-list": 2, "MRD": 1}
    rows = ingest.tagged_papers(pipe.store, "mrd")  # case-insensitive
    assert [r["id"] for r in rows] == [f"doi:{DOI}"] and rows[0]["tags"] == ["MRD", "reading-list"]
    assert len(ingest.tagged_papers(pipe.store)) == 2
    assert ingest.tagged_papers(pipe.store, "nothing") == []


def test_cli_lists_add_and_tags():
    out = subprocess.run([sys.executable, "-m", "litradar", "--help"], capture_output=True, text=True, check=False)
    assert out.returncode == 0 and " add " in out.stdout and " tags " in out.stdout
    out = subprocess.run([sys.executable, "-m", "litradar", "add", "--help"], capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "--no-fulltext" in out.stdout and "--tag" in out.stdout and "--file" in out.stdout


def test_cli_add_exit_codes_without_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "cli.sqlite"))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / "cache"))
    assert cli.main(["add", "not-an-identifier", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out[0]["error"].startswith("unrecognised identifier")
    assert cli.main(["add"]) == 2
    assert cli.main(["add", "--file", str(tmp_path / "missing.txt")]) == 2
    assert cli.main(["tags"]) == 0
    assert "no tags yet" in capsys.readouterr().out
