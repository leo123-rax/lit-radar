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
def test_plan_jobs_per_source_model_month():
    jobs = ts.plan_jobs(_specs(), ["pacb-sequel-ii"], ["2024-01", "2024-02"], ["sra", "ena", "epmc"])
    assert [(j.source, j.model, j.month) for j in jobs] == [
        ("sra", "sequel ii", "2024-01"), ("sra", "sequel ii", "2024-02"), ("sra", "sequel iie", "2024-01"), ("sra", "sequel iie", "2024-02"),
        ("ena", "Sequel II", "2024-01"), ("ena", "Sequel II", "2024-02"), ("ena", "Sequel IIe", "2024-01"), ("ena", "Sequel IIe", "2024-02"),
        ("epmc", "Sequel II|Sequel IIe", "2024-01"), ("epmc", "Sequel II|Sequel IIe", "2024-02"),
    ]
    assert jobs[-1].terms == ["Sequel II", "Sequel IIe"] and jobs[0].terms == []
    assert ts.plan_jobs(_specs(), ["unknown-id"], ["2024-01"], ["sra"]) == []


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
    assert text[0] == "source,entity_id,model,month,count" and text[1] == "sra,pacb-revio,revio,2024-01,38" and len(text) == 5
    md = ts.render_markdown(_rows(), months)
    assert "## sra (experiments by instrument_model)" in md and "## epmc (papers naming the model in Methods)" in md
    assert "| month | pacb-revio: revio | pacb-sequel-ii: sequel ii | total |" in md
    assert "| 2024-01 | 38 | 7272 | 7310 |" in md
    assert "| 2024-02 | 40 |  | 40 |" in md  # month with no Sequel II count stays blank
    assert "| **total** | 78 | 7272 | 7350 |" in md
    assert ts.render_markdown([], months).rstrip().endswith("_No data._")


def test_png_is_optional_and_never_raises(tmp_path):
    ok = ts.write_png(_rows(), ["2024-01", "2024-02"], tmp_path / "chart.png")
    try:
        import matplotlib  # noqa: F401

        assert ok is True and (tmp_path / "chart.png").exists()
    except ImportError:
        assert ok is False and not (tmp_path / "chart.png").exists()
    assert ts.write_png([], ["2024-01"], tmp_path / "empty.png") is False


# ---- end-to-end through run() and the CLI -----------------------------------------------------
def test_run_end_to_end_offline(tmp_path):
    http = FakeHttp([_sra(38), _sra(40), {"count": "38"}, {"count": "41"}, {"hitCount": 7}, {"hitCount": 9}])
    store = Store(tmp_path / "db.sqlite")
    # a row from a model string that is no longer configured for this entity must not leak into the outputs
    store.put_timeseries_count("sra", "pacb-revio", "revio (old spelling)", "2024-01", 5)
    store.put_timeseries_count("epmc", "pacb-revio", "Revio|Revio system", "2024-01", 9)
    store.commit()
    rep = ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2024-01", end="2024-02",
                 http=http, store=store, today=date(2026, 10, 17), chart=False)
    assert rep["errors"] == [] and rep["entities"] == ["pacb-revio"] and rep["months"] == 2
    assert rep["backfill"]["requests"] == 6 and rep["outputs"]["rows"] == 6
    assert Path(rep["outputs"]["csv"]).name == "timeseries_2024-01_2024-02.csv" and Path(rep["outputs"]["markdown"]).exists()
    assert "png" not in rep["outputs"]
    csv_text = Path(rep["outputs"]["csv"]).read_text(encoding="utf-8")
    assert "old spelling" not in csv_text and "Revio system" not in csv_text and csv_text.count("\n") == 7
    md = Path(rep["outputs"]["markdown"]).read_text(encoding="utf-8")
    assert "| 2024-01 | 38 | 38 |" in md  # sra table: one series + total
    assert "old spelling" not in md
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
    rc = main(["timeseries", "--vendor", "pacb", "--from", "2024-01", "--to", "2024-03", "--sources", "sra,ena", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    rep = json.loads(out)
    assert rep["dry_run"] is True and rep["entities"][:1] == ["pacb-rs-ii"] and "pacb-revio" in rep["entities"]
    # 6 PacBio entities with 8 archive model strings per source (rs ii, rs, sequel, sequel ii, sequel iie, revio, vega, onso)
    # x 3 months x 2 sources = 48 planned requests
    assert rep["backfill"]["requests"] == 48 and rep["backfill"]["cached"] == 0
    assert main(["timeseries", "--from", "2024-01", "--to", "2024-01", "--dry-run"]) == 1  # no entities selected


def test_check_config_validates_archive_models(capsys):
    from litradar.cli import main

    rc = main(["check-config"])
    out = capsys.readouterr().out
    assert rc == 0
    line = [ln for ln in out.splitlines() if ln.startswith("archive_models:")]
    assert len(line) == 1 and "entities" in line[0] and "0 warnings" in line[0], out
    assert "proposed" not in out
