"""CZ CELLxGENE Discover adapter tested against canned payloads (no network).

Payload shapes mirror the live `/curation/v1/datasets` and `/curation/v1/collections` responses
recorded on 2026-10-17 (see docs/sources/cellxgene.md).
"""
import yaml

from litradar.config import DEFAULT_CONFIG_DIR
from litradar.extract.gazetteer import Gazetteer
from litradar.http import DEFAULT_MIN_INTERVAL
from litradar.sources import cellxgene


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url, params=None, cache=False):
        self.calls.append((url, params))
        return self.payloads.pop(0) if self.payloads else None


def _ds(dataset_id, assays, published="2026-10-16T16:35:23+00:00", cells=1000, collection="c-1", **kw):
    d = {
        "dataset_id": dataset_id,
        "dataset_version_id": dataset_id + "-v",
        "collection_id": collection,
        "collection_name": "Atlas of something",
        "collection_doi": "10.1000/xyz",
        "title": f"Dataset {dataset_id}",
        "assay": [{"label": lab, "ontology_term_id": eid} for lab, eid in assays],
        "organism": [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}],
        "cell_count": cells,
        "primary_cell_count": cells,
        "published_at": published,
        "revised_at": None,
        "spatial": None,
        "suspension_type": ["cell"],
        "schema_version": "7.1.0",
        "visibility": "PUBLIC",
        "tombstone": False,
        "processing_status": "SUCCESS",
    }
    d.update(kw)
    return d


WINDOW = ("2026-10-10", "2026-10-17")


def test_normal_payload_fields_and_single_request():
    payload = [
        _ds("d1", [("10x 3' v3", "EFO:0009922")], cells=638930),
        _ds("d2", [("10x 5' v2", "EFO:0009900"), ("10x 5' v3", "EFO:0022605")], published="2026-10-12T00:00:00+00:00", cells=410646, collection="c-2"),
        _ds("d3", [("curio biosciences seeker, 3mm", "EFO:0920002")], published="2026-10-11T09:00:00+00:00", cells=3292, spatial={"is_single": True, "has_fullres": False}),
        _ds("old", [("10x 3' v2", "EFO:0009899")], published="2026-09-11T16:40:46+00:00"),  # outside the window
        _ds("rev", [("Drop-seq", "EFO:0008722")], published="2025-01-01T00:00:00+00:00", revised_at="2026-10-15T00:00:00+00:00"),
    ]
    http = FakeHttp([payload])
    recs = list(cellxgene.fetch(http, *WINDOW))
    assert len(http.calls) == 1
    assert http.calls[0][0] == "https://api.cellxgene.cziscience.com/curation/v1/datasets"
    assert http.calls[0][1] == {"visibility": "PUBLIC"}

    by_acc = {r.accession: r for r in recs}
    # one row per (dataset, assay); multi-assay datasets get a '#EFO' suffix so accessions stay unique
    assert set(by_acc) == {"d1", "d2#EFO:0009900", "d2#EFO:0022605", "d3"}
    r = by_acc["d1"]
    assert r.archive == "cellxgene" and r.instrument_model == "10x 3' v3" and r.library_strategy == "EFO:0009922"
    assert r.platform == "single-cell" and r.first_public == "2026-10-16" and r.study_accession == "c-1"
    assert r.study_title == "Atlas of something | Dataset d1 | 638,930 cells"
    assert r.organism == "Homo sapiens" and r.center_name == ""
    assert (r.entity_id, r.vendor, r.ticker) == ("txg-chromium", "10x Genomics", "TXG")
    assert by_acc["d2#EFO:0022605"].instrument_model == "10x 5' v3" and by_acc["d2#EFO:0022605"].study_accession == "c-2"
    s = by_acc["d3"]
    assert s.platform == "spatial" and s.entity_id == "curio-seeker" and s.vendor == "Curio Bioscience"
    # revisions are ignored unless asked for; when asked, first_public stays the original date
    recs2 = {r.accession: r for r in cellxgene.fetch(FakeHttp([payload]), *WINDOW, include_revisions=True)}
    assert "rev" in recs2 and recs2["rev"].first_public == "2025-01-01" and recs2["rev"].entity_id == ""


def test_empty_payload_yields_nothing_without_fallback():
    http = FakeHttp([[]])
    assert list(cellxgene.fetch(http, *WINDOW)) == []
    assert len(http.calls) == 1  # an empty array is a valid answer: no fallback to /collections


