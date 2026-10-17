"""Offline tests for `litradar track` / `untrack` / `prioritize` on temp copies of the config files."""
import json
import shutil

import pytest
import yaml

from litradar import track as T
from litradar.cli import main
from litradar.config import DEFAULT_CONFIG_DIR, Settings
from litradar.extract.gazetteer import Gazetteer


@pytest.fixture
def settings(tmp_path, monkeypatch):
    shutil.copytree(DEFAULT_CONFIG_DIR, tmp_path / "config")
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "radar.sqlite"))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    s = Settings.from_env(config_dir=tmp_path / "config")
    s.cache_dir = tmp_path / ".cache"
    s.db_path = tmp_path / "radar.sqlite"
    return s


def _read(settings, name):
    return (settings.config_dir / f"{name}.yaml").read_text(encoding="utf-8")


def _backups(settings):
    d = settings.cache_dir / "config-backups"
    return sorted(d.iterdir()) if d.exists() else []


# ----------------------------------------------------------------------------- companies / instruments
def test_add_company_appends_one_flow_line_and_backs_up(settings):
    before = _read(settings, "companies")
    entry, bak = T.add_company(settings, "Acme Genomics", ticker="ACME", type_="tools", aliases=["Acme", "Acme Genomics Inc"],
                               domains=["acmegenomics.com"], watch_news=True)
    after = _read(settings, "companies")
    assert entry["id"] == "acme-genomics"
    assert after.splitlines()[:-1] == before.splitlines()  # every original line untouched, in order
    last = after.splitlines()[-1]
    assert last.startswith("- {id: acme-genomics, name: Acme Genomics, ticker: ACME, type: tools, watch_news: true, domains: [acmegenomics.com], aliases: [")
    assert yaml.safe_load(last) == [entry]
    gz = Gazetteer.from_yaml(yaml.safe_load(after), "company")
    assert {m.entity_id for m in gz.find("Samples were provided by Acme Genomics Inc (ticker ACME).")} == {"acme-genomics"}
    assert bak is not None and bak.read_text(encoding="utf-8") == before
    assert bak.parent == settings.cache_dir / "config-backups"
    # the same checks `check-config` runs find nothing new
    assert T.gazetteer_problems(yaml.safe_load(after), "company") == T.gazetteer_problems(yaml.safe_load(before), "company")


def test_duplicates_and_alias_collisions_are_refused(settings):
    before = _read(settings, "companies")
    with pytest.raises(T.TrackError, match="already exists"):
        T.add_company(settings, "Illumina")
    with pytest.raises(T.TrackError, match="already a name/alias of pacbio"):
        T.add_company(settings, "Foo Corp", aliases=["PacBio"])
    with pytest.raises(T.TrackError, match="already a name/alias"):
        T.add_company(settings, "pacbio")  # case-insensitive on the name too
    assert _read(settings, "companies") == before
    assert _backups(settings) == []  # refused before any write


def test_rollback_restores_the_file_when_validation_fails(settings, monkeypatch):
    before = _read(settings, "companies")

    def broken_validator(kind, baseline):
        def validate(path):
            raise T.TrackError("simulated validation failure")

        return validate

    monkeypatch.setattr(T, "_validator_for_gazetteer", broken_validator)
    with pytest.raises(T.TrackError, match="rolled back"):
        T.add_company(settings, "Acme Genomics", ticker="ACME")
    assert _read(settings, "companies") == before
    assert len(_backups(settings)) == 1  # the backup was taken before the write


def test_untrack_company_restores_original_and_unknown_is_refused(settings):
    before = _read(settings, "companies")
    T.add_company(settings, "Acme Genomics", ticker="ACME")
    removed, bak = T.remove_gazetteer_entry(settings, "company", "Acme Genomics")  # by name, case-insensitive
    assert removed["id"] == "acme-genomics"
    assert _read(settings, "companies") == before
    with pytest.raises(T.TrackError, match="no entry"):
        T.remove_gazetteer_entry(settings, "company", "does-not-exist")


