"""`litradar serve`: a read-only JSON API and search page over the SQLite store, or Datasette.

    litradar serve [--port 8787] [--host 127.0.0.1] [--db PATH] [--digests DIR] [--cors ORIGIN]
    litradar serve --datasette [--port 8001] [--db PATH] [--metadata datasette/metadata.yaml] [--no-immutable]

The default server is the dependency-free one in :mod:`litradar.api`. ``--datasette`` launches
Datasette with the bundled metadata (full-text search, facets, canned queries) when the
``datasette`` package is installed; otherwise it prints the pip command and exits 1.

Exit codes: 0 after a clean shutdown (Ctrl-C), 1 for a port already in use or a missing Datasette,
2 for a database that is missing, unreadable or not a SQLite file.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import List

from ..config import REPO_ROOT, Settings

DATASETTE_METADATA = REPO_ROOT / "datasette" / "metadata.yaml"
PIP_HINT = "pip install 'lit-radar[serve]'   # or: pip install 'datasette>=0.64'"


def datasette_command(db_path: Path, metadata: Path, host: str, port: int, immutable: bool = True) -> List[str]:
    """argv for Datasette: read-only, 5 s SQL time limit, immutable unless the file may change underneath.

    Datasette's ``-i`` is ``--immutable PATH``: the database is passed through it instead of positionally.
    """
    cmd = [sys.executable, "-m", "datasette"]
    cmd += ["-i", str(db_path)] if immutable else [str(db_path)]
    cmd += [
        "-m", str(metadata),
        "--setting", "sql_time_limit_ms", "5000",
        "--host", host, "--port", str(port),
    ]
    return cmd


def datasette_available() -> bool:
    """Is the Datasette *package* importable?

    ``find_spec("datasette")`` alone is fooled when the current directory is the repository root: the
    ``datasette/`` folder that holds ``metadata.yaml`` is importable as a namespace package (no loader,
    no origin). Only a spec with a real loader and an ``__init__.py`` that does not live in this repository's
    ``datasette/`` directory counts.
    """
    try:
        spec = importlib.util.find_spec("datasette")
    except (ImportError, ValueError):
        return False
    if spec is None or spec.loader is None or not spec.origin or spec.origin == "namespace":
        return False
    try:
        origin = Path(spec.origin).resolve()
        if origin.parent == (REPO_ROOT / "datasette").resolve():
            return False
    except OSError:
        return False
    return True


def cmd_serve(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    db_path = Path(args.db or settings.db_path)
    if not db_path.is_file():
        print(f"database not found: {db_path} (run `litradar run` first, or pass --db PATH)", file=sys.stderr)
        return 2
    if args.datasette:
        metadata = Path(args.metadata or DATASETTE_METADATA)
        if not metadata.is_file():
            print(f"Datasette metadata not found: {metadata}", file=sys.stderr)
            return 2
        if not datasette_available():
            print("Datasette is not installed. Install it with:\n  " + PIP_HINT, file=sys.stderr)
            return 1
        port = args.port if args.port is not None else 8001
        cmd = datasette_command(db_path, metadata, args.host, port, immutable=not args.no_immutable)
        print(" ".join(cmd), flush=True)
        return subprocess.call(cmd)

    from ..api import RadarAPI, serve

    port = args.port if args.port is not None else 8787
    try:
        app = RadarAPI(db_path, digest_dir=Path(args.digests or settings.out_dir), cors_origin=args.cors)
    except (sqlite3.DatabaseError, FileNotFoundError) as exc:
        # "file is not a database", a locked or corrupt file, a path that vanished: say so instead of serving 500s
        print(f"litradar serve: cannot open {db_path}: {exc}", file=sys.stderr)
        return 2
    return serve(app, host=args.host, port=port)


def register(sub) -> None:
    p = sub.add_parser("serve", help="read-only JSON API + search page over the database (or --datasette)")
    p.add_argument("--port", type=int, default=None, help="TCP port (default 8787; 8001 with --datasette)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; use 0.0.0.0 to expose)")
    p.add_argument("--db", help="path to radar.sqlite (default: LITRADAR_DB / radar.yaml db_path / data/radar.sqlite)")
    p.add_argument("--digests", help="digest directory for /digest/latest (default: the configured out_dir)")
    p.add_argument("--cors", metavar="ORIGIN", help="send Access-Control-Allow-Origin for this origin ('*' for any)")
    p.add_argument("--datasette", action="store_true", help="launch Datasette with datasette/metadata.yaml instead of the built-in API")
    p.add_argument("--metadata", help="Datasette metadata file (default: datasette/metadata.yaml)")
    p.add_argument("--no-immutable", action="store_true", help="Datasette: do not pass -i (use when the pipeline may write while serving)")
    p.set_defaults(func=cmd_serve)
