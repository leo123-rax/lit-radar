"""Emerging-concepts surfacer: extraction, burst scoring, end-to-end on a temp DB, CLI, optional LLM pass."""
from __future__ import annotations

import json
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

from litradar import concepts as C
from litradar.models import Mention, Paper
from litradar.store import Store

CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(scope="module")
def excl() -> C.Exclusions:
    return C.load_exclusions(CONFIG)


# ---- normalisation --------------------------------------------------------------------------
def test_normalize_term_unifies_case_dashes_and_plurals():
    assert C.normalize_term("scRNA-seq") == "scrna seq"
    assert C.normalize_term("Single–cell  Foundation Models") == "single cell foundation model"
    assert C.normalize_term("Lipid Nanoparticles") == "lipid nanoparticle"
    assert C.normalize_term("Base Editors") == "base editor"
    assert C.normalize_term("Antibody-Drug Conjugates (ADCs)") == "antibody drug conjugates adc"  # only the head is singularised
    assert C.normalize_term("proteomics") == "proteomics"  # -ics is not a plural
    assert C.normalize_term("spatial transcriptomic") == "spatial transcriptomics"  # adjective merges with the noun
    assert C.normalize_term("RNAseq") == C.normalize_term("RNA-seq") == "rna seq"
    assert C.normalize_term("scRNAseq") == "scrna seq"
    assert C.normalize_term("NovaSeq X Plus") == "novaseq x plus"  # product names are not split
    assert C.normalize_term("species") == "species"
    assert C.normalize_term("Parkinson's disease") == "parkinson disease"
    assert C.normalize_term("") == ""


def test_week_helpers():
    assert C.week_key("2026-10-17") == "2026-10-12"  # Saturday -> Monday
    assert C.week_key("2026-10-12") == "2026-10-12"
    wks = C.weeks_ending("2026-10-17", 3)
    assert wks == ["2026-09-28", "2026-10-05", "2026-10-12"]


# ---- extraction -----------------------------------------------------------------------------
def test_extraction_normal_text(excl):
    text = (
        "We present a single-cell foundation model trained on Perturb-seq data. Spatial transcriptomics of "
        "patient-derived organoids revealed base editing outcomes. Cell-free DNA (cfDNA) and circulating tumor DNA (ctDNA) "
        "were profiled with long-read sequencing; ionizable lipid nanoparticles delivered self-amplifying mRNA. "
        "An epigenetic clock built from CRISPR-Cas9 Base Editing screens. Antibody-drug conjugates (ADCs) and CAR-T cells."
    )
    out = C.extract_terms(text, excl)
    for want in (
        "single cell foundation model", "foundation model", "perturb seq", "spatial transcriptomics", "patient derived organoid",
        "base editing", "cell free dna", "cfdna", "circulating tumor dna", "ctdna", "long read sequencing",
        "lipid nanoparticle", "ionizable lipid nanoparticle", "self amplifying mrna", "epigenetic clock",
        "crispr cas9 base editing", "antibody drug conjugate", "car t cell",
    ):
        assert want in out, (want, sorted(out))
    # verbs and trailing words are not glued onto concepts
    assert "delivered self amplifying mrna" not in out
    assert "epigenetic clock built" not in out
    # display keeps the author's surface form
    assert out["perturb seq"] == "Perturb-seq"
    cands = C.extract_candidates("", text, "", excl)
    assert cands["circulating tumor dna"].channel == "acronym" and cands["circulating tumor dna"].acronym == "ctDNA"


def test_extraction_hostile_text_emits_nothing_wrong(excl):
    hostile = (
        "The And Of In 2023 2024 10 mM 37 °C p < 0.05 n = 12 (CI 1.2-3.4). Samples were sequenced on an Illumina NovaSeq X Plus "
        "and a Xenium In Situ instrument from 10x Genomics. The Visium HD platform. Proteomics. Sequencing. Economics of "
        "Hong Kong. Analysis Of The Results Showed Significant Improvement. The World Health Organization (WHO) and "
        "the confidence interval (CI). Oxford Nanopore MinION nanopore sequencing. HiFi sequencing on Revio. "
        "Hazard ratio (HR) 0.8; United States; Machine Learning; Deep Learning; 5 min; 100 kDa; 2.5-fold."
    )
    out = C.extract_terms(hostile, excl)
    forbidden = {
        "the and", "and of", "2023 2024", "10 mm", "illumina novaseq x plu", "novaseq x plus", "novaseq x plu", "xenium in situ", "10x genomics",
        "visium hd", "visium hd platform", "proteomics", "sequencing", "economics", "hong kong", "world health organization",
        "confidence interval", "nanopore sequencing", "hifi sequencing", "hazard ratio", "united states", "machine learning",
        "deep learning", "minion", "revio", "illumina",
    }
    assert not (set(out) & forbidden), set(out) & forbidden
    for key in out:
        toks = key.split()
        assert len(toks) >= 2 or key in C.SINGLETON_OK or key.endswith("seq"), key
        assert not any(C._is_numeric(t) for t in toks), key
        assert toks[0] not in C.STOP_WORDS and toks[-1] not in C.STOP_WORDS, key
    assert out == {}, out  # nothing in this blob is a concept


