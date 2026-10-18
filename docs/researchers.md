# Researcher tracking: how it matches, how to resolve an author, what the 2026-10-17 pass changed

`config/subgroups.yaml` lists, per research subgroup, the people whose new papers and preprints the digest
should flag (`tracked_authors`). Each entry is one flow mapping on one line:

```yaml
- {name: "Fabian J. Theis", openalex_id: "A5083128670", orcid: "0000-0002-2419-1943", institution: "Helmholtz Munich"}  # resolved 2026-10-17: ...
```

* `name` is the display name the digest prints and the fallback matcher uses.
* `openalex_id` makes matching exact. `""` means "not resolved yet": the person is then matched by name only.
* `orcid` is recorded when OpenAlex knows it. The matcher uses it to reject a name match whose record carries a
  different ORCID (another person), and it is the stable handle for re-resolving a profile after OpenAlex splits or
  merges it (`/authors?filter=orcid:0000-...`).
* `institution` is a free-text hint for humans and for the resolver's institution check; it is never matched
  against papers.

## How tracking works

**Pull.** When the OpenAlex source runs, the pipeline queries `/works` with
`authorships.author.id:<id>|<id>|...` for every configured id, 25 ids per request
(`litradar/pipeline.py`, the `tracked_authors` query). People without an id are not pulled by this query; they
are only found when one of their papers arrives through another source or query (a subgroup topic query,
bioRxiv, Europe PMC, a journal feed).

**Match** (`Pipeline.extract`). First the record itself: a record that is not a paper produces no hits at all
(`tracked_hits.skipped_non_paper`) - an OpenAlex `type` of dataset / supplementary-materials / software / peer-review
(`extra_json.work_type`) or a figure / table / data component title ("Figure S5 from ..."). A data deposit credits
every facility user or depositor as an author (DOE / EMSL data records list 100+ names, bulk-attributed to each
of them), so it is never a tracked researcher's new paper. Then, for every author on the record:

1. If the record's author carries an OpenAlex id that is in the tracked list, it is a hit with `matched_by`
   `openalex_id`. This is exact and is never capped.
2. Otherwise the author's name is normalised to "first given name + surname" (`_norm_name`: ASCII-folded,
   lower-case, `Last, First M.` accepted) and compared with the tracked names; an exact display-name match
   (`_display_key`) also counts. A surname plus an initial (`F. Chen`, `Chen F`) never matches: on a common name
   that is not evidence of identity.
3. A name match is then qualified:
   * the tracked person has an id configured **and** the record's author carries a *different* OpenAlex id:
     this is another person, the match is dropped (counted as `tracked_hits.name_rejected_other_id`);
   * the tracked person has an id configured but the record carries **no** author ids at all (typical for
     bioRxiv and Europe PMC records): the hit is kept with `matched_by` **`name_no_ids`**. The digest prints it
     with the caveat "name match; the record carried no OpenAlex author ids, so the configured id could not be
     checked";
   * no id configured: `matched_by` `name`, printed with "name match; add the OpenAlex id to make it exact";
   * the tracked person has an ORCID configured **and** the record's author carries a *different* ORCID: another
     person, dropped (`tracked_hits.name_rejected_other_orcid`);
   * **topical plausibility**: a name match is kept only when the paper matches the tracked person's subgroup by
     keyword or OpenAlex topic (`Pipeline.assign_subgroups`, the same rule that files papers under subgroups).
     A namesake's insect-phylogenomics paper does not match a CRISPR subgroup and is dropped
     (`tracked_hits.name_rejected_off_topic`). Id hits are never gated this way. A subgroup configured without
     keywords and topics cannot judge plausibility and lets name matches through as before.

**Per-name cap.** Name-only hits (`name` and `name_no_ids`) are capped per tracked name per run:
`tracked_authors.name_match_cap` in `config/radar.yaml`, default 5. Hits beyond the cap are not stored
(`tracked_hits.name_capped`) and the run logs "N name-only matches this run, kept the first 5; add an OpenAlex
id". Id hits are never capped. A Fei Chen or a Xiao Wang without an id therefore produces at most five
provisional hits a week, most of them other people; with the id it produces exactly their papers.

**Coverage.** `litradar coverage` lists tracked authors with no new paper in the window and says how many of
them have no OpenAlex id, so a silent name-only entry is the first thing to resolve.

## How to resolve an author

### With the script (preferred)

```bash
python3 scripts/resolve_authors.py --dry-run                      # proposals for every tracked author, nothing written
python3 scripts/resolve_authors.py --dry-run --names "Fei Chen"   # one or a few names
python3 scripts/resolve_authors.py --write                        # apply; the file is re-parsed and refused if anything is malformed
python3 scripts/resolve_authors.py --dry-run --report r.json --markdown tables.md --sanity 5
```

The script reads `OPENALEX_API_KEY` and `LITRADAR_CONTACT_EMAIL` from the environment, loading the
repository's `.env` (or `--env PATH`) first; it never prints them. Responses are cached under
`.cache/resolve-authors/` (git-ignored), so a dry run followed by a write spends the budget once. A full pass
over this file costs about 170 list calls: one per 50 existing ids, one or two `/authors?search=` calls per
distinct name, and one `/works` count per `--sanity` author. The free key allows roughly 10,000 list calls a
day.

