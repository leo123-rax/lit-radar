"""Instrument usage time series, tested offline against canned payloads (no network).

Covers: normal payloads, empty payloads (zero hits / phrase not found), malformed payloads, and the
incremental stop (cached months are not requested again; recent months inside the refresh window are).
The count endpoints have no pagination, so "pagination stop" here means the backfill stops asking for
months it already has.
"""
from datetime import date
from pathlib import Path

import pytest

from litradar import timeseries as ts
from litradar.store import Store

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"

# Controlled vocabulary of SRA.common.xsd (github.com/enasequence/schema), fetched 2026-10-17. SRA and ENA share it.
ARCHIVE_VOCABULARY = {
    "HiSeq X Five", "HiSeq X Ten", "Illumina Genome Analyzer", "Illumina Genome Analyzer II", "Illumina Genome Analyzer IIx",
    "Illumina HiScanSQ", "Illumina HiSeq 1000", "Illumina HiSeq 1500", "Illumina HiSeq 2000", "Illumina HiSeq 2500",
    "Illumina HiSeq 3000", "Illumina HiSeq 4000", "Illumina HiSeq X", "Illumina iSeq 100", "Illumina MiSeq", "Illumina MiniSeq",
    "Illumina NovaSeq X", "Illumina NovaSeq X Plus", "Illumina NovaSeq 6000", "NextSeq 500", "NextSeq 550", "NextSeq 1000", "NextSeq 2000",
    "BGISEQ-50", "BGISEQ-500", "MGISEQ-2000RS",
    "Onso", "PacBio RS", "PacBio RS II", "Revio", "Sequel", "Sequel II", "Sequel IIe", "Vega",
    "Ion Torrent PGM", "Ion Torrent Proton", "Ion Torrent S5", "Ion Torrent S5 XL", "Ion Torrent Genexus", "Ion GeneStudio S5",
    "Ion GeneStudio S5 Prime", "Ion GeneStudio S5 Plus",
    "DNBSEQ-T7", "DNBSEQ-G400", "DNBSEQ-G800", "DNBSEQ-G50", "DNBSEQ-G400 FAST", "DNBSEQ-T10x4RS",
    "MinION", "GridION", "PromethION", "Element AVITI", "AVITI 24", "UG 100", "UG 200",
}


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


SRA_TRANSLATION = '2025/09/01[PDAT] : 2025/09/30[PDAT] AND "instrument revio"[Properties]'


def _sra(count, **extra):
    """esearch JSON as returned live on 2026-10-17 (retmax=0, so idlist is empty)."""
    result = {"count": str(count), "retmax": "0", "retstart": "0", "idlist": [], "translationset": [], "querytranslation": SRA_TRANSLATION}
    result.update(extra)
    return {"header": {"type": "esearch", "version": "0.3"}, "esearchresult": result}


def _sra_not_found(model="vega"):
    """A valid vocabulary string with zero deposits: count 0, the [Properties] clause kept, phrase flagged."""
    return _sra(0, querytranslation=SRA_TRANSLATION.replace("revio", model),
                warninglist={"phrasesignored": [], "quotedphrasesnotfound": [f'"instrument {model}"[Properties]'], "outputmessages": ["No items found."]})


class ExplodingHttp(FakeHttp):
    """Serves payloads, then raises on call number `blow_at` (simulates Ctrl-C / crash mid-backfill)."""

    def __init__(self, payloads, blow_at):
        super().__init__(payloads)
        self.blow_at = blow_at

    def get_json(self, url, params=None, cache=False):
        if len(self.calls) + 1 == self.blow_at:
            raise KeyboardInterrupt
        return super().get_json(url, params, cache)


def _specs():
    return {
        "pacb-revio": ts.ModelSpec("pacb-revio", vendor="Pacific Biosciences", sra=["revio"], ena=["Revio"], epmc_terms=["Revio"]),
        "pacb-sequel-ii": ts.ModelSpec("pacb-sequel-ii", vendor="Pacific Biosciences", sra=["sequel ii", "sequel iie"], ena=["Sequel II", "Sequel IIe"], epmc_terms=["Sequel II", "Sequel IIe"]),
        "ont-minion": ts.ModelSpec("ont-minion", vendor="Oxford Nanopore Technologies", sra=["minion"], ena=["MinION"], epmc_terms=["MinION", "Flongle"]),
    }


# ---- months -----------------------------------------------------------------------------------
def test_month_helpers():
    assert ts.month_range("2023-11", "2024-02") == ["2023-11", "2023-12", "2024-01", "2024-02"]
    assert ts.month_range("2026-10", "2026-10") == ["2026-10"]
    assert ts.month_bounds("2024-02") == ("2024-02-01", "2024-02-29")  # leap year
    assert ts.month_bounds("2023-02") == ("2023-02-01", "2023-02-28")
    assert ts.shift_month("2026-01", -2) == "2025-11" and ts.shift_month("2025-12", 1) == "2026-01"
    assert ts.refresh_cutoff(date(2026, 10, 17), 2) == "2026-08"
    assert ts.default_months(date(2026, 10, 17), span=36) == ("2023-11", "2026-10")
    with pytest.raises(ValueError):
        ts.month_range("2024-03", "2024-01")
    with pytest.raises(ValueError):
        ts.parse_month("2024/01")
    with pytest.raises(ValueError):
        ts.parse_month("2024-13")


# ---- query builders ---------------------------------------------------------------------------
def test_query_builders_match_verified_live_syntax():
    assert ts.sra_term("Revio", "2025-09-01", "2025-09-30") == '2025/09/01:2025/09/30[PDAT] AND "instrument revio"[Properties]'
    assert ts.ena_query("Revio", "2024-01-01", "2024-01-31") == 'first_public>=2024-01-01 AND first_public<=2024-01-31 AND instrument_model="Revio"'
    assert ts.epmc_query(["Revio", "Sequel II"], "2024-01-01", "2024-01-31") == '(METHODS:"Revio" OR METHODS:"Sequel II") AND (FIRST_PDATE:[2024-01-01 TO 2024-01-31])'
    assert ts.epmc_label(["Sequel II", "Sequel IIe"]) == "Sequel II|Sequel IIe"


