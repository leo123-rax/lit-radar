"""NCBI GEO adapter tested against canned E-utilities payloads (no network).

Payload shapes mirror live esearch/esummary responses for db=gds captured on 2026-10-17
(see the module docstring of litradar/sources/geo.py and docs/sources/geo.md).
"""

from litradar.sources import geo


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url, params=None, cache=False):
        self.calls.append((url, params))
        return self.payloads.pop(0) if self.payloads else None

    def get_text(self, url, params=None, cache=False):
        self.calls.append((url, params))
        return self.payloads.pop(0) if self.payloads else None


def _esearch(count, webenv="MCID_abc", qk="1"):
    return {"header": {"type": "esearch", "version": "0.3"},
            "esearchresult": {"count": str(count), "retmax": "0", "retstart": "0", "querykey": qk, "webenv": webenv, "idlist": [],
                              "querytranslation": "GSE[ETYP] AND 2026/10/10[PDAT] : 2026/10/17[PDAT]"}}


def _gse(uid, acc, gpl, taxon="Homo sapiens", gdstype="Expression profiling by high throughput sequencing", pdat="2026/10/16", n=3, title="A study"):
    return {"uid": uid, "accession": acc, "gds": "", "title": title, "summary": "...", "gpl": gpl, "gse": acc[3:], "taxon": taxon,
            "entrytype": "GSE", "gdstype": gdstype, "ptechtype": "", "pdat": pdat, "suppfile": "MTX, TSV",
            "samples": [{"accession": f"GSM{i}", "title": f"s{i}"} for i in range(n)], "n_samples": n,
            "platformtitle": "", "platformtaxa": "", "samplestaxa": "", "pubmedids": [], "projects": [],
            "ftplink": f"ftp://ftp.ncbi.nlm.nih.gov/geo/series/{acc[:-3]}nnn/{acc}/", "geo2r": "no", "bioproject": "PRJNA1"}


def _gpl(uid, acc, title, taxon, tech="high-throughput sequencing"):
    return {"uid": uid, "accession": acc, "gds": "", "title": title, "summary": "", "gpl": acc[3:], "gse": "1;2;3", "taxon": taxon,
            "entrytype": "GPL", "gdstype": "", "ptechtype": tech, "pdat": "2018/03/02", "n_samples": 1166851, "platformtitle": ""}


def _page(records):
    return {"header": {"type": "esummary", "version": "0.3"}, "result": dict({"uids": [r["uid"] for r in records]}, **{r["uid"]: r for r in records})}


# ----------------------------------------------------------------------------- pure helpers
def test_term_and_id_helpers():
    assert geo._term("2026-10-10", "2026-10-17") == "GSE[ETYP] AND 2026/10/10:2026/10/17[PDAT]"
    assert geo._term("2026-10-10", "2026-10-17", '"high-throughput sequencing"[PTYP]').endswith('[PDAT] AND "high-throughput sequencing"[PTYP]')
    assert geo.gpl_accession("24676") == geo.gpl_accession(24676) == geo.gpl_accession(" gpl24676 ") == "GPL24676"
    assert geo.gpl_accession("") == geo.gpl_accession("GPL") == geo.gpl_accession("abc") == ""
    assert geo.gpl_accession("\u00b2") == geo.gpl_accession("GPL\u0663\u0664") == ""  # non-ASCII digits pass str.isdigit but not int()
    assert geo.gpl_uid("GPL24676") == "100024676" and geo.gpl_uid("9020") == "100009020"
    assert geo._split_gpls("21290;24676") == ["GPL21290", "GPL24676"] and geo._split_gpls("24247") == ["GPL24247"]
    assert geo._split_gpls("") == [] and geo._split_gpls("x;;24247;24247") == ["GPL24247"]
    assert geo._date("2026/10/16") == "2026-10-16" and geo._date("") == "" and geo._date("n/a") == ""


