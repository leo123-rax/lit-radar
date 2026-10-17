# openFDA device decisions: 510(k), De Novo, PMA

Adapter: `litradar/sources/openfda.py` · tests: `tests/test_sources_openfda.py` · verified live 2026-10-17.

FDA premarket decisions are the regulatory half of the diagnostics story the radar follows in the
literature: a 510(k) clearance or PMA supplement for a sequencing-based companion diagnostic, a new
respiratory panel, or a De Novo grant for a first-of-kind assay. openFDA publishes the 510(k) and PMA
databases as a free JSON API, so the adapter turns every decision dated inside the look-back window into a
`NewsItem` that the existing news stage can match against the company gazetteer and group in the digest.

## Official documentation

* 510(k) endpoint: https://open.fda.gov/apis/device/510k/
* PMA endpoint: https://open.fda.gov/apis/device/pma/
* Query syntax: https://open.fda.gov/apis/query-syntax/ · paging: https://open.fda.gov/apis/paging/
* Authentication and rate limits: https://open.fda.gov/apis/authentication/
* Field reference (`openfda` sub-object): https://open.fda.gov/apis/openfda-fields/
* Terms: https://open.fda.gov/terms/ · licence: https://open.fda.gov/license/ (public domain, with the
  standard "unvalidated data" disclaimer in every response)

## Endpoints and parameters (as used)

```
GET https://api.fda.gov/device/510k.json?search=decision_date:[20260917+TO+20261017]&limit=1000&skip=0
GET https://api.fda.gov/device/pma.json?search=decision_date:[20260917+TO+20261017]&limit=1000&skip=0
```

| Parameter | Value | Notes |
|---|---|---|
| `search` | `decision_date:[YYYYMMDD TO YYYYMMDD]` | inclusive range; the space is sent as `+` (percent-encoded `%20` also works). `AND applicant:"Roche"` narrows to an applicant phrase (token match: `"Roche"` finds `Roche Diagnostics`). |
| `limit` | 250 (`fetch()` default; max 1000) | a 510(k) record is 8-20 KB because of the `openfda.registration_number` / `fei_number` lists, so 250 keeps one response at ~2-5 MB (250 records of Aug-Oct 2026 = 2.1 MB, 1.3 s live). Pass `page_size=1000` for the openFDA maximum. |
| `skip` | 0, 250, … | **capped at 25,000** (`"Skip value must 25000 or less."`), so skip/limit paging covers 26,000 matches. Larger sets need the `Link` header `search_after` cursor or the bulk download. A month is ~220-325 510(k)s and ~110-120 PMA decisions (2026 live counts). |
| `api_key` | from `OPENFDA_API_KEY` | optional; sent first, as the docs show |

Response shape (both endpoints):

```json
{"meta": {"disclaimer": "...", "terms": "...", "license": "...", "last_updated": "2026-10-05",
          "results": {"skip": 0, "limit": 1000, "total": 182}},
 "results": [ { ...one record per decision... } ]}
```

Empty window or no match: **HTTP 404** with `{"error": {"code": "NOT_FOUND", "message": "No matches found!"}}`.
The adapter calls `http.get(..., ok_statuses=(200, 404))`, so this routine answer is not retried, not logged
with the request URL (which carries `api_key` when a key is set) and not counted in `http.stats["failures"]`;
it is logged at INFO as "no matches". A request that actually fails (network error, 5xx after retries, 429
host block) reaches the adapter as `None` and is logged at **WARNING** ("request failed … no results"), so an
openFDA outage is distinguishable from an empty window in the run log. A 400 (e.g. `skip` over the cap) is
logged at WARNING with the API's message.

## Refresh cadence and the look-back window

The official 510(k) and PMA pages state **"Frequency of API updates: Monthly"**. Live on 2026-10-17:
`meta.last_updated` 2026-10-05, newest indexed `decision_date` **2026-10-02** (510k, K260232) and
**2026-10-03** (PMA), bulk-file `export_date` 2026-10-12, and `count=decision_date` for 2026 shows October
at 37 510(k) records against 217-325 for every full month. The index is therefore ~15 days behind today and
can be **4-6 weeks behind just before a monthly refresh**.

