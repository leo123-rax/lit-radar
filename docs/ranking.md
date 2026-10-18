# Ranking, storage rules and digest conventions

How a fetched record becomes a stored paper, a score and a digest line. Everything here is
implemented in `litradar/pipeline.py`, `litradar/store.py`, `litradar/rank/heuristics.py` and
`litradar/digest/render.py`; the data policy itself lives in `litradar/policy.py`.

## 1. What is stored from full text (data policy)

Before any full text is fetched the pipeline decides a *storage level* with
`policy.fulltext_storage_level(license, open_access, text_available)`:

| level | when | what the store keeps |
|---|---|---|
| `full` | CC-BY family, CC0, public domain, or the Europe PMC open-access subset (`isOpenAccess == 'Y'`) | sections (Methods, funding, COI, acknowledgements, data availability) and the body (FTS only) |
| `facts_only` | restrictive or unknown licence (bioRxiv `cc_no`, author manuscripts, blank licence) | nothing parsed from the text: only the gazetteer mentions with their short snippets; `funding` is rebuilt from grant *metadata* when present |
| `none` | no text to fetch | nothing |

* bioRxiv / medRxiv: the licence is the record's `license` field. Europe PMC: `extra['is_open_access']`.
* The level is written to `papers.extra_json.fulltext_policy`. `Store.upsert_paper` enforces it again
  (a non-`full` level strips sections and body whatever the caller passes), so the rule holds even for
  code paths that bypass the pipeline.
* Papers cached before the policy existed are cleaned by `litradar rematch`: each paper is re-extracted
  on its stored text first, then `Store.purge_paper_fulltext` strips the text. `Store.purge_restricted_fulltext`
  does the bulk version without re-extraction.
* Mentions derived from text that is not kept survive later re-ingests and rematches through
  `Store.replace_mentions(..., keep_sections=...)`: stored mentions for a section are retained when the new
  extraction has none for that section. `rematch` keeps every section it cannot re-derive from stored text
  (`Pipeline.underivable_sections`: Methods when neither Methods nor a body is stored, funding / COI /
  acknowledgements / data availability when that section is absent), whatever the policy level, so a paper whose
  text was never stored does not lose its company mentions on the next `litradar rematch`. Title, abstract and
  affiliations are always stored and therefore always re-derived (a stale mention there is dropped).
* The derived facts stay searchable even when the text is not: the `mentions` full-text column (docs/store.md)
  holds the entity names, matched aliases and ids of every stored mention, so `litradar search Revio` finds a
  facts-only paper whose Methods named a Revio, while `search --in methods Revio` (the text column) does not.

## 2. Re-ingest safety

The same paper arrives from several sources and in several weeks (preprint, then journal version;
metadata-only feeds after a full-text fetch). `Pipeline.run` folds the stored version into the fresh
record **before** full text and extraction (`Pipeline._merge_stored`), so a metadata-only re-fetch never
wipes Methods mentions and already-parsed papers are not fetched again.

`Store.upsert_paper` rules on update:

* `kind` never flips from `article` back to `preprint`.
* `pub_date` keeps the earliest non-empty date.
* `extra_json` is the union of stored and new keys (new non-empty values win; `sources` lists are unioned).
* abstract, sections, authors, affiliations and the body are never overwritten with empty values.
* `cited_by_count` keeps the maximum; `title` keeps the longer string.

## 3. Full-text search keying

`papers_fts` rows carry an explicit `rowid` equal to `papers.rowid`; every delete, lookup and join uses
the integer key. The previous layout keyed lookups on the `UNINDEXED id` column, which is a full scan
of the FTS table per paper (quadratic over a rematch). On open, `Store` checks once whether the FTS
rowids line up with `papers.rowid`; a store written by an older version is rebuilt in place (bodies
preserved) and the rebuild is logged.

`Store.iter_papers` streams rows in rowid order with keyset pagination, so `rematch` never holds more
than one batch of papers (and their bodies) in memory; `Pipeline.rank` accepts any iterable and scores
papers as they stream past while still assembling the LLM shortlist.

## 4. Heuristic score (ranking v2)

`rank/heuristics.py::score_paper_v2` scores a paper from signals that are already in the store and returns every
component with its points; `score_paper` is the `(scaled, reasons)` wrapper the pipeline calls. Points are raw
and summed, then scaled to 0-10 (`scale_max_raw`, default 16) and optionally blended with the LLM composite.
Defaults live in `DEFAULT_WEIGHTS`; `ranking.weights` in config/radar.yaml overrides any key.