def test_title_case_titles_do_not_leak_every_capitalised_bigram(excl):
    title = "A Novel Approach To The Analysis Of Breast Cancer Trials And Optimal Sequence Of Treatment"
    out = C.extract_candidates(title, "", "", excl)
    assert "novel approach" not in out and "optimal sequence" not in out and "breast cancer" not in out
    # but a seed-anchored concept in a Title Case title survives
    out2 = C.extract_candidates("A Multimodal Foundation Model To Integrate Histology With Spatial Molecular Programs", "", "", excl)
    assert "multimodal foundation model" in out2 and "foundation model" in out2


def test_gazetteer_products_are_excluded_but_concepts_near_them_survive(excl):
    text = "Nanopore protein sequencing with solid-state nanopores; data were generated on a PromethION 24 and analysed with Dorado."
    out = C.extract_terms(text, excl)
    assert "nanopore protein sequencing" in out and "solid state nanopore" in out
    assert "promethion 24" not in out and "promethion" not in out and "dorado" not in out
    assert excl.blocks("novaseq x plus") and excl.blocks("illumina novaseq x plus") and excl.blocks("spatial xenium analysis")
    assert not excl.blocks("spatial transcriptomics") and not excl.blocks("organoid intelligence")


def test_acronym_long_form_resolution():
    assert C._best_long_form("ST", "profiling with spatial transcriptomics") == "spatial transcriptomics"
    assert C._best_long_form("ctDNA", "levels of circulating tumor DNA") == "circulating tumor DNA"
    assert C._best_long_form("LNPs", "ionizable lipid nanoparticles") == "lipid nanoparticles"
    assert C._best_long_form("XYZ", "nothing matches here") is None
    out = C.extract_terms("We measured minimal residual disease (MRD) with tumor-informed assays; the sample size (n) was 40.")
    assert "minimal residual disease" in out
    assert not any(k.startswith("sample size") for k in out)


def test_subgroup_overlap(excl):
    assert excl.subgroups_for("spatial transcriptomics") == ["spatial"]
    assert "gene_editing" in excl.subgroups_for("base editing")
    assert "ai_bio" in excl.subgroups_for("single cell foundation model")
    assert "perturbation" in excl.subgroups_for("genetic perturbation")  # via the subgroup name
    assert "long_read" not in excl.subgroups_for("genome editing")  # 'genome' alone is too weak
    assert "structural_biology" not in excl.subgroups_for("single cell rna")  # so is 'single' ("single-particle analysis")
    assert "single_cell" in excl.subgroups_for("single cell transcriptomics")  # shared bigram "single cell"
    assert "spatial" not in excl.subgroups_for("single cell transcriptomics")  # 'transcriptomics' alone is weak
    assert excl.subgroups_for("organoid intelligence") == []


# ---- burst scoring ----------------------------------------------------------------------------
def test_burst_scoring_on_synthetic_series():
    flat = C.score_term(current_total=8, baseline_series=[2] * 26, n_current_active=4, is_new=False)
    burst = C.score_term(current_total=24, baseline_series=[2] * 26, n_current_active=4, is_new=False)
    noisy = C.score_term(current_total=24, baseline_series=[0, 4] * 13, n_current_active=4, is_new=False)
    new = C.score_term(current_total=6, baseline_series=[0] * 26, n_current_active=4, is_new=True)
    old_zero = C.score_term(current_total=6, baseline_series=[0] * 26, n_current_active=4, is_new=False)
    assert abs(flat["z"]) < 0.01 and flat["expected"] == 8.0
    assert burst["z"] > 4 and burst["score"] == burst["z"]
    assert noisy["z"] < burst["z"]  # a noisier baseline earns a lower z for the same jump
    assert new["score"] == pytest.approx(old_zero["score"] + C.NOVELTY_BONUS)
    assert old_zero["z"] == pytest.approx(6 / 1.0, abs=0.01)  # sqrt(max(expected,1)) floor keeps zeros finite
    empty = C.score_term(5, [], 1, True)
    assert empty["baseline_mean"] == 0 and empty["z"] == 5.0


