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
    # the window admits records first published in it OR indexed in it (Europe PMC loads PubMed records 1-14 days late)
    assert q == '(METHODS:"NovaSeq" OR METHODS:"Xenium") AND ((FIRST_PDATE:[2026-10-10 TO 2026-10-17]) OR (CREATION_DATE:[2026-10-10 TO 2026-10-17] AND FIRST_PDATE:[2026-09-10 TO 2026-10-17])) AND (OPEN_ACCESS:y)'
    assert europepmc.date_clause("2026-10-10", "2026-10-17", index_window=False) == "(FIRST_PDATE:[2026-10-10 TO 2026-10-17])"
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


def test_openalex_company_institution_needs_the_raw_affiliation_to_agree():
    """digest-D8: OpenAlex's ROR resolution typed an ORCID URL as a social-media company, 'Mivax Therapeutics' as the
    unrelated 'Mabvax Therapeutics' and 'Immunology Discovery Research, AbbVie' as 'Discovery Laboratories'."""
    ok = openalex.affiliation_corroborates
    assert ok("Twitter (United States)", ["https://orcid.org/0000-0001-5035-7655"]) is False
    assert ok("Mabvax Therapeutics (United States)", ["Mivax Therapeutics Inc., San Francisco, CA, USA"]) is False  # 'Therapeutics' is not identity
    assert ok("Discovery Laboratories (United States)", ["Immunology Discovery Research, AbbVie Bioresearch Center"]) is False  # no distinctive token: whole name needed
    assert ok("Discovery Laboratories (United States)", ["Discovery Laboratories Inc., Warrington, PA"]) is True
    assert ok("BGI Genomics", ["C2i Genomics Inc., New York, NY"]) is False
    # genuine affiliations survive: a superscript glued to the name, a longer trading name, a shorter one
    assert ok("Genentech", ["4Genentech, South San Francisco, CA;"]) is True
    assert ok("Amazon (United States)", ["3Amazon Web Services, Seattle, WA"]) is True
    assert ok("Thermo Fisher Scientific (United States)", ["Thermo Fisher, Waltham, MA, USA"]) is True
    assert ok("10x Genomics (United States)", ["10x Genomics, Pleasanton, CA"]) is True
    # nothing to check against -> accepted as typed
    assert ok("Illumina (United States)", []) is True and ok("Illumina (United States)", ["", None]) is True
    assert openalex.institution_tokens("Mabvax Therapeutics (United States)") == ["mabvax"]
    assert openalex.institution_tokens("Thermo Fisher Scientific (United States)") == ["thermo", "fisher"]
    assert openalex.institution_tokens("Discovery Laboratories (United States)") == []


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


def test_sra_expxml_raw_json_form_with_ampersand_is_parsed_fully():
    """retmode=json hands over raw XML; '&amp;' inside it must survive (the old html.unescape broke it)."""
    from litradar.sources import sra

    raw = ('<Summary><Title>WGS of Klebsiella:Chicken farm</Title><Platform instrument_model="Illumina NovaSeq 6000">ILLUMINA</Platform>'
           '<Statistics total_runs="1" total_spots="1" total_bases="1"/></Summary>'
           '<Submitter acc="SRA2" center_name="Agriculture &amp; Agri-Food Canada" contact_name="x"/>'
           '<Experiment acc="SRX35734411" status="public" name="n"/><Study acc="SRP677163" name="Farm &amp; field isolates"/>'
           '<Organism taxid="573" ScientificName="Klebsiella pneumoniae"/><Library_descriptor><LIBRARY_STRATEGY>WGS</LIBRARY_STRATEGY></Library_descriptor>')
    x = sra._parse_expxml(raw)
    assert x["experiment_accession"] == "SRX35734411" and x["study_accession"] == "SRP677163"
    assert x["center_name"] == "Agriculture & Agri-Food Canada" and x["study_title"] == "Farm & field isolates"
    assert x["instrument_model"] == "Illumina NovaSeq 6000" and x["library_strategy"] == "WGS" and x["organism"] == "Klebsiella pneumoniae"


