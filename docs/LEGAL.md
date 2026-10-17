# Legal and data policy

**Last reviewed: 2026-10-17.** Terms of service change; re-check the linked pages before relying on a
row of the table below. This document is a plain-language statement of what the tool does with
other people's content and why. It is not legal advice.

## 1. The rule in one paragraph

The radar is designed to be lawful for a **commercial operator with no institutional library or
text-and-data-mining (TDM) licence**, running from a laptop or GitHub Actions with its own API keys.
It stores bibliographic **metadata and abstracts** from every source it queries, stores **full text
only where the record's own licence allows it**, keeps **derived facts** (which instruments and
companies a paper names) for text it was allowed to read, and **links** to everything else. It never
logs in anywhere, never fetches paywalled text, never touches Google Scholar, and only uses
documented machine routes (APIs and RSS feeds that serve scripts), paced at or below each source's
published limit.

## 2. What is stored, and where

| Data | Stored? | Where it ends up |
|---|---|---|
| Bibliographic metadata (title, authors, affiliations, venue, DOI, dates, identifiers, licence string, open-access flag) | always | `data/radar.sqlite`; titles and links in `digests/*.md` |
| Abstracts | from sources whose terms allow it: bioRxiv/medRxiv API, Crossref, OpenAlex (reconstructed from the inverted index), Europe PMC, PubMed, arXiv, journal RSS | `data/radar.sqlite`; excerpts of at most 420 characters in `digests/*.md` |
| Full text (Methods, funding, competing-interest sections, body) | only for open-licensed items: the PMC / Europe PMC open-access subset and preprints under CC0 or a CC BY licence (see section 6) | `data/radar.sqlite` (sections and a full-text search column); raw XML in `.cache/` |
| Derived facts: gazetteer mentions with short snippets, scores, reasons | for every item whose text was read | `data/radar.sqlite`; entity names and counts in `digests/*.md` |
| Archive and registry records (SRA/ENA experiments with the declared instrument model, GEO series, PRIDE projects, CELLxGENE datasets, 510(k)/PMA decisions, trials, grants) | always; these are public-domain or unrestricted metadata | `data/radar.sqlite`; counts in `digests/*.md` |
| Headlines and links from RSS / SEC EDGAR | always; headline, link, date and the matched company only | `data/radar.sqlite`; grouped headlines in `digests/*.md` |
| Credentials | never | `.env` or GitHub Actions secrets only (see `SECURITY.md`) |

Three places hold content, with different exposure:

* **`digests/*.md`** are committed to the repository and are as public as the repository. They
  contain titles, DOI links, detected entity names, counts and abstract excerpts (420 characters at
  most), never full text or snippets. Every digest ends with a footer stating this policy.
* **`data/radar.sqlite`** is git-ignored. The weekly workflow publishes a zstd-compressed snapshot of
  it as a GitHub release asset (`db-latest`, `db-YYYY-MM`), which is as public as the repository. It
  holds abstracts and the full text of open-licensed items.
* **`.cache/`** holds the raw HTTP bodies of full-text fetches. It is git-ignored, pruned to 14 days
  in CI, and lives only on your machine or the runner.

What leaves the machine: the digest (e-mail, Slack, commit), the database snapshot, and, with
`--llm`, the **title and abstract** of each shortlisted paper sent to the LLM provider. Full text is
never sent to the LLM and never republished.

## 3. What is never done

* **No logins.** The pipeline holds no publisher credentials, replays no cookies, and runs no
  browser. A personal subscription to a journal is for reading the items the digest links to;
  publisher website terms allow access only through a normal web browser, forbid sharing login
  details, and forbid copying or storing content to populate a database. The `.env.example` has no
  slot for such a login on purpose.
* **No paywalled full text.** Items the open corpora do not carry are link-only. Methods detection
  for them falls back to the PMC author manuscript, the preprint version, archive metadata
  (SRA/GEO instrument model), or the abstract, in that order.
* **No HTML scraping of publisher sites**, and no adapters for sites whose `robots.txt` or
  `/.well-known/tdmrep.json` reserve rights. Only documented API endpoints and RSS feeds are used,
  so crawler directives are respected by construction; a feed that answers 403 is dropped, not
  worked around.
