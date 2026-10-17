"""PRIDE Archive adapter against canned payloads (no network)."""

from litradar.sources import pride


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url, params=None, cache=False):
        self.calls.append((url, params))
        return self.payloads.pop(0) if self.payloads else None


def _proj(acc, day, instruments, exp=("Bottom-up proteomics",), org=("Homo sapiens (human)",), **kw):
    d = {
        "accession": acc, "title": f"Dataset {acc}", "publicationDate": day, "submissionDate": "2026-01-01",
        "instruments": list(instruments), "experimentTypes": list(exp), "organisms": list(org),
        "affiliations": ["Example Institute of Proteomics"], "submissionType": "PARTIAL",
    }
    d.update(kw)
    return d


def _page(n, day, prefix="PXD9"):
    return [_proj(f"{prefix}{i:05d}", day, ["Orbitrap Exploris 480"]) for i in range(n)]


def test_normal_payload_one_record_per_instrument():
    http = FakeHttp([
        [
            _proj("PXD000001", "2026-10-15", ["Orbitrap Astral", "timsTOF Pro 2"], exp=["Data-independent acquisition", "Bottom-up proteomics"], org=["Homo sapiens (human)", "Mus musculus (mouse)"]),
            _proj("PXD000002", "2026-10-15", ["Q Exactive HF"]),
        ],
        [_proj("PXD000003", "2026-10-16", ["Olink Explore HT"], exp=["Olink affinity proteomics"], submissionType="AFFINITY")],
    ])
    recs = list(pride.fetch(http, "2026-10-15", "2026-10-16"))
    assert len(recs) == 4
    assert [r.accession for r in recs] == ["PXD000001:orbitrap-astral", "PXD000001:timstof-pro-2", "PXD000002:q-exactive-hf", "PXD000003:olink-explore-ht"]
    assert len({r.accession for r in recs}) == 4  # accession is the store's primary key
    r = recs[0]
    assert r.archive == "pride" and r.study_accession == "PXD000001" and r.instrument_model == "Orbitrap Astral"
    assert r.platform == "MS" and r.first_public == "2026-10-15" and r.study_title == "Dataset PXD000001"
    assert r.library_strategy == "Data-independent acquisition; Bottom-up proteomics"
    assert r.organism == "Homo sapiens (human); Mus musculus (mouse)" and r.center_name == "Example Institute of Proteomics"
    assert recs[3].platform == "AFFINITY"
    # one request per day, each with the exact-day filter the API supports (no range syntax)
    assert len(http.calls) == 2
    assert http.calls[0][0] == pride.SEARCH and http.calls[0][1]["filter"] == "publicationDate==2026-10-15"
    assert http.calls[1][1]["filter"] == "publicationDate==2026-10-16" and http.calls[1][1]["pageSize"] == 100
    assert pride.histogram(recs) == {"Orbitrap Astral": 1, "timsTOF Pro 2": 1, "Q Exactive HF": 1, "Olink Explore HT": 1}


def test_empty_payload_yields_nothing():
    http = FakeHttp([[], [], []])
    assert list(pride.fetch(http, "2026-10-13", "2026-10-15")) == []
    assert len(http.calls) == 3  # still one probe per day, then stop


def test_malformed_payloads_do_not_crash():
    http = FakeHttp([
        None,  # request failed / non-JSON
        {"error": "unexpected object instead of array"},
        ["not-a-dict", 42, {"title": "no accession"}, {"accession": "PXD000009", "instruments": None, "experimentTypes": "DIA", "organisms": [None, 7], "publicationDate": None}],
        [{"accession": "PXD000010", "publicationDate": "2026-10-16T00:00:00Z", "instruments": [{"@type": "CvParam", "accession": "MS:1001910", "name": "LTQ Orbitrap Elite"}, {"name": ""}, {"accession": "MS:1001911", "name": "Q Exactive"}, "Q Exactive"]}],
    ])
    recs = list(pride.fetch(http, "2026-10-13", "2026-10-16"))
    accs = [r.accession for r in recs]
    # the project without instruments is kept as one blank-instrument row; CvParam objects are read by name and deduped
    assert accs == ["PXD000009", "PXD000010:ltq-orbitrap-elite", "PXD000010:q-exactive"]
    assert recs[0].instrument_model == "" and recs[0].library_strategy == "" and recs[0].organism == "" and recs[0].first_public == ""
    assert recs[1].first_public == "2026-10-16" and recs[2].instrument_model == "Q Exactive"
    assert pride.to_records("garbage") == [] and pride.to_records({"accession": ""}) == []
    assert pride._names(None) == [] and pride._names("Orbitrap") == [] and pride._names([" A ", "A", {"value": "B"}]) == ["A", "B"]


