# Cookbook: fifteen questions, copy-paste answers

Every recipe has a shell form (`litradar fetch ...`, JSON lines out) and a Python form (the source
clients in `litradar.sources` plus the shared `litradar.http.Http` client). Nothing here needs the
weekly pipeline or the database except recipe 10, which is about the database.

Every recipe was run live on **2026-10-17** (window 2026-10-10..2026-10-17 unless stated) and the
output shown is that run, trimmed. Where a key was needed and not available, the recipe says so.

```bash
pip install -e .                                   # or: pip install git+https://github.com/<you>/lit-radar
export LITRADAR_CONTACT_EMAIL=you@example.com      # polite User-Agent / mailto; strongly recommended, not required
litradar fetch --list                              # every source, its options, access requirements and pacing
```

Output is one JSON object per line, so the shell recipes end with a three-line Python filter instead
of assuming `jq`. Swap in `jq` if you have it. Logs go to stderr, records to stdout or `--out`.

---

## 1. All preprints this week in genomics

```bash
litradar fetch biorxiv --days 7 --categories genomics --out preprints.jsonl
# medRxiv has its own category names:
litradar fetch medrxiv --days 7 --categories "genetic and genomic medicine,oncology" --out med.jsonl
```

```python
from litradar.http import Http
from litradar.sources import biorxiv

http = Http(contact_email="you@example.com")
papers = list(biorxiv.fetch(http, "2026-10-10", "2026-10-17", categories=["genomics"]))
for p in papers[:3]:
    print(p.pub_date, p.title[:80], p.url, p.license)
```

The details API lists every *version* posted in the window, 100 per page; the adapter keeps the latest
version per DOI and filters categories client-side, so a week costs ~60 requests whatever you filter on.
Each record carries `fulltext_url` (JATS XML on `www.biorxiv.org`, which blocks scripts; see
`docs/browser.md`) and `published_doi` once the preprint appears in a journal.

Live check: **87 preprints**, 61 requests.
```
2026-10-10 A chromosome-level genome assembly of the chilli thrips ...   https://www.biorxiv.org/content/10.64898/2026.10.08.757792v1  cc_by
2026-10-10 Single-cell exploration of ovarian aging across mammalian models  https://www.biorxiv.org/content/10.1101/2025.10.15.682639v2  cc_by_nc_nd
2026-10-10 Expanding the DNA Motif Lexicon of the Transcriptional Regulatory Code  https://www.biorxiv.org/content/10.1101/2025.07.09.662874v2  cc_by_nc
```

## 2. Open-access papers whose Methods name NovaSeq X

```bash
litradar fetch europepmc --section METHODS --terms "NovaSeq X" --days 7 --fulltext --out novaseq.jsonl
python3 -c '
import json
for r in map(json.loads, open("novaseq.jsonl")):
    m = r["sections"].get("methods", ""); i = m.lower().find("novaseq x")
    print(r["doi"] or r["id"], "|", r["journal"][:30], "|", " ".join(m[max(0, i-60):i+60].split()))'
```

```python
from litradar.http import Http
from litradar.sources import europepmc

http = Http(contact_email="you@example.com", cache_dir=".cache")
q = europepmc.section_query("METHODS", ["NovaSeq X"], "2026-10-10", "2026-10-17")   # open-access subset by default
print(europepmc.hit_count(http, q))
for rec in europepmc.search(http, q, max_records=50):
    paper = europepmc.to_paper(rec)
    europepmc.attach_fulltext(http, paper)       # one cached request per paper; fills paper.sections["methods"] etc.
    print(paper.doi, paper.journal, len(paper.sections.get("methods", "")))
```

Europe PMC answers the section query server-side (`METHODS:"NovaSeq X"`), so finding the papers costs
one request; only the full text you ask for is downloaded. Phrase search is prefix-inclusive at the
token level: "NovaSeq X" also matches "NovaSeq X Plus". `examples/methods_search.py` prints the
sentence that names the term.

