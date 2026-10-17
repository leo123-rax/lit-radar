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
    assert "&lt;script&gt;" in out and "&#124;" in out and "&amp; amp" in out and "\\[link\\]\\(http://evil)" in out
    for s in ("# heading", "- item", "> quote", "* star", "+ plus", "#hash"):
        assert esc(s).startswith("\\"), s
    assert esc("a\nb\r\n  c") == "a b c"
    assert esc(None) == "" and esc(5) == "5" and esc("plain text") == "plain text"
    assert esc("&amp;") == "&amp;amp;"  # already-escaped input is not trusted either


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
    out = render(st, *WINDOW, {})
    md = out["markdown"]
    top = md[md.index("## Top"): md.index("## Platforms")]
    assert "The journal version title" in top and "Preprint title that is long enough" not in top.split("### Preprints")[0]
    assert "_Nature (also as preprint 2026-10-11), 2026-10-15 · score 6.0_" in top
    assert "### Preprints that appeared as journal articles this window" in top and "linked by the publisher" in top
    assert out["summary"]["top"][0]["preprints"][0]["id"] == pre.id
    assert out["summary"]["preprint_conversions"][0]["article_id"] == art.id
    st.close()