What it does per author:

* **Existing id**: fetched in batches of 50 (`/authors?filter=openalex:A|B|...`); an id that OpenAlex has merged
  away is fetched singly and followed to its new id. The profile is *verified* when its display name agrees
  with the configured name (same surname; the configured given name among the profile's given names,
  nicknames allowed, `Xiao` is not `Xiaodong`), an institution in `last_known_institutions` or `affiliations`
  matches the configured institution (token match with an alias table for MIT, UCSF, EMBL-EBI, HHMI, CZI,
  KTH, WEHI ...), and the works count is plausible (`--stub-works` 10 to `--merged-works` 3000).
* **Every name**: `/authors?search=<name>` (25 rows; the relevance search also finds "Fabian Joachim Theis" for
  "Fabian J. Theis", which a `display_name.search` filter does not). When that is not conclusive or the name
  has more profiles than one page holds, a second query adds `filter=topics.id:<the subgroup's
  openalex_topics>`, then the name is retried without initials.
* **Decision**: `resolved` when one institution-matching candidate clearly leads (ten times the citations or
  five times the works of the next one, or the configured institution is its *last known* one while the
  rival only passed through); the smaller same-topic profiles are named in the comment. `ambiguous` when two
  institution-matching profiles are comparable: both are written into the comment and the id stays empty.
  `unresolved` when nothing matches the institution. `corrected` when an existing id fails (no topic overlap
  with the subgroup while a same-institution profile with the topics and the citations exists) and the
  replacement is clear. `suspicious` when it fails and nothing better exists: the id is kept, the comment says
  why.
* **Warnings** written into the comment and the report: a single publication year holding 40 % or more of a
  profile's works (a dataset or patent dump, or another person's catalogue), and alternative names that
  belong to a different person (different surname and given name), both signs of a merged profile.

Edits are line-based: each author line is rewritten with the same key order `litradar track author` uses
(`name`, `openalex_id`, `orcid`, `institution`), every other line and comment survives, and the file is
re-parsed before it is written (same set of (subgroup, name) pairs, every id `A\d{6,}`, no duplicate id inside
a subgroup, every ORCID well-formed). A note the script wrote on an earlier pass (`resolved 2026-10-17: ...`)
is kept when the id simply verifies again, so re-runs are idempotent; the header line is rewritten only by a
pass that changed an id. `tests/test_subgroups_config.py` enforces the same shape rules on the committed file.

### By hand

* `python3 -m litradar track author "Name" --subgroup <id>` looks the name up (`OPENALEX_API_KEY` required) and
  proposes the top profile; `--openalex A...` or `--orcid 0000-...` records a known id directly (`docs/track.md`).
* With an ORCID: `https://api.openalex.org/authors?filter=orcid:0000-0002-2419-1943` returns the profile(s)
  that carry it; a person split over several profiles usually shares one ORCID across them.
