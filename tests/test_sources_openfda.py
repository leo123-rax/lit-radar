"""openFDA device adapter tested against canned payloads (no network)."""
import logging

import pytest

from litradar.sources import openfda

NOT_JSON = object()


class FakeResp:
    """The two attributes of requests.Response the adapter reads."""

    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is NOT_JSON:
            raise ValueError("not json")
        return self._body


NO_MATCH = FakeResp(404, {"error": {"code": "NOT_FOUND", "message": "No matches found!"}})  # openFDA's empty-window answer


class FakeHttp:
    """Mimics Http.get: a canned payload is served as a 200 response, a FakeResp as itself, None as a failed request."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.kwargs = []

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,)):
        self.calls.append((url, params))
        self.kwargs.append({"headers": headers, "cache": cache, "ok_statuses": ok_statuses})
        if not self.payloads:
            return None
        payload = self.payloads.pop(0)
        if payload is None or isinstance(payload, FakeResp):
            return payload
        return FakeResp(200, payload)


def _meta(total, skip=0, limit=1000):
    return {"last_updated": "2026-10-05", "results": {"skip": skip, "limit": limit, "total": total}}


K_SESE = {
    "k_number": "K254179", "applicant": "Canyon Medical, Inc.", "device_name": "Drainage Catheter",
    "decision_date": "2026-09-18", "decision_code": "SESE", "decision_description": "Substantially Equivalent",
    "product_code": "FGE", "advisory_committee": "GU", "advisory_committee_description": "Gastroenterology, Urology",
    "review_advisory_committee": "GU", "clearance_type": "Traditional", "statement_or_summary": "Summary",
    "third_party_flag": "N", "expedited_review_flag": "", "date_received": "2025-12-23",
    "city": "Nanjing", "state": "", "country_code": "CN", "zip_code": "210032",
    "openfda": {"device_name": "Stents, Drains And Dilators For The Biliary Ducts", "regulation_number": "876.5010",
                "device_class": "2", "medical_specialty_description": "Gastroenterology, Urology",
                "registration_number": ["1319211"], "fei_number": ["1319211"]},
}
K_DENG = {
    "k_number": "DEN260007", "applicant": "BridgeSource Medical Corporation",
    "device_name": "SmartRead(TM) Medical Device Identifier (4100)", "decision_date": "2026-09-29",
    "decision_code": "DENG", "decision_description": "Unknown", "product_code": "SJJ", "advisory_committee": "",
    "advisory_committee_description": "Unknown", "review_advisory_committee": "CV", "clearance_type": "Direct",
    "statement_or_summary": "", "third_party_flag": "N", "city": "Austin", "state": "TX", "country_code": "US",
    "date_received": "2026-03-24",
}
K_IVD = {
    "k_number": "K262168", "applicant": "QIAGEN GmbH", "device_name": "QIAstat-Dx BCID GN Plus AMR Panel",
    "decision_date": "20260924", "decision_code": "SESE", "product_code": "PEN", "advisory_committee": "MI",
    "advisory_committee_description": "Microbiology", "third_party_flag": "Y", "expedited_review_flag": "Y",
}
P_SUPP = {
    "pma_number": "P200002", "supplement_number": "S015", "applicant": "AtriCure, Inc.", "city": "Mason", "state": "OH",
    "generic_name": "Surgical cardiac ablation device, for treatment of atrial fibrillation",
    "trade_name": "EPi- Sense ST Coagulation System", "product_code": "OCM", "advisory_committee": "CV",
    "advisory_committee_description": "Cardiovascular", "supplement_type": "30-Day Notice",
    "supplement_reason": "Process Change - Manufacturer/Sterilizer/Packager/Supplier", "expedited_review_flag": "N",
    "date_received": "2026-08-25", "decision_date": "2026-09-24", "docket_number": "", "decision_code": "OK30",
    "ao_statement": "an alternate sterilization cycle",
    "openfda": {"device_name": "Surgical Cardiac Ablation Device, For Treatment Of Atrial Fibrillation",
                "medical_specialty_description": "Unknown", "regulation_number": "", "device_class": "3"},
}
P_ORIG = {
    "pma_number": "P250050", "supplement_number": "", "applicant": "Bard Peripheral Vascular, Inc.",
    "trade_name": "Liverty\x99 TIPS Stent Graft", "generic_name": "Stent graft", "product_code": "QXZ",
    "advisory_committee": "CV", "advisory_committee_description": "Cardiovascular", "supplement_type": "",
    "decision_code": "APPR", "decision_date": "2026-10-01", "openfda": {"device_class": "3"},
}
P_CDX = {
    "pma_number": "P200010", "supplement_number": "S029", "applicant": "Guardant Health, Inc.",
    "trade_name": "Guardant360 CDx", "product_code": "PQP", "advisory_committee": "PA",
    "advisory_committee_description": "Pathology", "supplement_type": "Real-Time Process",
    "decision_code": "APPR", "decision_date": "2026-09-18",
}


def test_date_range_query_forms():
    assert openfda.date_range_query("2026-09-17", "2026-10-17") == "decision_date:[20260917 TO 20261017]"
    assert openfda.date_range_query("20260917", "20261017", 'applicant:"Roche"') == 'decision_date:[20260917 TO 20261017] AND applicant:"Roche"'
    with pytest.raises(ValueError):
        openfda.date_range_query("2026-9-17", "2026-10-17")


def test_510k_normal_payload_yields_510k_and_denovo_items(monkeypatch):
    monkeypatch.delenv("OPENFDA_API_KEY", raising=False)
    http = FakeHttp([{"meta": _meta(3), "results": [K_SESE, K_DENG, K_IVD]}])
    items = list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",)))
    assert [i.feed for i in items] == ["fda-510k", "fda-denovo", "fda-510k"]
    assert [i.id for i in items] == ["openfda:K254179", "openfda:DEN260007", "openfda:K262168"]
    a, d, q = items
    assert a.title == "Drainage Catheter — Canyon Medical, Inc."
    assert a.url == "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K254179"
    assert a.published == "2026-09-18"
    for piece in ("510(k) K254179", "product code FGE (Stents, Drains And Dilators For The Biliary Ducts)",
                  "regulation 876.5010", "class 2", "advisory committee GU (Gastroenterology, Urology)",
                  "decision SESE (Substantially Equivalent) 2026-09-18", "clearance type Traditional",
                  "applicant Canyon Medical, Inc., Nanjing, CN", "received 2025-12-23", "510(k) summary available"):
        assert piece in a.summary, piece
    assert d.url == "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/denovo.cfm?ID=DEN260007"
    assert "De Novo DEN260007" in d.summary and "decision DENG (De Novo granted) 2026-09-29" in d.summary
    assert "Unknown" not in d.summary and "clearance type Direct" in d.summary
    assert "review advisory committee CV" in d.summary  # advisory_committee is blank on De Novo rows
    assert q.published == "2026-09-24"  # compact date normalised
    assert "advisory committee MI (Microbiology)" in q.summary and "third-party review" in q.summary and "expedited review" in q.summary
    # one request, anonymous, with the documented parameters; 404 is an expected answer, pages are 250 records
    assert len(http.calls) == 1
    url, params = http.calls[0]
    assert url == "https://api.fda.gov/device/510k.json"
    assert params == {"search": "decision_date:[20260917 TO 20261017]", "limit": 250, "skip": 0}
    assert http.kwargs[0] == {"headers": {"Accept": "application/json"}, "cache": False, "ok_statuses": (200, 404)}


def test_pma_normal_payload_original_and_supplement(monkeypatch):
    monkeypatch.delenv("OPENFDA_API_KEY", raising=False)
    http = FakeHttp([{"meta": _meta(2), "results": [P_SUPP, P_ORIG]}])
    items = list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("pma",)))
    assert [i.feed for i in items] == ["fda-pma", "fda-pma"]
    s, o = items
    assert s.id == "openfda:P200002S015" and s.url.endswith("cfpma/pma.cfm?id=P200002S015")
    assert s.title == "EPi- Sense ST Coagulation System — AtriCure, Inc."
    assert "PMA P200002" in s.summary and "supplement S015: 30-Day Notice — Process Change - Manufacturer/Sterilizer/Packager/Supplier" in s.summary
    assert "decision OK30 (30-day notice accepted) 2026-09-24" in s.summary and "AO statement: an alternate sterilization cycle" in s.summary
    assert "product code OCM (Surgical Cardiac Ablation Device, For Treatment Of Atrial Fibrillation)" in s.summary
    assert "generic name Surgical cardiac ablation device, for treatment of atrial fibrillation" in s.summary
    assert "applicant AtriCure, Inc., Mason, OH" in s.summary
    assert o.id == "openfda:P250050" and o.url.endswith("cfpma/pma.cfm?id=P250050")
    assert o.title == "Liverty TIPS Stent Graft — Bard Peripheral Vascular, Inc."  # control character stripped
    assert "PMA P250050 (original approval)" in o.summary and "decision APPR (approved) 2026-10-01" in o.summary
    assert http.calls[0][0] == "https://api.fda.gov/device/pma.json"


def test_empty_payloads_yield_nothing():
    # HTTP 404 "No matches found!" is openFDA's answer for an empty window
    http = FakeHttp([NO_MATCH, NO_MATCH])
    assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", api_key="")) == []
    assert len(http.calls) == 2  # one per endpoint, no retry paging
    http = FakeHttp([{"meta": _meta(0), "results": []}])
    assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="")) == []
    assert len(http.calls) == 1


def test_no_match_is_info_but_failed_request_is_warning(caplog):
    logger = "litradar.sources.openfda"
    with caplog.at_level(logging.INFO, logger=logger):
        http = FakeHttp([NO_MATCH])
        assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="")) == []
    assert any(r.levelno == logging.INFO and "no matches" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger):
        http = FakeHttp([None])  # network error / 5xx after retries / 429 host block: the client returns None
        assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="")) == []
    assert any(r.levelno == logging.WARNING and "request failed" in r.getMessage() for r in caplog.records)
    assert not any("no matches" in r.getMessage() for r in caplog.records)


def test_non_json_and_bad_request_answers_are_warnings(caplog):
    logger = "litradar.sources.openfda"
    with caplog.at_level(logging.INFO, logger=logger):
        http = FakeHttp([FakeResp(200, NOT_JSON)])
        assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="")) == []
    assert any(r.levelno == logging.WARNING and "non-JSON" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger):
        http = FakeHttp([FakeResp(400, {"error": {"code": "BAD_REQUEST", "message": "Skip value must 25000 or less."}})])
        assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="")) == []
    assert any(r.levelno == logging.WARNING and "Skip value must 25000 or less." in r.getMessage() for r in caplog.records)
    http = FakeHttp([FakeResp(400, NOT_JSON)])  # non-JSON error body does not raise either
    assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="")) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"code": "NOT_FOUND", "message": "No matches found!"}},
        {"foo": "bar"},
        {"meta": _meta(1), "results": "not-a-list"},
        {"meta": _meta(1), "results": {"k_number": "K1"}},
        ["a", "list"],
        "a string",
        42,
    ],
)
def test_malformed_payloads_do_not_raise(payload):
    http = FakeHttp([payload])
    assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="")) == []
    assert len(http.calls) == 1


def test_malformed_records_are_skipped():
    recs = [None, "str", {"applicant": "No number"}, {"k_number": "", "device_name": "blank"}, {"k_number": "K9", "openfda": "bad"}, K_SESE]
    http = FakeHttp([{"meta": {"results": {"total": "6"}}, "results": recs}])  # non-int total tolerated
    items = list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key=""))
    assert [i.id for i in items] == ["openfda:K9", "openfda:K254179"]
    assert items[0].title == "(unnamed device)" and items[0].published == ""
    http = FakeHttp([{"meta": _meta(2), "results": [{"supplement_number": "S1"}, {"pma_number": "P1", "openfda": []}]}])
    items = list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("pma",), api_key=""))
    assert [i.id for i in items] == ["openfda:P1"]


def _page(prefix, start, n, total):
    return {"meta": _meta(total, skip=start), "results": [{"k_number": f"{prefix}{i}", "device_name": "D", "applicant": "A"} for i in range(start, start + n)]}


def test_pagination_stops_at_reported_total():
    pages = [_page("K", 0, 1000, 2500), _page("K", 1000, 1000, 2500), _page("K", 2000, 500, 2500), _page("K", 2500, 1000, 2500)]
    http = FakeHttp(pages)
    items = list(openfda.search(http, "510k", "q"))
    assert len(items) == 2500 and items[-1]["k_number"] == "K2499"
    assert [p["skip"] for _, p in http.calls] == [0, 1000, 2000]  # 4th canned page never requested
    assert all(p["limit"] == 1000 for _, p in http.calls)


def test_pagination_stops_on_short_page_without_total():
    pages = [{"results": [{"k_number": "K1"}, {"k_number": "K2"}]}, {"results": [{"k_number": "K3"}]}, {"results": [{"k_number": "K4"}]}]
    http = FakeHttp(pages)
    items = list(openfda.search(http, "510k", "q", page_size=2))
    assert [i["k_number"] for i in items] == ["K1", "K2", "K3"]
    assert len(http.calls) == 2


def test_pagination_stops_when_a_page_fails_midway(caplog):
    with caplog.at_level(logging.WARNING, logger="litradar.sources.openfda"):
        http = FakeHttp([_page("K", 0, 1000, 3000), None, _page("K", 2000, 1000, 3000)])
        items = list(openfda.search(http, "510k", "q"))
    assert len(items) == 1000 and len(http.calls) == 2
    assert any("skip=1000 failed" in r.getMessage() for r in caplog.records)
    http = FakeHttp([_page("K", 0, 1000, 3000), NO_MATCH, _page("K", 2000, 1000, 3000)])  # 404 mid-run also stops
    assert len(list(openfda.search(http, "510k", "q"))) == 1000 and len(http.calls) == 2


def test_pagination_respects_skip_cap_and_max_records():
    pages = [_page("K", s, 1000, 100000) for s in range(0, 30000, 1000)]
    http = FakeHttp(pages)
    items = list(openfda.search(http, "510k", "q"))
    assert len(items) == 26000  # skip 0..25000 inclusive
    assert http.calls[-1][1]["skip"] == 25000
    http = FakeHttp([_page("K", s, 1000, 100000) for s in range(0, 10000, 1000)])
    assert len(list(openfda.search(http, "510k", "q", max_records=2500))) == 3000  # whole pages, stops once the cap is passed
    assert len(http.calls) == 3


def test_page_size_is_clamped_to_openfda_maximum():
    http = FakeHttp([{"meta": _meta(1), "results": [K_SESE]}])
    list(openfda.search(http, "510k", "q", page_size=5000))
    assert http.calls[0][1]["limit"] == 1000
    http = FakeHttp([{"meta": _meta(1), "results": [K_SESE]}])
    list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="", page_size=1000))
    assert http.calls[0][1]["limit"] == 1000  # fetch() default is 250 but callers may raise it
    assert openfda.DEFAULT_PAGE_SIZE == 250 and openfda.RECOMMENDED_LOOKBACK_DAYS >= 45


def test_api_key_from_env_is_sent_first(monkeypatch):
    monkeypatch.setenv("OPENFDA_API_KEY", " secret-key ")
    assert openfda.api_key_from_env() == "secret-key"
    http = FakeHttp([{"meta": _meta(1), "results": [K_SESE]}])
    list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",)))
    params = http.calls[0][1]
    assert list(params) == ["api_key", "search", "limit", "skip"] and params["api_key"] == "secret-key"
    # explicit empty key overrides the environment
    http = FakeHttp([{"meta": _meta(1), "results": [K_SESE]}])
    list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key=""))
    assert "api_key" not in http.calls[0][1]


def test_filters_advisory_committee_and_supplements():
    http = FakeHttp([{"meta": _meta(3), "results": [K_SESE, K_DENG, K_IVD]}, {"meta": _meta(3), "results": [P_SUPP, P_ORIG, P_CDX]}])
    items = list(openfda.fetch(http, "2026-09-17", "2026-10-17", api_key="", advisory_committees=openfda.IVD_ADVISORY_COMMITTEES))
    assert [i.id for i in items] == ["openfda:K262168", "openfda:P200010S029"]
    http = FakeHttp([{"meta": _meta(3), "results": [P_SUPP, P_ORIG, P_CDX]}])
    items = list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("pma",), api_key="", exclude_supplement_types=["30-Day Notice"]))
    assert [i.id for i in items] == ["openfda:P250050", "openfda:P200010S029"]
    http = FakeHttp([{"meta": _meta(3), "results": [P_SUPP, P_ORIG, P_CDX]}])
    items = list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("pma",), api_key="", include_supplements=False))
    assert [i.id for i in items] == ["openfda:P250050"]


def test_denovo_review_committee_counts_for_the_filter():
    # Live De Novo rows have advisory_committee == "" and only review_advisory_committee set
    den_ivd = dict(K_DENG, k_number="DEN260008", review_advisory_committee="MI")
    http = FakeHttp([{"meta": _meta(2), "results": [K_DENG, den_ivd]}])
    items = list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="", advisory_committees=openfda.IVD_ADVISORY_COMMITTEES))
    assert [i.id for i in items] == ["openfda:DEN260008"]  # the CV-reviewed De Novo is still excluded
    assert items[0].feed == "fda-denovo" and "review advisory committee MI" in items[0].summary
    # the top-level code wins when both are present
    both = dict(K_DENG, k_number="DEN260009", advisory_committee="CV", review_advisory_committee="MI")
    http = FakeHttp([{"meta": _meta(1), "results": [both]}])
    assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("510k",), api_key="", advisory_committees=["MI"])) == []


def test_unknown_kind_is_skipped_without_requests():
    http = FakeHttp([])
    assert list(openfda.fetch(http, "2026-09-17", "2026-10-17", kinds=("denovo",), api_key="")) == []
    assert http.calls == []


def test_applicant_counts():
    http = FakeHttp([{"meta": _meta(3), "results": [K_SESE]}, NO_MATCH, {"meta": _meta(1), "results": [P_CDX]}, {"error": {"code": "NOT_FOUND"}}])
    out = openfda.applicant_counts(http, "2026-09-17", "2026-10-17", ["Roche", 'Gu"ardant'], api_key="")
    assert out == {"510k": {"Roche": 3, 'Gu"ardant': 0}, "pma": {"Roche": 1, 'Gu"ardant': 0}}
    assert http.calls[0][1]["search"] == 'decision_date:[20260917 TO 20261017] AND applicant:"Roche"'
    assert http.calls[1][1]["search"].endswith('applicant:"Guardant"') and http.calls[0][1]["limit"] == 1
    assert all(k["ok_statuses"] == (200, 404) for k in http.kwargs)


def test_applicant_counts_accepts_a_generator_and_marks_failed_lookups():
    http = FakeHttp([{"meta": _meta(3), "results": [K_SESE]}, NO_MATCH, {"meta": _meta(1), "results": [P_CDX]}, None])
    names = (n for n in ["Roche", "Guardant"])  # one-shot iterable must be consumed for every kind
    out = openfda.applicant_counts(http, "2026-09-17", "2026-10-17", names, api_key="")
    assert out == {"510k": {"Roche": 3, "Guardant": 0}, "pma": {"Roche": 1, "Guardant": None}}  # 0 = no matches, None = request failed
    assert len(http.calls) == 4
