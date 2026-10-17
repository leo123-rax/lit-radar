"""ClinicalTrials.gov API v2 adapter tested against canned payloads (no network)."""

from litradar.sources import clinicaltrials as ct


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url, params=None, cache=False):
        self.calls.append((url, dict(params or {})))
        return self.payloads.pop(0) if self.payloads else None


def _study(nct, title="T", posted="2026-10-15", sponsor="Example Diagnostics, Inc.", sponsor_class="INDUSTRY",
           collaborators=(), interventions=(), conditions=(), phases=("NA",), enrollment=(120, "ESTIMATED"),
           status="RECRUITING", study_type="INTERVENTIONAL", brief=""):
    ps = {
        "identificationModule": {"nctId": nct, "briefTitle": title, "officialTitle": title + " (official)"},
        "statusModule": {"overallStatus": status, "studyFirstPostDateStruct": {"date": posted, "type": "ACTUAL"}},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor, "class": sponsor_class},
                                       "collaborators": [{"name": c, "class": "OTHER"} for c in collaborators]},
        "conditionsModule": {"conditions": list(conditions)},
        "designModule": {"studyType": study_type, "phases": list(phases),
                         "enrollmentInfo": {"count": enrollment[0], "type": enrollment[1]}},
        "armsInterventionsModule": {"interventions": [{"type": t, "name": n} for t, n in interventions]},
    }
    if brief:
        ps["descriptionModule"] = {"briefSummary": brief}
    return {"protocolSection": ps}


NORMAL = {
    "totalCount": 2,
    "studies": [
        _study("NCT07000001", title="ctDNA-guided adjuvant therapy", sponsor="Example Diagnostics, Inc.",
               collaborators=["University Hospital A", "Example Pharma AG"],
               interventions=[("DIAGNOSTIC_TEST", "Signatera ctDNA assay"), ("DRUG", "Pembrolizumab")],
               conditions=["Colorectal Cancer", "Stage III Colon Cancer"], phases=["PHASE2", "PHASE3"],
               enrollment=(500, "ESTIMATED"), brief="Patients are randomised by circulating tumor DNA status."),
        _study("NCT07000002", title="Proteomic profiling with Olink panels", posted="2026-10-16T00:00:00",
               sponsor="Research Institute B", sponsor_class="OTHER", interventions=[("OTHER", "Olink Explore 3072")],
               conditions=["Healthy Volunteers"], phases=[], enrollment=(40, "ACTUAL"), status="COMPLETED",
               study_type="OBSERVATIONAL"),
    ],
}


def test_normal_payload_to_news_items():
    http = FakeHttp([NORMAL])
    items = list(ct.new_studies(http, "2026-10-03", "2026-10-17", sponsor_terms=["Example Diagnostics"]))
    assert [i.id for i in items] == ["ctgov:NCT07000001", "ctgov:NCT07000002"]
    a, b = items
    assert a.feed == "clinicaltrials" and a.title == "ctDNA-guided adjuvant therapy"
    assert a.url == "https://clinicaltrials.gov/study/NCT07000001" and a.published == "2026-10-15"
    # the summary carries every field the brief asks for, sponsor first so the gazetteer can match it
    assert a.summary.startswith("Sponsor: Example Diagnostics, Inc. (Industry). Collaborators: University Hospital A; Example Pharma AG. ")
    assert "Interventions: Diagnostic Test: Signatera ctDNA assay; Drug: Pembrolizumab" in a.summary
    assert "Conditions: Colorectal Cancer; Stage III Colon Cancer" in a.summary
    assert "Phase: Phase 2/3" in a.summary and "Enrollment: 500 (estimated)" in a.summary
    assert "Status: Recruiting" in a.summary and "Type: Interventional" in a.summary
    assert a.summary.endswith("Summary: Patients are randomised by circulating tumor DNA status.")
    assert len(a.summary) <= ct.SUMMARY_MAX
    # second study: timestamp trimmed to a date, no phase line when phases are empty, observational type
    assert b.published == "2026-10-16" and "Phase:" not in b.summary and "Type: Observational" in b.summary
    assert "Enrollment: 40 (actual)" in b.summary and "Status: Completed" in b.summary
    # request shape: date filter on first-post date, sponsor area query, field subset, countTotal on page 1
    assert len(http.calls) == 1
    url, params = http.calls[0]
    assert url == ct.API
    assert params["filter.advanced"] == "AREA[StudyFirstPostDate]RANGE[2026-10-03,2026-10-17]"
    assert params["query.spons"] == '"Example Diagnostics"' and "query.intr" not in params
    assert params["countTotal"] == "true" and params["pageSize"] == ct.PAGE_SIZE
    assert params["fields"].startswith("NCTId|BriefTitle|") and "LeadSponsorName" in params["fields"]


def test_empty_payload_yields_nothing():
    http = FakeHttp([{"totalCount": 0, "studies": []}])
    assert list(ct.new_studies(http, "2026-10-03", "2026-10-17", intervention_terms=["Signatera"])) == []
    assert len(http.calls) == 1 and http.calls[0][1]["query.intr"] == "Signatera"


def test_malformed_payloads_do_not_crash():
    # http failure (None), a list instead of an object, a 'studies' that is not a list, and studies missing pieces
    for bad in (None, ["not", "a", "dict"], {"studies": "nope"}, {"totalCount": 1}):
        http = FakeHttp([bad])
        assert list(ct.new_studies(http, "2026-10-03", "2026-10-17")) == []
    weird = {"studies": [
        "string instead of object",
        {"protocolSection": "not a dict"},
        {"protocolSection": {"identificationModule": {"briefTitle": "no id"}}},
        {"protocolSection": {"identificationModule": {"nctId": "NCT07000009"}}},  # bare: no status, sponsor, design
        {"noProtocolSection": {}},
    ]}
    items = list(ct.new_studies(FakeHttp([weird]), "2026-10-03", "2026-10-17"))
    assert [i.id for i in items] == ["ctgov:NCT07000009"]
    assert items[0].title == "NCT07000009" and items[0].published == "" and items[0].summary == ""