# ---- end to end on a temp DB ------------------------------------------------------------------
UNTIL = "2026-10-17"
BACKGROUND = [
    "Spatial transcriptomics of the mouse cortex at single-cell resolution",
    "A single-cell atlas of the developing human kidney built with snRNA-seq",
    "Base editing corrects a pathogenic variant in patient-derived organoids",
    "Long-read sequencing resolves structural variants in a rare-disease cohort",
    "Plasma proteomics identifies biomarkers of early cardiovascular disease",
    "CRISPR screens reveal regulators of T cell exhaustion",
]
PLANT = "Organoid intelligence: biocomputing with living neural tissue"


def _seed_db(path: Path, mentions: bool = True) -> Store:
    st = Store(path)
    end = date(2026, 10, 17)
    pid = 0
    for wk in range(30):  # week 0 is the oldest
        monday = C.week_start(end) - timedelta(weeks=29 - wk)
        day = (monday + timedelta(days=2)).isoformat()
        for k in range(2):  # 2 background papers / week = 60 papers
            pid += 1
            title = BACKGROUND[(wk + k) % len(BACKGROUND)]
            st.upsert_paper(Paper(id=f"doi:10.1000/bg{pid}", source="biorxiv", kind="preprint", title=title, doi=f"10.1000/bg{pid}",
                                  abstract=f"{title}. We describe the approach and its limitations.", journal="biorxiv", pub_date=day, url=f"https://example.org/bg{pid}"))
        if wk >= 26:  # planted burst: 2 papers / week in the current 4 weeks (8 total), none before
            for k in range(2):
                pid += 1
                ppid = f"doi:10.1000/plant{pid}"
                abstract = ("Organoid intelligence: brain organoids wired to electrodes learn a task." if k == 0
                            else "We train an organoid intelligence system on spatial transcriptomics readouts.")
                st.upsert_paper(Paper(id=ppid, source="biorxiv", kind="preprint", title=PLANT, doi=f"10.1000/plant{pid}",
                                      abstract=abstract, journal="biorxiv", pub_date=day, url=f"https://example.org/plant{pid}"))
                if mentions:
                    st.replace_mentions(ppid, [
                        Mention(ppid, "instrument", "txg-xenium", "Xenium", "10x Genomics", "TXG", "spatial", "abstract", 1, "..."),
                        Mention(ppid, "company", "10x-genomics", "10x Genomics", "", "TXG", "", "affiliations", 1, "..."),
                        Mention(ppid, "company", "unlisted:acme", "Acme Labs", "", "", "", "affiliations", 1, "..."),
                    ])
    st.commit()
    return st


def test_end_to_end_planted_burst(tmp_path):
    st = _seed_db(tmp_path / "radar.sqlite")
    excl = C.load_exclusions(CONFIG)
    stats = C.ensure_counts(st, UNTIL, weeks_total=30, excl=excl)
    assert stats["papers"] == 68 and len(stats["weeks"]) == 30
    # incremental: a second call only refreshes the latest two weeks
    again = C.ensure_counts(st, UNTIL, weeks_total=30, excl=excl)
    assert again["weeks"] == C.weeks_ending(UNTIL, 2)
    rows = st.conn.execute("SELECT COUNT(*), COUNT(DISTINCT week) FROM concept_counts").fetchone()
    assert rows[1] == 30
    report = C.burst_report(st, UNTIL, baseline_weeks=26, current_weeks=4, min_papers=5, top=10, excl=excl)
    assert report["baseline_active_weeks"] == 26 and report["weak_baseline"] is False
    top = report["risers"][0]
    assert top["term"] == "organoid intelligence", [r["term"] for r in report["risers"]]
    assert top["papers"] == 8 and top["is_new"] is True and top["baseline_mean"] == 0
    assert len(top["examples"]) == 3 and all(e["url"] and e["title"] == PLANT and e["venue"] == "biorxiv (preprint)" for e in top["examples"])
    assert [x["name"] for x in top["instruments"]] == ["Xenium"]
    assert [x["name"] for x in top["companies"]] == ["10x Genomics"]  # unlisted: entities are hidden
    # "new this month" lists first appearances that did not already make the risers table
    new_keys = {d["term"] for d in report["new_terms"]}
    assert "organoid intelligence" not in new_keys and "brain organoid" in new_keys and report["new_terms_total"] >= 2
    assert all(d["first_seen"] >= report["current_weeks"][0] for d in report["new_terms"])
    # a steady background term is not a riser
    steady = next(r for r in report["risers"] if r["term"] == "spatial transcriptomics")
    assert steady["score"] < top["score"] and "spatial" in steady["subgroups"]
    md = C.render_report_markdown(report, top=5)
    assert md.startswith("## Emerging concepts") and "Organoid intelligence" in md and "### New this month" in md
    # digest-section entry point (widens a one-week digest window to 4 weeks)
    section = C.render_concepts_markdown(st, "2026-10-11", UNTIL, top=5, config_dir=CONFIG)
    assert "## Emerging concepts" in section and "organoid" in section.lower()
    st.close()