| component | points (default) | source | shows in Signals as |
|---|---|---|---|
| venue prior | tier A +5.0, B +3.0, C +1.0, preprint +1.0, unranked +0.5 | `journal` / ISSN against config/journals.yaml; a preprint whose `published_doi` is a stored article takes that journal's tier | `journal tier A (+5.0)`, `preprint (+1.0)`, `journal unranked (+0.5)`, `journal tier A via published version Nature Methods (+5.0)` |
| first platform | +1.0 for the **first** platform entity only (`instrument_cap: 1`) | `mentions` with a platform category | `4 platform(s), scored 1: Xenium, Chromium, NovaSeq X (+1.0)` |
| platform central | +1.0 when a platform is named in the title or abstract | mention section | `platform named in title/abstract: Xenium (+1.0)` |
| lab tools | +0.2 each, cap 3 | infrastructure categories; `software` entities are listed, never scored | `8 lab tool(s) (+0.6)`, `2 software / model(s), not scored` |
| companies | +0.5 for the **first** commercial company only (`company_cap: 1`); institutes, foundations and AI tools listed, not scored | `mentions` + `type` from config/companies.yaml | `23 company mention(s), scored 1 (+0.5)`, `1 institute mention(s), not scored` |
| company author | +1.0 | a gazetteer company of commercial type in `affiliations` (never an `unlisted:` institution) | `company-affiliated author (+1.0)` |
| watchlist | +1.0 per term, cap 3 | `watchlist_terms` in title/abstract | `watchlist: single-cell, spatial transcriptomics (+2.0)` |
| tracked author | +3.0 by OpenAlex id, +1.5 by name only | `tracked_author_hits.matched_by` | `tracked author (+3.0)`, `tracked author (name match) (+1.5)` |
| novelty cues | +0.5 per cue, cap 1.0 | `NOVELTY_RE` | `3 novelty cue(s) (+1.0)` |
| new platform cue | +1.5 | `NEW_PLATFORM_RE`: "we present / introduce / develop ... a platform, method, assay, sequencer, instrument, ..." and the title shapes "NAME: a ... method", "NAME is a ... platform"; spans with "using / with / that / which" between verb and noun are rejected | `introduces a new assay (+1.5)` |
| citations | age-aware, cap 2.0: within 90 days of publication +1.0 once cited and +0.5 at >= 1 citation/week; older than 90 days only +1.0 at >= 3 citations/month; +0.5 for an influential citation (`extra_json.semantic_scholar.influential_citation_count`) | `cited_by_count`, `pub_date`, Semantic Scholar | `already cited 3x in 14 days, 1 influential (+2.0)`, `cited 240x over 24 months (9.9/month) (+1.0)`, `cited 1x over 4 months (0.3/month), not scored (slow for its age)` |
| preprint published | +1.0 | `published_doi` on a preprint | `preprint published as a journal article (Nature Methods) (+1.0)` |
| news co-mention | +1.0 | this window's `news_items`: a company the paper is about or authored from (title, abstract, affiliations), the vendor of a platform it names, or a platform named in a headline; a supplier named only in Methods does not count | `in the news this window: 10x Genomics, Xenium (+1.0)` |
| first concept | +1.0 | `concept_papers` / `concept_counts`: a concept whose first counted week is the current window and that at least `FIRST_CONCEPT_MIN_PAPERS` (2) papers in the window's weeks carry (a singleton phrase — a river name, one lab's acronym — is one paper's wording, not a concept entering the corpus; live, 5,901 of 6,548 first-seen terms were singletons), once the corpus holds `ranking.v2.concept_history_weeks` (4) earlier weeks of counts | `first corpus appearance: 'spatial proteomics' (+1.0)` |
| notice penalty | -4.0 | titles matching `NOTICE_RE` ("Author Correction:", "Erratum", "Retraction", ...), an OpenAlex `work_type` of `erratum` / `retraction`, and preprints whose record type is `withdrawn` | `correction / retraction notice (−4.0)` |
| not a research paper | -4.0 (`non_research_penalty`), and the venue prior falls to the unranked floor (+0.5) | `litradar/worktype.py`: OpenAlex `work_type` (`dataset`, `software`, `supplementary-materials`, `dissertation`, `peer-review`, `paratext`, `editorial`, `letter`), repository DOI prefixes and venues (Zenodo, figshare, MassIVE, PRIDE, Dryad, Mendeley Data, Dataverse, Code Ocean, protocols.io, the DOE PNNL / EMSL instrument-facility repository `10.25582` as `facility_deposit` ...), NEJM DOI codes (`nejmc` letter, `nejme` / `nejmp` editorial), and the cues "To the Editor:", "Reply to", "Comment on", "Supplementary Figure ... from", "MassIVE MSV", "github.com/" | `letter / correspondence, not a research paper (−4.0)`, `dataset deposit, not a research paper (−4.0)`, `journal tier A; prior withheld: letter / correspondence, not a research paper (+0.5)` |
| full text | 0 | `fulltext_available` | (off by default: data availability is not quality) |

