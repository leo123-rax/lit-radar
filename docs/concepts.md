# Emerging concepts (`litradar concepts`)

**Question answered:** "which technical ideas are suddenly being written about more, and which ones
are brand new?" The instrument and company tables answer *who is being used*; this command answers
*what is being done* — methods, assays, modalities and model classes, extracted from titles, abstracts
and parsed Methods sections with no machine learning, counted per week, and tested for bursts against
a trailing baseline.

```bash
python -m litradar concepts                              # last 4 weeks vs the 26 weeks before, top 30
python -m litradar concepts --weeks 12 --top 15 --out digests/concepts.md
python -m litradar concepts --until 2026-10-17 --json    # reproducible window, machine-readable
python -m litradar concepts --llm                        # add Claude definitions / new-vs-renamed / relevance
```

Options: `--weeks N` (trailing baseline length, default 26; the *current* window is always the last 4
whole ISO weeks), `--top N` (default 30), `--until YYYY-MM-DD` (default today), `--min-papers N` (papers
a term needs in the current window to be scored, default 5), `--llm` / `--llm-top N` (default 40),
`--json`, `--out FILE`, `--force` (recount every week; by default only weeks with no counts yet plus the
latest two are recomputed, because papers keep arriving for recent weeks).

## What counts as a concept

Three channels run over `title + abstract (+ Methods when the full text was parsed)`:

| channel | rule | examples |
|---|---|---|
| technical terms | runs of 2–4 tokens that are capitalised words, hyphenated terms, tokens with digits or Greek letters; stop words break the run; **title and abstract only** (Methods name reagents, buffers, statistics and software far more than ideas) | `CRISPR-Cas9 Base Editing`, `self-amplifying mRNA` |
| acronym definitions | `long form (ACRONYM)`, resolved with a Schwartz–Hearst style backward match; **title and abstract only** (measured live, the definitions in Methods were lab boilerplate — bicinchoninic acid, polyvinylidene fluoride, column volumes, multiple cloning site — and, since Methods text exists only for papers whose full text was parsed, every one of them was a "first appearance") | `circulating tumor DNA (ctDNA)` |
| seed anchors | a curated list of method/concept heads (`-seq`, `-omics`, editing, atlas, foundation model, perturbation, organoid, nanopore, cell-free, in situ, single-molecule, spatial, long-read, digital twin, agent, autonomous lab, LNP, mRNA, bispecific, ADC, CAR-T, TCR, base/prime editor, epigenetic clock, virtual cell) plus up to two preceding modifiers | `single-cell foundation model`, `patient-derived organoids` |

Guards: Title Case titles and sentence-initial capitals are neutralised (otherwise every title would
leak `Novel Approach`); pure numbers, units (`10 mM`, `37 °C`), statistics abbreviations and a list of
boilerplate phrases (`confidence interval`, `machine learning`, `whole genome sequencing`, lab consumables
such as `Cell Counting Kit-8` or `polyvinylidene fluoride`, routine statistics such as `intraclass correlation
coefficient` or `Mendelian randomization`, ...) are dropped — the list is compared in the extractor's own key
space (`GENERIC_PHRASES` is rebuilt through `normalize_term`, so `hazard ratios` meets `hazard ratio`; variants
that differ in a non-final token, `principal coordinates analysis`, `green fluorescence protein`, are listed as such); single generic words (`proteomics`, `sequencing`) never count on their own; and **every alias
in `config/instruments.yaml` and `config/companies.yaml` is excluded, inside phrases too** — `Xenium In
Situ` or `nanopore sequencing` (an Oxford Nanopore alias) are products, not concepts, and the instrument
tables already count them.

Phrases are normalised to a key (lower case, dashes and slashes to spaces, simple plural of the last
token singularised) so `scRNA-seq`, `scRNA-Seq` and `scRNA seq` are one term; the most frequent surface
form is kept for display.

## Counting and burst detection

