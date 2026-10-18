# Companies: profiles, export, co-mention graph, and the vetted watchlist

The gazetteer (`config/companies.yaml`) says which companies exist; the `mentions` table says where they
appear. `litradar.companies` joins the two: one company at a time (`show`, the dashboard profile page), every
company at once (`list`, `export`), and the pairs that share papers (`graph`). It reads `papers`, `mentions`,
`news_items` and, when present, `candidate_companies`; it never writes.

```
litradar companies list [--type pharma] [--public | --private] [--since 90d] [--limit 50] [--json]
litradar companies show <id|name|alias|ticker> [--since 90d] [--json]
litradar companies export [--csv companies.csv] [--since 90d]
litradar companies graph [--out companies.json] [--since 365d] [--min-weight 1] [--include-unlisted] [--pretty]
```

## Roles and windows

A mention is recorded with the section it was found in; the section decides the **role** the company plays in
that paper:

| Role | Sections | Reading |
|---|---|---|
| `author` | `affiliations` | a company author |
| `funding_coi` | `funding`, `coi`, `ack` | sponsor, employer or consulting relationship declared |
| `subject` | `title`, `abstract` | the paper is about the company or its product |
| `supplier` | `methods` | the company's product was used |
| `other` | anything else (`body`, `data_availability`) | |

Counts are **distinct papers**: a paper naming a company in its Methods and in a competing-interest statement
is one paper, counted once under `supplier` and once under `funding_coi`. `mentions` is the summed occurrence
count. A paper is dated by `pub_date`, else by the date part of `first_seen`: the rule the digest and the
dashboard already use, so no surface disagrees about which papers fall in a window. Windows are inclusive and
end today: `list` and `export` default to 90 days, `show` prints 90 and 365 days, `graph` defaults to 365.

`public` means the gazetteer ticker names a listing; `private`, `n/a` and the other placeholders do not.
A subsidiary carries its parent's ticker and a `parent` field.

## The command

