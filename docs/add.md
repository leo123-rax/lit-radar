# `litradar add`: put a paper into the corpus by identifier

Module: `litradar/ingest.py`. Command: `litradar/commands/add.py`. Tests: `tests/test_ingest.py`.

The weekly run decides on its own what enters the corpus. `add` is the other door: a paper you
read about, a preprint a colleague sent, a reference list from a call. Each identifier is resolved
through the **same source adapters** the pipeline uses, merged with `Pipeline.merge`, given full
text only where the data policy allows, annotated with the instrument and company gazetteers and
the tracked-author list (`Pipeline.extract`), scored (`Pipeline.rank`) and stored with the same
upsert. Afterwards the paper is indistinguishable from a pipeline paper: it shows up in `search`,
`entity`, `rematch` and the digest for its publication week.

## Usage

(`litradar ...` below is the console script installed by `pip install -e .`; without it, `python -m litradar ...`
is the identical command.)

```bash
litradar add 10.1038/s41592-024-02201-0                       # a DOI
litradar add PMID:38409223 PMC10887192 arXiv:2301.08370        # several ids of any kind
litradar add https://www.biorxiv.org/content/10.1101/2023.04.30.538439v2.full
litradar add --file reading-list.txt --tag reading-list        # a list, labelled
litradar add 10.1101/2023.04.30.538439 --no-fulltext           # metadata and abstract only
litradar add 10.1038/s41592-024-02201-0 --json                 # machine-readable result

litradar tags                                                  # tag counts
litradar tags reading-list                                     # papers carrying one tag
```

Options:

| Option | Meaning |
|---|---|
| `ID …` | one or more identifiers (see the table below) |
| `--file PATH` | text file with one identifier per line; blank lines and anything after `#` are ignored |
| `--fulltext` / `--no-fulltext` | fetch open-access full text where the licence allows (default on) |
| `--tag LABEL` | store a label in `extra_json.tags` (repeatable; labels accumulate across adds) |
| `--json` | print the results as a JSON list instead of the one-screen summaries |

Exit code: `0` when every identifier resolved, `1` when at least one failed (the others are still
stored), `2` when no identifier was given or `--file` does not exist. Adding a paper that is
already in the store **refreshes** it: metadata is re-fetched, mentions and the score are
recomputed, tags are unioned, `first_seen` and `added_at` are kept.

## Identifier types

| You can type | Parsed as | Example |
|---|---|---|
| bare DOI, `doi:…`, `https://doi.org/…`, `dx.doi.org/…` | DOI | `10.1038/s41592-024-02201-0` |
| digits, `PMID:…`, PubMed URL, Europe PMC `/article/MED/…` URL | PMID | `38409223`, `https://pubmed.ncbi.nlm.nih.gov/38409223/` |
| `PMC…`, `PMCID:…`, PMC URL, Europe PMC `/article/PMC/…` URL | PMCID | `PMC10887192`, `https://pmc.ncbi.nlm.nih.gov/articles/PMC10887192/` |
| `PPR…`, Europe PMC `/article/PPR/…` URL | Europe PMC preprint id | `PPR653043` |
| new or old arXiv id, `arXiv:…`, `arxiv.org/abs|pdf/…`, `10.48550/arXiv.…` DOI | arXiv id | `2301.08370`, `q-bio/0601001` |
| bioRxiv / medRxiv DOI or URL (`10.1101/…`, `10.64898/…`), with or without `v2`, `.full`, `.pdf` | DOI (preprint) | `https://www.biorxiv.org/content/10.64898/2026.10.05.756623v1.full` |
| any publisher, Crossref or aggregator URL that contains a DOI in its path or query | DOI | `https://api.crossref.org/works/10.1038/…`, `https://journals.plos.org/plosone/article?id=10.1371/…` |

Notes:

* DOIs are lower-cased; a preprint version suffix (`v2`) and page suffixes (`.full`, `.full.pdf`,
  `/abstract`) are dropped so every form of the same paper maps to the same store id (`doi:<doi>`).