def test_sra_regex_fallback_keeps_the_real_accession_on_malformed_xml():
    from litradar.sources import sra

    broken = '<Summary><Platform instrument_model="Revio">PACBIO_SMRT</Platform><Experiment acc="SRX9" status="public"/><Study acc="SRP9" name="S"/><Library_descriptor><LIBRARY_STRATEGY>WGS</LIBRARY_STRATEGY>'
    x = sra._parse_expxml(broken)
    assert x["experiment_accession"] == "SRX9" and x["study_accession"] == "SRP9" and x["instrument_model"] == "Revio" and x["library_strategy"] == "WGS"
    assert sra._parse_expxml("") == {}


def test_sra_fetch_uses_real_accessions_and_counts_synthetic_ones(caplog):
    from litradar.sources import sra

    esearch = {"esearchresult": {"count": "2", "webenv": "W", "querykey": "1"}}
    esummary = {"result": {"uids": ["1", "2"],
                           "1": {"expxml": '<Summary><Platform instrument_model="Illumina MiSeq">ILLUMINA</Platform></Summary><Submitter center_name="A &amp; B"/><Experiment acc="SRX1"/><Study acc="SRP1" name="t"/>', "createdate": "2026/10/15"},
                           "2": {"expxml": "", "createdate": "2026/10/15"}}}
    http = FakeHttp([esearch, esummary])
    recs = list(sra.fetch(http, "2026-10-10", "2026-10-17"))
    assert [r.accession for r in recs] == ["SRX1", "SRA-UID-2"] and recs[0].center_name == "A & B" and recs[0].first_public == "2026-10-15"
    assert "1 record(s) had no parseable experiment accession" in caplog.text


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _StatusHttp:
    """Mimics litradar.http.Http.get: queued Response-like objects, plus get_json for the esummary pages."""

    def __init__(self, responses, json_payloads=()):
        self.responses = list(responses)
        self.json_payloads = list(json_payloads)
        self.calls = []

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,)):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0) if self.responses else None

    def get_json(self, url, params=None, cache=False):
        self.calls.append((url, dict(params or {})))
        return self.json_payloads.pop(0) if self.json_payloads else None


def test_sra_api_key_is_sent_only_when_it_looks_like_a_key(caplog):
    from litradar.sources import sra

    sra._warned_bad_key = False
    assert sra.usable_api_key("0123456789abcdef0123456789abcdef0123") == "0123456789abcdef0123456789abcdef0123"
    assert sra.usable_api_key("") == "" and sra.usable_api_key("   ") == ""
    with caplog.at_level("WARNING", logger="litradar.sources.sra"):
        # what a README-literal `cp .env.example .env` used to load: the inline comment became the key value
        assert sra.usable_api_key("# https://account.ncbi.nlm.nih.gov/settings/   E-utilities 3 -> 10 requests/s") == ""
        assert sra.usable_api_key("abc def") == "" and sra.usable_api_key("key#1") == ""
    assert sra.usable_api_key("K") == "K"  # short but well-formed keys are NCBI's problem to refuse (HTTP 400 -> SraError)
    assert caplog.text.count("does not look like an NCBI key") == 1  # warned once, not per request
    esearch = {"esearchresult": {"count": "0", "webenv": "", "querykey": ""}}
    http = _StatusHttp([_Resp(200, esearch)])
    assert list(sra.fetch(http, "2026-10-10", "2026-10-17", api_key="# comment text")) == []
    assert "api_key" not in http.calls[0][1]
    http = _StatusHttp([_Resp(200, esearch)])
    list(sra.fetch(http, "2026-10-10", "2026-10-17", api_key="0123456789abcdef0123456789abcdef0123"))
    assert http.calls[0][1]["api_key"] == "0123456789abcdef0123456789abcdef0123"
    # GEO shares the rule
    from litradar.sources import geo

    assert "api_key" not in geo._params("# not a key", term="x") and geo._params("0123456789abcdef0123456789abcdef0123", term="x")["api_key"]