**`list`** prints one row per gazetteer entry, most papers first, with the four role counts, the news count
(items in `news_items` whose `companies_json` carries the entry's id or display name) and the number of
discovery candidates that resolve to the entry. `--type` keeps one gazetteer type, `--public` / `--private`
split on the ticker. Companies without a paper in the window are listed with zeros so the whole watchlist is
visible.

**`show`** resolves `<id|name|alias|ticker>` case-insensitively (a unique substring of an id or name also
works; an ambiguous one is refused with the matches) and prints: domains and aliases; the 90- and 365-day
windows (papers, mentions, roles, news); papers per month over the last 24 months; the gazetteer instruments and
the companies named in the same papers, by shared papers; the newest papers with the roles the company plays in
each; the newest news items; and the rows of `candidate_companies` that are this company under another
spelling (same normalised name as the name or an alias, a shared e-mail domain, or a promotion that produced
this id). `--json` returns the same structure as a dict (`companies.company_detail`).

**Coverage caveat.** Methods, funding and affiliation mentions exist only for papers whose full text or rich
metadata was harvested, while title and abstract mentions exist for every source. A month harvested with full
text next to an abstract-only backfill month therefore shows a step that is coverage, not adoption. Each row of
the monthly series carries `subject` (distinct papers naming the company in the title or abstract: the
like-for-like series) next to `papers`, and `company_detail` adds `coverage`: `null` when the section mix is flat,
else a `note`, the `basis` (`title/abstract`) and `deep_share` (per month with at least 5 papers, the share of
papers matched only outside the title/abstract). The note fires when that share differs by 25 points or more
between months; `show` then prints a second `by month (title/abstract only)` line and the note.

**`export`** writes one CSV row per company with the 90-day counts. The header is fixed
(`companies.EXPORT_COLUMNS`): `id, name, ticker, public, type, parent, papers_90d, mentions_90d, author_90d,
funding_coi_90d, subject_90d, supplier_90d, other_90d, news_90d, candidates_linked, last_paper, domains,
aliases` (`aliases` is the count, `domains` space-joined). Without `--csv PATH` the CSV goes to stdout.

**`graph`** writes the co-mention graph: `{generated, window, min_weight, nodes: [{id, name, ticker, type,
papers, degree}], links: [{source, target, weight}]}` where `weight` is the number of distinct papers naming
both companies in the window. Companies with no shared paper are left out (they would be isolated dots);
`unlisted:` institutions are left out unless `--include-unlisted`. This is the file the dashboard draws.

On a one-week store (3,562 papers; 2,494 company mentions across 176 companies after the vetting pass below and
a rematch; 264 entries) `list` takes 0.1 s, `show` 0.01 s and `graph` 0.02 s.

## Dashboard pages

`litradar dashboard` now also writes:

* **`companies/<id>.html`** for every company with at least `COMPANY_PAGE_MIN_PAPERS` (3) distinct papers in
  the dashboard window: papers and mentions per month over 24 months (chart plus table), the role table for the
  last 90 and 365 days, the newest papers with their roles, co-mentioned instruments, co-mentioned companies
  (linked when they have a page of their own), news, and, when any exist, the discovery candidates that resolve
  to the company. `companies.html` links to every profile from a "Company profiles" card and from a `Profile`
  tag on each company card.
* **`companies/graph.html`**: a force-directed drawing of the co-mention graph. Node size follows papers in the
  window, colour follows the gazetteer type (the legend is built from the data), edge width follows shared
  papers; drag to rearrange, scroll to zoom, click a node to open its profile. The filter card sets the minimum
  shared papers (the default is chosen so about 400 edges are drawn: 5 on the store above) and how many of the
  largest companies to include. The same pairs and companies are below the drawing as tables, so nothing is
  reachable only by hovering. The graph JSON is also written to `data/companies-graph.json`.

The graph uses **d3 v7.9.0**, pinned with a Subresource Integrity hash and `crossorigin="anonymous"` from cdnjs
(the same bytes are on jsdelivr: `https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js`, 279,706 bytes):

```
https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js
sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i
```

If the library is blocked the page says so and the tables stand; the profile pages use the Chart.js build the
rest of the site already pins. Everything written server-side is HTML-escaped and the inline JSON payload
cannot close its own script tag (the same rules as the other pages).

Sizes, measured on the one-week store after the vetting pass (52-week build): **117 files, 4,758,646 bytes**.
100 profile pages from 6,625 to 20,107 bytes (median 13,802; 1,299,829 bytes together), `companies/graph.html`
191,631 bytes and `data/companies-graph.json` 188,303 bytes for 170 companies and 2,638 pairs,
`companies.html` 51,188 bytes. For comparison `search.html` is 1,300,595 bytes and `instruments.html` 158,241.
The company pages add about 1.6 MB to a 3.1 MB site; the build still takes under two seconds.

## Library

| Function | Returns |
|---|---|
| `load_entries(settings_or_yaml)` / `normalize_entry(raw)` | gazetteer entries with every field typed (`public` derived from the ticker) |
| `resolve_entry(entries, text)` | entry by id, name, alias, ticker or unique substring; `LookupError` when ambiguous |
| `entries_from_store(conn)` | minimal entries for every company id in `mentions` (what the dashboard uses: it reads only the database) |
| `paper_counts(conn, since, until)` | per company id: distinct papers, summed mentions, distinct papers per role, last paper date |
| `news_counts(conn, since, until)` | news items per `companies_json` key |
| `list_companies(conn, entries, since, until, type_=None, public=None)` | one row per entry, sorted |
| `monthly_series(conn, entity_id, months)` | per month: distinct `papers`, summed `mentions`, distinct `subject` papers (title / abstract) |
| `coverage_note(monthly)` | the coverage caveat above, or None when the monthly section mix is flat |
| `company_detail(conn, entry, today=None, windows=(90, 365), months=24)` | the `show` / profile-page structure (`windows`, `monthly`, `coverage`, co-mentions, recent papers, news, candidates) |
| `export_rows(conn, entries, since, until)` / `write_csv(rows, fh)` | the CSV |
| `comention_graph(conn, since, until, entries=None, min_weight=1, include_unlisted=False, keep_isolated=False)` | nodes and weighted links |
| `sec_ticker_proposals(entries, tickers)` | exact-brand SEC matches for entries whose ticker is a placeholder |
| `render_vetting_table(rows)` | the Markdown table below |

Every function takes a `Store` or a raw `sqlite3.Connection`.

## Vetting pass: 77 candidates at the default threshold (2026-10-17)

`litradar candidates refresh --since 90d` on a one-week store produced 115 candidates, 77 of them at the default
`--min-score 2`. Each was checked against the open web (company site, registry or exchange filings, trade
press) for sector, listing status, ticker and exchange, HQ country, what the company does and its web domain.
Outcome:

* **55 life-science companies** landed: 53 new gazetteer entries, plus 2 e-mail-domain candidates that are the
  web domains of two of those entries and were folded into them. 18 of the 53 carry a listing ticker (their own
  or, for three subsidiaries, their parent's); the rest are `private`. Every entry carries `watch_news: false`, the domains and spellings seen in the papers as
  aliases, and a one-line `notes` field (what the company does, HQ, exchange, and anything that matters such
  as a rename, an insolvency or a parent). `check-config` after the append: 264 entries, 1,023 aliases,
  duplicates none; `litradar rematch --since 90d` then found papers for 49 of the 53 (the other four came from
  press headlines, not from papers).
* **17 off-sector names** ignored: five tobacco entities (two cigarette makers, a provincial tobacco company
  and its branch, two of their e-mail domains), three farms / agricultural companies (aquaculture, seed,
  state farm), a poultry-genetics breeder, a functional-food contract manufacturer, a food group's research
  subsidiary, a feed-and-food research competence centre, an engineering contractor, a quantum-computing
  company, a materials-science instrument maker, a digital-health app maker and an eye hospital.
* **5 left `new`** for want of evidence: no web footprint under the name in English or Chinese (or Korean).
  They stay in the candidate table and will resurface in the digest; the notes say what was tried.

Types were assigned as the gazetteer defines them: `pharma` for any therapeutics developer, `tools` for
instruments and reagents, `cro` for services (including a medical-communications agency, a PK/PD consultancy
and sequencing service labs), `ai_bio` for AI-first discovery companies, `diagnostics` for a diagnostic AI
vendor, `software` for a general-technology research arm whose foundation models appear in biology papers (the
same class as the AI labs already listed).

| candidate | verdict | name | type | ticker | hq | what | domain | action |
|---|---|---|---|---|---|---|---|---|
| 1stBiome ApS | life_science | 1stBiome | pharma | private | DK | Copenhagen gut-microbiome platform engineering beneficial microbes against low-grade inflammation | 1stbiome.com | added as `1stbiome` |
| Attoquant Diagnostics | life_science | Attoquant Diagnostics | cro | private | AT | Vienna LC-MS/MS peptide-quantification service lab (RAS-Fingerprint angiotensin / bradykinin assays) | attoquant.com | added as `attoquant-diagnostics` |
| Lactiga US Inc. | life_science | Lactiga | pharma | private | US | Secretory-IgA mucosal antibody therapies derived from pooled human milk (lead LCTG-001 for CVID) | lactiga.com | added as `lactiga` |
| Arda Therapeutics | life_science | Arda Therapeutics | pharma | private | US | Single-cell-guided discovery of pathogenic cells and cell-depleting biologics (fibrosis, autoimmune, metabolic) | ardatx.com | added as `arda-therapeutics` |
| Interdict Bio | life_science | Interdict Bio | pharma | private | US | Seed-stage small molecules that drug undruggable proteins through context-selective modulation of translation | interdictbio.com | added as `interdict-bio` |
| Relay Therapeutics | life_science | Relay Therapeutics | pharma | RLAY | US | Clinical-stage precision-medicine biotech; motion-based computational platform for small molecules | relaytx.com | added as `relay-therapeutics` |
| Soufflé Therapeutics | life_science | Soufflé Therapeutics | pharma | private | US | RNA therapeutics; receptor-discovery engine for cell- and tissue-targeted siRNA delivery (FSHD, heart failure) | souffletx.com | added as `souffle-therapeutics` |
| Costello Medical | life_science | Costello Medical | cro | private | GB | Independently owned medical-communications and HEOR / market-access agency (publications, economic models, RWE) | costellomedical.com | added as `costello-medical` |
| IBM Research | life_science | IBM Research | software | IBM | US | Research division of IBM; its biomedical foundation-model group publishes open bio FMs (BMFM-RNA, DNA and drug models) | ibm.com, research.ibm.com | added as `ibm-research` |
| Ipsen | life_science | Ipsen | pharma | IPN.PA | FR | Global mid-cap biopharma (oncology, rare disease, neuroscience); hepatology franchise incl. elafibranor and odevixibat | ipsen.com | added as `ipsen` |
| Société des Produits Nestlé SA | off_sector | Société des Produits Nestlé |  | NESN.SW | CH | Operating / IP subsidiary of Nestlé; employer of record for Nestlé Research (food, nutrition and health-science R&D); food and consumer-goods group; ignored (re-promote if nutrition science comes into scope) | nestle.com | ignored |
| 3P Instruments GmbH | off_sector | 3P Instruments |  | private | DE | Particle, powder and pore analyzers: BET gas sorption, porosimetry, laser diffraction (formerly Quantachrome GmbH); materials-science / analytical-chemistry instrumentation, not life-science tools; ignored | 3p-instruments.com | ignored |
| AHEAD Medicine Corporation | life_science | AHEAD Medicine | diagnostics | private | US | AI software for automated clinical flow-cytometry analysis (hematologic malignancies, cell-therapy QC) | aheadmedicine.com | added as `ahead-medicine` |
| Actinium Pharmaceuticals | life_science | Actinium Pharmaceuticals | pharma | ATNM | US | Clinical-stage targeted radiotherapy (antibody-radioconjugates Iomab-B, Actimab-A) | actiniumpharma.com | added as `actinium-pharmaceuticals` |
| Akamis Bio | life_science | Akamis Bio | pharma | private | GB | Clinical-stage oncology biotech (formerly PsiOxus Therapeutics); tumour-selective immuno-gene adenoviral platform | akamisbio.com | added as `akamis-bio` |
| Alfasigma Laboratories | life_science | Alfasigma | pharma | private | IT | Family-owned Bologna pharma group (GI, hepatology, vascular, rheumatology; Rx and OTC) | alfasigma.com | added as `alfasigma` |
| Arcus Biosciences | life_science | Arcus Biosciences | pharma | RCUS | US | Clinical-stage cancer immunotherapy (HIF-2a and anti-TIGIT programmes; Gilead partnership) | arcusbio.com | added as `arcus-biosciences` |
| Augment Biologics | life_science | Augment Biologics | ai_bio | private | US | Pre-seed AI-guided glycosylation design for recombinant protein therapeutics | augmentbiologics.com | added as `augment-biologics` |
| Azkarra Therapeutics | life_science | Azkarra Therapeutics | pharma | private | US | Small-molecule DNA-damage-response modulators (multi-PARP, PARG, OGG1 inhibitors) for cancer and inflammation | azkarrabio.com | added as `azkarra-therapeutics` |
| Biodepot LLC | life_science | Biodepot | software | private | US | Containerised drag-and-drop bioinformatics workflow platform (local, cluster or cloud); NCI SBIR contractor | biodepot.io | added as `biodepot` |
| BlueDolphin LLC | life_science | Blue Dolphin Lead Discovery | cro | private | US | Fee-for-service virtual screening, docking and library design for small drug developers |  | added as `blue-dolphin-lead-discovery` |
| BridgeBio Oncology Therapeutics | life_science | BridgeBio Oncology Therapeutics | pharma | BBOT | US | Clinical-stage small-molecule oncology (RAS ON-state and PI3Kα-driven cancers) | bbotx.com | added as `bridgebio-oncology-therapeutics` |
| CaniCatiCare, Inc. | unsure |  |  |  |  | no web record under this name or variants; the context is a companion-animal paper, so likely off-sector anyway; left as new |  | left `new` (unsure) |
| Cellarcus Biosciences | life_science | Cellarcus Biosciences | tools | private | US | Vesicle Flow Cytometry (vFC) assay kits, probes and antibodies for single extracellular-vesicle analysis, plus EV services | cellarcus.com | added as `cellarcus-biosciences` |
| Celldex Therapeutics | life_science | Celldex Therapeutics | pharma | CLDX | US | Clinical-stage antibody biotech; anti-KIT antibody in Phase 3 for chronic spontaneous urticaria | celldex.com | added as `celldex-therapeutics` |
| Chi Tian Hua Biotechnology Co. | unsure |  |  |  | CN | no entity of this name found in English or Chinese; the pinyin matches a fertilizer / coal-chemical / medical-services group (600227.SS), which would be off-sector; left as new |  | left `new` (unsure) |
| China Tobacco Hunan Industrial Co., Ltd. | off_sector | China Tobacco Hunan Industrial |  | private | CN | State-owned provincial cigarette manufacturer; tobacco; ignored |  | ignored |
| China Tobacco Sichuan Industrial Co., Ltd. | off_sector | China Tobacco Sichuan Industrial |  | private | CN | State-owned provincial cigarette and cigar manufacturer; tobacco; ignored |  | ignored |
| City Therapeutics | life_science | City Therapeutics | pharma | CTY | US | Clinical-stage RNAi company engineering next-generation siRNAs | citytx.com | added as `city-therapeutics` |
| Cobb-Vantress, Ltd. | off_sector | Cobb-Vantress |  | TSN | US | Pedigree broiler breeding company (poultry genetics); agriculture; wholly owned by Tyson Foods; ignored | cobbgenetics.com | ignored |
| Deep Apple Therapeutics, Inc | life_science | Deep Apple Therapeutics | ai_bio | private | US | Deep-learning and cryo-EM structure-based GPCR small-molecule discovery (inflammatory, immune, metabolic disease) | deepappletx.com | added as `deep-apple-therapeutics` |
| EISAI Inc. | life_science | Eisai | pharma | 4523.T | JP | Japanese pharma (oncology: lenvatinib; neurology: lecanemab); Eisai Inc. is the US commercial and R&D subsidiary | eisai.com, eisai.co.jp | added as `eisai` |
| Elgia Therapeutics | life_science | Elgia Therapeutics | pharma | private | US | Seed-stage first-in-class small molecules for chronic inflammatory and fibrotic diseases | elgiatherapeutics.com | added as `elgia-therapeutics` |
| Enhanced Pharmacodynamics, LLC | life_science | Enhanced Pharmacodynamics | cro | private | US | PK/PD, pharmacometrics and quantitative-systems-pharmacology consultancy for model-informed drug development |  | added as `enhanced-pharmacodynamics` |
| Epiodyne Inc | life_science | Epiodyne | pharma | private | US | Small-molecule opioid-receptor (biased mu-agonist) analgesics and opioid-use-disorder medicines; academic spin-out | epiodyne.com | added as `epiodyne` |
| Falk Pharma | life_science | Dr. Falk Pharma | pharma | private | DE | Family-owned gastroenterology / hepatology specialty pharma (budesonide, ursodeoxycholic acid) | drfalkpharma.de, drfalkpharma.com | added as `dr-falk-pharma` |
| GEn1E Lifesciences | life_science | GEn1E Lifesciences | pharma | private | US | Clinical-stage (Phase 2) p38α:MK2 signal modulator for ARDS and inflammatory disease | gen1e.com | added as `gen1e-lifesciences` |
| Hebei Yuanzhang Aquaculture Co., Ltd. | off_sector | Hebei Yuanzhang Aquaculture |  | private | CN | Aquaculture operator (fish / seafood farming); no web record; verdict from the name and the aquaculture-paper context; ignored |  | ignored |
| Inner Mongolia Xinyu Seed Industry Co., Ltd. | off_sector | Inner Mongolia Xinyu Seed Industry |  | private | CN | Crop seed company (Inner Mongolia); agriculture; no exact web match; ignored |  | ignored |
| Innovation FFoQSI GmbH | off_sector | FFoQSI |  | private | AT | Austrian COMET K1 research competence centre for feed and food quality, safety and innovation (Tulln); publicly funded research centre in GmbH form, food / feed sector; the scanner cut the name at 'Innovation'; ignored | ffoqsi.at | ignored |
| Interline Therapeutics | life_science | Interline Therapeutics | pharma | private | US | Mapping and correcting dysfunctional protein networks (inflammation, oncology) | interlinetx.com | added as `interline-therapeutics` |
| Maze Therapeutics | life_science | Maze Therapeutics | pharma | MAZE | US | Clinical-stage precision-genetics biotech; chronic kidney disease programmes (APOL1) | mazetx.com | added as `maze-therapeutics` |
| Monimoi Therapeutics | life_science | Monimoi Therapeutics | pharma | private | US | Early-stage oncology; covalent medicines and chemical proteomics against hard-to-drug cancer proteins | monimoitx.com | added as `monimoi-therapeutics` |
| NeuroLux Inc. | life_science | NeuroLux | tools | private | US | Wireless, battery-free implantable optogenetic and physiological-monitoring devices for neuroscience research | neurolux.org | added as `neurolux` |
| Ningxia Agricultural Land Reclamation Changshan Tou Farm Company Limited | off_sector | Ningxia Nongken Changshantou Farm |  | private | CN | State-owned farm (alfalfa forage, livestock); agriculture; ignored |  | ignored |
| Nordic Brain Tech AS | off_sector | Nordic Brain Tech |  | private | NO | Digital-therapeutics / medtech start-up: home biofeedback device and headache eDiary app for migraine; digital health software and device with no wet-lab biology component; ignored | nordicbraintech.com | ignored |
| Novital Biotechnology Company, Limited | unsure |  |  |  |  | no web footprint under this spelling in English or Chinese; 'Company, Limited' suggests a Hong Kong registration; left as new |  | left `new` (unsure) |
| OBI Pharma USA Inc. | life_science | OBI Pharma | pharma | 4174.TWO | TW | Taiwan oncology biotech (Globo H cancer vaccine, ADCs); OBI Pharma USA is its San Diego subsidiary | obipharma.com | added as `obi-pharma` |
| ORGANOIDSCIENCES CO. | life_science | OrganoidSciences | pharma | 476040.KQ | KR | Organoid regenerative therapy (ulcerative colitis) plus organoid-based efficacy / toxicity testing services and culture kits | organoidrx.com | added as `organoidsciences` |
| PAION Pharma GmbH | life_science | PAION Pharma | pharma | 600079.SS | DE | Aachen specialty pharma (ex-PAION AG business) marketing remimazolam and other hospital products in Europe | paionpharma.com | added as `paion-pharma` |
| PowerChina Guiyang Engineering Corporation Limited | off_sector | PowerChina Guiyang Engineering Corporation |  | 601669.SS | CN | State-owned engineering survey, design and EPC institute (hydropower, water, renewables); engineering contractor; ignored |  | ignored |
| PrimeFour Therapeutics | life_science | PrimeFour Therapeutics | pharma | private |  | Small private biotech; cancer chemotherapeutics (nucleoside analogs exploiting synthetic lethality) |  | added as `primefour-therapeutics` |
| Proteolyze Therapeutics, Inc. | unsure |  |  |  |  | no web footprint and proteolyze.com does not resolve; the COI context implies a stealth targeted-protein-degradation start-up; left as new |  | left `new` (unsure) |
| Rezo Therapeutics | life_science | Rezo Therapeutics | pharma | private | US | Precision-oncology biotech using integrated disease-network mapping (university spin-out) | rezotx.com | added as `rezo-therapeutics` |
| Rhea Natural Sciences Pte Ltd | life_science | Rhea Natural Sciences | pharma | private | SG | Singapore animal-health and anti-infectives group: natural antibiotic alternatives for farming; AI-guided anti-infective small molecules (human and veterinary) | rheapharma.com | added as `rhea-natural-sciences` |
| SEEDERS Inc. | unsure |  |  |  | KR | only trace is an academic profile placing it in Daejeon; no website, registry entry or news; left as new |  | left `new` (unsure) |
| SWAN Genomics | life_science | SWAN Genomics | tools | private | AU | University spin-out building plasmonic-nanoantenna single-molecule sequencing of native, unamplified DNA | swangenomics.com | added as `swan-genomics` |
| Shackleford Pharma Inc. | life_science | Shackelford Pharma | pharma | private | CA | Cannabinoid-derived prescription drugs for epilepsy and neurological disease | shackelfordpharma.com | added as `shackelford-pharma` |
| Shandong Benon Biological Technology Co., Ltd. | off_sector | Shandong Benon Bio-Technology |  | private | CN | OEM / ODM functional-food and nutraceutical contract manufacturer (solid drinks, candy, nutrition ingredients); food manufacturer, not tools or pharma; ignored | sino-benon.com | ignored |
| Shanghai Sequanta Technologies Co., Ltd. | life_science | Sequanta Technologies | cro | private | CN | Shanghai multi-omics sequencing and proteomics service lab (NGS, Olink, single-cell) for pharma biomarker work | sequanta.com | added as `sequanta-technologies` |
| Slingshot Biosciences | life_science | Slingshot Biosciences | tools | private | US | Synthetic cell mimics sold as flow-cytometry controls and calibrators | slingshotbio.com | added as `slingshot-biosciences` |
| TAD Pharma | life_science | TAD Pharma | pharma | KRKG.LJ | DE | Cuxhaven generics maker (cardio, pain, CNS, oncology, urology) plus veterinary products | tad.de | added as `tad-pharma` |
| Tango Therapeutics | life_science | Tango Therapeutics | pharma | TNGX | US | Precision-oncology biotech (synthetic lethality; PRMT5 inhibitors) | tangotx.com | added as `tango-therapeutics` |
| Tatara Therapeutics | life_science | Tatara Therapeutics | pharma | private | US | Ferrous-iron-activated (FIRE) cancer drug conjugates exploiting tumour ferro-addiction (university spin-out) | tatararx.com | added as `tatara-therapeutics` |
| Tenaya Therapeutics | life_science | Tenaya Therapeutics | pharma | TNYA | US | Cardiac gene-therapy biotech (MYBPC3 HCM, PKP2 ARVC programmes) | tenayatherapeutics.com | added as `tenaya-therapeutics` |
| VOS DISCOVERY CO. | life_science | VOS Discovery | cro | 476040.KQ | VN | Vietnam subsidiary of OrganoidSciences: cancer-organoid platform and organoid CRO services | vosdiscovery.vn | added as `vos-discovery` |
| Vilya Therapeutics | life_science | Vilya | ai_bio | private | US | Computational design of cell-permeable macrocycle peptide drugs (protein-design institute spin-out) | vilyatx.com | added as `vilya` |
| Vir Biotechnology | life_science | Vir Biotechnology | pharma | VIR | US | Immunology biotech: hepatitis delta programme plus dual-masked T-cell engagers in solid tumours | vir.bio | added as `vir-biotechnology` |
| Xanadu Quantum Technologies Inc. | off_sector | Xanadu |  | XNDU | CA | Photonic quantum-computing company (software and hardware); quantum computing, not life science; listed via SPAC Mar 2026; ignored | xanadu.ai | ignored |
| Zhangjiajie Branch of Hunan Tobacco Company | off_sector | Hunan Tobacco Company, Zhangjiajie Branch |  | private | CN | Municipal branch of the Hunan provincial tobacco company under the state tobacco monopoly; tobacco; ignored | hngytobacco.com | ignored |
| Citryll B.V | life_science | Citryll | pharma | private | NL | Antibody that clears and blocks neutrophil extracellular traps (rheumatoid arthritis, hidradenitis suppurativa) | citryll.com | added as `citryll` |
| Narayana Nethralaya | off_sector | Narayana Nethralaya |  | private | IN | Super-specialty eye hospital group in Bengaluru with ocular research labs; hospital, not a company; ignored | narayananethralaya.org, narayananethralaya.com | ignored |
| cytobacco.com | off_sector | China Tobacco Sichuan Industrial |  | private | CN | E-mail domain of the Sichuan provincial cigarette manufacturer; tobacco; same company as the Sichuan entry; ignored | cytobacco.com | ignored |
| genevec.com | life_science | Beijing WellGene Biotech | pharma | private | CN | Gene-therapy developer using an HSV-1 vector platform for oncolytic, skin and genetic-disease drugs | genevec.com | added as `beijing-wellgene-biotech` |
| hngytobacco.com | off_sector | Hunan Tobacco Company |  | private | CN | E-mail domain of the Hunan provincial tobacco company; tobacco; same company as the Zhangjiajie branch entry; ignored | hngytobacco.com | ignored |
| organoidrx.com | life_science | OrganoidSciences | pharma | 476040.KQ | KR | Company website / e-mail domain of OrganoidSciences | organoidrx.com | folded into `organoidsciences` |
| rheapharma.com | life_science | Rhea Natural Sciences | pharma | private | SG | Group website / e-mail domain of Rhea Natural Sciences (branded Rhea Pharmaceutical Sciences) | rheapharma.com | folded into `rhea-natural-sciences` |

### Applying the decisions to a live store

The gazetteer change is in the repository; candidate statuses live in the database. On a store that still has
these candidates:

```
litradar rematch --since 90d                 # the new entries pick up their stored papers
litradar candidates refresh --since 90d      # the 55 landed names drop out of the candidate table:
                                             # the gazetteer now knows them, so their evidence is no longer produced
```

and the 17 off-sector names are ignored by hand (`refresh` never changes a status, so this is one command each):

```
litradar candidates ignore "Société des Produits Nestlé SA"
litradar candidates ignore "3P Instruments GmbH"
litradar candidates ignore "China Tobacco Hunan Industrial Co., Ltd."
litradar candidates ignore "China Tobacco Sichuan Industrial Co., Ltd."
litradar candidates ignore "Cobb-Vantress, Ltd."
litradar candidates ignore "Hebei Yuanzhang Aquaculture Co., Ltd."
litradar candidates ignore "Inner Mongolia Xinyu Seed Industry Co., Ltd."
litradar candidates ignore "Innovation FFoQSI GmbH"
litradar candidates ignore "Ningxia Agricultural Land Reclamation Changshan Tou Farm Company Limited"
litradar candidates ignore "Nordic Brain Tech AS"
litradar candidates ignore "PowerChina Guiyang Engineering Corporation Limited"
litradar candidates ignore "Shandong Benon Biological Technology Co., Ltd."
litradar candidates ignore "Xanadu Quantum Technologies Inc."
litradar candidates ignore "Zhangjiajie Branch of Hunan Tobacco Company"
litradar candidates ignore "Narayana Nethralaya"
litradar candidates ignore "cytobacco.com"
litradar candidates ignore "hngytobacco.com"
```

## Ticker enrichment against the SEC list

`companies.sec_ticker_proposals(entries, tickers)` takes every entry whose ticker is a placeholder and keeps
only an **exact** normalised match against `company_tickers.json` (fetched with the `lit-radar/0.1 <contact>`
User-Agent the SEC asks for, cached under `.cache/` for 30 days; 10,443 registrants on 2026-10-17). The existing
`candidates --hints` fuzzy hint is for browsing; this one is for applying, so a shared first word is not enough.

Run over the 264 entries (119 without a listing ticker) it proposed two, both verified against the exchange
announcements:

| Entry | Current | Proposed | Why |
|---|---|---|---|
| `freenome` (Freenome) | `private` | `FRNM` | SEC title "Freenome, Inc."; began trading on Nasdaq on 2026-07-21 after closing its merger with a special-purpose acquisition company |
| `generate-biomedicines` (Generate Biomedicines) | `private` | `GENB` | SEC title "Generate Biomedicines, Inc."; initial public offering priced 2026-02-26, Nasdaq Global Select from 2026-02-27 |

One near miss was shown and withheld: `hamilton` (Hamilton Company, the liquid-handling maker) against
"Hamilton Lane INC" (`HLNE`, an asset manager; match 0.9 on the shared first word). The two proposals are
left as proposals because this pass only appends to `companies.yaml`; applying them is a one-field change on
each existing line:

```
- {id: freenome, ..., ticker: FRNM, ...}
- {id: generate-biomedicines, ..., ticker: GENB, ...}
```

## Tests

`tests/test_companies.py` seeds a temporary store through the normal `Store` API (five papers, two companies,
an `unlisted:` institution, one instrument, three news items, two candidate rows) and checks the window and
month helpers, role counting across sections, the listing filters and ordering, the detail profile (both
windows, the 24-month series, co-mentions, recent papers with roles, news by id and by name, linked
candidates), entry resolution including the ambiguity error, CSV export, the graph with and without
`unlisted:` nodes, the SEC proposals with a canned ticker list (exact match proposed, same-brand near miss
not), the vetting-table renderer, and the four CLI actions end to end (`list --json`, `--public`, `show` by
id and by ticker, the unknown-company exit code, `export` to a file and to stdout, `graph` to a file and with
a threshold that empties it). `tests/test_dashboard_companies.py` builds a site from a seeded store and checks
that a company above the threshold gets a profile and one below does not, the links from `companies.html`,
the nested asset and nav paths, HTML escaping of a hostile company name in the title and payload, the d3 pin
and hash on the graph page, the graph payload and `data/companies-graph.json`, the default-edge-weight rule,
and that a store without mentions still writes the graph page. Nothing touches the network.
