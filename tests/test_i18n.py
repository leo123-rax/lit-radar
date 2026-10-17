"""Language detection, country mapping, store tagging and the (mocked) translation path. No network."""
import json
import os
import sys
import types

import pytest

from litradar import i18n
from litradar.models import Author, Paper
from litradar.store import Store

# ----------------------------------------------------------------------------------------------
# (a) detect_language
# ----------------------------------------------------------------------------------------------
LANG_CASES = [
    ("zh", "基于单细胞测序的肿瘤微环境异质性研究"),
    ("zh", "CRISPR-Cas9介导的基因编辑技术研究进展"),  # Latin acronym inside a Chinese title
    ("ja", "シングルセルRNAシーケンスによる腫瘍微小環境の解析"),
    ("ja", "遺伝子発現の解析"),  # one hiragana particle is enough to separate Japanese from Chinese
    ("ko", "단일세포 RNA 시퀀싱을 이용한 종양 미세환경 분석"),
    ("ru", "Анализ микроокружения опухоли методом секвенирования единичных клеток"),
    ("es", "Análisis de la heterogeneidad del microambiente tumoral mediante secuenciación de célula única"),
    ("pt", "Análise da heterogeneidade do microambiente tumoral por sequenciamento de célula única"),
    ("fr", "Analyse de l'hétérogénéité du microenvironnement tumoral par séquençage de cellules uniques"),
    ("de", "Analyse der Heterogenität der Tumormikroumgebung mittels Einzelzellsequenzierung"),
    ("en", "Single-cell sequencing of the tumour microenvironment reveals heterogeneity"),
    ("en", "Spatial transcriptomics of liver cancer (肝癌空间转录组研究)"),  # English dominates the mixed string
    ("en", "Xenium"),  # no stopword evidence: English is the right prior for a title
    ("en", "Giant nonlocal modulation of magnetization reversal in van der Waals ferromagnets"),  # loan phrase, not German
    ("en", "De novo design of protein binders validated in vitro and in vivo"),  # loan phrases, not Spanish
    ("en", "Bone marrow ménage à trois balances neutrophil production"),  # French loanwords in an English title
    ("en", "Award-winning microscopy image disqualified over AI use"),  # 'over' is English here, not Dutch
    ("other", "Analisi dell'eterogeneità del microambiente tumorale nelle pazienti"),  # Italian
    ("other", "Een analyse van de heterogeniteit van deze tumoren: resultaten en onderzoek"),  # Dutch
    ("other", "Ανάλυση του μικροπεριβάλλοντος του όγκου"),  # Greek
    ("other", ""),
    ("other", None),
    ("other", "12345 --- 67 ***"),
]


@pytest.mark.parametrize("expected,text", LANG_CASES)
def test_detect_language(expected, text):
    assert i18n.detect_language(text) == expected


def test_detect_language_codes_are_iso639_1():
    for _, text in LANG_CASES:
        assert i18n.detect_language(text) in i18n.LANGS


def test_paper_language_prefers_non_latin_title():
    # Chinese-journal pattern: Chinese title, English abstract -> the paper is tagged zh
    assert i18n.paper_language("肿瘤微环境研究", "We profiled the tumour microenvironment of 40 patients with single-cell RNA sequencing.") == "zh"
    assert i18n.paper_language("A short title", "Esto es un estudio de la expresión de los genes en el cáncer de mama con secuenciación.") == "es"