`concept_counts(term, week, papers)` holds distinct papers per term per ISO week (Monday key), built from
the paper's `pub_date` or, when a feed has no date, its `first_seen`. `concept_papers(term, paper_id,
week, channel)` keeps the links so examples, co-mentioned instruments/companies (a join on `mentions`)
and subgroup overlaps can be reported. Both tables are created additively by `litradar/concepts.py`
(`CREATE TABLE IF NOT EXISTS`); `store.py` is untouched.

For every term with at least `--min-papers` papers in the current 4 weeks:

```
expected = baseline weekly mean x active current weeks
sd       = max(baseline weekly std x sqrt(k), sqrt(max(expected, 1)), 1.0)      # Poisson-style floor
z        = (papers in window - expected) / sd
score    = z + 2.0 if the term was first seen in the last 8 weeks else z
```

Only baseline weeks that actually hold papers enter the mean and std, so a corpus that started three
weeks ago does not compare against 23 phantom zero-weeks. When fewer than 8 baseline weeks hold papers
the report is flagged **weak baseline**: the z-scores then mostly reflect raw counts and nearly every term
is "new", so read the list as candidates, not as bursts. A separate **new this month** list ranks terms
whose very first appearance in the corpus falls inside the current window (at least 3 papers).

### Frequency-list mode (fewer than 8 baseline weeks)

`burst_report` labels every report with `mode`: `"burst"` when the baseline supports burst detection,
`"frequency_list"` when fewer than `WEAK_BASELINE_WEEKS` (8) baseline weeks hold papers. In frequency-list mode
the rendered section says so in plain words — *"this is a frequency list, not burst detection"* — renames the Score
column *Score (count-driven)*, and adds a **First seen in corpus** column: the ISO week (Monday) in which the term
first appeared in the counted corpus, or `≤ <week> (corpus start)` when the term was already present in the first
counted week, so its true first appearance is unknown. The report carries the data behind the column
(`corpus_start_week`, `history_weeks` = counted weeks before the current window, and `first_seen` /
`first_seen_label` per riser and per new term), so `--json` consumers can rebuild it.

The column follows the mode automatically; `render_report_markdown(report, first_seen_column=True | False)` and
`render_concepts_markdown(..., first_seen_column=...)` force it on or off in either mode. A `--since-baseline`
command-line switch maps onto `first_seen_column=True`; the one-line wiring belongs in `litradar/commands/concepts.py`
(`render_report_markdown(report, top=..., first_seen_column=bool(args.since_baseline))`) and is not part of this
module.

## Output

Markdown (default or `--out`): a *Top risers* table (concept, papers in window, baseline per week, score,
new flag, a weekly sparkline over baseline + window, overlapping subgroups from `config/subgroups.yaml`,
co-mentioned instruments and companies), the *New this month* list, and *Examples* — three papers per
riser (title, venue, link) ranked by the paper's existing score. `--json` returns the same data with the
full weekly series per term. The function `render_concepts_markdown(store, since, until, top=15)` returns
the same thing as a digest section headed `## Emerging concepts` (the digest's one-week window is
widened to at least 4 weeks; counts are computed incrementally on first use).

Wiring into the weekly digest is one line in `litradar/digest/render.py`, before the
`## Coverage and method` section:

```python
from ..concepts import render_concepts_markdown
md.append(render_concepts_markdown(store, since, until, top=int(dcfg.get("concepts_n", 15))))
```

## Blocklist file (`config/concepts_blocklist.yaml`)

`load_exclusions(config_dir)` reads `concepts_blocklist.yaml` from the config directory (`load_blocklist` does the
parsing) and adds its lists to the built-in ones; the file never removes anything, and a config directory without it
behaves exactly as before. Three keys, all optional lists of strings:

| key | effect | matching |
|---|---|---|
| `clinical_phrases` | whole phrases dropped from every non-seed channel | after `normalize_term` (lower case, dashes to spaces, last word singularised): `Prefrontal Cortex` and `prefrontal cortices` both hit `prefrontal cortex` |
| `clinical_head_words` | a phrase whose **last** word is listed is clinical (`tumor hypoxia`, `renal artery`) | the head is compared after normalisation; the spelling as written is kept too |
| `generic_phrases` | boilerplate that is not a concept in any channel | added to `Exclusions.exact` |