* **No Google Scholar**, ever: its `robots.txt` disallows automated queries and Google's terms
  forbid them.
* **Google News RSS only as an opt-in.** `news.google.com/robots.txt` disallows the `/rss/` path to
  all agents and Google's terms forbid automated access that contravenes machine-readable
  instructions. The project therefore treats the source as something the operator switches on
  knowingly (see section 7 for the current switch).
* **No re-serving.** Stored text is for the operator's own search. Nothing in the database is
  mirrored or served to others as text; preprint and PMC content is linked back to its host.

## 4. Per-source terms, rate limits and licence notes

Verified against the sources' own pages on the review date. "Pacing" is the interval the shared
HTTP client (`litradar/http.py`, `DEFAULT_MIN_INTERVAL`) enforces per host; it is at or below every
documented limit. Where a source publishes no number, the pacing is a courtesy default.

| Source | What is taken | Licence / terms note | Documented limit | Pacing |
|---|---|---|---|---|
| Europe PMC REST (`www.ebi.ac.uk`) | new articles by ISSN; section-level search; open-access full text as JATS XML | EMBL-EBI Terms of Use: no extra restrictions beyond the data owners'; open-access articles each carry their own CC (or similar) licence, "not identical for all articles", so the per-record `license` and `isOpenAccess` fields are stored | none published; "do not impede the service for others" | 0.2 s (5/s) |
| Crossref REST | per-journal DOI feeds; abstracts where deposited | metadata is facts, usable for any purpose; "some abstracts may be subject to copyright by publishers or authors"; identify with `mailto` for the polite pool | advertised in `X-Rate-Limit-*` headers | 0.2 s |
| OpenAlex | topic feeds, company-typed institutions, tracked authors, ISSN backstop | CC0 1.0; abstracts shipped as an inverted index "for legal reasons" | 10 req/s anonymous, 30 with a free key; a small per-IP daily budget without a key, a per-account daily budget with one (a free key is effectively required) | 0.12 s |
| NCBI E-utilities: SRA, GEO, PubMed | weekly census of experiments/series with the declared instrument model; PubMed metadata | NLM/NCBI terms: send `tool` and `email`; NCBI "places no restrictions on the use or distribution" of GEO/SRA data; PubMed abstracts "may be protected by copyright" | 3 req/s, 10 with an API key | 0.34 s, 0.11 s with a key |
| ENA portal API (`www.ebi.ac.uk`) | ERX/DRX read experiments with `instrument_model`, `first_public` | EMBL-EBI Terms of Use | none published | 0.2 s |
| bioRxiv / medRxiv API | all new preprint versions: metadata, abstract, licence, `jatsxml` URL | authors retain copyright and choose cc_by, cc_by_nc, cc_by_nd, cc_by_nc_nd, cc0 or "no reuse"; TDM via the API and bucket is permitted; tools "must link back" and must not re-host; `robots.txt` blocks generic scripts on the HTML site | none published | 0.5 s (API), 1.0 s (`www.`) |
| arXiv API (`export.arxiv.org`) | q-bio metadata and abstracts | metadata CC0 1.0; full text under the submitter's licence (not fetched) | one request every 3 s | 3.0 s |
| Semantic Scholar | citation enrichment (optional) | API Licence Agreement; datasets ODC-BY 1.0 with attribution; keys shareable only within the licensee's organisation | 1 req/s with a key | 1.0 s |
| SEC EDGAR Atom | 8-K filings per watched US-listed company | US Government work, public domain; fair-access policy requires a declared User-Agent with a contact and allows at most 10 req/s; the feeds are skipped when `LITRADAR_CONTACT_EMAIL` is empty | 10 req/s | sequential, with contact |
| openFDA | 510(k) clearances, De Novo grants, PMA decisions | public domain (CC0) with the FDA's "unvalidated data" disclaimer; not for clinical or regulatory decisions | 240/min; 1,000/day per IP or 120,000/day with a free key | 0.25 s |
| ClinicalTrials.gov API v2 | studies sponsored by or using watched companies/devices | US Government work, public domain | about 50 req/min per IP | 1.25 s |
| NIH RePORTER | shared-instrumentation, SBIR/STTR and keyword awards | US Government work, public domain | one request per second | 1.0 s |
| PRIDE Archive | projects by day with instrument names | EMBL-EBI Terms of Use | none published | shares `www.ebi.ac.uk` pacing |
| CZ CELLxGENE Discover | public dataset index with assay terms (one request per run) | curation API; each collection carries its own licence | none published | 1.0 s |
| Journal / publisher / press RSS | headlines, DOIs, links | publisher terms; feeds that refuse scripts are dropped | none | sequential |
| Google News RSS | headlines per watched company | Google Terms of Service; `robots.txt` disallows `/rss/`; **opt-in only** | none | sequential |

