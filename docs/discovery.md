# Company discovery: finding companies the gazetteer does not know yet

`config/companies.yaml` only finds companies somebody already listed. `litradar.discovery` reads what the
store already holds and surfaces names that *behave* like companies but are not in the gazetteer, with the
evidence attached, so the watchlist grows from the literature instead of from memory.

```
litradar candidates                       # review: score, signals, one evidence snippet each
litradar candidates refresh --since 90d   # recompute from the database (0.5 s for ~3,500 papers)
litradar candidates show <name>           # every piece of evidence for one candidate
litradar candidates ignore <name>         # not a company / not interesting
litradar candidates promote <name> [--ticker XYZ] [--type tools]
```

Everything lives in one extra table, `candidate_companies`, created on first use with
`CREATE TABLE IF NOT EXISTS`; no existing table or config file is modified.

## Signals

Each signal produces `Evidence(name, evidence_type, snippet, ref)` where `ref` is a paper id or a news item id.

| Type | Where it comes from | Accepted when |
|---|---|---|
| `email_domain` | author e-mail addresses (`extra_json.emails`, `authors_json[].email`) | the domain is not public mail (gmail, outlook, yahoo, 163, qq, 126, sina, protonmail, icloud, ...), not a publisher, not `.edu` / `.ac.*` / `.gov` / `.org` / `.mil` / `.int` / `edu.*` / `gov.*`, has no academic token in its labels (`univ`, `hospital`, `clinic`, `inserm`, `cnrs`, `institut`, `research`, ...; `smithlab.com`-style vanity domains count as academic) and **either** has a generic commercial TLD (`.com`, `.io`, `.bio`, `.ai`, `.co.uk`, `.com.cn`, ...) **or**, for a plain country-code domain (`.de`, `.ch`, `.cn`, ...), the brand token appears in an affiliation that carries a legal form (GmbH, B.V., Ltd, ...). The display name is the affiliation segment that carries the domain's brand token (`lactiga.com` + "Lactiga US Inc., North Brunswick" -> "Lactiga US Inc"), else the domain itself. |
| `coi`, `funding`, `ack` | `sections_json` competing-interest, funding and acknowledgement text | a relationship trigger is followed by a capitalised name: *employee(s) of / employed by / (co-)founder of / consultant for|to / holds equity|stock|shares in / shareholder of / advisory board of|for / funded by / supported by / grant from / sponsored by / collaboration with / funding from / received support from X*. A corporate suffix on X (Inc, Ltd, LLC, GmbH, AG, SA, Co., Corp, Biosciences, Biotechnologies, Therapeutics, Pharmaceuticals, Diagnostics, Genomics, Bio, Labs, Technologies, ...) makes the hit **confident**; a bare 1-3 word name is kept at **low confidence**. Lists ("..., Beta Bio and Gamma Labs LLC") continue only through suffixed names, so author initials never leak. |
| `openalex_company` | `mentions` rows whose `entity_id` starts with `unlisted:` (OpenAlex company-typed institutions the pipeline could not match) | always, unless generic or gazetteer-known |
| `news:nih-reporter` | `news_items` from the `nih-reporter` feed; the organisation is parsed from the ` \| `-joined meta line the adapter writes | the activity code is SBIR/STTR (R41-R44, U43/U44, SB1) or the organisation carries a corporate suffix |
| `news:clinicaltrials` | `Sponsor: X (Industry). Collaborators: A; B.` in the `clinicaltrials` summaries | sponsor class is Industry, or the name carries a corporate suffix (collaborators: suffix only) |
| `news:fda` | `fda-510k`, `fda-denovo`, `fda-pma` items; applicant is the part after ` — ` in the title | always, unless generic or gazetteer-known |
| `news:press` | every other feed (trade press, Google News queries, press wires) | the headline reads "*Name* raises / closes / secures / lands / launches / unveils / acquires / to acquire / emerges from stealth / Series A-D / seed round ..." and a sector word (sequencing, genomics, proteomics, diagnostics, spatial, single-cell, biotech, oncology, therapeutics, ...) appears in the headline or summary. Leading question words and possessives are stripped ("Why OMass' $310M series C ..." -> OMass; "France's ABL Texcell Acquires ... IntegraGen" -> ABL Texcell, IntegraGen). |

Rejected everywhere: names matching the existing gazetteer (`Gazetteer.find(name, ignore_context=True)`, e-mail
domains via `match_domains`) and names containing an institution word (University, Institute, Hospital,
Foundation, Council, Ministry, Society, Trust, Fund, Program, Department, College, School, Academy, Agency,
Association, National, Centre/Center, Laboratory, Health, System, Project, Plan, ... plus non-English forms such
as Universität, Fondation, Stiftung, Forschungsgemeinschaft) or a funder acronym (NIH, NSF, DFG, ERC, MRC,
NSFC, CAS, ...). A low-confidence name followed by a lower-case *government / ministry / grant / foundation /
research ...* is treated as an adjective ("the Korean government") and dropped.

## Grouping and scoring

Variants are grouped on a normalised key: accents folded, lower-case, legal forms removed (Inc, Ltd, LLC,
GmbH, Co., Corp, plc, B.V., ApS, ...), "and/the/of" and punctuation removed, no spaces. Sector words are kept,
so "Acme Therapeutics" and "Acme Diagnostics" stay separate while "Costello Medical" and the e-mail brand
`costellomedical.com` merge. A bare e-mail brand that is a prefix of exactly one longer key is folded into it.

