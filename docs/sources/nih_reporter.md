# NIH RePORTER (funded projects): shared-instrumentation awards, SBIR/STTR, keyword hits

Adapter: `litradar/sources/nih_reporter.py`. Yields `NewsItem(feed="nih-reporter", ...)`.
Official documentation: https://api.reporter.nih.gov/ (Swagger UI; field list, paging rules, etiquette).
Data dictionary: https://api.reporter.nih.gov/documents/Data%20Elements%20for%20RePORTER%20Project%20API%20v2.pdf

Tested live on 2026-10-17 from a residential IP with the project's `lit-radar/0.1` User-Agent.
No key, no registration.

## Why it is in the radar

* **S10 awards** (Shared Instrumentation Grant, High-End Instrumentation, and the smaller
  undergraduate-institution instrumentation programme) each pay for one named machine at a core
  facility. Titles read like purchase orders: "NovaSeq X Plus Sequencing System", "Orbitrap Astral
  MS Instrument", "Acquisition of Glacios 2 ...". That is an **installed-base signal** with a date
  (the award notice) and a dollar amount, independent of papers and of archive submissions.
* **SBIR/STTR awards** (R41/R42 STTR, R43/R44 SBIR, U43/U44 cooperative agreements, SB1
  commercialisation readiness) are small-company R&D, filterable by organisation name.
* **Keyword hits** across any mechanism: P30 cancer-centre shared resources and R01 abstracts name the
  instruments they run, so a text search for an instrument or vendor surfaces adopters.

## Endpoint, as verified

```
POST https://api.reporter.nih.gov/v2/projects/search
Content-Type: application/json
{
  "criteria": {
    "award_notice_date": {"from_date": "2026-07-19", "to_date": "2026-10-17"},
    "activity_codes": ["S10"],
    "advanced_text_search": {"operator": "advanced",
                             "search_field": "projecttitle,abstracttext",
                             "search_text": "\"NovaSeq X\" OR Revio"},
    "org_names": ["..."]
  },
  "include_fields": ["ApplId", "ProjectNum", "ProjectTitle", "AbstractText", "ActivityCode", "AwardAmount",
                     "AwardNoticeDate", "ProjectStartDate", "FiscalYear", "Organization", "AgencyIcAdmin",
                     "ProjectDetailUrl"],
  "offset": 0, "limit": 500,
  "sort_field": "award_notice_date", "sort_order": "asc"
}
```

Response (`include_fields` are PascalCase on the way in, snake_case on the way out):

```json
{"meta": {"search_id": "ZPL6Jq4D4kSbCctvbW-isA", "total": 57, "offset": 0, "limit": 500,
          "sort_field": "award_notice_date", "sort_order": "asc", "sorted_by_relevance": false,
          "properties": {"URL": "https:/reporter.nih.gov/search/<search_id>/projects"}},
 "results": [{"appl_id": 11365533, "project_num": "1S10GM164903-01",
              "project_title": "2-laser CytoFLEX Flow Cytometer equipped with Plate Loader",
              "award_notice_date": "2026-08-05T00:00:00", "project_start_date": "2026-09-01T00:00:00",
              "project_end_date": "2027-08-31T00:00:00", "activity_code": "S10", "award_amount": 232415,
              "fiscal_year": 2026, "is_new": false, "award_type": "1", "opportunity_number": "PAR-23-138",
              "organization": {"org_name": "...", "org_city": "CARSON", "org_state": "CA",
                               "org_country": "UNITED STATES", "dept_type": "...", "org_ueis": ["..."]},
              "agency_ic_admin": {"code": "GM", "abbreviation": "NIGMS", "name": "National Institute of General Medical Sciences"},
              "project_detail_url": "https://reporter.nih.gov/project-details/11365533",
              "abstract_text": "Flow cytometry is a cutting-edge laser-based technology ...",
              "pref_terms": "Area;Basic Science;Cell Size;...", "principal_investigators": [...], ...}]}
```

Without `include_fields` the full record is ~8 KB per project (45 keys, including `abstract_text`,
`phr_text`, `terms`, `pref_terms`, `principal_investigators`, `program_officers`, `spending_categories`,
`geo_lat_lon`); the adapter asks for 17 fields.

