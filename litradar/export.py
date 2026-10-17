"""Export the SQLite store to Parquet or CSV files, or to a DuckDB database.

    litradar export --parquet DIR            one <table>.parquet per table (pyarrow); CSV when pyarrow is missing
    litradar export --csv DIR [--gzip]       one <table>.csv per table, stdlib only
    litradar export --duckdb FILE            a DuckDB database with the same tables (duckdb package)

The SQLite file stays the system of record; this module is the bridge to the analytics layer
described in docs/SCALE.md (DuckDB / Parquet for trends over time, Postgres for many users).

Dependencies: nothing beyond the standard library for CSV. `pyarrow` and `duckdb` are optional and
are imported lazily; when one is missing the caller gets a clear message, never an ImportError at
import time.

Reference behaviour checked against the official documentation:

* pyarrow Parquet writer: `pyarrow.parquet.ParquetWriter(where, schema, compression='snappy', ...)`,
  `write_table(table)`, `close()`; tables are built per chunk with `pyarrow.Table.from_arrays`.
  https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetWriter.html
* DuckDB SQLite extension: `ATTACH 'file.sqlite' AS src (TYPE sqlite, READ_ONLY)`; the extension
  autoloads on first use. Declared SQLite types map INT->BIGINT, TEXT->VARCHAR, REAL->DOUBLE; a value
  that does not match its declared type raises a "Mismatch Type Error" (we fall back to a row copy).
  https://duckdb.org/docs/current/core_extensions/sqlite.html
* SQLite read-only URI: `file:path?mode=ro` (https://www.sqlite.org/uri.html). A `BEGIN` before the
  first SELECT pins one WAL snapshot for every table, so the export is consistent even while the
  weekly job is writing. A bare copy of a WAL database (no -wal/-shm files) cannot be opened with
  mode=ro; it is opened with `immutable=1` instead, which is correct because no process has it open.

Type handling: SQLite is dynamically typed. Columns declared INTEGER/REAL are exported as
int64/double only when every stored value really has that storage class (checked with
`typeof()` first); otherwise the column is written as text so nothing is silently dropped.
CSV writes NULL as an empty field and BLOBs as hex.
"""
from __future__ import annotations

import csv
import gzip
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

log = logging.getLogger(__name__)

DEFAULT_TABLES: Tuple[str, ...] = ("papers", "mentions", "scores", "archive_records", "news_items")
OPTIONAL_TABLES: Tuple[str, ...] = ("tracked_author_hits", "runs", "papers_fts")
KNOWN_TABLES: Tuple[str, ...] = DEFAULT_TABLES + OPTIONAL_TABLES
FORMATS = ("auto", "parquet", "csv")


class OptionalDependencyMissing(RuntimeError):
    """An optional package (pyarrow, duckdb) is needed for the requested output and is not importable."""


@dataclass
class TableResult:
    table: str
    rows: int
    path: str
    format: str
    bytes: int
    seconds: float
    note: str = ""


@dataclass
class ExportReport:
    db: str
    target: str
    format_requested: str
    format_used: str
    tables: List[TableResult] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    message: str = ""
    seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["total_rows"] = sum(t.rows for t in self.tables)
        d["total_bytes"] = sum(t.bytes for t in self.tables)
        return d


# ---------------------------------------------------------------------------------- optional imports
def _import_pyarrow():
    """Return (pyarrow, pyarrow.parquet) or None. Separate function so tests can monkeypatch it."""
    try:
        import pyarrow  # type: ignore
        import pyarrow.parquet  # type: ignore
    except ImportError:
        return None
    return pyarrow, pyarrow.parquet


def _import_duckdb():
    try:
        import duckdb  # type: ignore
    except ImportError:
        return None
    return duckdb