def test_model_from_title_strips_organism_only():
    assert geo.model_from_title("Illumina NovaSeq 6000 (Homo sapiens)", "Homo sapiens") == "Illumina NovaSeq 6000"
    assert geo.model_from_title("Illumina NovaSeq 6000 (Homo sapiens)") == "Illumina NovaSeq 6000"  # taxon unknown
    assert geo.model_from_title("Element AVITI (Escherichia coli; Escherichia phage MS2)", "Escherichia coli; Escherichia phage MS2") == "Element AVITI"
    assert geo.model_from_title("Xenium In Situ Analyzer: Danio rerio", "Danio rerio") == "Xenium In Situ Analyzer"
    assert geo.model_from_title("PacBio RS (mouse gut metagenome)", "mouse gut metagenome") == "PacBio RS"
    assert geo.model_from_title("Revio (Escherichia coli)", "") == "Revio"
    assert geo.model_from_title("NanoString CosMx SMI (human)", "Homo sapiens") == "NanoString CosMx SMI"  # seen live, GPL33484
    assert geo.model_from_title("NanoString CosMx SMI (Human)", "") == "NanoString CosMx SMI"  # capitalised single word
    assert geo.model_from_title("Oxford Nanopore MinION (Escherichia coli K-12)", "") == "Oxford Nanopore MinION"  # strain with digits
    assert geo.model_from_title("Illumina NovaSeq 6000 (Severe acute respiratory syndrome coronavirus 2)", "") == "Illumina NovaSeq 6000"
    assert geo.model_from_title("Some Array (V4)", "Homo sapiens") == "Some Array (V4)"  # version note -> design detail, kept
    assert geo.model_from_title("Some Array (v2.1)", "") == "Some Array (v2.1)" and geo.model_from_title("Some Array (2.0)", "") == "Some Array (2.0)"
    assert geo.model_from_title("Affymetrix Something (HG-U133A)", "Homo sapiens") == "Affymetrix Something (HG-U133A)"  # one non-alphabetic token, kept
    # array design details are not organisms and are kept
    agilent = "Agilent-021169 Arabidopsis 4 Oligo Microarray (V4) (Feature Number version)"
    assert geo.model_from_title(agilent, "Arabidopsis thaliana") == agilent
    affy = "[HuGene-2_0-st] Affymetrix Human Gene 2.0 ST Array [transcript (gene) version]"
    assert geo.model_from_title(affy, "Homo sapiens") == affy
    assert geo.model_from_title("", "Homo sapiens") == "" and geo.model_from_title("  NextSeq   2000 (Mus musculus) ", "Mus musculus") == "NextSeq 2000"


