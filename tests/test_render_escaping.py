"""The digest must be inert against hostile text from papers, LLM output, news and archives."""
import re

from litradar.digest.render import _link, esc, render, safe_url, split_companies_by_type
from litradar.models import ArchiveRecord, Author, Mention, NewsItem, Paper
from litradar.store import Store

HOSTILE = '<script>alert(1)</script> | pipe & amp [link](http://evil) # heading'
WINDOW = ("2026-10-10", "2026-10-17")


def test_esc_neutralises_html_tables_and_block_syntax():
    out = esc(HOSTILE)
    assert "<" not in out and ">" not in out and "|" not in out
    assert "&lt;script&gt;" in out and "&#124;" in out and "& amp" in out and "\\[link\\]\\(http://evil)" in out
    for s in ("# heading", "- item", "> quote", "* star", "+ plus", "#hash"):
        assert esc(s).startswith("\\"), s
    assert esc("a\nb\r\n  c") == "a b c"
    assert esc(None) == "" and esc(5) == "5" and esc("plain text") == "plain text"
    # a bare ampersand stays a bare ampersand (Markdown is fine with it; `&amp;` would leak into plain-text mail)
    assert esc("NEB enzymes & clean-up kits") == "NEB enzymes & clean-up kits"
    assert esc("&amp;") == "&amp;"  # an entity in the input renders as its character, never as markup
    assert esc("&lt;script&gt;") == "&lt;script&gt;"  # ... so pre-escaped tags stay visible text, not tags


def test_links_only_for_http_urls():
    assert _link("t", "javascript:alert(1)") == "t"
    assert _link("t", "data:text/html,x") == "t"
    assert _link("t", "ftp://x/y") == "t" and _link("t", "") == "t" and _link("t", None) == "t"
    assert _link("t", "https://example.org/a(b)") == "[t](https://example.org/a%28b%29)"
    assert _link("t", "http://example.org/x y") == "t"
    assert _link("t", "https://example.org/x<y") == "t"
    assert safe_url("HTTPS://EXAMPLE.ORG/") == "HTTPS://EXAMPLE.ORG/"
    assert _link("x" * 100, "https://e/1", max_len=10).startswith("[xxxxxxxxx…](")
    assert _link("", "https://e/1") == "[(untitled)](https://e/1)"
    assert _link("a | b", "https://e/1") == "[a &#124; b](https://e/1)"


def _hostile_store(tmp_path):
    st = Store(tmp_path / "h.sqlite")
    pid = "doi:10.1000/hostile"
    p = Paper(
        id=pid, source="europepmc", kind="article", title=HOSTILE + " title", doi="10.1000/hostile",
        abstract="Abstract <b>bold</b> | cell\n# not a heading", journal="Journal | <i>x</i>", pub_date="2026-10-12",
        url="javascript:alert(1)", authors=[Author(name="<img src=x onerror=alert(1)>")], affiliations=["Evil <Corp> | Inc"],
    )
    st.upsert_paper(p)
    st.replace_mentions(pid, [
        Mention(pid, "instrument", "txg-xenium", "Xenium <b>", "10x", "TXG", "spatial", "abstract", 1, "snippet | <x>"),
        Mention(pid, "company", "unlisted:evil-corp", "Evil <Corp> | Inc", "", "", "openalex-company", "affiliations", 1, "x"),
        Mention(pid, "company", "illumina", "Illumina", "", "ILMN", "", "methods", 1, "x"),
    ])
    llm = {"one_line": "<script>x</script> | one", "why_it_matters": "because [a](http://evil) & more", "novelty": "<b>5</b>",
           "commercial_implication": 3, "instrument_role": "central|", "clinical_readiness": "basic", "hype_risk": 1, "composite": 7.0}
    st.save_score(pid, 5.0, llm, 7.0, "watchlist: <x>")
    st.add_tracked_author_hit(pid, "<img src=x onerror=alert(1)>", "grp <b>", "name")
    st.upsert_news([
        NewsItem("n1", "feed <x>", "News <script>alert(1)</script> | title", "javascript:alert(1)", "2026-10-12", "", ["illumina"]),
        NewsItem("n2", "feed", "Other <b>headline</b>", "https://e/3", "2026-10-12", "", []),
        NewsItem("s1", "fda-510k", "Device <b> — Applicant | x", "https://e/2", "2026-10-12", "", ["illumina"]),
    ])
    st.upsert_archive_records([ArchiveRecord("sra", "SRX1", "SRP1", "Model <b>|x", "ILLUMINA", "WGS <i>", "2026-10-12")])
    st.record_run("2026-10-17T00:00:00Z", *WINDOW, {"counts": {"fetched.biorxiv <x>": 1}, "errors": ["<script>bad</script> | err"]})
    st.commit()
    return st


