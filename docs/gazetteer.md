# The gazetteers: how matching works and how to curate them

Two YAML files drive every instrument / company mention the radar reports:

* `config/instruments.yaml` -- platforms, lab infrastructure, reagents and software (one flow-mapping entry per line).
* `config/companies.yaml` -- tools vendors, diagnostics, synthesis, AI-bio, pharma, CROs, institutes and software vendors.

The matcher is `litradar/extract/gazetteer.py`. Everything below describes its behaviour as of 2026-10-17; the
rules are enforced by `tests/test_gazetteer.py` and the regression fixtures in `tests/fixtures/gazetteer_cases.yaml`.

## 1. How matching works

**Aliases become regexes.** Every entry is matched on its `name` plus each string in `aliases`. An alias is split on
whitespace and hyphens, each piece is regex-escaped, and the pieces are joined with `[\s\- ]*`, so `NovaSeq X Plus`
also matches `NovaSeq-X Plus` and `NovaSeqX Plus`. The pattern is wrapped in `(?<![\w\-]) ... (?![\w\-])`: a hit
must start and end at a word boundary **and** must not be glued to a hyphen (`self-evident` never matches `Evident`,
and `TransIT-LT1` is not a hit for the bare alias `TransIT`; the longer alias has to exist).

**Case.** Aliases are case-insensitive unless (a) they are listed in `case_sensitive_aliases`, or (b) the matcher's
own heuristic `_is_ambiguous()` flags them: any alias whose compact form (spaces and hyphens removed) is 3 characters
or shorter, or 4-5 all-lowercase letters, is automatically case-sensitive. Nothing else is: a capitalised
dictionary word such as `Evident`, `Myriad` or `TransIT` is matched case-insensitively **and** without context unless
you say otherwise. That single fact caused most of the false positives found in the 2026-10-17 audit.

