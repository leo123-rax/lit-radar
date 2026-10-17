"""The hand-rolled .env parser and Settings defaults."""
import os

from litradar.config import DEFAULT_LLM_MODEL, Settings, load_env, parse_env_line


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


def test_load_env_does_not_override_and_handles_all_forms(tmp_path, monkeypatch):
    for k in ("T_PASS", "T_URL", "T_EXPORTED", "T_PRESET", "T_EMPTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("T_PRESET", "keep-me")
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "T_PASS=\"p4ss #word\"\n"
        "T_URL=https://hooks.example/x #prod\n"
        "export T_EXPORTED=hello\n"
        "T_PRESET=override\n"
        "T_EMPTY=\n",
        encoding="utf-8",
    )
    loaded = load_env(env)
    assert loaded == {"T_PASS": "p4ss #word", "T_URL": "https://hooks.example/x", "T_EXPORTED": "hello", "T_EMPTY": ""}
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
