"""Company discovery: pattern extraction on fixtures, domain classification, scoring, status workflow."""
import json
from pathlib import Path

import pytest

from litradar import discovery as d
from litradar.config import load_yaml
from litradar.extract.gazetteer import Gazetteer
from litradar.models import Author, Mention, NewsItem, Paper
from litradar.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def gz():
    return Gazetteer.from_yaml(load_yaml(ROOT / "config" / "companies.yaml"), "company")


def _fixture_lines(name):
    for raw in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        if raw.strip() and not raw.startswith("#"):
            yield raw


# ---- (b) statements -----------------------------------------------------------------------------
def test_coi_fixture_true_positives(gz):
    rows = list(_fixture_lines("discovery_coi_positive.txt"))
    assert len(rows) >= 10
    for row in rows:
        expected, paragraph = row.split("\t", 1)
        found = {d.normalize_name(n) for n, _, _ in d.extract_statement_names(paragraph, gz)}
        for exp in expected.split(" || "):
            assert d.normalize_name(exp) in found, f"{exp!r} missing from {found} for: {paragraph[:80]}"


def test_coi_fixture_traps_yield_nothing(gz):
    rows = list(_fixture_lines("discovery_coi_traps.txt"))
    assert len(rows) >= 10
    for paragraph in rows:
        hits = d.extract_statement_names(paragraph, gz)
        assert hits == [], f"trap produced {hits} for: {paragraph[:80]}"


def test_statement_details(gz):
    hits = d.extract_statement_names("C.O.W. is a shareholder of and consultant for Proteolyze Therapeutics, Inc. The other authors declare no competing interests.", gz)
    assert [(n, c) for n, c, _ in hits] == [("Proteolyze Therapeutics, Inc.", True)]
    assert "consultant for Proteolyze" in hits[0][2]
    # a bare name is kept at low confidence; the known company in the same sentence is dropped
    hits = d.extract_statement_names("J.D.H. and C.M. are employees of Thermo Fisher Scientific. S.A. is currently employed by Meta.", gz)
    assert [(n, c) for n, c, _ in hits] == [("Meta", False)]
    # list continuation accepts only suffixed names, so author initials never leak
    hits = d.extract_statement_names("MRZ was previously employed by IBM Research, and MMD is currently employed by IBM Research.", gz)
    assert [n for n, _, _ in hits] == ["IBM Research"]
    # a programme name that happens to contain a sector word is not a company
    assert d.extract_statement_names("This research was financially supported by the Science and Technology Support Project of the Department of Science.", gz) == []
    assert d.extract_statement_names("This work was funded by the Caltech Bioinformatics Resource Center and the IGVF Consortium.", gz) == []
    # adjectives before a generic lower-case noun are not companies
    assert d.extract_statement_names("Grants funded by the Korean government (MSIT) supported this work.", gz) == []
    # longer names keep their sector tail and their legal tail
    names = [n for n, _, _ in d.extract_statement_names("Co-founder and holds equity in BridgeBio Oncology Therapeutics, Tatara Therapeutics. A.M.T. reports grants from OBI Pharma USA Inc., Macrogenics.", gz)]
    assert "BridgeBio Oncology Therapeutics" in names and "Tatara Therapeutics" in names and "OBI Pharma USA Inc." in names
    # "A and B" before the first suffix is a list, not one name; both items come through
    hits = d.extract_statement_names("He is a shareholder in GEn1E Lifesciences and Interline Therapeutics.", gz)
    assert [(n, c) for n, c, _ in hits] == [("GEn1E Lifesciences", True), ("Interline Therapeutics", True)]
    # sentence periods are dropped, abbreviation periods are kept
    names = [n for n, _, _ in d.extract_statement_names("Y-FW is employed by AHEAD Medicine Corporation. DM was employed by Enhanced Pharmacodynamics, LLC. The remaining authors have equity interest in Biodepot LLC. The terms were reviewed.", gz)]
    assert names == ["AHEAD Medicine Corporation", "Enhanced Pharmacodynamics, LLC", "Biodepot LLC"]