def test_add_instrument_infers_ticker_and_id_prefix_from_vendor(settings):
    entry, _ = T.add_instrument(settings, "Vega 2", vendor="PacBio", category="sequencer.long_read", aliases=["Vega 2 system"], context_terms=["PacBio"], launched=2027)
    assert entry["vendor"] == "Pacific Biosciences" and entry["ticker"] == "PACB"
    assert entry["id"] == "pacb-vega-2"  # follows the vendor's existing id convention
    assert entry["context_required"] is True and entry["context_terms"] == ["PacBio"]
    data = yaml.safe_load(_read(settings, "instruments"))
    assert data["entries"][-1] == entry
    gz = Gazetteer.from_yaml(data, "instrument")
    assert [m.entity_id for m in gz.find("Reads were generated on a Vega 2 system (PacBio).")] == ["pacb-vega-2"]
    assert not gz.find("The Vega 2 system telescope")  # context required
    with pytest.raises(T.TrackError, match="not in companies.yaml"):
        T.add_instrument(settings, "Z1", vendor="Nobody Corp", category="sequencer")
    e2, _ = T.add_instrument(settings, "Z1", vendor="Nobody Corp", category="sequencer", ticker="private")
    assert e2["id"] == "nobody-z1" and e2["ticker"] == "private"
    with pytest.raises(T.TrackError, match="category must start with"):
        T.add_instrument(settings, "Z2", vendor="PacBio", category="telescope")
    removed, _ = T.remove_gazetteer_entry(settings, "instrument", "pacb-vega-2")
    assert removed["name"] == "Vega 2"


def test_format_flow_entry_quotes_only_when_needed():
    entry = {"id": "yes-co", "name": "Yes", "ticker": "n/a", "type": "tools", "watch_news": False, "domains": [],
             "aliases": ["Yes: Co", "plain"], "notes": "has # hash"}
    line = T.format_flow_entry(entry, T.COMPANY_KEY_ORDER)
    assert line.startswith('- {id: yes-co, name: "Yes", ticker: n/a, type: tools, watch_news: false, domains: [], aliases: ["Yes: Co", "plain"]')
    assert yaml.safe_load(line) == [entry]
    assert T.kebab("Schrödinger, Inc.") == "schrodinger-inc"
    assert T.kebab("10x Genomics") == "10x-genomics"


# ----------------------------------------------------------------------------- subgroups.yaml
def test_add_author_is_a_line_insert_that_preserves_comments_and_order(settings):
    before = _read(settings, "subgroups").splitlines()
    author, _ = T.add_author(settings, "spatial", "Jane Q. Example", openalex_id="A5000000001", institution="Example University")
    after = _read(settings, "subgroups").splitlines()
    new = [ln for ln in after if "Jane Q. Example" in ln]
    assert len(new) == 1
    assert new[0] == '      - {name: "Jane Q. Example", openalex_id: "A5000000001", institution: "Example University"}'
    assert [ln for ln in after if ln != new[0]] == before  # nothing else moved or changed
    i = after.index(new[0])
    assert after[i - 1].startswith('      - {name: "Xiao Wang"')  # appended after the last spatial author
    assert after[i + 1].startswith("  - id: long_read")
    data = yaml.safe_load("\n".join(after))
    spatial = next(g for g in data["subgroups"] if g["id"] == "spatial")
    assert spatial["tracked_authors"][-1] == author
    with pytest.raises(T.TrackError, match="already tracked"):
        T.add_author(settings, "spatial", "jane example")
    with pytest.raises(T.TrackError, match="not found"):
        T.add_author(settings, "nope", "Someone")
    removed, _ = T.remove_author(settings, "Jane Q. Example")
    assert removed == [("spatial", "Jane Q. Example")]
    assert _read(settings, "subgroups").splitlines() == before


def test_topic_edits_keep_the_trailing_comment(settings):
    before = _read(settings, "subgroups").splitlines()
    topic_line = next(ln for ln in before if ln.startswith("    openalex_topics: [T11289, T10857]"))
    comment = topic_line.split("]", 1)[1]
    assert comment.strip().startswith("#")
    group, _ = T.edit_subgroup(settings, "spatial", add_keywords=["Stereo-seq", "Xenium"], add_topics=["T99999"])
    after = _read(settings, "subgroups").splitlines()
    new_topic_line = next(ln for ln in after if ln.startswith("    openalex_topics: [T11289, T10857, T99999]"))
    assert new_topic_line.endswith(comment)
    assert group["keywords"][-1] == "Stereo-seq" and group["keywords"].count("Xenium") == 1  # existing keyword not duplicated
    assert len(after) == len(before)  # in-place line replacements only
    group, _ = T.edit_subgroup(settings, "spatial", remove_keywords=["Stereo-seq"], remove_topics=["T99999"])
    assert _read(settings, "subgroups").splitlines() == before
    with pytest.raises(T.TrackError, match="look like T11289"):
        T.edit_subgroup(settings, "spatial", add_topics=["11289"])


