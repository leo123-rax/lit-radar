"""China coverage adapters tested against canned payloads (no network): GSA search + statistics,
GSA-Human list + run JSON, ChinaXiv OAI-PMH, the snapshot time series and the gazetteer entries."""
import sqlite3

import pytest

from litradar import gsa_snapshots
from litradar.config import Settings
from litradar.extract.gazetteer import Gazetteer
from litradar.sources import chinaxiv, gsa, gsa_human


class FakeHttp:
    """Same shape as tests/test_sources_offline.py: payloads are popped in call order."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url, params=None, cache=False):
        self.calls.append((url, params))
        return self.payloads.pop(0) if self.payloads else None

    def get_text(self, url, params=None, cache=False):
        self.calls.append((url, params))
        return self.payloads.pop(0) if self.payloads else None


# ------------------------------------------------------------------ GSA search page
def _gsa_hit(acc, cra, title, platform, strategy="RNASeq", source="TRANSCRIPTOMIC"):
    return (
        f'<input type="checkbox" class="checkbox_class" data-value="{acc}"/> <span> 1.</span>'
        f'<div class="title_block"><span class="result_title">Title: </span> <span class="result_context">{title}</span></div>'
        f'<div class="result_block_item"><span class="result_title">Accession: </span>'
        f'<a href="https://ngdc.cncb.ac.cn/gsa/browse/{cra}/{acc}" target="_blank"> {acc} </a></div>'
        f'<div class="result_block_item"><span class="result_title">Platform name: </span> <span class="result_context">{platform}</span></div>'
        f'<div class="result_block_item"><span class="result_title">Source name: </span> <span class="result_context">{source}</span></div>'
        f'<div class="result_block_item"><span class="result_title">Strategy name: </span> <span class="result_context">{strategy}</span></div>'
        f'<div class="result_block_item"><span class="result_title">Selection name: </span> <span class="result_context">PCR</span></div>'
    )


def _gsa_page(total, hits, facets=True):
    side = ""
    if facets:
        # mirrors the live sidebar: <a onclick="selectSource('X', 'platform')">X</a> (N)
        side = (
            '<ul class="platform_block"><span class="index_title">Platform</span>'
            '<li class="classification_option"><a onclick="selectSource(\'BGISEQ\', \'platform\')">BGISEQ</a> (11014)</li>'
            '<li class="classification_option"><a onclick="selectSource(\'ILLUMINA\', \'platform\')">ILLUMINA</a> (21891)</li>'
            '<li class="classification_option"><a onclick="selectSource(\'GENEMIND\', \'platform\')">GENEMIND</a> (11)</li></ul>'
            '<ul class="strategy_block"><span class="index_title">Strategy</span><li><a onclick="x">Genome</a> (11203)</li><li><a onclick="x">RNASeq</a> (10380)</li></ul>'
            '<ul class="source_block"><span class="index_title">Source</span><li><a onclick="x">METAGENOMIC</a> (11660)</li></ul>'
            '<ul class="center_block"><span class="index_title">Center</span><li><a onclick="x">NGDC</a> (33480) <span style="color: #0ea432">✔</span></li></ul>'
        )
    return (
        "<html><body>" + side + f'<li class="total" style="color: brown">Total Items:&nbsp;{total}</li>'
        '<div class="result_area">' + "".join(hits) + "</div></body></html>"
    )


def test_gsa_parse_hits_facets_and_total():
    html = _gsa_page(2, [_gsa_hit("CRX2182122", "CRA033661", "HC4-6A", "BGISEQ"), _gsa_hit("CRX2182139", "CRA033661", "PND3_5A", "ILLUMINA", "Genome", "GENOMIC")])
    assert gsa.parse_total(html) == 2
    hits = gsa.parse_hits(html)
    assert [h["accession"] for h in hits] == ["CRX2182122", "CRX2182139"]
    assert hits[0]["study_accession"] == "CRA033661" and hits[0]["platform"] == "BGISEQ" and hits[0]["strategy"] == "RNASeq" and hits[0]["title"] == "HC4-6A"
    assert hits[1]["source"] == "GENOMIC" and hits[1]["selection"] == "PCR"
    facets = gsa.parse_facets(html)
    assert facets["platform"] == {"BGISEQ": 11014, "ILLUMINA": 21891, "GENEMIND": 11}
    assert facets["strategy"]["Genome"] == 11203 and facets["center"]["NGDC"] == 33480
    assert gsa.release_term("2026-09-17", "2026-10-17") == '"09/17/2026 - 10/17/2026"[ReleaseDate]'


def test_gsa_platform_histogram_and_empty_or_malformed():
    http = FakeHttp([_gsa_page(33480, [])])
    h = gsa.platform_histogram(http, "2026-09-17", "2026-10-17")
    assert h["total"] == 33480 and h["platforms"]["BGISEQ"] == 11014 and h["center"] == "NGDC"
    assert http.calls[0][1]["centerSelectedItem"] == "NGDC" and http.calls[0][1]["from"] == 1
    # unavailable page -> zeros, no exception
    assert gsa.platform_histogram(FakeHttp([None]), "2026-10-01", "2026-10-02")["total"] == 0
    # malformed HTML -> nothing parsed
    assert gsa.parse_hits("<html><div class='garbage'>") == [] and gsa.parse_facets("not html") == {} and gsa.parse_total("") == 0
    assert list(gsa.fetch(FakeHttp(["<html>no results here</html>"]), "2026-10-16", "2026-10-16")) == []


def test_gsa_fetch_pages_per_day_and_stops_on_repeat():
    day1_p1 = _gsa_page(3, [_gsa_hit("CRX1", "CRA1", "a", "BGISEQ"), _gsa_hit("CRX2", "CRA1", "b", "ILLUMINA")])
    day1_p2 = _gsa_page(3, [_gsa_hit("CRX3", "CRA2", "c", "GENEMIND", "Genome")])
    day2_p1 = _gsa_page(2, [_gsa_hit("CRX4", "CRA3", "d", "PACBIO_SMRT")])
    day2_p2 = _gsa_page(2, [_gsa_hit("CRX4", "CRA3", "d", "PACBIO_SMRT")])  # server repeats the page -> stop
    http = FakeHttp([day1_p1, day1_p2, day2_p1, day2_p2])
    recs = list(gsa.fetch(http, "2026-10-15", "2026-10-16", page_size=2))
    assert [r.accession for r in recs] == ["CRX1", "CRX2", "CRX3", "CRX4"]
    assert recs[0].archive == "gsa" and recs[0].first_public == "2026-10-15" and recs[3].first_public == "2026-10-16"
    assert recs[0].instrument_model == "BGISEQ (model n/a)" and recs[0].platform == "BGISEQ" and recs[0].study_accession == "CRA1"
    assert recs[2].platform == "GENEMIND" and recs[2].library_strategy == "Genome" and recs[0].center_name == "NGDC"
    # day 1: page 2 was short (1 < page_size) so paging stopped; day 2: total reached after page 1 -> 3 requests total
    assert len(http.calls) == 3
    assert http.calls[0][1]["searchTerm"] == '"10/15/2026 - 10/15/2026"[ReleaseDate]' and http.calls[1][1]["from"] == 2


def test_gsa_fetch_respects_max_records():
    page = _gsa_page(5, [_gsa_hit(f"CRX{i}", "CRA9", "t", "ILLUMINA") for i in range(5)])
    recs = list(gsa.fetch(FakeHttp([page]), "2026-10-16", "2026-10-16", max_records=2))
    assert len(recs) == 2


# ------------------------------------------------------------------ GSA statistics snapshot
STATS = {
    "platforms": {"platformList": [
        {"experiments": 434392, "platformId": 57, "platforms": "Illumina NovaSeq 6000", "runs": 463600},
        {"experiments": 112846, "platformId": 64, "platforms": "DNBSEQ-T7", "runs": 119461},
        {"experiments": 453, "platformId": 79, "platforms": "GenoLab M", "runs": 453},
        {"bad": "row"},
    ], "yearList": ["2025", "2026"]},
    "total": {"archiveCount": 36178, "expCount": 2115280, "runCount": 2143928, "releasedExp": 1233693, "privateExp": 881587,
              "mouthArchiveList": [["2026-09", 1267], ["2026-10", 731], ["junk"]]},
    "article": {"publications": [{"doi": "10.1007/S40820-026-02320-7", "pubmedId": 42530726, "craAccession": "<a href='browse/CRA042678'>CRA042678</a>",
                                  "proAccession": "<a href='https://ngdc.cncb.ac.cn/bioproject/browse/PRJCA063830'>", "journal": "Nano-Micro Letters", "pDate": "2027-12", "articleTitle": "T"}]},
}


def test_gsa_statistics_parsing_and_snapshots():
    stats = gsa.fetch_statistics(FakeHttp([STATS]))
    rows = gsa.platform_snapshot(stats, "2026-10-17")
    assert [(r["platform"], r["experiments"]) for r in rows] == [("Illumina NovaSeq 6000", 434392), ("DNBSEQ-T7", 112846), ("GenoLab M", 453)]
    assert gsa.monthly_archives(stats) == [{"month": "2026-09", "archives": 1267}, {"month": "2026-10", "archives": 731}]
    assert gsa.totals(stats)["expCount"] == 2115280
    pubs = list(gsa.publications(stats))
    assert pubs[0]["doi"] == "10.1007/s40820-026-02320-7" and pubs[0]["cra"] == "CRA042678" and pubs[0]["bioproject"] == "PRJCA063830" and pubs[0]["pmid"] == "42530726"
    # malformed / missing
    assert gsa.fetch_statistics(FakeHttp([None])) is None and gsa.fetch_statistics(FakeHttp([{"nope": 1}])) is None
    assert gsa.platform_snapshot({}, "2026-10-17") == [] and gsa.monthly_archives({"total": "x"}) == []
    # time series in SQLite
    conn = sqlite3.connect(":memory:")
    assert gsa_snapshots.save_snapshot(conn, stats, "2026-10-10") == 3
    d1 = gsa_snapshots.platform_deltas(conn)
    assert d1[0]["platform"] == "Illumina NovaSeq 6000" and d1[0]["delta_experiments"] is None and d1[0]["baseline"] is None
    later = {"platforms": {"platformList": [
        {"platforms": "Illumina NovaSeq 6000", "experiments": 434392 + 1500, "runs": 1},
        {"platforms": "DNBSEQ-T7", "experiments": 112846 + 4000, "runs": 1},
        {"platforms": "GenoLab M", "experiments": 453 + 10, "runs": 1},
        {"platforms": "FASTASeq 300", "experiments": 268, "runs": 1},
    ]}, "total": {"mouthArchiveList": [["2026-10", 900]]}}
    gsa_snapshots.save_snapshot(conn, later, "2026-10-17")
    d2 = gsa_snapshots.platform_deltas(conn)
    assert d2[0]["platform"] == "DNBSEQ-T7" and d2[0]["delta_experiments"] == 4000 and d2[0]["baseline"] == "2026-10-10" and d2[0]["latest"] == "2026-10-17"
    assert {r["platform"]: r["delta_experiments"] for r in d2}["FASTASeq 300"] is None  # new platform: no baseline row
    assert gsa_snapshots.platform_deltas(conn, since="2026-10-12")[0]["baseline"] == "2026-10-10"
    assert gsa_snapshots.snapshot_dates(conn) == ["2026-10-10", "2026-10-17"]
    assert gsa_snapshots.monthly_series(conn)[0] == {"month": "2026-10", "archives": 900, "snapshot_date": "2026-10-17"}


# ------------------------------------------------------------------ GSA-Human
def _hra_row(acc, title, org, prj, strategy, exps, rel):
    cells = [f'<a href="browse/{acc}">{acc}</a>', title, org, "HDAC000001", "Control", "1", "1", "1", prj, strategy, "Disease Study", "-", "7", "7", str(exps), rel, "", "2026-10-16", '<a href="request/' + acc + '">Request</a>']
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _hra_page(total, rows):
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in ["Study", "Title", "Organization", "DAC", "Access", "Requests", "Valid Requests", "Approved", "Project", "Strategy", "Study Type", "Disease", "Individuals", "Samples", "Experiments", "Release Time", "Rank", "Last Processed", "Request"]) + "</tr>"
    return f'<html><table id="app_table">{head}{"".join(rows)}</table><div>from 1 to 10, Total {total}</div></html>'


def _runs(total, items, page_no=1, page_size=500):
    return {"totalCount": total, "pageNo": page_no, "pageSize": page_size, "runViews": [
        {"expName": e, "expTitle": "WGS", "platform": p, "runAcc": r, "strategyName": s, "biosampleAcc": "SAMC1", "sampleAcc": "HRS1", "runFileName": "x.fq.gz", "runFileSize": "1"}
        for e, p, r, s in items]}


def test_gsa_human_table_runs_and_fetch():
    page1 = _hra_page(2, [_hra_row("HRA021721", "Study one", "Org A", "PRJCA077098", "RNA-Seq", 2, "2026-10-17"), _hra_row("HRA021506", "Study two", "Org B", "PRJCA069857", "WGS", 1, "2026-10-16")])
    rows = gsa_human.parse_study_table(page1)
    assert [r["accession"] for r in rows] == ["HRA021721", "HRA021506"] and rows[0]["bioproject"] == "PRJCA077098" and rows[0]["release_time"] == "2026-10-17"
    assert gsa_human.parse_total(page1) == 2 and gsa_human.release_range("2026-09-17", "2026-10-17") == "09/17/2026 - 10/17/2026"
    runs1 = _runs(3, [("HRX1", "DNBSEQ-T7", "HRR1", "RNA-Seq"), ("HRX1", "DNBSEQ-T7", "HRR2", "RNA-Seq"), ("HRX2", "Illumina NovaSeq 6000", "HRR3", "RNA-Seq")])
    runs2 = _runs(1, [("HRX3", "Illumina HiSeq 2000", "HRR4", "WGS")])
    http = FakeHttp([page1, runs1, runs2])
    recs = list(gsa_human.fetch(http, "2026-09-17", "2026-10-17"))
    assert [r.accession for r in recs] == ["HRX1", "HRX2", "HRX3"]  # runs collapsed to experiments
    assert recs[0].archive == "gsa-human" and recs[0].instrument_model == "DNBSEQ-T7" and recs[0].platform == "BGISEQ" and recs[0].study_accession == "HRA021721"
    assert recs[0].first_public == "2026-10-17" and recs[0].organism == "Homo sapiens" and recs[0].center_name == "Org A" and recs[0].study_title == "Study one"
    assert recs[2].platform == "ILLUMINA" and recs[2].library_strategy == "WGS" and recs[2].first_public == "2026-10-16"
    assert http.calls[0][1]["releaseTime"] == "09/17/2026 - 10/17/2026" and http.calls[0][1]["order"] == "b.release_time"
    assert http.calls[1][1] == {"accession": "HRA021721", "pageSize": 500, "pageNo": 1}
    # one page of 2 rows with total 2 -> no second list request
    assert len(http.calls) == 3


def test_gsa_human_paging_stops_and_degrades():
    # list paging: total 11 -> pages 1 and 2; page 2 repeats page 1 -> stop without looping
    rows = [_hra_row(f"HRA{i:06d}", "t", "o", "PRJCA1", "WGS", 1, "2026-10-10") for i in range(10)]
    http = FakeHttp([_hra_page(11, rows), _hra_page(11, rows)])
    assert len(list(gsa_human.studies(http, "2026-10-01", "2026-10-17"))) == 10 and len(http.calls) == 2
    # run JSON paging: 2 pages until totalCount, then stop; malformed page ends the study gracefully
    http = FakeHttp([_runs(3, [("HRX1", "A", "HRR1", "WGS"), ("HRX2", "B", "HRR2", "WGS")], 1, 2), _runs(3, [("HRX3", "C", "HRR3", "WGS")], 2, 2)])
    assert [v["expName"] for v in gsa_human.runs(http, "HRA1", page_size=2)] == ["HRX1", "HRX2", "HRX3"] and len(http.calls) == 2
    assert list(gsa_human.runs(FakeHttp(["not json"]), "HRA1")) == [] and list(gsa_human.runs(FakeHttp([{"runViews": "x"}]), "HRA1")) == []
    # empty list page / unavailable page
    assert list(gsa_human.fetch(FakeHttp([_hra_page(0, [])]), "2026-10-01", "2026-10-17")) == []
    assert list(gsa_human.fetch(FakeHttp([None]), "2026-10-01", "2026-10-17")) == []
    # a study whose run endpoint fails still appears once (so the census is not silently short)
    recs = list(gsa_human.fetch(FakeHttp([_hra_page(1, [_hra_row("HRA000009", "t", "o", "PRJCA1", "WGS", 1, "2026-10-10")]), None]), "2026-10-01", "2026-10-17"))
    assert len(recs) == 1 and recs[0].accession == "HRA000009" and recs[0].instrument_model == ""
    assert gsa_human.parse_study_table("<html>no table</html>") == []


# ------------------------------------------------------------------ ChinaXiv OAI-PMH
OAI_HEAD = '<?xml version="1.0" encoding="UTF-8" ?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><responseDate>2026-10-17T20:26:02Z</responseDate><request verb="ListRecords">http://global.chinaxiv.org/oaiapi/getdata</request>'


def _rec(aid, title, date, subject, authors=("梁婧涵", "黄颂匡"), abstract="摘要 &amp;nbsp;正文", extra=""):
    au = "".join(f"<author>{a}</author>" for a in authors)
    return (f"<record><header><identifier>localIdentifier:chinaxiv_{aid.replace('.', '')}</identifier><datestamp>Oct 12, 2026 9:15:46 AM</datestamp></header>"
            f"<metadata><title>{title}</title>{au}<keyword>k1</keyword><keyword> k2</keyword><subject>{subject}</subject><abstract>{abstract}</abstract>"
            f"<article-id>{aid}</article-id><source>chinaxiv</source><date>{date}</date><type>articles</type><format>oai_dc</format>{extra}</metadata></record>")


def test_chinaxiv_parse_and_to_paper():
    xml = OAI_HEAD + "<ListRecords>" + _rec("202610.00074", "助推还是抑制？", "2026-10-12", "管理心理学", extra="<url>https://example.org/landing</url>") + \
        '<resumptionToken expirationDate="2026-10-18T06:26:02Z" completeListSize="163" cursor="100">1792268762149:100:163:OAI_DC</resumptionToken></ListRecords></OAI-PMH>'
    page = chinaxiv.parse_records(xml.encode("utf-8"))
    assert page["token"] == "1792268762149:100:163:OAI_DC" and page["complete_list_size"] == 163 and page["error"] is None
    d = page["records"][0]
    assert d["article_id"] == "202610.00074" and d["authors"] == ["梁婧涵", "黄颂匡"] and d["keywords"] == ["k1", "k2"] and d["subjects"] == ["管理心理学"]
    assert d["abstract"] == "摘要 正文"  # &nbsp; entity collapsed
    p = chinaxiv.to_paper(d)
    assert p.id == "doi:10.12074/202610.00074" and p.doi == "10.12074/202610.00074" and p.extra["doi_derived"] is True
    assert p.url == "https://chinaxiv.org/abs/202610.00074" and p.extra["landing_url"] == "https://example.org/landing"
    assert p.source == "chinaxiv" and p.kind == "preprint" and p.journal == "ChinaXiv" and p.pub_date == "2026-10-12"
    assert p.category == "管理心理学" and p.topics == ["k1", "k2"] and p.extra["language"] == "zh" and p.extra["life_science"] is False
    assert [a.name for a in p.authors] == ["梁婧涵", "黄颂匡"]
    assert chinaxiv.normalize_date("2026-9-19") == "2026-09-19" and chinaxiv.normalize_date("Oct 12") == "" and chinaxiv.normalize_date("2026-13-40") == ""
    assert chinaxiv.is_life_science(["预防医学与公共卫生学"]) and chinaxiv.is_life_science(["农业基础学科"]) and not chinaxiv.is_life_science(["核物理学"])
    assert chinaxiv.is_life_science(["植物生态学和植物地理学"]) and chinaxiv.is_life_science(["药物设计"]) and chinaxiv.is_life_science(["Biology"])
    # keywords do not fire (free text is too noisy) and English matching is whole-word only
    assert not chinaxiv.is_life_science(["认知心理学"], ["生物运动"]) and not chinaxiv.is_life_science(["Accelerator physics"], ["implantation"])
    assert not chinaxiv.is_life_science(["Excellent engineering"])


def test_chinaxiv_fetch_pages_dedupes_and_filters():
    p1 = OAI_HEAD + "<ListRecords>" + _rec("202610.00001", "A", "2026-9-19", "临床医学") + _rec("202610.00002", "B", "2026-10-01", "核物理学") + \
        '<resumptionToken completeListSize="3" cursor="2">tok1</resumptionToken></ListRecords></OAI-PMH>'
    p2 = OAI_HEAD + "<ListRecords>" + _rec("202610.00001", "A v2", "2026-10-05", "临床医学") + "</ListRecords></OAI-PMH>"  # same id, later date, no token
    http = FakeHttp([p1, p2])
    papers = list(chinaxiv.fetch(http, "2026-09-17", "2026-10-17"))
    assert [p.title for p in papers] == ["A v2", "B"] and papers[0].pub_date == "2026-10-05"
    assert http.calls[0][1]["startTime"] == "2026-09-17T00:00:00Z" and http.calls[0][1]["endTime"] == "2026-10-17T23:59:59Z" and http.calls[0][1]["source"] == "chinaxiv"
    assert http.calls[1][1] == {"verb": "ListRecords", "resumptionToken": "tok1"}
    # life-science filter keeps the clinical record only
    assert [p.title for p in chinaxiv.fetch(FakeHttp([p1, p2]), "2026-09-17", "2026-10-17", life_science_only=True)] == ["A v2"]
    # max_records caps the sweep
    assert len(list(chinaxiv.fetch(FakeHttp([p1, p2]), "2026-09-17", "2026-10-17", max_records=1))) == 1


def test_chinaxiv_empty_malformed_and_repeated_token():
    empty = OAI_HEAD + '<error code="noRecordsMatch">There are no metadata formats available for the specified item.</error></OAI-PMH>'
    assert list(chinaxiv.fetch(FakeHttp([empty]), "2026-10-01", "2026-10-02")) == []
    assert chinaxiv.parse_records(b"<OAI-PMH><ListRecords><record>")["error"].startswith("malformed XML")
    assert list(chinaxiv.fetch(FakeHttp(["<not xml"]), "2026-10-01", "2026-10-02")) == []
    assert list(chinaxiv.fetch(FakeHttp([None]), "2026-10-01", "2026-10-02")) == []
    loop = OAI_HEAD + "<ListRecords>" + _rec("202610.00009", "L", "2026-10-02", "数学") + '<resumptionToken>same</resumptionToken></ListRecords></OAI-PMH>'
    http = FakeHttp([loop, loop, loop])
    assert len(list(chinaxiv.fetch(http, "2026-10-01", "2026-10-02"))) == 1 and len(http.calls) == 2  # repeated token -> stop
    # record without article-id still yields a paper keyed by the OAI identifier
    xml = OAI_HEAD + "<ListRecords><record><header><identifier>localIdentifier:chinaxiv_1</identifier></header><metadata><title>T</title></metadata></record></ListRecords></OAI-PMH>"
    (p,) = list(chinaxiv.fetch(FakeHttp([xml]), "2026-10-01", "2026-10-02"))
    assert p.id == "chinaxiv:chinaxiv_1" and p.doi is None and p.extra["doi_derived"] is False


# ------------------------------------------------------------------ gazetteer: Chinese companies and archive vocabulary
@pytest.fixture(scope="module")
def settings():
    return Settings.from_env()


def test_companies_yaml_has_china_entries_without_duplicates(settings):
    data = settings.yaml("companies")
    ids = [e["id"] for e in data["entries"]]
    assert len(ids) == len(set(ids))
    for cid in ["mgi-tech", "bgi-genomics", "novogene", "genemind", "salus-biomed", "axbio", "singleron", "berry-genomics", "burning-rock", "geneplus",
                "genetron", "haplox", "biomarker-technologies", "annoroad", "biotree", "genecast", "amoydx", "sinopharm", "daan-gene", "sansure",
                "hangzhou-biotest", "pharmaron", "wuxi", "wuxi-biologics", "hengrui", "beigene", "zai-lab", "innovent", "akeso", "legend-biotech"]:
        assert cid in ids, cid
    by_id = {e["id"]: e for e in data["entries"]}
    for cid, ticker in [("mgi-tech", "688114.SS"), ("bgi-genomics", "300676.SZ"), ("novogene", "688315.SS"), ("berry-genomics", "000710.SZ"),
                        ("amoydx", "300685.SZ"), ("daan-gene", "002030.SZ"), ("sansure", "688289.SS"), ("wuxi-biologics", "2269.HK"), ("innovent", "1801.HK"), ("akeso", "9926.HK")]:
        assert by_id[cid]["ticker"] == ticker
    # every China entry carries at least one Chinese-character alias and one e-mail domain
    for cid in ["mgi-tech", "bgi-genomics", "novogene", "genemind", "wuxi-biologics", "hengrui", "beigene"]:
        assert any(any("一" <= ch <= "鿿" for ch in a) for a in by_id[cid]["aliases"]), cid
        assert by_id[cid]["domains"], cid
    # no alias is shared by two entries (what `check-config` reports)
    gz = Gazetteer.from_yaml(data, "company")
    seen = {}
    for e in gz.entries:
        for a in e.aliases:
            assert seen.setdefault(a.lower(), e.id) == e.id, f"alias collision: {a}"


def test_company_gazetteer_matches_chinese_names_and_domains(settings):
    gz = Gazetteer.from_yaml(settings.yaml("companies"), "company")
    # delimited registered names match; English context near a short Chinese brand name also matches
    assert [h.entity_id for h in gz.find("深圳华大基因股份有限公司, 深圳 518083")] == ["bgi-genomics"]
    assert [h.entity_id for h in gz.find("康龙化成(北京)新药技术股份有限公司")] == ["pharmaron"]
    assert {h.entity_id for h in gz.find("BGI Genomics (华大基因), Shenzhen; Novogene Co., Ltd., Beijing")} == {"bgi-genomics", "novogene"}
    # e-mail domains
    assert {e.id for e in gz.match_domains(["x@mgi-tech.com", "y@genemind.com", "z@wuxibiologics.com", "w@genomics.cn", "v@legendbiotech.com"])} == {
        "mgi-tech", "genemind", "wuxi-biologics", "bgi-genomics", "legend-biotech"}
    # known limitation (documented in docs/sources/china.md): a short Chinese brand embedded in running prose does not match
    assert gz.find("本研究由华大基因完成") == []


def test_instrument_gazetteer_maps_mgi_archive_models(settings):
    gz = Gazetteer.from_yaml(settings.yaml("instruments"), "instrument")
    # SRA/ENA controlled-vocabulary spellings (SRA.common.xsd 1.5) and the GSA family label resolve to the MGI entries
    for model in ["DNBSEQ-T7", "DNBSEQ-G400", "DNBSEQ-G99", "BGISEQ-500", "BGISEQ (model n/a)"]:
        e = gz.match_archive_model(model)
        assert e is not None and e.vendor == "MGI Tech" and e.ticker == "688114.SS", model
    assert gz.match_archive_model("Illumina NovaSeq 6000").vendor == "Illumina"
    assert gz.match_archive_model("ILLUMINA (model n/a)") is None  # family label alone is not a model; vendor share falls back to platform