# ---- count parsing: normal / empty / malformed -------------------------------------------------
def test_sra_count_payloads():
    # the misspelt-field payload is what NCBI returned live for "instrument revio"[Propertiez]: HTTP 200, a count,
    # an errorlist (with empty lists) and the clause rewritten to [All Fields]; it must never be taken as a count
    bad_field = _sra(851, querytranslation=SRA_TRANSLATION.replace("[Properties]", "[All Fields]"), errorlist={"phrasesnotfound": [], "fieldsnotfound": []})
    http = FakeHttp([
        _sra(851),
        _sra_not_found("vega"),
        bad_field,
        _sra(851, querytranslation=SRA_TRANSLATION.replace("[Properties]", "[All Fields]")),
        {"header": {}, "esearchresult": {"count": "851", "retmax": "0", "idlist": []}},
        {"error": "API rate limit exceeded", "api-key": "x"},
        _sra("lots"),
        [],
        None,
    ])
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") == 851
    assert ts.sra_count(http, "Vega", "2025-09-01", "2025-09-30") == 0  # phrase not found == zero deposits
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") is None  # errorlist present
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") is None  # Properties clause lost
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") is None  # no querytranslation at all
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") is None  # error envelope
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") is None  # non-numeric count
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") is None  # wrong JSON shape
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") is None  # HTTP failure
    url, params = http.calls[0]
    assert url == ts.EUTILS_ESEARCH and params["db"] == "sra" and params["retmax"] == 0 and params["retmode"] == "json"
    assert params["term"] == '2025/09/01:2025/09/30[PDAT] AND "instrument revio"[Properties]'
    assert "api_key" not in params
    http2 = FakeHttp([_sra(1)])
    ts.sra_count(http2, "Revio", "2025-09-01", "2025-09-30", api_key="K")
    assert http2.calls[0][1]["api_key"] == "K"


def test_ena_count_payloads():
    http = FakeHttp([{"count": "38"}, {"count": "0"}, {"count": "n/a"}, "count\n38", None])
    assert ts.ena_count(http, "Revio", "2024-01-01", "2024-01-31") == 38
    assert ts.ena_count(http, "Vega", "2024-01-01", "2024-01-31") == 0
    assert ts.ena_count(http, "Revio", "2024-01-01", "2024-01-31") is None  # malformed value
    assert ts.ena_count(http, "Revio", "2024-01-01", "2024-01-31") is None  # text/plain body, not JSON
    assert ts.ena_count(http, "Revio", "2024-01-01", "2024-01-31") is None  # HTTP failure
    url, params = http.calls[0]
    assert url == ts.ENA_COUNT and params == {"result": "read_experiment", "query": 'first_public>=2024-01-01 AND first_public<=2024-01-31 AND instrument_model="Revio"', "format": "json"}


def test_epmc_count_payloads():
    http = FakeHttp([
        {"version": "6.9", "hitCount": 117, "nextCursorMark": "AoII", "resultList": {"result": [{"id": "38168995", "source": "MED"}]}},
        {"version": "6.9", "hitCount": 0, "resultList": {"result": []}},
        {"version": "6.9", "resultList": {"result": []}},
        None,
    ])
    assert ts.epmc_count(http, ["Revio", "Sequel II"], "2024-01-01", "2024-01-31") == 117
    assert ts.epmc_count(http, ["NoSuchInstrument"], "2024-01-01", "2024-01-31") == 0
    assert ts.epmc_count(http, ["Revio"], "2024-01-01", "2024-01-31") is None  # no hitCount key
    assert ts.epmc_count(http, ["Revio"], "2024-01-01", "2024-01-31") is None  # HTTP failure
    assert ts.epmc_count(http, [], "2024-01-01", "2024-01-31") is None and len(http.calls) == 4  # no terms -> no call
    url, params = http.calls[0]
    assert url == ts.EPMC_SEARCH and params["resultType"] == "idlist" and params["pageSize"] == 1
    assert params["query"] == '(METHODS:"Revio" OR METHODS:"Sequel II") AND (FIRST_PDATE:[2024-01-01 TO 2024-01-31])'


# ---- planning, caching and the incremental stop -----------------------------------------------
def test_plan_jobs_one_request_per_entity_source_month():
    # an entity with two archive strings is ONE series (pipe-joined key) and ONE request per month per source,
    # the same convention Europe PMC term sets always had
    jobs = ts.plan_jobs(_specs(), ["pacb-sequel-ii"], ["2024-01", "2024-02"], ["sra", "ena", "epmc"])
    assert [(j.source, j.model, j.month) for j in jobs] == [
        ("sra", "sequel ii|sequel iie", "2024-01"), ("sra", "sequel ii|sequel iie", "2024-02"),
        ("ena", "Sequel II|Sequel IIe", "2024-01"), ("ena", "Sequel II|Sequel IIe", "2024-02"),
        ("epmc", "Sequel II|Sequel IIe", "2024-01"), ("epmc", "Sequel II|Sequel IIe", "2024-02"),
    ]
    assert jobs[0].terms == ["sequel ii", "sequel iie"] and jobs[2].terms == ["Sequel II", "Sequel IIe"] and jobs[-1].terms == ["Sequel II", "Sequel IIe"]
    # a single string is its own key, so a cache written per string by the earlier version is still valid for it
    single = ts.plan_jobs(_specs(), ["pacb-revio"], ["2024-01"], ["sra", "ena"])
    assert [(j.model, j.terms) for j in single] == [("revio", ["revio"]), ("Revio", ["Revio"])]
    assert ts.plan_jobs(_specs(), ["unknown-id"], ["2024-01"], ["sra"]) == []
    # the OR-ed request text, exactly as verified live on 2026-10-18
    assert ts.sra_term(["sequel ii", "sequel iie"], "2025-06-01", "2025-06-30") == (
        '2025/06/01:2025/06/30[PDAT] AND ("instrument sequel ii"[Properties] OR "instrument sequel iie"[Properties])'
    )
    assert ts.sra_term("revio", "2025-09-01", "2025-09-30") == '2025/09/01:2025/09/30[PDAT] AND "instrument revio"[Properties]'
    assert ts.ena_query(["Sequel II", "Sequel IIe"], "2025-06-01", "2025-06-30") == (
        'first_public>=2025-06-01 AND first_public<=2025-06-30 AND (instrument_model="Sequel II" OR instrument_model="Sequel IIe")'
    )
    assert ts.ena_query("Revio", "2024-01-01", "2024-01-31") == 'first_public>=2024-01-01 AND first_public<=2024-01-31 AND instrument_model="Revio"'
    # no strings at all is a named failure, never a request
    assert ts.sra_count_detail(FakeHttp([]), [], "2024-01-01", "2024-01-31") == (None, "no SRA instrument strings configured")
    assert ts.ena_count_detail(FakeHttp([]), [], "2024-01-01", "2024-01-31") == (None, "no ENA instrument_model values configured")