def test_pagination_follows_token_and_stops():
    page1 = {"totalCount": 3, "studies": [_study("NCT07000001"), _study("NCT07000002")], "nextPageToken": "TOK1"}
    page2 = {"studies": [_study("NCT07000003")]}  # no nextPageToken -> last page
    http = FakeHttp([page1, page2])
    items = list(ct.new_studies(http, "2026-10-03", "2026-10-17"))
    assert [i.id for i in items] == ["ctgov:NCT07000001", "ctgov:NCT07000002", "ctgov:NCT07000003"]
    assert len(http.calls) == 2
    assert "pageToken" not in http.calls[0][1]
    assert http.calls[1][1]["pageToken"] == "TOK1" and "countTotal" not in http.calls[1][1]
    assert http.calls[1][1]["filter.advanced"] == http.calls[0][1]["filter.advanced"]


def test_pagination_stops_on_repeated_token_empty_page_and_cap():
    # same token twice -> stop (defensive against an API loop)
    looping = FakeHttp([
        {"studies": [_study("NCT07000001")], "nextPageToken": "SAME"},
        {"studies": [_study("NCT07000002")], "nextPageToken": "SAME"},
        {"studies": [_study("NCT07000003")], "nextPageToken": "SAME"},
    ])
    assert len(list(ct.search(looping, "2026-10-03", "2026-10-17"))) == 2 and len(looping.calls) == 2
    # an empty page with a token -> stop without asking for more
    empty = FakeHttp([{"studies": [], "nextPageToken": "TOK"}, {"studies": [_study("NCT07000009")]}])
    assert list(ct.search(empty, "2026-10-03")) == [] and len(empty.calls) == 1
    # a failed later page keeps what was already yielded
    broken = FakeHttp([{"studies": [_study("NCT07000001")], "nextPageToken": "TOK"}, None])
    assert len(list(ct.search(broken, "2026-10-03"))) == 1 and len(broken.calls) == 2
    # max_records cap stops mid-page and does not request the next page
    capped = FakeHttp([{"studies": [_study(f"NCT0700000{i}") for i in range(5)], "nextPageToken": "TOK"}])
    assert len(list(ct.search(capped, "2026-10-03", max_records=3))) == 3 and len(capped.calls) == 1


def test_query_building_and_chunking_and_dedupe():
    assert ct.essie_term("Guardant") == "Guardant"
    assert ct.essie_term("circulating tumor DNA") == '"circulating tumor DNA"'
    assert ct.essie_term("Bio-Rad") == '"Bio-Rad"' and ct.essie_term(' say "hi" ') == '"say hi"'
    assert ct.essie_or(["Guardant", "Guardant", "whole genome sequencing", ""]) == 'Guardant OR "whole genome sequencing"'
    assert ct.date_range_filter("2026-10-03") == "AREA[StudyFirstPostDate]RANGE[2026-10-03,MAX]"
    assert ct.phase_label(["PHASE1", "PHASE2"]) == "Phase 1/2" and ct.phase_label(["EARLY_PHASE1"]) == "Early Phase 1"
    assert ct.phase_label(["NA"]) == "N/A" and ct.phase_label("PHASE1") == "" and ct.phase_label([]) == ""
    # 41 sponsor terms -> two sponsor queries; one intervention query; the same NCT across queries is yielded once
    sponsors = [f"Company {i:02d}" for i in range(41)]
    http = FakeHttp([
        {"studies": [_study("NCT07000001")]},
        {"studies": [_study("NCT07000002")]},
        {"studies": [_study("NCT07000001"), _study("NCT07000003")]},
    ])
    items = list(ct.new_studies(http, "2026-10-03", "2026-10-17", sponsor_terms=sponsors, intervention_terms=["Signatera"]))
    assert [i.id for i in items] == ["ctgov:NCT07000001", "ctgov:NCT07000002", "ctgov:NCT07000003"]
    assert len(http.calls) == 3
    assert http.calls[0][1]["query.spons"].count(" OR ") == 39 and http.calls[1][1]["query.spons"] == '"Company 40"'
    assert http.calls[2][1]["query.intr"] == "Signatera" and "query.spons" not in http.calls[2][1]


def test_summary_keeps_registry_spelling_without_double_periods():
    s = _study("NCT07000005", sponsor="Example Pharmaceutical Group Co., Ltd.", collaborators=["Example Health, Inc."],
               interventions=[("DRUG", "Aumolertinib")], conditions=["NSCLC"])
    item = ct.to_news_item(s)
    assert item.summary.startswith("Sponsor: Example Pharmaceutical Group Co., Ltd. (Industry). Collaborators: Example Health, Inc. Interventions: Drug: Aumolertinib. ")
    assert ".." not in item.summary
    # the gazetteer matches on the raw registry spelling, so names are never abbreviated or re-cased
    assert "Example Pharmaceutical Group Co., Ltd." in item.summary and "Example Health, Inc." in item.summary


def test_count_helper():
    http = FakeHttp([{"totalCount": 1754, "studies": [{}]}])
    assert ct.count(http, "2026-10-03", "2026-10-17") == 1754
    assert http.calls[0][1]["pageSize"] == 1 and http.calls[0][1]["fields"] == "NCTId"
    assert ct.count(FakeHttp([None]), "2026-10-03") == 0
    assert ct.count(FakeHttp([{"totalCount": "many"}]), "2026-10-03") == 0