def test_sra_fetch_raises_on_esearch_failures_instead_of_an_empty_census():
    import pytest

    from litradar.sources import sra

    # HTTP 400 (what a garbage api_key earns): the status, NCBI's body and the hint are in the message
    http = _StatusHttp([_Resp(400, None, text='{"error":"API key invalid","api-key":"# comment"}')])
    with pytest.raises(sra.SraError, match=r"HTTP 400 .*check NCBI_API_KEY.*API key invalid"):
        list(sra.fetch(http, "2026-10-10", "2026-10-17"))
    # an error envelope with HTTP 200
    http = _StatusHttp([_Resp(200, {"error": "API rate limit exceeded", "api-key": "1.2.3.4", "count": 3, "limit": 3})])
    with pytest.raises(sra.SraError, match="API rate limit exceeded"):
        list(sra.fetch(http, "2026-10-10", "2026-10-17"))
    # NCBI's own ERROR field inside esearchresult
    http = _StatusHttp([_Resp(200, {"esearchresult": {"ERROR": "Database: sra - is not supported"}})])
    with pytest.raises(sra.SraError, match="is not supported"):
        list(sra.fetch(http, "2026-10-10", "2026-10-17"))
    # the client gave up (retries exhausted / host blocked)
    with pytest.raises(sra.SraError, match="gave no answer"):
        list(sra.fetch(_StatusHttp([None]), "2026-10-10", "2026-10-17"))
    # a count without a WebEnv cannot be paged: say so
    http = _StatusHttp([_Resp(200, {"esearchresult": {"count": "12", "webenv": "", "querykey": ""}})])
    with pytest.raises(sra.SraError, match="no WebEnv"):
        list(sra.fetch(http, "2026-10-10", "2026-10-17"))
    # a genuine zero is not an error
    assert list(sra.fetch(_StatusHttp([_Resp(200, {"esearchresult": {"count": "0", "idlist": []}})]), "2026-10-10", "2026-10-17")) == []
    # count() stays advisory: logs and returns 0 rather than raising
    assert sra.count(_StatusHttp([_Resp(400, None, text="bad")]), "x[PDAT]") == 0


# ---- arXiv: a blocked or dead host is reported, never silently partial --------------------------------------
ARXIV_PAGE = (
    '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
    '<entry><id>http://arxiv.org/abs/2610.00001v1</id><title>A  preprint\n title</title><summary>S</summary>'
    "<published>2026-10-12T00:00:00Z</published><author><name>A B</name></author>"
    '<arxiv:primary_category term="q-bio.GN"/></entry></feed>'
)


class _BlockingHttp(FakeHttp):
    """get_text answers from the queue; `block_after` responses later the host is marked blocked, as Http does on 429s."""

    def __init__(self, payloads, block_after=None):
        super().__init__(payloads)
        self.blocked_hosts = set()
        self.block_after = block_after

    def get_text(self, url, params=None, cache=False):
        if self.block_after is not None and len(self.calls) >= self.block_after:
            self.blocked_hosts.add("export.arxiv.org")
            self.calls.append((url, params))
            return None
        return super().get_text(url, params, cache)


def test_arxiv_stops_and_reports_when_the_host_is_blocked_or_silent():
    import pytest

    from litradar.sources import arxiv

    # a complete single page: one paper, no error
    papers = list(arxiv.fetch(_BlockingHttp([ARXIV_PAGE]), "2026-10-10", "2026-10-17", ["q-bio.GN"]))
    assert len(papers) == 1 and papers[0].title == "A preprint title" and papers[0].extra["arxiv_id"] == "2610.00001v1"
    # first page dead (retries exhausted, host not blocked): raised with the reason, nothing yielded
    with pytest.raises(arxiv.ArxivUnavailable, match="gave no usable answer on the first page"):
        list(arxiv.fetch(_BlockingHttp([None]), "2026-10-10", "2026-10-17", ["q-bio.GN"]))
    # host blocked after two 429s mid-way: the pages already yielded stay with the caller, the error names the block
    full_page = ARXIV_PAGE.replace("<entry>", "<entry>" * 1).replace("</entry>", "</entry>")
    many = full_page.replace('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">',
                             '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">' + "".join(
                                 f'<entry><id>http://arxiv.org/abs/2610.{i:05d}v1</id><title>T{i}</title><summary>S</summary>'
                                 f'<published>2026-10-12T00:00:00Z</published><arxiv:primary_category term="q-bio.GN"/></entry>'
                                 for i in range(199)))
    http = _BlockingHttp([many], block_after=1)
    got = []
    with pytest.raises(arxiv.ArxivUnavailable) as info:
        for p in arxiv.fetch(http, "2026-10-10", "2026-10-17", ["q-bio.GN"], max_records=1000):
            got.append(p)
    assert len(got) == 200 and len(http.calls) == 2  # no third request on a blocked host
    assert "export.arxiv.org rate-limited (HTTP 429/503 twice in a row) and skipped for the rest of the run after 1 page(s), 200 record(s)" in str(info.value)
    # a non-Atom body (an HTML error page) is reported the same way
    with pytest.raises(arxiv.ArxivUnavailable, match="not Atom XML"):
        list(arxiv.fetch(_BlockingHttp(["<html>503 Service Unavailable"]), "2026-10-10", "2026-10-17", ["q-bio.GN"]))