# ---- names ------------------------------------------------------------------------------------
def test_normalize_suffix_generic():
    assert d.normalize_name("Proteolyze Therapeutics, Inc.") == "proteolyzetherapeutics"
    assert d.normalize_name("ILLUMINA, INC.") == d.normalize_name("Illumina") == "illumina"
    assert d.normalize_name("Merck & Co., Inc.") == "merck"
    assert d.normalize_name("costellomedical.com") == d.normalize_name("Costello Medical") == "costellomedical"
    assert d.normalize_name("Acme Therapeutics") != d.normalize_name("Acme Diagnostics")
    assert d.has_corporate_suffix("Beijing WellGene Company, Ltd") and d.has_corporate_suffix("Helix Spatial Biosciences")
    assert not d.has_corporate_suffix("Meta")
    for generic in ("National Institutes of Health", "University of Washington", "Wellcome Trust", "Deutsche Forschungsgemeinschaft",
                    "Cancer Research UK", "NIH", "the company", "Klaus Tschira Stiftung", "Guangdong Basic and Applied Basic Research Foundation"):
        assert d.is_generic_name(generic), generic
    for company in ("Lactiga US Inc.", "Soufflé Therapeutics", "IBM Research", "Prime Medicine", "Exact Sciences", "1stBiome ApS"):
        assert not d.is_generic_name(company), company
    assert d.slugify("Proteolyze Therapeutics, Inc.") == "proteolyze-therapeutics-inc"


# ---- (a) domains ------------------------------------------------------------------------------
@pytest.mark.parametrize("domain,expected", [
    ("lactiga.com", "corporate"), ("acme.bio", "corporate"), ("example.co.uk", "corporate"), ("example.com.cn", "corporate"),
    ("rd.nestle.com", "corporate"), ("ibm.com", "corporate"),
    ("stanford.edu", "excluded_tld"), ("cam.ac.uk", "excluded_tld"), ("nih.gov", "excluded_tld"), ("biohub.org", "excluded_tld"),
    ("inl.int", "excluded_tld"), ("icmr.gov.in", "excluded_tld"), ("army.mil", "excluded_tld"),
    ("gmail.com", "public_mail"), ("vip.163.com", "public_mail"), ("qq.com", "public_mail"), ("outlook.de", "public_mail"),
    ("yahoo.co.jp", "public_mail"), ("protonmail.com", "public_mail"), ("icloud.com", "public_mail"), ("sina.com", "public_mail"),
    ("oup.com", "publisher"),
    ("charite.de", "academic"), ("med.uni-heidelberg.de", "academic"), ("zs-hospital.sh.cn", "academic"), ("smithlab.com", "academic"),
    ("unil.ch", "cctld"), ("itm-oncolab.cn", "cctld"),
    ("v1.0_custom", "invalid"), ("", "invalid"),
])
def test_classify_domain(domain, expected):
    assert d.classify_domain(domain) == expected


def test_display_name_from_affiliations():
    affs = ["M. G. DeGroote Institute for Infectious Disease Research, McMaster University", "Lactiga US Inc., 675 US-1, North Brunswick, NJ"]
    assert d.display_name_from_affiliations("lactiga.com", affs) == "Lactiga US Inc"
    assert d.display_name_from_affiliations("merck.com", ["Discovery Biologics, Merck & Co., Inc."]) == "Merck & Co, Inc"
    # the legal entity beats a shorter brand mention
    assert d.display_name_from_affiliations("rd.nestle.com", ["Société des Produits Nestlé SA, Nestlé Research"]) == "Société des Produits Nestlé SA"
    assert d.display_name_from_affiliations("acme.com", ["Department of Biology, Some University"]) is None


