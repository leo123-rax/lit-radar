import random
import time
from pathlib import Path

import yaml

from litradar.config import DEFAULT_CONFIG_DIR
from litradar.extract.gazetteer import Gazetteer, _literal_token
from litradar.extract.jats import parse_jats

FIX = Path(__file__).parent / "fixtures" / "sample_jats.xml"


def _instruments():
    return Gazetteer.from_yaml(yaml.safe_load(open(DEFAULT_CONFIG_DIR / "instruments.yaml")), "instrument")


def _companies():
    return Gazetteer.from_yaml(yaml.safe_load(open(DEFAULT_CONFIG_DIR / "companies.yaml")), "company")


def test_longest_alias_wins_and_no_double_count():
    gz = _instruments()
    hits = {m.entity_id: m for m in gz.find("Libraries were sequenced on a NovaSeq X Plus instrument.")}
    assert "ilmn-novaseq-x" in hits
    assert "ilmn-novaseq" not in hits  # generic entry must not fire on the same span
    assert hits["ilmn-novaseq-x"].count == 1


def test_loose_spacing_and_hyphens():
    gz = _instruments()
    for text in ["NovaSeq-X Plus", "NovaSeqX Plus", "NovaSeq X  Plus"]:
        assert any(m.entity_id == "ilmn-novaseq-x" for m in gz.find(text)), text


def test_ambiguous_alias_requires_context():
    gz = _instruments()
    # 'Chromium' the metal, no 10x context -> no hit
    assert not [m for m in gz.find("Chromium concentrations in drinking water exceeded limits.") if m.entity_id == "txg-chromium"]
    # with context -> hit
    assert [m for m in gz.find("Cells were loaded on the Chromium Controller (10x Genomics).") if m.entity_id == "txg-chromium"]
    # 'G4' without Singular -> nothing; with -> hit
    assert not gz.find("The G4 quadruplex structure was stable.")
    assert [m for m in gz.find("Sequenced on the Singular Genomics G4 platform.") if m.entity_id == "singular-g4"]


def test_case_sensitive_short_alias():
    gz = _instruments()
    assert not [m for m in gz.find("the revio of the data was done") if m.entity_id == "pacb-revio"]
    assert [m for m in gz.find("HiFi reads were generated on a Revio system.") if m.entity_id == "pacb-revio"]


def test_counts_and_snippet():
    gz = _instruments()
    text = "Xenium was run twice. Later, Xenium Analyzer output was processed; Xenium panels were custom."
    m = [m for m in gz.find(text) if m.entity_id == "txg-xenium"][0]
    assert m.count == 3
    assert "Xenium" in m.snippet


def test_company_domains_and_affiliations():
    gz = _companies()
    found = {e.id for e in gz.match_domains(["a.b@illumina.com", "x@research.pacb.com", "y@example.edu"])}
    assert found == {"illumina", "pacbio"}
    hits = {m.entity_id for m in gz.find("Department of X, Example University; Guardant Health, Redwood City, CA")}
    assert "guardant" in hits


def test_archive_model_mapping():
    gz = _instruments()
    assert gz.match_archive_model("Illumina NovaSeq X Plus").id == "ilmn-novaseq-x"
    assert gz.match_archive_model("PacBio Revio").id == "pacb-revio"
    assert gz.match_archive_model("PromethION").id == "ont-promethion"
    assert gz.match_archive_model("Illumina HiSeq 4000").id == "ilmn-hiseq"
    assert gz.match_archive_model("unspecified") is None


def test_no_alias_collisions_between_entries():
    for gz in (_instruments(), _companies()):
        seen = {}
        for e in gz.entries:
            for a in e.aliases:
                assert seen.get(a.lower(), e.id) == e.id, f"alias {a!r} in both {seen.get(a.lower())} and {e.id}"
                seen[a.lower()] = e.id