Live check: query `(METHODS:"NovaSeq X") AND (FIRST_PDATE:[2026-10-10 TO 2026-10-17]) AND (OPEN_ACCESS:y)`, **14 hits**.
```
europepmc:PMC:PMC13656642 | Mitochondrial DNA. Part B | ... Whole-genome sequencing was performed on the Illumina NovaSeq X Plus (PE150) ...
europepmc:PMC:PMC13665941 | Science advances          | ... Nextera library preparation and sequencing were performed on the NovaSeq X Plus. ...
europepmc:PMC:PMC13661941 | Frontiers in immunology   | ... paired-end sequencing on the Illumina NovaSeq X Plus platform. ...
```

## 3. Papers with a company-affiliated author

Two routes. OpenAlex types every institution (`company`, `education`, ...), which is the clean answer
but needs a free API key since anonymous traffic became metered per IP:

```bash
export OPENALEX_API_KEY=...        # https://help.openalex.org/api/authentication/
litradar fetch openalex --company-authors --query "spatial transcriptomics" --days 30 --out company.jsonl
python3 -c '
import json
for r in map(json.loads, open("company.jsonl")): print(r["pub_date"], r["extra"]["company_institutions"], r["title"][:70])'
```

```python
from litradar.sources import openalex
flt = {"from_publication_date": "2026-09-17", "to_publication_date": "2026-10-17",
       "authorships.institutions.type": "company", "title_and_abstract.search": "spatial transcriptomics"}
for w in openalex.works(http, flt, mailto="you@example.com", api_key=OPENALEX_API_KEY):
    p = openalex.to_paper(w)
    print(p.pub_date, p.extra["company_institutions"], p.title[:70])
```

Live check: **not run** (no key on the test machine). Without a key the adapter raises
`OpenAlexBudgetExhausted: Insufficient budget ...`, which `litradar fetch` reports as one line and exit 1.

Without a key, Europe PMC's `AFF:` field with the company names from `config/companies.yaml` is the
fallback (substring match on affiliation strings, all access levels):

```python
from litradar.config import DEFAULT_CONFIG_DIR, load_yaml
names = [e["name"] for e in load_yaml(DEFAULT_CONFIG_DIR / "companies.yaml")["entries"] if e.get("type") == "tools"]
q = europepmc.section_query("AFF", names[:40], "2026-10-10", "2026-10-17", open_access_only=False)
for rec in europepmc.search(http, q):
    p = europepmc.to_paper(rec)
    hit = [n for n in names if any(n.lower() in a.lower() for a in p.affiliations)]
    print(p.pub_date, hit, p.title[:70])
```

Live check: 40 names, **5 papers**.
```
2026-10-12 ['BGI Genomics']       Phenotypic and Genotypic Analysis of Two Rare Cases of --SEA/-a4.3 HbH Disease
2026-10-12 ['Waters Corporation'] Assessing the use of ASAP-MS for Screening a UK Pub for Illicit Drug Substances
2026-10-16 ['Bruker']             Next Generation NMR Flow Tube Optimized for Small Volume NMR Reaction Monitoring
```

## 4. SRA experiments per instrument model this week

Exact per-model counts come from SRA's Properties index, one request per model and no download:

```python
from litradar.http import Http
from litradar.sources import sra

http = Http(contact_email="you@example.com", ncbi_api_key=NCBI_API_KEY)   # key: 3 -> 10 requests/s
since, until = "2026-10-10", "2026-10-17"
total = sra.count(http, f"{since.replace('-', '/')}:{until.replace('-', '/')}[PDAT]")
models = ["Illumina NovaSeq X Plus", "Illumina NovaSeq X", "Illumina NovaSeq 6000", "NextSeq 2000", "Revio", "PromethION", "DNBSEQ-T7", "Element AVITI"]
for model, n in sorted(sra.counts_by_instrument(http, since, until, models).items(), key=lambda kv: -kv[1]):
    print(f"{model:28} {n:8,} {100*n/total:5.1f}%")
```

