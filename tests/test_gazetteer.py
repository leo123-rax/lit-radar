import re
from pathlib import Path

import pytest
import yaml

from litradar.config import DEFAULT_CONFIG_DIR
from litradar.extract.gazetteer import Gazetteer, _is_ambiguous
from litradar.rank.heuristics import PLATFORM_CATEGORIES, is_platform

FIXTURES = Path(__file__).parent / "fixtures"
DICT_WORDS = Path("/usr/share/dict/words")

COMPANY_TYPES = {"tools", "diagnostics", "synthesis", "ai_bio", "pharma", "cro", "institute", "software"}
INSTRUMENT_CATEGORIES = {
    "sequencer.short_read", "sequencer.long_read", "single_cell", "spatial", "proteomics.affinity", "proteomics.ms",
    "library_prep", "synthesis", "pcr", "array", "cytometry", "cryo_em", "imaging", "automation", "cell_engineering",
    "bioprocess", "qc", "software", "reagent",
}
# analysis / acquisition software names: they must live in a `software` entry, never credit hardware
SOFTWARE_NAMES = [
    "FlowJo", "FACSDiva", "CytExpert", "Kaluza", "SpectroFlo", "NIS-Elements", "ZEN Blue", "cellSens", "Harmony software",
    "MetaXpress", "MetaMorph", "Imaris", "Image Lab", "QuantaSoft", "Cell Ranger", "Space Ranger", "Dorado", "Guppy",
    "MassLynx", "MassHunter", "LabSolutions", "TopSpin", "EPU software", "DigitalMicrograph", "LAS X", "Gen5", "UNICORN software",
    "Analyst software", "Bionano Access", "AtoMx", "inForm", "xPONENT", "CaseViewer", "FluentControl", "Venus software",
    "MACSQuantify", "CFX Maestro", "Progenesis QI", "Columbus", "Image Studio", "AzureSpot", "ASTRA software", "SoftMax Pro",
    "CLC Genomics Workbench", "Ingenuity Pathway Analysis", "Momentum software", "NovoExpress", "IDEAS software", "MCD Viewer",
]
# bare company names allowed as instrument aliases: single-platform vendors whose name implies the instrument
SINGLE_PLATFORM_VENDORS = {
    "oxford nanopore", "sciex", "gatan", "covaris", "evosep", "olink", "luminex", "maxcyte", "vizgen", "lunaphore",
    "akoya codex", "parse biosciences", "parse bio", "scale biosciences", "scalebio", "scale bio", "mission bio", "singleron",
    "honeycomb biotechnologies", "1cellbio", "dolomite bio", "cellanome", "resolve biosciences", "curio bioscience",
    "cantata bio", "codetta bio", "syndex bio", "ansa bio", "dna script", "codex dna", "elegen corp", "genewiz", "eurofins genomics",
    "eurofins mwg", "lgc biosearch", "evonetix", "camena bioscience", "sangon biotech", "tsingke", "stilla technologies",
    "cepheid", "biofire", "nanostring", "quantum-si", "nautilus biotechnology", "seer inc", "proteinsimple", "ionpath",
    "cytena", "cryosol", "quantifoil", "protochips", "3i", "intelligent imaging innovations", "nanolive", "oxford nanoimaging",
    "indica labs", "visiopharm", "aiforia", "pathai", "formulatrix", "integra biosciences", "spt labtech", "ttp labtech",
    "cybio", "analytik jena", "berthold technologies", "bmg labtech", "nanotemper technologies", "unchained labs", "nicoya",
    "refeyn", "hidex", "volta labs", "biosero", "highres biosolutions", "gilson", "dispendix", "cellenion", "wyatt", "ta instruments",
    "synthego", "mirus", "mirus bio", "polyplus", "nepa gene", "celetrix", "cellecta", "benchling", "inscripta", "cellares",
    "terumo bct", "pbs biotech", "infors ht", "getinge applikon", "applikon", "precision nanosystems", "omega bio-tek", "sage science",
    "circulomics", "denovix", "watchmaker", "zymo research", "active motif", "epicypher", "arima genomics", "phase genomics",
    "universal sequencing technology", "biomodal", "cambridge epigenetix", "twist bioscience", "telesis bio", "elegen",
    "genemind", "genapsys", "thorlabs", "physik instrumente", "mad city labs", "abberior", "sutter instrument", "prior scientific",
    "coolled", "excelitas", "lumencor", "coherent", "spectra-physics", "hamamatsu", "yokogawa", "3dhistech", "evident", "evident scientific",
    "lavision biotec", "bruker", "bruker paintscape", "stellaromics", "rebus biosystems", "atlasxomics", "spatial genomics", "nomic", "nomic bio",
    "alamar biosciences", "somalogic", "biognosys", "ionopticks", "preomics", "fluent biosciences",
    "standard biotools", "fluidigm", "bionano genomics", "akoya", "advanced cell diagnostics", "acd bio", "rnascope", "bio-techne",
    "cytiva", "repligen", "sartorius", "lonza", "miltenyi", "miltenyi biotec", "bd biosciences", "ultima genomics", "element biosciences",
    "singular genomics", "roche", "qiagen", "genscript", "idt", "integrated dna technologies", "takara bio", "clontech", "new england biolabs",
    "nuggen", "nugen", "tecan genomics", "revvity", "perkinelmer", "cytek", "sony biotechnology", "beckman coulter", "molecular devices",
    "leica microsystems", "leica biosystems", "carl zeiss", "zeiss", "nikon", "olympus", "jeol", "hitachi", "andor", "oxford instruments",
    "thermo fisher scientific", "thermo scientific", "invitrogen", "gibco", "applied biosystems", "life technologies", "illumina", "pacbio",
    "pacific biosciences", "10x genomics", "bio-rad", "agilent", "waters", "shimadzu", "promega", "eppendorf", "corning", "stemcell technologies",
    "merck millipore", "milliporesigma", "hologic", "diagenode", "abcam", "hamilton robotics", "opentrons", "azenta", "brooks automation",
    # brand == platform: the company alias and the instrument name are the same string by design
    "ion torrent", "canopy biosciences", "ansa biotechnologies", "wyatt technology",
    # MSD = Meso Scale Discovery (instrument, gated) and Merck Sharp & Dohme (pharma, gated); both are case-sensitive + context
    "msd",
}