### Paging and limits (each verified with a live call)

| Request | Response |
|---|---|
| `limit: 501` | HTTP 400 `{"message": "System doesn't support limit value greater than 500. Please reduce your limit value."}` |
| `offset: 15000` | HTTP 400 `{"message": "Requested limit exceeded. Maximum offset is 14,999."}` |
| `offset` ≥ `meta.total` (e.g. 500 when total is 57) | HTTP 400 `{"message": "Requested offset 500 exceeded total records count of 57"}`, **not** an empty page |
| `limit: 0` | HTTP 200, `meta.total` filled, `results: []` (used by `count()`) |
| malformed criteria (`"award_notice_date": "x"`) | HTTP 400 with a JSON **list** of strings: `["Error converting value ... to type 'Nih.Rptrshared.Core.DateRange' ..."]` |
| unknown name in `include_fields` | silently ignored |
| dates with a time component | accepted |
| no criteria match | HTTP 200, `total: 0` |

So the adapter pages `offset += len(results)` and stops when `offset >= meta.total`, on an empty page,
at `max_records`, or at the 14,999 offset cap (narrow the date window for bigger pulls). It never
raises on API trouble; a missing page ends the iteration with a warning.

### Rate limit

The documentation asks users to "post no more than one URL request per second and limit large jobs
to either weekends or weekdays between 9:00 PM and 5:00 AM EST". Response headers advertise a
per-minute window: `X-Rate-Limit-Limit: 1m`, `X-Rate-Limit-Remaining: 199`, `X-Rate-Limit-Reset`.
`litradar/http.py` sets `DEFAULT_MIN_INTERVAL["api.reporter.nih.gov"] = 1.0`. A weekly S10 pull is one
request; the whole S10 history since 2023 (578 awards) is two.

### Text search semantics

* `operator: "and"` / `"or"` split `search_text` on whitespace, so `NovaSeq X` becomes two words.
* `operator: "advanced"` honours quoted phrases and `AND` / `OR` / `NOT`; the adapter quotes every
  term and joins with `OR`, 25 phrases per request (`terms_per_query`), de-duplicating across batches.
  A 32-phrase query ran as two requests.
* `search_field` is a comma list of `projecttitle`, `abstracttext`, `terms`.
* Matching is case-insensitive and whole-word: `"PromethION"` also returns Sable Systems'
  "Promethion" metabolic cages, `"Vega"` returns unrelated items. Keyword hits are candidates;
  run the instrument gazetteer (with its context rules) over title + summary downstream.
* With any text search the response says `sorted_by_relevance: true` even when `sort_field` is given;
  the adapter de-duplicates on `appl_id` across pages in case ties reorder.

### Organisation filter

`org_names` is a case-insensitive **substring** filter: `"MICROPURE"`, `"micropure genomics"` and
`"MICROPURE GENOMICS INC"` all return the same company, and `"ILLUMINA"` + SBIR codes returned
*Illumination Diagnostics* and *Illuminant Surgical*. `org_names_exact_match` (adapter:
`exact_org_names=True`) is exact. RePORTER organisation names are upper-case legal names
("PACIFIC BIOSCIENCES OF CALIFORNIA, INC."), so substring on a distinctive stem is usually right.

## Entry points

```python
from litradar.sources import nih_reporter as nr

# every S10 award noticed in the window (~60 per quarter); the gazetteer tags instruments downstream
for item in nr.s10_awards(http, "2026-07-19", "2026-10-17"):
    ...
# S10 awards whose title/abstract names any of these phrases (batched, de-duplicated)
nr.s10_awards(http, "2023-01-01", "2026-10-17", terms=["PacBio", "Revio", "Sequel IIe", "NovaSeq X"])
# any mechanism, keyword hits, optional activity-code / organisation filters
nr.keyword_awards(http, start, end, ["Revio"])
nr.keyword_awards(http, start, end, [], activity_codes=nr.SBIR_STTR_CODES, org_names=["GENOMICS"])
# SBIR/STTR convenience wrapper (R41-R44, U43/U44, SB1)
nr.sbir_awards(http, start, end, org_names=["Example Instruments"], exact_org_names=True)
# cheap sizing
nr.count(http, {"award_notice_date": nr.date_range(start, end), "activity_codes": ["S10"]})
```