def test_solid_tumour_is_not_a_sequencer():
    gz = _instruments()
    assert not [m for m in gz.find("Patients with solid tumors were enrolled; SOLID evidence supports this.") if m.entity_id == "thermo-solid"]
    assert [m for m in gz.find("Libraries were sequenced on an Applied Biosystems SOLiD 4 system.") if m.entity_id == "thermo-solid"]
    assert gz.match_archive_model("AB SOLiD 3 Plus System").id == "thermo-solid"


# ---- literal-token prefilter: identical results, much faster ------------------------------------
def _as_tuples(mentions):
    return sorted((m.entity_id, m.count, m.section, m.snippet) for m in mentions)


def _synthetic_texts(gazetteers, n=50, seed=7):
    """Random prose seeded with aliases and context terms in mixed case / spacing, to exercise every pattern path."""
    rng = random.Random(seed)
    aliases = [a for gz in gazetteers for e in gz.entries for a in e.aliases]
    contexts = [t for gz in gazetteers for e in gz.entries for t in (e.context_terms or [e.vendor, e.name]) if t]
    filler = "the of and in with were was using on for to a by sequencing analysis samples cells data method protein tissue 10 v3 plus".split()
    texts = []
    for _ in range(n):
        words = []
        for _ in range(rng.randint(40, 400)):
            r = rng.random()
            if r < 0.08:
                w = rng.choice(aliases)
            elif r < 0.12:
                w = rng.choice(contexts)
            else:
                w = rng.choice(filler)
            r2 = rng.random()
            if r2 < 0.15:
                w = w.upper()
            elif r2 < 0.30:
                w = w.lower()
            elif r2 < 0.35:
                w = w.replace(" ", "-")
            elif r2 < 0.40:
                w = w.replace(" ", "")
            words.append(w)
        texts.append(rng.choice([" ", "  ", "\n", ", ", "; ", " (", ") "]).join(words))
    return texts


def test_literal_token():
    assert _literal_token("NovaSeq X Plus") == "novaseq"
    assert _literal_token("Chai-1") == "chai"
    assert _literal_token("Generate:Biomedicines") == "biomedicines"
    assert _literal_token("10x 3' v3") == "10x"
    assert _literal_token("Schrödinger") == "schrödinger"
    assert _literal_token("---") == ""


def test_prefilter_matches_unfiltered_results_exactly():
    inst, comp = _instruments(), _companies()
    d = parse_jats(FIX.read_bytes())
    texts = [d["methods"], d["coi"], d["ack"], d["body"]] + _synthetic_texts([inst, comp])
    assert len(texts) == 54
    checked = 0
    for gz in (inst, comp):
        for text in texts:
            fast = gz.find(text, section="methods", paper_id="p")
            slow = gz.find(text, section="methods", paper_id="p", _prefilter=False)
            assert _as_tuples(fast) == _as_tuples(slow), text[:120]
            assert _as_tuples(gz.find(text, ignore_context=True)) == _as_tuples(gz.find(text, ignore_context=True, _prefilter=False))
            checked += len(fast)
    assert checked > 100  # the synthetic texts really do hit many entries


def test_prefilter_is_faster_on_a_long_text():
    inst = _instruments()
    d = parse_jats(FIX.read_bytes())
    filler = "The samples were processed according to the instructions and analysed with standard statistical methods in the laboratory. "
    text = d["body"] + " " + d["methods"]
    while len(text) < 60_000:
        text += " " + filler
    assert _as_tuples(inst.find(text)) == _as_tuples(inst.find(text, _prefilter=False))

    def best_of(prefilter, reps=3):
        best = float("inf")
        for _ in range(reps):
            t0 = time.perf_counter()
            inst.find(text, _prefilter=prefilter)
            best = min(best, time.perf_counter() - t0)
        return best

    fast, slow = best_of(True), best_of(False)
    assert fast < slow * 0.7, f"prefilter {fast * 1000:.1f} ms vs {slow * 1000:.1f} ms"