def test_unavailable_endpoint_falls_back_to_collections():
    collections = [
        {"collection_id": "c-new", "name": "New collection", "doi": "10.1/a", "published_at": "2026-10-15T10:00:00+00:00", "revised_at": None,
         "datasets": [{"dataset_id": "x", "assay": [{"label": "10x 3' v3", "ontology_term_id": "EFO:0009922"}]}]},
        {"collection_id": "c-old", "name": "Old collection", "doi": None, "published_at": "2024-01-01T00:00:00+00:00", "revised_at": "2024-06-01T00:00:00+00:00", "datasets": []},
    ]
    detail = {"collection_id": "c-new", "name": "New collection", "doi": "10.1/a", "datasets": [
        {"dataset_id": "x", "title": "T", "assay": [{"label": "10x 3' v3", "ontology_term_id": "EFO:0009922"}],
         "organism": [{"label": "Mus musculus", "ontology_term_id": "NCBITaxon:10090"}], "cell_count": 42,
         "published_at": "2026-10-15T10:00:00+00:00", "revised_at": None},
    ]}
    http = FakeHttp([None, collections, detail])  # /datasets fails -> /collections -> /collections/c-new
    recs = list(cellxgene.fetch(http, *WINDOW))
    assert [c[0] for c in http.calls] == [
        "https://api.cellxgene.cziscience.com/curation/v1/datasets",
        "https://api.cellxgene.cziscience.com/curation/v1/collections",
        "https://api.cellxgene.cziscience.com/curation/v1/collections/c-new",
    ]
    assert len(recs) == 1
    r = recs[0]
    assert r.accession == "x" and r.study_accession == "c-new" and r.organism == "Mus musculus"
    assert r.study_title == "New collection | T | 42 cells" and r.first_public == "2026-10-15"


def test_malformed_payloads_are_skipped_not_raised():
    # top-level object instead of an array -> treated as unavailable -> fallback, which is itself junk
    http = FakeHttp([{"detail": "oops"}, {"not": "a list"}])
    assert list(cellxgene.fetch(http, *WINDOW)) == []
    assert len(http.calls) == 2

    # junk inside an otherwise valid array
    payload = [
        "a string",
        None,
        42,
        {"no_dataset_id": True, "published_at": "2026-10-16T00:00:00+00:00"},
        _ds("weird-dates", [("10x 3' v3", "EFO:0009922")], published="not a date"),
        _ds("bad-cells", [("10x 3' v3", "EFO:0009922")], cells="lots"),
        _ds("no-assay", [], cells=None),
        _ds("assay-junk", [], assay=["Smart-seq2", 7, None, {"label": None, "ontology_term_id": None}]),
        _ds("organism-junk", [("Visium Spatial Gene Expression V1", "EFO:0022857")], organism="Homo sapiens"),
    ]
    recs = {r.accession: r for r in cellxgene.fetch(FakeHttp([payload]), *WINDOW)}
    assert "weird-dates" not in recs  # unparseable publication date cannot be placed in the window
    assert recs["bad-cells"].study_title == "Atlas of something | Dataset bad-cells"  # no cell note when the count is not an int
    assert recs["no-assay"].instrument_model == "" and recs["no-assay"].entity_id == ""  # kept, like SRA/ENA records without a model
    assert recs["assay-junk"].instrument_model == "Smart-seq2" and recs["assay-junk"].library_strategy == ""
    assert recs["organism-junk"].organism == "" and recs["organism-junk"].entity_id == "txg-visium" and recs["organism-junk"].platform == "spatial"


def test_pagination_stops_at_caps():
    # fallback route: three candidate collections, cap of two detail calls -> newest two only, then stop
    colls = [
        {"collection_id": f"c{i}", "name": f"C{i}", "published_at": f"2026-10-1{i}T00:00:00+00:00", "revised_at": None, "datasets": []}
        for i in (1, 2, 3)
    ]
    details = [
        {"collection_id": "c3", "name": "C3", "datasets": [{"dataset_id": "d3", "assay": [{"label": "inDrop", "ontology_term_id": "EFO:0008780"}], "published_at": "2026-10-13T00:00:00+00:00", "cell_count": 1}]},
        {"collection_id": "c2", "name": "C2", "datasets": [{"dataset_id": "d2", "assay": [{"label": "inDrop", "ontology_term_id": "EFO:0008780"}], "published_at": "2026-10-12T00:00:00+00:00", "cell_count": 1}]},
        {"collection_id": "c1", "name": "C1", "datasets": [{"dataset_id": "d1", "assay": [{"label": "inDrop", "ontology_term_id": "EFO:0008780"}], "published_at": "2026-10-11T00:00:00+00:00", "cell_count": 1}]},
    ]
    http = FakeHttp([None, colls] + details)
    recs = list(cellxgene.fetch(http, *WINDOW, max_collection_details=2))
    assert [r.accession for r in recs] == ["d3", "d2"]
    assert len(http.calls) == 4 and http.calls[-1][0].endswith("/collections/c2")
    assert len(http.payloads) == 1  # the third detail was never requested

    # primary route: max_records stops the generator early
    payload = [_ds(f"d{i}", [("10x 3' v3", "EFO:0009922")]) for i in range(10)]
    recs = list(cellxgene.fetch(FakeHttp([payload]), *WINDOW, max_records=3))
    assert len(recs) == 3


