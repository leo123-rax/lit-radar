"""`litradar fetch` and the toolkit registry against canned payloads (no network).

Four representative sources, one per transport / record type: bioRxiv (get_json paging -> Paper),
Europe PMC (query building -> Paper, with --fulltext), ENA (one get_json -> ArchiveRecord) and
NIH RePORTER (post_json -> NewsItem). Plus the registry/parser contract: every source is described by
`--list`, every option a source declares exists on the parser, `--raw` emits the API's own payloads,
`--limit` caps the stream and usage errors exit 2 with a message instead of a traceback.
"""
import json

import pytest

from litradar import cli, toolkit
from litradar.http import Http
from litradar.toolkit import OPTS, REGISTRY, FetchRequest, RecordingHttp, UsageError


class FakeHttp:
    """Answers get_json / get_text / post_json / get from one queue and records every call."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.stats = {"requests": 0}

    def _next(self):
        self.stats["requests"] += 1
        return self.payloads.pop(0) if self.payloads else None

    def get_json(self, url, params=None, cache=False, **kwargs):
        self.calls.append(("GET", url, params))
        return self._next()

    def get_text(self, url, params=None, cache=False, **kwargs):
        self.calls.append(("GET", url, params))
        return self._next()

    def post_json(self, url, json_body, headers=None, ok_statuses=(200,)):
        self.calls.append(("POST", url, json_body))
        return self._next()

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,), **kwargs):
        self.calls.append(("GET", url, params))
        import requests

        body = self._next()
        if body is None:
            return None
        resp = requests.Response()
        resp.status_code = 200
        resp._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        resp.url = url
        return resp


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeHttp as the client `litradar fetch` builds; returns a setter for the payload queue."""
    holder = {}

    def make_http(settings, browser=False):
        return holder["http"]

    monkeypatch.setattr(toolkit, "make_http", make_http)

    def install(payloads):
        holder["http"] = FakeHttp(payloads)
        return holder["http"]

    return install


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------------ registry / parser contract


def test_every_source_is_listed_with_docstring_summary_and_pacing(capsys):
    assert cli.main(["fetch", "--list"]) == 0
    text = capsys.readouterr().out
    for spec in REGISTRY:
        assert f"\n{spec.name:17}" in text or text.startswith(spec.name), spec.name
        assert spec.summary.split(" ")[0] in text
    assert "api.biorxiv.org >= 0.5 s" in text  # pacing comes from http.DEFAULT_MIN_INTERVAL
    assert "OPENALEX_API_KEY" in text and "NCBI_API_KEY" in text  # access requirements
    assert "--section" in text and "--doi" in text