def test_consolidate_cache_folds_per_string_rows_into_entity_rows(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    specs = _specs()
    # the earlier layout: one row per vocabulary string; 2024-03 is partial (only one of the two strings cached)
    for month, a, b in (("2024-01", 100, 7), ("2024-02", 120, 9)):
        store.put_timeseries_count("sra", "pacb-sequel-ii", "sequel ii", month, a)
        store.put_timeseries_count("sra", "pacb-sequel-ii", "sequel iie", month, b)
    store.put_timeseries_count("sra", "pacb-sequel-ii", "sequel ii", "2024-03", 130)
    store.put_timeseries_count("ena", "pacb-sequel-ii", "Sequel II", "2024-01", 101)
    store.put_timeseries_count("ena", "pacb-sequel-ii", "Sequel IIe", "2024-01", 8)
    store.put_timeseries_count("sra", "pacb-revio", "revio", "2024-01", 38)              # single string: untouched
    store.put_timeseries_count("sra", "pacb-sequel-ii", "sequel (old spelling)", "2024-01", 5)  # unconfigured: untouched
    store.commit()
    # dry run: the plan sees the rows that would be derived, the table is not written
    before = store.timeseries_rows()
    rep = ts.consolidate_cache(store, specs, ["pacb-sequel-ii", "pacb-revio"], apply=False)
    assert rep["derived"] == 3 and rep["pruned"] == 7 and rep["partial"] == 1
    assert rep["virtual"] == {("sra", "pacb-sequel-ii", "sequel ii|sequel iie", "2024-01"): 107,
                              ("sra", "pacb-sequel-ii", "sequel ii|sequel iie", "2024-02"): 129,
                              ("ena", "pacb-sequel-ii", "Sequel II|Sequel IIe", "2024-01"): 109}
    assert store.timeseries_rows() == before
    jobs = ts.plan_jobs(specs, ["pacb-sequel-ii"], ["2024-01", "2024-02", "2024-03"], ["sra", "ena"])
    plan = ts.backfill(FakeHttp([]), store, jobs, today=date(2026, 10, 17), dry_run=True, virtual_cache=rep["virtual"])
    assert plan["cached"] == 3 and plan["requests"] == 3  # sra 2024-03 (partial) + ena 2024-02, 2024-03
    # real: derived rows written, per-string rows of the configured strings gone, everything else kept
    rep = ts.consolidate_cache(store, specs, ["pacb-sequel-ii", "pacb-revio"], apply=True)
    assert rep["derived"] == 3 and rep["pruned"] == 7 and rep["partial"] == 1
    assert [e["entity_id"] for e in rep["entities"]] == ["pacb-sequel-ii", "pacb-sequel-ii"]
    rows = {(r["source"], r["model"], r["month"]): r["count"] for r in store.timeseries_rows()}
    assert rows == {
        ("sra", "sequel ii|sequel iie", "2024-01"): 107, ("sra", "sequel ii|sequel iie", "2024-02"): 129,
        ("ena", "Sequel II|Sequel IIe", "2024-01"): 109,
        ("sra", "revio", "2024-01"): 38, ("sra", "sequel (old spelling)", "2024-01"): 5,
    }
    # idempotent: a second pass finds nothing to fold
    assert ts.consolidate_cache(store, specs, ["pacb-sequel-ii", "pacb-revio"]) == {"derived": 0, "pruned": 0, "partial": 0, "entities": [], "virtual": {}}
    # an entity row that already exists is kept rather than re-derived
    store.put_timeseries_count("sra", "pacb-sequel-ii", "sequel ii", "2024-05", 1)
    store.put_timeseries_count("sra", "pacb-sequel-ii", "sequel iie", "2024-05", 1)
    store.put_timeseries_count("sra", "pacb-sequel-ii", "sequel ii|sequel iie", "2024-05", 999)
    store.commit()
    rep = ts.consolidate_cache(store, specs, ["pacb-sequel-ii"])
    assert rep["derived"] == 0 and rep["pruned"] == 2
    assert store.get_timeseries_count("sra", "pacb-sequel-ii", "sequel ii|sequel iie", "2024-05")["count"] == 999


def test_all_entities_and_warm_cache_request_budget(tmp_path):
    specs = ts.load_archive_models(CONFIG / "archive_models.yaml")
    instruments = ts.load_instrument_index(CONFIG / "instruments.yaml")
    ids, warnings = ts.resolve_entities([], [], instruments, specs, all_entities=True)
    assert ids == list(specs) and len(ids) == 27 and warnings == []
    ids2, warnings2 = ts.resolve_entities(["pacb-revio"], ["ont"], instruments, specs, all_entities=True)
    assert ids2 == ids and warnings2 == ["--all selects every entity in archive_models.yaml; --entity / --vendor were ignored"]
    # series arithmetic for the real configuration: one per entity per source that has strings
    months = ts.month_range("2023-01", "2026-10")
    jobs = ts.plan_jobs(specs, ids, months, ts.SOURCES)
    series = {(j.source, j.entity_id, j.model) for j in jobs}
    per_source = {s: sum(1 for k in series if k[0] == s) for s in ts.SOURCES}
    assert per_source == {"sra": 24, "ena": 24, "epmc": 26} and len(series) == 74
    # a warm cache with --refresh-months 2 asks only for the current month and the two before it:
    # 74 series x 3 months = 222 requests, inside the (entities x sources x 3) = 243 budget
    store = Store(tmp_path / "warm.sqlite")
    for j in jobs:
        store.put_timeseries_count(j.source, j.entity_id, j.model, j.month, 1)
    store.commit()
    today = date(2026, 10, 18)
    plan = ts.backfill(FakeHttp([]), store, jobs, refresh_months=2, today=today, dry_run=True)
    assert plan["cutoff"] == "2026-08" and plan["requests"] == 74 * 3 == 222 and plan["cached"] == len(jobs) - 222
    assert plan["requests"] <= len(ids) * len(ts.SOURCES) * 3 == 243
    # the same through run(): --all, dry run, no network, and the output stem is "_all"
    rep = ts.run(CONFIG, tmp_path / "warm.sqlite", tmp_path / "out", all_entities=True, start="2023-01", end="2026-10",
                 refresh_months=2, dry_run=True, http=FakeHttp([]), store=store, today=today)
    assert rep["errors"] == [] and rep["entities"] == ids and rep["series"] == 74 and rep["dry_run"] is True
    assert rep["backfill"]["requests"] == 222 and rep["backfill"]["cached"] == 3182 and rep["consolidated"]["derived"] == 0
    assert ts.output_stem("2023-01", "2026-10", (), (), ids) == "timeseries_2023-01_2026-10_all"


def test_backfill_caches_and_stops_asking_for_old_months(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    today = date(2026, 10, 17)  # refresh window with refresh_months=2 -> 2026-08 and later
    months = ["2026-06", "2026-07", "2026-08", "2026-09"]
    jobs = ts.plan_jobs(_specs(), ["pacb-revio"], months, ["sra"])
    http = FakeHttp([_sra(10), _sra(11), _sra(12), _sra(13)])
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=today)
    assert rep["requests"] == 4 and rep["cached"] == 0 and rep["failed"] == [] and len(http.calls) == 4
    assert [r["count"] for r in store.timeseries_rows(sources=["sra"], entity_ids=["pacb-revio"])] == [10, 11, 12, 13]
    # second run: June and July are cached and NOT requested; August and September are inside the refresh window
    http = FakeHttp([_sra(120), _sra(130)])
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=today)
    assert rep["requests"] == 2 and rep["cached"] == 2 and len(http.calls) == 2
    assert [p["term"][:10] for _, p in http.calls] == ["2026/08/01", "2026/09/01"]
    assert [r["count"] for r in store.timeseries_rows(sources=["sra"], entity_ids=["pacb-revio"])] == [10, 11, 120, 130]
    # a failed call is reported and never overwrites the cached value
    http = FakeHttp([None, _sra(131)])
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=today)
    assert rep["failed"] == ["sra:pacb-revio:revio:2026-08"] and rep["by_source"]["sra"]["failed"] == 1
    assert store.get_timeseries_count("sra", "pacb-revio", "revio", "2026-08")["count"] == 120
    assert store.get_timeseries_count("sra", "pacb-revio", "revio", "2026-09")["count"] == 131
    # a fresh 0 (phrase not found) never overwrites a positive cached count: reported under `kept`
    http = FakeHttp([_sra_not_found("revio"), _sra(132)])
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=today)
    assert rep["kept"] == ["sra:pacb-revio:revio:2026-08"] and rep["failed"] == [] and rep["by_source"]["sra"]["kept"] == 1
    assert store.get_timeseries_count("sra", "pacb-revio", "revio", "2026-08")["count"] == 120
    assert store.get_timeseries_count("sra", "pacb-revio", "revio", "2026-09")["count"] == 132
    # ... but a 0 on top of a cached 0 or a missing month is stored, and force overwrites anything
    store.put_timeseries_count("sra", "pacb-revio", "revio", "2026-09", 0)
    http = FakeHttp([_sra_not_found("revio"), _sra_not_found("revio")])
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=today)
    assert rep["kept"] == ["sra:pacb-revio:revio:2026-08"] and store.get_timeseries_count("sra", "pacb-revio", "revio", "2026-09")["count"] == 0
    http = FakeHttp([_sra_not_found("revio")] * 4)
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=today, force=True)
    assert rep["requests"] == 4 and rep["kept"] == [] and len(http.calls) == 4
    assert [r["count"] for r in store.timeseries_rows(sources=["sra"], entity_ids=["pacb-revio"])] == [0, 0, 0, 0]
    # dry_run plans without touching the network
    http = FakeHttp([])
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=today, dry_run=True)
    assert rep["requests"] == 2 and rep["cached"] == 2 and http.calls == []