def _yaml(name):
    return yaml.safe_load(open(DEFAULT_CONFIG_DIR / f"{name}.yaml"))


def _instruments():
    return Gazetteer.from_yaml(_yaml("instruments"), "instrument")


def _companies():
    return Gazetteer.from_yaml(_yaml("companies"), "company")


def _ids(mentions):
    return {m.entity_id for m in mentions}


# ---------------------------------------------------------------- matcher mechanics


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
    assert gz.match_archive_model("Revio").id == "pacb-revio"
    assert gz.match_archive_model("PromethION").id == "ont-promethion"
    assert gz.match_archive_model("MinION").id == "ont-minion"
    assert gz.match_archive_model("Illumina HiSeq 4000").id == "ilmn-hiseq"
    assert gz.match_archive_model("unspecified") is None


def test_pacbio_generations_in_archive_vocabulary():
    gz = _instruments()
    assert gz.match_archive_model("Sequel").id == "pacb-sequel"
    assert gz.match_archive_model("Sequel II").id == "pacb-sequel-ii"
    assert gz.match_archive_model("Sequel IIe").id == "pacb-sequel-ii"
    assert gz.match_archive_model("PacBio RS II").id == "pacb-rs-ii"
    assert gz.match_archive_model("PacBio RS").id == "pacb-rs-ii"
    assert gz.match_archive_model("Onso").id == "pacb-onso"
    assert gz.match_archive_model("Vega").id == "pacb-vega"


def test_every_pacbio_entry_shares_the_vendor_context():
    required = {"PacBio", "Pacific Biosciences", "SMRT", "HiFi", "CCS"}
    for e in _instruments().entries:
        if e.id.startswith("pacb-"):
            assert required <= set(e.context_terms), f"{e.id} lacks the shared PacBio context terms"


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


# ---------------------------------------------------------------- schema hygiene


def test_instrument_categories_are_in_the_vocabulary():
    for raw in _yaml("instruments")["entries"]:
        assert raw.get("category") in INSTRUMENT_CATEGORIES, f"{raw['id']}: category {raw.get('category')!r}"


def test_platform_categories_match_heuristics():
    # the ranking module decides which categories are platforms; make sure every category we use is classified
    for cat in INSTRUMENT_CATEGORIES:
        assert isinstance(is_platform(cat), bool)
    assert is_platform("sequencer.long_read") and is_platform("spatial") and is_platform("cell_engineering")
    assert not is_platform("software") and not is_platform("reagent") and not is_platform("qc")
    assert set(PLATFORM_CATEGORIES) <= {c.split(".")[0] for c in INSTRUMENT_CATEGORIES} | set(INSTRUMENT_CATEGORIES)


def test_every_company_has_a_known_type():
    for raw in _yaml("companies")["entries"]:
        assert raw.get("type") in COMPANY_TYPES, f"{raw['id']}: type {raw.get('type')!r}"


def test_ai_tool_vendors_are_type_software():
    types = {raw["id"]: raw.get("type") for raw in _yaml("companies")["entries"]}
    for cid in ("openai", "anthropic-co", "microsoft-research", "nvidia", "google-deepmind"):
        assert types[cid] == "software", cid
    assert types["isomorphic"] == "ai_bio"
    gz = _companies()
    aliases = {a for e in gz.entries if e.id == "google-deepmind" for a in e.aliases}
    assert "AlphaFold" in aliases and "AlphaFold3" in aliases