The model strings are SRA's own `instrument_model` vocabulary, matched as a whole ("Illumina NovaSeq X"
does not include "... X Plus"); `config/archive_models.yaml` lists them per gazetteer entity. For a
histogram over *every* model, sweep records instead (a full week is ~120k experiments, ~240 requests;
`--limit` samples):

```bash
litradar fetch sra --days 7 --limit 5000 --out sra.jsonl
python3 -c '
import json, collections
c = collections.Counter(json.loads(l)["instrument_model"] for l in open("sra.jsonl")); print(c.most_common(10))'
```

`examples/weekly_instrument_census.py` does both (SRA counts, optional ENA sweep).

Live check: **121,868 experiments** made public in the week.
```
Illumina NovaSeq 6000          64,140  52.6%
NextSeq 2000                    8,100   6.6%
Illumina NovaSeq X Plus         5,993   4.9%
Illumina NovaSeq X              4,962   4.1%
DNBSEQ-T7                       3,949   3.2%
Element AVITI                     921   0.8%
PromethION                        641   0.5%
Revio                             292   0.2%
```

## 5. Revio deposits per month since 2023

```bash
litradar timeseries --entity pacb-revio --from 2023-01 --sources sra,ena --no-chart --out ts/
# -> ts/timeseries_2023-01_2026-10.csv (long format) and .md (pivot); add --no-chart off for a PNG when matplotlib is installed
# any vendor at once:  litradar timeseries --vendor pacb --from 2023-01
```

Counts are cached per (source, entity, model, month) in the database, so re-running only fetches the
current month and the two before it (deposit lag). The Python API is `litradar.timeseries.run(...)`
or, for one number, `timeseries.sra_count(http, "revio", "2026-09-01", "2026-09-30")`.

Live check: 46 months, 92 requests, first non-zero month **2023-08** in both archives.
```
month    SRA    ENA           year  SRA     ENA
2026-02  1211    923          2023     224     224
2026-03  3175   2246          2024   3,326   3,226
2026-04  1091   1741          2025  10,921  10,520
2026-05  2729   2580          2026  19,550  18,633   (to 2026-10-17)
2026-06  1443   1647
2026-07  1149   1198          total 34,021  32,603
2026-08  4418   4444
2026-09  2081   1936
2026-10  1166    783
```

## 6. New 510(k) clearances for sequencing IVDs

```bash
# the datasets refresh monthly, so reach back at least 45 days
litradar fetch openfda --kinds 510k --ivd --since 2026-01-01 --out ivd.jsonl
python3 -c '
import json, re
rows = [json.loads(l) for l in open("ivd.jsonl")]
ngs = [r for r in rows if "product code PZM" in r["summary"] or re.search(r"sequenc|\bNGS\b", r["title"] + r["summary"], re.I)]
print(len(rows), "IVD decisions;", len(ngs), "sequencing-related")
for r in ngs: print(r["published"], r["feed"], r["title"])'
```

```python
from litradar.sources import openfda
for item in openfda.fetch(http, "2026-01-01", "2026-10-17", kinds=("510k",), advisory_committees=openfda.IVD_ADVISORY_COMMITTEES):
    if "product code PZM" in item.summary:        # PZM = next-generation-sequencing oncology panel
        print(item.published, item.title, item.url)
```

`--ivd` keeps the in-vitro-diagnostic advisory committees (CH, HE, IM, MI, PA, TX, MG); De Novo grants
ride along in the `fda-denovo` feed. Product code is the precise handle: PZM is the NGS oncology-panel
code; a free-text filter also catches PCR kits. An `OPENFDA_API_KEY` raises the daily quota from 1,000
to 120,000 requests.

Live check: last 45 days, 31 IVD decisions, **0 sequencing-based**; year to date, 182 decisions,
**1 PZM** (plus 3 keyword hits that are PCR or panel controls).
```
2026-05-12 fda-510k CellDx-Tissue — Datar Cancer Genetics Private Limited | PZM
```

## 7. Trials first posted this month mentioning ctDNA