# ----------------------------------------------------------------------------- normal payload
def test_fetch_normal_payload_one_record_per_series_platform():
    page = _page([
        _gse("200351130", "GSE351130", "24247", taxon="Mus musculus", n=8, title="scRNA-seq of spinal cord"),
        _gse("200350364", "GSE350364", "21290;24676", gdstype="Expression profiling by high throughput sequencing; Other", pdat="2026/10/15"),
        {"uid": "200000001", "error": "cannot get document summary"},  # NCBI's per-uid error shape is skipped
    ])
    gpls = _page([
        _gpl("100024247", "GPL24247", "Illumina NovaSeq 6000 (Mus musculus)", "Mus musculus"),
        _gpl("100021290", "GPL21290", "Illumina HiSeq 4000 (Homo sapiens)", "Homo sapiens"),
        _gpl("100024676", "GPL24676", "Illumina NovaSeq 6000 (Homo sapiens)", "Homo sapiens"),
    ])
    http = FakeHttp([_esearch(2), page, gpls])
    cache = {}
    recs = list(geo.fetch(http, "2026-10-10", "2026-10-17", api_key="K", gpl_cache=cache))
    assert [r.accession for r in recs] == ["GSE351130/GPL24247", "GSE350364/GPL21290", "GSE350364/GPL24676"]
    r0 = recs[0]
    assert r0.archive == "geo" and r0.study_accession == "GSE351130" and r0.instrument_model == "Illumina NovaSeq 6000"
    assert r0.platform == "high-throughput sequencing" and r0.library_strategy == "Expression profiling by high throughput sequencing"
    assert r0.first_public == "2026-10-16" and r0.organism == "Mus musculus" and r0.study_title == "scRNA-seq of spinal cord"
    assert r0.center_name == "" and r0.entity_id == ""  # matching happens in the pipeline, not the adapter
    assert recs[1].instrument_model == "Illumina HiSeq 4000" and recs[2].instrument_model == "Illumina NovaSeq 6000"
    assert recs[1].first_public == recs[2].first_public == "2026-10-15"
    # three calls: esearch (history), one esummary page, one batched GPL lookup with uids 1000xxxxx
    assert len(http.calls) == 3
    es_url, es_params = http.calls[0]
    assert es_url.endswith("/esearch.fcgi") and es_params["db"] == "gds" and es_params["usehistory"] == "y" and es_params["api_key"] == "K"
    assert es_params["term"] == "GSE[ETYP] AND 2026/10/10:2026/10/17[PDAT]"
    pg_url, pg_params = http.calls[1]
    assert pg_url.endswith("/esummary.fcgi") and pg_params["WebEnv"] == "MCID_abc" and pg_params["query_key"] == "1" and pg_params["retstart"] == 0 and pg_params["retmax"] == 2
    gpl_url, gpl_params = http.calls[2]
    assert gpl_url.endswith("/esummary.fcgi") and gpl_params["id"] == "100024247,100021290,100024676"
    assert set(cache) == {"GPL24247", "GPL21290", "GPL24676"} and cache["GPL24676"]["technology"] == "high-throughput sequencing"


def test_fetch_series_dicts_and_summary_fields():
    page = _page([_gse("200351148", "GSE351148", "33484", gdstype="Other", n=1, title="CosMx tonsil")])
    http = FakeHttp([_esearch(1), page])
    out = list(geo.fetch_series(http, "2026-10-10", "2026-10-17"))
    assert out == [{
        "accession": "GSE351148", "title": "CosMx tonsil", "summary": "...", "gpls": ["GPL33484"], "taxon": "Homo sapiens",
        "gdstype": "Other", "pdat": "2026-10-16", "n_samples": 1, "bioproject": "PRJNA1", "pubmedids": [], "platform_title": "",
    }]


def test_platform_titles_batches_dedupes_and_uses_cache():
    b1 = _page([_gpl("100024676", "GPL24676", "Illumina NovaSeq 6000 (Homo sapiens)", "Homo sapiens"),
                _gpl("100016791", "GPL16791", "Illumina HiSeq 2500 (Homo sapiens)", "Homo sapiens")])
    b2 = _page([_gpl("100037502", "GPL37502", "Xenium In Situ Analyzer: Macaca fascicularis", "Macaca fascicularis", tech="other")])
    http = FakeHttp([b1, b2])
    cache = {"GPL9020": {"accession": "GPL9020", "title": "Agilent array", "taxon": "Arabidopsis thaliana", "technology": "in situ oligonucleotide", "instrument_model": "Agilent array", "released": ""}}
    titles = geo.platform_titles(http, ["GPL24676", 24676, "16791", "GPL37502", "GPL9020", "junk"], batch=2, cache=cache)
    assert titles == {"GPL24676": "Illumina NovaSeq 6000 (Homo sapiens)", "GPL16791": "Illumina HiSeq 2500 (Homo sapiens)",
                      "GPL37502": "Xenium In Situ Analyzer: Macaca fascicularis", "GPL9020": "Agilent array"}
    assert len(http.calls) == 2  # cached id not requested; 3 uncached ids in batches of 2
    assert http.calls[0][1]["id"] == "100024676,100016791" and http.calls[1][1]["id"] == "100037502"
    assert cache["GPL37502"]["instrument_model"] == "Xenium In Situ Analyzer" and cache["GPL37502"]["technology"] == "other"