**Context.** A hit "needs context" when the entry has `context_required: true`, or the alias is case-sensitive, or the
alias is auto-ambiguous and is not the entry's name. A hit that needs context only counts if one of the entry's
context terms occurs within 250 characters on either side of the match (the match itself is excluded from the
window, so an alias cannot vouch for itself). Context terms default to `[vendor, parent, name]`; set `context_terms`
explicitly whenever the default is useless (company entries have no vendor, so a case-sensitive company alias without
`context_terms` can only ever fire next to the company's own long name). Context terms go through the same
regex builder, so `consult` does **not** match `consulting`; list the forms you mean.

**Longest alias wins.** Patterns are tried longest-first and spans are never re-used, so `NovaSeq X Plus` is not also
counted as `NovaSeq X` or `NovaSeq`, `Sequel II` is not counted as `Sequel`, and `Novo Nordisk Foundation` is not
counted as `Novo Nordisk`. This is the mechanism to use when a longer phrase must be routed to a different entity
than its prefix (the foundation vs the company, `CFX Maestro` vs `Schrödinger Maestro`, `Coherent Chameleon` vs the
SPT Labtech `chameleon`).

**Addresses are not mentions.** A hit that is part of a web or e-mail address is skipped: `gnomad.broadinstitute.org`,
`https://github.com/broadinstitute/gatk`, `name@illumina.com`, `imagej.net`. The rule looks at the character before
the hit (`@`; a `.` preceded by an alphabetic host label such as `www.` or `portals.`; a `/` inside a URL) and at the
text after it (`.` + a lower-case top-level domain with nothing glued on). "2.3.Illumina sequencing" (a section number
with the space missing) and "Illumina.Interestingly" stay hits; "Illumina.in vitro" (missing space, lower-case `in`)
is an accepted loss. Archive strings (`ignore_context=True`) are never checked, they contain no addresses. Live
effect on the audited store: 12 company mentions, all `broad-institute` and `addgene` URL credits (`gnomad.`, `gatk.`,
`software.`, `portals.` sub-domains, GitHub paths), stop counting as institute mentions.

**Unicode hyphens.** The word-boundary test treats the hyphen U+2010 and the non-breaking hyphen U+2011 like the
ASCII hyphen, both as separators inside an alias (`NovaSeq‐X Plus` matches) and as glue that blocks a hit
(`University Medical Center Hamburg‐Eppendorf` -- the hospital -- is not a hit for the company `Eppendorf`). En and em
dashes are deliberately left out: they are sentence punctuation (`Illumina—the vendor—`).

**Sections.** Instruments are searched in title, abstract and Methods; companies additionally in funding, competing
interests, acknowledgements and affiliations, and through author e-mail `domains`. Archive instrument strings
(SRA/ENA `instrument_model`) are mapped with `match_archive_model`, which ignores context because the input is a
controlled vocabulary: `Revio`, `Sequel`, `MinION` map on their own even though prose requires context for them.

**Families.** Sibling models that a reader may want to see as one line share a `family` key: `novaseq-x`
(NovaSeq X, NovaSeq X Plus), `sequel` (Sequel, Sequel II/IIe), `chromium` (Chromium, Chromium Flex, Chromium Multiome,
Chromium Single Cell 3'), `nextseq-1000-2000`, `promethion`. The key never affects matching or storage: every entry
keeps its own id and its own mention rows. `Gazetteer.families` maps entity ids to family keys and
`Store.instrument_counts(..., families=gazetteer.families)` groups the rows (docs/store.md §8); the group is labelled
with the member whose name slugifies to the key ("NovaSeq X", "Chromium"), else with the member names joined by
" / " ("NextSeq 1000 / NextSeq 2000"). `litradar show <instrument>` prints the family in the identity block.

**Scoring.** `litradar/rank/heuristics.py` splits instrument mentions into *platforms* (weight 1.0 per distinct entity,
cap 3) and *lab tools* (0.2, cap 3) by category prefix; see section 4.

### Which alias matched

`Gazetteer.find` returns `MatchedMention` objects (a `Mention` plus `alias`: the alias strings that actually
matched, `; `-joined in match order). The store writes it to `mentions.alias` and indexes it, with the entity
name and id, in the `mentions` full-text column (docs/store.md), so a paper that wrote "Next GEM" is found by
`litradar search "Next GEM"` as well as by `search Chromium`. The alias also tells a reviewer what the digest's
canonical label stands for when an entry bundles distinct product lines (a `Ceta` match shown under a different
camera's name is visible as such in `mentions.alias`).

## 2. How to add an entry

1. **Pick the gazetteer and the category/type** (section 4). Hardware goes in an instrument entry whose category
   says what the box does; analysis or acquisition software goes in a `software` entry; kits, dyes, resins, media
   and plasticware go in a `reagent` entry; the company itself goes in `companies.yaml`.
2. **Write the entry on one line** as a YAML flow mapping (`- {id: ..., name: ..., ...}`). `id` is the stable key
   stored in the database -- never rename an id. Give `vendor`, `ticker` (`private` or `n/a` when unlisted), and
   `parent` when a brand survived an acquisition. Quote names that contain `&`. One entry per model the archives
   keep apart (`NovaSeq X` and `NovaSeq X Plus` are two entries); give sibling models a shared `family` key when a
   roll-up line is useful.
3. **Add aliases as they appear in Methods**, including spelling variants, model numbers and the useful phrase forms
   (`Xenium spatial`, `MinION flow cell`, `KAPA HyperPrep`). Prefer phrase aliases over gating when recall matters.
4. **Gate anything risky.** If an alias is an English word (any case), a place, a surname, a 2-4 letter token or a
   name shared with another product, add it to `case_sensitive_aliases` and give the entry `context_terms` that
   name the vendor, the product family and the lab vocabulary that always surrounds a genuine use (`"flow cytometer"`,
   `"bioreactor"`, `"column"`). Entries whose every alias is risky use `context_required: true` instead.
5. **Never put a bare company name in an instrument entry** unless the vendor sells exactly one platform (the
   allow-list `SINGLE_PLATFORM_VENDORS` in `tests/test_gazetteer.py` documents the accepted cases). A supplier
   mention belongs to `companies.yaml`, where it is counted as a supplier, not as hardware.
6. **Run the gates**: `python3 -m litradar check-config` (duplicates, alias collisions, archive-model consistency),
   `python3 -m pytest -q -p no:warnings tests/test_gazetteer.py`, `python3 -m ruff check litradar tests`.
7. **Add regression cases** to `tests/fixtures/gazetteer_cases.yaml`: one sentence that must fire the entry and one
   look-alike that must not. The test fails loudly when either drifts.
8. If the entry is a sequencer, also add its SRA/ENA vocabulary to `config/archive_models.yaml` so the time series
   can count it.

After changing a gazetteer, stored mentions are stale until `python -m litradar rematch` re-runs the matcher over the
stored sections; the digest keeps showing the old hits until then.

## 3. Rules for ambiguous names

The three canonical cases, all enforced by tests:

* **`SOLiD`** (legacy ligation sequencer) collides with *solid tumour* and *SOLID evidence*. The entry is
  `context_required: true` with `case_sensitive_aliases: ["SOLiD", ...]` and context terms `Applied Biosystems`,
  `ABI`, `sequenc`, `ligation`. "Patients with solid tumors" never counts; "Applied Biosystems SOLiD 4 system" does;
  the archive string `AB SOLiD 3 Plus System` maps because archive matching ignores context.
* **`Chromium`** (10x single-cell controller) collides with the metal. The entry is `context_required: true` with
  `10x`, `Genomics`, `single-cell`, `GEM`, `scRNA`, `ATAC`, `Multiome` as context. "Chromium concentrations in drinking
  water" never counts; "loaded on the Chromium Controller (10x Genomics)" does.
* **`G4`** (Singular Genomics sequencer) collides with G-quadruplex DNA and a thousand other G4s. The alias is two
  characters, so the matcher makes it case-sensitive on its own; the entry adds `context_required: true` with
  `Singular` as the only context term. "The G4 quadruplex structure" never counts; "Singular Genomics G4 platform"
  does.

The same pattern now covers the aliases that leaked in the 2026-10-17 audit: `Sequel` (the first-generation PacBio
machine vs "the sequel to"), `Evident` (Olympus' new name vs the adjective), `ENrich` (Bio-Rad columns vs the verb and
the ENRICH scale), `TransIT` (Mirus transfection reagents vs transit time), `Juno` (Fluidigm IFC controller vs the egg
receptor), `Meso Scale` (vs *mesoscale* networks), `Harmony` and `Columbus` (Revvity software vs the scRNA
integration method and the city), `Typhoon`, `Criterion`, `Operetta`, `Attune`, `Octet`, `chameleon`/`apricot`
(SPT Labtech's lower-case product names), `Myriad`, `Recursion`, `Ginkgo`, `Schrödinger`, `WuXi` (camel case; the
city is Wuxi), `the Broad`, `Janssen` (a surname in citations), `Lilly` (a given name and a foundation), `deCODE`,
`Biogen` (vs the FBI BIOGEN imaging infrastructure), `Sartorius` (vs the muscle), `Anthropic`, `Profluent`.

Three practical rules follow:

1. **Dictionary sweep.** `test_dictionary_words_and_short_tokens_are_gated` loads `/usr/share/dict/words` and fails
   on any alias that is a plain dictionary word (<= 10 letters, hyphens/spaces/dots removed) or a 2-4 letter token
   unless it is case-sensitive, belongs to a `context_required` entry, or is auto-ambiguous. Tokens that contain a
   digit are exempt because the digit sequence is preserved whatever the case flag (`DS-11`, `Gen5`, `ZEN 3`).
2. **Phrase aliases rescue recall.** Gating `Xenium` costs the bare title mention; `Xenium spatial`, `Xenium
   platform`, `Xenium assay` ... are plain aliases that need no context, so the common forms still fire.
3. **COI vocabulary.** Gated pharma names (`Bayer`, `GSK`, `BMS`, `MSD`, `Vertex`, `Kite`, `Lilly`, `Biogen`,
   `Janssen`) carry conflict-of-interest words in `context_terms` (`honoraria`, `consulting`, `consultant`, `advisory`,
   `grants`, `fees`, `employee`, `speaker`, `stock`, `shareholder`, `research funding`, ...), so they fire inside the
   disclosure lists where they actually appear.

Known, accepted limitations: genericised trademarks (`Eppendorf tube`, `Transwell`, `Sepharose`, `Whatman paper`,
`Matrigel`) still credit their trademark owner; vendor analysis pipelines (`Cell Ranger`, `Space Ranger`) imply a
vendor's data but not that the instrument ran in the study, which is why they are software, not platforms.

## 4. Category vocabulary

Instrument `category` values and how the ranking treats them (`PLATFORM_CATEGORIES` in `litradar/rank/heuristics.py`):

| category | counts as | examples |
|---|---|---|
| `sequencer.short_read`, `sequencer.long_read` | **platform** | NovaSeq X, Revio, PromethION, DNBSEQ-T7, Saphyr (OGM reuses the long-read tier) |
| `single_cell` | **platform** | Chromium, Evercode, BD Rhapsody, PIPseq |
| `spatial` | **platform** | Visium, Xenium, CosMx, GeoMx, MERSCOPE, PhenoCycler, Cell DIVE, MACSima |
| `proteomics.affinity`, `proteomics.ms` | **platform** | Olink, SomaScan, NULISA, Simoa, Orbitrap Astral, timsTOF, SCIEX |
| `cryo_em` | **platform** | Titan Krios, Glacios, Falcon / K3 detectors, Leica EM sample prep |
| `array` | **platform** | Infinium, Axiom |
| `synthesis` | **platform** | Twist, IDT oligonucleotides, GenScript, DNA Script |
| `cell_engineering` | **platform** | MaxCyte ExPERT, Nucleofector, Neon, Mirus TransIT, Beacon, CliniMACS |
| `library_prep` | lab tool | NEBNext, KAPA, SureSelect, xGen, Covaris |
| `pcr` | lab tool | QuantStudio, ddPCR, CFX, LightCycler, Biomark |
| `cytometry` | lab tool | BD FACS, Cytek Aurora, CyTOF, CytoFLEX, MACSQuant |
| `imaging` | lab tool | Zeiss / Leica / Nikon / Olympus microscopes, Opera Phenix, IVIS, ChemiDoc, Typhoon, Aperio |
| `automation` | lab tool | liquid handlers, plate readers, histology instruments, electrophoresis gear, NMR, Biacore |
| `bioprocess` | lab tool | Ambr, HyPerforma, ÄKTA, BioFlo, Xcellerex, centrifuges |
| `qc` | lab tool | TapeStation, Bioanalyzer, Qubit, NanoDrop, cell counters |
| `software` | lab tool | FlowJo, ZEN, NIS-Elements, Cell Ranger, Imaris, DIA-NN, cryoSPARC, vendor control software; AI / foundation models used as tools (AlphaFold, ESMFold, RoseTTAFold, Boltz, Chai-1, BioNeMo, Parabricks, Evo 2, CELLxGENE) with the vendor set |
| `reagent` | lab tool | Lipofectamine, QIAGEN extraction kits, TaqMan / SuperScript, Cytiva resins, Corning plasticware, Gibco media |
| `method` | lab tool | academic protocols without a vendor (Slide-seq / Slide-seqV2 / Slide-tags); `vendor: open source`, `ticker: n/a` |

Two more fields shape how entries are *displayed*, never how they match: `family` (section 1, "Families") and
`granularity: unspecified`, which marks the per-vendor bucket for mentions that name a platform or chemistry but no
model. Those buckets are named consistently -- `Illumina (model unspecified)` (`ilmn-sequencing`), `NovaSeq (model
unspecified)` (`ilmn-novaseq`), `NextSeq (model unspecified)` (`ilmn-nextseq`), `Oxford Nanopore (model unspecified)`
(`ont-chemistry`), `PacBio (model unspecified)` (`pacb-hifi`), `MGI DNBSEQ (model unspecified)` (`mgi-dnbseq`) -- so the
digest's platform table and the dashboard read the same way; the chemistry entries `ilmn-5-base`, `ilmn-xleap-sbs` and
`mgi-coolmps` carry the flag under their own names. Ids are stable and the previous display names ("Illumina
sequencing", "NovaSeq", "Oxford Nanopore sequencing", "PacBio HiFi", "DNBSEQ") stayed on as aliases, so `rematch`
migrates stored mentions without a rename map: every row it re-derives takes the new display name. Rows that
`rematch` keeps because their source text is not stored under the data policy keep the old name until
`litradar maintenance --refresh-entity-names` rewrites them from the gazetteer (docs/store.md §4). `litradar show`
prints `family` and `granularity` in the identity block.

Platforms are the instruments an investor tracks; everything else is infrastructure or consumables and scores 0.2
per distinct entity. `software` and `reagent` exist precisely so that analysis software and consumables never
inflate a vendor's platform count.

Company `type` values: `tools`, `diagnostics`, `synthesis`, `ai_bio`, `pharma`, `cro`, `institute`, `software`.
`software` is for AI labs and model/pipeline vendors whose names appear as tools used in a paper rather than as
subjects (OpenAI, Anthropic, Google DeepMind / AlphaFold, NVIDIA, Microsoft Research); Isomorphic Labs stays `ai_bio`
because an Isomorphic affiliation *is* a subject. Every entry must carry a `type` (tested).

## 5. Changelog: precision pass of 2026-10-17

Starting point: instruments 335 entries / 2,652 aliases, companies 170 / 571. After the pass: instruments 404 entries /
3,662 aliases, companies 184 / 650. Full-suite tests, `check-config` (0 warnings) and ruff all green.

Dictionary sweep: the first run of the new test flagged 66 ungated aliases (46 instrument, 20 company); after the
pass 0 remain. Twenty-three of the original 66 carried a digit (`DS-11`, `Gen5`, `LLS7`, `ESM3`, ...) and are exempt
by rule; the other 43 were gated with `case_sensitive_aliases` + `context_terms` (`iSeq`, `RS II`, `xMAP`, `xGen`,
`Alt-R`, `KAPA`, `Watchmaker`, `Xenium`, `MinION`, `Exactive`, `Attune`, `STARlet`, `KingFisher`, `Costar`, `Juno`,
`Operetta`, `Pyxa`, `Xuri`, `Ambr`, `AKTA`/`ÄKTA`, `iQue`, `Octet`, `I-DOT`, `E.Z.N.A.`, `Gctf`, `ENrich`, `TransIT`,
`Evident`, `chameleon`, `apricot`, `Dako`, `Sartorius`, `Guardant`, `Myriad`, `Recursion`, `Ginkgo`, `Profluent`,
`Anthropic`, `deCODE`, `Biogen`, `WuXi`, `HHMI`, `EMBL`, `NYGC`, `NIBR`, `PGDx`, `JEOL`) or replaced by product
phrases (`PBS 3`, `Empyrean` dropped, bare `JEOL` moved to the company gazetteer).

### Confirmed findings applied

1. **PacBio generations.** New `pacb-sequel` entry (Sequel I, 2015, discontinued; `Sequel` case-sensitive with PacBio
   context); `pacb-sequel-ii` lost `Sequel`, `Sequel 2` and `PacBio Sequel`. Every `pacb-*` entry now carries
   `context_terms: [PacBio, Pacific Biosciences, SMRT, HiFi, CCS]`, so bare `Revio` fires next to "(PacBio, Menlo
   Park)". The `proposed` flag for `pacb-sequel` was dropped from `config/archive_models.yaml` and the two
   time-series tests that pinned it were updated.
2. **`Evident`** is case-sensitive with Olympus / Evident Scientific / microscope context; `Evident Scientific` added.
3. **`ENrich`** is case-sensitive with Bio-Rad / SEC / NGC / column context; `ENrich SEC`, `ENrich Q`, `ENrich S` added.
4. **`TransIT`** is case-sensitive (with the existing Mirus / transfection context); `TransIT-mRNA`, `-Lenti`,
   `-siQUEST`, `-TKO`, `-293` added.
5. **Software never credits hardware.** FlowJo / FACSDiva / FACSuite / FACSChorus -> `bd-flowjo`; CytExpert /
   Kaluza -> `beckman-cytexpert`; SpectroFlo -> `cytek-spectroflo`; NIS-Elements -> `nikon-nis-elements`; ZEN ->
   `zeiss-zen`; cellSens -> `evident-cellsens`; Harmony / Columbus / Signals Image Artist -> `revvity-harmony`;
   MetaXpress / MetaMorph / IN Carta / SoftMax Pro / pCLAMP -> `molecular-devices-software`; Imaris -> `andor-imaris`;
   Image Lab / QuantaSoft / QX Manager / CFX Maestro / ChromLab -> `biorad-software`; Cell Ranger / Space Ranger /
   Loupe -> `txg-software`; Dorado / Guppy / Bonito / MinKNOW / EPI2ME / Medaka / Remora / modkit -> `ont-software`;
   plus `bionano-software`, `bgi-stereo-seq-software`, `bruker-atomx`, `akoya-inform`, `luminex-xponent`,
   `waters-software`, `agilent-software`, `shimadzu-software`, `sciex-software`, `standardbio-software`, `cytek-ideas`,
   `miltenyi-software`, `thermo-epu`, `gatan-digitalmicrograph`, `leica-las-x`, `3dhistech-caseviewer`,
   `licor-software`, `azure-software`, `hamilton-venus`, `tecan-software`, `thermo-automation-software`,
   `biotek-gen5`, `agilent-novoexpress`, `bruker-topspin`, `cytiva-unicorn`, `waters-astra`, `qiagen-software`.
6. **Reviewer-flagged dictionary words** fixed in the same way: `Juno`, `chameleon`/`apricot`, `New Brunswick` (only
   `New Brunswick Scientific` / `Innova` forms remain), `PBS 3` (entry is now `context_required` with product
   phrases), `Quick-DNA` (full Zymo product names only), Octet (own `sartorius-octet` entry, `Octet` case-sensitive).

### Unverified findings evaluated

Applied: Leica company aliases split out of the confocal entry (`leica-aperio`, `leica-bond`, `leica-histology`,
`leica-cell-dive`, `leica-las-x`; `leica-em-cryo` extended); `the Broad` gated; `Myriad` gated; IDT context on the
Danaher entry; `New Brunswick`; `Juno`; `chameleon`/`apricot`; `PBS 3`; `Quick-DNA`; Octet split; generic
`ilmn-sequencing` entry; `Recursion`, `Ginkgo`, `Schrödinger` (and `Maestro software` -> `Schrödinger Maestro`),
`WuXi` gated; COI vocabulary added to every gated pharma entry.

Rejected or out of scope: the "run `rematch` / backfill archive rows" item is a pipeline operation, not a gazetteer
change (it still has to be run after this pass); the "change `_is_ambiguous` in code" suggestion is replaced by the
dictionary-sweep test, because the matcher module is out of scope for a config pass and the test catches the same
class of alias at review time.

### Live audit (5 random snippets for the 60 most-mentioned instruments and 40 companies)

Beyond the reviewer's list the audit found and fixed: bare `Invitrogen` / `Sorvall` crediting the HyPerforma bioreactor
(100 % of its sample), `Whatman` / `Sepharose` crediting ÄKTA, `PerkinElmer` ICP-OES and radiochemicals crediting Opera
Phenix, `Molecular Devices` plate readers crediting ImageXpress, `Tecan` plate readers crediting Fluent, `Miltenyi`
dissociation kits crediting MACSQuant, `Affymetrix` antibodies crediting Axiom, `Diagenode` antibodies crediting the
Bioruptor, `Leica Biosystems` stainers and cryostats crediting STELLARIS, PANalytical diffractometers crediting
Zetasizer, `BioTek` matching *Norgen Biotek*, `Meso Scale` matching *mesoscale*, `Advanced Analytical` matching
"advanced analytical platforms", `Applied Biosystems` / `SuperScript` / `TaqMan` reagents crediting QuantStudio,
`Fluidigm` crediting Biomark on CyTOF papers, `Cytek` crediting Aurora on Amnis papers. Each was resolved by moving
the alias to the company gazetteer, to a new `reagent` / `software` / infrastructure entry, or by gating. Companies:
`Janssen et al.` citations, `decode`, `Lilly & Blair Foundation`, `FBI BIOGEN`, `the broad range`, `Ginkgo biloba`,
`Schrödinger equation`, `Wuxi` (city), `Novo Nordisk Fonden` (funder, now its own institute entry), and
`progenomes3.embl.de` URLs were all firing; `Fisher Scientific` (140 of 866 stored Methods) was missing from the
Thermo Fisher entry.

### Company gazetteer

`type: software` for OpenAI, Anthropic, Microsoft Research, NVIDIA and the new `google-deepmind` entry (DeepMind /
AlphaFold / AlphaGenome / Google Research aliases moved there); `isomorphic` keeps `ai_bio`. SomaLogic moved from
Standard BioTools to Illumina (sold 30 Jan 2026). Added from the live Methods frequency census (the stored
`unlisted:` company-type institutions table was empty in the audited database): Hologic (HOLX, parent of
Diagenode), Vazyme (688105.SS), Sino Biological (301047.SZ), Proteintech, Santa Cruz Biotechnology, Beyotime,
Solarbio, MedChemExpress, ABclonal, Servicebio, Majorbio (CRO), Addgene (institute), Novo Nordisk Foundation
(institute); `Vital River` added to Charles River; `Olympus`, `Nikon`, `Leica`, `Beckman`, `Hitachi`, `Cepheid`
added as gated company aliases.

### Entries renamed (ids unchanged)

`zeiss-lsm` -> "Zeiss microscopes", `leica-stellaris` -> "Leica microscopy", `nikon-ax` -> "Nikon microscopy",
`andor-dragonfly` -> "Andor imaging", `sciex-zenotof` -> "SCIEX mass spectrometry", `malvern-panalytical` ->
"Malvern Panalytical biophysical instruments", `hologic-diagenode` -> "Diagenode Bioruptor", `repligen` ->
"Repligen bioprocess", `watchmaker` -> "Watchmaker library prep", `leica-em-cryo` -> "Leica EM sample preparation",
`merck-millipore` -> "Merck Millipore bioprocess & water", `aldevron` / `revvity-horizon` / `cellecta` / `synthego`
re-categorised from `cell_engineering` to `reagent` (they are reagent suppliers, not platforms).

### Measured effect on the audited corpus

Re-matching the 3,562 stored papers (866 with section text) with the old and the new gazetteers, counting papers per
entity:

| entity | before | after | why |
|---|---|---|---|
| `thermo-hyperforma` (bioreactor) | 41 | 0 | all 41 were `Invitrogen` / `Sorvall` reagent and centrifuge mentions |
| `biorad-ngc` (chromatography) | 42 | 1 | ChemiDoc / blot gear split out; `enrich` gated |
| `evident-microscopy` | 49 | 27 | the 22 lost hits were the English word |
| `thermo-quantstudio` | 42 | 21 | TaqMan / SuperScript / Applied Biosystems reagents moved to `thermo-molbio-reagents` (90) |
| `revvity-opera` | 23 | 2 | ICP, radiochemicals and IVIS no longer credit a high-content imager |
| `cytiva-akta` | 22 | 4 | Whatman / Sepharose / Superdex now consumables |
| `bd-facs` | 38 | 23 | FlowJo (25 papers) moved to `bd-flowjo` |
| `mirus` | 11 | 0 | every hit was *transit* |
| `miltenyi-macsquant`, `tecan-fluent`, `molecular-devices-imagexpress` | 14 / 12 / 16 | 0 / 0 / 0 | dissociation kits, plate readers and SpectraMax readers now in their own entries |
| `ilmn-sequencing` (new) | 0 | 26 | papers that name only "Illumina platform / sequencing" |
| `pacb-sequel` (new) | 0 | 5 | first-generation Sequel separated from Sequel II |
| `isomorphic` / `google-deepmind` | 35 / 0 | 0 / 41 | AlphaFold credits moved to the software-type entry |
| `novo-nordisk` / `novo-nordisk-foundation` | 14 / 0 | 6 / 8 | grants no longer credited to the company |
| `broad-institute`, `amgen` | 23, 20 | 15, 13 | *the broad range*, *to decode* no longer count |
| `danaher`, `thermo-fisher`, `evident`, `nikon` (companies) | 83, 220, 4, 3 | 103, 230, 32, 20 | `IDT` now fires; `Fisher Scientific`, `Olympus`, `Nikon`, `Leica` added |

Distinct instrument entities with at least one hit went from 202 to 249 (the splits), company entities from 109 to
119. Gating cost no true positive in the sampled snippets for HHMI, EMBL, JEOL or Sartorius after the context terms
were extended to affiliation / funding vocabulary.

### Known follow-ups (not config)

* Run `python -m litradar rematch` and re-annotate `archive_records` so stored mentions and the archive census pick
  up the new entries; until then the digest shows the pre-pass hits.
* `twist-dx-dup` is the id of the Burning Rock entry; ids are stable keys, so it was left alone.

## 6. Changelog: hygiene pass of 2026-10-17 (deferred shakedown items)

Starting point: instruments 404 entries / 3,663 aliases, companies 211 / 838. After the pass: instruments 407 /
3,676, companies 211 / 838. `check-config` 0 warnings (archive_models 28 entities / 49 model strings); full suite
and ruff green.

**Model granularity.** `ilmn-novaseq-x` was one entry whose aliases swallowed `NovaSeq X Plus`, while the archive
census kept "Illumina NovaSeq X" (5,221 experiments in the audited store) and "Illumina NovaSeq X Plus" (6,023) apart,
so the paper table and the archive table used different definitions. Now two entries, `ilmn-novaseq-x` and
`ilmn-novaseq-x-plus`, with `family: novaseq-x`; `config/archive_models.yaml` splits the SRA/ENA strings the same way
(the Europe PMC term "NovaSeq X" is prefix-inclusive and still counts Plus papers; the yaml notes say so). Of the 15
stored NovaSeq X Methods mentions, 13 were Plus matches. `family` was also added to the Sequel generations, the
PromethION entry, the four Chromium assays and the NextSeq 1000/2000 pair (the shared alias "NextSeq 1000/2000" is
credited to the 2000; the family roll-up shows both).

**Bundled product lines split.** `Ceta` / `Ceta-D` (TEM cameras) moved from the Falcon direct-electron-detector
entry to `thermo-ceta-camera` (category `imaging`, case-sensitive + TEM/camera/kV context): 3 of the 4 live
"Falcon" hits were Ceta matches, one of them a Gatan K3 line. `ImageQuant` (software and LAS/800 CCD imagers) moved
from `cytiva-typhoon` to `cytiva-imagequant` (category `software`): 2 of 3 live "Typhoon" hits were ImageQuant
software credits.

**Matcher.** Web and e-mail addresses are skipped and Unicode hyphens are word boundaries (section 1). Found by the
live sweep of the 40 most-mentioned instruments and 40 companies (6 random snippets each, the method of section 5):
`University Medical Center Hamburg‐Eppendorf` (U+2010) credited the company Eppendorf; `gnomad.broadinstitute.org`,
`github.com/broadinstitute`, `software.broadinstitute.org` credited the Broad Institute as a Methods mention; Addgene
URLs did the same. No other false positive was found in the 480 snippets; the remaining judgement calls are the
known limitations (genericised trademarks `Transwell`, `Matrigel`, `Eppendorf tube`, `Milli-Q`; vendor-labelled
consumables such as `iTaq Universal` credited to the CFX instrument; a paper that mislabels the ABI 7500 as a
"Sciex" instrument).

**Tickers verified against the SEC.** Every US-style ticker in `companies.yaml` (87 entries; exchange-suffixed
tickers such as `ONT.L`, `688114.SS`, `ROG.SW` are outside the SEC file and were not checked) was compared with
`https://www.sec.gov/files/company_tickers.json` (fetched 2026-10-17 with a declared User-Agent; the download is
refused without one). 85 match the company or its parent. Two no longer exist in the file and were changed:
`hologic` `HOLX` -> `private` (taken private; deal announced Oct 2025) and `exact-sciences` `EXAS` -> `ABT` with
`parent: Abbott` (acquired; deal announced Nov 2025; `ABT` = ABBOTT LABORATORIES in the file). `SDGR` was flagged by
the name comparison only because of the umlaut (Schrödinger vs "Schrodinger, Inc.") and is correct.

**Findings from a rematch on an 89,165-paper backfilled copy** (reported by the pipeline shakedown, folded in here):

* `google-deepmind` was the #1 "company" with 2,425 papers, 100 % from model aliases (AlphaFold/abstract 999,
  AlphaFold/title 469, AlphaFold 2/abstract 413, AlphaFold 3/abstract 363, ...). A model name in a paper is tool
  usage, not company involvement. Every AI / foundation model name moved out of `companies.yaml` into `software`
  entries of `instruments.yaml` with the vendor set: `deepmind-alphafold` (AlphaFold 2/3, Multimer, the AlphaFold
  Database, AlphaMissense, AlphaProteo), `deepmind-alphagenome`, `meta-esm` (ESMFold, ESM-2), `evolutionaryscale-esm3`,
  `rosettafold` (RoseTTAFold, RFdiffusion, ProteinMPNN), `boltz`, `chai-discovery-models`, `nvidia-bionemo`,
  `nvidia-parabricks`, `microsoft-bioemu`, `microsoft-evodiff`, `profluent-models`, `generate-chroma`, `latent-x`,
  `bioptimus-h-optimus`, `arc-evo`, `arc-virtual-cell-atlas`, `czi-cellxgene`, `czi-transcriptformer`. The company
  entries keep the organisation's names only (`google-deepmind`: DeepMind, Google Research; `nvidia`: NVIDIA, NVIDIA
  Corporation -- its 65 affiliation + 46 funding mentions are real company involvement and stay). Rule for the
  future: a commercial *product* a lab buys (Schrödinger Maestro, Olink panels, Illumina kits) stays a supplier alias
  on the company; a *model* anyone can run is a software tool.
* `curio-seeker` (75 papers) was 84 % Slide-seq / Slide-seqV2 / Slide-tags mentions, i.e. the academic method, with
  "Curio Seeker" itself in about two. New `slide-seq` entry (category `method`, no vendor); `curio-seeker` keeps the
  commercial kit names only.
* The per-vendor "no model named" buckets (`ilmn-sequencing` 483 papers, `ont-chemistry` 1,683, `pacb-hifi` 1,155 on
  that copy) were named three different ways. They now share the `(model unspecified)` naming and the
  `granularity: unspecified` flag (section 4), ids unchanged.

Measured by re-matching the 3,562-paper live copy (papers per entity, before -> after): `google-deepmind` 41 -> 2
(the two remaining are a Google Research affiliation and a DeepMind acknowledgement); `deepmind-alphafold` 0 -> 37,
`deepmind-alphagenome` 0 -> 3, `meta-esm` 0 -> 9, `boltz` 0 -> 7, `chai-discovery-models` 0 -> 4, `rosettafold` 0 -> 3,
`arc-evo` 0 -> 2, `czi-cellxgene` 0 -> 2, `evolutionaryscale-esm3` / `microsoft-bioemu` 0 -> 1 each; `nvidia` 9 (unchanged:
all organisation mentions), `chai-discovery` / `evolutionaryscale` / `microsoft-research` 0 as companies; `slide-seq`
0 -> 1 and `curio-seeker` 0 (the live copy has no Curio kit paper). Distinct instrument entities 254 -> 265, company
entities 127 -> 124; total mention rows 4,435 -> 4,458. Role split after the pass: 3,845 `used`, 573 `mentioned`, 40
`reanalysis`.

**Not changed here.** The ticker *hint* threshold in the discovery code (the 0.91 "Arda" vs "Aprea Therapeutics" case)
and the concepts stop-list live in `litradar/discovery.py` and `litradar/concepts.py`, outside this pass. The CELLxGENE
archive adapter (`litradar/sources/cellxgene.py`) still maps the EFO assay term "Slide-seqV2" to `curio-seeker`; with
the new `slide-seq` entry the adapter owner may want to point it there.
