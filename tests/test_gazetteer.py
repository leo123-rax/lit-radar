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
    "bioprocess", "qc", "software", "reagent", "method",
}
# analysis / acquisition software names: they must live in a `software` entry, never credit hardware
SOFTWARE_NAMES = [
    "FlowJo", "FACSDiva", "CytExpert", "Kaluza", "SpectroFlo", "NIS-Elements", "ZEN Blue", "cellSens", "Harmony software",
    "MetaXpress", "MetaMorph", "Imaris", "Image Lab", "QuantaSoft", "Cell Ranger", "Space Ranger", "Dorado", "Guppy",
    "MassLynx", "MassHunter", "LabSolutions", "TopSpin", "EPU software", "DigitalMicrograph", "LAS X", "Gen5", "UNICORN software",
    "Analyst software", "Bionano Access", "AtoMx", "inForm", "xPONENT", "CaseViewer", "FluentControl", "Venus software",
    "MACSQuantify", "CFX Maestro", "Progenesis QI", "Columbus", "Image Studio", "AzureSpot", "ASTRA software", "SoftMax Pro",
    "CLC Genomics Workbench", "Ingenuity Pathway Analysis", "Momentum software", "NovoExpress", "IDEAS software", "MCD Viewer",
    # AI / foundation models are tools used in papers, never company involvement (moved out of companies.yaml 2026-10-17)
    "AlphaFold", "AlphaFold 3", "AlphaMissense", "AlphaGenome", "ESMFold", "ESM3", "RoseTTAFold", "RFdiffusion", "Boltz-1", "Chai-1",
    "BioNeMo", "Parabricks", "BioEmu", "EvoDiff", "ProGen2", "OpenCRISPR", "Chroma model", "Latent-X", "H-optimus-0", "Evo 2", "CELLxGENE",
]
# AI model names that must NOT be company aliases any more (they made Google DeepMind the #1 "company" on a backfill)
MODEL_NAMES_NOT_COMPANY_ALIASES = ["AlphaFold", "AlphaFold3", "AlphaFold 2", "AlphaGenome", "ESM3", "Chai-1", "BioNeMo", "Parabricks", "BioEmu",
                                   "EvoDiff", "ProGen2", "OpenCRISPR", "Chroma model", "Latent-X", "H-optimus", "Evo 2", "CELLxGENE", "TranscriptFormer"]
# the per-vendor "no model named" buckets: one per vendor, named consistently, flagged for the digest / dashboard
UNSPECIFIED_BUCKETS = {"ilmn-sequencing": "Illumina", "ilmn-novaseq": "NovaSeq", "ilmn-nextseq": "NextSeq", "ont-chemistry": "Oxford Nanopore",
                       "pacb-hifi": "PacBio", "mgi-dnbseq": "MGI DNBSEQ"}
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
    assert "ilmn-novaseq-x-plus" in hits
    assert "ilmn-novaseq-x" not in hits  # the base model must not fire on the same span (split 2026-10-17)
    assert "ilmn-novaseq" not in hits  # generic entry must not fire on the same span
    assert hits["ilmn-novaseq-x-plus"].count == 1
    hits = {m.entity_id: m for m in gz.find("Libraries were sequenced on a NovaSeq X instrument.")}
    assert "ilmn-novaseq-x" in hits and "ilmn-novaseq-x-plus" not in hits and "ilmn-novaseq" not in hits


def test_loose_spacing_and_hyphens():
    gz = _instruments()
    for text in ["NovaSeq-X Plus", "NovaSeqX Plus", "NovaSeq X  Plus", "NovaSeq‐X Plus", "NovaSeq‑X Plus"]:
        assert any(m.entity_id == "ilmn-novaseq-x-plus" for m in gz.find(text)), text