The shipped file seeds anatomy (`prefrontal cortex`, `bone marrow`, `blood brain barrier`, ...), study settings and
outcome measures (`intensive care unit`, `overall survival`, `body mass index`, ...), common pathogens and
conditions (`escherichia coli`, `hepatitis b virus`, ...) and head words such as `cortex`, `survival`, `pneumonia`,
`ischemia`, `lesion`. `Exclusions.clinical_phrases`, `Exclusions.clinical_head_words` and `Exclusions.blocklist_path`
expose what was loaded; `is_clinical_phrase(key, excl)` applies built-ins plus the file. A file that is not a mapping
of lists raises `ValueError` naming it. As with the Python lists, run `litradar concepts --force` after editing so
every week is recounted.

## Optional Claude pass (`--llm`)

Never a hard dependency: it needs the `anthropic` package (`pip install 'lit-radar[llm]'`) and
`ANTHROPIC_API_KEY`. The top candidates (risers first, then new terms, `--llm-top` in total, default 40)
go to the model in one structured request per 20 concepts, each with its paper count, first-seen week
and up to three example titles. The response is schema-constrained JSON (`output_config.format`), the
model is `LITRADAR_LLM_MODEL` (default `claude-opus-5-5`), the rubric sits in a cached system prompt,
and refusals (`stop_reason == "refusal"`) are recorded as unscored rather than crashing. With more than
one request and `ranking.llm.use_batch: true` the Message Batches API is used. The estimated cost is
printed before any call (about $0.09 for 40 concepts on the default model, synchronous; half that via batch).

Per concept the model returns a one-line plain-language definition, a status (`new_concept`,
`renamed_or_rebranded` with the older name, `established`, `not_a_concept`), the vendor categories it
affects (sequencing, single_cell, spatial, proteomics, synthesis, gene_editing_and_delivery,
cell_therapy, diagnostics, imaging_and_structural, software_and_ai, lab_automation, pharma_modality,
other, none) and a 1–5 commercial-relevance score for tools and diagnostics vendors. Results are stored
in `concepts(term, display, definition_json, model, scored_at)` and shown under *Examples* on later runs
without calling the model again.

## Caveats

* A term is counted once per paper, however often it appears; abstracts contribute for every paper,
  Methods only when open full text was parsed (and only through the seed channel), so Methods-only
  vocabulary is under-counted — deliberately: a phrase that exists only where Methods text exists is
  "new" to the corpus by construction, not by the literature.
* Disease names, anatomy, study populations and regulators are not concepts here: a built-in blocklist
  (`CLINICAL_PHRASES`: amyotrophic lateral sclerosis, autism spectrum disorder, Parkinson's disease,
  healthy controls, natural killer, drug administration, ...) plus a head-word rule (`CLINICAL_HEAD_WORDS`:
  a phrase ending in disease / disorder / syndrome / carcinoma / patients / controls / ... ) drops them at
  extraction; curated seeds are exempt. **`config/concepts_blocklist.yaml` extends both lists without a code
  change** (see below). Fragments are dropped at report time: a term that is a strict
  sub-phrase of another counted term with at least as many papers this month ("dependent acquisition"
  inside "data dependent acquisition", "dna double strand" inside "dna double strand break") is the parent
  seen through the 4-token window, and the report lists them under `fragments_dropped`.
* The seed list and stop lists are deliberately conservative. Edit `_SEED_SPECS`, `GENERIC_PHRASES`,
  `CLINICAL_PHRASES` and `HYPHEN_STOP` in `litradar/concepts.py` to tune recall; run `litradar concepts --force` after
  changing them so every week is recounted with the new rules.
* With a one-week corpus (a fresh install) everything is "new" and the ranking degrades to paper counts
  of established terms; bursts need a few months of history to mean anything.
* Acronym resolution picks the shortest matching long form; unusual expansions (`T-cell receptor (TCR)`
  is fine, `the Cancer Genome Atlas (TCGA)` keeps `the`) are normalised but not corrected.