def test_email_evidence_rules(gz):
    base = {"id": "doi:10.1/x", "title": "t", "url": "", "pub_date": "2026-10-12", "first_seen": "2026-10-17T00:00:00Z", "sections_json": "{}"}
    # brand corroborated by an affiliation -> confident, display from the affiliation
    p = dict(base, extra_json=json.dumps({"emails": ["a@lactiga.com", "b@mcmaster.ca", "c@gmail.com"]}), authors_json="[]",
             affiliations_json=json.dumps(["McMaster University", "Lactiga US Inc., North Brunswick, NJ"]))
    ev = d.email_evidence(p, gz)
    assert [(e.name, e.confident, e.domain) for e in ev] == [("Lactiga US Inc", True, "lactiga.com")]
    # known gazetteer domain and academic .edu are skipped; a .com with academic-only affiliations is low confidence
    p = dict(base, extra_json=json.dumps({"emails": ["x@illumina.com", "y@stanford.edu", "z@ibm.com"]}), authors_json="[]",
             affiliations_json=json.dumps(["Hebrew University of Jerusalem"]))
    ev = d.email_evidence(p, gz)
    assert [(e.name, e.confident) for e in ev] == [("ibm.com", False)]
    # a country-code domain needs a legal-entity affiliation
    p = dict(base, extra_json=json.dumps({"emails": ["q@fiocruz.br", "r@draeger.de"]}), authors_json="[]",
             affiliations_json=json.dumps(["Fiocruz, Rio de Janeiro", "Draeger Medical Deutschland GmbH, Lübeck"]))
    ev = d.email_evidence(p, gz)
    assert [e.name for e in ev] == ["Draeger Medical Deutschland GmbH"]
    # a hospital with a vanity .com is not a company
    p = dict(base, extra_json=json.dumps({"emails": ["s@narayananethralaya.com"]}), authors_json="[]",
             affiliations_json=json.dumps(["GROW Research Laboratory, Narayana Nethralaya Foundation"]))
    assert d.email_evidence(p, gz) == []


# ---- (d) news ---------------------------------------------------------------------------------
def test_news_parsers(gz):
    assert d.parse_nih_reporter("Abstract text.\nR43 | 1R43GM123456-01 | ACME GENOMICS, INC. (CARSON, CA) | $299,000 | NIGMS | FY2026") == ("Acme Genomics, Inc", "R43", True)
    assert d.parse_nih_reporter("S10 | 1S10OD012345-01 | EXAMPLE UNIVERSITY (SPRINGFIELD, IL) | $1,371,730 | OD | FY2026") == ("Example University", "S10", False)
    assert d.parse_nih_reporter("") is None
    ct = d.parse_clinicaltrials("Sponsor: Example Pharmaceutical Group Co., Ltd. (Industry). Collaborators: Example Health, Inc.; University Hospital A. Interventions: Drug: X. Status: Recruiting.")
    assert ct[0] == ("Example Pharmaceutical Group Co., Ltd.", "sponsor (industry)", True)
    assert ("Example Health, Inc", "collaborator", True) in ct and ("University Hospital A", "collaborator", False) in ct
    assert d.parse_clinicaltrials("Sponsor: Hospital Central do Funchal (Other). Type: Observational.")[0][2] is False
    assert d.parse_fda_applicant("Drainage Catheter — Canyon Medical, Inc.") == "Canyon Medical, Inc."
    assert d.parse_fda_applicant("Drainage Catheter", "510(k) K254179 · applicant Canyon Medical, Inc., Nanjing, CN · received 2025-12-23") == "Canyon Medical, Inc."
    assert d.parse_press_headline("Microbiome Medicines Firm Microbiotica Raises £36M - GenomeWeb") == ["Microbiotica"]
    assert d.parse_press_headline("Soufflé Therapeutics Raises $135M Series B to Fund RNA Pipeline - GenomeWeb") == ["Soufflé Therapeutics"]
    assert d.parse_press_headline("Why OMass’ $310M series C marks a milestone for the biotech - Fierce Biotech") == ["OMass"]
    assert d.parse_press_headline("France’s ABL Texcell Acquires Precision Oncology Developer IntegraGen - GenomeWeb") == ["ABL Texcell", "IntegraGen"]
    assert d.parse_press_headline("Parkview Health announces agreement to acquire Ovation Healthcare") == []  # no sector word
    assert d.parse_press_headline("Over $4 Million Raised at a Children's Hospital gala for genomics") == []
    assert d.parse_press_headline("New England Biolabs Secures Rights to License a Transposase Enzyme for sequencing") == ["New England Biolabs"]
    assert d.parse_press_headline("Biogen-backed Estuary raises $50M Series A for genomics") == ["Estuary"]
    for bad in ("MCIU/AEI/10.13039/501100011033", "P30AG059298", "PRIN 2020", "2PT", "UK MRC", "Mass Spectrometry Core", "Vanier Canada Graduate Scholarships", "Fonds"):
        assert not d._accept_name(bad, gz), bad
    assert d._accept_name("Novo Nordisk A/S", None) and d._accept_name("3P Instruments GmbH", gz)

    def _item(feed, title, summary, iid="n1"):
        return {"id": iid, "feed": feed, "title": title, "url": "", "published": "2026-10-15", "summary": summary, "first_seen": ""}

    ev = d.news_evidence(_item("nih-reporter", "Rapid assay", "A.\nR43 | 1R43GM1-01 | ACME GENOMICS, INC. (CARSON, CA) | $1 | NIGMS | FY2026"), gz)
    assert [(e.name, e.evidence_type) for e in ev] == [("Acme Genomics, Inc", "news:nih-reporter")]
    assert d.news_evidence(_item("nih-reporter", "Core", "S10 | 1S10OD1-01 | EXAMPLE UNIVERSITY (X, IL) | $1 | OD | FY2026"), gz) == []
    ev = d.news_evidence(_item("clinicaltrials", "Trial", "Sponsor: Example Diagnostics, Inc. (Industry). Collaborators: Illumina, Inc.; University Hospital A."), gz)
    assert [e.name for e in ev] == ["Example Diagnostics, Inc."]  # known company and hospital collaborator dropped
    ev = d.news_evidence(_item("fda-510k", "Drainage Catheter — Canyon Medical, Inc.", "510(k) K1"), gz)
    assert [(e.name, e.evidence_type) for e in ev] == [("Canyon Medical, Inc.", "news:fda")]
    ev = d.news_evidence(_item("GenomeWeb (via Google News)", "FDA Launches Rare Disease Drug Development Framework - GenomeWeb", ""), gz)
    assert ev == []
    ev = d.news_evidence(_item("GenomeWeb (via Google News)", "Veracyte Nabs Medicare Reimbursement for Prosigna Breast Cancer Test - GenomeWeb", ""), gz)
    assert ev == []  # gazetteer-known