Not used, by policy: Google Scholar (prohibited), publisher HTML pages (terms and `tdmrep`
reservations), Altmetric without a commercial contract (its free tier is for non-commercial academic
projects), any paid aggregator.

## 5. Commercial-use caveats

* **A fund, a company or an individual investor is a commercial user.** Creative Commons defines
  NonCommercial as use "not primarily intended for or directed towards commercial advantage or
  monetary compensation"; the test is the purpose of the use, not the kind of organisation. An
  investment process fails it. Content under **CC BY-NC, BY-NC-SA or BY-NC-ND** is therefore
  treated as *facts-only* by default: the tool may read it to extract mentions but stores no text.
  Academic forks may relax this (section 7).
* **The UK TDM exception (CDPA s.29A) is non-commercial only**, requires lawful access, and does not
  allow copies to be transferred or used for another purpose. It does not help a commercial operator.
* **EU Directive 2019/790:** Article 3 (research organisations) is unavailable to a commercial
  operator; Article 4 allows TDM of lawfully accessible works by anyone, **unless the rightholder has
  reserved rights in a machine-readable way** (Article 4(3)), and publisher terms may contract out of
  it (Article 7). Major publisher sites publish such reservations (`/.well-known/tdmrep.json` with
  `tdm-reservation: 1`). That is one more reason the tool never fetches publisher HTML.
* **United States:** building a search index over lawfully accessed text has strong fair-use
  support, even for a commercial operator, but unlawful access (a scraper behind a login, ignoring
  terms) weighs against it and breaches contract. The contract layer, not copyright, is the binding
  constraint; the design stays on the right side of the contracts so the question never arises.
* **Publisher APIs with their own terms** (for example a metadata API whose non-open-access abstracts
  are restricted to personal, non-commercial use) are not used for abstracts; the same abstracts are
  taken from Crossref, PubMed or OpenAlex, whose terms allow it.
* **Abstracts** from Crossref and PubMed "may be subject to copyright". Storing them for the
  operator's own indexing is low-risk and common practice; republishing them in bulk is not. The
  digest keeps to 420-character excerpts and never ships the database as text.

## 6. How the code enforces it

The policy is split into a decision module, an allow-list and a transport layer, so that each piece
can be tested offline (`tests/test_policy.py`, `tests/test_http_post_json.py`).

**Storage levels (`litradar/policy.py`).** `fulltext_storage_level(license, open_access,
text_available)` returns one of three levels and is a pure function of the record:

| Level | Meaning | When |
|---|---|---|
| `full` | store sections and body | licence starts with a permissive prefix (CC0, public domain, any `cc_by` variant including ND and NC-ND as stored by bioRxiv), or the record is in the open-access subset (`isOpenAccess == 'Y'`) |
| `facts_only` | parse the text for mentions and short snippets, store no sections or body | restrictive tokens (`cc_no`, "all rights reserved", "no reuse", "copyright", "proprietary"), author manuscripts, unknown licence with readable text |
| `none` | do not fetch | no text available from the source (paywalled, or the source gave no text) |

`normalize_license()` maps Creative Commons URLs and SPDX-style strings to the same tokens so a
Crossref `license.URL`, a bioRxiv `cc_by_nc` and a Europe PMC `cc by-nc` are judged alike.