```bash
litradar fetch clinicaltrials --terms ctDNA --since 2026-10-01 --out ctdna.jsonl
# sponsor / intervention fields instead of free text:
litradar fetch clinicaltrials --sponsor "Example Diagnostics" --intervention "liquid biopsy" --days 30
```

```python
from litradar.sources import clinicaltrials
for study in clinicaltrials.new_studies(http, "2026-10-01", "2026-10-17", terms=["ctDNA"]):
    print(study.published, study.id, study.title[:80])
    # study.summary: sponsor, collaborators, interventions, conditions, phase, enrollment, status
```

Live check: **13 studies** first posted 2026-10-01..17.
```
2026-10-01 ctgov:NCT07851363 Rapid Diagnosis and Treatment Response Monitoring of Primary CNS Lymphoma Using CSF ctDNA
2026-10-02 ctgov:NCT07854769 Blood Testing to Detect Cancer Recurrence in Patients With Early-Stage Non-Small Cell Lung ...
2026-10-14 ctgov:NCT07873086 MRD-Guided Aumolertinib-Based Adjuvant Therapy for Resected EGFR-Mutant NSCLC
2026-10-02 ctgov:NCT07856303 SHR-A1811 Versus Trastuzumab Plus Pertuzumab in ctDNA-Positive HER2-Positive Early Breast ...
2026-10-16 ctgov:NCT07878351 Prospective ctDNA Testing in Large B-Cell Lymphoma
```

## 8. S10 instrument grants this quarter

```bash
litradar fetch nih-reporter --mode s10 --since 2026-07-01 --until 2026-09-30 --out s10.jsonl
litradar fetch nih-reporter --mode s10 --terms "mass spectrometer,sequencer" --since 2026-07-01 --until 2026-09-30   # narrowed
```

```python
from litradar.sources import nih_reporter
# NewsItems (title, awardee, amount in the summary) ...
for award in nih_reporter.s10_awards(http, "2026-07-01", "2026-09-30"):
    print(award.published, award.title[:90])
# ... or the raw rows when you want the numbers as numbers
criteria = {"award_notice_date": nih_reporter.date_range("2026-07-01", "2026-09-30"), "activity_codes": ["S10"]}
rows = list(nih_reporter.search(http, criteria))
print(len(rows), sum(r.get("award_amount") or 0 for r in rows))
```

Live check: **82 S10 awards**, **$56,723,540** in total (one POST request).
```
2026-07-02 A next-generation mass spectrometry system for advancing quantitative proteome science ...
2026-07-02 Acquisition of a Highly Sensitive Time of Flight (ToF) Mass Spectrometry System ...
2026-07-03 AVIT24 System for Next-Generation Sequencing and Spatial MultiOmics ...
2026-07-06 Thermogravimetric Analyzer with Evolved Gas Analysis
```

## 9. Citation counts for a list of DOIs

```bash
litradar fetch semantic-scholar --doi 10.1038/s41592-024-02201-0,10.1038/s41586-020-2308-7 --out cites.jsonl
cat dois.txt | litradar fetch semantic-scholar --doi - --out cites.jsonl       # one DOI per line on stdin
```

```python
from litradar.sources import semantic_scholar
found = semantic_scholar.fetch_batch(http, dois, api_key=SEMANTIC_SCHOLAR_API_KEY)   # up to 500 DOIs per POST
for doi in dois:
    item = (found or {}).get(doi)
    print(doi, item and item["citation_count"], item and item["influential_citation_count"])
```

Each output line says which of three things happened: `found: true` with the counts, `found: false`
(Semantic Scholar does not know the DOI), or `found: null` with an `error` (the batch request itself
failed). Anonymous requests share one throttled pool; a free `SEMANTIC_SCHOLAR_API_KEY` is the fix.

Live check: first run answered, second run (minutes later) was throttled twice and reported it.
```
{"doi": "10.1038/s41592-024-02201-0", "found": true, "citation_count": 1462, "influential_citation_count": 133, "publication_date": "2024-02-26", ...}
{"doi": "10.9999/nope", "found": false}
{"doi": "10.1038/s41586-020-2308-7", "found": null, "error": "batch request failed (throttled or unreachable); retry later or set SEMANTIC_SCHOLAR_API_KEY"}
```