# ----------------------------------------------------------------------------------------------
# (b) countries_in / country_of
# ----------------------------------------------------------------------------------------------
AFF_CASES = [
    (["CN"], "State Key Laboratory of Membrane Biology, Tsinghua University, Beijing 100084, China"),
    (["CN"], "BGI-Shenzhen, Shenzhen 518083"),
    (["CN"], "Fudan University"),
    (["CN"], "School of Medicine, Hangzhou, Zhejiang"),
    (["CN"], "Chinese Academy of Sciences"),
    (["CN"], "Department of Bioinformatics, Shanghai Jiao Tong University"),
    (["CN"], "Sun Yat-sen University"),
    (["CN"], "The First Affiliated Hospital of Zhengzhou University , , ,"),
    (["CN"], "Guangzhou National Laboratory, Guangzhou, China"),
    (["CN"], "中国科学院上海生命科学研究院"),
    (["HK"], "Chinese University of Hong Kong"),  # consumed as one phrase: no stray CN from "Chinese"
    (["HK", "CN"], "Hong Kong University of Science and Technology (Guangzhou)"),
    (["TW"], "National Taiwan University, Taipei"),
    (["TW"], "China Medical University Hospital, Taichung"),  # ambiguous name consumed, city decides
    (["JP"], "RIKEN Center for Biosystems Dynamics Research, Kobe, Japan"),
    (["JP"], "Graduate School of Medicine, Kyoto University"),
    (["JP"], "東京大学医科学研究所"),
    (["KR"], "Seoul National University College of Medicine"),
    (["KR"], "서울대학교 의과대학"),
    (["US"], "Department of Genetics, Stanford University School of Medicine, Stanford, CA 94305, USA"),
    (["US"], "Harvard Medical School, Boston, MA 02115"),
    (["US"], "Houston, TX"),  # trailing state code is the last resort
    (["US"], "Department of Emergency Medicine, Intermountain Medical Center, Murray, Utah"),
    (["US"], "University of Georgia, Athens, GA"),
    (["GB"], "Wellcome Sanger Institute, Hinxton, UK"),
    (["GB"], "Queen's University Belfast, Northern Ireland"),  # not IE
    (["GB"], "University of Cambridge, Cambridge CB2 1TN, United Kingdom"),
    (["IE"], "Trinity College Dublin, Ireland"),
    (["DE"], "Universität Tübingen, Tübingen, Germany"),
    (["DE"], "Heinrich-Heine-University Duesseldorf"),
    (["NL"], "Vrije Universiteit Amsterdam"),
    (["BE"], "Department of Biology, KU Leuven"),
    (["FR"], "Université Paris Cité, CNRS, Saints-Pères Paris Institute for the Neurosciences"),
    (["BR"], "Universidade Federal de Santa Maria (UFSM), Santa Maria, Rio Grande do Sul, Brazil"),
    (["RU"], "Ophthalmology Clinic, Moscow, Russia"),
    (["RU"], "Московский государственный университет"),
    (["IN"], "Pharmacology, Jawaharlal Institute of Postgraduate Medical Education and Research, Karaikal, IND"),
    (["AU"], "Perth, WA, Australia"),  # WA is Western Australia here, not Washington
    (["SG"], "School of Biological Sciences, Nanyang Technological University, 60 Nanyang Drive, 637551, Singapore"),
    (["ZA"], "South African Medical Research Council"),
    ([], "Cambridge"),  # ambiguous on its own: no guess
    ([], "Georgia"),  # US state or country: no guess
    ([], "Boehringer Ingelheim RCV GmbH & Co KG"),
    ([], ""),
]


@pytest.mark.parametrize("expected,text", AFF_CASES)
def test_countries_in(expected, text):
    assert i18n.countries_in(text) == expected


def test_country_of_prefers_openalex_country_codes():
    p = Paper(
        id="doi:10.1/x", source="openalex", title="T",
        authors=[
            Author(name="A", affiliation="Somewhere in Paris", institutions=[{"name": "Inst", "type": "education", "ror": "r", "country_code": "JP"}]),
            Author(name="B", affiliation="Berlin", institutions=[{"name": "Inst2", "type": "education", "ror": "r2", "country_code": "jp"}]),
            Author(name="C", affiliation="Beijing", institutions=[{"name": "Inst3", "type": "education", "ror": "r3", "country_code": "US"}]),
        ],
        affiliations=["Paris, France"],
    )
    assert i18n.country_of(p) == ["JP", "US"]  # exact codes win over the affiliation text; JP has 2 authors
    assert i18n.country_counts(p) == {"JP": 2, "US": 1}


def test_country_of_from_extra_institution_countries():
    row = {"extra_json": json.dumps({"institution_countries": ["CN", "CN", "GB"]}), "authors_json": "[]", "affiliations_json": json.dumps(["Boston, MA"])}
    assert i18n.country_of(row) == ["CN", "GB"]


