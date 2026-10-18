"""Date windows across `run`, `digest`, `coverage` and `update`: one resolver (toolkit.resolve_window), so --until
alone ends the --days window, since after until is a usage error (exit 2) everywhere, a --days below 1 is rejected,
and `update` never plans a silent 0-day window from an inverted override (while a recorded run that ends in the
future is still clamped)."""
from datetime import date, timedelta

import pytest

from litradar import cli, incremental, toolkit
from litradar import query as Q
from litradar.store import Store

TODAY = date(2026, 10, 17)


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "w.sqlite"
    Store(db).close()
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)
    return tmp_path


def test_resolve_window_handles_every_combination_and_rejects_the_impossible():
    today = Q.today_utc()
    assert toolkit.resolve_window(None, "2026-10-10", None) == ("2026-10-03", "2026-10-10")  # --until alone: the 7 days ending there
    assert toolkit.resolve_window(None, "2026-10-10", 3) == ("2026-10-07", "2026-10-10")
    assert toolkit.resolve_window("2026-10-01", "2026-10-10", None) == ("2026-10-01", "2026-10-10")
    assert toolkit.resolve_window("2026-10-01", None, None) == ("2026-10-01", today.isoformat())
    assert toolkit.resolve_window(None, None, 2) == ((today - timedelta(days=2)).isoformat(), today.isoformat())
    assert toolkit.resolve_window(None, None, None) == ((today - timedelta(days=7)).isoformat(), today.isoformat())
    with pytest.raises(toolkit.UsageError, match="--since 2026-10-10 is after --until 2026-10-01"):
        toolkit.resolve_window("2026-10-10", "2026-10-01", None)
    for days in (0, -3):
        with pytest.raises(toolkit.UsageError, match="--days must be at least 1"):
            toolkit.resolve_window(None, None, days)
    with pytest.raises(toolkit.UsageError, match="--until must be"):
        toolkit.resolve_window(None, "lastweek", None)


def test_digest_until_alone_renders_the_week_ending_there(env):
    out = env / "dg"
    assert cli.main(["digest", "--until", "2026-10-10", "--out", str(out), "--allow-empty"]) == 0
    assert sorted(p.name for p in out.glob("*.md")) == ["2026-10-10.md", "latest.md"]  # not <today>.md
    head = (out / "2026-10-10.md").read_text(encoding="utf-8").splitlines()[0]
    assert head.endswith("2026-10-03 to 2026-10-10")
    # --days sizes that window
    assert cli.main(["digest", "--until", "2026-10-10", "--days", "2", "--out", str(out), "--allow-empty"]) == 0
    assert (out / "2026-10-10.md").read_text(encoding="utf-8").splitlines()[0].endswith("2026-10-08 to 2026-10-10")


@pytest.mark.parametrize("argv", [
    ["digest", "--since", "2026-10-10", "--until", "2026-10-01", "--allow-empty"],
    ["run", "--since", "2026-10-10", "--until", "2026-10-01"],
    ["coverage", "--since", "2026-10-10", "--until", "2026-10-01"],
    ["update", "--dry-run", "--since", "2026-10-15", "--until", "2026-10-01"],
])
def test_inverted_windows_exit_2_everywhere(env, capsys, argv):
    out = env / "inverted"
    rc = cli.main(argv + (["--out", str(out)] if argv[0] == "digest" else []))
    captured = capsys.readouterr()
    assert rc == 2, captured.err
    assert f"litradar {argv[0]}: error: --since 2026-10-1" in captured.err and "is after --until 2026-10-01" in captured.err
    assert "Traceback" not in captured.err and captured.out == ""
    assert not out.exists()  # the digest wrote nothing


def test_coverage_rejects_a_non_positive_days_and_honours_until_alone(env, capsys):
    assert cli.main(["coverage", "--days", "-3"]) == 2
    assert "litradar coverage: error: --days must be at least 1, got -3" in capsys.readouterr().err
    assert cli.main(["coverage", "--days", "0"]) == 2
    capsys.readouterr()
    assert cli.main(["coverage", "--until", "2026-10-10", "--days", "3", "--json"]) == 0
    import json

    rep = json.loads(capsys.readouterr().out)
    assert (rep["since"], rep["until"]) == ("2026-10-07", "2026-10-10")


def test_update_override_rejects_since_after_until_but_a_future_last_run_is_still_clamped():
    with pytest.raises(ValueError, match="--since 2026-10-15 is after --until 2026-10-01"):
        incremental.compute_window("2026-10-16", today=TODAY, since="2026-10-15", until="2026-10-01")
    with pytest.raises(ValueError, match="--since 2026-10-20 is after --until 2026-10-17"):
        incremental.compute_window(None, today=TODAY, since="2026-10-20")  # until defaults to today
    # a recorded `until` in the future is clock skew, not user input: clamp as before
    w = incremental.compute_window("2026-10-30", today=TODAY, overlap_days=0)
    assert (w["since"], w["until"], w["basis"], w["days"]) == ("2026-10-17", "2026-10-17", "last_run", 0)
    # a valid override still works unchanged
    w = incremental.compute_window("2026-10-16", today=TODAY, since="2026-08-01", until="2026-10-10")
    assert (w["since"], w["until"], w["basis"]) == ("2026-08-01", "2026-10-10", "override")