* bioRxiv and medRxiv both issue date-shaped DOIs under `10.1101/` and, since 2026, `10.64898/`.
  Only date-shaped DOIs are sent to the preprint API: `10.1101/gr.…` (a CSHL journal) is treated as
  a journal DOI.
* A bioRxiv URL in the `/content/early/YYYY/MM/DD/<id>` form carries no DOI prefix and is rejected
  with a hint; use the `/content/10.xxxx/…` form shown on the paper page.
* Arbitrary URLs without a DOI in them (for example a Nature `/articles/<suffix>` page or a Cell
  Press `fulltext/S…` page) are rejected: copy the DOI from the page instead.

## How an identifier is resolved

Lookups run in this order; a step is skipped when it does not apply to the identifier, and a
record found by one step that carries a DOI continues through the DOI-keyed steps. Whatever came
back is merged in the same fixed order (`Pipeline.merge`: longer abstract and title win, the
earliest date wins, an article record outranks a preprint record), so a hand-added paper looks
exactly like one the weekly run would have assembled from several sources.

| Step | Service and request | Applies to |
|---|---|---|
| 1 | Europe PMC `GET /webservices/rest/search?query=…&resultType=core` with `DOI:"<doi>"`, `EXT_ID:<pmid> AND SRC:MED`, `PMCID:<pmcid>` or `EXT_ID:<ppr> AND SRC:PPR` | DOI, PMID, PMCID, PPR; arXiv ids only when arXiv returned a journal DOI |
| 2 | Crossref `GET https://api.crossref.org/works/<doi>` (`mailto` added when `LITRADAR_CONTACT_EMAIL` is set) | any DOI known after step 1 |
| 3 | bioRxiv `GET https://api.biorxiv.org/details/{biorxiv,medrxiv}/<doi>`; every version comes back and the latest is kept; the other server is tried when the first says `no posts found` (a medRxiv URL tries medRxiv first) | date-shaped `10.1101/` and `10.64898/` DOIs |
| 4 | arXiv `GET https://export.arxiv.org/api/query?id_list=<id>` (one request per 3 s, as the adapter's pacing table requires) | arXiv ids |

Europe PMC indexes arXiv preprints but without a DOI or arXiv id in the record, so an arXiv id is
resolved by arXiv alone unless arXiv knows the paper's journal DOI, in which case steps 1 and 2 run
on that DOI too and the journal record becomes the base of the merge.

### Verified live (2026-10-17)

| Lookup | Request | Result |
|---|---|---|
| Europe PMC by DOI | `query=DOI:"10.1038/s41592-024-02201-0"`, `resultType=core` | `hitCount 1`, `source MED`, `pmid 38409223`, `pmcid null`, `isOpenAccess N`, journal title, 7 authors, abstract |
| Europe PMC by PMID | `query=EXT_ID:38409223 AND SRC:MED` | the same record (`EXT_ID:38409223` alone also returns it) |
| Europe PMC by PMCID | `query=PMCID:PMC10887192` | `hitCount 1`, `isOpenAccess Y`, `license "cc by"`, DOI 10.3390/brainsci14020176 |
| Europe PMC by preprint DOI | `query=DOI:"10.1101/2023.04.30.538439"` | `source PPR`, `id PPR653043`, abstract, `isOpenAccess N`; a `10.64898/2026.10.05.756623` DOI also resolves to a PPR record |
| Europe PMC full text | `GET /PMC10887192/fullTextXML` | HTTP 200, 374,139 bytes; `parse_jats` filled funding, COI, data availability, body, 11 authors (a case report, so no Methods section) |
| Crossref | `GET /works/10.1038/s41592-024-02201-0` | `status ok`, `message-type work`, type `journal-article`, ISSN `1548-7091`/`1548-7105`, funders, no abstract (publisher did not deposit one); an unknown DOI answers HTTP 404 |
| bioRxiv details | `GET /details/biorxiv/10.1101/2023.04.30.538439` | `messages[0].status ok`, two versions (v1 2023-05-01, v2 2023-07-02), `license cc_by_nc_nd`, `published 10.1038/s41592-024-02201-0`, `jatsxml` URL per version |
| bioRxiv details, new prefix | `GET /details/biorxiv/10.64898/2026.10.05.756623` | two versions, `license cc_by_nc`; unknown DOIs answer HTTP 200 with `{"status": "no posts found"}` |
| medRxiv details | `GET /details/medrxiv/10.1101/2020.03.24.20042937` | two versions, `license cc_by_nd`; the 2026-10-14..16 window shows medRxiv also issuing `10.64898/` DOIs |
| arXiv | `GET http://export.arxiv.org/api/query?id_list=2301.08370` | HTTP 301 to `https://`, then one entry (`2301.08370v1`, `math.FA`, 3 authors); `id_list=9999.99999` answers an empty feed (`totalResults 0`), not an error entry |

End-to-end, through `ingest.add_papers` with the real HTTP client into a throwaway database
(same day; 23 requests, 1 retry, 2 non-200 answers):

| Identifier given | Resolved via | Stored as | Full text |
|---|---|---|---|
| `https://doi.org/10.1038/s41592-024-02201-0` | `europepmc:MED:38409223`, `crossref` | `doi:…`, article, tier-A journal, score 6.07 | `none` (no PMC id) |
| `PMID:38409223` | same two lookups | **refresh** of the row above (idempotent) | `none` |
| `PMC10887192` | `europepmc:MED:38391750`, `crossref` | `doi:10.3390/brainsci14020176`, 3 instrument mentions from Methods | `full` (Europe PMC XML) |
| `PPR653043` | `europepmc:PPR:PPR653043`, `crossref`, `biorxiv v2` | `doi:10.1101/2023.04.30.538439`, preprint, `published as` its journal DOI, 1 tracked author, 7 mentions | `full` (bioRxiv JATS, `cc_by_nc_nd`) |
| `https://www.biorxiv.org/content/10.64898/2026.10.05.756623v2` | `europepmc:PPR:PPR1336144`, `crossref`, `biorxiv v2` | `doi:10.64898/…`, preprint | `none`: the v2 JATS URL (posted two days earlier) did not answer 200 |
| `10.1101/2020.03.24.20042937` | `europepmc:PPR:PPR130492`, `crossref`, `medrxiv v2` (bioRxiv said `no posts found` first) | `doi:…`, preprint | `none`: `www.medrxiv.org` answered 429 twice and the client closed the host for the run |
| `arXiv:2301.08370` | `arxiv:2301.08370v1` | `arxiv:2301.08370v1`, preprint, journal `arXiv` | `none` |
| `9999.99999` | - | `FAILED no record found (arxiv: no record)` | |
| `not-an-identifier` | - | `FAILED unrecognised identifier`, no request made | |

## What gets fetched and stored, by licence level

Metadata and the abstract are always stored. Full text follows `litradar/policy.py`
(`fulltext_storage_level`), the same rule the weekly run applies:

| Level | When | What `add` does |
|---|---|---|
| `full` | Europe PMC record in the open-access subset (`isOpenAccess Y`) or carrying a CC / CC0 licence; preprint licence `cc_by`, `cc_by_nc`, `cc_by_nd`, `cc_by_nc_nd`, `cc0` | downloads the JATS XML (Europe PMC `fullTextXML`, or the preprint `jatsxml` URL), stores Methods, funding, competing-interest, acknowledgement and data-availability sections plus the body for full-text search, and annotates all of them |
| `facts_only` | Europe PMC has XML but the item is not in the open-access subset (author manuscripts); preprint licence `cc_no` or unknown | reads the text once for gazetteer mentions and tracked authors, stores those mentions with short snippets, and stores **no** section or body (`fulltext_available = 0`); author e-mails read from the text are dropped |
| `none` | no PMC id and no preprint XML (paywalled journal article, arXiv paper), `--no-fulltext`, or the source's `fulltext` switch is off in `radar.yaml` | metadata and abstract only; mentions come from the title and abstract (and from funders / affiliations the indexes supply) |

Switches honoured from `config/radar.yaml`: `sources.europepmc.fulltext` and
`sources.biorxiv.fulltext`. The bioRxiv and medRxiv web hosts rate-limit scripted clients (HTTP
429; the client closes a host after two in a row for the rest of the command), and a version posted
in the last few days may have no JATS yet, so a preprint download can fail even when the licence
allows it; the summary says so, and re-adding later retries it. Re-adding a
paper with `--no-fulltext` keeps the text an earlier run or add already stored (and the summary
reports `kept from an earlier run`); re-adding with full text on re-fetches it (the HTTP cache
makes that free). Note that `litradar rematch` recomputes mentions from **stored** text, so
`facts_only` mentions survive a refresh only when the add re-reads the XML.

The stored level is recorded in `extra_json.fulltext_level`, next to `tags`, `added_at` and
`added_ids` (every identifier string that has been used to add or refresh the paper).

## Reading the summary

```
NEW     doi:10.1038/s41592-026-0001-1
        Spatial profiling of tumours with a multiplexed assay
        Nature methods · 2026-10-11 · article · via europepmc:MED:40000001, crossref
        instruments  NovaSeq X [methods], Xenium [abstract/methods], Orbitrap Astral [methods]
        companies    10x Genomics [coi/affiliations], Illumina [coi], Twist Bioscience [ack]
        score        6.43  journal tier A; 3 platform(s); 3 company mention(s); company-affiliated author; watchlist: single-cell, spatial transcriptomics
        full text    full: Europe PMC PMC9000001
        tags         reading-list
        https://doi.org/10.1038/s41592-026-0001-1
```

`via` lists the lookups that answered, in resolution order (`biorxiv v2` names the version kept);
a `not found at …` line lists the ones that did not. `published as doi:…` appears when a preprint
record names its journal version; add that DOI too if you want the article. `--json` returns the
same fields (`instruments` and `companies` as lists of `{id, name, vendor, ticker, sections,
count}`, `tracked_authors` as `{name, subgroup, matched_by}`) for scripts.

## Adding a reading list from a file

```
# reading-list.txt: one identifier per line, '#' starts a comment
10.1038/s41592-024-02201-0          # scGPT, Nature Methods
https://www.biorxiv.org/content/10.1101/2023.04.30.538439v2
PMID:38391750
arXiv:2301.08370
```

```bash
litradar add --file reading-list.txt --tag reading-list --tag 2026-q4
litradar tags reading-list
```

Every line is resolved independently; a line that fails is reported and the rest are stored, and
the exit code is `1` so a script can notice. Re-running the same file is safe (refresh, not
duplicate). Lines are run in order and arXiv requests are paced at one per 3 seconds, so a long
list of arXiv ids takes a while; everything else is sub-second per lookup **when the services
answer**. When one does not, the HTTP client retries with exponential backoff (1, 2, 4, 8 s on 503
and 429, honouring `Retry-After`) and closes a host after two consecutive 429s for the rest of the
command, so a batch that meets a throttled arXiv or bioRxiv can spend a minute or two waiting with
only the retry log lines to show for it; run with `-v` to watch the requests, and expect the summary
for every identifier only at the end.

## Finding hand-added papers again

Tags live in `extra_json.tags`; `litradar search` indexes title, abstract, Methods, funding,
competing interests, affiliations and body, **not** `extra_json`, so searching for a tag will not
find the paper. Use:

```bash
litradar tags                         # every tag with its paper count
litradar tags reading-list            # papers carrying one tag (case-insensitive)
litradar tags reading-list --json     # for scripts: id, doi, title, journal, pub_date, url, tags, added_at
```

Hand-added papers are otherwise ordinary: `litradar search "Xenium" --since 365d` finds them by
content, `litradar entity txg-xenium` by instrument, and the digest for their publication week lists
them. For ad-hoc SQL, every hand-added paper has `added_at` in `extra_json`:

```sql
SELECT id, title FROM papers WHERE json_extract(extra_json, '$.added_at') IS NOT NULL;
```
