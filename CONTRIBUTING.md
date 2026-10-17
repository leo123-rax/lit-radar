# Contributing

Thanks for helping build the radar. The project is deliberately small and dependency-light so that
anyone can fork it, add their own API keys, and run it on a laptop or on GitHub Actions.

## Dev setup

```bash
git clone <your fork>
cd lit-radar
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm]"
cp .env.example .env            # fill in what you have; everything is optional
python -m litradar check-config  # validates the YAML gazetteers
pytest -q                        # offline tests, no network needed
```

## Where things live

| Path | What |
|---|---|
| `config/instruments.yaml` | the instrument / platform / kit gazetteer (the heart of the project) |
| `config/companies.yaml` | company gazetteer: tickers, parents, aliases, e-mail domains |
| `config/subgroups.yaml` | research subgroups, keywords, tracked authors |
| `config/journals.yaml`, `config/feeds.yaml` | journals by ISSN/RSS; news and press feeds |
| `litradar/sources/` | one adapter per data source; they only normalise records |
| `litradar/extract/` | gazetteer matching and JATS section parsing |
| `litradar/rank/` | heuristic scoring and the optional Claude scorer |
| `litradar/digest/` | Markdown rendering and delivery |
| `litradar/store.py` | SQLite + FTS5 schema and queries |
| `docs/` | design notes, source catalog, legal boundaries |

## The most useful contributions

1. **Gazetteer entries.** New instruments, new aliases as they appear in Methods sections, fixed
   launch years, ownership changes after acquisitions. Run `python -m litradar check-config` to catch
   duplicate ids and alias collisions, then `pytest`.
2. **Context terms for ambiguous names.** If a product name is also a common word, set
   `context_required: true` and list `context_terms`. The tests in `tests/test_gazetteer.py` show the
   pattern.
3. **New sources.** Add a module under `litradar/sources/` that yields `Paper`, `ArchiveRecord` or
   `NewsItem` objects, wire it into `litradar/pipeline.py`, and add an offline test with a canned payload.
4. **OpenAlex topic ids and author ids** in `config/subgroups.yaml`; these make researcher tracking exact.

## Ground rules

* Never commit `.env`, database files, or anything from behind a paywall. The pipeline stores metadata,
  abstracts and open-licensed full text only (see `docs/LEGAL.md`).
* Keep adapters polite: use the shared `Http` client so rate limits and User-Agent rules are honoured.
* Keep Python 3.9 compatibility (no `match`, no `X | Y` unions at runtime).
* Run `ruff check .` and `pytest -q` before opening a pull request.
