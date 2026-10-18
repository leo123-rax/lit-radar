"""The hand-rolled .env parser, Settings defaults, and the config-directory checks behind `check-config`."""
import os

import pytest
import yaml

from litradar.config import (
    DEFAULT_LLM_MODEL,
    REPO_ROOT,
    REQUIRED_CONFIG_FILES,
    ConfigError,
    Settings,
    check_config_dir,
    duplicate_top_level_keys,
    format_yaml_error,
    load_env,
    load_yaml_checked,
    parse_env_line,
    resolve_config_dir,
)
from litradar.incremental import key_presence

ENV_EXAMPLE = REPO_ROOT / ".env.example"
DOCUMENTED_KEYS = {
    "LITRADAR_CONTACT_EMAIL", "OPENALEX_API_KEY", "NCBI_API_KEY", "OPENFDA_API_KEY", "SEMANTIC_SCHOLAR_API_KEY",
    "ALTMETRIC_API_KEY", "ANTHROPIC_API_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "DIGEST_FROM",
    "DIGEST_TO", "SLACK_WEBHOOK_URL", "LITRADAR_READ_CAP", "LITRADAR_READ_INTERVAL",
}


def test_parse_env_line_quotes_comments_and_export():
    assert parse_env_line('SMTP_PASSWORD="p4ss #word"') == ("SMTP_PASSWORD", "p4ss #word")
    assert parse_env_line("SINGLE='p4ss #word'") == ("SINGLE", "p4ss #word")
    assert parse_env_line("URL=https://hooks.example/services/T0/B0/abc #prod") == ("URL", "https://hooks.example/services/T0/B0/abc")
    assert parse_env_line("HASH=p4ss#word") == ("HASH", "p4ss#word")  # no space before '#': data
    assert parse_env_line("export EXPORTED=hello") == ("EXPORTED", "hello")
    assert parse_env_line('export QUOTED="a # b" # trailing') == ("QUOTED", "a # b")
    assert parse_env_line("EMPTY=") == ("EMPTY", "")
    assert parse_env_line("UNTERMINATED=\"abc") == ("UNTERMINATED", "abc")
    assert parse_env_line("# comment") is None and parse_env_line("") is None and parse_env_line("novalue") is None
    assert parse_env_line("=x") is None and parse_env_line("export") is None


def test_blank_value_followed_by_a_comment_is_an_empty_value():
    # the shakedown blocker: the comment text used to become the key's value and was sent to NCBI as api_key
    line = "NCBI_API_KEY=                 # https://account.example.org/settings/   E-utilities 3 -> 10 requests/s (SRA and GEO census)"
    assert parse_env_line(line) == ("NCBI_API_KEY", "")
    assert parse_env_line("KEY=value # note") == ("KEY", "value")
    assert parse_env_line("KEY=\t# tab before the comment") == ("KEY", "")
    assert parse_env_line("KEY=#looks-like-a-comment") == ("KEY", "")
    assert parse_env_line('KEY="#literal"') == ("KEY", "#literal")  # quoted: a '#' is data
    assert parse_env_line("KEY='a # b' # c") == ("KEY", "a # b")
    assert parse_env_line("DIGEST_TO=                    # comma-separated") == ("DIGEST_TO", "")


def test_env_example_parses_with_every_key_present_and_blank(tmp_path, monkeypatch):
    loaded = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(raw)
        if parsed:
            assert parsed[0] not in loaded, f"{parsed[0]} appears twice in .env.example"
            loaded[parsed[0]] = parsed[1]
    assert DOCUMENTED_KEYS <= set(loaded), DOCUMENTED_KEYS - set(loaded)
    for key, value in loaded.items():
        assert "#" not in value and "http" not in value and "@" not in value, (key, value)  # never a comment, never a placeholder address
    assert {k: v for k, v in loaded.items() if v} == {"SMTP_PORT": "587", "LITRADAR_READ_CAP": "20", "LITRADAR_READ_INTERVAL": "20"}
    # loaded for real into a clean environment: nothing counts as configured
    for key in loaded:
        monkeypatch.delenv(key, raising=False)
    assert load_env(ENV_EXAMPLE) == loaded
    s = Settings.from_env(config_dir=tmp_path, env_file=ENV_EXAMPLE)
    assert s.contact_email == "" and s.ncbi_api_key == "" and s.semantic_scholar_api_key == "" and s.digest_to == "" and s.smtp_port == 587
    presence = key_presence(s)
    assert presence and not any(presence.values()), presence
    assert "OPENFDA_API_KEY" in presence and "ALTMETRIC_API_KEY" not in presence