# ---------------------------------------------------------------------------------- sqlite helpers
def open_readonly(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    uri = "file:" + quote(db_path.resolve().as_posix(), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")  # the open is lazy; force it
        return conn
    except sqlite3.OperationalError:
        conn.close()
    # A WAL-mode database whose -wal/-shm side files do not exist (a bare copy, a restored snapshot)
    # cannot be opened with mode=ro, which refuses to create them. Nobody has such a file open, so it
    # is safe to treat it as immutable (no locking, no shared memory); see https://www.sqlite.org/uri.html
    if Path(str(db_path) + "-wal").exists():
        raise
    log.info("opening %s as immutable (WAL database without side files)", db_path)
    return sqlite3.connect(uri + "&immutable=1", uri=True)


def existing_tables(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def table_columns(conn: sqlite3.Connection, table: str) -> List[Tuple[str, str]]:
    """[(name, declared_type_upper)] — virtual (FTS) tables report an empty type."""
    return [(r[1], (r[2] or "").upper()) for r in conn.execute(f'PRAGMA table_info("{table}")')]


def resolve_tables(requested: Optional[Sequence[str]]) -> List[str]:
    tables = list(requested) if requested else list(DEFAULT_TABLES)
    unknown = [t for t in tables if t not in KNOWN_TABLES]
    if unknown:
        raise ValueError(f"unknown table(s): {', '.join(unknown)}; known: {', '.join(KNOWN_TABLES)}")
    return tables


def _storage_kind(conn: sqlite3.Connection, table: str, name: str, decl: str) -> str:
    """'int', 'float' or 'text' — numeric only when the declared type AND the stored values agree."""
    if "INT" in decl:
        want = "int"
        bad = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE typeof("{name}") NOT IN (\'integer\',\'null\')').fetchone()[0]
    elif any(k in decl for k in ("REAL", "FLOA", "DOUB")):
        want = "float"
        bad = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE typeof("{name}") NOT IN (\'real\',\'integer\',\'null\')').fetchone()[0]
    else:
        return "text"
    return want if bad == 0 else "text"


def iter_chunks(conn: sqlite3.Connection, table: str, columns: Sequence[str], chunk_size: int) -> Iterator[List[tuple]]:
    cols = ", ".join(f'"{c}"' for c in columns)
    cur = conn.execute(f'SELECT {cols} FROM "{table}"')
    while True:
        rows = cur.fetchmany(chunk_size)
        if not rows:
            break
        yield rows


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


# ---------------------------------------------------------------------------------- writers
def write_csv(conn: sqlite3.Connection, table: str, path: Path, chunk_size: int = 20000, gzip_output: bool = False) -> TableResult:
    t0 = time.perf_counter()
    columns = [c for c, _ in table_columns(conn, table)]
    path = Path(path)
    opener = (lambda: gzip.open(path, "wt", encoding="utf-8", newline="")) if gzip_output else (lambda: open(path, "w", encoding="utf-8", newline=""))
    n = 0
    with opener() as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for rows in iter_chunks(conn, table, columns, chunk_size):
            w.writerows([[_text(v) for v in r] for r in rows])
            n += len(rows)
    return TableResult(table, n, str(path), "csv.gz" if gzip_output else "csv", path.stat().st_size, round(time.perf_counter() - t0, 3))


def write_parquet(conn: sqlite3.Connection, table: str, path: Path, pa, pq, chunk_size: int = 20000) -> TableResult:
    t0 = time.perf_counter()
    cols = table_columns(conn, table)
    kinds = [_storage_kind(conn, table, name, decl) for name, decl in cols]
    types = {"int": pa.int64(), "float": pa.float64(), "text": pa.string()}
    schema = pa.schema([pa.field(name, types[k], nullable=True) for (name, _), k in zip(cols, kinds)])
    names = [c for c, _ in cols]
    n = 0
    path = Path(path)
    writer = pq.ParquetWriter(str(path), schema, compression="zstd")
    try:
        for rows in iter_chunks(conn, table, names, chunk_size):
            arrays = []
            for i, k in enumerate(kinds):
                col = [r[i] for r in rows]
                if k == "text":
                    col = [None if v is None else (v.hex() if isinstance(v, bytes) else str(v)) for v in col]
                elif k == "float":
                    col = [None if v is None else float(v) for v in col]
                arrays.append(pa.array(col, type=schema.field(i).type))
            writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
            n += len(rows)
    finally:
        writer.close()
    note = ", ".join(f"{name} stored as text (mixed storage classes)" for (name, decl), k in zip(cols, kinds) if k == "text" and ("INT" in decl or "REAL" in decl))
    return TableResult(table, n, str(path), "parquet", path.stat().st_size, round(time.perf_counter() - t0, 3), note)


# ---------------------------------------------------------------------------------- public API
def export_tables(
    db_path: Path,
    out_dir: Path,
    fmt: str = "auto",
    tables: Optional[Sequence[str]] = None,
    chunk_size: int = 20000,
    gzip_csv: bool = False,
) -> ExportReport:
    """Write one file per table into out_dir. fmt: 'parquet' (needs pyarrow), 'csv', or 'auto'
    (Parquet when pyarrow is importable, else CSV). Never raises for a missing optional package."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    t0 = time.perf_counter()
    wanted = resolve_tables(tables)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    arrow = _import_pyarrow() if fmt in ("auto", "parquet") else None
    used = "parquet" if arrow else "csv"
    report = ExportReport(db=str(db_path), target=str(out_dir), format_requested=fmt, format_used=used)
    if fmt == "parquet" and not arrow:
        report.message = "pyarrow is not importable: wrote CSV instead (pip install pyarrow for Parquet)"
        log.warning(report.message)
    elif fmt == "auto" and not arrow:
        report.message = "pyarrow is not importable: writing CSV (pip install pyarrow for Parquet)"
    conn = open_readonly(Path(db_path))
    try:
        conn.execute("BEGIN")  # one read snapshot for every table
        present = set(existing_tables(conn))
        for table in wanted:
            if table not in present:
                report.skipped[table] = "table not present in this database"
                continue
            if arrow:
                res = write_parquet(conn, table, out_dir / f"{table}.parquet", arrow[0], arrow[1], chunk_size=chunk_size)
            else:
                res = write_csv(conn, table, out_dir / (f"{table}.csv.gz" if gzip_csv else f"{table}.csv"), chunk_size=chunk_size, gzip_output=gzip_csv)
            report.tables.append(res)
            log.info("exported %s: %d rows, %d bytes (%s)", table, res.rows, res.bytes, res.format)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
    report.seconds = round(time.perf_counter() - t0, 3)
    return report


def _duckdb_type(decl: str) -> str:
    if "INT" in decl:
        return "BIGINT"
    if any(k in decl for k in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE"
    return "VARCHAR"


def _copy_rows_into_duckdb(con, conn: sqlite3.Connection, table: str, chunk_size: int) -> int:
    """Fallback path: stream rows through Python. Used for FTS tables and when the SQLite scanner
    rejects a column whose stored values do not match the declared type."""
    cols = table_columns(conn, table)
    kinds = [_storage_kind(conn, table, name, decl) for name, decl in cols]
    types = {"int": "BIGINT", "float": "DOUBLE", "text": "VARCHAR"}
    defs = ", ".join(f'"{name}" {types[k]}' for (name, _), k in zip(cols, kinds))
    con.execute(f'CREATE OR REPLACE TABLE "{table}" ({defs})')
    placeholders = ", ".join("?" for _ in cols)
    n = 0
    for rows in iter_chunks(conn, table, [c for c, _ in cols], chunk_size):
        clean = []
        for r in rows:
            clean.append(tuple(
                (None if v is None else (v.hex() if isinstance(v, bytes) else str(v))) if k == "text" else v
                for v, k in zip(r, kinds)
            ))
        con.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', clean)
        n += len(rows)
    return n


def export_duckdb(db_path: Path, duckdb_path: Path, tables: Optional[Sequence[str]] = None, chunk_size: int = 20000) -> ExportReport:
    """Create/replace the tables in a DuckDB file. Attaches the SQLite file through DuckDB's sqlite
    extension (CREATE TABLE AS SELECT); falls back to a Python row copy per table when the
    extension is unavailable (offline first use) or rejects a table."""
    duckdb = _import_duckdb()
    if duckdb is None:
        raise OptionalDependencyMissing("duckdb is not importable: pip install duckdb to write a DuckDB file")
    t0 = time.perf_counter()
    wanted = resolve_tables(tables)
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    duckdb_path = Path(duckdb_path)
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    report = ExportReport(db=str(db_path), target=str(duckdb_path), format_requested="duckdb", format_used="duckdb")
    conn = open_readonly(db_path)
    con = duckdb.connect(str(duckdb_path))
    attached = False
    try:
        present = set(existing_tables(conn))
        try:
            lit = db_path.resolve().as_posix().replace("'", "''")
            con.execute(f"ATTACH '{lit}' AS src (TYPE sqlite, READ_ONLY)")
            attached = True
        except Exception as exc:  # extension not installed and offline, or incompatible build
            report.message = f"sqlite extension unavailable ({type(exc).__name__}); copied rows through Python"
            log.warning(report.message)
        for table in wanted:
            if table not in present:
                report.skipped[table] = "table not present in this database"
                continue
            ts = time.perf_counter()
            note = ""
            n = -1
            if attached and table != "papers_fts":
                try:
                    con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM src."{table}"')
                    n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                except Exception as exc:
                    note = f"scanner failed ({type(exc).__name__}); copied rows through Python"
            if n < 0:
                n = _copy_rows_into_duckdb(con, conn, table, chunk_size)
                note = note or ("copied rows through Python" if table != "papers_fts" else "virtual table copied through Python")
            report.tables.append(TableResult(table, n, f"{duckdb_path}::{table}", "duckdb", 0, round(time.perf_counter() - ts, 3), note))
        if attached:
            con.execute("DETACH src")
    finally:
        con.close()
        conn.close()
    size = duckdb_path.stat().st_size if duckdb_path.exists() else 0
    if report.tables:
        report.tables[0].bytes = size  # file size is reported once, on the first table
    report.seconds = round(time.perf_counter() - t0, 3)
    return report