# ----------------------------------------------------------------------------- empty payload
def test_empty_window_makes_one_call_and_yields_nothing():
    http = FakeHttp([_esearch(0)])
    assert list(geo.fetch(http, "2026-10-10", "2026-10-17")) == []
    assert len(http.calls) == 1
    assert geo.platform_titles(FakeHttp([]), []) == {}
    assert geo.count(FakeHttp([_esearch(0)]), "2026-10-10", "2026-10-17") == 0
    assert list(geo.fetch_series(FakeHttp([_esearch(5, webenv="")]), "2026-10-10", "2026-10-17")) == []  # no history -> nothing


# ----------------------------------------------------------------------------- malformed payloads
def test_malformed_payloads_never_raise():
    # esearch garbage / None
    assert list(geo.fetch(FakeHttp([{"unexpected": True}]), "2026-10-10", "2026-10-17")) == []
    assert list(geo.fetch(FakeHttp([None]), "2026-10-10", "2026-10-17")) == []
    assert list(geo.fetch(FakeHttp(["<html>429</html>"]), "2026-10-10", "2026-10-17")) == []
    assert geo.esearch_history(FakeHttp([{"esearchresult": {"count": "lots"}}]), "x") == (0, "", "")
    assert geo.count(FakeHttp([{"esearchresult": []}]), "2026-10-10", "2026-10-17") == 0
    # esummary page garbage: result missing / uids not a list / record not a dict
    assert list(geo.fetch(FakeHttp([_esearch(2), {"result": "nope"}]), "2026-10-10", "2026-10-17")) == []
    assert list(geo.fetch(FakeHttp([_esearch(2), {"result": {"uids": "200351130"}}]), "2026-10-10", "2026-10-17")) == []
    weird = {"result": {"uids": ["1", "2", "3"], "1": "string", "2": {"uid": "2", "title": "no accession", "gpl": "1"}, "3": {"uid": "3", "accession": "GSE3", "gpl": None, "pdat": None, "n_samples": "many"}}}
    recs = list(geo.fetch(FakeHttp([_esearch(3), weird]), "2026-10-10", "2026-10-17"))
    # only GSE3 survives; with no platform it still yields one record keyed by the series alone
    assert [r.accession for r in recs] == ["GSE3"] and recs[0].instrument_model == "" and recs[0].first_public == "2026-10-10"
    # GPL lookup garbage: records are still yielded, instrument_model stays empty
    page = _page([_gse("200351130", "GSE351130", "24247")])
    recs = list(geo.fetch(FakeHttp([_esearch(1), page, {"result": {"uids": ["100024247"], "100024247": {"uid": "100024247", "error": "cannot get document summary"}}}]), "2026-10-10", "2026-10-17"))
    assert [(r.accession, r.instrument_model, r.platform) for r in recs] == [("GSE351130/GPL24247", "", "")]
    recs = list(geo.fetch(FakeHttp([_esearch(1), page, None]), "2026-10-10", "2026-10-17"))
    assert [(r.accession, r.instrument_model) for r in recs] == [("GSE351130/GPL24247", "")]
    assert geo.platform_titles(FakeHttp([{"result": {"uids": ["100024247"], "100024247": {"uid": "100024247", "accession": "GPL24247", "title": None, "taxon": None}}}]), ["24247"]) == {"GPL24247": ""}