def test_load_env_does_not_override_and_handles_all_forms(tmp_path, monkeypatch):
    for k in ("T_PASS", "T_URL", "T_EXPORTED", "T_PRESET", "T_EMPTY", "T_BLANK_COMMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("T_PRESET", "keep-me")
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "T_PASS=\"p4ss #word\"\n"
        "T_URL=https://hooks.example/x #prod\n"
        "export T_EXPORTED=hello\n"
        "T_PRESET=override\n"
        "T_EMPTY=\n"
        "T_BLANK_COMMENT=      # see the docs\n",
        encoding="utf-8",
    )
    loaded = load_env(env)
    assert loaded == {"T_PASS": "p4ss #word", "T_URL": "https://hooks.example/x", "T_EXPORTED": "hello", "T_EMPTY": "", "T_BLANK_COMMENT": ""}
    assert os.environ["T_PASS"] == "p4ss #word" and os.environ["T_EXPORTED"] == "hello"
    assert os.environ["T_PRESET"] == "keep-me" and "export T_EXPORTED" not in os.environ
    assert load_env(tmp_path / "missing.env") == {}


def test_empty_llm_model_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("LITRADAR_LLM_MODEL", "")
    monkeypatch.setenv("SMTP_PORT", "")
    s = Settings.from_env(config_dir=tmp_path, env_file=tmp_path / "none.env")
    assert s.llm_model == DEFAULT_LLM_MODEL and s.smtp_port == 587
    monkeypatch.setenv("LITRADAR_LLM_MODEL", "claude-test-model")
    assert Settings.from_env(config_dir=tmp_path, env_file=tmp_path / "none.env").llm_model == "claude-test-model"


def test_whitespace_only_values_are_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "   ")
    s = Settings.from_env(config_dir=tmp_path, env_file=tmp_path / "none.env")
    assert s.ncbi_api_key == "" and key_presence(s)["NCBI_API_KEY"] is False


# ---- config directory checks (check-config) -------------------------------------------------------
def test_check_config_dir_reports_missing_directory_and_files(tmp_path, monkeypatch):
    assert check_config_dir(tmp_path / "nope") == [f"config directory not found: {tmp_path / 'nope'}"]
    (tmp_path / "radar.yaml").write_text("sources: {}\n", encoding="utf-8")
    problems = check_config_dir(tmp_path)
    assert all(p.startswith("missing ") for p in problems) and len(problems) == len(REQUIRED_CONFIG_FILES) - 1
    assert check_config_dir(REPO_ROOT / "config") == []
    monkeypatch.setenv("LITRADAR_CONFIG_DIR", str(tmp_path))
    assert resolve_config_dir(None) == tmp_path and resolve_config_dir(tmp_path / "x") == tmp_path / "x"


def test_yaml_errors_are_one_line(tmp_path):
    bad = tmp_path / "companies.yaml"
    bad.write_text("entries:\n  - {id: a, name: A}\nthis: [is: broken\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_yaml_checked(bad)
    msg = str(exc.value)
    assert msg.startswith("companies.yaml: YAML parse error at line ") and "column" in msg and "\n" not in msg
    try:
        yaml.safe_load(bad.read_text(encoding="utf-8"))
    except yaml.YAMLError as raw:
        assert format_yaml_error(bad, raw) == msg
    with pytest.raises(ConfigError, match="not found"):
        load_yaml_checked(tmp_path / "absent.yaml")
    assert load_yaml_checked(REPO_ROOT / "config" / "radar.yaml")["sources"]


def test_duplicate_top_level_keys_are_detected(tmp_path):
    f = tmp_path / "radar.yaml"
    f.write_text("sources:\n  biorxiv: {}\npriorities: {}\n# comment\nwatchlist_terms: [a]\npriorities:\n  company:\n    illumina: 2\n", encoding="utf-8")
    assert duplicate_top_level_keys(f) == {"priorities": [3, 6]}
    assert duplicate_top_level_keys(REPO_ROOT / "config" / "radar.yaml") == {}