def test_country_of_from_affiliations_orders_by_frequency():
    row = {
        "extra_json": "{}",
        "authors_json": json.dumps([{"name": "A", "affiliation": "Tsinghua University, Beijing"}, {"name": "B", "affiliation": "Zhejiang University, Hangzhou"}]),
        "affiliations_json": json.dumps(["Tsinghua University, Beijing", "Zhejiang University, Hangzhou", "Broad Institute, Cambridge, MA 02142, USA"]),
    }
    assert i18n.country_of(row) == ["CN", "US"]
    assert i18n.country_of({"extra_json": "not json", "authors_json": None, "affiliations_json": "[]"}) == []
    assert i18n.country_of("nonsense") == []


# ----------------------------------------------------------------------------------------------
# (d) tag_store + histograms
# ----------------------------------------------------------------------------------------------
def _paper(i, **kw):
    base = dict(id=f"doi:10.1000/i18n{i}", source="europepmc", title=f"Paper {i}", abstract="", journal="J", pub_date="2026-10-12", url=f"https://e/{i}")
    base.update(kw)
    return Paper(**base)


def _fill(store):
    store.upsert_paper(_paper(1, title="Spatial transcriptomics of the tumour microenvironment with Xenium", abstract="We used the Xenium platform on 30 patients.",
                              affiliations=["Broad Institute of MIT and Harvard, Cambridge, MA 02142, USA", "Dana-Farber Cancer Institute, Boston, MA"]))
    store.upsert_paper(_paper(2, title="基于单细胞测序的肿瘤微环境异质性研究", abstract="Single-cell sequencing was performed on tumour samples from 40 patients.",
                              affiliations=["State Key Laboratory of Genetic Engineering, Fudan University, Shanghai 200438, China", "BGI-Shenzhen, Shenzhen, China"],
                              authors=[Author(name="Li Wei", affiliation="Fudan University, Shanghai"), Author(name="Zhang San", affiliation="BGI-Shenzhen")]))
    store.upsert_paper(_paper(3, title="遺伝子発現の解析による腫瘍免疫の研究", abstract="",
                              affiliations=["RIKEN Center for Integrative Medical Sciences, Yokohama, Japan"]))
    store.upsert_paper(_paper(4, title="Análisis de la expresión génica en el cáncer de mama", abstract="Estudio de 200 pacientes con secuenciación de ARN.", pub_date="2025-01-05",
                              affiliations=["Hospital Universitario La Paz, Madrid, Spain", "CNIO, Madrid"]))
    store.upsert_paper(_paper(5, title="Untraceable paper", abstract="", affiliations=[], extra={"emails": ["a@b.c"]}))
    store.commit()


def test_tag_store_writes_lang_and_countries(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _fill(st)
    rep = i18n.tag_store(st)
    assert rep["papers"] == 5 and rep["tagged"] == 5 and rep["with_country"] == 4
    assert rep["languages"] == {"en": 2, "zh": 1, "ja": 1, "es": 1}
    assert rep["countries"]["CN"] == 1 and rep["countries"]["US"] == 1 and rep["countries"]["JP"] == 1 and rep["countries"]["ES"] == 1 and rep["countries"]["??"] == 1
    e1 = json.loads(st.get_paper("doi:10.1000/i18n1")["extra_json"])
    assert e1["lang"] == "en" and e1["countries"] == ["US"]
    e2 = json.loads(st.get_paper("doi:10.1000/i18n2")["extra_json"])
    assert e2["lang"] == "zh" and e2["countries"] == ["CN"]
    e5 = json.loads(st.get_paper("doi:10.1000/i18n5")["extra_json"])
    assert e5["lang"] == "en" and e5["countries"] == [] and e5["emails"] == ["a@b.c"]  # existing keys preserved
    # idempotent
    assert i18n.tag_store(st)["languages"] == rep["languages"]
    # window filter: only the 2026 papers
    rep2 = i18n.tag_store(st, since="2026-01-01")
    assert rep2["papers"] == 4 and "es" not in rep2["languages"]
    h = i18n.histograms(st, since="2026-01-01")
    assert h["papers"] == 4 and h["tagged"] == 4 and h["languages"] == {"en": 2, "zh": 1, "ja": 1}
    assert h["primary_countries"] == {"US": 1, "CN": 1, "JP": 1, "??": 1}
    assert st.conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 0  # table exists, empty


def test_translation_candidates_skip_english_and_translated(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _fill(st)
    cands = i18n.translation_candidates(st)  # tags not written yet: computed on the fly
    assert {c["id"] for c in cands} == {"doi:10.1000/i18n2", "doi:10.1000/i18n3", "doi:10.1000/i18n4"}
    i18n.save_translation(st, "doi:10.1000/i18n3", "ja", "Study of tumour immunity", "", "m")
    assert {c["id"] for c in i18n.translation_candidates(st, since="2026-01-01")} == {"doi:10.1000/i18n2"}
    assert i18n.translation_candidates(st, max_items=1)[0]["id"] in {"doi:10.1000/i18n2", "doi:10.1000/i18n4"}


# ----------------------------------------------------------------------------------------------
# (c) translate_titles with a fake client
# ----------------------------------------------------------------------------------------------
class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Message:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)] if text is not None else []
        self.stop_reason = stop_reason