# ---- RSS: parse errors name the feed, once per URL; tracking suffixes are stripped ---------------------------
def test_rss_parse_errors_name_the_feed_once_and_links_lose_tracking_params(caplog):
    import logging

    rss._BAD_FEEDS.clear()
    html_body = "<!doctype html>\n<html><head><title>Just a moment...</title></head><body>challenge</body></html>"
    with caplog.at_level(logging.DEBUG, logger="litradar.sources.rss"):
        assert rss.parse_feed(html_body, feed_name="Example Journal ToC", url="https://feeds.example.org/toc.xml") == []
        assert rss.parse_feed(html_body, feed_name="Example Journal ToC", url="https://feeds.example.org/toc.xml") == []
        assert rss.parse_feed("not xml at all", feed_name="Other feed") == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # one per feed, the repeat went to DEBUG
    assert warnings[0].getMessage().startswith("feed parse error in Example Journal ToC (https://feeds.example.org/toc.xml): ")
    assert "body starts '<!doctype html> <html><head><title>Just a moment...</title>" in warnings[0].getMessage()
    assert warnings[1].getMessage().startswith("feed parse error in Other feed: ")
    assert rss.bad_feeds() == [
        {"url": "https://feeds.example.org/toc.xml", "name": "Example Journal ToC", "error": warnings[0].getMessage().split(": ", 1)[1].split("; body")[0]},
        {"url": "Other feed", "name": "Other feed", "error": warnings[1].getMessage().split(": ", 1)[1].split("; body")[0]},
    ]
    rss._BAD_FEEDS.clear()
    # links: feed-reader tracking suffixes go, everything else stays byte for byte
    assert rss.clean_link("https://ascopubs.org/doi/abs/10.1200/JCO-26-00565?af=R") == "https://ascopubs.org/doi/abs/10.1200/JCO-26-00565"
    assert rss.clean_link("https://www.nature.com/articles/s41586-026-1?utm_source=feed&utm_medium=rss&dgcid=rss_sd_all") == "https://www.nature.com/articles/s41586-026-1"
    assert rss.clean_link("https://example.org/a?id=7&utm_campaign=x&page=2") == "https://example.org/a?id=7&page=2"
    assert rss.clean_link("https://example.org/a?id=7") == "https://example.org/a?id=7" and rss.clean_link("") == ""
    rss2 = ('<rss version="2.0"><channel><item><title>T</title><link>https://ascopubs.org/doi/10.1200/X?af=R</link>'
            "<pubDate>Mon, 12 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>")
    assert rss.parse_feed(rss2)[0]["link"] == "https://ascopubs.org/doi/10.1200/X"
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title>'
            '<link rel="alternate" href="https://example.org/p?utm_source=rss"/><updated>2026-10-12T00:00:00Z</updated></entry></feed>')
    assert rss.parse_feed(atom)[0]["link"] == "https://example.org/p"