def test_list_json_is_machine_readable(capsys):
    assert cli.main(["fetch", "--list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    names = [d["source"] for d in data]
    assert names == [s.name for s in REGISTRY]
    for d in data:
        assert d["summary"] and d["access"] and d["pacing"] and d["record"] and d["example"].startswith("litradar fetch ")
        assert set(d["options"]) >= {"--raw", "--out"}


def test_every_declared_option_exists_in_the_catalogue_and_parser():
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    p = toolkit.add_fetch_parser(sub)
    parser_flags = {a for action in p._actions for a in action.option_strings}
    for spec in REGISTRY:
        for flag in spec.options:
            assert flag in OPTS, (spec.name, flag)
            assert flag in parser_flags, (spec.name, flag)
    # module docstrings are the descriptions: nothing is duplicated in the registry
    assert toolkit.get("sra").summary.startswith("NCBI SRA via E-utilities")
    assert toolkit.get("nih_reporter") is toolkit.get("nih-reporter")


def test_no_source_prints_list_and_exits_2(capsys):
    assert cli.main(["fetch"]) == 2
    assert "litradar fetch: raw access to" in capsys.readouterr().out


def test_unknown_source_and_missing_required_option_exit_2(capsys, fake, tmp_path):
    fake([])
    assert cli.main(["fetch", "nope"]) == 2
    assert "unknown source 'nope'" in capsys.readouterr().err
    assert cli.main(["fetch", "europepmc", "--days", "3", "--out", str(tmp_path / "x.jsonl")]) == 2
    assert "europepmc needs --query, --issn, or --section" in capsys.readouterr().err
    assert cli.main(["fetch", "crossref", "--out", str(tmp_path / "y.jsonl")]) == 2
    assert cli.main(["fetch", "rss", "--out", str(tmp_path / "z.jsonl")]) == 2
    with pytest.raises(UsageError):
        toolkit.get("semantic-scholar")
        list(toolkit.records(toolkit.get("semantic-scholar"), FakeHttp([]), FetchRequest("2026-01-01", "2026-01-02", None, _ns(doi=None), _settings())))


def _ns(**kw):
    import argparse

    return argparse.Namespace(**kw)


def _settings():
    from litradar.config import Settings

    return Settings(contact_email="", ncbi_api_key="", openalex_api_key="", semantic_scholar_api_key="")


# ------------------------------------------------------------------ bioRxiv: paging, categories, Paper records, --limit, --raw

_B1 = {"doi": "10.1101/2026.10.01.1", "title": "A", "authors": "X, Y; Z, W", "date": "2026-10-10", "version": "1", "category": "genomics",
       "license": "cc_by", "jatsxml": "https://www.biorxiv.org/content/biorxiv/early/2026/10/10/2026.10.01.1.source.xml", "abstract": "abs", "published": "NA"}
_B2 = {"doi": "10.1101/2026.10.01.2", "title": "B", "authors": "Q, R", "date": "2026-10-11", "version": "1", "category": "neuroscience",
       "license": "cc_by", "jatsxml": "", "abstract": "abs", "published": "10.1038/s41586-026-0001-1"}
_B3 = {"doi": "10.1101/2026.10.01.3", "title": "C", "authors": "Q", "date": "2026-10-11", "version": "1", "category": "genomics",
       "license": "cc_by_nc", "jatsxml": "", "abstract": "abs", "published": "NA"}


def test_biorxiv_records_are_paper_json_lines_filtered_by_category(fake, tmp_path):
    http = fake([
        {"messages": [{"status": "ok", "count": 2, "total": 3}], "collection": [_B1, _B2]},
        {"messages": [{"status": "ok", "count": 1, "total": 3}], "collection": [_B3]},
    ])
    out = tmp_path / "preprints.jsonl"
    rc = cli.main(["fetch", "biorxiv", "--since", "2026-10-10", "--until", "2026-10-17", "--categories", "genomics", "--out", str(out)])
    assert rc == 0
    rows = _lines(out)
    assert [r["doi"] for r in rows] == ["10.1101/2026.10.01.1", "10.1101/2026.10.01.3"]
    assert rows[0]["source"] == "biorxiv" and rows[0]["kind"] == "preprint" and rows[0]["id"] == "doi:10.1101/2026.10.01.1"
    assert rows[0]["authors"][0] == {"name": "X, Y", "affiliation": "", "orcid": "", "openalex_id": "", "email": "", "institutions": []}
    assert rows[0]["fulltext_url"].endswith(".source.xml") and rows[0]["sections"] == {}
    assert [c[1] for c in http.calls] == [
        "https://api.biorxiv.org/details/biorxiv/2026-10-10/2026-10-17/0",
        "https://api.biorxiv.org/details/biorxiv/2026-10-10/2026-10-17/2",
    ]


def test_medrxiv_uses_the_medrxiv_server_and_limit_caps_the_stream(fake, tmp_path):
    http = fake([{"messages": [{"status": "ok", "count": 3, "total": 3}], "collection": [_B1, _B2, _B3]}])
    out = tmp_path / "m.jsonl"
    assert cli.main(["fetch", "medrxiv", "--since", "2026-10-10", "--until", "2026-10-17", "--limit", "2", "--out", str(out)]) == 0
    rows = _lines(out)
    assert len(rows) == 2 and all(r["source"] == "medrxiv" for r in rows)
    assert http.calls[0][1].startswith("https://api.biorxiv.org/details/medrxiv/")


def test_raw_mode_emits_the_api_payload_per_response(fake, tmp_path):
    page = {"messages": [{"status": "ok", "count": 1, "total": 1}], "collection": [_B1]}
    fake([page])
    out = tmp_path / "raw.jsonl"
    assert cli.main(["fetch", "biorxiv", "--since", "2026-10-10", "--until", "2026-10-17", "--raw", "--out", str(out)]) == 0
    rows = _lines(out)
    assert len(rows) == 1
    assert rows[0]["method"] == "GET" and rows[0]["url"].endswith("/2026-10-10/2026-10-17/0") and rows[0]["payload"] == page


def test_biorxiv_fulltext_fetches_the_jats_xml_and_fills_sections(fake, tmp_path):
    jats = (b'<?xml version="1.0"?><article><front><article-meta></article-meta></front><body>'
            b'<sec><title>Methods</title><p>Libraries were sequenced on a NovaSeq X Plus.</p></sec></body></article>')
    http = fake([
        {"messages": [{"status": "ok", "count": 1, "total": 1}], "collection": [_B1]},
        jats,
    ])
    out = tmp_path / "ft.jsonl"
    assert cli.main(["fetch", "biorxiv", "--since", "2026-10-10", "--until", "2026-10-17", "--fulltext", "--out", str(out)]) == 0
    rows = _lines(out)
    assert http.calls[1][1] == _B1["jatsxml"]
    assert rows[0]["fulltext_available"] is True and "NovaSeq X Plus" in rows[0]["sections"]["methods"]


# ------------------------------------------------------------------ Europe PMC: query building, records, --fulltext

_E1 = {"id": "123", "source": "MED", "pmid": "123", "pmcid": "PMC999", "doi": "10.1038/S41592-026-1", "title": "Title.", "abstractText": "Abs",
       "authorList": {"author": [{"fullName": "Doe J", "authorAffiliationDetailsList": {"authorAffiliation": [{"affiliation": "Example Institute"}]}}]},
       "journalInfo": {"journal": {"title": "Nature methods"}}, "firstPublicationDate": "2026-10-11", "isOpenAccess": "Y"}


def test_europepmc_section_query_and_records(fake, tmp_path):
    http = fake([{"resultList": {"result": [_E1]}, "nextCursorMark": "*"}])
    out = tmp_path / "e.jsonl"
    rc = cli.main(["fetch", "europepmc", "--section", "methods", "--terms", "NovaSeq X, Xenium", "--since", "2026-10-10", "--until", "2026-10-17", "--out", str(out)])
    assert rc == 0
    params = http.calls[0][2]
    assert params["query"] == '(METHODS:"NovaSeq X" OR METHODS:"Xenium") AND (FIRST_PDATE:[2026-10-10 TO 2026-10-17]) AND (OPEN_ACCESS:y)'
    assert params["resultType"] == "core" and params["cursorMark"] == "*"
    rows = _lines(out)
    assert rows[0]["id"] == "doi:10.1038/s41592-026-1" and rows[0]["journal"] == "Nature methods" and rows[0]["extra"]["pmcid"] == "PMC999"
    assert rows[0]["affiliations"] == ["Example Institute"] and rows[0]["source"] == "europepmc"


def test_europepmc_issn_query_all_access_and_verbatim_query(fake, tmp_path):
    http = fake([{"resultList": {"result": []}}, {"resultList": {"result": []}}, {"resultList": {"result": []}}, {"resultList": {"result": []}}])
    out = str(tmp_path / "q.jsonl")
    assert cli.main(["fetch", "europepmc", "--issn", "1548-7091,0028-0836", "--issn", "1476-4687", "--since", "2026-10-01", "--until", "2026-10-07", "--out", out]) == 0
    assert http.calls[0][2]["query"] == '(ISSN:"1548-7091" OR ISSN:"0028-0836" OR ISSN:"1476-4687") AND (FIRST_PDATE:[2026-10-01 TO 2026-10-07])'
    assert cli.main(["fetch", "europepmc", "--section", "COMP_INT", "--terms", "Acme", "--all-access", "--since", "2026-10-01", "--until", "2026-10-07", "--out", out]) == 0
    assert http.calls[1][2]["query"] == '(COMP_INT:"Acme") AND (FIRST_PDATE:[2026-10-01 TO 2026-10-07])'
    # a verbatim query is sent as-is when no window was asked for, and AND-ed with the window otherwise
    assert cli.main(["fetch", "europepmc", "--query", 'TITLE:"spatial" AND OPEN_ACCESS:y', "--out", out]) == 0
    assert http.calls[2][2]["query"] == 'TITLE:"spatial" AND OPEN_ACCESS:y'
    assert cli.main(["fetch", "europepmc", "--query", 'TITLE:"spatial"', "--since", "2026-10-01", "--until", "2026-10-07", "--out", out]) == 0
    assert http.calls[3][2]["query"] == '(TITLE:"spatial") AND (FIRST_PDATE:[2026-10-01 TO 2026-10-07])'


def test_europepmc_fulltext_uses_the_pmcid(fake, tmp_path):
    jats = b'<article><body><sec><title>Methods</title><p>Run on a Xenium Analyzer.</p></sec></body></article>'
    http = fake([{"resultList": {"result": [_E1]}}, jats])
    out = tmp_path / "ef.jsonl"
    assert cli.main(["fetch", "europepmc", "--query", "x", "--fulltext", "--out", str(out)]) == 0
    assert http.calls[1][1] == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC999/fullTextXML"
    rows = _lines(out)
    assert rows[0]["fulltext_available"] is True and "Xenium Analyzer" in rows[0]["sections"]["methods"]


# ------------------------------------------------------------------ ENA: ArchiveRecord


def test_ena_archive_records(fake, tmp_path):
    http = fake([[
        {"experiment_accession": "ERX1", "study_accession": "ERP1", "instrument_model": "Illumina NovaSeq X Plus", "instrument_platform": "ILLUMINA",
         "library_strategy": "WGS", "scientific_name": "Homo sapiens", "center_name": "Example Centre", "first_public": "2026-10-15", "study_title": "S"},
        {"experiment_accession": "ERX2", "study_accession": "ERP2", "instrument_model": "Revio", "instrument_platform": "PACBIO_SMRT",
         "library_strategy": "WGS", "scientific_name": "Mus musculus", "center_name": "", "first_public": "2026-10-16", "study_title": "T"},
    ]])
    out = tmp_path / "ena.jsonl"
    assert cli.main(["fetch", "ena", "--since", "2026-10-15", "--until", "2026-10-16", "--out", str(out)]) == 0
    rows = _lines(out)
    assert [r["accession"] for r in rows] == ["ERX1", "ERX2"]
    assert rows[0] == {"archive": "ena", "accession": "ERX1", "study_accession": "ERP1", "instrument_model": "Illumina NovaSeq X Plus", "platform": "ILLUMINA",
                       "library_strategy": "WGS", "first_public": "2026-10-15", "center_name": "Example Centre", "study_title": "S", "organism": "Homo sapiens",
                       "entity_id": "", "vendor": "", "ticker": ""}
    params = http.calls[0][2]
    assert params["result"] == "read_experiment" and params["limit"] == 0 and 'first_public>="2026-10-15"' in params["query"]


def test_ena_limit_is_passed_to_the_portal(fake, tmp_path):
    http = fake([[]])
    assert cli.main(["fetch", "ena", "--days", "1", "--limit", "25", "--out", str(tmp_path / "e.jsonl")]) == 0
    assert http.calls[0][2]["limit"] == 25


# ------------------------------------------------------------------ NIH RePORTER: post_json -> NewsItem


def _row(appl_id, title):
    return {"appl_id": appl_id, "project_num": f"1S10OD0{appl_id}-01", "project_title": title, "abstract_text": "Abstract.", "activity_code": "S10",
            "award_amount": 1000000, "award_notice_date": "2026-09-18T00:00:00", "fiscal_year": 2026,
            "organization": {"org_name": "EXAMPLE UNIVERSITY", "org_city": "SPRINGFIELD", "org_state": "IL", "org_country": "UNITED STATES"},
            "agency_ic_admin": {"code": "OD", "abbreviation": "OD", "name": "Office of the Director"},
            "project_detail_url": f"https://reporter.nih.gov/project-details/{appl_id}"}


def test_nih_reporter_s10_mode_posts_criteria_and_yields_news_items(fake, tmp_path):
    http = fake([{"meta": {"total": 2, "offset": 0, "limit": 500}, "results": [_row(1, "Orbitrap Astral mass spectrometer"), _row(2, "A sequencer")]}])
    out = tmp_path / "nih.jsonl"
    assert cli.main(["fetch", "nih-reporter", "--since", "2026-07-01", "--until", "2026-09-30", "--out", str(out)]) == 0
    method, url, body = http.calls[0]
    assert method == "POST" and url == "https://api.reporter.nih.gov/v2/projects/search"
    assert body["criteria"]["activity_codes"] == ["S10"]
    assert body["criteria"]["award_notice_date"] == {"from_date": "2026-07-01", "to_date": "2026-09-30"}
    rows = _lines(out)
    assert len(rows) == 2 and rows[0]["feed"] == "nih-reporter" and "Orbitrap Astral" in rows[0]["title"]
    assert rows[0]["url"] == "https://reporter.nih.gov/project-details/1" and rows[0]["published"] == "2026-09-18"


def test_nih_reporter_keyword_mode_needs_a_criterion_and_sends_terms(fake, tmp_path, capsys):
    http = fake([{"meta": {"total": 0}, "results": []}])
    out = str(tmp_path / "k.jsonl")
    assert cli.main(["fetch", "nih-reporter", "--mode", "keyword", "--since", "2026-07-01", "--until", "2026-09-30", "--out", out]) == 2
    assert "needs --terms, --activity-codes or --org" in capsys.readouterr().err
    assert cli.main(["fetch", "nih-reporter", "--mode", "keyword", "--terms", "Revio,PromethION", "--activity-codes", "R01", "--since", "2026-07-01", "--until", "2026-09-30", "--out", out]) == 0
    body = http.calls[0][2]
    assert body["criteria"]["activity_codes"] == ["R01"]
    assert body["criteria"]["advanced_text_search"]["search_text"] == '"Revio" OR "PromethION"'


# ------------------------------------------------------------------ Semantic Scholar: found / not found / request failed are distinct


def test_semantic_scholar_distinguishes_missing_dois_from_failed_batches(fake, tmp_path, monkeypatch):
    from litradar.sources import semantic_scholar as s2

    monkeypatch.setattr(s2.time, "sleep", lambda s: None)
    # batch 1 answers (one known, one unknown); batch 2 fails after the adapter's retries
    http = fake([
        [{"paperId": "p1", "externalIds": {"DOI": "10.1000/known"}, "citationCount": 7, "influentialCitationCount": 1, "publicationDate": "2024-01-02", "tldr": None}, None],
        None, None, None, None,
    ])
    out = tmp_path / "s2.jsonl"
    assert cli.main(["fetch", "semantic-scholar", "--doi", "10.1000/known,10.1000/unknown", "--out", str(out)]) == 0
    rows = _lines(out)
    assert rows[0]["doi"] == "10.1000/known" and rows[0]["found"] is True and rows[0]["citation_count"] == 7
    assert rows[1] == {"doi": "10.1000/unknown", "found": False}
    assert http.calls[0][0] == "POST" and http.calls[0][2] == {"ids": ["DOI:10.1000/known", "DOI:10.1000/unknown"]}
    monkeypatch.setattr(s2, "BATCH_LIMIT", 1)
    assert cli.main(["fetch", "semantic-scholar", "--doi", "10.1000/x", "--out", str(out)]) == 0
    rows = _lines(out)
    assert rows[0]["found"] is None and "batch request failed" in rows[0]["error"]


# ------------------------------------------------------------------ RecordingHttp forwards everything else


def test_recording_http_forwards_attributes_and_records_posts():
    inner = FakeHttp([{"ok": 1}])
    seen = []
    rec = RecordingHttp(inner, seen.append)
    assert rec.post_json("https://x/y", {"a": 1}) == {"ok": 1}
    assert seen == [{"method": "POST", "url": "https://x/y", "body": {"a": 1}, "payload": {"ok": 1}}]
    assert rec.stats is inner.stats  # attribute forwarding


def test_make_http_builds_a_plain_client_by_default():
    from litradar.config import Settings

    h = toolkit.make_http(Settings(contact_email="a@b.c"))
    assert isinstance(h, Http) and "a@b.c" in h.session.headers["User-Agent"]  # SEC-style "<tool> <email>" UA


# ------------------------------------------------------------------ exit codes: hard failures are never "0 records, exit 0"


def test_zero_records_after_failed_requests_exits_1(fake, tmp_path, capsys):
    http = fake([[]])  # the adapter yields nothing ...
    http.stats["failures"] = 1  # ... because its one request failed (HTTP 400 after retries, say)
    assert cli.main(["fetch", "ena", "--days", "1", "--out", str(tmp_path / "e.jsonl")]) == 1
    err = capsys.readouterr().err
    assert "litradar fetch ena: FAILED: 0 records and 1 failed request(s)" in err and "Traceback" not in err


def test_skipped_host_exits_1_even_with_partial_output(fake, tmp_path, capsys):
    http = fake([[{"experiment_accession": "ERX1", "study_accession": "ERP1", "instrument_model": "Revio", "instrument_platform": "PACBIO_SMRT",
                   "library_strategy": "WGS", "scientific_name": "x", "center_name": "", "first_public": "2026-10-16", "study_title": "T"}]])
    http.blocked_hosts = {"www.ebi.ac.uk"}
    out = tmp_path / "e.jsonl"
    assert cli.main(["fetch", "ena", "--days", "1", "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "host(s) skipped for the rest of the run after repeated 429s: www.ebi.ac.uk" in err and "1 record(s) were written" in err
    assert len(_lines(out)) == 1  # partial output is kept


def test_an_empty_window_without_failures_is_a_clean_exit(fake, tmp_path, capsys):
    fake([[]])
    assert cli.main(["fetch", "ena", "--days", "1", "--out", str(tmp_path / "e.jsonl")]) == 0
    assert "0 ArchiveRecord records" in capsys.readouterr().err


def test_fetch_exit_code_helper(capsys):
    assert toolkit.fetch_exit_code("sra", 0, {"failures": 0}) == 0
    assert toolkit.fetch_exit_code("sra", 12, {"failures": 3}) == 0  # partial failures with records: reported in the stats line only
    assert toolkit.fetch_exit_code("sra", 0, {"failures": 1}) == 1
    assert toolkit.fetch_exit_code("sra", 5, {"failures": 0}, blocked_hosts={"eutils.ncbi.nlm.nih.gov"}) == 1
    assert toolkit.fetch_exit_code("sra", 0, {}) == 0
    capsys.readouterr()


def test_openalex_early_stop_is_reported_and_exits_1(fake, tmp_path, capsys, monkeypatch):
    def works(http, filters, state=None, **kw):
        if state is not None:
            state["error"] = "HTTP 429: Insufficient budget"
        return iter(())

    monkeypatch.setattr(toolkit.openalex, "works", works)
    fake([])
    assert cli.main(["fetch", "openalex", "--days", "1", "--out", str(tmp_path / "o.jsonl")]) == 1
    err = capsys.readouterr().err
    assert "OpenAlex stopped early: HTTP 429: Insufficient budget" in err and "no OPENALEX_API_KEY configured" in err


def test_bad_dates_are_usage_errors(fake, tmp_path, capsys):
    fake([[]])
    out = str(tmp_path / "e.jsonl")
    assert cli.main(["fetch", "ena", "--since", "yesterday", "--out", out]) == 2
    assert "--since must be YYYY-MM-DD or Nd (for example 90d)" in capsys.readouterr().err
    assert cli.main(["fetch", "ena", "--until", "2026-13-45", "--out", out]) == 2
    assert "not a real calendar date" in capsys.readouterr().err
    assert cli.main(["fetch", "ena", "--since", "2026-10-10", "--until", "2026-10-01", "--out", out]) == 2
    assert "is after --until" in capsys.readouterr().err
    assert cli.main(["fetch", "ena", "--since", "3d", "--out", out]) == 0  # Nd works here too


def test_europepmc_usage_error_lists_the_three_selectors(fake, tmp_path, capsys):
    fake([])
    assert cli.main(["fetch", "europepmc", "--days", "3", "--out", str(tmp_path / "x.jsonl")]) == 2
    err = capsys.readouterr().err
    assert "--query" in err and "--issn" in err and "--section METHODS --terms" in err and err.count("\n") >= 3