`keyword_awards` raises `ValueError` when given no terms, no activity codes and no organisations,
because that query would be "every NIH award in the window".

### NewsItem mapping

| NewsItem | From |
|---|---|
| `id` | `nih-reporter:<appl_id>` |
| `feed` | `nih-reporter` |
| `title` | `project_title` (whitespace collapsed); `decorate_titles=True` appends ` — <org> (<activity code>, $<amount>)` for digest lines |
| `url` | `project_detail_url`, else `https://reporter.nih.gov/project-details/<appl_id>` |
| `published` | `award_notice_date`, else `project_start_date`, else `budget_start` (YYYY-MM-DD) |
| `summary` | first 1,500 characters of `abstract_text`, newline, then `S10 \| 1S10GM164903-01 \| ORG (CITY, ST) \| $232,415 \| NIGMS \| FY2026` |
| `companies` | empty; the pipeline's company gazetteer fills it from title + summary |

## Live test, 2026-10-17

`s10_awards(http, "2026-07-19", "2026-10-17")`: `count()` said 57, the adapter yielded 57 items in one
request (plus one for the count). Notice months: Jul 10, Aug 28, Sep 19. Award total $34,385,926,
median $249,966 (many awards sit just under $250k, consistent with a programme ceiling).

Titles naming a gazetteer instrument: **10 of 57**. Each once: NovaSeq X (Illumina, ILMN; a
$1,371,730 "NovaSeq X Plus Sequencing System"), Orbitrap Tribrid (Thermo Fisher; "Orbitrap Ascend
Tribrid ... Proteomics", $1,533,926), Glacios cryo-EM (Thermo Fisher, $2,000,000), Attune and CytoFLEX
flow cytometers, Seahorse XF Pro (Agilent), the Leica STELLARIS entry (matched a "Leica Mica" title),
the Nikon AX entry (matched a "Nikon Ti2-E" title), the Evident/Olympus entry ("FLUOVIEW FV4000"),
SPT Labtech Mosquito/Dragonfly. Vendor tally: Thermo Fisher 3, Danaher 2, Agilent,
Evident, Nikon, SPT Labtech, Illumina 1 each.

The other 47 titles are generic ("confocal microscope", "600 MHz NMR", "GPU cluster", "cell sorter")
or name brands the gazetteer does not carry yet: LUMICKS C-Trap (three awards, $0.99-1.24M each),
Abberior MINFLUX ($1.8M), Bruker BioSpec 7T, TA ElectroForce, Delsys Trigno, Phasics SID4, Dell
PowerEdge/NVIDIA H200 servers. Those are gazetteer candidates, not adapter gaps.

Other calls in the same session:

* S10 since 2023-01-01 naming long-read sequencers (phrase query): Sequel IIe at two universities
  (Apr and Jun 2023, ~$0.49M each) and a PromethION 24 (May 2026, $433,700); no Revio or Vega S10 award
  exists in RePORTER. Two false positives from "Promethion" metabolic systems, as noted above.
* Any mechanism mentioning "Revio" since 2024-01-01: 11 awards (R01 4, P30 3, U19, R24, R15, R43).
* SBIR/STTR, last 30 days: 198 awards in total; 0 with "GENOMICS" in the organisation name.
* `limit > 500`, `offset > 14,999`, `offset ≥ total`, malformed body: the four HTTP 400 shapes above.

## Caveats

* `award_notice_date` is when the notice was issued; `project_start_date` can be weeks later, and
  RePORTER's own data refresh is periodic (weekly according to its FAQ; not verified here), so a 7-day
  window catches most but not all new notices. A 14-day
  look-back with the store's `INSERT OR IGNORE` de-duplication is safer.
* S10 titles are free text with typos ("Oxford Nanospore Technologies PromethlON 24"); the gazetteer
  needs the alias, not the API.
* An S10 award is a purchase intent, not a shipment; as a rule of thumb the instrument installs
  months after the notice (not measured here).
* Only NIH-funded US institutions appear. Instruments bought with NSF MRI, institutional or
  non-US money are invisible here (NSF has a separate awards API).
* `/v2/publications/search` (the sibling endpoint) links PMIDs to project numbers and is not used yet.
