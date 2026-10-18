from pathlib import Path

import yaml

from litradar.config import DEFAULT_CONFIG_DIR
from litradar.extract.gazetteer import Gazetteer
from litradar.extract.jats import parse_jats

FIX = Path(__file__).parent / "fixtures" / "sample_jats.xml"


def test_sections_extracted():
    d = parse_jats(FIX.read_bytes())
    assert "NovaSeq X Plus" in d["methods"]
    assert "Orbitrap Astral" in d["methods"]
    assert "Introduction" not in d["methods"] and "We found things" not in d["methods"]
    assert "National Institutes of Health" in d["funding"]
    assert "employee of 10x Genomics" in d["coi"]
    assert "GSE000001" in d["data_availability"]
    assert "Twist Bioscience" in d["ack"]


def test_authors_affiliations_emails():
    d = parse_jats(FIX.read_bytes())
    names = [a["name"] for a in d["authors"]]
    assert names == ["Jane Doe", "Richard Roe"]
    assert d["authors"][0]["orcid"].endswith("0000-0001-2345-6789")
    assert "10x Genomics, Pleasanton, CA, USA" in d["affiliations"]
    assert d["emails"] == ["jane.doe@10xgenomics.com"]


def test_end_to_end_matching_on_fixture():
    d = parse_jats(FIX.read_bytes())
    inst = Gazetteer.from_yaml(yaml.safe_load(open(DEFAULT_CONFIG_DIR / "instruments.yaml")), "instrument")
    comp = Gazetteer.from_yaml(yaml.safe_load(open(DEFAULT_CONFIG_DIR / "companies.yaml")), "company")
    ids = {m.entity_id for m in inst.find(d["methods"])}
    assert {"ilmn-novaseq-x", "txg-xenium", "thermo-orbitrap-astral", "evosep-one", "txg-chromium"} <= ids
    assert "singular-g4" not in ids  # "G4 quadruplex" must not count
    coi = {m.entity_id for m in comp.find(d["coi"])}
    assert coi == {"10x-genomics", "illumina"}
    ack = {m.entity_id for m in comp.find(d["ack"])}
    assert ack == {"twist"}


def test_garbage_input():
    assert parse_jats(b"not xml at all") == {} or isinstance(parse_jats(b"not xml at all"), dict)
    assert parse_jats(b"") == {}


def test_abstract_is_extracted_skipping_graphical_and_label():
    d = parse_jats(FIX.read_bytes())
    assert d["abstract"].startswith("We profile tumours with a multiplexed in situ assay") and "The assay resolves single cells." in d["abstract"]
    assert not d["abstract"].lower().startswith("abstract") and "toc.jpg" not in d["abstract"]
    assert "We profile tumours" not in d["methods"] and "We profile tumours" not in d["body"]
    assert parse_jats(b"<article><front><article-meta/></front><body><sec><title>Methods</title><p>x</p></sec></body></article>")["abstract"] == ""
    # a structured abstract keeps its section labels; a sub-article abstract is ignored
    xml = (b"<article><front><article-meta><abstract><sec><title>Background</title><p>Why.</p></sec><sec><title>Results</title><p>What.</p></sec></abstract>"
           b"</article-meta></front><body/><sub-article><front-stub><abstract><p>Not this one.</p></abstract></front-stub></sub-article></article>")
    assert parse_jats(xml)["abstract"] == "Background Why. Results What."