# ---- RSS: the outcome dict tells a caller the body was not a feed; SEC helpers -----------------------------------
def test_rss_outcome_reports_non_feed_bodies_and_sec_helpers_resolve_tickers_to_ciks():
    """The WARNING alone never reaches a run report, so parse_feed/fetch_items record why a body yielded nothing:
    not XML (EDGAR's "No matching Ticker Symbol." page), or well-formed XHTML with an <html> root."""
    from litradar.sources import sec

    rss._BAD_FEEDS.clear()
    out = {}
    assert rss.parse_feed("<!doctype html><html><body>No matching Ticker Symbol.</body></html>", "SEC 8-K TEM", "https://www.sec.gov/x", outcome=out) == []
    assert out["parsed"] is False and out["error"].startswith("not XML (syntax error: line 1, column 0; body starts ") and "No matching Ticker Symbol." in out["body"]
    out = {}
    assert rss.parse_feed('<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Sign in</p></body></html>', "Journal", outcome=out) == []
    assert out == {"parsed": False, "error": "HTML document, not a feed", "body": '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Sign in</p></body></html>'}
    out = {}
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link href="https://example.org/p"/></entry></feed>'
    assert len(rss.parse_feed(atom, outcome=out)) == 1 and out["parsed"] is True and out["error"] == ""
    assert {e["name"] for e in rss.bad_feeds()} == {"SEC 8-K TEM", "Journal"}
    rss._BAD_FEEDS.clear()

    class _Http:
        def __init__(self, body):
            self.body = body

        def get_text(self, url, params=None, cache=False, retries=None):
            return self.body

    out = {}
    assert rss.fetch_items(_Http(None), "https://example.org/feed", outcome=out) == [] and out == {"fetched": False}
    out = {}
    assert rss.fetch_items(_Http("<html/>"), "https://example.org/feed", "F", outcome=out) == [] and out["fetched"] is True and out["parsed"] is False
    rss._BAD_FEEDS.clear()
    # SEC helpers: 10-digit CIK URLs, both company_tickers.json shapes, unresolved tickers listed, EDGAR messages recognised
    rows = [{"cik": "1717115", "ticker": "TEM", "title": "Tempus AI, Inc."}, {"cik_str": 31791, "ticker": "rvty", "title": "REVVITY, INC."}, {"ticker": "BAD", "cik": "n/a"}]
    assert sec.resolve(["TEM", "RVTY", "GRAL", "BAD"], rows) == ({"TEM": "0001717115", "RVTY": "0000031791"}, ["BAD", "GRAL"])
    assert sec.feed_url("0001717115") == "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001717115&type=8-K&dateb=&owner=include&count=20&output=atom"
    assert sec.pad_cik(320193) == "0000320193" and sec.pad_cik("TEM") == "TEM"
    assert sec.is_plain_ticker("ILMN") and sec.is_plain_ticker("P") and not sec.is_plain_ticker("ROG.SW") and not sec.is_plain_ticker("private") and not sec.is_plain_ticker("")
    assert sec.error_message("<p>Previous Page --> No matching Ticker Symbol.</p>") == "No matching Ticker Symbol."
    assert sec.error_message("<p>No matching CIK.</p>") == "No matching CIK." and sec.error_message("<feed/>") == "" and sec.error_message(None) == ""


# ---- metadata hygiene shared by the adapters -------------------------------------------------------------
def test_clean_text_and_crossref_titles_and_funders():
    from litradar.sources import clean_text

    assert clean_text("Integrating glycosylation in <i>de novo</i> protein design") == "Integrating glycosylation in de novo protein design"
    assert clean_text("A B1 SINE element within the mouse\n                    <i>Malat1</i>\n                    long noncoding RNA") == "A B1 SINE element within the mouse Malat1 long noncoding RNA"
    assert clean_text("H<sub>2</sub>O &amp; CO<sub>2</sub>") == "H 2 O & CO 2".replace("H 2 O", "H 2 O")  # tags become spaces, entities decode
    assert clean_text("plain title") == "plain title" and clean_text(None) == "" and clean_text("  a   b ") == "a b"
    item = {
        "DOI": "10.1038/s41592-026-03264-x", "type": "journal-article",
        "title": ["Integrating glycosylation in <i>de novo</i>\n   protein design"], "container-title": ["Nature\nMethods"],
        "created": {"date-time": "2026-10-13T00:00:00Z"}, "published": {"date-parts": [[2026, 10]]},
        "funder": [{"name": "U.S. Department of Health & Human Services | NIH | National Cancer Institute"}] * 24 + [{"name": "Cancer Research UK"}] * 9,
    }
    p = crossref.to_paper(item, "Nature Methods")
    assert p.title == "Integrating glycosylation in de novo protein design" and p.journal == "Nature Methods"
    assert p.extra["funders"] == ["U.S. Department of Health & Human Services | NIH | National Cancer Institute", "Cancer Research UK"]
    assert p.sections["funding"] == "U.S. Department of Health & Human Services | NIH | National Cancer Institute; Cancer Research UK"


