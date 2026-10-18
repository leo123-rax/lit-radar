"""The weekly and daily workflows never publish a database that fails `PRAGMA integrity_check`.

The Python heredoc that optimizes, checkpoints and checks the database before it is compressed is extracted from
the workflow files and executed against a healthy and a damaged store, so the guard is tested as it will run on
the runner: a result other than `ok` (or a file SQLite cannot read) must end the step with a `::error::` annotation
and a non-zero exit before `zstd` and `gh release upload` are reached. The publish job's re-check of the incoming
file is asserted structurally.
"""
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
HEREDOC = re.compile(r"python3? - <<'EOF'\n(.*?)\nEOF\n", re.S)


def _workflow(name: str):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _compress_step(name: str):
    """The step that optimizes the database and compresses it: weekly radar job / daily update job."""
    job = "radar" if name == "weekly.yml" else "update"
    steps = _workflow(name)["jobs"][job]["steps"]
    return next(s for s in steps if "PRAGMA integrity_check" in s.get("run", "") and "zstd -T0" in s.get("run", ""))


def _heredoc(run: str) -> str:
    m = HEREDOC.search(run)
    assert m, run
    return m.group(1)


def _store(path: Path, rows: int = 50, fts: bool = True) -> None:
    """A small store shaped like the radar's (a papers table and the papers_fts FTS5 table)."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE papers(id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
    c.executemany("INSERT INTO papers(title, body) VALUES (?, ?)",
                  [(f"title {i}", f"body text number {i} with words") for i in range(rows)])
    if fts:
        c.execute("CREATE VIRTUAL TABLE papers_fts USING fts5(title, body)")
        c.execute("INSERT INTO papers_fts(rowid, title, body) SELECT id, title, body FROM papers")
    c.commit()
    c.close()


def _integrity(path: Path) -> str:
    try:
        c = sqlite3.connect(path)
        status = c.execute("PRAGMA integrity_check").fetchone()[0]
        c.close()
        return status
    except sqlite3.DatabaseError as exc:
        return f"unreadable: {exc}"


def _damage_fts_index(path: Path) -> bool:
    """Zero 16 bytes inside the FTS5 index page so VACUUM still succeeds (the segment blob is copied as data) but
    integrity_check does not return ok. Returns False when this SQLite build does not flag the damage."""
    c = sqlite3.connect(path)
    root = c.execute("SELECT rootpage FROM sqlite_master WHERE name = 'papers_fts_data'").fetchone()[0]
    page_size = c.execute("PRAGMA page_size").fetchone()[0]
    c.close()
    pristine = path.read_bytes()
    for back in (120, 300, 40, 600):
        data = bytearray(pristine)
        start = (root - 1) * page_size + page_size - back
        data[start:start + 16] = b"\x00" * 16
        path.write_bytes(bytes(data))
        if _integrity(path) != "ok":
            return True
    path.write_bytes(pristine)
    return False


def _run(script: str, workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-"], input=script, text=True, capture_output=True, cwd=str(workdir))


@pytest.fixture(params=["weekly.yml", "daily.yml"])
def guard(request, tmp_path):
    """(heredoc script, working directory with data/) for one workflow."""
    (tmp_path / "data").mkdir()
    return _heredoc(_compress_step(request.param)["run"]), tmp_path


def test_healthy_database_passes_and_prints_the_status(guard):
    script, workdir = guard
    _store(workdir / "data" / "radar.sqlite")
    res = _run(script, workdir)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "integrity_check: ok" in res.stdout and "::error::" not in res.stdout


def test_first_run_without_the_fts_table_still_passes(guard):
    script, workdir = guard
    _store(workdir / "data" / "radar.sqlite", fts=False)
    res = _run(script, workdir)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "FTS optimize skipped" in res.stdout and "integrity_check: ok" in res.stdout


def test_unreadable_database_fails_the_step_with_an_error_annotation(guard):
    """A truncated file: VACUUM raises, the script must turn that into ::error:: + exit 1, not a traceback."""
    script, workdir = guard
    db = workdir / "data" / "radar.sqlite"
    _store(db, rows=500)
    size = db.stat().st_size
    with open(db, "r+b") as fh:
        fh.truncate(int(size * 0.6))
    res = _run(script, workdir)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "::error::database failed integrity_check" in res.stdout and "unreadable:" in res.stdout
    assert "Traceback" not in res.stderr


def test_damaged_index_that_vacuum_survives_fails_the_step(guard):
    """The defect: a corrupt FTS5 segment is copied verbatim by VACUUM, so the old heredoc printed the non-ok status
    and exited 0, and the file went on to zstd and the release. Now the status is enforced."""
    script, workdir = guard
    db = workdir / "data" / "radar.sqlite"
    _store(db)
    if not _damage_fts_index(db):
        pytest.skip("this SQLite build does not flag the crafted FTS5 damage")
    res = _run(script, workdir)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "::error::database failed integrity_check" in res.stdout
    assert "Traceback" not in res.stderr


@pytest.mark.parametrize("name", ["weekly.yml", "daily.yml"])
def test_guard_runs_before_compression_and_upload(name):
    step = _compress_step(name)
    run = step["run"]
    assert "set -euo pipefail" in run and "${{" not in run
    assert "sys.exit(1)" in _heredoc(run) and '::error::database failed integrity_check' in _heredoc(run)
    heredoc_end = run.index("\nEOF\n")
    assert heredoc_end < run.index("zstd -T0")
    if name == "daily.yml":
        assert run.index("zstd -T0") < run.index("gh release upload")
        assert step["if"] == "always() && steps.restore.outcome == 'success'"


def test_publish_job_rechecks_the_incoming_snapshot_before_overwriting_the_releases():
    publish = _workflow("weekly.yml")["jobs"]["publish"]
    step = next(s for s in publish["steps"] if "gh release upload" in s.get("run", ""))
    run = step["run"]
    assert 'zstd -d -q -f "incoming/$STATE_ASSET" -o incoming/radar.sqlite' in run
    assert "PRAGMA integrity_check" in run and "::error::incoming snapshot failed integrity_check" in run
    check = run.index("::error::incoming snapshot failed integrity_check")
    assert run.index("exit 1", check) < run.index("gh release upload")
    assert run.index("new snapshot is $new_size bytes") < check < run.index("gh release upload")  # size guard, then integrity, then upload
    assert "${{" not in run
