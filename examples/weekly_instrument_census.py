#!/usr/bin/env python3
"""Weekly census of the sequencers behind newly public data, from NCBI SRA and ENA, with no pipeline and no database.

    python examples/weekly_instrument_census.py                       # last 7 days, a default list of models
    python examples/weekly_instrument_census.py --days 30 --models "Revio,PromethION,Element AVITI"
    python examples/weekly_instrument_census.py --ena-sweep           # also list every model ENA saw (one request)

Two signals, both free:

* SRA's Properties index gives an instant count per instrument model ("instrument revio"[Properties])
  without downloading records. Model strings are SRA's own `instrument_model` vocabulary
  (config/archive_models.yaml lists them per gazetteer entity).
* ENA's portal returns every ERX/DRX experiment made public in the window in one request, with its
  `instrument_model`; a Counter over that is the ENA-side histogram.

Set LITRADAR_CONTACT_EMAIL (polite User-Agent) and NCBI_API_KEY (3 -> 10 requests/s) in the environment
if you have them; neither is required.
"""
from __future__ import annotations

import argparse
import os
from collections import Counter
from datetime import date, timedelta

try:
    from litradar.http import Http
    from litradar.sources import ena, sra
except ImportError:  # pragma: no cover - the examples only need the package importable
    raise SystemExit("lit-radar is not installed in this interpreter: run `pip install -e .` from the repository root (or `pip install git+<repo-url>`)") from None

DEFAULT_MODELS = [
    "Illumina NovaSeq X Plus",
    "Illumina NovaSeq X",
    "Illumina NovaSeq 6000",
    "NextSeq 2000",
    "Illumina MiSeq",
    "Revio",
    "Sequel IIe",
    "PromethION",
    "GridION",
    "MinION",
    "DNBSEQ-T7",
    "DNBSEQ-G400",
    "Element AVITI",
    "UG 100",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--until", help="window end YYYY-MM-DD (default today)")
    ap.add_argument("--models", help="comma-separated SRA instrument_model strings (default: a cross-vendor list)")
    ap.add_argument("--ena-sweep", action="store_true", help="also pull every ENA experiment in the window and histogram instrument_model")
    args = ap.parse_args()

    until = date.fromisoformat(args.until) if args.until else date.today()
    since = until - timedelta(days=args.days)
    s, u = since.isoformat(), until.isoformat()
    models = [m.strip() for m in (args.models or ",".join(DEFAULT_MODELS)).split(",") if m.strip()]

    http = Http(contact_email=os.environ.get("LITRADAR_CONTACT_EMAIL", ""), ncbi_api_key=os.environ.get("NCBI_API_KEY", ""))
    key = os.environ.get("NCBI_API_KEY", "")

    term = f"{s.replace('-', '/')}:{u.replace('-', '/')}[PDAT]"  # SRA's publication-date range syntax
    total = sra.count(http, term, key)
    counts = sra.counts_by_instrument(http, s, u, models, api_key=key)

    print(f"NCBI SRA experiments made public {s}..{u}: {total:,}")
    print(f"{'instrument_model':28} {'experiments':>12} {'share':>7}")
    for model, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        share = (100.0 * n / total) if total else 0.0
        print(f"{model:28} {n:12,} {share:6.1f}%")
    listed = sum(counts.values())
    print(f"{'(listed models)':28} {listed:12,} {100.0 * listed / total if total else 0:6.1f}%")

    if args.ena_sweep:
        hist: Counter = Counter()
        n_rows = 0
        for rec in ena.fetch(http, s, u):
            n_rows += 1
            hist[rec.instrument_model or "(not declared)"] += 1
        print(f"\nENA experiments (ERX/DRX) made public {s}..{u}: {n_rows:,}")
        for model, n in hist.most_common(20):
            print(f"{model:28} {n:12,}")
    print(f"\nhttp: {http.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