What changed from v1 and why:

* **Verbosity no longer wins.** v1 paid +1.0 per platform (cap 3) and +0.5 per company (cap 3), so an open-access
  paper whose Methods named six machines, seventeen kits and twenty-three vendors outscored a Nature paper that
  introduced one assay (shakedown: mean score 2.80 with parsed full text vs 1.43 without). v2 scores the first
  platform and the first company only and lists the rest.
* **Venue is a prior, not a tiebreaker.** Tier A moved from 4.0 to 5.0 and tier B from 2.5 to 3.0, so the venue
  outweighs any Methods stack on its own; a bare tier-A paper still does not reach the top 25 without a topical
  signal (5.0 of 16 raw = 3.1).
* **Name-matched tracked authors are evidence, not proof.** Every tracked-author hit in the shakedown window was a
  name match (none by OpenAlex id); at +3.0 they lifted a Nature opinion piece and an "Author Correction" into the
  list. A name match is now +1.5 and the notice penalty removes corrections and retractions outright.
* **Platform in the abstract beats platform in the Methods.** A paper *about* a platform (named in title or abstract)
  earns a second point; a paper merely run on one does not.
* **Signals already in the store are used:** citation velocity (age-aware, so a backfilled 2024 paper with hundreds
  of citations is not "early"), preprint-to-journal conversion, news co-mention, first-in-corpus concepts.
* **A record that is not a paper is not ranked as one.** OpenAlex topic feeds return everything with a DOI: on a
  backfilled corpus a top-25 list carried a letter to the editor in a tier-A journal (5.3, of which +5.0 was the
  venue prior, kept alive by a tools-company affiliation) and two dataset deposits labelled "(preprint)" with a
  tracked author each. `litradar/worktype.py::classify` recognises letters, editorials, dataset / software /
  protocol deposits, supplementary files, dissertations, peer-review reports and front matter from what is already
  stored (OpenAlex `work_type`, repository DOI prefixes and venues, NEJM DOI codes, title and abstract cues); such a
  record loses its venue prior and takes `non_research_penalty` (−4.0), and the "why it matters" line says what it
  is ("... letter / correspondence, not a research paper"). The same rule orders `litradar search` (research papers
  first) and is one SQL expression there (`worktype.sql_predicate`). `Pipeline.rank` scores with a minimal record
  (title, abstract, kind), so there only the text cues fire; `rerank_window` sees the full row at digest time. The
  upstream filter (`type:article|preprint` on the OpenAlex queries, docs/DESIGN.md section 6) would keep most of
  these records out of the store altogether and is tracked separately.

### Context, re-ranking and persistence

`Pipeline.rank` still scores every paper as it streams in, with the minimal record and no context: the new caps and
the venue prior apply there immediately, the store-derived signals do not. At digest time `rank/rerank.py::
rerank_window` re-scores the whole window with the full record (`extra_json`, `published_doi`, `pub_date`, every
mention section) and a `RankContext` built once per window by `rank/context.py::build_context`: the window's news
company ids and headlines, the concepts first seen in the window (only when the corpus already holds four weeks of
concept counts), and the journal version of every preprint that carries a `published_doi`. Stored LLM composites are
blended exactly as the pipeline blends them, `priorities:` multipliers are applied, and the refreshed `heuristic`,
`final` and `reasons` are written back (`ranking.v2.persist`, default on), so `litradar search`, the dashboard and the
digest show the same numbers. `ranking.v2.enabled: false` lists the stored scores as before.

Complexity is linear in the window: one `papers_in_window` query, one chunked `mentions` query and one chunked
`tracked_author_hits` query per 500 papers, then a single pass. The 3,280-paper shakedown window re-ranks in under a
second on a laptop.