# ---- scoring ----------------------------------------------------------------------------------
def _ev(name, etype, ref, date="2026-10-12", confident=True, domain=""):
    return d.Evidence(name=name, evidence_type=etype, snippet="s", ref=ref, date=date, confident=confident, domain=domain)


def test_scoring_components_and_monotonicity():
    today = "2026-10-17"
    single_low = d.Candidate("meta", "Meta", [_ev("Meta", "coi", "p1", confident=False)])
    single_suffix = d.Candidate("acmebio", "Acme Bio", [_ev("Acme Bio", "coi", "p1")])
    two_refs = d.Candidate("acmebio", "Acme Bio", [_ev("Acme Bio", "coi", "p1"), _ev("Acme Bio", "coi", "p2")])
    two_types = d.Candidate("acmebio", "Acme Bio", [_ev("Acme Bio", "coi", "p1"), _ev("Acme Bio", "email_domain", "p2", domain="acme.bio")])
    stale = d.Candidate("acmebio", "Acme Bio", [_ev("Acme Bio", "coi", "p1", date="2026-01-01")])
    s_low, s_suf, s_two, s_types, s_stale = (d.score_candidate(c, today) for c in (single_low, single_suffix, two_refs, two_types, stale))
    assert s_low == 1.0  # 0.5 ref + 1.0 recency - 0.5 nothing confident
    assert s_suf == 2.5  # + suffix
    assert s_two == 3.0 and s_types == 4.5  # + ref ; + ref + type + high-precision confident signal
    assert s_stale == 1.5  # no recency bonus
    assert s_low < s_suf < s_two < s_types
    # coi / funding / ack are one family: the same sentence stored twice earns no "two signals" bonus
    dup = d.Candidate("fonds", "Fonds", [_ev("Fonds", "funding", "p1", confident=False), _ev("Fonds", "ack", "p1", confident=False)])
    assert d.score_candidate(dup, today) == 1.0
    grouped = d.group_candidates([_ev("Costello Medical", "coi", "p1"), _ev("costellomedical.com", "email_domain", "p1", domain="costellomedical.com"),
                                  _ev("Ipsen", "coi", "p2"), _ev("ipsen.com", "email_domain", "p3", domain="ipsen.com")])
    assert set(grouped) == {"costellomedical", "ipsen"}
    assert grouped["costellomedical"].display_name == "Costello Medical" and grouped["costellomedical"].domains == ["costellomedical.com"]
    assert grouped["ipsen"].refs == 2