**Licence allow-list.** `PERMISSIVE_PREFIXES` and `RESTRICTIVE_TOKENS` in `litradar/policy.py` are
the single place to change what counts as storable; an academic fork with a TDM agreement extends
the permissive list, a stricter operator removes the NC variants. Each stored paper keeps its
`license` string so the decision can be audited later from the database alone.

**Transport (`litradar/http.py`).** One shared client for every adapter: an honest `User-Agent`
with `mailto:` when a contact is configured; per-host minimum intervals (`DEFAULT_MIN_INTERVAL`,
tightened for NCBI when a key is present); exponential backoff capped at 30 s honouring
`Retry-After`; no retry on 400/401/403/404/410; a host that answers 429 twice in a row is skipped for
the rest of the run; nothing in the client ever raises into an adapter. Adapters only call documented
endpoints, so `robots.txt` crawler rules are satisfied by never crawling. The client does not parse
`robots.txt` itself; the policy that no HTML page is fetched is what keeps it compliant, and
`CONTRIBUTING.md` makes that a review rule.

**Digest (`litradar/digest/render.py`).** Abstract excerpts are cut at 420 characters; snippets of
full text are not rendered; the footer states the storage policy in every digest.

**Enforcement status on the review date.** Honesty matters more than tidiness here:

| Rule | Status |
|---|---|
| Storage levels and the licence allow-list | implemented and unit-tested in `litradar/policy.py`; the adapters' call into it is being wired (check `litradar/sources/biorxiv.py` and `litradar/sources/europepmc.py` for `fulltext_storage_level` in your checkout; until it is there, the Europe PMC server itself refuses non-open-access full text, and the bioRxiv web host rate-limits scripted full-text fetches) |
| Pacing, backoff, host blocking, honest User-Agent | implemented in `litradar/http.py` |
| SEC EDGAR skipped without a contact | implemented in `litradar/pipeline.py` |
| No logins, no publisher HTML, no Google Scholar | no code path exists; review rule in `CONTRIBUTING.md` |
| Google News opt-in | the query is attached to every company with `watch_news: true` in `config/companies.yaml`; a dedicated switch is being added. To stay off it today, set `watch_news: false` or point `LITRADAR_CONFIG_DIR` at an overlay without it |
| Title/abstract-only payload to the LLM | implemented in `litradar/rank/llm.py` |

## 7. Changing the policy for your copy

* **Academic or licensed operators:** extend `PERMISSIVE_PREFIXES` in `litradar/policy.py` (for
  example to admit `cc_by_nc` text in full) and add adapters for publisher TDM APIs you hold a licence
  for. You are then responsible for that agreement's storage, retention, snippet and deletion terms.
* **Stricter operators:** remove the ND/NC variants from the permissive list, set
  `sources.<name>.fulltext: false` in `config/radar.yaml`, or run with `--no-fulltext`.
* **Keeping the watchlist private:** keep the shipped `config/` generic, put personal lists in an
  overlay directory and point `LITRADAR_CONFIG_DIR` at it; in a public fork leave the repository
  variable `LITRADAR_COMMIT_DIGESTS` unset so digests are not committed (see `docs/deploy.md`).

## 8. Removing an item

There is no purge command yet. To remove a paper and everything derived from it: delete its row
from `papers` and the matching rows from `papers_fts`, `mentions` and `scores` (keyed by the paper
id), delete the cached XML under `.cache/` if present, and re-render the digest with
`litradar digest`. Then let the weekly workflow publish a fresh snapshot, or do it by hand with
`gh release upload db-latest radar.sqlite.zst --clobber`.

## 9. What this document does not cover

* The lawfulness of **your** downstream use of the digest or the database (for example sharing
  tables that include abstracts).
* **Personal data.** Author names, affiliations and corresponding-author e-mail addresses are taken
  from the papers themselves and stored as published; if you operate under GDPR or similar rules,
  decide whether you need to drop the e-mail field.
* **Export control, insider-trading or investment-compliance rules** that may apply to how you act
  on what the digest tells you.
* **Terms that changed after the review date.** Several source pages block automated readers and
  were read from captures or developer pages; the uncertainties are flagged in the per-source notes
  under `docs/sources/`.
* **Legal advice.** Have counsel read sections 3 to 6 before publishing or relying on a fork.
