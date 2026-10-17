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


def _sra(count):
    return {"header": {"type": "esearch", "version": "0.3"}, "esearchresult": {"count": str(count), "retmax": "0", "retstart": "0", "idlist": [], "translationset": [], "querytranslation": "x"}}


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
    http = FakeHttp([
        _sra(851),
        {"header": {}, "esearchresult": {"count": "0", "retmax": "0", "idlist": [], "warninglist": {"quotedphrasesnotfound": ['"instrument vega"[Properties]'], "outputmessages": ["No items found."]}}},
        {"error": "API rate limit exceeded", "api-key": "x"},
        {"esearchresult": {"count": "lots"}},
        [],
        None,
    ])
    assert ts.sra_count(http, "Revio", "2025-09-01", "2025-09-30") == 851
    assert ts.sra_count(http, "Vega", "2025-09-01", "2025-09-30") == 0  # phrase not found == zero deposits
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
    # force re-fetches everything; dry_run plans without touching the network
    http = FakeHttp([_sra(1)] * 4)
    assert ts.backfill(http, store, jobs, refresh_months=2, today=today, force=True)["requests"] == 4 and len(http.calls) == 4
    http = FakeHttp([])
    rep = ts.backfill(http, store, jobs, refresh_months=2, today=today, dry_run=True)
    assert rep["requests"] == 2 and rep["cached"] == 2 and http.calls == []


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
    for eid, spec in specs.items():
        assert spec.proposed or eid in instruments, f"{eid} is not a gazetteer id"
        assert all(s == s.lower() for s in spec.sra), eid
        assert set(spec.ena) <= ARCHIVE_VOCABULARY, f"{eid}: unknown ENA instrument_model {set(spec.ena) - ARCHIVE_VOCABULARY}"
        assert sorted(spec.sra) == sorted(v.lower() for v in spec.ena), f"{eid}: sra and ena lists must be the same models"
        assert spec.sra or spec.ena or spec.epmc_terms, eid
    # every archive model string is claimed by exactly one entity
    claimed = [m for s in specs.values() for m in s.ena]
    assert len(claimed) == len(set(claimed)), "an ENA model string is listed under two entities"
    # only the two legacy PacBio ids are pre-registered ahead of the gazetteer
    assert {eid for eid, s in specs.items() if s.proposed} == {"pacb-rs-ii", "pacb-sequel"}
    warnings = ts.check_models(specs, instruments)
    assert all("proposed" in w for w in warnings), warnings
    # Flongle has no archive model; it is reachable through Europe PMC only
    assert "Flongle" in specs["ont-minion"].epmc_terms and "Vega" in specs["pacb-vega"].ena


def test_load_archive_models_tolerates_sparse_and_odd_entries(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("entities:\n  a-one: {sra: 'Revio', ena: [Revio]}\n  b-two: null\n  c-three: [not, a, mapping]\n", encoding="utf-8")
    specs = ts.load_archive_models(p)
    assert set(specs) == {"a-one", "b-two"}
    assert specs["a-one"].sra == ["revio"] and specs["a-one"].ena == ["Revio"] and specs["a-one"].epmc_terms == []
    assert specs["b-two"].models_for("sra") == [] and specs["b-two"].models_for("epmc") == []
    assert ts.load_archive_models(tmp_path / "empty.yaml") == {} if (tmp_path / "empty.yaml").write_text("", encoding="utf-8") == 0 else True
    assert ts.load_instrument_index(tmp_path / "missing.yaml") == {}


def test_vendor_expansion_and_entity_resolution():
    specs = _specs()
    instruments = {
        "pacb-revio": {"name": "Revio", "vendor": "Pacific Biosciences", "ticker": "PACB"},
        "pacb-sequel-ii": {"name": "Sequel II", "vendor": "Pacific Biosciences", "ticker": "PACB"},
        "ont-minion": {"name": "MinION", "vendor": "Oxford Nanopore Technologies", "ticker": "ONT.L"},
        "txg-xenium": {"name": "Xenium", "vendor": "10x Genomics", "ticker": "TXG"},  # no archive models -> never expanded
    }
    for token in ("pacb", "PACB", "Pacific Biosciences", "pacific"):
        assert ts.expand_vendor(token, instruments, specs) == ["pacb-revio", "pacb-sequel-ii"], token
    assert ts.expand_vendor("nanopore", instruments, specs) == ["ont-minion"]
    assert ts.expand_vendor("ont.l", instruments, specs) == ["ont-minion"]
    assert ts.expand_vendor("txg", instruments, specs) == [] and ts.expand_vendor("", instruments, specs) == []
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
    rep = ts.run(CONFIG, tmp_path / "db.sqlite", tmp_path / "out", entities=["pacb-revio"], start="2024-01", end="2024-02",
                 http=http, store=store, today=date(2026, 10, 17), chart=False)
    assert rep["errors"] == [] and rep["entities"] == ["pacb-revio"] and rep["months"] == 2
    assert rep["backfill"]["requests"] == 6 and rep["outputs"]["rows"] == 6
    assert Path(rep["outputs"]["csv"]).name == "timeseries_2024-01_2024-02.csv" and Path(rep["outputs"]["markdown"]).exists()
    assert "png" not in rep["outputs"]
    md = Path(rep["outputs"]["markdown"]).read_text(encoding="utf-8")
    assert "| 2024-01 | 38 | 38 |" in md  # sra table: one series + total
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
