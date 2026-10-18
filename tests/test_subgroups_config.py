"""config/subgroups.yaml: the researcher-tracking file parses and every tracked author is well-formed.

The pipeline matches tracked authors by OpenAlex id when one is configured and by display name otherwise
(docs/researchers.md), so a malformed id silently degrades a person to name matching and a duplicate id inside
a subgroup double-counts every hit. These checks run against the shipped file; `scripts/resolve_authors.py`
applies the same rules before it writes.
"""
import re
from collections import Counter

import pytest
import yaml

from litradar.config import REPO_ROOT, load_yaml_checked

SUBGROUPS = REPO_ROOT / "config" / "subgroups.yaml"
OPENALEX_ID_RE = re.compile(r"^A\d{6,}$")
ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
OPENALEX_TOPIC_RE = re.compile(r"^T\d{5,}$")
AUTHOR_KEYS = {"name", "openalex_id", "institution", "orcid"}


@pytest.fixture(scope="module")
def groups():
    data = load_yaml_checked(SUBGROUPS)
    assert isinstance(data, dict) and isinstance(data.get("subgroups"), list) and data["subgroups"], "subgroups.yaml has no `subgroups:` list"
    return data["subgroups"]


def _authors(groups):
    for g in groups:
        for a in g.get("tracked_authors") or []:
            yield g["id"], a


def test_file_parses_with_plain_yaml():
    with open(SUBGROUPS, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict) and "subgroups" in data


def test_every_subgroup_has_id_name_and_list_fields(groups):
    ids = [g.get("id") for g in groups]
    assert all(ids), "a subgroup has no id"
    assert len(set(ids)) == len(ids), f"duplicate subgroup ids: {[i for i, n in Counter(ids).items() if n > 1]}"
    for g in groups:
        assert g.get("name"), f"subgroup {g['id']} has no name"
        assert isinstance(g.get("tracked_authors", []), list), f"{g['id']}.tracked_authors is not a list"
        for key in ("keywords", "openalex_topics"):
            assert isinstance(g.get(key, []), list), f"{g['id']}.{key} is not a list"
        for t in g.get("openalex_topics") or []:
            assert OPENALEX_TOPIC_RE.match(str(t)), f"{g['id']}: topic id {t!r} does not look like T12345"


def test_every_tracked_author_has_name_and_institution(groups):
    for sg, a in _authors(groups):
        assert isinstance(a, dict), f"{sg}: tracked author is not a mapping: {a!r}"
        assert str(a.get("name") or "").strip(), f"{sg}: tracked author without a name: {a!r}"
        assert str(a.get("institution") or "").strip(), f"{sg}: {a.get('name')!r} has no institution"
        assert "openalex_id" in a, f"{sg}: {a['name']!r} has no openalex_id key (use \"\" when unresolved)"
        unknown = set(a) - AUTHOR_KEYS
        assert not unknown, f"{sg}: {a['name']!r} carries unknown keys {sorted(unknown)}"


def test_every_openalex_id_is_well_formed(groups):
    bad = [(sg, a["name"], a["openalex_id"]) for sg, a in _authors(groups) if a.get("openalex_id") and not OPENALEX_ID_RE.match(str(a["openalex_id"]))]
    assert not bad, f"openalex_id must match ^A\\d{{6,}}$ or be empty: {bad}"
    assert all(isinstance(a.get("openalex_id"), str) for _sg, a in _authors(groups)), "openalex_id must be a quoted string"


def test_no_duplicate_ids_within_a_subgroup(groups):
    for g in groups:
        ids = [str(a.get("openalex_id")) for a in g.get("tracked_authors") or [] if a.get("openalex_id")]
        dups = sorted({i for i, n in Counter(ids).items() if n > 1})
        assert not dups, f"{g['id']}: openalex_id used twice: {dups}"
        names = [str(a.get("name")).casefold() for a in g.get("tracked_authors") or []]
        dup_names = sorted({n for n, c in Counter(names).items() if c > 1})
        assert not dup_names, f"{g['id']}: tracked author listed twice: {dup_names}"


def test_one_id_per_person_across_subgroups(groups):
    """The same display name in two subgroups is one person and must carry one id (the pipeline keys hits by id)."""
    by_name = {}
    for sg, a in _authors(groups):
        aid = str(a.get("openalex_id") or "")
        if not aid:
            continue
        prev = by_name.setdefault(a["name"], (sg, aid))
        assert prev[1] == aid, f"{a['name']!r} has id {prev[1]} in {prev[0]} but {aid} in {sg}"


def test_orcids_are_well_formed_when_present(groups):
    bad = [(sg, a["name"], a["orcid"]) for sg, a in _authors(groups) if a.get("orcid") and not ORCID_RE.match(str(a["orcid"]))]
    assert not bad, f"orcid must look like 0000-0002-1825-0097: {bad}"


def test_header_records_the_resolution_pass():
    first = SUBGROUPS.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# OpenAlex author ids:"), "the first line should say when the ids were last resolved against the API"
    assert re.search(r"\d{4}-\d{2}-\d{2}", first) and "resolved" in first and "verified" in first