def test_cli_concepts_json_and_markdown(tmp_path, monkeypatch, capsys):
    from litradar.cli import main

    db = tmp_path / "radar.sqlite"
    _seed_db(db).close()
    monkeypatch.setenv("LITRADAR_DB", str(db))
    assert main(["--config-dir", str(CONFIG), "concepts", "--until", UNTIL, "--weeks", "26", "--top", "5", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["risers"][0]["term"] == "organoid intelligence"
    assert data["counting"]["papers_scanned"] == 68
    out_md = tmp_path / "concepts.md"
    assert main(["--config-dir", str(CONFIG), "concepts", "--until", UNTIL, "--top", "5", "--out", str(out_md)]) == 0
    assert "report written" in capsys.readouterr().out
    assert "Organoid intelligence" in out_md.read_text(encoding="utf-8")


def test_cli_help_lists_concepts():
    import subprocess

    out = subprocess.run([sys.executable, "-m", "litradar", "--help"], capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "concepts" in out.stdout


# ---- optional LLM pass with a fake SDK ----------------------------------------------------------
class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Msg:
    def __init__(self, text, stop_reason="end_turn"):
        self.content, self.stop_reason = [_Block(text)], stop_reason


def _fake_anthropic(monkeypatch, responder):
    mod = types.ModuleType("anthropic")

    class _Err(Exception):
        pass

    class _Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            return responder(kw)

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()
            _Client.last = self

    mod.Anthropic = _Client
    mod.RateLimitError = _Err
    mod.APIStatusError = _Err
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod


def test_llm_pass_structured_output_and_storage(tmp_path, monkeypatch):
    def responder(kw):
        assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["format"]["type"] == "json_schema"
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
        terms = [line.split(" | ")[0].split(". ", 1)[1] for line in kw["messages"][0]["content"].splitlines() if line[:1].isdigit()]
        return _Msg(json.dumps({"concepts": [
            {"term": t, "definition": f"{t} defined", "status": "new_concept", "older_name": "", "vendor_categories": ["single_cell"],
             "commercial_relevance": 4, "rationale": "r"} for t in terms]}))

    mod = _fake_anthropic(monkeypatch, responder)
    cands = [
        {"term": "organoid intelligence", "display": "Organoid intelligence", "papers": 8, "first_seen": "2026-09-21", "examples": [{"title": PLANT}]},
        {"term": "spatial transcriptomics", "display": "spatial transcriptomics", "papers": 12, "first_seen": "2026-03-23", "examples": []},
    ]
    defs = C.define_concepts(cands, model="claude-opus-5-5", api_key="k")
    assert defs["organoid intelligence"]["definition"] == "Organoid intelligence defined"
    assert defs["spatial transcriptomics"]["commercial_relevance"] == 4
    assert len(mod.Anthropic.last.messages.calls) == 1  # one structured request for the whole list
    st = Store(tmp_path / "radar.sqlite")
    assert C.save_definitions(st, defs, {c["term"]: c["display"] for c in cands}, "claude-opus-5-5") == 2
    loaded = C.load_definitions(st, ["organoid intelligence", "missing"])
    assert set(loaded) == {"organoid intelligence"} and loaded["organoid intelligence"]["scored_at"]
    assert C.estimate_cost_usd(40, "claude-opus-5-5", use_batch=False) > C.estimate_cost_usd(40, "claude-haiku-5-5", use_batch=False) > 0
    assert C.estimate_cost_usd(40, "claude-opus-5-5", use_batch=True) == pytest.approx(C.estimate_cost_usd(40, "claude-opus-5-5", use_batch=False) / 2, abs=0.001)


def test_llm_pass_handles_refusal_and_bad_json(monkeypatch):
    _fake_anthropic(monkeypatch, lambda kw: _Msg("", stop_reason="refusal"))
    defs = C.define_concepts([{"term": "x y", "display": "x y"}], model="claude-opus-5-5", api_key="k")
    assert defs == {"x y": None}
    _fake_anthropic(monkeypatch, lambda kw: _Msg("not json"))
    assert C.define_concepts([{"term": "x y", "display": "x y"}], model="claude-opus-5-5", api_key="k") == {"x y": None}
