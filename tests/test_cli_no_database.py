"""Commands that only READ the store never create one. Store.__init__ makes the directory, the file, the WAL and the
schema, so before this guard a mistyped LITRADAR_DB gave `stats` all-zero counts with exit 0 (and a fresh empty
database on disk), `search` printed 'no matches', and an unwritable path ended in an OSError traceback. Now every
reader exits 1 with 'no database at <path>' and leaves nothing behind, as `digest`, `status` and `serve` already did."""
import os

import pytest

from litradar import cli

READERS = [
    ["stats"],
    ["stats", "--json"],
    ["search", "xenium"],
    ["search", "xenium", "--json"],
    ["entity", "txg-xenium"],
    ["show", "illumina"],
    ["trends", "--print"],
    ["tag", "--check"],
    ["tag"],
    ["digest"],  # the pre-existing contract, kept as the reference
]


@pytest.fixture
def missing(tmp_path, monkeypatch):
    db = tmp_path / "nope1" / "x.sqlite"
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)
    return db


@pytest.mark.parametrize("argv", READERS, ids=[" ".join(a) for a in READERS])
def test_readers_report_a_missing_database_and_leave_nothing_behind(missing, capsys, argv):
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert f"no database at {missing}" in captured.err and "Traceback" not in captured.err
    assert captured.out == ""
    assert not missing.exists() and not missing.parent.exists()
    assert not missing.with_name(missing.name + "-wal").exists() and not missing.with_name(missing.name + "-shm").exists()


def test_a_zero_byte_file_is_no_database_and_stays_empty(missing, capsys):
    missing.parent.mkdir()
    missing.touch()
    assert cli.main(["stats"]) == 1
    assert f"no database at {missing}" in capsys.readouterr().err
    assert missing.stat().st_size == 0 and not missing.with_name(missing.name + "-wal").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere, so the directory cannot be made unwritable")
def test_an_unwritable_parent_is_a_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    db = locked / "sub" / "x.sqlite"
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)
    try:
        assert cli.main(["stats"]) == 1
        err = capsys.readouterr().err
        assert f"no database at {db}" in err and "Traceback" not in err and "OSError" not in err
        assert not (locked / "sub").exists()
    finally:
        locked.chmod(0o700)


def test_readers_still_work_on_a_real_database(tmp_path, monkeypatch, capsys):
    from litradar.store import Store

    db = tmp_path / "real.sqlite"
    Store(db).close()
    monkeypatch.setenv("LITRADAR_DB", str(db))
    monkeypatch.setenv("LITRADAR_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)
    assert cli.main(["stats"]) == 0
    assert "recent runs: none recorded" in capsys.readouterr().out
    assert cli.main(["search", "xenium"]) == 0
    assert capsys.readouterr().out.strip() == "no matches"