Consequence for a weekly caller: the look-back must exceed one refresh interval plus the weekly cadence.
With a 21-day look-back, the run after a refresh would compute `start = since - 21` and never request the
decisions dated between the previous index edge and `start` (`INSERT OR IGNORE` dedupes, but cannot recover
rows that were never fetched). Use **`lookback_days: 45`** (`openfda.RECOMMENDED_LOOKBACK_DAYS`; 60 is safer).
The store dedupes re-fetched decisions by id, and `news_in_window()` also selects on `first_seen`, so a
decision indexed late still appears in the digest of the week it is first seen.

### 510(k) record fields seen live

Top level: `k_number` (`K254179`; De Novo grants appear as `DEN260007`), `applicant`, `device_name`,
`decision_date` (`"2026-09-18"`), `decision_code`, `decision_description`, `product_code`,
`advisory_committee` (two-letter), `advisory_committee_description`, `review_advisory_committee`,
`clearance_type` (`Traditional`, `Special`, `Abbreviated`, `Direct`), `statement_or_summary`
(`Summary` 179 / `Statement` 2 / `""` 1 in the test window), `third_party_flag`, `expedited_review_flag`,
`date_received`, `contact`, `address_1`, `address_2`, `city`, `state`, `zip_code`, `postal_code`,
`country_code`. `openfda`: `device_name` (the product-code classification name), `regulation_number`,
`device_class`, `medical_specialty_description`, `registration_number[]`, `fei_number[]` (the two lists
can hold 150+ entries and dominate the payload: a record is 8-20 KB and a 1,000-record page 10-20 MB,
which is why `fetch()` pages by 250).

Decision codes, all time (`count=decision_code.exact`): SESE 172,800 · SN 1,397 · SESK 499 · **DENG 490** ·
SESU 434 · SEKD 254 · ST 226 · SESD 128 · SESP 121 · PT 16 · SI 1.

### PMA record fields seen live

`pma_number` (`P200002`), `supplement_number` (`S015`, or `""` for an original approval), `applicant`,
`trade_name`, `generic_name`, `product_code`, `advisory_committee`, `advisory_committee_description`,
`supplement_type`, `supplement_reason`, `decision_code` (`APPR` original/panel approvals, `OK30` 30-day
notices accepted), `decision_date`, `date_received`, `ao_statement`, `docket_number`,
`expedited_review_flag`, `street_1`, `street_2`, `city`, `state`, `zip`, `zip_ext`, `openfda` as above.

Supplement types in the test window (116 decisions): 30-Day Notice 63 · Real-Time Process 18 ·
Normal 180 Day Track 12 · Normal 180 Day Track No User Fee 9 · Special (Immediate Track) 5 ·
135 Review Track For 30-Day Notice 4 · Panel Track 1 · `""` (originals) 4. Manufacturing-change
notices are half the feed, hence the `exclude_supplement_types` option.

Quirks: `count=decision_code.exact` and `count=advisory_committee.exact` return "Nothing to count" /
empty on the PMA endpoint (those fields are not keyword-indexed there), and trade names can carry
stray control characters (`Liverty\x99`), which the adapter strips.

## De Novo: confirmed absent as an endpoint, present inside 510k

* `GET https://api.fda.gov/device/denovo.json` → HTTP 404 `Cannot GET /device/denovo.json`.
* The device endpoint list from `https://api.fda.gov/download.json` is: classification, 510k,
  covid19serology, registrationlisting, enforcement, udi, event, recall, pma. No De Novo dataset.
* **But** De Novo grants are rows of the 510k dataset: `k_number` starts with `DEN`, `decision_code`
  is `DENG`, `clearance_type` is `Direct`, `decision_description` is `Unknown` (490 records all time;
  one in the 30-day test window, `DEN260007`). The adapter routes these to `feed='fda-denovo'` and links
  them to the De Novo database page.
