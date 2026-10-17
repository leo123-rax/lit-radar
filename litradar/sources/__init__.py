"""Source adapters. Each exposes functions that yield Paper / ArchiveRecord / NewsItem objects.

Adding a source: write a module here with a `fetch(...)` generator and register it in
litradar/pipeline.py. Keep adapters dumb: no matching, no scoring, just normalised records.
"""
