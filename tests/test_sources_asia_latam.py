"""Offline tests for the Japan / Korea / Latin America adapters (J-STAGE, KCI, SciELO): canned payloads, no network."""
import json
from pathlib import Path

import pytest

from litradar.sources import jstage, kci, scielo
from litradar.sources.script_lang import detect_script_lang

FIX = Path(__file__).parent / "fixtures"


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.min_interval = {}

    def get_json(self, url, params=None, cache=False):
        self.calls.append((url, dict(params or {})))
        return self.payloads.pop(0) if self.payloads else None

    def get_text(self, url, params=None, cache=False):
        self.calls.append((url, dict(params or {})))
        return self.payloads.pop(0) if self.payloads else None


# ---------------------------------------------------------------------------------------- script_lang
def test_detect_script_lang():
    assert detect_script_lang("전장유전체 시퀀싱") == "ko"
    assert detect_script_lang("ゲノム解析") == "ja"
    assert detect_script_lang("基因组测序") == "zh"
    assert detect_script_lang("基因組", han="ja") == "ja"
    assert detect_script_lang("Genome sequencing") == "en"
    assert detect_script_lang("", default="pt") == "pt"


# ---------------------------------------------------------------------------------------- J-STAGE
def _jstage_feed(status, total, entries_xml, start=1):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xml:lang="ja">
  <result><status>{status}</status><message>{'' if status == '0' else status}</message></result>
  <title>Articles</title><servicecd>3</servicecd>
  <opensearch:totalResults>{total if total is not None else ''}</opensearch:totalResults>
  <opensearch:startIndex>{start}</opensearch:startIndex>
  <opensearch:itemsPerPage>{entries_xml.count('<entry>')}</opensearch:itemsPerPage>
  {entries_xml}
</feed>"""


def _jstage_entry(n, updated, doi=True):
    return f"""<entry>
  <article_title><en><![CDATA[Paper {n} on <i>genome</i> biology]]></en><ja><![CDATA[ゲノム論文{n}]]></ja></article_title>
  <article_link><en>https://www.jstage.jst.go.jp/article/exj/1/1/1_{n}/_article</en><ja>https://www.jstage.jst.go.jp/article/exj/1/1/1_{n}/_article/-char/ja/</ja></article_link>
  <author><en><name><![CDATA[Author {n}]]></name></en><ja><name><![CDATA[著者{n}]]></name></ja></author>
  <cdjournal>exj</cdjournal><material_title><en><![CDATA[Example Journal]]></en><ja><![CDATA[例誌]]></ja></material_title>
  <prism:issn>1111-1111</prism:issn><prism:eIssn>2222-2222</prism:eIssn><prism:volume>1</prism:volume><prism:number>1</prism:number>
  <prism:startingPage>{n}</prism:startingPage><pubyear>2026</pubyear><prism:doi>{'10.9999/exj.1.' + str(n) if doi else ''}</prism:doi>
  <title><![CDATA[ゲノム論文{n}]]></title><link href="https://www.jstage.jst.go.jp/article/exj/1/1/1_{n}/_article/-char/ja/"/>
  <id>https://www.jstage.jst.go.jp/article/exj/1/1/1_{n}/_article/-char/ja/</id><updated>{updated}</updated>