def _table_rows_are_rectangular(md: str) -> None:
    lines = md.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.startswith("|") and re.fullmatch(r"\|(?:---\|)+", lines[i + 1]):
            ncols = line.count("|")
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                assert lines[j].count("|") == ncols, lines[j]
                j += 1


def test_render_escapes_every_untrusted_field(tmp_path):
    st = _hostile_store(tmp_path)
    out = render(st, *WINDOW, {"digest": {"top_n": 5}}, company_types={"illumina": "tools"})
    md = out["markdown"]
    assert "<script" not in md and "<img" not in md and "<b>" not in md and "<i>" not in md
    assert "javascript:" not in md
    assert "](http://evil)" not in md and "\\[link\\]\\(http://evil)" in md  # bracket and paren both inert
    assert "\n# not a heading" not in md
    _table_rows_are_rectangular(md)
    assert "<sub>" in md  # our own markup survives
    assert "Xenium &lt;b&gt;" in md and "Evil &lt;Corp&gt; &#124; Inc" in md
    assert "&lt;script&gt;bad&lt;/script&gt; &#124; err" in md  # run errors are untrusted too
    assert "Model &lt;b&gt;&#124;x" in md and "WGS &lt;i&gt;" in md
    assert "&lt;img src=x onerror=alert(1)&gt;" in md  # tracked author name
    assert "**feed &lt;x&gt;**" not in md  # company id headings use the gazetteer id
    st.close()


def test_company_tables_split_by_type(tmp_path):
    st = Store(tmp_path / "c.sqlite")
    pid = "doi:10.1000/c1"
    st.upsert_paper(Paper(id=pid, source="x", kind="article", title="A paper about many companies in one", pub_date="2026-10-12", url="https://e/1"))
    st.replace_mentions(pid, [
        Mention(pid, "company", "illumina", "Illumina", "", "ILMN", "", "methods", 1, ""),
        Mention(pid, "company", "genentech", "Genentech", "Roche", "ROG.SW", "", "coi", 1, ""),
        Mention(pid, "company", "wuxi", "WuXi AppTec", "", "2359.HK", "", "funding", 1, ""),
        Mention(pid, "company", "broad-institute", "Broad Institute", "", "n/a", "", "affiliations", 1, ""),
        Mention(pid, "company", "openai", "OpenAI", "", "private", "", "methods", 1, ""),
        Mention(pid, "company", "mystery", "Mystery Co", "", "", "", "abstract", 1, ""),
    ])
    st.save_score(pid, 1.0, None, 1.0, "r")
    st.commit()
    types = {"illumina": "tools", "genentech": "pharma", "wuxi": "cro", "broad-institute": "institute", "openai": "software"}
    md = render(st, *WINDOW, {}, company_types=types)["markdown"]
    main = md[md.index("### Tools, diagnostics"): md.index("### Pharma and CROs")]
    pharma = md[md.index("### Pharma and CROs"): md.index("**Institutes:**")]
    assert "| Illumina |" in main and "| Mystery Co |" in main  # unknown type lands in the main table
    assert "Genentech" not in main and "| Genentech |" in pharma and "| WuXi AppTec |" in pharma
    assert "**Institutes:** Broad Institute (1; author 1)" in md
    assert "**AI tools and software used:** OpenAI (1; supplier 1)" in md
    assert "| OpenAI |" not in md and "| Broad Institute |" not in md
    groups = split_companies_by_type(st.company_counts(*WINDOW), types)
    assert [c["entity_id"] for c in groups["ai_tools"]] == ["openai"] and [c["entity_id"] for c in groups["institutes"]] == ["broad-institute"]
    assert {c["entity_id"] for c in groups["pharma"]} == {"genentech", "wuxi"}
    # without the mapping everything is in the main table
    md2 = render(st, *WINDOW, {})["markdown"]
    assert "| OpenAI |" in md2 and "| Genentech |" in md2[md2.index("### Tools"): md2.index("### Pharma")] and "**AI tools" not in md2
    st.close()