* Check a profile before trusting it: `works_count` and `cited_by_count` against what you know of the person,
  `last_known_institutions`, `topics`, `counts_by_year` (a spike of hundreds of works in one year is bulk
  misattribution), and `display_name_alternatives` (another person's name there means a merge).
* OpenAlex re-disambiguates authors continually. Re-run the dry run when `litradar coverage` shows a tracked
  author silent for a quarter, or every few months; a profile that was merged away is followed and corrected
  automatically.

## The 2026-10-17 resolution pass

Before the pass, 37 of the 160 author lines had no id and several ids were marked in comments as "probable",
"may be a split profile" or "merged profile", none verified against the API. The pass ran
`scripts/resolve_authors.py` over the whole file: 160 author lines, 154 distinct people (six people sit in two
subgroups), 171 list calls (166 for the resolution, 5 for the live check below).

| outcome | count |
|---|---:|
| resolved (was empty, now has an id) | 37 |
| verified (existing id confirmed) | 115 |
| corrected (existing id replaced) | 2 |
| suspicious, kept with a warning | 0 |
| ambiguous or unresolved, still empty | 0 |
| ORCID added | 160 lines (every tracked author) |

### Changed ids

The two corrections first. In both the configured id pointed at a **namesake at the same institution**: the
profile's topics had nothing to do with the subgroup and a second profile at the institution carried the
person's topics and most of the citations.

| name | subgroup(s) | old id | new id | evidence |
|---|---|---|---|---|
| David R. Liu | perturbation, gene_editing | A5100739514 | A5065535610 | old profile "David Liu": 361 works, 14,463 citations, cancer-genomics topics, no CRISPR topic (T10878) — a different Broad Institute David Liu. New: "David R. Liu", 479 works, 81,557 citations, Broad Institute (last known), topics T10878/T10613/T10725, ORCID 0000-0002-9943-7557. The old comment ("361 works, may be a split profile") was a misread: not a split, another person. |
| Jesper V. Olsen | proteomics | A5023019360 | A5046276741 | old profile "Jesper Olsen": 381 works, 18,005 citations, no proteomics topic — a namesake at Copenhagen. New: "Jesper Velgaard Olsen", 507 works, 57,779 citations, University of Copenhagen (last known), topics T10519/T10683/T10836, ORCID 0000-0002-4747-4938. |

The 37 resolutions. "Institution" is the matching entry in the profile's `last_known_institutions`; "topics" are
the subgroup topic ids found among the profile's top topics.

| name | subgroup | new id | ORCID | evidence |
|---|---|---|---|---|
| Alex Shalek | single_cell | A5083743078 | 0000-0001-5670-8778 | "Alex K. Shalek"; 615 works, 58,009 citations; Broad Institute; T11289 |
| Bo Wang | ai_bio | A5100408160 | 0000-0002-9620-3413 | 318 works, 18,518 citations; University of Toronto + Vector Institute + University Health Network (last known). No overlap with the ai_bio topic ids; the profile's own topics are single-cell transcriptomics, gene-regulatory networks and AI in healthcare, consistent with the person. The other two "Bo Wang" profiles with a Toronto affiliation are a palaeontologist (562 works) and a TCM oncologist (412 works). Checked by hand. |
| Britt Adamson | perturbation | A5089203415 | 0000-0002-9451-5819 | 45 works, 12,069 citations; Princeton University; T10878, T11289 |
| Christopher A. Voigt | synthetic_biology | A5028170615 | 0000-0003-0844-4776 | 355 works, 23,952 citations; MIT; T10932 |
| Claudia Langenberg | proteomics | A5055584673 | 0000-0002-5017-7344 | 958 works, 112,532 citations; 2026 affiliations Queen Mary University of London, University of Cambridge (MRC Epidemiology Unit), Berlin Institute of Health. No overlap with the proteomics topic ids: the profile is genetic epidemiology, which is this person's field (plasma-proteome genetics). Only two profiles carry the name; the other has 9 works. One alternative name on the profile belongs to another person (a co-author misattribution), so expect the odd stray hit. Checked by hand. |
| Daniel De Carvalho | liquid_biopsy | A5069130022 | 0000-0002-8572-5259 | "Daniel Diniz De Carvalho"; 271 works, 13,313 citations; University Health Network (Princess Margaret's parent); T11287 |
| Daniel G. Anderson | gene_editing | A5036096394 | 0000-0001-5629-4798 | 549 works, 79,765 citations; Koch Institute / Harvard–MIT HST; T10725 |
| Deb Schrag | mced | A5088893766 | 0000-0002-4334-5717 | "Deborah Schrag"; 832 works, 67,550 citations; Memorial Sloan Kettering; T10556, T11287 |
| Dennis Lo | liquid_biopsy | A5005665172 | 0000-0001-8746-0293 | "Yuk Ming Dennis Lo"; 453 works, 41,818 citations; Chinese University of Hong Kong; T11287, T10978. Three smaller same-topic profiles at the institution (45, 56 and 12 works) are noted in the comment. |
| Eric Klein | mced | A5001069772 | 0000-0002-1783-0698 | "Eric A. Klein"; 1,441 works, 57,388 citations; last known institution GRAIL, Cleveland Clinic in the affiliation history; T10124. Three small same-topic Cleveland Clinic profiles noted. |
| Erich Jarvis | long_read | A5028548956 | 0000-0001-8931-5049 | "Erich David Jarvis"; 724 works, 44,806 citations; Howard Hughes Medical Institute; T10015, T11213 |
| Evan E. Eichler | long_read | A5014870107 | 0000-0002-8246-4014 | 1,055 works, 259,978 citations; University of Washington; T11213, T13664 |
| Fabian J. Theis | single_cell | A5083128670 | 0000-0002-2419-1943 | "Fabian Joachim Theis"; 1,032 works, 82,037 citations; Helmholtz Munich; T11289, T10621. The 18-work stub the old comment mentions is A5062566971, a split of the same person. |
| Fei Chen | spatial | A5100405446 | 0000-0003-2308-3649 | 183 works, 17,119 citations; Broad Institute; T11289. A second Broad "Fei Chen" (A5100405431, 106 works, 180 citations) is a split; 1,966 profiles carry the name. |
| Ido Amit | single_cell | A5048280875 | 0000-0003-2968-877X | 513 works, 104,633 citations; Weizmann Institute of Science; T11289 |
| James J. Collins | synthetic_biology | A5065209470 | 0000-0002-5560-8246 | 586 works, 115,903 citations; Broad Institute (MIT in the history); T10932. Two MIT namesakes without topic overlap (96 and 42 works) were rejected. |
| Jason Chin | synthetic_biology | A5077915220 | 0000-0003-1219-4757 | "Jason W. Chin"; 227 works, 24,904 citations; MRC Laboratory of Molecular Biology; T10521 |
| Jesse Engreitz | perturbation | A5102868909 | 0000-0002-5754-1719 | "J Engreitz"; 499 works, 17,764 citations; Stanford University; T10222, T10878. **Merged profile**: 281 of the 499 works are dated 2021 and the alternative names include another chromatin researcher's name. It is nevertheless the only non-stub profile for the person (the "Stanford Jesse Engreitz" stub has 2,216 works and 0 citations); expect some off-target hits. |
| Jonathan S. Weissman | perturbation | A5026663934 | 0000-0003-2445-670X | 621 works, 120,046 citations; Whitehead Institute; T10878, T11289. One alternative name on the profile belongs to another person. |
| Joshua Coon | proteomics | A5086840789 | 0000-0002-0004-8253 | "Joshua J. Coon"; 596 works, 32,746 citations; University of Wisconsin System; T10519, T10683, T10836 |
| Kerstin Lindblad-Toh | long_read | A5007985109 | 0000-0001-8338-0253 | 652 works, 81,539 citations; Broad Institute (Uppsala in the history); T10015 |
| Luis A. Diaz | liquid_biopsy | A5050789569 | 0000-0002-7079-8914 | "Luis Alberto Diaz"; 707 works, 101,028 citations; Memorial Sloan Kettering; T11287 |
| Luke A. Gilbert | perturbation | A5035219354 | 0000-0001-5854-0825 | 220 works, 28,698 citations; Arc Research Institute; T10878 |
| Michael C. Schatz | long_read | A5058192453 | 0000-0002-4118-4446 | 362 works, 59,752 citations; Johns Hopkins University; T10015, T11213. One 21-work same-topic split noted. |
| Michael Jewett | synthetic_biology | A5059800162 | 0000-0003-2948-6211 | "Michael C. Jewett"; 341 works, 17,919 citations; Stanford University; T10521, T10932 |
| Neil Kelleher | proteomics | A5076257201 | 0000-0002-8815-3372 | "Neil L. Kelleher"; 679 works, 45,217 citations; Northwestern University; T10519, T10683, T10836. One 48-work split noted. |
| Pamela Silver | synthetic_biology | A5087859961 | 0000-0002-7856-4071 | "Pamela A. Silver"; 496 works, 40,279 citations; Harvard University; T10932 |
| Rebecca Fitzgerald | liquid_biopsy | A5050348689 | 0000-0002-3434-3568 | "Rebecca Claire Fitzgerald"; 1,077 works, 43,212 citations; University of Cambridge; T11287 |
| Richard Henderson | structural_biology | A5073357045 | 0000-0003-1444-0528 | "Richard A. Henderson"; 477 works, 33,493 citations; MRC Laboratory of Molecular Biology (last known); T10857. **Merged profile**: the topics also include organometallic chemistry and the affiliation history includes Newcastle and York, i.e. a same-name inorganic chemist. The only other LMB profile is a 5-work stub, so the id is used and the comment says so; expect occasional chemistry hits. Annotated by hand. |
| Ruedi Aebersold | proteomics | A5053361384 | 0000-0002-9576-3267 | "Ruedi H. Aebersold"; 1,591 works, 179,263 citations; ETH Zurich; T10519, T10683, T10836 |
| Samuel H. Sternberg | gene_editing | A5072427514 | 0000-0001-8240-9114 | 132 works, 13,222 citations; Columbia University; T10878 |
| Stephen Quake | single_cell | A5090505297 | 0000-0002-1613-0809 | "Stephen R. Quake"; 765 works, 93,769 citations; Chan Zuckerberg Biohub San Francisco; T11289 |
| Steven A. Carr | proteomics | A5036255260 | 0000-0002-7203-4299 | 1,171 works, 95,993 citations; Broad Institute; T10519, T10683, T10836 |
| Thomas M. Norman | perturbation | A5084321543 | 0000-0002-3755-4379 | 43 works, 7,528 citations; Memorial Sloan Kettering; T10878, T11289, T10222 |
| Timothy Lu | synthetic_biology | A5023856282 | 0000-0002-3918-8923 | "Timothy K. Lu"; 257 works, 23,044 citations; MIT; T10932 |
| Tomasz Beer | mced | A5048477205 | 0000-0001-5600-9993 | "Tomasz M. Beer"; 788 works, 38,626 citations; Exact Sciences; T10124, T11287 |
| Xiao Wang | spatial | A5076270465 | 0000-0002-3090-9894 | 103 works, 24,542 citations; Broad Institute (last known), MIT; T11289. Chosen over three MIT-affiliated namesakes without topic overlap (576, 400 and 339 works) because the configured institution is this profile's last known one and it leads on citations. |

### Existing ids: what the old comments claimed and what the API says

| name | old comment | finding |
|---|---|---|
| Heng Li (long_read) | id "probable", 382 works, no institution hint | verified: 297,193 citations, affiliations Broad Institute and Harvard University in 2025–2026 (the profile has no `last_known_institutions`, which is why the earlier look-up found no institution), topics T10015/T11213, ORCID 0000-0003-4874-2874. A Dana-Farber "Heng Li" with 451 works and no genomics topic is a namesake. |
| David R. Liu | "361 works, may be a split profile" | not a split: a namesake. Corrected (table above). |
| Alexander Rives (ai_bio) | "profile carries only ~20 works" | verified: 20 works but 12,563 citations, topics T12254/T10044. The configured institution (EvolutionaryScale) does not exist in OpenAlex; the profile lists New York University and Meta. Kept because the topics agree and no rival profile exists. |
| George M. Church | display name "George McDonald Church" (1,288 works) | verified as described. |
| Ed Lein (spatial) | "split profile; second id A5110230528" | verified; the second id is confirmed as a 30-work, 4,602-citation split at the Allen Institute. Comment updated. |
| Jürgen Cox (proteomics) | "split profile; also A5033409837, A5084242032 (same ORCID)" | verified (134 works, 53,518 citations); both splits confirmed at the Max Planck Institute of Biochemistry (76 and 54 works). |
| Nir Yosef (perturbation) | "split profile; also A5077844473" | verified (326 works, 36,450 citations, Weizmann, T11289/T10222). The split A5077844473 ("Yosef Nir A", 365 works) carries **more** citations (59,516) but no subgroup topic in its top topics; the configured id is the one with the single-cell work, so it stays. If his hits look thin, add the split as a second entry. |
| E. Shelley Hwang (mced) | "split profile; also A5072004172" | verified (207 works, 6,550 citations, Duke); the named split (153 works) and two more Duke profiles (83 and 57 works) share the topics. |

### Kept with a warning

These ids verify but the profiles are polluted; the comment in the file records it and the digest may show an
occasional paper that is not theirs.

| name | id | warning |
|---|---|---|
| Karin Strauss (synthetic_biology) | A5024493389 | display name "Karin Strauß"; 847 of 1,001 works are dated 2020, a bulk misattribution. Career affiliations (Microsoft, University of Washington, AMD, UIUC, IBM) and the DNA-storage topic are the person's. |
| Eva Nogales (structural_biology) | A5034946559 | 922 of 1,396 works are dated 2026 (a dataset dump; this also inflates any "works since 2026" count for the id). |
| Luis Ceze (synthetic_biology) | A5081914923 | 856 of 1,205 works are dated 2020. |
| Victor E. Velculescu (liquid_biopsy) | A5006560755 | 381 of 934 works are dated 2023. |
| Sergey Ovchinnikov (ai_bio) | A5084204507 | one alternative name on the profile belongs to another person. |
| Jesse Engreitz, Richard Henderson | see above | merged profiles, used for want of a better one. |

Namesakes found at the same institution but rejected for lack of topic overlap (David Baker at Washington,
Heng Li at Dana-Farber, Bo Wang at Toronto, Ash Alizadeh at Stanford, Daniel Bauer, Elizabeth Villa,
Jeanne Tie, Traver Hart, Rong Fan, Eva Nogales) are listed in the dry-run output and the JSON report, not in
the file.

### Still unresolved

None. Every tracked author now carries an id. The two names the old comments called "common" (Fei Chen,
Xiao Wang) resolved once the search was narrowed by the subgroup's topic ids and ranked by last-known
institution and citations.

### Live check: works since 2026-01-01

`/works?filter=authorships.author.id:<id>,from_publication_date:2026-01-01`, queried 2026-10-17 for five of
the newly resolved ids (`--sanity 5`):

| name | id | works dated 2026 |
|---|---|---:|
| Fabian J. Theis | A5083128670 | 76 |
| Stephen Quake | A5090505297 | 34 |
| Alex Shalek | A5083743078 | 64 |
| Ido Amit | A5048280875 | 34 |
| Fei Chen | A5100405446 | 12 |

Plausible annual output for the four large groups and for a mid-sized one; nothing resembling the 922-work
2026 spike on the Eva Nogales profile.

### Reproducing

```bash
python3 scripts/resolve_authors.py --dry-run --report .cache/resolve-authors/report.json --markdown .cache/resolve-authors/tables.md --sanity 5
```

The Markdown file holds the full tables (including the 115 verified ids with works, citations, matching
institution and ORCID) in the generated form; the JSON report holds every decision with its top six
candidates. The appendix below is the verified table from the pass.

## Appendix: verified ids (2026-10-17)

| name | subgroup(s) | id | works | citations | institution evidence | ORCID |
|---|---|---|---:|---:|---|---|
| Adam M. Phillippy | long_read | A5086869042 | 316 | 72,223 | National Institutes of Health | 0000-0003-2983-8934 |
| Albert Heck | proteomics | A5053603496 | 1315 | 71,085 | Utrecht University | 0000-0002-2405-4404 |
| Alexander Marson | cell_therapy | A5070915778 | 224 | 19,749 | Gladstone Institutes | 0000-0002-2734-5776 |
| Alexander Rives | ai_bio | A5029063409 | 20 | 12,563 | not matched (New York University, Meta on the profile); topics agree | 0000-0003-2208-0796 |
| Alexander van Oudenaarden | single_cell | A5035202405 | 498 | 63,034 | Hubrecht Institute | 0000-0002-9442-3551 |
| Alexey Nesvizhskii | proteomics | A5085497637 | 477 | 48,979 | University of Michigan | 0000-0002-2806-7819 |
| Alexis Komor | gene_editing | A5044327001 | 70 | 15,252 | University of California San Diego | 0000-0001-5043-0998 |
| Allon Klein | single_cell | A5064938000 | 132 | 28,470 | Harvard University | 0000-0001-8913-7879 |
| Anne-Claude Gingras | proteomics | A5083905650 | 826 | 56,385 | University of Toronto | 0000-0002-6090-4437 |
| Anne-Renee Hartman | mced | A5073892003 | 164 | 9,094 | Adela | 0000-0003-3506-6440 |
| Ash A. Alizadeh | liquid_biopsy | A5007420876 | 619 | 73,675 | Stanford University | 0000-0002-5153-5625 |
| Aviv Regev | single_cell | A5004067604 | 1180 | 236,937 | Genentech | 0000-0003-3293-3158 |
| Benedict Paten | long_read | A5061203088 | 376 | 57,650 | University of California, Santa Cruz | 0000-0001-8863-3539 |
| Benjamin Kleinstiver | gene_editing | A5058898175 | 157 | 14,210 | Massachusetts General Hospital | 0000-0002-5469-0655 |
| Bernd Bodenmiller | spatial | A5010642463 | 495 | 27,286 | University of Zurich | 0000-0002-6325-7861 |
| Bernhard Küster | proteomics | A5058082024 | 518 | 46,354 | Technical University of Munich | 0000-0002-9094-1677 |
| Bert Vogelstein | liquid_biopsy | A5072858943 | 1202 | 391,404 | Johns Hopkins University | 0000-0003-0766-3854 |
| Bonnie Berger | ai_bio | A5044078921 | 605 | 34,570 | Broad Institute | 0000-0002-2724-7228 |
| Bosiljka Tasic | single_cell | A5012251441 | 207 | 27,567 | Allen Institute for Brain Science | 0000-0002-6861-4506 |
| Brian L. Hie | ai_bio | A5021962955 | 82 | 10,463 | Stanford Medicine | 0000-0003-3224-8142 |
| Bridget Carragher | structural_biology | A5027958286 | 402 | 21,376 | New York Structural Biology Center | 0000-0002-0624-5020 |
| Bruno Correia | ai_bio | A5040838563 | 166 | 10,711 | École Polytechnique Fédérale de Lausanne | 0000-0002-7377-8636 |
| Carl June | cell_therapy | A5008558372 | 1544 | 156,015 | Hospital of the University of Pennsylvania | 0000-0003-0241-3557 |
| Catherine Alix-Panabières | liquid_biopsy | A5043582421 | 332 | 18,119 | Centre National de la Recherche Scientifique | 0000-0002-6401-2903 |
| Charles Lee | long_read | A5100716198 | 526 | 149,203 | The Jackson Laboratory for Genomic Medicine | 0000-0001-7317-6662 |
| Charles Swanton | liquid_biopsy | A5016475369 | 1425 | 111,998 | The Francis Crick Institute | 0000-0002-4299-3018 |
| Christopher Klebanoff | cell_therapy | A5074579867 | 248 | 25,211 | Memorial Sloan Kettering Cancer Center | 0000-0001-9645-3896 |
| Crystal Mackall | cell_therapy | A5087779758 | 928 | 54,116 | Stanford University | 0000-0003-0359-9023 |
| Dana Pe'er | single_cell | A5074451448 | 406 | 52,771 | Memorial Sloan Kettering Cancer Center | 0000-0002-9259-8817 |
| Daniel E. Bauer | gene_editing | A5071805086 | 213 | 17,844 | Broad Institute | 0000-0001-5076-7945 |
| David Baker | ai_bio | A5053192926 | 2498 | 139,574 | University of Washington | 0000-0001-7896-6217 |
| Debora Marks | ai_bio | A5022191527 | 283 | 33,313 | Broad Institute | 0000-0001-9388-2281 |
| Drew Endy | synthetic_biology | A5047829117 | 223 | 11,751 | Stanford University | 0000-0001-6952-8098 |
| E. Shelley Hwang | mced | A5016887928 | 207 | 6,550 | Duke University | 0000-0002-8571-1148 |
| Ed Lein | spatial | A5085405013 | 252 | 47,660 | Allen Institute for Brain Science | 0000-0001-9012-6552 |
| Edward Egelman | structural_biology | A5014879094 | 730 | 25,475 | University of Virginia | 0000-0003-4844-5212 |
| Elizabeth Villa | structural_biology | A5087861735 | 168 | 26,566 | University of California San Diego | 0000-0003-4677-9809 |
| Erik Sontheimer | gene_editing | A5025571012 | 181 | 19,505 | University of Massachusetts Chan Medical School | 0000-0002-0881-0310 |
| Eva Nogales | structural_biology | A5034946559 | 1396 | 33,953 | Lawrence Berkeley National Laboratory | 0000-0001-9816-3681 |
| Evan Z. Macosko | single_cell, spatial | A5062710108 | 197 | 33,874 | Broad Institute | 0000-0002-2794-5165 |
| Ewan Birney | long_read | A5061379058 | 681 | 197,888 | European Bioinformatics Institute | 0000-0001-8314-8497 |
| Feng Zhang | perturbation, gene_editing | A5100401298 | 405 | 174,427 | Broad Institute | 0000-0003-0178-7995 |
| Fritz J. Sedlazeck | long_read | A5037440162 | 463 | 27,046 | Baylor College of Medicine | 0000-0001-6040-2691 |
| Fyodor Urnov | gene_editing | A5030339120 | 243 | 24,675 | Innovative Genomics Institute | 0000-0001-7542-4084 |
| Garry P. Nolan | spatial | A5033995613 | 914 | 69,228 | Stanford University | 0000-0002-8862-9043 |
| George M. Church | synthetic_biology | A5103076360 | 1288 | 147,910 | Harvard University | 0000-0003-3535-2076 |
| Heng Li | long_read | A5100338825 | 382 | 297,193 | Harvard University (affiliation; no last-known institution on the profile) | 0000-0003-4874-2874 |
| Holger Stark | structural_biology | A5016614839 | 292 | 19,315 | Max Planck Institute for Multidisciplinary Sciences | 0000-0002-6161-1118 |
| Hongkui Zeng | single_cell | A5010189175 | 484 | 51,403 | Allen Institute for Brain Science | 0000-0002-0326-5878 |
| J. Keith Joung | gene_editing | A5005370545 | 259 | 55,147 | Harvard University | 0000-0001-5630-5645 |
| Jacob Berchuck | mced | A5061307462 | 198 | 2,248 | Dana-Farber Cancer Institute | 0000-0002-7607-2428 |
| Jason Moffat | perturbation | A5086112491 | 659 | 30,158 | University of Toronto | 0000-0002-5663-8586 |
| Jay Keasling | synthetic_biology | A5000312223 | 1060 | 68,224 | Lawrence Berkeley National Laboratory | 0000-0003-4170-6088 |
| Jay Shendure | single_cell | A5083897996 | 780 | 118,611 | University of Washington | 0000-0002-1516-1865 |
| Jeanne Tie | liquid_biopsy | A5075700550 | 473 | 20,027 | Walter and Eliza Hall Institute of Medical Research | 0000-0001-9244-2057 |
| Jef D. Boeke | synthetic_biology | A5075536007 | 658 | 64,104 | NYU Langone Health | 0000-0001-5322-4946 |
| Jeffrey Moffitt | spatial | A5087838801 | 121 | 13,364 | Boston Children's Hospital | 0000-0002-3836-3101 |
| Jennifer A. Doudna | perturbation, gene_editing | A5067184382 | 758 | 116,953 | Lawrence Berkeley National Laboratory | 0000-0001-9161-999X |
| Joachim Frank | structural_biology | A5042892654 | 894 | 44,745 | Columbia University | 0000-0001-5449-6943 |
| Joakim Lundeberg | spatial | A5054656710 | 682 | 47,798 | KTH Royal Institute of Technology | 0000-0003-4313-1601 |
| John G. Doench | perturbation | A5051881453 | 736 | 43,104 | Broad Institute | 0000-0002-3707-9889 |
| John Jumper | ai_bio | A5059179935 | 55 | 86,609 | Google DeepMind | 0000-0001-6169-6580 |
| Jonas Frisén | spatial | A5042664107 | 247 | 47,223 | Karolinska Institutet | 0000-0001-5819-458X |
| Jonathan S. Gootenberg | gene_editing | A5063743060 | 111 | 44,235 | Harvard University | 0000-0003-2757-2175 |
| Julia Mahamid | structural_biology | A5032622493 | 154 | 13,014 | European Molecular Biology Laboratory | 0000-0001-6968-041X |
| Jürgen Cox | proteomics | A5108375431 | 134 | 53,518 | Max Planck Institute of Biochemistry | 0000-0001-8597-205X |
| Karen H. Miga | long_read | A5045267050 | 131 | 18,667 | University of California, Santa Cruz | 0000-0002-3670-4507 |
| Karin Strauss | synthetic_biology | A5024493389 | 1001 | 7,990 | Microsoft | 0000-0002-8327-5477 |
| Kiran Musunuru | gene_editing | A5040568538 | 265 | 16,556 | University of Pennsylvania | 0000-0003-3298-0368 |
| Kole Roybal | cell_therapy | A5074263773 | 93 | 8,366 | Gladstone Institutes | 0000-0003-3030-7954 |
| Long Cai | spatial | A5061996201 | 89 | 16,634 | California Institute of Technology | 0000-0002-7154-5361 |
| Luis Ceze | synthetic_biology | A5081914923 | 1205 | 15,629 | University of Washington | 0000-0002-1377-6217 |
| Marcela Maus | cell_therapy | A5029763484 | 566 | 34,968 | Massachusetts General Hospital | 0000-0002-7578-0393 |
| Mark Akeson | long_read | A5012942178 | 119 | 13,067 | University of California, Santa Cruz | 0000-0001-9392-8075 |
| Matthias Mann | proteomics | A5090940750 | 2019 | 304,828 | Max Planck Institute of Biochemistry | 0000-0003-1292-4799 |
| Maximilian Diehn | liquid_biopsy | A5001873561 | 841 | 56,922 | Stanford University | 0000-0003-2032-0581 |
| Michael Bassik | perturbation | A5022593887 | 312 | 28,052 | Stanford University | 0000-0001-5185-8427 |
| Michael J. MacCoss | proteomics | A5043959168 | 481 | 63,084 | University of Washington | 0000-0003-1853-0256 |
| Michel Sadelain | cell_therapy | A5059625599 | 660 | 64,482 | Columbia University Irving Medical Center | 0000-0002-9031-8025 |
| Mohammad Lotfollahi | perturbation | A5077814625 | 71 | 7,638 | University of Cambridge | 0000-0001-6858-7985 |
| Mohammed AlQuraishi | ai_bio | A5002987125 | 72 | 4,355 | Columbia University Irving Medical Center | 0000-0001-6817-1322 |
| Neville Sanjana | perturbation | A5016855642 | 153 | 25,800 | New York Genome Center | 0000-0002-1504-0027 |
| Nickolas Papadopoulos | liquid_biopsy, mced | A5037021577 | 569 | 94,364 | Johns Hopkins University | 0000-0001-7135-7451 |
| Nikolai Slavov | proteomics | A5067445892 | 220 | 12,233 | Northeastern University | 0000-0003-2035-1820 |
| Nir Yosef | perturbation | A5026299103 | 326 | 36,450 | Weizmann Institute of Science | 0000-0001-9004-1225 |
| Nitzan Rosenfeld | liquid_biopsy | A5059136338 | 217 | 26,090 | University of Cambridge | 0000-0002-2825-4788 |
| Omar O. Abudayyeh | gene_editing | A5033170303 | 128 | 38,877 | Harvard University | 0000-0002-7979-3220 |
| Patrick D. Hsu | ai_bio, gene_editing | A5060049408 | 110 | 54,062 | Arc Research Institute | 0000-0002-9380-2648 |
| Patrik Ståhl | spatial | A5083056852 | 93 | 8,824 | Science for Life Laboratory | 0000-0002-2207-7370 |
| Po-Ssu Huang | ai_bio | A5021369429 | 108 | 9,639 | Stanford University | 0000-0002-7948-2895 |
| Rahul Satija | single_cell | A5058180202 | 157 | 65,727 | New York Genome Center | 0000-0001-9448-8833 |
| Regina Barzilay | ai_bio | A5010124873 | 422 | 36,233 | MIT Computer Science and Artificial Intelligence Laboratory | 0000-0002-2921-8201 |
| Rickard Sandberg | single_cell | A5077749659 | 268 | 36,437 | Karolinska Institutet | 0000-0001-6473-1740 |
| Rong Fan | spatial | A5013529158 | 484 | 20,479 | Yale University | 0000-0001-7805-8059 |
| Ruth Etzioni | mced | A5064344218 | 514 | 27,103 | Fred Hutch Cancer Center | 0000-0002-9164-6370 |
| Sarah A. Teichmann | single_cell | A5028596003 | 862 | 94,294 | University of Cambridge | 0000-0002-6294-6366 |
| Sergey Ovchinnikov | ai_bio | A5084204507 | 187 | 31,520 | Massachusetts Institute of Technology | 0000-0003-2774-2744 |
| Sjors H. W. Scheres | structural_biology | A5061952716 | 495 | 46,843 | MRC Laboratory of Molecular Biology | 0000-0002-0462-6540 |
| Stanley Riddell | cell_therapy | A5087281332 | 562 | 50,015 | Fred Hutch Cancer Center | 0000-0002-4688-9920 |
| Sten Linnarsson | spatial | A5086924433 | 232 | 42,102 | Karolinska Institutet | 0000-0002-3491-3444 |
| Steven Gygi | proteomics | A5079793809 | 1185 | 174,252 | Harvard University | 0000-0001-7626-0034 |
| Tamir Gonen | structural_biology | A5084200031 | 430 | 15,567 | University of California, Los Angeles | 0000-0002-9254-4069 |
| Tanja Kortemme | ai_bio | A5000906927 | 248 | 26,955 | University of California, San Francisco | 0000-0002-8494-680X |
| Tobias Marschall | long_read | A5067570776 | 275 | 20,838 | Heinrich Heine University Düsseldorf | 0000-0002-9376-1030 |
| Tom Ellis | synthetic_biology | A5061080095 | 187 | 7,664 | Imperial College London | 0000-0001-5392-976X |
| Tommi Jaakkola | ai_bio | A5048915657 | 549 | 47,312 | IIT@MIT | 0000-0002-2199-0379 |
| Traver Hart | perturbation | A5033882667 | 161 | 10,844 | The University of Texas MD Anderson Cancer Center | 0000-0002-1880-3341 |
| Victor E. Velculescu | liquid_biopsy | A5006560755 | 934 | 119,878 | Johns Hopkins University | 0000-0003-1195-438X |
| Viktor Adalsteinsson | liquid_biopsy | A5002786000 | 252 | 7,314 | Broad Institute | 0000-0003-4555-2485 |
| Wah Chiu | structural_biology | A5049689333 | 1007 | 42,205 | SLAC National Accelerator Laboratory | 0000-0002-8910-3078 |
| Werner Kühlbrandt | structural_biology | A5003711513 | 399 | 26,642 | Max Planck Institute of Biophysics | 0000-0002-2013-4810 |
| Winston Timp | long_read | A5081156860 | 235 | 15,832 | Johns Hopkins University | 0000-0003-2083-6027 |
| Wolfgang Baumeister | structural_biology | A5029559203 | 696 | 52,635 | Max Planck Institute of Biochemistry | 0000-0001-8154-8809 |
| Xiaowei Zhuang | spatial | A5087959577 | 373 | 64,499 | Harvard University | 0000-0002-6034-7853 |
| Yifan Cheng | structural_biology | A5016221669 | 455 | 37,869 | University of California, San Francisco | 0000-0001-9535-0369 |

All institution evidence is the matching entry of the profile's `last_known_institutions` unless stated otherwise.