def test_unicode_hyphens_are_word_boundaries():
    gz = _companies()
    # U+2010 hyphen and U+2011 non-breaking hyphen glue the hospital's name together exactly like the ASCII hyphen
    for hyphen in ("-", "‐", "‑"):
        assert not [m for m in gz.find(f"University Medical Center Hamburg{hyphen}Eppendorf, Germany") if m.entity_id == "eppendorf"], repr(hyphen)
    assert [m for m in gz.find("tubes (Eppendorf, Hamburg, Germany)") if m.entity_id == "eppendorf"]
    # en and em dashes stay sentence punctuation: a brand glued to one is still a hit
    assert [m for m in gz.find("reagents from Illumina—the sequencing vendor—were used") if m.entity_id == "illumina"]


def test_hits_inside_web_and_email_addresses_are_skipped():
    from litradar.extract.gazetteer import _inside_address

    gz = _companies()
    for text in (
        "gnomAD (https://gnomad.broadinstitute.org/) and GATK (https://github.com/broadinstitute/gatk-sv)",
        "Morpheus ( https://software.broadinstitute.org/morpheus/ ) was used",
        "contact: first.last@illumina.com",
        "see illumina.com for details",
        "www.illumina.com/products",
    ):
        assert not [m for m in gz.find(text) if m.entity_id in ("broad-institute", "illumina")], text
    # prose next to a URL still counts, once
    hits = {m.entity_id: m for m in gz.find("DRAGEN (Illumina) output was uploaded to BaseSpace (basespace.illumina.com).")}
    assert hits["illumina"].count == 1
    # a missing space after a section number is not a host name; a capitalised word after a full stop is not a TLD
    assert [m for m in gz.find("2.3.Illumina sequencing was performed") if m.entity_id == "illumina"]
    assert _inside_address("x Illumina.Interestingly", 2, 10) is False
    assert _inside_address("x Illumina.in vitro", 2, 10) is True  # lower-case 'in' after the dot reads as a host name (accepted loss)
    # archive vocabulary strings never carry addresses, and ignore_context skips the check anyway
    assert gz.find("illumina.com", ignore_context=True)


def test_family_fields_and_labels():
    gz = _instruments()
    fam = gz.families
    assert fam["ilmn-novaseq-x"] == fam["ilmn-novaseq-x-plus"] == "novaseq-x"
    assert fam["pacb-sequel"] == fam["pacb-sequel-ii"] == "sequel"
    assert {e.id for e in gz.family_members("chromium")} >= {"txg-chromium", "txg-chromium-flex", "txg-multiome", "txg-single-cell-3prime"}
    assert fam["ont-promethion"] == "promethion" and fam["ilmn-nextseq-1000"] == fam["ilmn-nextseq-2000"] == "nextseq-1000-2000"
    assert "ilmn-novaseq-6000" not in fam and "txg-xenium" not in fam
    # the label is the member whose name slugifies to the key, else the member names joined
    assert gz.family_label("novaseq-x") == "NovaSeq X" and gz.family_label("sequel") == "Sequel"
    assert gz.family_label("chromium") == "Chromium" and gz.family_label("promethion") == "PromethION"
    assert gz.family_label("nextseq-1000-2000") == "NextSeq 1000 / NextSeq 2000"
    from litradar.extract.gazetteer import family_label

    assert family_label("foo", []) == "foo"
    # a family never changes what matches
    assert {m.entity_id for m in gz.find("sequenced on a NovaSeq X Plus")} == {"ilmn-novaseq-x-plus"}
    for raw in _yaml("instruments")["entries"]:
        if raw.get("family"):
            assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", raw["family"]), f"{raw['id']}: family {raw['family']!r} is not a slug"


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
    assert gz.match_archive_model("Illumina NovaSeq X Plus").id == "ilmn-novaseq-x-plus"
    assert gz.match_archive_model("Illumina NovaSeq X").id == "ilmn-novaseq-x"
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
    assert aliases == {"Google DeepMind", "DeepMind", "Google Research"}  # the organisation only; models live in instruments.yaml


