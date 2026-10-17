# bioRxiv / medRxiv — preprints via api.biorxiv.org

Adapter: `litradar/sources/biorxiv.py` → `Paper(source="biorxiv" | "medrxiv", kind="preprint")`, latest version per DOI.
Tests: `tests/test_sources_offline.py::test_biorxiv_paging_and_latest_version`.

## Endpoint

`GET https://api.biorxiv.org/details/{server}/{YYYY-MM-DD}/{YYYY-MM-DD}/{cursor}`

Observed live on 2026-10-17 for `biorxiv/2026-10-10/2026-10-17`:

| cursor | `messages[0]` | items |
|---|---|---|
| 0 | `{status: ok, cursor: 0, count: 30, count_new_papers: "1353", total: "1803"}` | 30 |
| 30 | `{..., cursor: "30", count: 30, ...}` | 30 |
| 100 | `{..., cursor: "100", count: 30, ...}` | 30 |

**The page size is 30 records**, not the 100 the API documentation suggests; `cursor` is a plain record
offset. A one-week bioRxiv window is therefore ~60 requests (1,803 / 30) at the 0.5 s host pacing, and
medRxiv adds its own sweep. Each record carries `doi`, `title`, `authors`, `author_corresponding_institution`,
`date`, `version`, `category`, `license` (`cc_by`, `cc_by_nc`, `cc_by_nc_nd`, `cc_by_nd`, `cc0`, `cc_no`),
`jatsxml` (full-text URL on www.biorxiv.org) and `published` (journal DOI once published, else `NA`).

## Full text is off by default

`config/radar.yaml` → `sources.biorxiv.fulltext: false`. The `jatsxml` URLs point at `www.biorxiv.org` /
`www.medrxiv.org`, which rate-limit scripted clients (HTTP 429 with a 30 s `Retry-After`, Cloudflare error
1015) after a handful of requests. In a live run with the flag on and `fulltext_max: 600`, 570 of 600
attempts failed and the client blocked the host after two 429s. Europe PMC carries the open-access subset
of preprints, so Methods-level matching still works through that route.

### Enabling it anyway

1. Set `sources.biorxiv.fulltext: true` and a modest `fulltext_max` (tens, not hundreds) in `config/radar.yaml`.
2. Keep `biorxiv.attach_fulltext(..., retries=1)` (the default): a second 429 would block the host for the run.
3. Respect the licence: the pipeline's data policy (`litradar/policy.py`) stores sections only for permissive
   licences; `cc_no` (all rights reserved) preprints are read for mentions at most.

### The sanctioned bulk route: requester-pays S3

bioRxiv and medRxiv publish their full-text corpus as MECA archives (JATS XML + figures) in
requester-pays Amazon S3 buckets:

* bioRxiv: `s3://biorxiv-src-monthly/` (monthly batches, e.g. `Current_Content/October_2026/`) and
  `s3://biorxiv-src-current/`
* medRxiv: `s3://medrxiv-src-monthly/`

Access with an AWS account, e.g. `aws s3 ls --request-payer requester s3://biorxiv-src-monthly/Current_Content/`;
the requester pays data-transfer costs. Each `.meca` zip holds `content/<id>.xml` (JATS), which
`litradar/extract/jats.py` already parses. Wiring a monthly S3 sync into the pipeline is the recommended
path for full-text coverage; it is not implemented here.

## Known limitations

* Re-posted versions (v2+) of older preprints are listed in the window and currently count as new.
* Only the corresponding author's institution is provided; other affiliations need the full text.