JATS_WITH_ABSTRACT = (
    '<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>'
    "<abstract abstract-type=\"graphical\"><p>Graphical abstract figure.</p></abstract>"
    "<abstract><title>Abstract</title><p>In most cells, <italic>Malat1</italic> long noncoding RNA localizes to the\n nucleus.</p>"
    "<p>Here we show a micropeptide.</p></abstract></article-meta></front>"
    "<body><sec><title>Methods</title><p>Cells were sequenced on a NovaSeq 6000.</p></sec></body></article>"
)


def test_fulltext_fills_a_missing_abstract_from_the_jats_document():
    from litradar.models import Paper
    from litradar.sources import jats_abstract

    assert jats_abstract(JATS_WITH_ABSTRACT) == "Abstract In most cells, Malat1 long noncoding RNA localizes to the nucleus. Here we show a micropeptide."
    assert jats_abstract("<article/>") == "" and jats_abstract("<not xml") == "" and jats_abstract(None) == ""

    class _XmlHttp:
        def __init__(self, body):
            self.body = body

        def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,), retries=None):
            r = type("R", (), {})()
            r.status_code = 200
            r.content = self.body.encode("utf-8")
            return r

    p = Paper(id="europepmc:MED:1", source="europepmc", kind="article", title="T", doi=None, abstract="", journal="Science Advances",
              pub_date="2026-10-16", url="u", extra={"pmcid": "PMC13666002", "is_open_access": "Y"})
    assert europepmc.attach_fulltext(_XmlHttp(JATS_WITH_ABSTRACT), p) is True
    assert p.abstract.startswith("Abstract In most cells, Malat1") and "sequenced on a NovaSeq 6000" in p.sections["methods"]
    # an abstract the search record already had is never overwritten
    p2 = Paper(id="europepmc:MED:2", source="europepmc", kind="article", title="T", doi=None, abstract="Original.", journal="J",
               pub_date="2026-10-16", url="u", extra={"pmcid": "PMC2", "is_open_access": "Y"})
    europepmc.attach_fulltext(_XmlHttp(JATS_WITH_ABSTRACT), p2)
    assert p2.abstract == "Original."
    # the bioRxiv JATS path does the same
    p3 = Paper(id="doi:10.64898/1", source="biorxiv", kind="preprint", title="T", doi="10.64898/1", abstract="", journal="biorxiv",
               pub_date="2026-10-16", url="u", fulltext_url="https://www.biorxiv.org/content/x.source.xml")
    assert biorxiv.attach_fulltext(_XmlHttp(JATS_WITH_ABSTRACT), p3) is True and p3.abstract.startswith("Abstract In most cells")