### "Why it matters" without a model

`rank/explain.py::why_it_matters` turns the components into one sentence, e.g. *"Nature paper introducing a new
assay; cited 3x in its first week; names Xenium and 10x Genomics; on watchlist topics spatial proteomics; first corpus
appearance of 'spatial proteomics'"*. Every clause names a fact the heuristic scored; nothing is inferred beyond that.
The digest prints it under an entry that has no LLM json, tagged *(from the score components; no LLM)*
(`digest.why_it_matters`, default on); an entry with LLM json keeps the model's `one_line` / `why_it_matters`.

### Diversity and near-duplicates

Two soft caps reorder the top list without shortening it (`rank/diversity.py::diversify`; `ranking.diversity`):
at most `max_per_kind` (15) preprints or articles and at most `max_per_subgroup` (8) entries per primary subgroup
(`rank/subgroups.py` mirrors `Pipeline.assign_subgroups`: subgroup keywords in title/abstract, the OpenAlex
`primary_topic`, or a backfill tag; the catch-all `other` is never capped). Candidates that would breach a cap are
deferred and come back in score order if slots remain; the method line under the heading says how many were deferred,
and each entry shows its subgroup tag after the score (`digest.show_subgroup`).

Before the caps, `rank/rerank.py::cluster_related` folds near-duplicates: two papers in the same venue on the same
day that share a first or last author whose first name is spelled out identically on both records, and that either
share an informative title token (six letters or more, not boilerplate) or share both first and last author, are one
story. The better-scoring one is shown with `+N related: ...`; initials-only author names ("Zhang Z") never cluster,
because on a busy preprint day a shared surname and initial is not one group (`ranking.diversity.cluster_same_group`).

### Before and after on a snapshot

Window 2026-10-11 to 2026-10-18 of a 3,562-paper store (3,280 papers in the window; 496 news items; two weeks of
concept counts, so first-in-corpus concepts were not scored; no paper in the window had a citation or a Semantic
Scholar record, so the citation signals did not fire; every tracked-author hit was a name match).

Top 25 with the stored v1 scores:

| # | score | venue | title |
|---|---|---|---|
| 1 | 6.4 | Cell | A telomere-to-telomere map of somatic mutation burden and functional impact in a cancer |
| 2 | 6.4 | preprint | Rethinking Syndecan-1 as a Marker of Trauma-Induced Endothelial Injury |
| 3 | 6.2 | preprint | Unifying N- and O-glycopeptide dissociation methods with expedient electron transfer dissociation |
| 4 | 5.8 | preprint | Spatially resolved tumor-microenvironment architecture defines therapeutic response in unresectable hepatoblastoma |
| 5 | 5.8 | preprint | An integrated multi-omic atlas of the choroid plexus highlights conserved molecular programs of epithelial function |
| 6 | 5.8 | preprint | WarpDemuX-custom - Multiplexed sequence-specific direct RNA sequencing |
| 7 | 5.7 | Cell | Somatic epimutations cap genetic determinism in the human diploid chromatin epigenome |
| 8 | 5.4 | Science Advances | Vitamin D binding protein in ventral CA3 of the hippocampus drives stress-induced depression |
| 9 | 5.4 | Science Advances | Macrophage- or neutrophil-expressed UPP1 drives tumor growth and immune suppression in the tumor microenvironment |
| 10 | 5.4 | Science Advances | Local control of Cre-regulated genetic modeling in vivo using wireless implantable optoelectronics |
| 11 | 5.4 | Science Advances | A B1 SINE element within the mouse Malat1 long noncoding RNA acts as an internal ribosome entry site mediating micropeptide translation |
| 12 | 5.4 | eLife | Product-stabilized filamentation by human glutamine synthetase allosterically tunes metabolic activity |
| 13 | 5.4 | eLife | Allosteric disulfide control of ligand binding and endocytosis of KIR2DL4, the natural killer cell receptor for HLA-G |
| 14 | 5.4 | preprint | Anisomycin-induced ribosome collisions follow a dose-dependent positional rule, distinct from the UV-induced collision landscape |
| 15 | 5.4 | Nucleic Acids Research | HURDLER: a cloning method to overcome length limits in designed repeat proteins |
| 16 | 5.4 | Nucleic Acids Research | Structural basis of assembly-dependent activation of the MAD3–MAD4 nuclease in the Pseudomonas aeruginosa MADS defense system |
| 17 | 5.4 | Nucleic Acids Research | PXL: a nucleic acid-binding module of the promyelocytic leukemia protein |
| 18 | 5.4 | Nucleic Acids Research | Genomic sequence context differentially impacts stepwise oxidation by TET enzymes to shape DNA hydroxymethylation landscapes |
| 19 | 5.4 | Nucleic Acids Research | Development of CRISPR/Cas13-based analytical tools to study RNA–protein interactions |
| 20 | 5.4 | Nucleic Acids Research | CircACVR2A(2–4) regulates stem cell pluripotency by uncoupling CDK-activating kinase functions |
| 21 | 5.4 | preprint | Genome assemblies and annotations of the poneroid ants Platythyrea mocquerysi (Ponerinae) and Stigmatomma oregonense (Amblyoponinae) |
| 22 | 5.4 | Genome Biology | Amira: detection of single and multi-copy antimicrobial resistance genes directly from long reads using gene-space de Bruijn graphs |
| 23 | 5.1 | preprint | Deep-learning-driven tumor microenvironment profiling improves immunotherapy response prediction |
| 24 | 5.1 | preprint | Spatial Transcriptomics Reveals Region-Specific Host Responses in Amoebic Gill Disease of Atlantic Salmon |
| 25 | 5.1 | preprint | T cell engagers convert acute tumor killing into endogenous vaccination |