def test_case_sensitive_aliases_and_context_terms_are_declared_aliases():
    for name in ("instruments", "companies"):
        for raw in _yaml(name)["entries"]:
            aliases = set(raw.get("aliases") or []) | {raw.get("name")}
            for a in raw.get("case_sensitive_aliases") or []:
                assert a in aliases, f"{raw['id']}: case-sensitive alias {a!r} is not an alias"
            if raw.get("case_sensitive_aliases") or raw.get("context_required"):
                # the matcher needs something to look for: explicit terms or a vendor / parent to fall back on
                assert raw.get("context_terms") or raw.get("vendor") or raw.get("parent"), f"{raw['id']}: gated but no context source"


def test_software_lives_in_software_entries():
    gz = _instruments()
    by_alias = {}
    for e in gz.entries:
        for a in e.aliases:
            by_alias[a.lower()] = e
    for sw in SOFTWARE_NAMES:
        e = by_alias.get(sw.lower())
        assert e is not None, f"software alias {sw!r} missing from the gazetteer"
        assert e.category == "software", f"{sw!r} sits in {e.id} ({e.category}); software never credits hardware"
    for e in gz.entries:
        if e.category == "software":
            assert not is_platform(e.category)


def test_bare_company_names_are_not_instrument_aliases_unless_single_platform():
    """An instrument alias equal to a company gazetteer name/alias credits hardware for a mere supplier mention.
    Allowed only for vendors that sell one platform (documented list)."""
    companies = _companies()
    company_names = {}
    for e in companies.entries:
        for a in e.aliases:
            company_names[a.lower()] = e.id
    offenders = []
    for e in _instruments().entries:
        for a in e.aliases:
            key = a.lower()
            if key in company_names and key not in SINGLE_PLATFORM_VENDORS:
                offenders.append((e.id, a, company_names[key]))
    assert not offenders, f"instrument aliases that are plain company names: {offenders}"


# ---------------------------------------------------------------- dictionary sweep


def _dictionary():
    words = set()
    with open(DICT_WORDS, encoding="utf-8", errors="ignore") as fh:
        for w in fh:
            w = w.strip()
            if w:
                words.add(w.lower())
    return words


def _risky(alias: str, words) -> str:
    """'dict' if the alias is a plain dictionary word (<= 10 letters), 'short' if a 2-4 letter token, else ''.
    Tokens containing a digit are exempt: the regex keeps the digit sequence whatever the case flag."""
    compact = re.sub(r"[\s\-\.]+", "", alias)
    if not compact or any(ch.isdigit() for ch in compact):
        return ""
    if compact.isalpha() and len(compact) <= 10 and compact.lower() in words:
        return "dict"
    if compact.isalpha() and 2 <= len(compact) <= 4:
        return "short"
    return ""


@pytest.mark.skipif(not DICT_WORDS.exists(), reason="system dictionary not available")
def test_dictionary_words_and_short_tokens_are_gated():
    words = _dictionary()
    violations = []
    for name, kind in (("instruments", "instrument"), ("companies", "company")):
        gz = Gazetteer.from_yaml(_yaml(name), kind)
        for e in gz.entries:
            for a in e.aliases:
                why = _risky(a, words)
                if not why:
                    continue
                protected = a in e.case_sensitive_aliases or e.context_required or _is_ambiguous(a)
                if not protected:
                    violations.append((kind, e.id, a, why))
    assert not violations, "ungated dictionary-word / short aliases: " + ", ".join(f"{k}:{i}:{a}({w})" for k, i, a, w in violations)


@pytest.mark.skipif(not DICT_WORDS.exists(), reason="system dictionary not available")
def test_dictionary_sweep_helper_examples():
    words = _dictionary()
    assert _risky("Evident", words) == "dict"
    assert _risky("transit", words) == "dict"
    assert _risky("KAPA", words) in ("dict", "short")  # 4 letters, and 'kapa' happens to be in the dictionary
    assert _risky("RS II", words) == "short"
    assert _risky("E.Z.N.A.", words) == "short"  # dots stripped -> 4 letters
    assert _risky("DS-11", words) == ""  # digit -> exempt
    assert _risky("NovaSeq X Plus", words) == ""


# ---------------------------------------------------------------- fixture-driven regression cases


def _cases():
    return yaml.safe_load(open(FIXTURES / "gazetteer_cases.yaml"))["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["text"][:60])
def test_fixture_cases(case):
    gz = _instruments() if case["gazetteer"] == "instrument" else _companies()
    hits = _ids(gz.find(case["text"]))
    missing = set(case.get("expect") or []) - hits
    wrong = set(case.get("reject") or []) & hits
    assert not missing, f"expected {sorted(missing)} to fire; got {sorted(hits)}"
    assert not wrong, f"{sorted(wrong)} must not fire; got {sorted(hits)}"


def test_fixture_ids_exist():
    known = {e.id for e in _instruments().entries} | {e.id for e in _companies().entries}
    for case in _cases():
        for cid in list(case.get("expect") or []) + list(case.get("reject") or []):
            assert cid in known, f"fixture references unknown entity {cid!r}"
