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
    assert geo.model_from_title("Some Array (V4)", "Homo sapiens") == "Some Array (V4)"  # digits -> design detail, kept
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
    http = FakeHttp([_esearch(4), _esearch(2)])
    assert geo.series_count_by_platform(http, ["GPL1", "1", "GPL2", "GPL3"], "2026-01-01", "2026-10-17", chunk=2) == 6
    assert http.calls[0][1]["term"] == "GSE[ETYP] AND 2026/01/01:2026/10/17[PDAT] AND (GPL1[ACCN] OR GPL2[ACCN])"
    assert http.calls[1][1]["term"].endswith("AND (GPL3[ACCN])")