Top 25 after re-ranking with v2, diversity caps and clustering (26 near-duplicates folded across the window, nothing
deferred: 15 articles / 10 preprints sat within the caps):

| # | score | venue | subgroup | title |
|---|---|---|---|---|
| 1 | 6.1 | preprint | spatial | Deep-learning-driven tumor microenvironment profiling improves immunotherapy response prediction |
| 2 | 5.9 | Nature Communications | single_cell | Integrated multi-omics identifies distinct immune signatures with diagnostic and therapeutic implications in immune checkpoint inhibitor-induced myocarditis |
| 3 | 5.6 | Cell | other | Somatic epimutations cap genetic determinism in the human diploid chromatin epigenome (+1 related: A telomere-to-telomere map of somatic mutation burden and functional impact in a cancer) |
| 4 | 5.4 | preprint | single_cell | Spatial Transcriptomics Reveals Region-Specific Host Responses in Amoebic Gill Disease of Atlantic Salmon |
| 5 | 5.4 | preprint | long_read | WarpDemuX-custom - Multiplexed sequence-specific direct RNA sequencing |
| 6 | 5.3 | preprint | long_read | Linking somatic variants to altered methylation profiles in nanopore-derived breast and pancreatic cancer genomes using unduliner |
| 7 | 5.0 | Science | other | Atomic functional site scaffolding in biopolymers with RFdiffusion3 |
| 8 | 5.0 | preprint | single_cell | A Single-Cell Atlas of PVOD Across the Lifespan Reveals Cellular and Molecular Therapeutic Targets |
| 9 | 4.9 | preprint | single_cell | Rethinking Syndecan-1 as a Marker of Trauma-Induced Endothelial Injury |
| 10 | 4.8 | Nucleic Acids Research | other | HURDLER: a cloning method to overcome length limits in designed repeat proteins |
| 11 | 4.7 | Nature Methods | other | AmpliconSuite: an end-to-end workflow for analyzing focal amplifications in cancer genomes |
| 12 | 4.7 | Genome Biology | long_read | Benchmarking long-read aligner-caller pipelines for structural variant detection across ten angiosperm species with PacBio high-fidelity sequencing |
| 13 | 4.7 | New England Journal of Medicine | other | Lonvoguran Ziclumeran — In Vivo CRISPR Gene Editing in Hereditary Angioedema |
| 14 | 4.7 | New England Journal of Medicine | other | Adapting to Genomic Medicine |
| 15 | 4.7 | preprint | proteomics | Unifying N- and O-glycopeptide dissociation methods with expedient electron transfer dissociation |
| 16 | 4.4 | eLife | other | Allosteric disulfide control of ligand binding and endocytosis of KIR2DL4, the natural killer cell receptor for HLA-G |
| 17 | 4.4 | Nucleic Acids Research | other | Genomic sequence context differentially impacts stepwise oxidation by TET enzymes to shape DNA hydroxymethylation landscapes |
| 18 | 4.4 | Genome Biology | long_read | Amira: detection of single and multi-copy antimicrobial resistance genes directly from long reads using gene-space de Bruijn graphs |
| 19 | 4.4 | Nature Communications | other | Orientation tomography of lanthanide-doped nanoprobes by polarization-resolved micro-spectroscopy |
| 20 | 4.4 | Nature Genetics | single_cell | Clonal heterogeneity in human pancreatic ductal adenocarcinoma and its impact on tumor progression |
| 21 | 4.4 | preprint | spatial | STARgrid: an open-source pipeline from Visium HD sequencing reads to count matrices |
| 22 | 4.2 | preprint | spatial | An integrated multi-omic atlas of the choroid plexus highlights conserved molecular programs of epithelial function |
| 23 | 4.1 | preprint | long_read | Genome assemblies and annotations of the poneroid ants Platythyrea mocquerysi (Ponerinae) and Stigmatomma oregonense (Amblyoponinae) |
| 24 | 4.1 | Genome Research | other | Conditional diffusion for single-cell Hi-C data imputation and enhancement with scHiC-Diff |
| 25 | 4.1 | Clinical Cancer Research | liquid_biopsy | Radiotherapy combined with serplulimab, and chemotherapy as first-line treatment for metastatic pancreatic adenocarcinoma: a multicenter, single-arm, phase II trial |