def test_create_subgroup_add_author_and_remove(settings):
    before = _read(settings, "subgroups")
    with pytest.raises(T.TrackError, match="pass --name"):
        T.edit_subgroup(settings, "organoids", add_keywords=["organoid"])
    group, _ = T.edit_subgroup(settings, "Organoids", add_keywords=["organoid", "assembloid"], add_topics=["T12345"], name="Organoids & disease models")
    assert group == {"id": "organoids", "name": "Organoids & disease models", "openalex_topics": ["T12345"], "keywords": ["organoid", "assembloid"], "tracked_authors": []}
    author, _ = T.add_author(settings, "organoids", "Example Researcher", orcid="0000-0001-2345-6789")
    data = yaml.safe_load(_read(settings, "subgroups"))
    org = data["subgroups"][-1]
    assert org["tracked_authors"] == [{"name": "Example Researcher", "openalex_id": "", "orcid": "0000-0001-2345-6789", "institution": ""}]
    assert "tracked_authors: []" not in _read(settings, "subgroups").split("- id: organoids", 1)[1]
    T.remove_subgroup(settings, "organoids")
    assert _read(settings, "subgroups") == before


# ----------------------------------------------------------------------------- radar.yaml
def test_watchlist_term_add_and_remove(settings):
    before = _read(settings, "radar")
    T.add_term(settings, "organoid: intestinal")
    radar = yaml.safe_load(_read(settings, "radar"))
    assert radar["watchlist_terms"][-1] == "organoid: intestinal"
    assert radar["watchlist_terms"][:-1] == yaml.safe_load(before)["watchlist_terms"]
    with pytest.raises(T.TrackError, match="already"):
        T.add_term(settings, "CTDNA")
    T.remove_term(settings, "Organoid: Intestinal")
    assert _read(settings, "radar") == before
    with pytest.raises(T.TrackError, match="not found"):
        T.remove_term(settings, "nothing like this")


def test_priorities_write_update_clear_and_multiplier(settings):
    before = _read(settings, "radar")
    T.set_priority(settings, "subgroup", "spatial", 1.5)
    T.set_priority(settings, "company", "illumina", 2)
    T.set_priority(settings, "journal", "Nature Methods", 1.25)
    T.set_priority(settings, "instrument-category", "sequencer", 1.1)
    T.set_priority(settings, "source", "europepmc", 0.8)
    T.set_priority(settings, "subgroup", "spatial", 1.75)  # update in place
    text = _read(settings, "radar")
    assert text.startswith(before)  # append-only: the original file is a prefix
    assert text.count("    spatial:") == 1
    radar = yaml.safe_load(text)
    assert radar["priorities"] == {"subgroup": {"spatial": 1.75}, "company": {"illumina": 2}, "journal": {"Nature Methods": 1.25},
                                   "instrument_category": {"sequencer": 1.1}, "source": {"europepmc": 0.8}}
    pri = T.load_priorities(radar)
    assert pri["journal"] == {"nature methods": 1.25}
    mentions = [{"kind": "instrument", "entity_id": "ilmn-novaseq-x", "ticker": "ILMN", "vendor": "Illumina", "category": "sequencer.short_read"},
                {"kind": "instrument", "entity_id": "ilmn-miseq", "ticker": "ILMN", "vendor": "Illumina", "category": "sequencer.short_read"}]
    paper = {"source": "europepmc", "journal": "Nature Methods", "extra_json": json.dumps({"sources": ["crossref", "europepmc"]})}
    m = T.priority_multiplier(paper, mentions, "Nature Methods", ["spatial", "single_cell"], priorities=radar["priorities"])
    assert m == pytest.approx(1.75 * 2 * 1.25 * 1.1 * 0.8)  # best per kind, kinds multiply, duplicates do not compound
    assert T.priority_multiplier({"source": "arxiv"}, [], "", [], priorities=radar["priorities"]) == 1.0
    assert T.priority_multiplier({"source": "arxiv"}, [], "", [], priorities={}) == 1.0
    T.set_priority(settings, "company", "illumina", None)
    T.set_priority(settings, "source", "europepmc", None)
    radar = yaml.safe_load(_read(settings, "radar"))
    assert "company" not in radar["priorities"] and "source" not in radar["priorities"]  # empty kinds are dropped
    assert radar["priorities"]["subgroup"] == {"spatial": 1.75}
    with pytest.raises(T.TrackError, match="no company priorit"):
        T.set_priority(settings, "company", "illumina", None)  # the empty kind header was dropped above
    T.set_priority(settings, "subgroup", "single_cell", 1.2)
    with pytest.raises(T.TrackError, match="no subgroup priority for 'mced'"):
        T.set_priority(settings, "subgroup", "mced", None)
    with pytest.raises(T.TrackError, match="weight must be"):
        T.set_priority(settings, "company", "illumina", 0)
    with pytest.raises(T.TrackError, match="kind must be"):
        T.set_priority(settings, "ticker", "ILMN", 2)


