"""Source adapters tested against canned payloads (no network)."""

from litradar.sources import biorxiv, crossref, europepmc, openalex, rss
from litradar.sources.ena import FIELDS_EXPERIMENT


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


def test_biorxiv_paging_and_latest_version():
    page1 = {"messages": [{"status": "ok", "count": 2, "total": 3}], "collection": [
        {"doi": "10.1101/2026.10.01.1", "title": "A", "authors": "X, Y; Z, W", "date": "2026-10-10", "version": "1", "category": "genomics", "license": "cc_by", "jatsxml": "https://x/1.xml", "abstract": "abs", "published": "NA", "server": "bioRxiv", "author_corresponding_institution": "Inst"},
        {"doi": "10.1101/2026.10.01.2", "title": "B", "authors": "Q, R", "date": "2026-10-11", "version": "1", "category": "neuroscience", "license": "cc_by", "jatsxml": "", "abstract": "abs", "published": "10.1038/s41586-026-0001-1", "server": "bioRxiv"},
    ]}
    page2 = {"messages": [{"status": "ok", "count": 1, "total": 3}], "collection": [
        {"doi": "10.1101/2026.10.01.1", "title": "A v2", "authors": "X, Y; Z, W", "date": "2026-10-12", "version": "2", "category": "genomics", "license": "cc_by", "jatsxml": "https://x/1v2.xml", "abstract": "abs2", "published": "NA", "server": "bioRxiv"},
    ]}
    http = FakeHttp([page1, page2])
    papers = {p.doi: p for p in biorxiv.fetch(http, "2026-10-10", "2026-10-17", categories=["genomics", "neuroscience"])}
    assert set(papers) == {"10.1101/2026.10.01.1", "10.1101/2026.10.01.2"}
    assert papers["10.1101/2026.10.01.1"].title == "A v2" and papers["10.1101/2026.10.01.1"].fulltext_url.endswith("1v2.xml")
    assert papers["10.1101/2026.10.01.2"].published_doi == "10.1038/s41586-026-0001-1"
    assert len(http.calls) == 2 and http.calls[1][0].endswith("/2")


def test_europepmc_to_paper_and_queries():
    rec = {"id": "123", "source": "MED", "pmid": "123", "pmcid": "PMC999", "doi": "10.1038/S41592-026-1", "title": "Title.", "abstractText": "Abs",
           "authorList": {"author": [{"fullName": "Doe J", "authorAffiliationDetailsList": {"authorAffiliation": [{"affiliation": "Illumina, San Diego"}]}, "authorId": {"type": "ORCID", "value": "0000-0002-1"}}]},
           "journalInfo": {"journal": {"title": "Nature methods"}}, "firstPublicationDate": "2026-10-11", "isOpenAccess": "Y", "grantsList": {"grant": [{"agency": "NIH"}]}}
    p = europepmc.to_paper(rec)
    assert p.id == "doi:10.1038/s41592-026-1" and p.journal == "Nature methods" and p.extra["pmcid"] == "PMC999"
    assert p.authors[0].orcid == "0000-0002-1" and p.affiliations == ["Illumina, San Diego"]
    assert p.sections["funding"] == "NIH"
    q = europepmc.section_query("METHODS", ["NovaSeq", "Xenium"], "2026-10-10", "2026-10-17")
    assert q == '(METHODS:"NovaSeq" OR METHODS:"Xenium") AND (FIRST_PDATE:[2026-10-10 TO 2026-10-17]) AND (OPEN_ACCESS:y)'
    assert europepmc.journal_query(["1548-7091"], "2026-10-10", "2026-10-17").startswith('(ISSN:"1548-7091")')


def test_europepmc_cursor_paging_stops():
    http = FakeHttp([
        {"resultList": {"result": [{"id": "1", "source": "MED"}]}, "nextCursorMark": "AAA"},
        {"resultList": {"result": [{"id": "2", "source": "MED"}]}, "nextCursorMark": "AAA"},  # same cursor -> stop
    ])
    out = list(europepmc.search(http, "x", max_records=10))
    assert [r["id"] for r in out] == ["1", "2"]