def test_top_list_shows_journal_version_and_marks_the_preprint(tmp_path):
    st = Store(tmp_path / "p.sqlite")
    pre = Paper(id="doi:10.1101/x", source="biorxiv", kind="preprint", title="Preprint title that is long enough to match", doi="10.1101/x",
                pub_date="2026-10-11", url="https://www.biorxiv.org/x", published_doi="10.1038/y")
    art = Paper(id="doi:10.1038/y", source="crossref", kind="article", title="The journal version title", doi="10.1038/y", journal="Nature",
                pub_date="2026-10-15", url="https://doi.org/10.1038/y")
    st.upsert_paper(pre)
    st.upsert_paper(art)
    st.save_score(pre.id, 6.0, None, 6.0, "r")
    st.save_score(art.id, 5.0, None, 5.0, "r")
    st.commit()
    # with ranking v2 off the stored scores are listed unchanged (rendered first: the v2 render below refreshes them)
    legacy = render(st, *WINDOW, {"ranking": {"v2": {"enabled": False}}})["markdown"]
    assert "_Nature (also as preprint 2026-10-11), 2026-10-15 · score 6.0_" in legacy
    out = render(st, *WINDOW, {})
    md = out["markdown"]
    top = md[md.index("## Top"): md.index("## Platforms")]
    assert "The journal version title" in top and "Preprint title that is long enough" not in top.split("### Preprints")[0]
    # ranking v2 recomputes the score at render time and tags the subgroup: the preprint takes Nature's tier through its
    # published version (5.0) plus the conversion point (1.0) = 6.0 of 16 raw -> 3.75, and the article inherits the
    # better of the two scores as before
    assert "_Nature (also as preprint 2026-10-11), 2026-10-15 · score 3.8 · other_" in top
    assert "### Preprints that appeared as journal articles this window" in top and "linked by the publisher" in top
    assert out["summary"]["top"][0]["preprints"][0]["id"] == pre.id
    assert out["summary"]["preprint_conversions"][0]["article_id"] == art.id
    st.close()


def _mention(pid, kind, eid, name, section, category="", count=1, snippet=""):
    return Mention(pid, kind, eid, name, "", "", category, section, count, snippet)


def test_platform_and_lab_tool_tables_have_separate_budgets_and_truncation_notes(tmp_path):
    st = Store(tmp_path / "i.sqlite")
    pid = "doi:10.1000/i1"
    st.upsert_paper(Paper(id=pid, source="x", kind="article", title="A paper naming many products", pub_date="2026-10-12", url="https://e/1"))
    st.replace_mentions(pid, [
        _mention(pid, "instrument", "kits", "QIAGEN kits", "methods", "library_prep", 5),
        _mention(pid, "instrument", "nanodrop", "NanoDrop", "methods", "qc", 4),
        _mention(pid, "instrument", "mystery", "Mystery tool", "methods", "", 3),
        _mention(pid, "instrument", "txg-xenium", "Xenium", "methods", "spatial", 2),
        _mention(pid, "instrument", "ilmn-novaseq-x", "NovaSeq X", "methods", "sequencer.short_read", 1),
    ])
    st.save_score(pid, 1.0, None, 1.0, "r")
    st.commit()
    md = render(st, *WINDOW, {"digest": {"platforms_table_n": 1, "lab_tools_table_n": 2}})["markdown"]
    plat = md[md.index("## Platforms named"): md.index("### Lab infrastructure")]
    tools = md[md.index("### Lab infrastructure"): md.index("## Instruments behind")]
    assert "| Xenium |" in plat and "| NovaSeq X |" not in plat and "| QIAGEN kits |" not in plat
    assert "… and 1 more platform(s) named in the window; the table shows the 1 with most papers (digest.platforms_table_n)." in plat
    assert "| QIAGEN kits |" in tools and "| NanoDrop |" in tools and "| Mystery tool |" not in tools
    assert "… and 1 more product line(s) named in the window" in tools
    out = render(st, *WINDOW, {})
    assert out["summary"]["platforms_total"] == 2 and out["summary"]["lab_tools_total"] == 3
    assert [r["entity_id"] for r in out["summary"]["platforms"]] == ["txg-xenium", "ilmn-novaseq-x"]
    assert len(out["summary"]["instruments"]) == 5 and "more platform(s)" not in out["markdown"] and "more product line(s)" not in out["markdown"]
    # the legacy single budget still caps the platform table
    assert "… and 1 more platform(s)" in render(st, *WINDOW, {"digest": {"instruments_table_n": 1}})["markdown"]
    st.close()