# ----------------------------------------------------------------------------- SEC ticker lookup
CANNED_SEC = {
    "0": {"cik_str": 1110803, "ticker": "ILMN", "title": "ILLUMINA, INC."},
    "1": {"cik_str": 1770787, "ticker": "TXG", "title": "10x Genomics, Inc."},
    "2": {"cik_str": 1299130, "ticker": "PACB", "title": "PACIFIC BIOSCIENCES OF CALIFORNIA, INC."},
    "3": {"cik_str": 313616, "ticker": "DHR", "title": "DANAHER CORP /DE/"},
    "4": {"cik_str": 1581280, "ticker": "TWST", "title": "Twist Bioscience Corp"},
    "5": {"cik_str": 99, "ticker": "SEER", "title": "Seer, Inc."},
    "6": {"cik_str": 98, "ticker": "GH", "title": "Guardant Health, Inc."},
    "7": {"cik_str": 97, "ticker": "NDRA", "title": "ENDRA Life Sciences Inc."},
    "8": {"cik_str": 96, "ticker": "AZTA", "title": "Azenta, Inc."},
}


class _FakeResp:
    def __init__(self, payload, status):
        self._payload, self.status_code = payload, status

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeHttp:
    def __init__(self, contact_email="noreply@example.org", payload=CANNED_SEC, status=200):
        self.contact_email = contact_email
        self.payload, self.status = payload, status
        self.calls = 0
        self.headers = None

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,)):
        self.calls += 1
        self.headers = headers
        assert url == T.SEC_TICKERS_URL
        return _FakeResp(self.payload, self.status)


def test_sec_lookup_uses_canned_json_and_caches(settings):
    http = _FakeHttp()
    rows = T.sec_company_tickers(http, settings.cache_dir)
    assert http.headers["User-Agent"] == "lit-radar/0.1 noreply@example.org"  # the SEC's "<name> <e-mail>" format, no URL
    assert len(rows) == 9 and rows[0] == {"cik": "1110803", "ticker": "ILMN", "title": "ILLUMINA, INC."}
    assert (settings.cache_dir / "sec_company_tickers.json").exists()
    assert T.sec_company_tickers(http, settings.cache_dir) == rows and http.calls == 1  # served from the cache
    with pytest.raises(T.TrackError, match="contact e-mail"):
        T.sec_company_tickers(_FakeHttp(contact_email=""), settings.cache_dir / "other")
    with pytest.raises(T.TrackError, match=r"could not download .*\(403\)"):
        T.sec_company_tickers(_FakeHttp(payload=None, status=403), settings.cache_dir / "other2")

    best, conf, _ = T.match_ticker("Illumina", rows)
    assert best["ticker"] == "ILMN" and conf == 1.0
    best, conf, _ = T.match_ticker("10x Genomics", rows)
    assert best["ticker"] == "TXG" and conf == 1.0
    best, conf, _ = T.match_ticker("Pacific Biosciences", rows)
    assert best["ticker"] == "PACB" and conf >= T.TICKER_MIN_CONFIDENCE
    best, conf, _ = T.match_ticker("Danaher Corporation", rows)
    assert best["ticker"] == "DHR"
    best, conf, _ = T.match_ticker("Twist Bioscience", rows)
    assert best["ticker"] == "TWST"
    best, conf, top = T.match_ticker("Acme Genomics", rows)
    assert best is None and conf < T.TICKER_MIN_CONFIDENCE  # never guess below the threshold
    # seen live: "Azenta Life Sciences" scored 0.87 against "ENDRA Life Sciences" by edit distance alone
    best, conf, top = T.match_ticker("Azenta Life Sciences", rows)
    assert best is None and conf < T.TICKER_MIN_CONFIDENCE
    assert "NDRA" in {r["ticker"] for _, r in top}  # still shown as a candidate for the user to judge
    assert T.match_ticker("", rows) == (None, 0.0, [])