Judgement: the second list is the better one for a reader who follows life-science tools. Twelve of the v1 entries
(#8-#13, #15-#20) were open-access biology papers whose only signal was the number of vendors in their Methods, with
nothing a tools investor would act on; all twelve are gone, and the two that survive (#16, #17) do so on a
company-affiliated author or a watchlist term. In their place are papers *about* platforms and methods: a CosMx /
Visium deep-learning framework, an Olink panel with a diagnostic claim, nanopore methylation calling, Xenium atlases,
a Visium HD processing pipeline, PacBio HiFi benchmarking, a Nature Methods workflow, and an in vivo CRISPR therapy
in the NEJM with a company author. The two Cell papers from one group on one day became a single entry with a
"+1 related" link instead of ranks 1 and 7. The weak spots are visible rather than hidden: #14 is an NEJM
perspective lifted by tier A plus a company author in the news, the news co-mention is broad in a week when the
large vendors are always in the news (it acts as a tiebreaker, never as the reason an entry is there), the
first-in-corpus and citation signals could not fire on this snapshot (they are exercised by tests), and `other`
holds ten entries because the subgroup keywords are narrow. The verbosity defect from the shakedown is closed; the
list is now a list of tools stories.

Company mentions are scored by the entry's `type` in `config/companies.yaml` (`Pipeline.rank` passes
`Pipeline.company_types`; `heuristics.company_role` decides):

| type | role | effect |
|---|---|---|
| tools, diagnostics, synthesis, ai_bio, pharma, cro, unknown | `company` | counted in "N company mention(s)" and eligible for the company-author bonus |
| institute, foundation, non_profit | `institute` | listed as "N institute mention(s), not scored"; an institute affiliation is not a company-affiliated author |
| software, ai_tool, llm | `ai_tool` | listed as "N AI tool(s), not scored" (language-polishing and structure-prediction credits are not commercial signals) |
| *(no entry)* `unlisted:<slug>` | `unlisted` | an OpenAlex company-typed affiliation the gazetteer does not know (`Pipeline.extract`); listed as "N company-type institution(s) not in the gazetteer, not scored", never a company mention, never a company-affiliated author and never named in "why it matters". OpenAlex's institution resolution is noisy (an ORCID URL typed as a social-media company, "Mivax" resolved to "Mabvax"), so the pipeline already drops a company-typed institution whose name the author's raw affiliation string does not corroborate (docs/gazetteer.md, Sections); what survives is a candidate for `companies.yaml`, not evidence about the paper |

The top list applies the same split: its per-paper line reads `Companies: ...`, `Institutes / funders: ...`
and `AI tools: ...`, and an entity that appears only in funding, competing-interest or acknowledgement text
carries a role tag, e.g. `Profluent Bio (COI)`, so a disclosed equity stake never reads like supplier usage.

## 5. Preprint to publication

When a preprint and its journal version are both in the digest window the digest shows **only the
journal version**, marked `(also as preprint <date>)` with the preprint's *first posting* date (a bioRxiv /
medRxiv DOI encodes it, so a revision fetched this week is not dated after its own journal version), and
gives it the better of the two scores. The link is made by the preprint's `published_doi` (the server's own
statement, taken as it stands) or, failing that, by an identical normalised title (25+ characters) — but a
title match needs a record that demonstrably comes from a preprint server (`Store.is_preprint_record`:
a preprint-server source, a DOI minted by one, or a venue naming one; Figshare / Zenodo / wwPDB deposits
that OpenAlex types as repository preprints are not) and a posting date no later than the article's.
`Store.preprint_conversions(since, until)` lists the pairs for the window, including preprints posted in
earlier weeks whose journal version is in the window (found from the article by `published_doi` or title),
each with `matched_by`, `preprint_date`, `article_date`, `lag_days` and `preprint_server` (the venue, else the
server the DOI was minted by). The digest prints them once, under "Preprints that appeared as journal articles
this window", one line per article dated by its earliest preprint record (`+N further preprint records` when a
paper has several); the "What changed" section points there instead of printing its own table, so one document
never carries two answers.