# ----------------------------------------------------------------------------- pagination
def test_pagination_stops_at_count_and_on_empty_page():
    p1 = _page([_gse("200000001", "GSE1", "1"), _gse("200000002", "GSE2", "1")])
    p2 = _page([_gse("200000003", "GSE3", "2")])
    gpl1 = _page([_gpl("100000001", "GPL1", "Revio (Escherichia coli)", "Escherichia coli")])
    gpl2 = _page([_gpl("100000002", "GPL2", "Sequel II (Oxytricha trifallax)", "Oxytricha trifallax")])
    extra = _page([_gse("200000004", "GSE4", "1")])  # must never be requested: count is 3
    http = FakeHttp([_esearch(3), p1, p2, extra])
    series = list(geo.fetch_series(http, "2026-10-10", "2026-10-17", batch=2))
    assert [s["accession"] for s in series] == ["GSE1", "GSE2", "GSE3"]
    assert len(http.calls) == 3 and http.calls[1][1]["retstart"] == 0 and http.calls[2][1]["retstart"] == 2 and http.calls[2][1]["retmax"] == 1
    assert http.payloads == [extra]
    # an empty page stops the sweep even though the count says more
    http = FakeHttp([_esearch(10), p1, _page([]), extra])
    assert [s["accession"] for s in geo.fetch_series(http, "2026-10-10", "2026-10-17", batch=2)] == ["GSE1", "GSE2"]
    assert len(http.calls) == 3 and http.payloads == [extra]
    assert geo.sweep_error() == "geo: partial sweep 2/10 series for 2026-10-10..2026-10-17"
    assert geo.last_sweep == {"start": "2026-10-10", "end": "2026-10-17", "total": 10, "fetched": 2, "complete": False, "error": geo.sweep_error()}
    # max_records caps the sweep
    http = FakeHttp([_esearch(3), p1, p2])
    assert [s["accession"] for s in geo.fetch_series(http, "2026-10-10", "2026-10-17", batch=2, max_records=2)] == ["GSE1", "GSE2"]
    assert len(http.calls) == 2
    # fetch() resolves platforms per flushed page and reuses the cache across pages
    geo_page = geo.SERIES_PAGE
    try:
        geo.SERIES_PAGE = 2
        http = FakeHttp([_esearch(3), p1, gpl1, p2, gpl2])
        recs = list(geo.fetch(http, "2026-10-10", "2026-10-17"))
    finally:
        geo.SERIES_PAGE = geo_page
    assert [(r.accession, r.instrument_model) for r in recs] == [("GSE1/GPL1", "Revio"), ("GSE2/GPL1", "Revio"), ("GSE3/GPL2", "Sequel II")]
    assert [c[1].get("id") for c in http.calls if c[1].get("id")] == ["100000001", "100000002"]


# ----------------------------------------------------------------------------- count helpers
def test_search_platforms_and_series_count_by_platform():
    http = FakeHttp([{"esearchresult": {"count": "3", "idlist": ["100037398", "100036612", "bad", "5"]}}])
    assert geo.search_platforms(http, ["Revio", "Sequel II", ""]) == ["GPL37398", "GPL36612"]
    assert http.calls[0][1]["term"] == 'GPL[ETYP] AND (Revio[TITL] OR "Sequel II"[TITL])'
    assert geo.search_platforms(FakeHttp([]), []) == [] and geo.search_platforms(FakeHttp([None]), ["x"]) == []


def _ids(count, idlist):
    return {"esearchresult": {"count": str(count), "retmax": "10000", "retstart": "0", "idlist": [str(i) for i in idlist]}}