def test_ai_model_names_are_software_tools_not_company_aliases():
    """A model name in a paper is tool usage. On an 89,165-paper backfill the AlphaFold aliases alone made Google DeepMind
    the #1 'company' (2,425 papers, 100 % from AlphaFold/AlphaFold 2/AlphaFold 3 in titles and abstracts)."""
    companies, instruments = _companies(), _instruments()
    company_aliases = {a.lower(): e.id for e in companies.entries for a in e.aliases}
    for name in MODEL_NAMES_NOT_COMPANY_ALIASES:
        assert name.lower() not in company_aliases, f"{name!r} is still a company alias ({company_aliases.get(name.lower())})"
    by_alias = {a.lower(): e for e in instruments.entries for a in e.aliases}
    for name in MODEL_NAMES_NOT_COMPANY_ALIASES:
        e = by_alias.get(name.lower())
        assert e is not None and e.category == "software", f"{name!r} must be a software entry in instruments.yaml"
        assert e.vendor, f"{e.id}: a model entry names its vendor"
    text = "Structures were predicted with AlphaFold 3 and ESMFold, binders designed with RFdiffusion and scored by Boltz-2 co-folding."
    assert {m.entity_id for m in instruments.find(text)} == {"deepmind-alphafold", "meta-esm", "rosettafold", "boltz"}
    assert not [m for m in companies.find(text) if m.entity_id in ("google-deepmind", "evolutionaryscale", "chai-discovery")]
    # the organisation itself still resolves as a company
    assert {m.entity_id for m in companies.find("Google DeepMind, London, UK; NVIDIA Corporation, Santa Clara")} >= {"google-deepmind", "nvidia"}
    assert {m.entity_id for m in instruments.find("Variant calling used NVIDIA Clara Parabricks; embeddings came from BioNeMo.")} == {"nvidia-parabricks", "nvidia-bionemo"}


def test_academic_methods_are_not_vendor_platforms():
    gz = _instruments()
    e = gz.by_id["slide-seq"]
    assert e.category == "method" and e.vendor == "open source" and not is_platform(e.category)
    assert {m.entity_id for m in gz.find("Spatial transcriptomics was performed with Slide-seqV2 on 10-um sections.")} == {"slide-seq"}
    assert {m.entity_id for m in gz.find("Slide-tags was applied to human cortex nuclei.")} == {"slide-seq"}
    hits = {m.entity_id for m in gz.find("Sections were processed with the Curio Seeker kit (Curio Bioscience) following the Slide-seq protocol.")}
    assert hits == {"curio-seeker", "slide-seq"}
    assert "Slide-seq" not in gz.by_id["curio-seeker"].aliases and "Slide-seqV2" not in gz.by_id["curio-seeker"].aliases


def test_unspecified_model_buckets_are_named_consistently():
    gz = _instruments()
    for eid, vendor_word in UNSPECIFIED_BUCKETS.items():
        e = gz.by_id[eid]
        assert e.granularity == "unspecified", eid
        assert e.name == f"{vendor_word} (model unspecified)", (eid, e.name)
    # every entry named "(model unspecified)" is flagged, and every flagged bucket keeps its old display name as an alias
    for e in gz.entries:
        if "(model unspecified)" in e.name:
            assert e.granularity == "unspecified", e.id
    for old_name, eid in (("Illumina sequencing", "ilmn-sequencing"), ("NovaSeq", "ilmn-novaseq"), ("NextSeq", "ilmn-nextseq"),
                          ("Oxford Nanopore sequencing", "ont-chemistry"), ("PacBio HiFi", "pacb-hifi"), ("DNBSEQ", "mgi-dnbseq")):
        assert old_name in gz.by_id[eid].aliases, (eid, old_name)
    # matching unchanged: the buckets fire on vendor-level phrases and lose to a model name on the same span
    assert {m.entity_id for m in gz.find("Libraries were sequenced on an Illumina platform (2 x 150 bp).")} == {"ilmn-sequencing"}
    assert {m.entity_id for m in gz.find("Long reads were generated by Oxford Nanopore sequencing on a PromethION 24.")} == {"ont-chemistry", "ont-promethion"}
    assert {m.entity_id for m in gz.find("HiFi sequencing (PacBio) produced 20-kb reads.")} == {"pacb-hifi"}
    assert {m.entity_id for m in gz.find("Sequenced on the DNBSEQ platform (MGI).")} == {"mgi-dnbseq"}
    assert {m.entity_id for m in gz.find("sequenced on the NovaSeq 6000 platform")} == {"ilmn-novaseq-6000"}
    flagged = {e.id for e in gz.entries if e.granularity == "unspecified"}
    assert flagged >= set(UNSPECIFIED_BUCKETS) | {"ilmn-5-base", "ilmn-xleap-sbs", "mgi-coolmps"}
    for e in gz.entries:
        assert e.granularity in ("", "unspecified"), (e.id, e.granularity)


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