def test_openalex_abstract_and_company_institutions():
    w = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1038/abc", "title": "T", "publication_date": "2026-10-12", "type": "article",
         "primary_location": {"source": {"display_name": "Nature", "type": "journal"}, "landing_page_url": "https://x"},
         "authorships": [{"author": {"display_name": "A B", "id": "https://openalex.org/A5", "orcid": None}, "institutions": [{"display_name": "Illumina (United States)", "type": "company", "ror": "r"}], "raw_affiliation_strings": ["Illumina Inc, San Diego"]}],
         "abstract_inverted_index": {"Hello": [0], "world": [1]}, "topics": [{"display_name": "Genomics"}], "primary_topic": {"id": "https://openalex.org/T10001"}, "cited_by_count": 2}
    p = openalex.to_paper(w)
    assert p.abstract == "Hello world" and p.id == "doi:10.1038/abc" and p.cited_by_count == 2
    assert p.extra["company_institutions"] == ["Illumina (United States)"] and p.extra["primary_topic"] == "T10001"
    assert p.authors[0].openalex_id == "A5"


def test_crossref_to_paper_strips_jats_abstract():
    it = {"DOI": "10.1038/X", "title": ["Great paper"], "abstract": "<jats:p>Hello <jats:italic>there</jats:italic></jats:p>", "author": [{"given": "A", "family": "B", "affiliation": [{"name": "Twist Bioscience"}]}],
          "container-title": ["Nature Methods"], "published": {"date-parts": [[2026, 10, 13]]}, "URL": "https://doi.org/10.1038/X", "type": "journal-article", "funder": [{"name": "Wellcome Trust"}]}
    p = crossref.to_paper(it)
    assert p.abstract == "Hello there" and p.pub_date == "2026-10-13" and p.affiliations == ["Twist Bioscience"] and p.sections["funding"] == "Wellcome Trust"


def test_rss_parsers():
    rss2 = """<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"><channel><title>t</title>
      <item><title>Paper &amp; co</title><link>https://www.nature.com/articles/s41586-026-0001-1</link><pubDate>Mon, 13 Oct 2026 00:00:00 GMT</pubDate><dc:creator>A, B</dc:creator><prism:doi>10.1038/s41586-026-0001-1</prism:doi><description>&lt;p&gt;Summary&lt;/p&gt;</description></item>
    </channel></rss>"""
    items = rss.parse_feed(rss2)
    assert items[0]["title"] == "Paper & co" and items[0]["date"] == "2026-10-13" and items[0]["doi"] == "10.1038/s41586-026-0001-1" and items[0]["summary"] == "Summary"
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>E</title><link rel="alternate" href="https://e/1"/><published>2026-10-14T10:00:00Z</published><author><name>Z</name></author><summary>s</summary></entry></feed>"""
    items = rss.parse_feed(atom)
    assert items[0]["link"] == "https://e/1" and items[0]["date"] == "2026-10-14" and items[0]["authors"] == ["Z"]


def test_ena_fields_constant():
    assert "instrument_model" in FIELDS_EXPERIMENT and "first_public" in FIELDS_EXPERIMENT


def test_sra_expxml_parsing():
    from litradar.sources import sra

    xml = ('&lt;Summary&gt;&lt;Title&gt;WGS of sample&lt;/Title&gt;&lt;Platform instrument_model="Illumina NovaSeq X Plus"&gt;ILLUMINA&lt;/Platform&gt;&lt;/Summary&gt;'
           '&lt;Submitter acc="SRA1" center_name="Example Center"/&gt;&lt;Experiment acc="SRX1" status="public" name="n"/&gt;&lt;Study acc="SRP1" name="Study title"/&gt;'
           '&lt;Organism taxid="9606" ScientificName="Homo sapiens"/&gt;&lt;Library_descriptor&gt;&lt;LIBRARY_STRATEGY&gt;WGS&lt;/LIBRARY_STRATEGY&gt;&lt;/Library_descriptor&gt;&lt;Bioproject&gt;PRJNA1&lt;/Bioproject&gt;')
    x = sra._parse_expxml(xml)
    assert x["instrument_model"] == "Illumina NovaSeq X Plus" and x["platform"] == "ILLUMINA"
    assert x["experiment_accession"] == "SRX1" and x["study_accession"] == "SRP1" and x["organism"] == "Homo sapiens"
    assert x["library_strategy"] == "WGS" and x["center_name"] == "Example Center"
    assert sra._term("2026-10-10", "2026-10-17") == "2026/10/10:2026/10/17[PDAT]"