</entry>"""


def test_jstage_parse_feed_and_entry_to_paper():
    page = jstage.parse_feed((FIX / "jstage_articles.xml").read_text(encoding="utf-8"))
    assert page["status"] == "0" and page["total"] == 2 and page["per_page"] == 2 and len(page["entries"]) == 2
    e = page["entries"][0]
    assert e["title_en"] == "Long-read genome assembly of a Rhodobacter isolate from a hot spring"  # <i> stripped
    assert e["title_ja"] == "温泉由来Rhodobacter株のロングリードゲノムアセンブリ"
    assert e["authors_en"] == ["Hanako SUZUKI", "Taro YAMADA"] and e["authors_ja"] == ["鈴木 花子", "山田 太郎"]
    p = jstage.entry_to_paper(e)
    assert p.id == "doi:10.9999/examplej.70.201" and p.source == "jstage" and p.kind == "article"
    assert p.title.startswith("Long-read genome assembly") and p.extra["title_native"].startswith("温泉由来")
    assert p.extra["lang"] == "ja" and p.extra["country"] == "JP"
    assert p.pub_date == "2026-10-10" and p.url.endswith("/70_201/_article")
    assert p.journal == "Example Journal of Microbiology" and p.extra["issn"] == ["1234-5678", "8765-4321"]
    assert [a.name for a in p.authors] == ["Hanako SUZUKI", "Taro YAMADA"] and p.extra["pages"] == "201-210"
    # second entry: English only, no DOI -> synthetic id from the English link, lang en
    p2 = jstage.entry_to_paper(page["entries"][1])
    assert p2.doi is None and p2.id.startswith("jstage:https://www.jstage.jst.go.jp/article/exampleag/")
    assert p2.extra["lang"] == "en" and p2.extra["title_native"] == p2.title and p2.pub_date == "2025-11-02"


def test_jstage_fetch_filters_window_pages_and_dedupes():
    inside, outside = "2026-10-10T09:00+09:00", "2026-08-01T09:00+09:00"
    page1 = _jstage_feed("WARN_002", 3, _jstage_entry(1, inside) + _jstage_entry(2, outside))
    page2 = _jstage_feed("WARN_002", 3, _jstage_entry(3, inside), start=3)
    # second term returns an entry already seen (same DOI) -> deduped
    page3 = _jstage_feed("0", 1, _jstage_entry(1, inside))
    http = FakeHttp([page1, page2, page3])
    stats = {}
    papers = list(jstage.fetch(http, "2026-09-17", "2026-10-17", terms=["genome", "genomics"], max_records=4, stats=stats))
    assert [p.extra["jstage_updated"][:10] for p in papers] == ["2026-10-10", "2026-10-10"]
    assert {p.id for p in papers} == {"doi:10.9999/exj.1.1", "doi:10.9999/exj.1.3"}
    assert len(http.calls) == 3
    url, params = http.calls[0]
    assert url == jstage.API and params["service"] == 3 and params["abst"] == "genome"
    assert params["pubyearfrom"] == "2025" and params["pubyearto"] == "2026" and params["start"] == 1 and params["count"] == 4
    assert http.calls[1][1]["start"] == 3 and http.calls[1][1]["count"] == 2  # start advanced by the page size, count = remaining
    assert http.calls[2][1]["abst"] == "genomics"
    assert stats == {"queries": 2, "entries": 4, "in_window": 3, "papers": 2}
    assert http.min_interval["api.jstage.jst.go.jp"] == jstage.MIN_INTERVAL


def test_jstage_empty_malformed_and_error_statuses():
    assert list(jstage.search(FakeHttp([_jstage_feed("ERR_001", None, "")]), {"abst": "zzz"})) == []
    assert list(jstage.search(FakeHttp(["<feed>not closed"]), {"abst": "zzz"})) == []
    assert list(jstage.search(FakeHttp([None]), {"abst": "zzz"})) == []
    assert jstage.parse_feed(None)["status"] == "HTTP_ERROR" and jstage.parse_feed("<x")["status"] == "PARSE_ERROR"
    http = FakeHttp([_jstage_feed("ERR_003", None, "")])  # concurrent-access limit -> stop, no crash
    assert list(jstage.fetch(http, "2026-09-17", "2026-10-17", terms=["genome"])) == [] and len(http.calls) == 1
    assert jstage.hit_count(FakeHttp([_jstage_feed("WARN_002", 1312, _jstage_entry(1, "2026-10-10T09:00+09:00"))]), {"text": "genome"}) == 1312
    assert jstage.hit_count(FakeHttp([_jstage_feed("ERR_001", None, "")]), {"text": "zzz"}) == 0
    with pytest.raises(ValueError):
        list(jstage.fetch(FakeHttp([]), "2026-09-17", "2026-10-17", terms=["x"], field="sortflg"))


def test_jstage_search_respects_max_records_and_total():
    # total says 100 but max_records=2 -> one page, no second request
    http = FakeHttp([_jstage_feed("WARN_002", 100, _jstage_entry(1, "2026-10-10T09:00+09:00") + _jstage_entry(2, "2026-10-10T09:00+09:00"))])
    assert len(list(jstage.search(http, {"abst": "genome"}, max_records=2))) == 2 and len(http.calls) == 1
    # a page shorter than requested with no total -> stop
    http = FakeHttp([_jstage_feed("0", None, _jstage_entry(1, "2026-10-10T09:00+09:00"))])
    assert len(list(jstage.search(http, {"abst": "genome"}, max_records=50))) == 1 and len(http.calls) == 1


# ---------------------------------------------------------------------------------------- KCI
def test_kci_parse_page_and_record_to_paper():
    page = kci.parse_page((FIX / "kci_oai_kci.xml").read_text(encoding="utf-8"))
    assert page["error"] == "" and page["token"] == "2026-09-17:2026-10-17:100:168917" and len(page["records"]) == 2
    r = page["records"][0]
    assert r["article_id"] == "ART009000001" and r["pub_year"] == "2026" and r["pub_mon"] == "09" and r["categories"] == ["의학"]
    assert r["authors"] == [{"name": "김민준", "affiliation": "예시대학교"}, {"name": "이서연", "affiliation": "예시병원"}]
    p = kci.record_to_paper(r)
    assert p.id == "doi:10.99999/ejgm.2026.12.3.101" and p.source == "kci" and p.pub_date == "2026-09-01"
    assert p.title == "Whole-genome sequencing for rare disease diagnosis" and p.extra["title_native"].startswith("전장유전체")
    assert p.extra["lang"] == "ko" and p.extra["country"] == "KR" and p.cited_by_count == 3 and p.topics == ["의학"]
    assert p.abstract.startswith("We evaluated") and p.affiliations == ["예시대학교", "예시병원"]
    assert p.journal == "Example Journal of Genomic Medicine" and p.extra["issn"] == ["1111-2222", "3333-4444"]
    assert p.url.startswith("https://www.kci.go.kr/") and p.extra["pages"] == "101-110"
    p2 = kci.record_to_paper(page["records"][1])
    assert p2.doi is None and p2.id == "kci:ART009000002" and p2.title == "근대 소설의 서술 구조" and p2.pub_date == "2014-06-01"


def test_kci_oai_dc_records_are_parsed_too():
    xml = """<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords><record>
      <header><identifier>oai:kci.go.kr:ARTI/1</identifier><datestamp>2026-10-05</datestamp><setSpec>ARTI</setSpec></header>
      <metadata><oai_dc:dc xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/">
        <dc:title lang="original">유전체 연구</dc:title><dc:title lang="english">Genome study</dc:title>
        <dc:creator>김민준(예시대학교); 이서연(예시병원)</dc:creator><dc:subject>생물학</dc:subject>
        <dc:identifier eissn="3333-4444" issn="1111-2222" type="journalInfo">Example Journal, 12(3), , pp.1-9</dc:identifier>
        <dc:identifier type="artiId">ART000000001</dc:identifier><dc:identifier type="citedCnt">7</dc:identifier>
        <dc:identifier type="doi">http://dx.doi.org/10.99999/ex.2026.1</dc:identifier><dc:publisher>예시학회</dc:publisher>
        <dc:date>2026-09</dc:date><dc:type>Article</dc:type><dc:url>https://www.kci.go.kr/x?artiId=ART000000001</dc:url>
      </oai_dc:dc></metadata></record></ListRecords></OAI-PMH>"""
    page = kci.parse_page(xml)
    r = page["records"][0]
    assert r["title_english"] == "Genome study" and r["journal"] == "Example Journal" and r["pissn"] == "1111-2222"
    assert r["authors"][1] == {"name": "이서연", "affiliation": "예시병원"} and r["categories"] == ["생물학"] and r["citation_count"] == "7"
    p = kci.record_to_paper(r)
    assert p.id == "doi:10.99999/ex.2026.1" and p.pub_date == "2026-09-01" and p.cited_by_count == 7 and page["token"] == ""


def test_kci_list_records_stops_on_repeated_page_or_token():
    xml = (FIX / "kci_oai_kci.xml").read_text(encoding="utf-8")
    http = FakeHttp([xml, xml, xml])  # a server that keeps returning the same page must not loop the harvest
    recs = list(kci.list_records(http, "2026-09-17", "2026-10-17", max_pages=10))
    assert [r["identifier"] for r in recs] == ["oai:kci.go.kr:ARTI/900001", "oai:kci.go.kr:ARTI/900002"]
    assert len(http.calls) == 2
    first, second = http.calls[0][1], http.calls[1][1]
    assert first == {"verb": "ListRecords", "set": "ARTI", "metadataPrefix": "oai_kci", "from": "2026-09-17", "until": "2026-10-17"}
    assert second == {"verb": "ListRecords", "set": "ARTI", "metadataPrefix": "oai_kci", "resumptionToken": "2026-09-17:2026-10-17:100:168917"}
    # a page without a token ends the harvest after one request
    no_token = xml.replace("<resumptionToken>2026-09-17:2026-10-17:100:168917</resumptionToken>", "")
    http = FakeHttp([no_token, xml])
    assert len(list(kci.list_records(http, "2026-09-17", "2026-10-17"))) == 2 and len(http.calls) == 1
    # max_pages caps requests even when tokens differ
    xml2 = xml.replace("ARTI/900001", "ARTI/900011").replace("ARTI/900002", "ARTI/900012").replace(":100:", ":200:")
    http = FakeHttp([xml, xml2, xml])
    assert len(list(kci.list_records(http, "2026-09-17", "2026-10-17", max_pages=2))) == 4 and len(http.calls) == 2


def test_kci_errors_and_malformed_payloads():
    err = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="noRecordsMatch">The combination of the values of arguments results in an empty set</error></OAI-PMH>'
    page = kci.parse_page(err)
    assert page["error_code"] == "noRecordsMatch" and page["records"] == []
    assert list(kci.list_records(FakeHttp([err]), "2013-01-01", "2013-01-02")) == []
    bad = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="badResumptionToken">Not valid resumptionToken string</error></OAI-PMH>'
    assert list(kci.list_records(FakeHttp([bad]), "2026-09-17", "2026-10-17")) == []
    assert kci.parse_page("<OAI-PMH><broken")["error_code"] == "PARSE_ERROR"
    assert kci.parse_page(None)["error_code"] == "HTTP_ERROR"
    assert list(kci.fetch(FakeHttp([None]), "2026-09-17", "2026-10-17")) == []


def test_kci_fetch_filters_year_category_and_keywords():
    xml = (FIX / "kci_oai_kci.xml").read_text(encoding="utf-8")
    stats = {}
    papers = list(kci.fetch(FakeHttp([xml, xml]), "2026-09-17", "2026-10-17", stats=stats))
    assert [p.extra["kci_article_id"] for p in papers] == ["ART009000001"]  # the 2014 record is dropped (min_pub_year = 2025)
    assert stats == {"records": 2, "kept": 1}
    assert len(list(kci.fetch(FakeHttp([xml, xml]), "2026-09-17", "2026-10-17", min_pub_year=2000))) == 2
    assert len(list(kci.fetch(FakeHttp([xml, xml]), "2026-09-17", "2026-10-17", min_pub_year=2000, categories=["한국어와문학"]))) == 1
    assert len(list(kci.fetch(FakeHttp([xml, xml]), "2026-09-17", "2026-10-17", keywords=["genome"]))) == 1
    assert len(list(kci.fetch(FakeHttp([xml, xml]), "2026-09-17", "2026-10-17", keywords=["proteom"]))) == 0
    assert len(list(kci.fetch(FakeHttp([xml, xml]), "2026-09-17", "2026-10-17", keywords=["전장유전체"]))) == 1  # native-script keyword


# ---------------------------------------------------------------------------------------- SciELO
def _scielo_doc():
    return json.loads((FIX / "scielo_article_opac.json").read_text(encoding="utf-8"))


def test_scielo_to_paper():
    p = scielo.to_paper(_scielo_doc())
    assert p.id == "doi:10.1590/1234-5678v10e20260001" and p.source == "scielo" and p.kind == "article"
    assert p.title == "Whole-genome sequencing of clinical isolates" and p.extra["title_native"].startswith("Sequenciamento")
    assert p.extra["lang"] == "pt" and p.extra["country"] == "BR" and p.extra["collection"] == "scl"
    assert p.pub_date == "2026-06-22" and p.extra["pub_date_basis"] == "online" and p.extra["processing_date"] == "2026-09-30"
    assert p.abstract.startswith("ABSTRACT: We sequenced") and p.extra["abstract_langs"] == ["en", "pt"]
    assert p.journal == "Example Journal of Genomics" and p.extra["issn"] == ["1234-5678", "8765-4321"]
    assert p.authors[0].name == "Ana Beatriz Silva" and p.authors[0].orcid == "0000-0001-0000-0001"
    assert p.authors[0].affiliation == "Example Federal University, Department of Genetics, Example City, SP, Brazil"
    assert p.authors[1].affiliation.count(";") == 1  # two affiliations resolved from 'aff1 aff2'
    assert len(p.affiliations) == 2 and p.sections["funding"] == "Example National Research Council; Example State Foundation"
    assert p.sections["ack"].startswith("The authors thank") and p.topics == ["Biological Sciences", "GENETICS & HEREDITY"]
    assert p.extra["keywords"] == ["sequenciamento", "whole-genome sequencing"] and p.url == "https://doi.org/10.1590/1234-5678v10e20260001"
    assert p.category == "research-article" and p.extra["scielo_pid"] == "S1234-56782026000100001"


def test_scielo_pub_date_fallbacks_and_url_without_doi():
    doc = _scielo_doc()
    doc["doi"] = None
    doc["article"].pop("v237")
    doc["article"].pop("v223")
    doc["article"]["v265"] = []
    assert scielo.pub_date_of(doc) == ("2026-09-30", "processing")  # v65 20260000 has no month -> processing date
    doc["article"]["v65"] = [{"_": "20260500"}]
    assert scielo.pub_date_of(doc) == ("2026-05-01", "issue")
    doc["article"]["v65"] = [{"_": "20260514"}]
    assert scielo.pub_date_of(doc) == ("2026-05-14", "issue")
    p = scielo.to_paper(doc)
    assert p.doi is None and p.id == "scielo:S1234-56782026000100001" and p.url.startswith("http://www.scielo.br/scielo.php?script=sci_arttext")
    doc["fulltexts"] = {}
    assert scielo.to_paper(doc).url == "https://www.scielo.br/scielo.php?script=sci_arttext&pid=S1234-56782026000100001"
    doc["processing_date"] = ""
    doc["article"]["v65"] = [{"_": "20260000"}]
    doc["article"]["v91"] = []
    assert scielo.pub_date_of(doc) == ("2026-01-01", "year")
    assert scielo.issn_from_code("S2317-15372026000101013") == "2317-1537" and scielo.issn_from_code("garbage") == ""


def _ids(objs, total, limit=1000, offset=0):
    return {"meta": {"limit": limit, "offset": offset, "filter": {}, "total": total}, "objects": objs}


def test_scielo_identifiers_paging_stops():
    http = FakeHttp([
        _ids([{"code": "S1", "collection": "scl"}, {"code": "S2", "collection": "scl"}], 3),
        _ids([{"code": "S3", "collection": "scl"}], 3, offset=2),
        _ids([{"code": "S4", "collection": "scl"}], 3, offset=3),  # must not be requested
    ])
    out = list(scielo.identifiers(http, "2026-09-17", "2026-10-17", collection="scl"))
    assert [o["code"] for o in out] == ["S1", "S2", "S3"] and len(http.calls) == 2
    assert http.calls[0][1] == {"from": "2026-09-17", "until": "2026-10-17", "limit": 1000, "offset": 0, "collection": "scl"}
    assert http.calls[1][1]["offset"] == 2 and http.calls[1][1]["limit"] == 1000
    assert list(scielo.identifiers(FakeHttp([_ids([], 0)]), "a", "b")) == []  # empty
    assert list(scielo.identifiers(FakeHttp([None]), "a", "b")) == []  # HTTP failure
    assert list(scielo.identifiers(FakeHttp(["not a dict"]), "a", "b")) == []  # malformed
    http = FakeHttp([_ids([{"code": f"S{i}", "collection": "scl"} for i in range(5)], 500)])
    assert len(list(scielo.identifiers(http, "a", "b", max_records=5))) == 5 and len(http.calls) == 1 and http.calls[0][1]["limit"] == 5
    assert "collection" not in http.calls[0][1]


def test_scielo_fetch_subject_prefilter_keywords_and_missing_articles():
    doc = _scielo_doc()
    other = json.loads(json.dumps(doc))
    other["code"] = "S1234-56782026000100002"
    other["doi"] = "10.1590/other"
    other["article"]["v12"] = [{"l": "en", "_": "Soil chemistry of coffee plantations"}]
    other["article"]["v83"] = [{"a": "Nothing about sequencing here.", "l": "en"}]
    other["article"]["v85"] = []
    bio_journal = {"code": "1234-5678", "v441": [{"_": "Biological Sciences"}]}
    hum_journal = {"code": "9999-0000", "v441": [{"_": "Human Sciences"}]}
    http = FakeHttp([
        _ids([
            {"code": "S1234-56782026000100001", "collection": "scl"},
            {"code": "S9999-00002026000100007", "collection": "scl"},  # Human Sciences journal -> skipped before any article fetch
            {"code": "S1234-56782026000100002", "collection": "scl"},  # same journal (cached) but no keyword hit
            {"code": "S1234-56782026000100003", "collection": "scl"},  # article endpoint answers null
        ], 4),
        bio_journal, doc, hum_journal, other, None,
    ])
    stats = {}
    papers = list(scielo.fetch(http, "2026-09-17", "2026-10-17", collections=["scl"], keywords=["genom"], stats=stats))
    assert [p.extra["scielo_pid"] for p in papers] == ["S1234-56782026000100001"]
    assert stats == {"identifiers": 4, "skipped_subject": 1, "fetched": 3, "missing": 1, "kept": 1, "capped": 0}
    urls = [u for u, _ in http.calls]
    assert urls.count(f"{scielo.BASE}/journal/") == 2 and urls.count(f"{scielo.BASE}/article/") == 3
    j_params = [p for u, p in http.calls if u.endswith("/journal/")]
    assert j_params[0] == {"collection": "scl", "issn": "1234-5678"} and j_params[1]["issn"] == "9999-0000"
    a_params = [p for u, p in http.calls if u.endswith("/article/")]
    assert a_params[0] == {"collection": "scl", "code": "S1234-56782026000100001", "format": "opac"}
    assert http.min_interval["articlemeta.scielo.org"] == scielo.MIN_INTERVAL


def test_scielo_fetch_without_subject_filter_and_with_cap():
    doc = _scielo_doc()
    http = FakeHttp([_ids([{"code": "S1234-56782026000100001", "collection": "arg"}, {"code": "S1234-56782026000100002", "collection": "arg"}], 2), doc])
    stats = {}
    papers = list(scielo.fetch(http, "2026-09-17", "2026-10-17", subject_areas=None, max_articles=1, stats=stats))
    assert len(papers) == 1 and stats["capped"] == 1 and stats["fetched"] == 1
    assert all(not u.endswith("/journal/") for u, _ in http.calls)  # no journal lookups when the area filter is off
    assert http.calls[1][1]["collection"] == "arg"