_INFLECTION_SUFFIXES = ("ing", "est", "ies", "ied", "ier", "es", "ed", "er", "s")


def _inflected_stem(compact: str, words) -> str:
    """The dictionary word that `compact` is a regular inflection of ('smarter' -> 'smart', 'latitudes' -> 'latitude'),
    or ''. Deliberately narrow, so brand names do not trip it: a plural stem needs six letters ('GenePS' is not
    'genep' + s), the restored silent e needs a five-letter stem ('Bruker' is not 'bruke' + r), '-es' only follows a
    sibilant, and doubled consonants are not undone ('CoolLED' is not 'cool' + ed)."""
    low = compact.lower()
    for suffix in _INFLECTION_SUFFIXES:
        if not low.endswith(suffix):
            continue
        stem = low[: -len(suffix)]
        candidates = []
        if suffix in ("s", "es"):
            if suffix == "es" and not stem.endswith(("s", "x", "z", "ch", "sh")):
                continue
            if len(stem) >= 6:
                candidates.append(stem)
        elif suffix in ("ies", "ied", "ier"):
            if len(stem) >= 4:
                candidates.append(stem + "y")
        else:
            if len(stem) >= 4:
                candidates.append(stem)
            if len(stem) >= 5:
                candidates.append(stem + "e")
        for cand in candidates:
            if cand in words:
                return cand
    return ""


def _risky(alias: str, words) -> str:
    """'dict' if the alias is a plain dictionary word (<= 10 letters), 'inflected' if it is a regular inflection of
    one (the dictionary lists 'smart', not 'smarter'; the matcher is case-insensitive, so 'SMARTer' read the
    comparative), 'short' if a 2-4 letter token, else ''. Tokens containing a digit are exempt: the regex keeps the
    digit sequence whatever the case flag."""
    compact = re.sub(r"[\s\-\.]+", "", alias)
    if not compact or any(ch.isdigit() for ch in compact):
        return ""
    if compact.isalpha() and len(compact) <= 10 and compact.lower() in words:
        return "dict"
    if compact.isalpha() and len(compact) <= 10 and _inflected_stem(compact, words):
        return "inflected"
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
    # inflected forms the plain membership test missed (DATA-06: 'SMARTer' matched the comparative in 23 of 30 hits)
    assert _risky("SMARTer", words) == "inflected" and _inflected_stem("SMARTer", words) == "smart"
    assert _risky("Latitude S", words) == "inflected"  # the optional separator makes the regex read 'latitudes'
    assert _risky("ENrich S", words) == "inflected"
    for brand in ("Bruker", "Cellares", "CoolLED", "GenePS", "Illumina", "NovaSeq"):
        assert _risky(brand, words) == "", brand


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


# ---- prefilter equivalence and speed (from the store/pipeline hardening pass) ----
import random  # noqa: E402 - appended block from a sibling branch
import time  # noqa: E402 - appended block from a sibling branch

from litradar.extract.gazetteer import _literal_token  # noqa: E402 - appended block from a sibling branch
from litradar.extract.jats import parse_jats  # noqa: E402 - appended block from a sibling branch

FIX = FIXTURES / "sample_jats.xml"


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


def _as_tuples(mentions):
    return sorted((m.entity_id, m.count, m.section, m.snippet) for m in mentions)


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