## 10. Search my corpus for Xenium in Methods

This one needs the database the weekly run fills (`litradar run`); it is SQLite with FTS5, so a search
over a year of Methods sections is instant.

```bash
litradar search "Xenium" --in methods --since 90d --limit 20
litradar search "Xenium" --in methods --since 90d --json | python3 -c 'import json,sys; [print(r["pub_date"], r["title"][:80]) for r in json.load(sys.stdin)]'
litradar entity txg-xenium --since 180d          # gazetteer-resolved mentions, including aliases
```

```python
from litradar.config import Settings
from litradar.store import Store
store = Store(Settings.from_env().db_path)
for row in store.search("Xenium", since="2026-07-19", limit=20, sections=("methods",)):
    print(row["pub_date"], row["journal"], row["title"][:70], "...", row["snippet"][:80])
```

Live check: see the end of this file (`__Q10__`).

## 11. Which companies co-occur with MRD

Run the company gazetteer over this month's MRD papers (title, abstract, affiliations, funding):

```python
from collections import Counter
from litradar.config import DEFAULT_CONFIG_DIR, load_yaml
from litradar.extract.gazetteer import Gazetteer
from litradar.sources import europepmc

companies = Gazetteer.from_yaml(load_yaml(DEFAULT_CONFIG_DIR / "companies.yaml"), "company")
q = f'("minimal residual disease" OR "measurable residual disease") AND {europepmc.date_clause("2026-10-01", "2026-10-17")}'
hits, n = Counter(), 0
for rec in europepmc.search(http, q, max_records=1000):
    p = europepmc.to_paper(rec); n += 1
    text = "\n".join([p.title, p.abstract, *p.affiliations, p.sections.get("funding", "")])
    for name in {m.entity_name for m in companies.find(text)}:    # one count per paper, aliases resolved
        hits[name] += 1
print(n, hits.most_common(10))
```

With a populated database the same question is a join (`litradar search "minimal residual disease" --json`
gives the paper ids; `store.mentions_for(paper_id)` gives the company mentions, including those found in
full-text COI and funding sections the API search cannot see).

Live check: **138 papers** this month; co-occurring companies (papers): Johnson & Johnson 3, then
Gilead Sciences, Adaptive Biotechnologies, AbbVie, GSK, Merck & Co, Takeda, BillionToOne, AstraZeneca, Sanofi at 1 each.

## 12. Proteomics datasets per mass spectrometer this week

```bash
litradar fetch pride --days 7 --out pride.jsonl
python3 -c '
import json, collections
print(collections.Counter(json.loads(l)["instrument_model"] for l in open("pride.jsonl")).most_common(10))'
```

```python
from litradar.sources import pride
counts = pride.instrument_counts(http, "2026-10-10", "2026-10-17")   # facet endpoint: one request per day, no records
print(sorted(counts.items(), key=lambda kv: -kv[1])[:10])
```

Live check (facet counts, datasets per instrument):
```
Orbitrap Exploris 480 20 | Q Exactive HF 16 | Orbitrap Fusion Lumos 12 | Orbitrap Astral 11 | Orbitrap Eclipse 8
Q Exactive 7 | timsTOF Pro 6 | Orbitrap Fusion 5 | TripleTOF 5600 4 | timsTOF HT 4
```

## 13. Single-cell and spatial datasets per assay this month

```bash
litradar fetch cellxgene --since 2026-10-01 --out cxg.jsonl        # one record per (dataset, assay)
```

```python
from litradar.sources import cellxgene
datasets = cellxgene.fetch_datasets(http)                             # the whole public index, one ~10 MB request
print(cellxgene.window_summary(datasets, "2026-10-01", "2026-10-17"))
for row in cellxgene.assay_histogram(datasets, "2026-10-01", "2026-10-17")[:5]:
    print(row["assay"], row["datasets"], row["cells"], row["entity_id"])
```