def test_biorxiv_by_doi_lookup_logs_at_debug_not_info(caplog):
    import logging

    record = {"collection": [{"doi": "10.64898/2026.04.16.718906", "title": "T", "version": "1", "date": "2026-04-16", "category": "bioinformatics",
                              "authors": "A B", "server": "biorxiv"}], "messages": [{"count": 1, "total": 1}]}
    with caplog.at_level(logging.DEBUG, logger="litradar.sources.biorxiv"):
        assert len(list(biorxiv.fetch(FakeHttp([record]), "", "", server="biorxiv"))) == 1
    assert not [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("1 version(s) kept for 10.64898/2026.04.16.718906" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="litradar.sources.biorxiv"):
        list(biorxiv.fetch(FakeHttp([record]), "2026-04-10", "2026-04-17", server="biorxiv"))
    assert any("biorxiv: 1 preprints in 2026-04-10..2026-04-17" in r.getMessage() for r in caplog.records)


def test_crossref_filters_on_created_date_and_falls_back_to_online_pub_date():
    empty = {"status": "ok", "message": {"items": [], "next-cursor": "c1"}}
    hit = {"status": "ok", "message": {"items": [{"DOI": "10.1016/x"}], "next-cursor": None}}
    http = FakeHttp([empty, hit])
    items = list(crossref.journal_works(http, "0092-8674", "2026-10-03", "2026-10-17", mailto="x@example.org"))
    assert [i["DOI"] for i in items] == ["10.1016/x"]
    assert http.calls[0][1]["filter"] == "from-created-date:2026-10-03,until-created-date:2026-10-17"
    assert http.calls[1][1]["filter"] == "from-online-pub-date:2026-10-03,until-online-pub-date:2026-10-17"
    assert "created" in http.calls[0][1]["select"] and "published-online" in http.calls[0][1]["select"]
    # a non-empty created-date answer is not followed by the fallback
    http = FakeHttp([hit])
    assert len(list(crossref.journal_works(http, "0092-8674", "2026-10-03", "2026-10-17"))) == 1 and len(http.calls) == 1


def test_crossref_pub_date_prefers_full_dates_and_uses_created_for_month_precision():
    full = {"published": {"date-parts": [[2026, 10, 13]]}, "created": {"date-time": "2026-10-01T00:00:00Z"}}
    assert crossref.pub_date(full) == "2026-10-13"
    month = {"published": {"date-parts": [[2026, 10]]}, "created": {"date-time": "2026-10-15T14:39:56Z", "date-parts": [[2026, 10, 15]]}}
    assert crossref.pub_date(month) == "2026-10-15"
    online = {"published": {"date-parts": [[2026, 10]]}, "published-online": {"date-parts": [[2026, 10, 9]]}, "created": {"date-time": "2026-10-15T14:39:56Z"}}
    assert crossref.pub_date(online) == "2026-10-09"
    assert crossref.pub_date({"published": {"date-parts": [[2026, 10]]}}) == "2026-10-01"
    assert crossref._date({"date-parts": [[None]]}) == "" and crossref._date({"date-parts": [["2026", "10"]]}) == "2026-10-01"
    p = crossref.to_paper({"DOI": "10.1016/j.cell.2026.09.065", "title": ["T"], "published": {"date-parts": [[2026, 10]]}, "created": {"date-time": "2026-10-14T19:54:44Z"}, "ISSN": ["0092-8674", "1097-4172"], "type": "journal-article"})
    assert p.pub_date == "2026-10-14" and p.extra["issn"] == ["0092-8674", "1097-4172"] and p.extra["published_parts"] == [2026, 10] and p.extra["created"] == "2026-10-14"


def test_ena_date_bound_includes_the_last_day_and_rows_are_deduplicated():
    from litradar.sources import ena

    assert ena.exclusive_end("2026-10-15") == "2026-10-16" and ena.exclusive_end("2026-12-31") == "2027-01-01"
    assert ena.date_query("2026-10-15", "2026-10-15") == 'first_public>="2026-10-15" AND first_public<"2026-10-16"'
    assert ena.date_query("2026-10-10", "2026-10-17", quoted=False) == "first_public>=2026-10-10 AND first_public<2026-10-18"
    rows = [
        {"experiment_accession": "ERX1", "run_accession": "ERR1", "instrument_model": "Illumina NovaSeq X", "first_public": "2026-10-15"},
        {"experiment_accession": "ERX1", "run_accession": "ERR2", "instrument_model": "Illumina NovaSeq X", "first_public": "2026-10-15"},
        {"experiment_accession": "ERX2", "run_accession": "ERR3", "instrument_model": "PacBio Revio", "first_public": "2026-10-15"},
    ]
    http = FakeHttp([rows])
    recs = list(ena.fetch(http, "2026-10-15", "2026-10-15"))
    assert [r.accession for r in recs] == ["ERX1", "ERX2"]
    assert http.calls[0][1]["query"] == 'first_public>="2026-10-15" AND first_public<"2026-10-16"'


def test_europepmc_preprint_server_license_and_open_access_gate():
    rec = {"id": "PPR1344081", "source": "PPR", "doi": "10.64898/2026.10.14.759099", "title": "A preprint", "firstPublicationDate": "2026-10-14",
           "bookOrReportDetails": {"publisher": "bioRxiv", "yearOfPublication": 2026}, "journalInfo": None, "isOpenAccess": "N", "license": None}
    p = europepmc.to_paper(rec)
    assert p.kind == "preprint" and p.journal == "bioRxiv" and p.license == "" and p.extra["is_open_access"] == "N"
    assert europepmc.preprint_server({"bookOrReportDetails": None, "journalInfo": {"journal": {"title": "Research Square"}}}) == "Research Square"
    assert europepmc.to_paper({"id": "1", "source": "PPR", "title": "x"}).journal == "preprint"
    art = {"id": "2", "source": "MED", "pmcid": "PMC999", "title": "Art", "journalInfo": {"journal": {"title": "Cell", "issn": "0092-8674", "essn": "1097-4172", "medlineAbbreviation": "Cell"}},
           "isOpenAccess": "Y", "license": "cc by"}
    q = europepmc.to_paper(art)
    assert q.license == "cc by" and q.extra["license"] == "cc by" and q.extra["issn"] == ["0092-8674", "1097-4172"] and europepmc.is_open_access(q) is True
    # not open access -> no request is made (FakeHttp has no .get, so a request would raise)
    closed = europepmc.to_paper({**art, "isOpenAccess": "N", "license": None})
    assert europepmc.is_open_access(closed) is False and europepmc.attach_fulltext(FakeHttp([]), closed) is False
    assert europepmc.is_open_access(europepmc.to_paper({"id": "3", "source": "MED", "pmcid": "PMC1", "title": "t"})) is None


def test_openalex_repository_deposits_are_preprints_only_when_the_repository_is_a_preprint_server():
    """Seen live: Figshare, Zenodo, wwPDB and EMPIAR deposits (and PubMed mirrors) were all labelled preprints because
    their primary location is a repository, and a same-day Figshare publisher deposit was then printed as 'the preprint
    of' the article it belongs to."""
    def work(doi, src_type, src_name, work_type="article"):
        return {"id": "https://openalex.org/W1", "doi": f"https://doi.org/{doi}", "title": "Potential to improve early detection", "type": work_type,
                "publication_date": "2026-10-12", "primary_location": {"source": {"type": src_type, "display_name": src_name}}}
    assert openalex.to_paper(work("10.6084/m9.figshare.34339374", "repository", "Figshare")).kind == "article"
    assert openalex.to_paper(work("10.5281/zenodo.123", "repository", "Zenodo (CERN European Organization for Nuclear Research)")).kind == "article"
    assert openalex.to_paper(work("10.2210/pdb9abc/pdb", "repository", "Worldwide Protein Data Bank")).kind == "article"
    assert openalex.to_paper(work("10.1101/2026.10.01.123456", "repository", "bioRxiv (Cold Spring Harbor Laboratory)")).kind == "preprint"
    assert openalex.to_paper(work("10.21203/rs.3.rs-1", "repository", "Research Square")).kind == "preprint"
    assert openalex.to_paper(work("10.64898/2026.10.05.756623", "repository", "")).kind == "preprint"  # the DOI alone says bioRxiv
    assert openalex.to_paper(work("10.1038/s41586-026-1", "journal", "Nature", work_type="preprint")).kind == "preprint"  # OpenAlex's own type wins
    assert openalex.to_paper(work("10.1038/s41586-026-1", "journal", "Nature")).kind == "article"


def test_biorxiv_preprint_is_dated_by_its_first_posting_not_the_version_listed():
    """The API lists every version with that version's date; a weekly pull that only sees v2 of a December preprint
    must not date it after its own journal version. The DOI encodes the first posting."""
    page = {"messages": [{"status": "ok", "count": 2, "total": 2}], "collection": [
        {"doi": "10.64898/2025.12.05.691730", "title": "AlphaFold3 and disordered proteins", "authors": "A, B", "date": "2026-10-13", "version": "2",
         "category": "bioinformatics", "license": "cc_by", "jatsxml": "", "abstract": "abs", "published": "10.1002/prot.70189", "server": "bioRxiv"},
        {"doi": "10.1101/2026.10.12.654321", "title": "Fresh", "authors": "C", "date": "2026-10-12", "version": "1",
         "category": "genomics", "license": "cc_by", "jatsxml": "", "abstract": "abs", "published": "NA", "server": "bioRxiv"},
    ]}
    papers = {p.doi: p for p in biorxiv.fetch(FakeHttp([page]), "2026-10-10", "2026-10-17")}
    revised = papers["10.64898/2025.12.05.691730"]
    assert revised.pub_date == "2025-12-05" and revised.extra["version_date"] == "2026-10-13" and revised.extra["version"] == "2"
    assert revised.published_doi == "10.1002/prot.70189"
    fresh = papers["10.1101/2026.10.12.654321"]
    assert fresh.pub_date == "2026-10-12" and fresh.extra["version_date"] == "2026-10-12"