def test_series_count_by_platform_dedupes_across_chunks_and_pages():
    # chunk 1 (GPL1, GPL2) -> series 200000001..3; chunk 2 (GPL3) -> 200000003 again + 200000004: counted once
    http = FakeHttp([_ids(3, [200000001, 200000002, 200000003]), _ids(2, [200000003, 200000004])])
    assert geo.series_count_by_platform(http, ["GPL1", "1", "GPL2", "GPL3"], "2026-01-01", "2026-10-17", chunk=2) == 4
    assert http.calls[0][1]["term"] == "GSE[ETYP] AND 2026/01/01:2026/10/17[PDAT] AND (GPL1[ACCN] OR GPL2[ACCN])"
    assert http.calls[0][1]["retmax"] == geo.ESEARCH_PAGE and http.calls[0][1]["retstart"] == 0
    assert http.calls[1][1]["term"].endswith("AND (GPL3[ACCN])")
    assert geo.series_uids_by_platform(FakeHttp([_ids(1, [5])]), ["GPL9"], "2026-01-01", "2026-10-17", extra_term='"high-throughput sequencing"[PTYP]') == {"5"}
    # a chunk with more hits than one esearch page is paged with retstart (count 5, page 2)
    http = FakeHttp([_ids(5, [1, 2]), _ids(5, [3, 4]), _ids(5, [5])])
    assert geo.esearch_uids(http, "x", page=2) == (5, {"1", "2", "3", "4", "5"})
    assert [c[1]["retstart"] for c in http.calls] == [0, 2, 4]
    # paging stops on a page that adds nothing new, on an empty page, and on a malformed page
    http = FakeHttp([_ids(5, [1, 2]), _ids(5, [1, 2]), _ids(5, [3])])
    assert geo.esearch_uids(http, "x", page=2) == (5, {"1", "2"}) and len(http.calls) == 2
    assert geo.esearch_uids(FakeHttp([_ids(5, [1, 2]), _ids(5, [])]), "x", page=2) == (5, {"1", "2"})
    assert geo.esearch_uids(FakeHttp([_ids(5, [1, 2]), None]), "x", page=2) == (5, {"1", "2"})
    assert geo.esearch_uids(FakeHttp([{"esearchresult": {"count": "2", "idlist": "oops"}}]), "x") == (2, set())
    assert geo.esearch_uids(FakeHttp([None]), "x") == (0, set())
    # no accessions -> no calls; windows helper labels each period
    assert geo.series_count_by_platform(FakeHttp([]), ["junk", ""], "2026-01-01", "2026-10-17") == 0
    http = FakeHttp([_ids(2, [1, 2]), _ids(1, [3])])
    out = geo.series_counts_by_window(http, ["GPL1"], [("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-12-31")])
    assert out == [{"start": "2024-01-01", "end": "2024-12-31", "series": 2}, {"start": "2025-01-01", "end": "2025-12-31", "series": 1}]
    assert "2024/01/01:2024/12/31[PDAT]" in http.calls[0][1]["term"] and "2025/01/01:2025/12/31[PDAT]" in http.calls[1][1]["term"]


# ----------------------------------------------------------------------------- GPL uid fallback
def test_platform_info_recovers_uids_by_accession_when_convention_fails():
    all_error = {"result": {"uids": ["100024676", "100033484"],
                            "100024676": {"uid": "100024676", "error": "cannot get document summary"},
                            "100033484": {"uid": "100033484", "error": "cannot get document summary"}}}
    accn = {"esearchresult": {"count": "2", "idlist": ["987", "988"]}}
    good = _page([_gpl("987", "GPL24676", "Illumina NovaSeq 6000 (Homo sapiens)", "Homo sapiens"),
                  _gpl("988", "GPL33484", "NanoString CosMx SMI (human)", "Homo sapiens", tech="other")])
    http = FakeHttp([all_error, accn, good])
    info = geo.platform_info(http, ["GPL24676", "GPL33484"])
    assert info["GPL24676"]["instrument_model"] == "Illumina NovaSeq 6000" and info["GPL33484"]["technology"] == "other"
    assert len(http.calls) == 3
    assert http.calls[0][1]["id"] == "100024676,100033484"
    assert http.calls[1][0].endswith("/esearch.fcgi") and http.calls[1][1]["term"] == "(GPL24676[ACCN] OR GPL33484[ACCN]) AND GPL[ETYP]" and http.calls[1][1]["retmax"] == 2
    assert http.calls[2][1]["id"] == "987,988"
    # fallback also empty / malformed: no exception, nothing resolved, exactly one retry
    assert geo.platform_info(FakeHttp([None, None]), ["GPL1"]) == {}
    assert geo.platform_info(FakeHttp([all_error, {"esearchresult": {"idlist": []}}]), ["GPL24676"]) == {}
    http = FakeHttp([all_error, accn, None])
    assert geo.platform_info(http, ["GPL24676"]) == {} and len(http.calls) == 3
    # a chunk with one good record and one error does not trigger the fallback
    mixed = _page([_gpl("100024676", "GPL24676", "Illumina NovaSeq 6000 (Homo sapiens)", "Homo sapiens")])
    mixed["result"]["uids"].append("100000001")
    mixed["result"]["100000001"] = {"uid": "100000001", "error": "cannot get document summary"}
    http = FakeHttp([mixed])
    assert list(geo.platform_info(http, ["GPL24676", "GPL1"])) == ["GPL24676"] and len(http.calls) == 1


