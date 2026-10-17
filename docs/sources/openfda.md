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
| `limit` | 1000 | the maximum openFDA allows per page |
| `skip` | 0, 1000, … | **capped at 25,000** (`"Skip value must 25000 or less."`), so skip/limit paging covers 26,000 matches. Larger sets need the `Link` header `search_after` cursor or the bulk download. A week is ~50 510(k)s and ~30 PMA decisions. |
| `api_key` | from `OPENFDA_API_KEY` | optional; sent first, as the docs show |

Response shape (both endpoints):

```json
{"meta": {"disclaimer": "...", "terms": "...", "license": "...", "last_updated": "2026-10-05",
          "results": {"skip": 0, "limit": 1000, "total": 182}},
 "results": [ { ...one record per decision... } ]}
```

Empty window or no match: **HTTP 404** with `{"error": {"code": "NOT_FOUND", "message": "No matches found!"}}`.
The shared `Http` client returns `None` for that, and the adapter treats `None` on the first page as
"no matches". (The client also counts the 404 in `http.stats["failures"]`, so a run over an empty window
shows one "failure" per endpoint that is not an error.)

### 510(k) record fields seen live

Top level: `k_number` (`K254179`; De Novo grants appear as `DEN260007`), `applicant`, `device_name`,
`decision_date` (`"2026-09-18"`), `decision_code`, `decision_description`, `product_code`,
`advisory_committee` (two-letter), `advisory_committee_description`, `review_advisory_committee`,
`clearance_type` (`Traditional`, `Special`, `Abbreviated`, `Direct`), `statement_or_summary`
(`Summary` 179 / `Statement` 2 / `""` 1 in the test window), `third_party_flag`, `expedited_review_flag`,
`date_received`, `contact`, `address_1`, `address_2`, `city`, `state`, `zip_code`, `postal_code`,
`country_code`. `openfda`: `device_name` (the product-code classification name), `regulation_number`,
`device_class`, `medical_specialty_description`, `registration_number[]`, `fei_number[]` (the two lists
can hold 150+ entries and dominate the payload; a 1,000-record page is tens of MB).

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
client's warning/info log lines when a request fails (same as the NCBI key).

`litradar/http.py` paces `api.fda.gov` at 0.25 s between requests (240/min). A weekly run with the
default settings makes **2 requests** (one page per endpoint); the applicant-count helper makes one
request per applicant per endpoint.

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
* `api_key=None`: read the environment; `""`: anonymous.
* `page_size=1000`, `max_records=26000`: paging guards; a warning is logged when a window exceeds the cap.

`openfda.applicant_counts(http, start, end, ["Roche", ...])` returns `{kind: {name: total}}` using
`meta.results.total` with `limit=1`.

## Live test, 2026-10-17 (window 2026-09-17..2026-10-17, anonymous)

Through the project's `Http` client, `openfda.fetch()` produced **298 items**: 181 `fda-510k`,
1 `fda-denovo`, 116 `fda-pma`; 298 unique ids; 0 items without a decision date; decision dates span
2026-09-17..2026-10-03 while the dataset's `last_updated` was 2026-10-05, i.e. openFDA indexes decisions
with a lag of roughly one to two weeks. With `advisory_committees=IVD_ADVISORY_COMMITTEES`: 31 items
(20 510(k), 11 PMA), including an EBV serology trio, a respiratory PCR panel, a blood-culture ID/AMR panel,
three PD-L1/HER2/MAGE-A4 IHC companion-diagnostic supplements and two liquid-biopsy CDx supplements.
Excluding 30-Day Notices leaves 53 of the 116 PMA decisions.

510(k) and PMA decisions in the window by applicant phrase (sequencing / IVD names used as the probe):

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

A zero means no decision with that applicant phrase was dated inside the window (HTTP 404 from the API),
not that the search failed. 21 requests in total, 0 retries.

## Wiring (not applied here; the pipeline and config files are owned by other work)

1. `litradar/pipeline.py`: import `from .sources import openfda as openfda_src` and append to
   `fetch_news()` before `self.store.commit()`:

   ```python
   # openFDA: 510(k) clearances, De Novo grants and PMA decisions as news items (docs/sources/openfda.md)
   ocfg = self._src("openfda")
   if ocfg.get("enabled", False):
       try:
           lookback = int(ocfg.get("lookback_days", 21))  # openFDA indexes decisions 1-2 weeks late
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
       enabled: true             # FDA 510(k) clearances, De Novo grants, PMA decisions via api.fda.gov
       kinds: [510k, pma]        # 510k also yields the fda-denovo feed
       lookback_days: 21         # the API indexes decisions 1-2 weeks after the decision date
       advisory_committees: []   # e.g. [CH, HE, IM, MI, PA, TX, MG] to keep in-vitro diagnostics only
       include_supplements: true
       exclude_supplement_types: ["30-Day Notice"]   # manufacturing-change notices are ~half of PMA decisions
   ```

3. `.env.example`: `OPENFDA_API_KEY=   # https://open.fda.gov/apis/authentication/ (1,000/day anonymous -> 120,000/day)`.

4. `docs/SOURCES.md`, company and news table: one row pointing here.