def test_top_list_falls_back_to_full_text_when_abstract_is_missing(tmp_path):
    st = Store(tmp_path / "f.sqlite")
    with_body = Paper(id="doi:10.1000/f1", source="europepmc", kind="article", title="Body paper", pub_date="2026-10-12", url="https://e/1",
                      sections={"body": "Introduction In most cells, a long noncoding RNA localizes to the nucleus. " + "More text. " * 60})
    with_methods = Paper(id="doi:10.1000/f2", source="europepmc", kind="article", title="Methods paper", pub_date="2026-10-12", url="https://e/2",
                         sections={"methods": "Cells were lysed in RIPA buffer. Lysates were then cleared by centrifugation."})
    bare = Paper(id="doi:10.1000/f3", source="crossref", kind="article", title="Bare paper", pub_date="2026-10-12", url="https://e/3")
    with_abstract = Paper(id="doi:10.1000/f4", source="crossref", kind="article", title="Normal paper", pub_date="2026-10-12", url="https://e/4", abstract="A real abstract.")
    for i, p in enumerate((with_body, with_methods, bare, with_abstract)):
        st.upsert_paper(p)
        st.save_score(p.id, 5.0 - i, None, 5.0 - i, "r")
    st.commit()
    md = render(st, *WINDOW, {})["markdown"]
    top = md[md.index("## Top"): md.index("## Platforms")]
    assert "In most cells, a long noncoding RNA localizes to the nucleus." in top and "<sub>(from full text; no abstract on record)</sub>" in top
    assert not any(line.startswith("Introduction In most") for line in top.splitlines())
    assert "Cells were lysed in RIPA buffer. <sub>(first Methods sentence; no abstract on record)</sub>" in top
    assert "Lysates were then cleared" not in top
    assert "A real abstract." in top and top.count("no abstract on record") == 2  # the bare paper gets no summary line
    body_entry = top[top.index("Body paper"): top.index("Methods paper")]
    assert len(body_entry) < 1200  # the fallback is cut like an abstract (~420 chars)
    st.close()


def test_top_list_company_line_separates_institutes_ai_tools_and_disclosures(tmp_path):
    st = Store(tmp_path / "c2.sqlite")
    pid = "doi:10.1000/c2"
    st.upsert_paper(Paper(id=pid, source="x", kind="article", title="A paper with many kinds of company mention", pub_date="2026-10-12", url="https://e/1"))
    st.replace_mentions(pid, [
        _mention(pid, "company", "illumina", "Illumina", "methods"),
        _mention(pid, "company", "profluent", "Profluent Bio", "coi"),
        _mention(pid, "company", "roche", "Roche", "methods"),
        _mention(pid, "company", "roche", "Roche", "coi"),
        _mention(pid, "company", "hhmi", "Howard Hughes Medical Institute", "funding"),
        _mention(pid, "company", "openai", "OpenAI", "ack"),
        _mention(pid, "company", "unlisted:some-corp", "Some Corp", "affiliations"),
    ])
    st.save_score(pid, 1.0, None, 1.0, "r")
    st.commit()
    md = render(st, *WINDOW, {}, company_types={"hhmi": "institute", "openai": "software"})["markdown"]
    line = md[md.index("<sub>Companies:"): md.index("</sub>", md.index("<sub>Companies:"))]
    assert "Companies: Illumina, Profluent Bio (COI), Roche" in line  # Roche is also a supplier: no tag
    assert "Institutes / funders: Howard Hughes Medical Institute (funding)" in line
    assert "AI tools: OpenAI (ack)" in line and "Some Corp" not in line
    md2 = render(st, *WINDOW, {})["markdown"]  # without types everything is a company
    assert "Companies: Howard Hughes Medical Institute (funding), Illumina, OpenAI (ack), Profluent Bio (COI), Roche" in md2
    st.close()