CELLxGENE records the assay chemistry as an EFO term (not the sequencer), so it complements the SRA/ENA
census. Live check: index of 2,243 datasets; this month **6 datasets, 5 collections, 2,362,822 cells**.
```
10x 3' v3   3 datasets  1,471,221 cells  txg-chromium
10x 5' v2   2 datasets    731,625 cells  txg-chromium
```

## 14. Using the clients from another project

```bash
pip install "git+https://github.com/<you>/lit-radar"          # three runtime dependencies: requests, PyYAML, lxml
pip install "lit-radar[browser] @ git+https://github.com/<you>/lit-radar"   # with the optional browser fetcher
```

```python
from litradar.http import Http
from litradar.sources import europepmc, ena, openfda, nih_reporter, clinicaltrials

http = Http(contact_email="you@example.com", cache_dir=".cache", ncbi_api_key="")
```

* One `Http` per process. It carries the per-host pacing every API asks for (`litradar.http.DEFAULT_MIN_INTERVAL`),
  retries with backoff, honours `Retry-After`, stops hitting a host that answers 429 twice, and caches
  on disk when you pass `cache=True`. Never run two loops against the same host concurrently.
* Every adapter is `module.fetch(http, start, end, ...)` or `module.search(http, ...)` plus a
  `to_paper` / `to_records` / `to_news_item` normaliser; the dataclasses are in `litradar.models`
  (`dataclasses.asdict(record)` is what `litradar fetch` writes).
* To build offline fixtures for your own tests, capture real payloads with `--raw`:
  `litradar fetch ena --days 1 --limit 5 --raw --out fixtures/ena.jsonl` writes one HTTP response per line
  (`url`, `params`, `payload`). The test files under `tests/` show the `FakeHttp` pattern that replays them.
* The three scripts in `examples/` import only `litradar.sources` and `litradar.http` and run as-is.

## 15. Adding a new source in 30 lines

A source is a module that yields `Paper`, `ArchiveRecord` or `NewsItem` objects. Say an archive with a
JSON search endpoint that lists datasets made public in a window with the instrument declared:

```python
# litradar/sources/examplearchive.py
"""Example Archive: datasets made public in a window, with the instrument declared (GET /api/datasets?from=&to=)."""
from __future__ import annotations
import logging
from typing import Iterator
from ..http import Http
from ..models import ArchiveRecord

log = logging.getLogger(__name__)
API = "https://archive.example.org/api/datasets"

def fetch(http: Http, start: str, end: str, max_records: int = 10000) -> Iterator[ArchiveRecord]:
    """One record per dataset made public in [start, end] (YYYY-MM-DD)."""
    page, n = 1, 0
    while n < max_records:
        data = http.get_json(API, params={"from": start, "to": end, "page": page, "size": 200})
        items = (data or {}).get("items") or []
        if not items:
            break
        for it in items:
            n += 1
            yield ArchiveRecord(archive="examplearchive", accession=str(it.get("id") or ""), instrument_model=it.get("instrument") or "",
                                first_public=(it.get("released") or "")[:10], study_title=it.get("title") or "", organism=it.get("species") or "")
        page += 1
    log.info("examplearchive: %d datasets in %s..%s", n, start, end)
```

Three more lines make it a shell tool, described by `--list` from that docstring, paced by whatever you
add for its host in `litradar/http.py` `DEFAULT_MIN_INTERVAL`:

```python
# litradar/toolkit.py, inside REGISTRY
SourceSpec("examplearchive", examplearchive, "ArchiveRecord", lambda http, req: examplearchive.fetch(http, req.since, req.until, max_records=req.max_records or 10000),
           WINDOW + ("--limit",), access="free, no key", hosts=("archive.example.org",), example="litradar fetch examplearchive --days 7"),
```

Then an offline test with a canned payload (copy the `FakeHttp` from `tests/test_toolkit_fetch.py`), and,
if the weekly digest should carry it, a call in `litradar/pipeline.py` (`CONTRIBUTING.md`).