## 6. Companies by type

`render(..., company_types={id: type})` splits the Companies section using the `type` field of
`config/companies.yaml` (the caller passes the mapping; `Pipeline.company_types` builds it):

* main table: `tools`, `diagnostics`, `synthesis`, `ai_bio` and any unknown type;
* compact table: `pharma`, `cro`;
* one line: `institute`;
* one line "AI tools and software used": `software`, `ai_tool`.

Roles (author / funding-COI / subject / supplier) are kept everywhere.

## 7. Escaping

Every untrusted string (titles, abstracts, snippets, LLM `one_line` / `why_it_matters` and flag values,
news headlines and feed names, company and author names, archive instrument strings, source error
messages) passes through `render.esc`, which escapes `< > | [ ]`, collapses whitespace and neutralises
a leading `# - + * >`. A bare `&` is left as is: Markdown renders it correctly and `&amp;` would leak into
plain-text readers (e-mail, chat); because `<` is always escaped, an entity sequence in the input can only ever
render as visible text. Links are emitted only for `http(s)` URLs (`render.safe_url`).

## 8. Archive census labels

`Store.archive_instrument_counts` never returns a blank vendor: models the gazetteer cannot map are
labelled from the declared platform string, e.g. `Illumina (model unspecified)` for `ILLUMINA` /
`unspecified`, `Illumina (unmapped model)` for an unknown Illumina model string. Rows carry `archive`
and `vendor_mapped`, and the query accepts an `archives` filter so the digest can show sequencing archives
(SRA, ENA, GEO), proteomics datasets (PRIDE) and single-cell / spatial assays (CELLxGENE) separately.

## 9. Tracked authors

An OpenAlex author id on the record is an exact match (`matched_by = openalex_id`). Without one the match
is by name, and a name match requires the **full given name and the surname** (`pipeline._norm_name`:
`First M. Last`, `First Last` and `Last, First M.` all reduce to `first last`) or the **exact display name**
as configured. A surname plus an initial (`Zhang F`, `Anderson, D. J.`, `F. Zhang`) never matches: on a common
name that is not evidence of identity. When the tracked person has an id configured and the record carries
author ids, a different id means a different person and the name match is dropped
(`tracked_hits.name_rejected_other_id`).

Hits store `matched_by`: `openalex_id`; `name` (no id configured: the digest says "add the OpenAlex id to
make it exact"); `name_no_ids` (an id is configured but the record carried no author ids, so it could not be
checked, and the digest says so instead of asking for an id that already exists). Name matches of both kinds
are capped per run at `tracked_authors.name_match_cap` hits per tracked name (default 5); the overflow is
counted (`tracked_hits.name_capped`), logged and reported in the Coverage section.

## 10. Run end

`Pipeline.run` folds the WAL into the main database file in a `finally` block (`Store.checkpoint`) so
the `.sqlite-wal` sidecar is empty on disk even when a stage fails; the store stays open because the
caller renders from it afterwards and should close it (`Store.close` is idempotent and also works as a
context manager). Before that, `run` (and `rematch`) merge the FTS5 segments when the run rewrote at least
`fts_optimize_after` full-text rows (default 50; `radar.yaml` top-level key), see docs/store.md. The run
report carries `started`, `finished`, `since`, `until`, `counts`, `errors` and `http` (client counters plus
`blocked_hosts` and `failures_by_host`).

## 11. Which failures reach the error list