def test_backfill_commits_each_count_so_an_interrupted_run_keeps_progress(tmp_path):
    db = tmp_path / "t.sqlite"
    store = Store(db)
    jobs = ts.plan_jobs(_specs(), ["pacb-revio"], ["2024-01", "2024-02", "2024-03", "2024-04"], ["sra"])
    http = ExplodingHttp([_sra(10), _sra(11)], blow_at=3)
    with pytest.raises(KeyboardInterrupt):
        ts.backfill(http, store, jobs, today=date(2026, 10, 17))
    # a SECOND connection sees the first two counts: they were committed, not left in an open transaction
    other = Store(db)
    assert [(r["month"], r["count"]) for r in other.timeseries_rows(sources=["sra"], entity_ids=["pacb-revio"])] == [("2024-01", 10), ("2024-02", 11)]
    other.close()
    # the next run only asks for the two missing months
    http = FakeHttp([_sra(12), _sra(13)])
    rep = ts.backfill(http, store, jobs, today=date(2026, 10, 17))
    assert rep["requests"] == 2 and rep["cached"] == 2 and [p["term"][:7] for _, p in http.calls] == ["2024/03", "2024/04"]


def test_backfill_routes_each_source(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    jobs = ts.plan_jobs(_specs(), ["pacb-revio"], ["2024-01"], ["sra", "ena", "epmc"])
    http = FakeHttp([_sra(38), {"count": "38"}, {"hitCount": 7}])
    rep = ts.backfill(http, store, jobs, today=date(2026, 10, 17))
    assert rep["requests"] == 3 and rep["failed"] == []
    assert [c[0] for c in http.calls] == [ts.EUTILS_ESEARCH, ts.ENA_COUNT, ts.EPMC_SEARCH]
    rows = store.timeseries_rows(entity_ids=["pacb-revio"], start_month="2024-01", end_month="2024-01")
    assert [(r["source"], r["model"], r["count"]) for r in rows] == [("ena", "Revio", 38), ("epmc", "Revio", 7), ("sra", "revio", 38)]


# ---- configuration ----------------------------------------------------------------------------
def test_real_config_is_consistent_with_gazetteer_and_vocabulary():
    specs = ts.load_archive_models(CONFIG / "archive_models.yaml")
    instruments = ts.load_instrument_index(CONFIG / "instruments.yaml")
    required = {
        "pacb-rs-ii", "pacb-sequel", "pacb-sequel-ii", "pacb-revio", "pacb-vega", "pacb-onso",
        "ont-minion", "ont-gridion", "ont-promethion",
        "ilmn-hiseq", "ilmn-novaseq-6000", "ilmn-novaseq-x", "ilmn-nextseq-550", "ilmn-nextseq-1000", "ilmn-nextseq-2000", "ilmn-miseq", "ilmn-iseq-100",
        "elem-aviti", "ultima-ug100", "mgi-dnbseq-t7", "mgi-dnbseq-g400", "mgi-dnbseq", "thermo-ion-torrent",
    }
    assert required <= set(specs)
    assert "pacb-rs-ii" in instruments  # landed in the gazetteer; its `proposed` flag was dropped
    for eid, spec in specs.items():
        assert spec.proposed or eid in instruments, f"{eid} is not a gazetteer id"
        assert all(s == s.lower() for s in spec.sra), eid
        assert set(spec.ena) <= ARCHIVE_VOCABULARY, f"{eid}: unknown ENA instrument_model {set(spec.ena) - ARCHIVE_VOCABULARY}"
        assert sorted(spec.sra) == sorted(v.lower() for v in spec.ena), f"{eid}: sra and ena lists must be the same models"
        assert spec.sra or spec.ena or spec.epmc_terms, eid
    # every archive model string is claimed by exactly one entity
    claimed = [m for s in specs.values() for m in s.ena]
    assert len(claimed) == len(set(claimed)), "an ENA model string is listed under two entities"
    # every pre-registered entity has landed in the gazetteer (pacb-sequel on 2026-10-17), so nothing is `proposed`
    assert {eid for eid, s in specs.items() if s.proposed} == set()
    assert "pacb-sequel" in instruments
    warnings = ts.check_models(specs, instruments)
    assert warnings == []
    # Flongle has no archive model; it is reachable through Europe PMC only
    assert "Flongle" in specs["ont-minion"].epmc_terms and "Vega" in specs["pacb-vega"].ena
    # Europe PMC terms are vendor-qualified or brand tokens: the bare words that matched knee implants, odometry
    # papers and buffer recipes live on 2026-10-17 must never come back
    all_terms = {t for s in specs.values() for t in s.epmc_terms}
    for banned in ("Vega", "Vega system", "Revio", "UG 100", "RS II system", "Sequel II", "Sequel", "Onso", "Element"):
        assert banned not in all_terms, banned
    assert specs["pacb-vega"].epmc_terms == ["PacBio Vega", "Vega sequencer"]
    assert all(t.startswith("PacBio") or t.lower().startswith("revio ") for t in specs["pacb-revio"].epmc_terms)
    assert all(t.startswith("PacBio") or t.lower().startswith("onso ") for t in specs["pacb-onso"].epmc_terms)
    assert all(t.startswith("PacBio ") for t in specs["pacb-rs-ii"].epmc_terms)
    # every launch month parses and sits before the first real deposits/mentions seen in the live audit
    launches = {eid: s.launch for eid, s in specs.items() if s.launch}
    assert all(ts.MONTH_RE.match(m) for m in launches.values())
    assert launches["pacb-revio"] == "2022-10" and launches["pacb-vega"] == "2024-11" and launches["pacb-onso"] == "2022-10"
    assert launches["ilmn-novaseq-x"] <= "2023-01" and launches["elem-aviti"] <= "2023-10" and launches["ultima-ug100"] <= "2024-05"
    assert "pacb-sequel-ii" not in launches  # early-access deposits predate the April 2019 launch; no floor


def test_load_archive_models_tolerates_sparse_and_odd_entries(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("entities:\n  a-one: {sra: 'Revio', ena: [Revio]}\n  b-two: null\n  c-three: [not, a, mapping]\n", encoding="utf-8")
    specs = ts.load_archive_models(p)
    assert set(specs) == {"a-one", "b-two"}
    assert specs["a-one"].sra == ["revio"] and specs["a-one"].ena == ["Revio"] and specs["a-one"].epmc_terms == []
    assert specs["b-two"].models_for("sra") == [] and specs["b-two"].models_for("epmc") == []
    (tmp_path / "empty.yaml").write_text("", encoding="utf-8")
    assert ts.load_archive_models(tmp_path / "empty.yaml") == {}
    assert ts.load_archive_models(tmp_path / "missing.yaml") == {}
    assert ts.load_instrument_index(tmp_path / "missing.yaml") == {}


def test_check_models_flags_missing_ids_stale_flags_and_case():
    specs = {
        "x-new": ts.ModelSpec("x-new", proposed=True, sra=["x"], ena=["X"]),
        "x-landed": ts.ModelSpec("x-landed", proposed=True, sra=["y"], ena=["Y"]),
        "x-unknown": ts.ModelSpec("x-unknown", sra=["Z"], ena=["Z"]),
        "x-fine": ts.ModelSpec("x-fine", sra=["w"], ena=["W"]),
    }
    instruments = {"x-landed": {"name": "L", "vendor": "V", "ticker": ""}, "x-fine": {"name": "F", "vendor": "V", "ticker": ""}}
    assert ts.check_models(specs, instruments) == [
        "x-new: proposed, add the gazetteer entry",
        "x-landed: is in instruments.yaml now; drop `proposed: true` from archive_models.yaml",
        "x-unknown: NOT in instruments.yaml",
        "x-unknown: sra string 'Z' must be lower case",
    ]
    assert ts.check_models(specs, {}) == ["x-unknown: sra string 'Z' must be lower case"]  # no gazetteer: ids unchecked


def test_vendor_expansion_and_entity_resolution():
    specs = _specs()
    instruments = {
        "pacb-revio": {"name": "Revio", "vendor": "Pacific Biosciences", "ticker": "PACB"},
        "pacb-sequel-ii": {"name": "Sequel II", "vendor": "Pacific Biosciences", "ticker": "PACB"},
        "ont-minion": {"name": "MinION", "vendor": "Oxford Nanopore Technologies", "ticker": "ONT.L"},
        "txg-xenium": {"name": "Xenium", "vendor": "10x Genomics", "ticker": "TXG"},  # no archive models -> never expanded
    }
    for token in ("pacb", "PACB", "Pacific Biosciences", "pacific", "biosciences"):
        assert ts.expand_vendor(token, instruments, specs) == ["pacb-revio", "pacb-sequel-ii"], token
    assert ts.expand_vendor("nanopore", instruments, specs) == ["ont-minion"]
    assert ts.expand_vendor("ont.l", instruments, specs) == ["ont-minion"]
    assert ts.expand_vendor("txg", instruments, specs) == [] and ts.expand_vendor("", instruments, specs) == []
    # partial tokens must start a word and be at least three characters: no accidental select-all
    for token in ("i", "e", "ci", "cific", "sciences", "ore"):
        assert ts.expand_vendor(token, instruments, specs) == [], token
    # vendor fallback from archive_models when the id is not in the gazetteer
    assert ts.expand_vendor("pacb", {}, specs) == ["pacb-revio", "pacb-sequel-ii"]
    ids, warnings = ts.resolve_entities(["ont-minion", "nope"], ["pacb", "acme"], instruments, specs)
    assert ids == ["pacb-revio", "pacb-sequel-ii", "ont-minion"]  # config order, de-duplicated
    assert len(warnings) == 2 and "nope" in warnings[0] and "acme" in warnings[1]


# ---- outputs ----------------------------------------------------------------------------------
def _rows():
    return [
        {"source": "sra", "entity_id": "pacb-revio", "model": "revio", "month": "2024-01", "count": 38},
        {"source": "sra", "entity_id": "pacb-revio", "model": "revio", "month": "2024-02", "count": 40},
        {"source": "sra", "entity_id": "pacb-sequel-ii", "model": "sequel ii", "month": "2024-01", "count": 7272},
        {"source": "epmc", "entity_id": "pacb-revio", "model": "Revio", "month": "2024-01", "count": 7},
    ]


def test_csv_and_markdown_outputs(tmp_path):
    months = ["2024-01", "2024-02"]
    csv_path = tmp_path / "out" / "ts.csv"
    ts.write_csv(_rows(), csv_path)
    text = csv_path.read_text(encoding="utf-8").splitlines()
    # the unit travels with every row: experiments (sra), runs (ena), papers (epmc)
    assert text[0] == "source,entity_id,model,month,count,unit" and text[1] == "sra,pacb-revio,revio,2024-01,38,experiments" and len(text) == 5
    assert text[4] == "epmc,pacb-revio,Revio,2024-01,7,papers"
    ts.write_csv([{"source": "ena", "entity_id": "pacb-revio", "model": "Revio", "month": "2024-01", "count": 1500}], csv_path)
    assert csv_path.read_text(encoding="utf-8").splitlines()[1] == "ena,pacb-revio,Revio,2024-01,1500,runs"
    md = ts.render_markdown(_rows(), months)
    assert "## sra (SRA experiments by instrument_model)" in md and "## epmc (open-access papers naming the model in Methods)" in md
    assert "ena = ENA runs (experiment-run rows" in md and "Blank = never fetched; fail = the request failed" in md
    assert "| month | pacb-revio: revio | pacb-sequel-ii: sequel ii | total |" in md
    assert "|---|---:|---:|---:|" in md  # numbers right-aligned
    assert "| 2024-01 | 38 | 7,272 | 7,310 |" in md  # thousands separators
    assert "| 2024-02 | 40 |  | 40 |" in md  # month never fetched stays blank
    assert "| **total** | 78 | 7,272 | 7,350 |" in md
    assert ts.render_markdown([], months).rstrip().endswith("_No data._")


def test_markdown_labels_order_and_failed_cells():
    months = ["2024-01", "2024-02"]
    rows = [
        {"source": "epmc", "entity_id": "pacb-sequel-ii", "model": "Sequel II|Sequel IIe", "month": "2024-01", "count": 5},
        {"source": "ena", "entity_id": "pacb-revio", "model": "Revio", "month": "2024-01", "count": 38},
        {"source": "sra", "entity_id": "pacb-revio", "model": "revio", "month": "2024-01", "count": 38},
        {"source": "sra", "entity_id": "pacb-sequel-ii", "model": "sequel ii", "month": "2024-01", "count": 7272},
    ]
    names = {"pacb-revio": "Revio", "pacb-sequel-ii": "Sequel II"}
    order = [("sra", "pacb-revio", "revio"), ("sra", "pacb-sequel-ii", "sequel ii"), ("ena", "pacb-revio", "Revio"), ("epmc", "pacb-sequel-ii", "Sequel II|Sequel IIe")]
    failed = {("sra", "pacb-revio", "revio", "2024-02"), ("ena", "pacb-revio", "Revio", "2024-02")}
    md = ts.render_markdown(rows, months, sources=["sra", "ena", "epmc"], names=names, failed=failed, order=order)
    # sources in the requested order, not alphabetical
    assert md.index("## sra (") < md.index("## ena (") < md.index("## epmc (")
    # gazetteer names label the columns; the pipe-joined epmc term set is rendered with commas, never a bare pipe
    assert "| month | Revio (revio) | Sequel II (sequel ii) | total |" in md
    assert "| month | Sequel II (Sequel II, Sequel IIe) | total |" in md
    assert "\\|" not in md
    # every header has as many cells as its separator
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("|---"):
            assert lines[i - 1].count("|") == line.count("|"), (lines[i - 1], line)
    # a failed month is marked, a never-fetched month is blank
    assert "| 2024-02 | fail |  | 0 |" in md.split("## sra")[1].split("## ena")[0]
    assert "| 2024-02 | fail | 0 |" in md.split("## ena")[1].split("## epmc")[0]
    assert ts.series_label("x-id", "Model|Other") == "x-id: Model, Other" and ts.series_label("x", "MinION", {"x": "MinION"}) == "MinION (MinION)"


def test_png_is_optional_and_never_raises(tmp_path):
    ok = ts.write_png(_rows(), ["2024-01", "2024-02"], tmp_path / "chart.png")
    try:
        import matplotlib  # noqa: F401

        assert ok is True and (tmp_path / "chart.png").exists()
    except ImportError:
        assert ok is False and not (tmp_path / "chart.png").exists()
    assert ts.write_png([], ["2024-01"], tmp_path / "empty.png") is False


# ---- failure reasons, launch floor, run() verdicts --------------------------------------------
class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class StatusHttp:
    """Mimics litradar.http.Http.get: queued Response-like objects (None = the client gave up)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,)):
        self.calls.append((url, params))
        return self.responses.pop(0) if self.responses else None


def test_failure_reasons_name_the_http_status_or_cause(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    jobs = ts.plan_jobs(_specs(), ["pacb-revio"], ["2026-08", "2026-09", "2026-10"], ["sra"])
    http = StatusHttp([_Resp(400, None, text='{"error":"API key invalid"}'), None, _Resp(200, _sra(12))])
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=date(2026, 10, 17))
    assert rep["failed"] == ["sra:pacb-revio:revio:2026-08", "sra:pacb-revio:revio:2026-09"]
    assert [(f["month"], f["reason"]) for f in rep["failures"]] == [
        ("2026-08", 'HTTP 400: {"error":"API key invalid"}'),
        ("2026-09", ts.NO_ANSWER),
    ]
    assert rep["failures"][0]["source"] == "sra" and rep["failures"][0]["entity_id"] == "pacb-revio" and rep["failures"][0]["model"] == "revio"
    assert store.get_timeseries_count("sra", "pacb-revio", "revio", "2026-10")["count"] == 12
    # the detail functions on every unusual answer shape
    assert ts.sra_count_detail(StatusHttp([_Resp(200, None, text="<html>")]), "Revio", "2026-08-01", "2026-08-31") == (None, "non-JSON body")
    assert ts.sra_count_detail(StatusHttp([_Resp(200, {"error": "API rate limit exceeded"})]), "Revio", "2026-08-01", "2026-08-31")[1].startswith("NCBI error: API rate limit")
    assert ts.ena_count_detail(StatusHttp([_Resp(503, None, text="Service Unavailable")]), "Revio", "2026-08-01", "2026-08-31") == (None, "HTTP 503: Service Unavailable")
    assert ts.ena_count_detail(StatusHttp([_Resp(200, {"count": "n/a"})]), "Revio", "2026-08-01", "2026-08-31") == (None, "non-numeric count 'n/a'")
    assert ts.epmc_count_detail(StatusHttp([_Resp(200, {"version": "6.9"})]), ["PacBio Revio"], "2026-08-01", "2026-08-31") == (None, "unexpected payload (no hitCount)")
    assert ts.epmc_count_detail(StatusHttp([]), [], "2026-08-01", "2026-08-31") == (None, "no Europe PMC terms configured")
    # a client that only offers get_json still works and reports 'no answer'
    assert ts.sra_count_detail(FakeHttp([None]), "Revio", "2026-08-01", "2026-08-31") == (None, ts.NO_ANSWER)
    # a garbage NCBI key is not sent (and so cannot earn the 400 it used to)
    http = StatusHttp([_Resp(200, _sra(1))])
    ts.sra_count(http, "Revio", "2026-08-01", "2026-08-31", api_key="# https://account.ncbi.nlm.nih.gov/settings/")
    assert "api_key" not in http.calls[0][1]


def test_launch_floor_stores_zero_without_a_request(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    specs = _specs()
    jobs = ts.plan_jobs(specs, ["pacb-revio"], ["2022-08", "2022-09", "2022-10", "2022-11"], ["sra", "epmc"])
    http = FakeHttp([_sra(0), _sra(3), {"hitCount": 0}, {"hitCount": 2}])
    rep = ts.backfill(http, store, jobs, today=date(2026, 10, 17), launches={"pacb-revio": "2022-10"})
    assert rep["pre_launch"] == 4 and rep["requests"] == 4 and len(http.calls) == 4
    assert rep["by_source"]["sra"]["pre_launch"] == 2 and rep["by_source"]["epmc"]["pre_launch"] == 2
    rows = store.timeseries_rows(entity_ids=["pacb-revio"])
    assert {(r["source"], r["month"]): r["count"] for r in rows} == {
        ("sra", "2022-08"): 0, ("sra", "2022-09"): 0, ("sra", "2022-10"): 0, ("sra", "2022-11"): 3,
        ("epmc", "2022-08"): 0, ("epmc", "2022-09"): 0, ("epmc", "2022-10"): 0, ("epmc", "2022-11"): 2,
    }
    # pre-launch months stay request-free on a forced re-run too; only the four post-launch jobs go to the network
    http = FakeHttp([_sra(0), _sra(3), {"hitCount": 0}, {"hitCount": 2}])
    rep = ts.backfill(http, store, jobs, today=date(2026, 10, 17), launches={"pacb-revio": "2022-10"}, force=True)
    assert rep["pre_launch"] == 4 and rep["requests"] == 4 and rep["failed"] == [] and len(http.calls) == 4
    # a bad launch value in the config is ignored with a warning, never a crash
    p = tmp_path / "m.yaml"
    p.write_text("entities:\n  a-one: {sra: [x], ena: [X], launch: 'October 2022'}\n  b-two: {sra: [y], ena: [Y], launch: 2022-10}\n", encoding="utf-8")
    loaded = ts.load_archive_models(p)
    assert loaded["a-one"].launch == "" and loaded["b-two"].launch == "2022-10"


def test_run_verdicts_future_months_and_output_stem(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    # (a) two of six requests fail: a warning with the reasons, exit code 0 territory (errors empty), cells marked fail
    http = StatusHttp([_Resp(200, _sra(38)), _Resp(429, None, text="too many requests"), _Resp(200, {"count": "38"}), None,
                       _Resp(200, {"hitCount": 7}), _Resp(200, {"hitCount": 9})])
    rep = ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2024-01", end="2024-02",
                 http=http, store=store, today=date(2026, 10, 17), chart=False)
    assert rep["errors"] == []
    assert rep["warnings"] == [
        "sra: 1 of 2 count request(s) failed (shown as `fail`, not cached): HTTP 429: too many requests (x1). Re-run to retry",
        f"ena: 1 of 2 count request(s) failed (shown as `fail`, not cached): {ts.NO_ANSWER} (x1). Re-run to retry",
    ]
    md = Path(rep["outputs"]["markdown"]).read_text(encoding="utf-8")
    assert "| 2024-02 | fail | 0 |" in md.split("## sra")[1].split("## ena")[0]
    assert "| 2024-02 | fail | 0 |" in md.split("## ena")[1].split("## epmc")[0]
    # (b) every request of a source fails: that source is an error (exit 1), the others carry on
    http = StatusHttp([_Resp(400, None, text="bad key"), _Resp(400, None, text="bad key"), _Resp(200, {"count": "40"}), _Resp(200, {"count": "41"}),
                       _Resp(200, {"hitCount": 7}), _Resp(200, {"hitCount": 9})])
    rep = ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2024-01", end="2024-02",
                 http=http, store=store, today=date(2026, 10, 17), chart=False, force=True)
    assert rep["errors"] == ["sra: all 2 count request(s) of this run failed: HTTP 400: bad key (x2). Nothing cached for them; fix the cause and re-run"]
    assert [w for w in rep["warnings"] if w.startswith("ena") or w.startswith("epmc")] == []
    assert store.get_timeseries_count("sra", "pacb-revio", "revio", "2024-01")["count"] == 38  # the earlier value survives a failed refresh
    # (c) future months: --to is clamped with a warning, --from in the future is an error before any request
    rep = ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2026-09", end="2027-03",
                 http=FakeHttp([]), store=store, today=date(2026, 10, 17), dry_run=True)
    assert rep["errors"] == [] and rep["to"] == "2026-10" and rep["months"] == 2
    assert rep["warnings"] == ["--to 2027-03 is in the future; clamped to the current month 2026-10 (future months cannot have deposits)"]
    rep = ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2026-11", end="2027-03",
                 http=FakeHttp([]), store=store, today=date(2026, 10, 17), dry_run=True)
    assert rep["errors"] == ["--from 2026-11 is in the future (current month 2026-10); nothing can be deposited or published yet"] and "backfill" not in rep
    # (d) the stem carries the selection, shortened with a hash when long
    assert ts.output_stem("2019-01", "2026-09", [], ["pacb"], ["pacb-revio"]) == "timeseries_2019-01_2026-09_pacb"
    assert ts.output_stem("2019-01", "2026-09", ["pacb-revio", "pacb-sequel-ii"], ["Oxford Nanopore"], ["a", "b", "c"]) == "timeseries_2019-01_2026-09_oxford-nanopore+pacb-revio+pacb-sequel-ii"
    long = ts.output_stem("2019-01", "2026-09", ["ilmn-novaseq-x", "ilmn-novaseq-6000", "ilmn-nextseq-2000", "ilmn-miseq"], [], ["a", "b", "c", "d"])
    assert long.startswith("timeseries_2019-01_2026-09_ilmn-novaseq-x+4ids-") and len(long) < 70
    assert ts.output_stem("2019-01", "2026-09", [], [], ["x"]) == "timeseries_2019-01_2026-09_all"


# ---- end-to-end through run() and the CLI -----------------------------------------------------
def test_run_end_to_end_offline(tmp_path):
    http = FakeHttp([_sra(38), _sra(40), {"count": "38"}, {"count": "41"}, {"hitCount": 7}, {"hitCount": 9}])
    store = Store(tmp_path / "db.sqlite")
    # a row from a model string that is no longer configured for this entity must not leak into the outputs
    store.put_timeseries_count("sra", "pacb-revio", "revio (old spelling)", "2024-01", 5)
    store.put_timeseries_count("epmc", "pacb-revio", "Revio|Revio legacy term", "2024-01", 9)
    store.commit()
    rep = ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2024-01", end="2024-02",
                 http=http, store=store, today=date(2026, 10, 17), chart=False)
    assert rep["errors"] == [] and rep["warnings"] == [] and rep["entities"] == ["pacb-revio"] and rep["months"] == 2
    assert rep["backfill"]["requests"] == 6 and rep["backfill"]["failures"] == [] and rep["outputs"]["rows"] == 6
    # the selection is part of the file stem, so two vendors written into one --out directory keep both files
    assert Path(rep["outputs"]["csv"]).name == "timeseries_2024-01_2024-02_pacb-revio.csv" and Path(rep["outputs"]["markdown"]).exists()
    assert "png" not in rep["outputs"]
    csv_text = Path(rep["outputs"]["csv"]).read_text(encoding="utf-8")
    assert "old spelling" not in csv_text and "legacy term" not in csv_text and csv_text.count("\n") == 7
    assert "ena,pacb-revio,Revio,2024-01,38,runs" in csv_text and "sra,pacb-revio,revio,2024-01,38,experiments" in csv_text
    md = Path(rep["outputs"]["markdown"]).read_text(encoding="utf-8")
    assert "| month | Revio (revio) | total |" in md and "| 2024-01 | 38 | 38 |" in md  # sra table: gazetteer name, one series + total
    assert "old spelling" not in md
    # a second run into the same directory replaces its own files and says so
    rep2 = ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2024-01", end="2024-02",
                  http=FakeHttp([]), store=store, today=date(2026, 10, 17), chart=False)
    assert rep2["errors"] == [] and any("replacing the earlier outputs timeseries_2024-01_2024-02_pacb-revio" in w for w in rep2["warnings"])
    # a config dir without archive_models.yaml is an error in the report, not a traceback
    rep = ts.run(tmp_path, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], http=http, store=store)
    assert len(rep["errors"]) == 1 and "archive_models.yaml" in rep["errors"][0]
    # nothing selected / bad month / unknown source are reported, not raised
    assert ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", http=http, store=store)["errors"]
    assert "before" in ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2024-03", end="2024-01", http=http, store=store)["errors"][0]
    assert "unknown sources" in ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], sources=["geo"], http=http, store=store)["errors"][0]


def test_cli_dry_run_makes_no_requests(tmp_path, monkeypatch, capsys):
    from litradar.cli import main

    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "cli.sqlite"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "digests"))
    rc = main(["timeseries", "--vendor", "pacb", "--from", "2024-01", "--to", "2024-03", "--sources", "sra,ena", "--dry-run", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    rep = json.loads(out)
    assert rep["dry_run"] is True and rep["entities"][:1] == ["pacb-rs-ii"] and "pacb-revio" in rep["entities"]
    # 6 PacBio entities = 6 series per source (one request per entity, however many strings it lists)
    # x 3 months x 2 sources = 36 planned requests, minus the 6 Vega months before its 2024-11 launch (stored as 0, no request)
    assert rep["backfill"]["requests"] == 30 and rep["backfill"]["cached"] == 0 and rep["backfill"]["pre_launch"] == 6
    assert rep["series"] == 12 and rep["consolidated"] == {"derived": 0, "pruned": 0, "partial": 0, "entities": []}
    # without --json the summary lines carry the same digits
    rc = main(["timeseries", "--vendor", "pacb", "--from", "2024-01", "--to", "2024-03", "--sources", "sra,ena", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("timeseries: 6 entities, 12 series, 3 months (2024-01..2024-03), sources sra,ena\n")
    assert "dry run: would make 30 requests; 0 cached, 6 pre-launch zeros, 0 failed, 0 kept; refresh cutoff " in out
    assert "(sra 15 requests / 0 cached, ena 15 requests / 0 cached)" in out
    # --all plans every entity; --all with --vendor warns and still selects everything
    rc = main(["timeseries", "--all", "--vendor", "pacb", "--from", "2024-01", "--to", "2024-01", "--sources", "sra", "--dry-run", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(rep["entities"]) == 27 and rep["backfill"]["requests"] == 24 - 1  # 24 sra series, Vega pre-launch
    assert any("--all selects every entity" in w for w in rep["warnings"])
    assert main(["timeseries", "--from", "2024-01", "--to", "2024-01", "--dry-run"]) == 1  # no entities selected


def test_weekly_workflow_refreshes_the_cache_after_the_radar_run():
    """weekly.yml: one `timeseries --all` step after `litradar run` and before the database is compressed, allowed to
    fail without failing the digest, no `${{ }}` in its shell, every action still pinned to a full SHA."""
    import re

    import yaml

    path = ROOT / ".github" / "workflows" / "weekly.yml"
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = wf["jobs"]["radar"]["steps"]
    runs = [s.get("run", "") for s in steps]
    radar_i = next(i for i, r in enumerate(runs) if "python -m litradar run " in r)
    ts_i = next(i for i, r in enumerate(runs) if "python -m litradar timeseries --all --refresh-months 2 --no-chart" in r)
    compress_i = next(i for i, r in enumerate(runs) if "zstd -T0" in r)
    assert radar_i < ts_i < compress_i
    step = steps[ts_i]
    assert step["continue-on-error"] is True and "${{" not in step["run"] and "set -euo pipefail" in step["run"]
    assert re.search(r"timeout -s INT -k 60 \d+m python -m litradar timeseries --all --refresh-months 2 --no-chart", step["run"])
    assert "222" in path.read_text(encoding="utf-8")   # the documented weekly request budget
    assert wf["jobs"]["radar"]["permissions"] == {"contents": "read"}
    pin = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
    for job in wf["jobs"].values():
        for s in job.get("steps", []):
            if s.get("uses"):
                assert pin.match(s["uses"]), s["uses"]
    # the on-demand job is untouched: still one vendor into a scratch database, nothing committed
    assert "--vendor \"$VENDOR_INPUT\"" in wf["jobs"]["timeseries"]["steps"][-2]["run"]


def test_check_config_validates_archive_models(capsys):
    from litradar.cli import main

    rc = main(["check-config"])
    out = capsys.readouterr().out
    assert rc == 0
    line = [ln for ln in out.splitlines() if ln.startswith("archive_models:")]
    assert len(line) == 1 and "entities" in line[0] and "0 warnings" in line[0], out
    assert "proposed" not in out