# ----------------------------------------------------------------------------- sweep coverage
def test_partial_sweep_is_reported_and_complete_sweep_is_clean():
    p1 = _page([_gse("200000001", "GSE1", "1"), _gse("200000002", "GSE2", "1")])
    gpl1 = _page([_gpl("100000001", "GPL1", "Revio (Escherichia coli)", "Escherichia coli")])
    # second page never comes back (retries exhausted -> None): records from page 1 are kept, the gap is reported
    http = FakeHttp([_esearch(4), p1, None, gpl1])
    recs = list(geo.fetch(http, "2026-10-10", "2026-10-17", max_records=4))
    assert [r.accession for r in recs] == ["GSE1/GPL1", "GSE2/GPL1"]
    assert geo.sweep_error() == "geo: partial sweep 2/4 series for 2026-10-10..2026-10-17"
    # esearch itself failed vs. a genuinely empty window
    assert list(geo.fetch(FakeHttp([None]), "2026-10-10", "2026-10-17")) == []
    assert geo.sweep_error() == "geo: esearch failed for 2026-10-10..2026-10-17"
    assert list(geo.fetch(FakeHttp([_esearch(0)]), "2026-10-10", "2026-10-17")) == []
    assert geo.sweep_error() == "" and geo.last_sweep["complete"] is True and geo.last_sweep["total"] == 0
    assert list(geo.fetch(FakeHttp([_esearch(7, webenv="")]), "2026-10-10", "2026-10-17")) == []
    assert geo.sweep_error() == "geo: esearch found 7 series but returned no history for 2026-10-10..2026-10-17"
    # complete sweep (count 2, one page) -> no error; the max_records cap is deliberate and not an error
    http = FakeHttp([_esearch(2), p1, gpl1])
    assert len(list(geo.fetch(http, "2026-10-10", "2026-10-17"))) == 2
    assert geo.sweep_error() == "" and geo.last_sweep == {"start": "2026-10-10", "end": "2026-10-17", "total": 2, "fetched": 2, "complete": True, "error": ""}
    http = FakeHttp([_esearch(5), p1, gpl1])
    assert len(list(geo.fetch(http, "2026-10-10", "2026-10-17", max_records=2))) == 2 and geo.sweep_error() == ""


# ----------------------------------------------------------------------------- summarize_window
def test_summarize_window_counts_titles_models_and_technologies():
    page = _page([
        _gse("200000001", "GSE1", "24676"),
        _gse("200000002", "GSE2", "24676;33484", taxon="Homo sapiens"),
        _gse("200000003", "GSE3", "99"),  # platform the server does not know -> unresolved
    ])
    gpls = _page([_gpl("100024676", "GPL24676", "Illumina NovaSeq 6000 (Homo sapiens)", "Homo sapiens"),
                  _gpl("100033484", "GPL33484", "NanoString CosMx SMI (human)", "Homo sapiens", tech="other")])
    http = FakeHttp([_esearch(3), page, gpls, _esearch(3)])
    out = geo.summarize_window(http, "2026-10-10", "2026-10-17", top=2)
    assert out["records"] == 4 and out["series"] == 3 and out["platforms_resolved"] == 2
    assert out["top_platform_titles"] == [("Illumina NovaSeq 6000 (Homo sapiens)", 2), ("NanoString CosMx SMI (human)", 1)]
    assert out["top_instrument_models"] == [("Illumina NovaSeq 6000", 2), ("NanoString CosMx SMI", 1)]
    assert out["technology_types"] == [("high-throughput sequencing", 2), ("other", 1), ("(unresolved)", 1)]
    assert len(http.calls) == 4 and http.calls[3][1]["retmax"] == 0