# ---- store workflow ---------------------------------------------------------------------------
def _paper(i, **kw):
    base = dict(
        id=f"doi:10.1000/disc{i}", source="europepmc", kind="article", title=f"Paper {i}", doi=f"10.1000/disc{i}",
        abstract="abstract", journal="J", pub_date="2026-10-12", url=f"https://example.org/{i}", authors=[Author(name="A B")],
        affiliations=["Some University"], sections={"methods": "m"},
    )
    base.update(kw)
    return Paper(**base)


def _seed(tmp_path):
    st = Store(tmp_path / "d.sqlite")
    st.upsert_paper(_paper(1, sections={"coi": "C.O.W. is a shareholder of and consultant for Proteolyze Therapeutics, Inc."},
                           extra={"emails": ["cow@proteolyze.com"]}, affiliations=["Proteolyze Therapeutics, Inc., Boston, MA", "Some University"]))
    st.upsert_paper(_paper(2, sections={"funding": "This work was supported by Lactiga US Inc. and the National Institutes of Health."}))
    st.upsert_paper(_paper(3, sections={"coi": "L.H.H. has equity interest in Proteolyze Therapeutics. J.S. is an employee of Illumina, Inc."}, pub_date="2026-10-14"))
    st.upsert_paper(_paper(4, sections={"ack": "We thank the Wellcome Trust."}, pub_date="2026-01-05"))  # outside the window
    st.replace_mentions("doi:10.1000/disc2", [Mention("doi:10.1000/disc2", "company", "unlisted:acme-spatial-inc", "Acme Spatial Inc", "", "", "openalex-company", "affiliations", 1, "Acme Spatial Inc")])
    st.upsert_news([
        NewsItem("nih-reporter:1", "nih-reporter", "Rapid assay", "u", "2026-10-15", "A.\nR43 | 1R43GM1-01 | ACME SPATIAL, INC. (CARSON, CA) | $1 | NIGMS | FY2026"),
        NewsItem("ctgov:NCT1", "clinicaltrials", "Trial", "u", "2026-10-15", "Sponsor: Example Diagnostics, Inc. (Industry). Status: Recruiting."),
        NewsItem("fda:K1", "fda-510k", "Drainage Catheter — Canyon Medical, Inc.", "u", "2026-10-15", "510(k) K1"),
        NewsItem("press:1", "GenomeWeb (via Google News)", "Soufflé Therapeutics Raises $135M Series B to Fund RNA Pipeline - GenomeWeb", "u", "2026-10-15", ""),
    ])
    st.commit()
    return st