def test_assay_to_gazetteer_mapping():
    cases = {
        ("10x 3' v3", "EFO:0009922"): "txg-chromium",
        ("10x 5' v1", "EFO:0011025"): "txg-chromium",
        ("10x multiome", "EFO:0030059"): "txg-multiome",
        ("10x GEM-X Flex v1", "EFO:0920088"): "txg-chromium-flex",
        ("Visium Spatial Gene Expression V1", "EFO:0022857"): "txg-visium",
        ("Visium HD", "EFO:9999999"): "txg-visium-hd",  # unseen id -> label rule
        ("Xenium", ""): "txg-xenium",
        ("Parse Evercode Whole Transcriptome v2", "EFO:0022601"): "parse-evercode",
        ("SPLiT-seq", "EFO:0009919"): "parse-evercode",
        ("BD Rhapsody Whole Transcriptome Analysis", "EFO:0700003"): "bd-rhapsody",
        ("Slide-seqV2", "EFO:0030062"): "slide-seq",  # the academic method since 2026-10-17; the kit stays Curio
        ("curio biosciences seeker, 3mm", "EFO:0920002"): "curio-seeker",
        ("Slide-tags", "EFO:9999998"): "slide-seq",  # unseen id -> label rule
        ("ScaleBio single cell RNA sequencing", "EFO:0022490"): "scale-bio",
        ("particle-templated instant partition sequencing", "EFO:0900000"): "fluent-pipseq",
        ("Drop-seq", "EFO:0008722"): "",
        ("sci-RNA-seq3", "EFO:0030028"): "",
        ("MERFISH", "EFO:0008992"): "",  # a method, not a product; MERSCOPE would map
        ("", ""): "",
    }
    for (label, eid), want in cases.items():
        assert cellxgene.entity_for(label, eid) == want, (label, eid)


def test_mapping_ids_exist_in_gazetteer_with_same_vendor_and_ticker():
    gz = Gazetteer.from_yaml(yaml.safe_load(open(DEFAULT_CONFIG_DIR / "instruments.yaml")), "instrument")
    for entity, (vendor, ticker) in cellxgene.ENTITY_VENDORS.items():
        e = gz.by_id.get(entity)
        assert e is not None, f"{entity} is not in config/instruments.yaml"
        assert (e.vendor, e.ticker) == (vendor, ticker), entity
    for entity in set(cellxgene.ASSAY_ENTITY.values()):
        assert entity in cellxgene.ENTITY_VENDORS, entity
    # when the gazetteer's own archive-model matcher fires on a live label, it must agree at vendor level
    for label, eid in [("Visium Spatial Gene Expression V1", "EFO:0022857"), ("Slide-seqV2", "EFO:0030062"),
                       ("BD Rhapsody Whole Transcriptome Analysis", "EFO:0700003"), ("10x GEM-X Flex v1", "EFO:0920088"),
                       ("ScaleBio single cell RNA sequencing", "EFO:0022490"), ("GEXSCOPE technology", "EFO:0700011")]:
        hit = gz.match_archive_model(label)
        assert hit is not None and hit.vendor == cellxgene.ENTITY_VENDORS[cellxgene.entity_for(label, eid)][0], label
    assert gz.match_archive_model("10x 3' v3") is None  # the adapter's map is the only source for the plain 10x labels


def test_histogram_and_window_summary():
    payload = [
        _ds("a", [("10x 3' v3", "EFO:0009922")], cells=10),
        _ds("b", [("10x 3' v3", "EFO:0009922"), ("10x 5' v2", "EFO:0009900")], cells=5, collection="c-2"),
        _ds("c", [("10x 3' v3", "EFO:0009922")], cells=100, published="2026-01-01T00:00:00+00:00"),
        "junk",
    ]
    hist = cellxgene.assay_histogram(payload, *WINDOW)
    assert hist[0] == {"assay": "10x 3' v3", "efo": "EFO:0009922", "entity_id": "txg-chromium", "datasets": 2, "cells": 15, "collections": 2, "vendor": "10x Genomics", "ticker": "TXG"}
    assert hist[1]["assay"] == "10x 5' v2" and hist[1]["datasets"] == 1
    assert cellxgene.assay_histogram(payload)[0]["datasets"] == 3  # no window -> all time
    summary = cellxgene.window_summary(payload, *WINDOW)
    assert (summary["datasets"], summary["collections"], summary["cells"]) == (2, 2, 15)


def test_http_pacing_entry_present():
    assert DEFAULT_MIN_INTERVAL["api.cellxgene.cziscience.com"] >= 1.0