```
score = 0.5 x distinct papers/items (capped at 8)
      + 1.0 x (distinct signal families - 1)      # coi / funding / ack count as one family

      + 1.0 if last seen within 30 days, 0.5 within 90 days
      + 1.0 if any variant carries a corporate suffix
      + 0.5 if a confident high-precision signal is present
            (email_domain, openalex_company, news:nih-reporter, news:clinicaltrials, news:fda)
      - 0.5 if no evidence is confident
```

A single confident COI hit this month scores 2.5 and shows under the default `--min-score 2`; a single bare
name ("employed by Meta") scores 1.0 and needs `--min-score 1`. Two papers plus an e-mail domain score 4.0.

## Status workflow

`new` -> `ignored` (`litradar candidates ignore <name>`) or `promoted` (`litradar candidates promote <name>
[--ticker T] [--type tools|diagnostics|synthesis|ai_bio|pharma|cro|institute]`). `reset` puts a row back to
`new`. `refresh` recomputes evidence and scores but never changes a status; a `new` row whose evidence fell
inside the refreshed window and did not come back (an earlier rule version, a re-annotated paper) is removed,
while ignored and promoted rows are decisions and always survive. `<name>` may be the display name, the
normalised key or any unique substring.

`promote` builds a gazetteer entry (`id` slug, display name, ticker or `private`, type, `watch_news: false`,
the e-mail domains seen, the other spellings as aliases) and hands it to `litradar.track.add_company`, which
appends one flow-style line to `config/companies.yaml` (the directory `LITRADAR_CONFIG_DIR` points at, else
the repository's `config/`), backs the file up first and validates the result. The status flips to `promoted`
**only when that write succeeded**; a refused write (an id that already exists, a name or alias another entry
claims, an unknown `--type`) is reported with the reason, the command exits non-zero and the candidate stays
`new`. Fix the conflict (`--ticker`, `--type`, or `litradar untrack` the other entry) and retry, or paste the
printed line under `entries:` by hand. After a promotion, `litradar rematch` picks the company up in the
stored papers:

```
- {id: acme-spatial-inc, name: Acme Spatial Inc, ticker: private, type: tools, watch_news: false, domains: [acmespatial.com], aliases: [Acme Spatial]}
```

### Ticker hint

`--hints` (listing) and `promote` / `show` (always, best effort) fuzzy-match the candidate against the SEC's
`company_tickers.json` (about 10,000 registrants, one download cached for 30 days under `.cache/`). It is a
hint only: exact normalised match scores 1.0, prefix match 0.9, close edit distance 0.88+. Verify before use;
most candidates are private and get no hint.

## Digest section

`render_candidates_markdown(store, since, until, top=15)` returns a Markdown block headed
**"Companies spotted (not yet tracked)"** with a table of the top new candidates (score, signals, papers/items,
last seen, one evidence snippet with a source link) and the commands to act on them. It reads only the
candidate table, so `refresh` must have run for the window.

Wiring (two one-line additions in files this module does not own):

* `litradar/pipeline.py`, end of `Pipeline.run()` after extraction and scoring are committed:
  `from . import discovery; discovery.refresh(self.store, self.companies, since, until)`
* `litradar/digest/render.py`, in `render()` after the "Companies in the literature" block:
  `md.append(discovery.render_candidates_markdown(store, since, until, top=int(dcfg.get("candidates_n", 15))))`
  and, for the JSON summary, `summary["candidates"] = discovery.list_candidates(store, since=since, min_score=2.0, limit=15)`.
  Optional knob in `config/radar.yaml`: `digest.candidates_n`.

Until that is wired, `litradar candidates refresh` after each `litradar run` keeps the table current.

## Precision, measured

On a one-week store (3,562 papers, 504 news items; 413 papers with a competing-interest statement, 824 with a
funding statement, 423 with author e-mails) a refresh takes about half a second and yields 141 pieces of
evidence grouped into 115 candidates, 77 of them at the default `--min-score 2`. Read by hand:

* top 20 by score: 20 of 20 are companies (biotech and pharma, a diagnostics start-up, an instrument maker, a
  corporate research arm, a medical-communications agency, a food group's research entity); 18 of 20 are in the
  life-science sector.
* the 77 rows at the default threshold: 76 are companies (the one miss is a hospital with a vanity `.com`
  e-mail domain); roughly a fifth are off-sector companies that happen to employ authors (tobacco, aquaculture,
  seed, engineering, quantum computing), which is what a competing-interest statement actually records.
* below the threshold (score 1-1.5, hidden by default): single bare names, roughly two thirds real companies
  ("employed by Meta", a headline subject with no sector tail) and one third funders, people or products that
  no rule caught.

Earlier iterations on the same data surfaced a provincial science-and-technology programme cut at the word
"Technology", author initials picked up as list items, adjectives before "government", a government research
foundation accepted on a bare country-code e-mail match, duplicated funding/acknowledgement text counted as
two signals, and grant codes; each now has a rule and a regression test. Known blind spots: a company named
only by a bare acronym ("employed by IBM") is dropped as initials unless a second word follows; a headline whose
subject is a product or a possessive ("X's Y gets approval") yields the product; names in languages other than
English are only caught through legal forms (GmbH, S.A., Co., Ltd., ApS, B.V.).

## Tests

`tests/test_discovery.py` covers the statement patterns on `tests/fixtures/discovery_coi_positive.txt` (12
paragraphs, 15 companies) and `tests/fixtures/discovery_coi_traps.txt` (11 paragraphs that must yield nothing:
funders, universities, a known gazetteer company, "the company"), domain classification, affiliation-derived
display names, the four news parsers, scoring, grouping, status transitions on a temporary database, the
digest block, the YAML line and the SEC hint with canned data. Nothing in the suite touches the network.