Every source adapter's exception lands in `errors` as `<source>: <message>`; the first word before the colon
is the source id that `litradar status` and the coverage section report and that the suggested re-run command
uses (`litradar run --sources <id> -v`), so messages are prefixed with that id (`sec: SEC 8-K feeds skipped (...)`).
In addition, at the end of a run (`Pipeline._fold_http_errors`):

* a host the HTTP client skipped after repeated 429s is reported as
  `<source>: host <host> rate-limited (HTTP 429 N times in a row); skipped for the rest of the run, K request(s) not sent`;
* a host that was asked and never answered successfully is reported as `<source>: all N request(s) to <host> failed`;
* OpenAlex queries report per group (`topics`, `company_authors`, `tracked_authors`, `journals`):
  `openalex: <group>: F of T queries failed (<API message>)`, with a WARNING per failed query carrying the API's
  message (the client also logs the first HTTP 400 body per host at WARNING);
* Semantic Scholar enrichment that failed every batch without matching a DOI is an error, not a counter.

Hosts map to source ids through `pipeline.HOST_SOURCES`; a source that already reported its own error (gdelt,
OpenAlex) is not listed twice.

Every id that can appear there is accepted by `litradar run --sources` (`pipeline.RUN_SOURCES`; anything else is a
usage error, exit 2). The news layer has its own ids: `news` runs all of it, `press`, `sec`, `gdelt` and `gnews` run
one sub-layer each (the feed kinds of `config/feeds.yaml`, the SEC 8-K feeds, GDELT), so the re-run hint for
`sec: ...` or `gdelt: ...` does what it says; `rss` still runs the journal / subject paper feeds plus the whole news
layer. Google News stays off unless `news.google_news` is true, whatever `--sources` names.

## 12. Coverage and method block

The digest's "Coverage and method" section separates two kinds of numbers that used to be mixed:

* **This window, counted in the database**: papers in the digest window and how many have parsed full
  text, by source. These are the numbers the headline refers to.
* **Last pipeline run**: the counters of one pipeline process (records pulled per source before
  de-duplication, unique / new / merged papers, full text parsed, archive records, signals, citation
  enrichment, skipped hosts, errors), labelled with the run's finish time and its own window. When the last
  recorded run for exactly this window exists it is used; otherwise the most recent run of all, and the block
  says in so many words that its window is not the digest's. A digest for a database without runs says so.

Under the title a line records when the digest was rendered: every count is a snapshot of the database at
that moment, which is why a committed digest and a later copy of the database can disagree.

## 13. Top-list summaries

An entry shows the LLM one-liner when there is one, else the abstract (cut at ~420 characters) followed by the
template "why it matters" line built from the score components (section 4). When the
record has no abstract the pipeline first tries to recover it from the full-text XML it already fetched
(`extract.jats.parse_jats` returns `abstract`; `Pipeline._fill_abstract_from_jats` uses the client's disk cache,
so this costs no request; counted as `fulltext.abstract_from_jats` and recorded in `extra_json.abstract_source`).
If the stored record still has none, the renderer falls back to the opening of the stored body ("(from full
text; no abstract on record)") or to the first sentence of the stored Methods; facts-only papers without any
stored text get no summary line.

## 14. Platform and lab-tool tables

`Store.instrument_counts(..., platform_only=True / False)` filters by gazetteer category in SQL before the row
limit, so the "Platforms named in this week's papers" table and the "Lab infrastructure and consumables" table
have separate budgets (`digest.platforms_table_n`, default `instruments_table_n` = 40, and
`digest.lab_tools_table_n`, default 25). A cut table ends with "… and N more ... named in the window". The
summary JSON carries `platforms`, `lab_tools`, `platforms_total`, `lab_tools_total` and the combined
`instruments` list.


## Topical gate

A tier-A or tier-B venue prior is worth more than most topical signals, so a general-science journal issue would
otherwise push off-topic articles (weather forecasting, quantum hardware) into the shortlist on venue alone. The
scorer therefore withholds a tier-A or tier-B prior (it falls back to the unranked-venue floor, +0.5; tier C and
preprint priors are small and stay) when none of the
topical components fire: a platform or lab tool named, a watchlist term, a tracked author, or a company named or
affiliated whose gazetteer type is known and not `software` (big-tech labs publish in every field, and an
affiliation-derived `unlisted:` company says nothing about the field, so neither counts; a first-in-corpus n-gram or a bare "we introduce a new framework" cue is not either). The reason string says "prior withheld" so the reader can
see why a prestigious venue scored low. `ranking.weights.topical_gate: false` restores the plain prior.
