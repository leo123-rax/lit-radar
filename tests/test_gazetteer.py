import yaml

from litradar.config import DEFAULT_CONFIG_DIR
from litradar.extract.gazetteer import Gazetteer


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