# ----------------------------------------------------------------------------- CLI round trip
def test_cli_track_prioritize_untrack_round_trip(settings, capsys):
    cfg = str(settings.config_dir)
    before_c, before_r = _read(settings, "companies"), _read(settings, "radar")
    assert main(["--config-dir", cfg, "track", "company", "Acme Genomics", "--ticker", "ACME", "--alias", "Acme", "--watch-news", "--no-lookup"]) == 0
    out = capsys.readouterr().out
    assert "added to companies.yaml" in out and "backup:" in out
    assert main(["--config-dir", cfg, "track", "list", "company"]) == 0
    assert "acme-genomics" in capsys.readouterr().out
    assert main(["--config-dir", cfg, "track", "company", "Acme Genomics", "--no-lookup"]) == 1  # duplicate
    assert "already exists" in capsys.readouterr().err
    assert main(["--config-dir", cfg, "prioritize", "company", "acme", "--weight", "1.5"]) == 0  # resolved via alias
    assert "priorities.company.acme-genomics = 1.5" in capsys.readouterr().out
    assert main(["--config-dir", cfg, "prioritize", "journal", "No Such Journal", "--weight", "2"]) == 1
    assert main(["--config-dir", cfg, "prioritize", "list"]) == 0
    assert "acme-genomics" in capsys.readouterr().out
    assert yaml.safe_load(_read(settings, "radar"))["priorities"] == {"company": {"acme-genomics": 1.5}}
    assert main(["--config-dir", cfg, "prioritize", "company", "acme-genomics", "--clear"]) == 0
    assert main(["--config-dir", cfg, "track", "term", "organoid"]) == 0
    assert main(["--config-dir", cfg, "untrack", "term", "organoid"]) == 0
    assert main(["--config-dir", cfg, "track", "author", "Example Researcher", "--subgroup", "spatial", "--openalex", "A5000000009"]) == 0
    assert main(["--config-dir", cfg, "untrack", "author", "A5000000009"]) == 0
    assert main(["--config-dir", cfg, "untrack", "company", "acme-genomics"]) == 0
    assert _read(settings, "companies") == before_c
    # priorities header + section remain (append-only), but with no entries the file still parses and yields no multipliers
    assert _read(settings, "radar").startswith(before_r)
    assert T.load_priorities(yaml.safe_load(_read(settings, "radar"))) == {}
    assert main(["--config-dir", cfg, "check-config"]) == 0


def test_tracked_summary_counts_match_the_yaml(settings):
    s = T.tracked_summary(settings)
    assert len(s["companies"]) == len(yaml.safe_load(_read(settings, "companies"))["entries"])
    assert len(s["instruments"]) == len(yaml.safe_load(_read(settings, "instruments"))["entries"])
    groups = yaml.safe_load(_read(settings, "subgroups"))["subgroups"]
    assert len(s["authors"]) == sum(len(g.get("tracked_authors") or []) for g in groups)
    assert [t["id"] for t in s["topics"]] == [g["id"] for g in groups]
    assert s["terms"] == yaml.safe_load(_read(settings, "radar"))["watchlist_terms"]
    assert s["priorities"] == {}