def test_news_display_names_coverage_labels_and_single_archive_placeholder(tmp_path):
    st = Store(tmp_path / "n.sqlite")
    pid = "doi:10.1000/n1"
    st.upsert_paper(Paper(id=pid, source="crossref", kind="article", title="A paper mentioning a pharma", pub_date="2026-10-12", url="https://e/1", fulltext_available=True))
    st.replace_mentions(pid, [_mention(pid, "company", "astrazeneca", "AstraZeneca", "coi")])
    st.save_score(pid, 1.0, None, 1.0, "r")
    st.upsert_news([NewsItem("n1", "feed", "Deal announced", "https://e/news", "2026-10-12", "", ["astrazeneca", "unknown-co"])])
    st.record_run("2026-10-09T00:00:00Z", "2026-10-01", "2026-10-08", {
        "counts": {"fetched.biorxiv": 12, "papers.unique": 10}, "errors": ["arxiv: host export.arxiv.org rate-limited (HTTP 429)"],
        "http": {"blocked_hosts": ["export.arxiv.org"]},
    })
    st.commit()
    md = render(st, *WINDOW, {}, company_names={"unknown-co": "Unknown Co"})["markdown"]
    news = md[md.index("## Trade press"): md.index("## Coverage and method")]
    assert "**AstraZeneca**" in news and "**Unknown Co**" in news and "**astrazeneca**" not in news and "**unknown-co**" not in news
    assert "_Rendered " in md and "every count is a snapshot of the database at that moment" in md
    cov = md[md.index("## Coverage and method"):]
    assert "**This window (2026-10-10 to 2026-10-17), counted in the database:** 1 papers, 1 with parsed full text; by source: crossref 1 (1 with full text)." in cov
    assert "**Last pipeline run** (finished 20" in cov  # record_run stamps the real finish time
    assert "UTC, window 2026-10-01 to 2026-10-08; that is NOT this digest's window, so these counters describe that run alone)." in cov
    assert "Records pulled per source in that run, before de-duplication: biorxiv 12." in cov
    assert "In that run: unique papers after de-duplication 10 (0 new to the database" in cov
    assert "Hosts skipped after repeated HTTP 429 in that run: export.arxiv.org." in cov
    assert "Source errors in that run (1): arxiv: host export.arxiv.org rate-limited (HTTP 429)." in cov
    # empty archive section: one placeholder, generic heading
    arch = md[md.index("## Instruments behind"): md.index("### Proteomics")]
    assert arch.count("_nothing in this window_") == 1 and "(NCBI SRA, ENA, GEO)" in arch
    st.close()
    # with only SRA rows the heading names only SRA, and a window with no run says so
    st2 = Store(tmp_path / "n2.sqlite")
    st2.upsert_archive_records([ArchiveRecord("sra", "SRX1", "SRP1", "Illumina NovaSeq X Plus", "ILLUMINA", "WGS", "2026-10-12", "", "", "", "ilmn-novaseq-x", "Illumina", "ILMN")])
    st2.commit()
    md2 = render(st2, *WINDOW, {})["markdown"]
    assert "## Instruments behind newly public sequencing data (NCBI SRA)\n" in md2
    assert "No pipeline run is recorded for this database yet." in md2
    st2.close()