* Authoritative De Novo listing (web database, per-request detail pages, searchable by date, no bulk
  file as of 2026-10-17): https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/denovo.cfm and the
  programme page https://www.fda.gov/medical-devices/premarket-submissions-selecting-and-preparing-correct-submission/de-novo-classification-request.
  Bulk files exist for 510(k) (`https://www.accessdata.fda.gov/premarket/ftparea/pmn96cur.zip`, monthly
  `pmnlstmn.zip`; index: https://www.fda.gov/medical-devices/510k-clearances/downloadable-510k-files) and PMA
  (`https://www.accessdata.fda.gov/premarket/ftparea/pma.zip`); these are the fallback if the API's
  26,000-record paging ceiling ever matters (it does not for a weekly window).

## Rate limits, API key, politeness

| | Without key | With free key |
|---|---|---|
| per minute | 240 per IP | 240 per key |
| per day | 1,000 per IP | 120,000 per key |

Set `OPENFDA_API_KEY` in `.env` (sign-up link on the authentication page). The adapter reads it with
`os.environ` (`openfda.api_key_from_env()`) and sends it as the `api_key` query parameter; pass
`api_key=""` to force anonymous access. Because it travels in the URL, the key appears in the HTTP
client's retry / failure log lines (5xx, 429, 400), same as the NCBI key; the routine 404 "no matches"
answer is no longer logged with the URL.

`litradar/http.py` paces `api.fda.gov` at 0.25 s between requests (240/min). A weekly run with the
recommended 45-day look-back and 250-record pages makes **3-4 requests** (two 510(k) pages, one or two
PMA pages); the applicant-count helper makes one request per applicant per endpoint. All well inside the
1,000/day anonymous quota.

## Mapping to `NewsItem`

| Field | 510(k) / De Novo | PMA |
|---|---|---|
| `id` | `openfda:K254179`, `openfda:DEN260007` | `openfda:P200002S015` (original: `openfda:P250050`) |
| `feed` | `fda-510k`, or `fda-denovo` when the number starts with `DEN` or the decision is `DENG` | `fda-pma` |
| `title` | `<device_name> — <applicant>` | `<trade_name or generic_name> — <applicant>` |
| `url` | `…/cfpmn/pmn.cfm?ID=K254179` · `…/cfpmn/denovo.cfm?ID=DEN260007` | `…/cfpma/pma.cfm?id=P200002S015` |
| `published` | `decision_date` as `YYYY-MM-DD` | same |
| `summary` | `510(k) K… · product code FGE (classification name) · regulation 876.5010 · class 2 · advisory committee GU (Gastroenterology, Urology) · decision SESE (Substantially Equivalent) 2026-09-18 · clearance type Traditional · applicant …, City, ST/CC · received … · 510(k) summary available · third-party review · expedited review` | `PMA P… · supplement S015: 30-Day Notice — reason · generic name … · product code … · class 3 · advisory committee CV (Cardiovascular) · decision OK30 (30-day notice accepted) 2026-09-24 · applicant … · received … · docket … · AO statement: …` |
| `companies` | left empty; the pipeline's news stage fills it from the company gazetteer | same |

All three FDA link patterns returned HTTP 200 on 2026-10-17.

### Options on `openfda.fetch(http, start, end, ...)`

* `kinds=("510k", "pma")`: `510k` also yields the `fda-denovo` feed.
* `advisory_committees=()`: keep only these two-letter codes. `openfda.IVD_ADVISORY_COMMITTEES` =
  `CH, HE, IM, MI, PA, TX, MG` (Clinical Chemistry, Hematology, Immunology, Microbiology, Pathology,
  Clinical Toxicology, Medical Genetics). Live 510(k) counts for 2025-01-01..2026-10-17: RA 857, OR 758,
  CV 681, SU 652, GU 458, DE 398, NE 383, HO 304, PM 219, **MI 195**, OB 178, AN 173, `""` 139,
  **CH 108**, EN 90, OP 70, **IM 41**, **HE 36**, **TX 32**, **PA 23**, **MG 1**.
* `include_supplements=True`, `exclude_supplement_types=()`: PMA noise control
  (`["30-Day Notice"]` removes manufacturing-change notices).
  The filter reads `advisory_committee` and falls back to `review_advisory_committee`, because De Novo rows
  leave the former blank (live `DEN260007`: `advisory_committee=""`, `review_advisory_committee="CV"`);
  the summary shows the fallback as `review advisory committee CV`.
* `api_key=None`: read the environment; `""`: anonymous.
* `page_size=250`, `max_records=26000`: paging guards; `page_size` is clamped to 1000 and a warning is
  logged when a window exceeds the skip cap. Monthly backfill chunks fit the defaults (max ~325 510(k)s).

`openfda.applicant_counts(http, start, end, ["Roche", ...])` returns `{kind: {name: total}}` using
`meta.results.total` with `limit=1`; `0` means HTTP 404 "no matches", `None` means the request failed.
Any iterable (including a generator) is accepted for the names.

## Live test, 2026-10-17 (anonymous, through the project's `Http` client)

Re-run after the review fixes (`page_size=250`, `ok_statuses=(200, 404)`, committee fallback). **30 requests
in total, 0 retries, 0 failures** in `http.stats` (the empty-window 404s no longer count); `last_updated`
2026-10-05 on every response.

| Run | Window | Items | Feeds | Requests |
|---|---|---|---|---|
| adapter defaults (all devices) | 2026-09-17..2026-10-17 (30 d) | **298** | 181 `fda-510k` · 1 `fda-denovo` · 116 `fda-pma` | 2 |
| recommended config: IVD committees, no 30-Day Notices | 2026-09-02..2026-10-17 (45 d) | **40** | 31 `fda-510k` · 0 `fda-denovo` · 9 `fda-pma` | 3 |
| all devices | 2026-09-02..2026-10-17 (45 d) | 549 | 344 · 2 · 203 | 3 |
| all devices | 2026-08-18..2026-10-17 (60 d) | 762 | 476 · 7 · 279 | 4 |
| empty window | 2030-01-01..2030-01-31 | 0 | HTTP 404 on both endpoints, logged at INFO, `failures` unchanged | 2 |

Every run: all ids unique, no item without a decision date, and decision dates end at 2026-10-02 (510k) /
2026-10-03 (PMA) whatever the window, because the index has not refreshed since 2026-10-05 (see "Refresh
cadence"). Excluding 30-Day Notices keeps 53 of the 116 PMA decisions in the 30-day window.

The 40 items of the recommended configuration: an EBV serology trio (K260232 / K260221 / K260233),
respiratory PCR panels (K261336, K263120, K263054, K263035), a blood-culture ID/AMR panel (K262168),
antimicrobial-susceptibility cards, an HCV antibody assay, an NT-proBNP assay, an SMA newborn-screening kit,
a hematology analyser; on the PMA side, companion-diagnostic supplements for a tumour-profiling NGS test
(P210011S002), an NGS oncology panel (P240040S005), an IHC CDx (P230016S003), a tissue CDx (P170019S069) and
a liquid-biopsy CDx (P200010S024, P200010S029), a stool RNA screening test (P230001S014), an original PMA for
an MSI analysis system (P250046) and one insulin-pump system supplement (P160017S140). Note that the CH
(Clinical Chemistry) panel also carries diabetes-management devices: six of the 31 510(k)s here are glucose
monitors, insulin pumps or dosing software. Drop `CH` from `advisory_committees` if those are noise, at the
cost of the chemistry assays. The one De Novo in the 45-day window (DEN260007, review panel CV) is correctly
excluded by the IVD filter; the 60-day all-device run carried 7 De Novo grants.

510(k) and PMA decisions in the 30-day window by applicant phrase (`applicant_counts`, names passed as a
generator, 16 requests, 0 failures):

| Applicant phrase | 510(k) | PMA | Items |
|---|---|---|---|
| Illumina | 0 | 0 | |
| Roche | 3 | 0 | K260232 / K260221 / K260233, Elecsys EBV VCA IgG / EBNA IgG / IgM, 2026-10-02, product codes LSE / LJN |
| Thermo Fisher | 0 | 0 | |
| Bio-Rad | 0 | 0 | |
| Qiagen | 1 | 0 | K262168, QIAstat-Dx BCID GN Plus AMR Panel, 2026-09-24, product code PEN |
| Guardant | 0 | 1 | P200010 S029, Guardant360 CDx, 2026-09-18, product code PQP |
| Foundation Medicine | 0 | 1 | P190032 S032, FoundationOne Liquid CDx (F1LCDx), 2026-09-18, product code PQP |
| Exact Sciences | 0 | 0 | |

A zero means openFDA answered HTTP 404 "No matches found!" for that applicant phrase inside the window, not
that the search failed; a failed lookup is reported as `None`.

## Wiring (not applied here; the pipeline and config files are owned by other work)

1. `litradar/pipeline.py`: import `from .sources import openfda as openfda_src` and append to
   `fetch_news()` before `self.store.commit()`:

   ```python
   # openFDA: 510(k) clearances, De Novo grants and PMA decisions as news items (docs/sources/openfda.md)
   ocfg = self._src("openfda")
   if ocfg.get("enabled", False):
       try:
           lookback = int(ocfg.get("lookback_days", openfda_src.RECOMMENDED_LOOKBACK_DAYS))  # datasets refresh monthly
           start = (date.fromisoformat(since) - timedelta(days=lookback)).isoformat()
           until = datetime.now(timezone.utc).date().isoformat()
           items: List[NewsItem] = []
           for it in openfda_src.fetch(
               self.http, start, until,
               kinds=tuple(ocfg.get("kinds") or ("510k", "pma")),
               advisory_committees=tuple(ocfg.get("advisory_committees") or ()),
               include_supplements=bool(ocfg.get("include_supplements", True)),
               exclude_supplement_types=tuple(ocfg.get("exclude_supplement_types") or ()),
           ):
               it.companies = sorted({m.entity_id for m in self.companies.find(f"{it.title} {it.summary}")})
               items.append(it)
           added = self.store.upsert_news(items)
           n += added
           self.counts["news.openfda"] += added
       except Exception as exc:  # noqa: BLE001
           self.errors.append(f"openfda: {exc}")
           log.exception("openfda failed")
   ```

   The store's `INSERT OR IGNORE` on `news_items.id` makes the overlapping look-back idempotent, and
   `news_in_window()` selects on `first_seen` as well as `published`, so a decision indexed late still
   appears in the digest of the week it is first seen. In `run()`, let `--only openfda` reach the news
   stage: `if only is None or "rss" in only or "openfda" in only: self.fetch_news(since)`.

2. `config/radar.yaml`, under `sources:`:

   ```yaml
     openfda:
       enabled: true             # FDA 510(k) clearances, De Novo grants, PMA decisions via api.fda.gov (OPENFDA_API_KEY optional)
       kinds: [510k, pma]        # 510k also yields the fda-denovo feed
       lookback_days: 45         # the datasets refresh MONTHLY; the look-back must exceed one refresh interval (60 is safer)
       advisory_committees: [CH, HE, IM, MI, PA, TX, MG]   # in-vitro diagnostics only; [] = every device (see note)
       include_supplements: true
       exclude_supplement_types: ["30-Day Notice"]   # manufacturing-change notices are ~half of PMA decisions
   ```

   `advisory_committees: []` is the opt-out that ingests every device decision (~400-500 510(k)s plus
   ~100 PMA decisions per 45-day window live). The digest reads `store.news_in_window(since, limit=news_n*4)`
   ordered by `published DESC`, and FDA decision dates lag by weeks, so with `[]` the SQL limit can cut
   company-matched FDA items before the renderer groups them and unmatched items never reach the capped
   "Other headlines" section. If you use `[]`, raise `digest.news_n` (e.g. to 150) or keep the IVD list.

3. `.env.example`: `OPENFDA_API_KEY=   # https://open.fda.gov/apis/authentication/ (1,000/day anonymous -> 120,000/day)`.

4. `docs/SOURCES.md`, company and news table: one row pointing here.