class _Result:
    def __init__(self, custom_id, kind, message=None):
        self.custom_id = custom_id
        self.result = types.SimpleNamespace(type=kind, message=message)


class FakeBatches:
    def __init__(self, outcomes, end_after=1):
        self.outcomes = outcomes  # custom_id -> _Result
        self.created = []
        self.retrieves = 0
        self.end_after = end_after

    def create(self, requests):
        self.created.append(requests)
        return types.SimpleNamespace(id="msgbatch_test", processing_status="in_progress")

    def retrieve(self, batch_id):
        self.retrieves += 1
        return types.SimpleNamespace(id=batch_id, processing_status="ended" if self.retrieves >= self.end_after else "in_progress")

    def results(self, batch_id):
        yield from self.outcomes.values()


class FakeClient:
    def __init__(self, outcomes, end_after=1):
        self.messages = types.SimpleNamespace(batches=FakeBatches(outcomes, end_after), create=self._create)
        self.sync_calls = []

    def _create(self, **params):
        self.sync_calls.append(params)
        title = params["messages"][0]["content"].split("TITLE: ", 1)[1].split("\n", 1)[0]
        return _Message(json.dumps({"language": "zh", "title_en": f"EN<{title}>", "abstract_en": "abs"}))


def test_translate_titles_batch_path(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st = Store(tmp_path / "t.sqlite")
    _fill(st)
    i18n.tag_store(st)
    # candidates are ordered pub_date DESC then id: i18n2 (zh), i18n3 (ja), then i18n4 (es, 2025)
    outcomes = {
        "tr-0": _Result("tr-0", "succeeded", _Message(json.dumps({"language": "ZH", "title_en": "Heterogeneity of the tumour microenvironment by single-cell sequencing", "abstract_en": "Single-cell sequencing was performed on tumour samples from 40 patients."}))),
        "tr-1": _Result("tr-1", "succeeded", _Message("I cannot help with that.", stop_reason="refusal")),  # refusal -> skipped
        "tr-2": _Result("tr-2", "errored"),  # API-side error -> counted
        "tr-99": _Result("tr-99", "succeeded", _Message(json.dumps({"language": "xx", "title_en": "orphan", "abstract_en": ""}))),  # unknown id ignored
    }
    client = FakeClient(outcomes, end_after=2)
    rep = i18n.translate_titles(st, model="claude-opus-5-5", client=client, poll_seconds=0, max_wait_seconds=10)
    assert rep["status"] == "ok" and rep["candidates"] == 3 and rep["submitted"] == 3
    assert rep["translated"] == 1 and rep["refused_or_invalid"] == 1 and rep["errored"] == 1 and rep["batch_id"] == "msgbatch_test"
    # one Message Batches request carrying all candidates, structured JSON output, the configured model
    reqs = client.messages.batches.created[0]
    assert len(client.messages.batches.created) == 1 and [r["custom_id"] for r in reqs] == ["tr-0", "tr-1", "tr-2"]
    params = reqs[0]["params"]
    assert params["model"] == "claude-opus-5-5"
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": i18n.TRANSLATION_SCHEMA}}
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "LANGUAGE HINT (heuristic, may be wrong): zh" in params["messages"][0]["content"]
    assert "基于单细胞测序" in params["messages"][0]["content"]
    rows = st.conn.execute("SELECT paper_id, lang, title_en, abstract_en, model FROM translations").fetchall()
    assert rows == [("doi:10.1000/i18n2", "zh", "Heterogeneity of the tumour microenvironment by single-cell sequencing", "Single-cell sequencing was performed on tumour samples from 40 patients.", "claude-opus-5-5")]
    assert i18n.translations_for(st, ["doi:10.1000/i18n2", "missing"]) == {"doi:10.1000/i18n2": {"lang": "zh", "title_en": rows[0][2], "abstract_en": rows[0][3], "model": "claude-opus-5-5"}}
    # second run: the translated paper is no longer a candidate, the refused/errored ones are retried
    client2 = FakeClient({}, end_after=1)
    rep2 = i18n.translate_titles(st, client=client2, poll_seconds=0, max_wait_seconds=1)
    assert rep2["candidates"] == 2 and rep2["translated"] == 0


def test_translate_titles_batch_never_ends(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _fill(st)
    client = FakeClient({"tr-0": _Result("tr-0", "succeeded", _Message(json.dumps({"language": "zh", "title_en": "x", "abstract_en": ""})))}, end_after=10**6)
    rep = i18n.translate_titles(st, client=client, poll_seconds=1, max_wait_seconds=2)
    assert rep["status"] == "incomplete" and rep["translated"] == 0 and client.messages.batches.retrieves == 2
    assert st.conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 0


def test_translate_titles_sync_path_and_malformed_json(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    _fill(st)
    client = FakeClient({})
    rep = i18n.translate_titles(st, client=client, use_batch=False)
    assert rep["translated"] == 3 and rep["submitted"] == 3 and len(client.sync_calls) == 3
    assert st.conn.execute("SELECT title_en FROM translations WHERE paper_id='doi:10.1000/i18n3'").fetchone()[0] == "EN<遺伝子発現の解析による腫瘍免疫の研究>"
    # malformed / truncated / empty answers are skipped, never raised
    assert i18n._parse_translation(_Message("{not json")) is None
    assert i18n._parse_translation(_Message('{"language": "zh", "title_en": "", "abstract_en": ""}')) is None
    assert i18n._parse_translation(_Message('{"language": "zh", "title_en": "ok"', stop_reason="max_tokens")) is None
    assert i18n._parse_translation(_Message(None)) is None
    assert i18n._parse_translation(_Message(json.dumps({"language": "zh", "title_en": "T", "abstract_en": "A"})))["title_en"] == "T"


def test_translate_titles_skips_without_key_and_errors_without_sdk(tmp_path, monkeypatch):
    st = Store(tmp_path / "t.sqlite")
    _fill(st)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rep = i18n.translate_titles(st)
    assert rep["status"] == "skipped" and "ANTHROPIC_API_KEY" in rep["reason"]
    # key present but the optional SDK is missing -> one clear error, no network
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(RuntimeError, match="pip install"):
        i18n.translate_titles(st, api_key="sk-test")
    # no candidates -> never tries to import the SDK at all
    for pid in ("doi:10.1000/i18n2", "doi:10.1000/i18n3", "doi:10.1000/i18n4"):
        i18n.save_translation(st, pid, "zz", "t", "", "m")
    assert i18n.translate_titles(st, api_key="sk-test")["candidates"] == 0


def test_typed_request_falls_back_to_dict_without_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    r = i18n._typed_request("tr-0", {"model": "m", "max_tokens": 1, "messages": []})
    assert r == {"custom_id": "tr-0", "params": {"model": "m", "max_tokens": 1, "messages": []}}


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------
def test_cli_tag_subcommand(tmp_path, monkeypatch, capsys):
    from litradar import cli

    st = Store(tmp_path / "cli.sqlite")
    _fill(st)
    st.close()
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "cli.sqlite"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(["tag", "--since", "3650d", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tagged"] == 5 and out["languages"]["zh"] == 1 and "translation" not in out
    assert cli.main(["tag", "--translate"]) == 0
    text = capsys.readouterr().out
    assert "skipped" in text and "ANTHROPIC_API_KEY" in text
    os.environ.pop("LITRADAR_DB", None)