def test_refresh_status_transitions_and_promote(tmp_path, gz):
    st = _seed(tmp_path)
    rep = d.refresh(st, gz, "2026-10-01", "2026-10-17", today="2026-10-17")
    assert rep["candidates"] >= 6 and rep["new"] == rep["candidates"] and rep["removed"] == 0
    assert {"coi", "funding", "email_domain", "openalex_company", "news:nih-reporter", "news:clinicaltrials", "news:fda", "news:press"} <= set(rep["by_type"])
    rows = {r["name_norm"]: r for r in d.list_candidates(st, min_score=0)}
    assert "illumina" not in rows and "nationalinstitutesofhealth" not in rows and "wellcometrust" not in rows
    prot = rows["proteolyzetherapeutics"]
    assert prot["papers"] == 2 and prot["status"] == "new" and set(prot["types"]) == {"coi", "email_domain"}
    assert prot["domains"] == ["proteolyze.com"] and prot["first_seen"] == "2026-10-12" and prot["last_seen"] == "2026-10-14"
    assert rows["acmespatial"]["papers"] == 2 and set(rows["acmespatial"]["types"]) == {"openalex_company", "news:nih-reporter"}
    assert rows["acmespatial"]["score"] > rows["lactigaus"]["score"]  # two refs, two signal types
    assert rows["soufflétherapeutics".replace("é", "e")]["types"] == {"news:press": 1}
    assert rows["canyonmedical"]["types"] == {"news:fda": 1} and rows["examplediagnostics"]["types"] == {"news:clinicaltrials": 1}
    # --min-score hides weak ones, --since filters on last_seen
    assert all(r["score"] >= 2 for r in d.list_candidates(st, min_score=2))
    assert d.list_candidates(st, since="2026-10-15", min_score=0) and all(r["last_seen"] >= "2026-10-15" for r in d.list_candidates(st, since="2026-10-15", min_score=0))

    # ignore -> excluded from the default list, kept under --status ignored, survives a refresh
    row = d.set_status(st, "Lactiga US", "ignored")
    assert row["status"] == "ignored"
    assert "lactigaus" not in {r["name_norm"] for r in d.list_candidates(st, min_score=0)}
    assert "lactigaus" in {r["name_norm"] for r in d.list_candidates(st, min_score=0, statuses=("ignored",))}
    d.refresh(st, gz, "2026-10-01", "2026-10-17", today="2026-10-17")
    assert d.get_candidate(st, "lactigaus")["status"] == "ignored"
    # a stale `new` row (older rule, re-annotated paper) is dropped on the next refresh; decisions survive
    st.conn.execute("INSERT INTO candidate_companies (name_norm, display_name, evidence_json, first_seen, last_seen, papers, score, status) VALUES ('stalecorp','Stale Corp','{}','2026-10-12','2026-10-12',1,2.5,'new')")
    st.conn.execute("INSERT INTO candidate_companies (name_norm, display_name, evidence_json, first_seen, last_seen, papers, score, status) VALUES ('oldignored','Old Ignored','{}','2026-10-12','2026-10-12',1,2.5,'ignored')")
    st.commit()
    rep = d.refresh(st, gz, "2026-10-01", "2026-10-17", today="2026-10-17")
    assert rep["removed"] == 1 and d.get_candidate(st, "stalecorp") is None and d.get_candidate(st, "oldignored")["status"] == "ignored"
    # name resolution: substring, ambiguity, unknown
    assert d.get_candidate(st, "proteolyze")["name_norm"] == "proteolyzetherapeutics"
    assert d.get_candidate(st, "no-such-company") is None
    with pytest.raises(LookupError):
        d.set_status(st, "no-such-company", "ignored")
    with pytest.raises(ValueError):
        d.set_status(st, "proteolyze", "bogus")

    # promote without a track module -> yaml line, status promoted, promotion recorded
    res = d.promote(st, "proteolyze", ticker=None, type_="pharma", add_company=None)
    assert res["tracked"] is False and res["yaml"].startswith("- {id: proteolyze-therapeutics")
    assert "ticker: private" in res["yaml"] and "type: pharma" in res["yaml"] and "domains: [proteolyze.com]" in res["yaml"]
    assert d.get_candidate(st, "proteolyze")["status"] == "promoted"
    assert d.get_candidate(st, "proteolyze")["promotion"]["yaml"] == res["yaml"]
    d.refresh(st, gz, "2026-10-01", "2026-10-17", today="2026-10-17")
    assert d.get_candidate(st, "proteolyze")["status"] == "promoted" and d.get_candidate(st, "proteolyze")["promotion"]

    # promote with a track hook -> the entry is handed over
    calls = []
    res = d.promote(st, "acme spatial", ticker="ACME", type_="tools", add_company=lambda entry: calls.append(entry))
    assert res["tracked"] is True and calls[0]["ticker"] == "ACME" and calls[0]["id"] == "acme-spatial-inc"
    # reset back to new
    assert d.set_status(st, "acme spatial", "new")["status"] == "new"

    md = d.render_candidates_markdown(st, "2026-10-01", "2026-10-17", top=5)
    assert md.startswith("## Companies spotted (not yet tracked)")
    assert "Acme Spatial" in md and "Lactiga" not in md and "Proteolyze" not in md  # ignored / promoted are excluded
    assert "| Candidate | Score | Signals |" in md
    assert "_no new candidates" in d.render_candidates_markdown(st, "2030-01-01", "2030-01-02")