def test_pagination_stops_on_short_page_and_empty_page():
    # day 1: a full page then a short page -> 2 calls; day 2: a full page then an empty page -> 2 calls
    http = FakeHttp([_page(100, "2026-10-15", "PXD1"), _page(37, "2026-10-15", "PXD2"), _page(100, "2026-10-16", "PXD3"), []])
    recs = list(pride.fetch(http, "2026-10-15", "2026-10-16"))
    assert len(recs) == 237 and len({r.accession for r in recs}) == 237
    assert [c[1]["page"] for c in http.calls] == [0, 1, 0, 1]
    assert [c[1]["filter"] for c in http.calls] == ["publicationDate==2026-10-15"] * 2 + ["publicationDate==2026-10-16"] * 2


def test_pagination_stops_when_server_repeats_a_page_or_hits_max_pages():
    same = _page(100, "2026-10-15")
    http = FakeHttp([same, same, same])
    recs = list(pride.search_day(http, "2026-10-15"))
    assert len(recs) == 100 and len(http.calls) == 2  # second identical page adds nothing -> stop
    http = FakeHttp([_page(100, "2026-10-15", f"PXD{i}") for i in range(10)])
    recs = list(pride.search_day(http, "2026-10-15", max_pages=3))
    assert len(recs) == 300 and len(http.calls) == 3


def test_window_helpers_and_caps():
    assert pride._days("2026-10-15", "2026-10-17") == ["2026-10-15", "2026-10-16", "2026-10-17"]
    assert pride._days("2026-10-17", "2026-10-15") == [] and pride._days("bad", "2026-10-15") == []
    assert pride._days("2026-10-15T12:00:00Z", "2026-10-15") == ["2026-10-15"]
    http = FakeHttp([[_proj("PXD000001", "2026-10-15", ["A"]), _proj("PXD000002", "2026-10-15", ["B", "C"])], [_proj("PXD000003", "2026-10-16", ["D"])]])
    recs = list(pride.fetch(http, "2026-10-15", "2026-10-16", max_projects=1))
    assert [r.accession for r in recs] == ["PXD000001:a"] and len(http.calls) == 1
    assert pride._slug("SELECT SERIES Cyclic IMS") == "select-series-cyclic-ims" and pride._slug("***") == "unknown"
    two = pride.to_records(_proj("PXD000004", "2026-10-15", ["timsTOF", "TimsTOF"]))
    assert [r.accession for r in two] == ["PXD000004:timstof", "PXD000004:timstof-2"]


def test_facet_instrument_counts_sum_over_days_and_skip_junk():
    http = FakeHttp([
        {"instruments": {"Orbitrap Exploris 480": 9, "timsTOF Pro": 3}, "experimentTypes": {"Bottom-up proteomics": 27}},
        None,
        {"instruments": "not-a-dict"},
        {"instruments": {"Orbitrap Exploris 480": 2, "Orbitrap Astral": "x"}},
    ])
    counts = pride.instrument_counts(http, "2026-10-14", "2026-10-17")
    assert counts == {"Orbitrap Exploris 480": 11, "timsTOF Pro": 3}
    assert len(http.calls) == 4 and http.calls[0][0] == pride.FACET and http.calls[0][1]["filter"] == "publicationDate==2026-10-14"