def test_yaml_line_quotes_special_characters():
    line = d.yaml_line({"id": "x", "name": "Acme: Bio, Inc.", "ticker": "", "type": "", "watch_news": False, "domains": ["acme.bio"], "aliases": ["Acme Bio"]})
    assert line == '- {id: x, name: "Acme: Bio, Inc.", ticker: private, type: tools, watch_news: false, domains: [acme.bio], aliases: [Acme Bio]}'


def test_sec_ticker_hint_offline(tmp_path):
    rows = [(d.normalize_name(t), tk, t) for tk, t in (("ILMN", "ILLUMINA, INC."), ("TXG", "10x Genomics, Inc."), ("RLAY", "Relay Therapeutics, Inc."), ("ARCT", "Arcturus Therapeutics Holdings Inc."))]
    assert d.sec_ticker_hint("Illumina", tickers=rows)["ticker"] == "ILMN"
    assert d.sec_ticker_hint("Relay Therapeutics", tickers=rows)["match"] == 1.0
    assert d.sec_ticker_hint("Arcturus Therapeutics", tickers=rows)["ticker"] == "ARCT"
    assert d.sec_ticker_hint("Proteolyze Therapeutics", tickers=rows) is None
    assert d.sec_ticker_hint("Meta", tickers=rows) is None  # too short to match fuzzily

    class FakeHttp:
        def get_json(self, url):
            assert url == d.SEC_TICKERS_URL
            return {"0": {"cik_str": 1, "ticker": "ILMN", "title": "ILLUMINA, INC."}}

    d._SEC_CACHE.clear()
    loaded = d.load_sec_tickers(http=FakeHttp(), cache_dir=tmp_path / ".cache")
    assert loaded == [("illumina", "ILMN", "ILLUMINA, INC.")]
    assert (tmp_path / ".cache" / "sec_company_tickers.json").exists()
    d._SEC_CACHE.clear()

    class Boom:
        def get_json(self, url):
            raise AssertionError("network must not be used when the cache is fresh")

    assert d.load_sec_tickers(http=Boom(), cache_dir=tmp_path / ".cache") == loaded
    d._SEC_CACHE.clear()


# ---- CLI --------------------------------------------------------------------------------------
def test_cli_candidates_roundtrip(tmp_path, monkeypatch, capsys):
    from litradar.cli import main

    st = _seed(tmp_path)
    st.close()
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "d.sqlite"))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    assert main(["candidates", "refresh", "--since", "2026-10-01", "--until", "2026-10-17"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["candidates"] >= 6
    assert main(["candidates", "--since", "2026-10-01", "--min-score", "0", "--evidence", "2"]) == 0
    out = capsys.readouterr().out
    assert "Proteolyze Therapeutics" in out and "[coi]" in out and "Lactiga US Inc" in out
    assert main(["candidates", "ignore", "lactiga"]) == 0
    assert "status -> ignored" in capsys.readouterr().out
    assert main(["candidates", "promote", "proteolyze", "--type", "pharma"]) == 0
    out = capsys.readouterr().out
    assert "status -> promoted" in out and "- {id: proteolyze-therapeutics" in out
    assert main(["candidates", "show", "proteolyze", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["status"] == "promoted" and row["promotion"]["entry"]["type"] == "pharma"
    assert main(["candidates", "--since", "2026-10-01", "--min-score", "0", "--json"]) == 0
    names = {r["name_norm"] for r in json.loads(capsys.readouterr().out)}
    assert "proteolyzetherapeutics" not in names and "lactigaus" not in names and "acmespatial" in names
    assert main(["candidates", "ignore", "no-such-company"]) == 1
    assert main(["candidates", "ignore"]) == 2
