"""Emerging concepts: technical phrases that are bursting or new in the literature.

No ML. Three extraction channels run over title + abstract (+ parsed Methods when present):

  (a) multiword technical terms   runs of 2-4 tokens that are capitalised words, hyphenated terms,
                                  tokens with digits or Greek letters (stop words break a run;
                                  Title Case titles and sentence-initial capitals are neutralised)
  (b) acronym definitions         "long form (ACRONYM)" resolved with a Schwartz-Hearst style match
  (c) seed-anchored phrases       a curated list of method/concept anchors (-seq, -omics, editing,
                                  atlas, foundation model, perturbation, organoid, nanopore, cell-free,
                                  in situ, single-molecule, spatial, long-read, digital twin, agent,
                                  autonomous lab, LNP, mRNA, bispecific, ADC, CAR-T, TCR, base/prime
                                  editor, epigenetic clock, virtual cell) extended by their modifiers

Product and company names from the gazetteers (config/instruments.yaml, config/companies.yaml) are
excluded: we want concepts, not products. Phrases are normalised (lower case, dashes unified to
spaces, simple plurals singularised) and counted as distinct papers per ISO week.

Tables created here (additive, `CREATE TABLE IF NOT EXISTS` on the store's connection):
  concept_counts(term, week, papers, display, computed_at)   weekly distinct-paper counts
  concept_papers(term, paper_id, week, channel)              which paper carried which term
  concepts(term, display, definition_json, model, scored_at)  optional Claude definitions

Burst score: for each term with >= `min_papers` papers in the current 4 weeks, compare the 4-week
total with the trailing 26-week baseline (weekly mean and std with a Poisson-style floor) and add a
novelty bonus when the term was first seen in the last 8 weeks. Terms first seen in the current 4
weeks are also ranked separately as "new this month".
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import yaml

from .extract.gazetteer import _is_ambiguous, load_entries
from .store import Store

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS concept_counts (
  term TEXT NOT NULL,
  week TEXT NOT NULL,
  papers INTEGER NOT NULL,
  display TEXT,
  computed_at TEXT,
  PRIMARY KEY (term, week)
);
CREATE INDEX IF NOT EXISTS idx_concept_counts_week ON concept_counts(week);

CREATE TABLE IF NOT EXISTS concept_papers (
  term TEXT NOT NULL,
  paper_id TEXT NOT NULL,
  week TEXT NOT NULL,
  channel TEXT,
  PRIMARY KEY (term, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_concept_papers_week ON concept_papers(week);
CREATE INDEX IF NOT EXISTS idx_concept_papers_paper ON concept_papers(paper_id);

CREATE TABLE IF NOT EXISTS concepts (
  term TEXT PRIMARY KEY,
  display TEXT,
  definition_json TEXT,
  model TEXT,
  scored_at TEXT
);
"""

# ---- defaults -----------------------------------------------------------------------------
CURRENT_WEEKS = 4
BASELINE_WEEKS = 26
NOVELTY_WEEKS = 8
MIN_PAPERS = 5
MIN_NEW_PAPERS = 3
STD_FLOOR = 1.0
NOVELTY_BONUS = 2.0
WEAK_BASELINE_WEEKS = 8  # fewer active baseline weeks than this and the z-scores are flagged as weak

# ---- vocabulary ---------------------------------------------------------------------------
STOP_WORDS: Set[str] = set(
    """a an the and or of in on at to for with by from as is are was were be been being this that these
    those we our us it its their they them he she his her i you your not no nor but if then than so such
    which who whom whose what when where while how why can could may might will would shall should must
    do does did done have has had having here there also both each either neither between among into onto
    over under within without across through during before after above below per via versus vs using used
    use based towards toward upon about against along around because however therefore thus hence whereas
    although though moreover furthermore respectively et al etc eg ie de la le von van der den des du los
    las el all any some many much more most less least few other another same own only just very even
    still yet already often always never again further off out up down well whether due show shown showed
    shows found find finds suggest suggests suggesting suggested indicate indicates indicating demonstrate
    demonstrates demonstrated reveal reveals revealed report reported reports describe described describes
    present presented presents propose proposed proposes provide provides provided compared compare
    comparing including include includes included identify identified identifies observed observe
    observes remain remains remained associated related regarding according whereby wherein hereby
    thereby delivered delivers deliver enabled enables enable enabling performed perform developed
    develop generated generate applied apply combined combine trained train built build revealed
    achieved achieve obtained obtain measured measure measuring tested test validated validate
    established conducted carried leveraged leveraging employed employ utilized utilised utilizing
    utilising introduced introduce allowing allows allow requiring requires require yielded yield
    exhibited exhibit displayed called termed named known designed implemented created produced
    induced treated exposed collected analyzed analysed estimated predicted detected characterized
    characterised quantified confirmed assessed evaluated examined investigated explored determined
    defined considered selected recruited enrolled excluded received underwent followed extracted
    isolated cultured incubated stained imaged sequenced mapped aligned filtered normalized normalised
    clustered annotated""".split()
)

# words that are not concepts on their own; a phrase made only of these is dropped
GENERIC_WORDS: Set[str] = set(
    """analysis analyses approach approaches method methods methodology study studies result results data
    dataset datasets model models modeling modelling framework frameworks system systems technique
    techniques technology technologies tool tools platform platforms strategy strategies novel new high
    low large small scale level levels improved enhanced efficient effective rapid robust comprehensive
    integrated integrative potential role roles effect effects impact impacts mechanism mechanisms
    function functions pathway pathways response responses regulation expression development
    application applications evaluation assessment characterization characterisation identification
    comparison review overview perspective perspectives insight insights evidence implication
    implications case cases report reports patient patients human humans mouse mice rat rats cell cells
    protein proteins gene genes disease diseases cancer cancers treatment treatments therapy therapies
    clinical trial trials randomized randomised controlled group groups cohort cohorts population
    populations sample samples control controls time year years month months week weeks day days first
    second third one two three four five six seven eight nine ten multiple several various different
    specific general total mean median significant significantly increase increased increases decrease
    decreased decreases association risk factor factors outcome outcomes performance accuracy quality
    type types number numbers rate rates early late recent current previous present future further
    additional overall open access article articles preprint abstract title figure figures fig table
    tables supplementary introduction discussion conclusion conclusions background objective objectives
    aim aims purpose design setting participants intervention interventions measure measures main
    primary secondary key major important united states university department institute hospital
    journal international national global world health organization organisation center centre school
    college laboratory foundation program programme committee council society association agency
    ministry author authors correspondence copyright license licence press limited inc ltd llc gmbh
    corporation company co january february march april june july august september october november
    december monday tuesday wednesday thursday friday saturday sunday north south east west
    new york london boston san francisco hong kong los angeles science sciences natural research medical
    medicine biomedical engineering chemistry physics uses africa europe asia america american european
    african asian chinese""".split()
)

# hyphenated adjective compounds that look technical but are prose
HYPHEN_STOP: Set[str] = set(
    """state-of-the-art so-called well-established well-known well-defined well-characterized long-term
    short-term real-time large-scale small-scale follow-up well-being in-depth cross-sectional
    double-blind single-blind placebo-controlled open-label dose-dependent time-dependent age-related
    sex-specific cost-effective high-quality low-cost high-risk low-risk non-significant one-year two-year
    three-year five-year ten-year in-hospital long-standing up-to-date case-control meta-analysis
    meta-analyses year-old post-hoc per-protocol intention-to-treat first-line second-line third-line
    one-sided two-sided high-dose low-dose full-text peer-reviewed open-access non-small small-cell
    cut-off built-in trade-off de-identified re-analysis co-first co-author co-authors pre-print
    pre-and post-treatment pre-treatment post-operative pre-operative peri-operative in-vivo in-vitro
    ex-vivo in-silico wild-type knock-out knock-in loss-of-function gain-of-function dose-response
    whole-body full-length high-throughput genome-wide transcriptome-wide proteome-wide multi-center
    multi-centre multi-site single-center single-centre non-invasive non-coding self-reported
    population-based hospital-based community-based evidence-based data-driven end-to-end
    cell-type-specific tissue-specific context-dependent concentration-dependent""".split()
)

# measurement units and statistics abbreviations that sit next to numbers
UNITS: Set[str] = set(
    """mm mM nm µm μm um kg mg ml mL µl μl ul h hr hrs min mins s sec secs °c c kda bp kb mb gb tb x fold ng
    pg rpm g cm mmol nmol µM μM uM da hz khz mhz ghz kv ev kev mev ms µs μs ns ps mv v a ma µa μa
    mol m km mi lb oz l dl cc ppm ppb hr/d mg/kg mg/ml ng/ml iu u% % n p sd se sem ci or hr rr vs
    md phd mph msc bsc""".split()
)

# boilerplate long forms and established generic phrases that would otherwise dominate the counts
GENERIC_PHRASES: Set[str] = set(
    " ".join(s.split())
    for s in """confidence interval|hazard ratio|odds ratio|relative risk|standard deviation|standard error|
    interquartile range|body mass index|area under the curve|receiver operating characteristic|quality of
    life|randomized controlled trial|randomised controlled trial|world health organization|united states|
    united kingdom|european union|food and drug administration|national institutes of health|machine
    learning|deep learning|artificial intelligence|neural network|convolutional neural network|polymerase
    chain reaction|magnetic resonance imaging|computed tomography|positron emission tomography|electronic
    health record|progression free survival|overall survival|adverse event|standard of care|principal
    component analysis|analysis of variance|enzyme linked immunosorbent assay|western blot|flow
    cytometry|gene ontology|false discovery rate|reactive oxygen species|extracellular matrix|central
    nervous system|blood brain barrier|type 2 diabete|type 1 diabete|cardiovascular disease|coronary
    artery disease|inflammatory bowel disease|mean arterial pressure|heart rate|blood pressure|intensive
    care unit|emergency department|length of stay|covid 19 pandemic|sars cov 2 infection|not applicable|
    not available|et al|e g|i e|wild type|room temperature|real time|generalized linear model|linear
    mixed model|mixed effect model|cross validation|hazard ratios|whole genome sequencing|whole exome
    sequencing|next generation sequencing|rna sequencing|single cell rna sequencing|single cell rna seq|
    rna seq|scrna seq|single cell|quantitative pcr|real time pcr|reverse transcription|bone marrow|
    stem cell|t cell|b cell|immune cell|tumor microenvironment|tumour microenvironment|gene expression|
    differential expression|differentially expressed gene|transcription factor|signaling pathway|
    signalling pathway|amino acid|fatty acid|nucleic acid|cell line|cell lines|mass spectrometry|
    liquid chromatography|high performance liquid chromatography|dna methylation|copy number variation|
    single nucleotide polymorphism|genome wide association study|genome wide association|minimum
    inhibitory concentration|lower limit|upper limit|systematic review|case report|case series|
    cross sectional study|cohort study|retrospective study|prospective study|pilot study|
    one way anova|two way anova|student t test|t test|wilcoxon rank sum|wilcoxon rank sum test|mann whitney u|
    mann whitney u test|kruskal wallis|kruskal wallis test|chi square|chi square test|fisher exact test|log rank test|
    bonferroni correction|benjamini hochberg|tukey hsd|principal component analysis|linear regression|logistic
    regression|cox proportional hazard|cox proportional hazards model|kaplan meier|standard error of the mean|
    phosphate buffered saline|fetal bovine serum|bovine serum albumin|triton x 100|tween 20|sodium dodecyl sulfate|
    dimethyl sulfoxide|polyvinylidene difluoride|horseradish peroxidase|ethylenediaminetetraacetic acid|hanks balanced
    salt solution|dulbecco modified eagle medium|penicillin streptomycin|16s rrna|16s rrna gene|genomic dna|total rna|
    complementary dna|messenger rna|graphpad prism|graphpad software|imagej|fiji|flowjo|r package|rstudio|r studio|
    python package|scikit learn|adobe illustrator|adobe photoshop|microsoft excel|spss statistics|ibm spss|
    institutional animal care|institutional review board|animal care and use committee|declaration of helsinki|
    informed consent|written informed consent|kyoto encyclopedia|gene set enrichment analysis|quality control|
    data availability|supplementary table|supplementary figure|extended data|source data|conflict of interest|
    competing interest|author contributions|creative commons|national natural science foundation|national science
    foundation|european research council|wellcome trust|howard hughes medical institute|medical research council|
    deutsche forschungsgemeinschaft|japan society for the promotion of science|frequently asked questions|
    body weight|cell viability|cell proliferation|western blotting|immunohistochemistry|immunofluorescence|
    electron microscopy|confocal microscopy|light microscopy|fluorescence microscopy|standard deviations|
    gene expression omnibus|sequence read archive|european nucleotide archive|uk biobank|genome atlas|
    cancer genome atlas|protein data bank|human protein atlas|all of us|million veteran program|
    peripheral blood mononuclear cell|endoplasmic reticulum|formalin fixed paraffin embedded|protein protein
    interaction|electrospray ionization|electrospray ionisation|double strand break|single stranded dna|
    double stranded dna|transmission electron microscopy|scanning electron microscopy|superoxide dismutase|
    principal coordinate analysis|amplicon sequence variant|operational taxonomic unit|average nucleotide
    identity|unique molecular identifier|bulk rna seq|single cell rna|single nucleus rna|green fluorescent
    protein|red fluorescent protein|glyceraldehyde 3 phosphate dehydrogenase|lactate dehydrogenase|
    nuclear magnetic resonance|high resolution mass spectrometry|tandem mass spectrometry|liquid chromatography
    mass spectrometry|liquid chromatography tandem mass spectrometry|gas chromatography mass spectrometry|
    atomic force microscopy|dynamic light scattering|surface plasmon resonance|isothermal titration
    calorimetry|circular dichroism|x ray crystallography|x ray diffraction|small angle x ray scattering|
    half maximal inhibitory concentration|minimum inhibitory concentration|colony forming unit|colony forming
    units|multiplicity of infection|post translational modification|open reading frame|untranslated region|
    coding sequence|guide rna|single guide rna|short hairpin rna|small interfering rna|micro rna|long non coding
    rna|messenger rna|transfer rna|ribosomal rna|support vector machine|random forest|coefficient of variation|
    enhanced chemiluminescence|fourier transform infrared|alkaline phosphatase|centered log ratio|molecular
    function|biological process|cellular component|region of interest|regions of interest|specific pathogen free|
    propidium iodide|variable importance in projection|non metric multidimensional scaling|available phosphorus|
    cerebrospinal fluid|short tandem repeat|quantitative real time pcr|real time quantitative pcr|fluorescence in
    situ hybridization|fluorescence in situ hybridisation|sub saharan africa|area under the receiver|mean squared
    error|root mean square|standard operating procedure|ethics committee|institutional ethics committee|
    hematoxylin and eosin|haematoxylin and eosin|optical density|relative abundance|fold change|log2 fold change|
    median fluorescence intensity|mean fluorescence intensity|lower limit of quantification|limit of detection|
    limit of quantification|transcripts per million|reads per kilobase|tris buffered saline|american type culture
    collection|bayesian information criterion|akaike information criterion|total nitrogen|total phosphorus|total
    carbon|total organic carbon|connectome project|human connectome project|hematopoietic stem cell|
    induced pluripotent stem cell|embryonic stem cell|mesenchymal stem cell""".split("|")
    if s.strip()
)

# -omics tokens that act as anchors for modifiers but are not concepts on their own
GENERIC_OMICS: Set[str] = {
    "omics", "genomics", "proteomics", "transcriptomics", "metabolomics", "epigenomics", "multiomics",
    "multi-omics", "metagenomics", "lipidomics", "pharmacogenomics", "immunogenomics", "radiomics",
}
# words ending in -omics that are not biology at all
NOT_OMICS: Set[str] = {"economics", "socioeconomics", "ergonomics", "bioeconomics", "macroeconomics", "microeconomics", "comics", "atomics", "agronomics"}

_GREEK_RE = re.compile(r"[Ͱ-Ͽ]")
_DASH_RE = re.compile(r"[‐-―−]")  # hyphen variants, en/em dash, minus
_NUMERIC_RE = re.compile(r"^[\d.,%±+\-/x×~<>=]+$")
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?(?:[-‐-―/+&.][^\W_]+)*\+?", re.UNICODE)
_BREAK_RE = re.compile(r"[,;:()\[\]{}\"“”«»?!\n|]|\.(?=\s|$)|\s[-‐-―]+(?=\s|$)")
_ACRONYM_RE = re.compile(r"\(\s*([A-Za-z][A-Za-z0-9\-]{1,9})s?\s*\)")

# ---- seed anchors (channel c): regex over the normalised run text, flags -------------------
# singleton: the anchor alone is a concept; "omics": singleton unless the token is in GENERIC_OMICS
_SEED_SPECS: List[Tuple[str, Any]] = [
    (r"[\w&+/.]*-seq", True),
    (r"(?:sc|sn|bulk|mi|total|small)?(?:rna|atac|chip|dna|ribo)seq", True),
    (r"[a-z][\w-]*omics", "omics"),
    (r"(?:base|prime|epigenome|epigenetic|genome|gene|rna|mitochondrial|multiplex(?:ed)?|programmable|crispr|dna|protein) editing", True),
    (r"(?:base|prime) editors?", True),
    (r"(?:cell|brain|tissue|tumou?r|reference|single-cell|spatial|developmental|organ|whole-body|molecular|protein|perturbation|immune|human|mouse|body|proteome|transcriptome|genome|epigenome|aging|ageing|disease|metabolic|gut|lung|heart|kidney|liver|skin) (?:[\w-]+ )?atlas(?:es)?", True),
    (r"foundation models?", True),
    (r"(?:genetic|single-cell|pooled|crispr|combinatorial|chemical|genome-wide|massively parallel|multiplexed|in vivo|systematic|transcriptional) perturbations?|perturbation (?:screens?|atlas(?:es)?|responses?|modeling|modelling|prediction|biology|data|maps?|experiments?|effects?)", True),
    (r"(?:patient-derived|brain|intestinal|tumou?r|cerebral|kidney|liver|lung|cardiac|retinal|gut|human|ipsc-derived|stem cell-derived|vascularized|vascularised|cortical|pancreatic|colon|colonic|gastric|airway|hepatic|neural) organoids?|organoid (?:intelligence|models?|cultures?|platforms?|biobanks?|screening|technology|systems?)|assembloids?", True),
    (r"nanopore (?:[\w-]+ )?(?:sequencing|protein sequencing|sensing|adaptive sampling|direct rna|basecalling|sensors?|detection)|solid-state nanopores?|nanopore-based [\w-]+|protein nanopores?|biological nanopores?", True),
    (r"cell-free (?:dna|rna|protein synthesis|expression|systems?|biosensors?|transcription|translation|tumou?r dna|methylation|fetal dna|nucleic acids?|gene expression|protein expression|synthesis)|cfdna|ctdna|cfrna", True),
    (r"in situ (?:sequencing|hybridi[sz]ation|transcriptom\w+|capture|profiling|genome sequencing|imaging|car-t|car t|proteom\w+|protein sequencing|cryo-?et|cryo-electron tomography|structural biology)", True),
    (r"single-molecule (?:[\w-]+ )?(?:imaging|sequencing|protein sequencing|localization microscopy|localisation microscopy|fish|tracking|detection|fluorescence|counting|proteomics|analysis|spectroscopy|force spectroscopy|footprinting)", True),
    (r"spatial (?:transcriptom\w+|proteom\w+|omics|multi-?omics|biology|metabolom\w+|genom\w+|epigenom\w+|atlas|profiling|single-cell|lipidom\w+|phenotyping|mapping|multiomics|proteogenomics)", True),
    (r"long-read (?:[\w-]+ )?(?:sequencing|rna sequencing|transcriptom\w+|metagenom\w+|single-cell|isoform|whole-genome|assembly|proteomics?|methylation|data)", True),
    (r"digital twins?", True),
    (r"(?:ai|llm|autonomous|agentic|language[- ]model|multi|intelligent|scientific|research|software|coding|generative|conversational|lab|laboratory) agents?|agentic (?:ai|systems?|workflows?|science|frameworks?|discovery)|ai (?:co-)?scientists?", True),
    (r"(?:autonomous|self-driving|closed-loop|robotic) (?:labs?|laboratory|laboratories|experimentation|discovery|science|research|synthesis)", True),
    # overlapping anchors live in separate patterns: finditer is non-overlapping within one regex
    (r"lipid nanoparticles?", True),
    (r"ionizable lipids?", True),
    (r"lnp (?:delivery|formulations?|platforms?)|mrna-lnps?", True),
    (r"(?:circular|self-amplifying|synthetic|modified|in vitro transcribed|ivt|therapeutic|personalized|personalised|individualized|individualised|trans-amplifying) mrna|mrna(?:-lnp| lnp| vaccines?| therap\w+| delivery| cancer vaccines?| medicines?| encoded|-encoded| display)|sarna|circrna vaccines?", True),
    (r"bispecific (?:antibod\w+|t[- ]cell engagers?|adcs?|molecules?)|bispecifics|trispecific [\w-]+|t[- ]cell engagers?|tri-?specific antibod\w+|multispecific antibod\w+", True),
    (r"antibody-drug conjugates?|antibody drug conjugates?|bispecific adcs?|adc payloads?|radioligand therap\w+|radiopharmaceuticals?", True),
    (r"car-t(?: cells?| cell therap\w+| therap\w+)?|car t cells?|car t-cells?|car-nk(?: cells?)?|car-macrophages?|car-m cells?|in vivo car-?t?|allogeneic car-t|armored car-t|car-t cell therap\w+", True),
    (r"tcr-t(?: cells?| therap\w+)?|tcr(?:-engineered| mimics?| therap\w+|-based therap\w+)|t cell receptor therap\w+|soluble tcrs?|tcr discovery", True),
    (r"(?:epigenetic|methylation|aging|ageing|proteomic|transcriptomic|biological|dna methylation|epigenetic aging|epigenetic ageing|glycan|immune) (?:age )?clocks?", True),
    (r"(?:ai )?virtual cells?", True),
]
_SEEDS: List[Tuple[re.Pattern, Any]] = [(re.compile(rf"(?<![\w-])(?:{p})(?![\w-])"), flag) for p, flag in _SEED_SPECS]

# a few single tokens that are concepts on their own (everything else needs >= 2 tokens)
SINGLETON_OK: Set[str] = {"cfdna", "ctdna", "cfrna", "crispri", "crispra", "assembloid", "assembloids", "sarna", "radiopharmaceutical", "radiopharmaceuticals", "bispecifics"}


# ---- normalisation ------------------------------------------------------------------------
def singularize(word: str) -> str:
    """Cheap singular form for the last token of a phrase (good enough for counting)."""
    w = word
    if len(w) <= 3 or w in ("species", "series", "studies", "analyses", "diabetes"):
        return w
    if w.endswith("ies") and len(w) > 5:
        return w[:-3] + "y"
    if w.endswith(("ss", "us", "is", "ics", "as", "os", "ses", "xes", "zes")):
        return w
    if w.endswith("s"):
        return w[:-1]
    return w


def normalize_token(tok: str) -> str:
    t = _DASH_RE.sub("-", tok).lower().strip(".-/+&")
    t = re.sub(r"['’]s$", "", t)
    return t


def normalize_term(phrase: str) -> str:
    """Canonical key: lower case, dashes/slashes to spaces, collapsed whitespace, last token singularised."""
    toks = [normalize_token(t) for t in _TOKEN_RE.findall(phrase)]
    toks = [t for t in toks if t]
    if not toks:
        return ""
    joined = " ".join(toks).replace("-", " ").replace("/", " ")
    parts: List[str] = []
    for p in joined.split():
        if not p:
            continue
        # RNAseq / scRNAseq -> rna seq / scrna seq (same key as RNA-seq); product names like NovaSeq are left alone
        m = re.fullmatch(r"((?:sc|sn|bulk|mi|total|small|si|cite|share|multi)?(?:rna|atac|chip|dna|ribo|tag|cut))seq", p)
        if m:
            parts.extend([m.group(1), "seq"])
        else:
            parts.append(p)
    if not parts:
        return ""
    parts[-1] = singularize(parts[-1])
    if parts[-1].endswith("omic") and len(parts[-1]) > 5:
        parts[-1] += "s"  # "spatial transcriptomic" (adjective) counts with "spatial transcriptomics"
    return " ".join(parts)


def _is_numeric(tok: str) -> bool:
    return bool(_NUMERIC_RE.match(tok)) or not re.search(r"[^\W\d_]", tok)


def _is_unit(tok: str) -> bool:
    return tok.lower() in UNITS or tok in UNITS


def _alpha_len(tok: str) -> int:
    return sum(1 for c in tok if c.isalpha())


# ---- exclusions ---------------------------------------------------------------------------
@dataclass
class Exclusions:
    """Gazetteer-derived phrases to drop, plus subgroup keywords for the overlap report."""

    exact: Set[str] = field(default_factory=set)       # any alias, matched as a whole phrase
    contained: Set[str] = field(default_factory=set)   # unambiguous aliases, dropped when they appear inside a phrase
    subgroups: Dict[str, Tuple[str, List[str]]] = field(default_factory=dict)  # id -> (name, normalised keywords)

    def blocks(self, key: str) -> bool:
        if not key:
            return True
        if key in self.exact or key in GENERIC_PHRASES:
            return True
        toks = key.split()
        for n in range(1, len(toks) + 1):
            for i in range(0, len(toks) - n + 1):
                if " ".join(toks[i : i + n]) in self.contained:
                    return True
        return False

    def subgroups_for(self, key: str) -> List[str]:
        """Subgroups whose keywords (or name) overlap the term: whole-phrase containment either way, or a
        shared distinctive token (>= 6 letters, not in the weak list), so 'genome editing' does not light up
        every group that mentions 'genome'."""
        ktoks_list = key.split()
        ktoks = set(ktoks_list)
        kbigrams = {" ".join(ktoks_list[i : i + 2]) for i in range(len(ktoks_list) - 1)}
        out = []
        for sid, (name, kws) in self.subgroups.items():
            hit = False
            for kw in list(kws) + [name]:
                if not kw:
                    continue
                if kw == key or f" {kw} " in f" {key} " or f" {key} " in f" {kw} ":
                    hit = True
                    break
                kw_toks = kw.split()
                if kbigrams & {" ".join(kw_toks[i : i + 2]) for i in range(len(kw_toks) - 1)}:
                    hit = True  # a shared bigram such as "single cell" or "base editor"
                    break
                shared = ktoks & set(kw_toks)
                if any(len(t) >= 6 and t not in GENERIC_WORDS and t not in STOP_WORDS and t not in _WEAK_OVERLAP for t in shared):
                    hit = True
                    break
            if hit:
                out.append(sid)
        return out


_WEAK_OVERLAP: Set[str] = {
    "single", "sequencing", "analysis", "genome", "genomic", "genomics", "cancer", "imaging", "multiplexed",
    "profiling", "detection", "methylation", "protein", "cellular", "clinical", "screening", "learning",
    "circulating", "biology", "molecular", "structure", "structural", "engineering", "therapy", "design",
    "transcriptomics", "transcriptomic", "proteomics", "proteomic", "metabolomics", "nucleus", "nuclear",
}


def load_exclusions(config_dir: Path) -> Exclusions:
    ex = Exclusions()
    config_dir = Path(config_dir)
    for fname, kind in (("instruments.yaml", "instrument"), ("companies.yaml", "company")):
        path = config_dir / fname
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        items = data.get("entries") if isinstance(data, dict) else data
        for e in load_entries(items or [], kind):
            names = list(e.aliases) + [e.name] + ([e.vendor] if e.vendor else []) + ([e.parent] if e.parent else [])
            for alias in names:
                key = normalize_term(alias)
                if not key:
                    continue
                ex.exact.add(key)
                compact = alias.replace(' ', '')
                brand_like = len(compact) >= 5 and not alias.isupper() and (
                    (alias == e.name and not e.context_required)
                    or (' ' in alias.strip() and any(ch.isupper() for ch in alias))
                    or (any(ch.isupper() for ch in alias[1:]) and ' ' not in alias and not e.context_required)
                )
                ambiguous = (_is_ambiguous(alias) or alias in e.case_sensitive_aliases or e.context_required) and not brand_like
                compact = key.replace(" ", "")
                if not ambiguous and len(compact) >= 4 and key not in STOP_WORDS and key not in GENERIC_WORDS:
                    ex.contained.add(key)
    sub_path = config_dir / "subgroups.yaml"
    if sub_path.exists():
        with open(sub_path, encoding="utf-8") as fh:
            sub = yaml.safe_load(fh) or {}
        for g in (sub.get("subgroups") or []) if isinstance(sub, dict) else []:
            sid = str(g.get("id") or g.get("name") or "")
            if not sid:
                continue
            kws = [normalize_term(k) for k in (g.get("keywords") or [])]
            ex.subgroups[sid] = (normalize_term(str(g.get("name") or sid.replace("_", " "))), [k for k in kws if k])
    return ex


# ---- extraction ---------------------------------------------------------------------------
@dataclass
class Candidate:
    key: str
    display: str
    channel: str  # term | acronym | seed
    acronym: str = ""


def _runs(text: str) -> List[Tuple[List[str], bool]]:
    """Split text into punctuation-free runs of tokens; the flag says whether the run opens a sentence."""
    runs: List[Tuple[List[str], bool]] = []
    sentence_start = True
    pos = 0
    for m in _BREAK_RE.finditer(text):
        seg = text[pos : m.start()]
        toks = _TOKEN_RE.findall(seg)
        if toks:
            runs.append((toks, sentence_start))
            sentence_start = False
        brk = m.group(0).strip()
        if brk in (".", "?", "!", "") or "\n" in m.group(0):
            sentence_start = True
        pos = m.end()
    toks = _TOKEN_RE.findall(text[pos:])
    if toks:
        runs.append((toks, sentence_start))
    return runs


def _is_title_case(text: str) -> bool:
    words = [w for w in _TOKEN_RE.findall(text) if _alpha_len(w) >= 4]
    if len(words) < 4:
        return False
    caps = sum(1 for w in words if w[0].isupper())
    return caps / float(len(words)) >= 0.6


def _is_stopish(low: str) -> bool:
    return low in STOP_WORDS or low in HYPHEN_STOP or _is_unit(low)


def _technical(tok: str, sentence_initial: bool, neutralise_caps: bool) -> bool:
    low = normalize_token(tok)
    if not low or _is_stopish(low) or low in GENERIC_WORDS:
        return False
    if _is_numeric(tok):
        return False
    if any(c.isdigit() for c in tok):
        return True
    if _GREEK_RE.search(tok):
        return True
    if re.search(r"[^\W\d_][-‐-―/+&][^\W\d_]", tok):
        return True
    letters = [c for c in tok if c.isalpha()]
    if len(letters) >= 2 and sum(1 for c in letters[1:] if c.isupper()) >= 1:
        return True  # CRISPR, scRNA, mRNA
    if tok[0].isupper() and not (sentence_initial or neutralise_caps):
        return True
    return False


def _content(tok: str) -> bool:
    low = normalize_token(tok)
    return bool(low) and len(low) >= 2 and not _is_stopish(low) and low not in GENERIC_WORDS and not _is_numeric(tok)


def _acceptable(tokens: Sequence[str], allow_single: bool = False, curated: bool = False) -> bool:
    toks = [t for t in tokens if t]
    if not toks or len(toks) > 4:
        return False
    if len(toks) == 1 and not allow_single:
        return False
    lows = [normalize_token(t) for t in toks]
    if any(not low for low in lows):
        return False
    if _is_stopish(lows[0]) or _is_stopish(lows[-1]):
        return False
    if not curated and all((low in GENERIC_WORDS or low in STOP_WORDS) for low in lows):
        return False  # seed anchors are curated, so "foundation model" passes even though both words are generic alone
    if not any(_alpha_len(t) >= 3 for t in toks):
        return False
    if any(_is_numeric(t) for t in toks):
        return False
    if all(_alpha_len(t) <= 3 and t.isupper() for t in toks):
        return False
    if sum(len(t) for t in toks) + len(toks) - 1 > 60:
        return False
    return True


def _emit(out: Dict[str, Candidate], tokens: Sequence[str], channel: str, excl: Optional[Exclusions], allow_single: bool = False, acronym: str = "") -> None:
    if not _acceptable(tokens, allow_single=allow_single, curated=(channel == "seed")):
        return
    display = " ".join(tokens)
    key = normalize_term(display)
    if not key or len(key.split()) > 4 or (len(key.split()) == 1 and not allow_single):
        return
    if key in GENERIC_PHRASES or (excl is not None and excl.blocks(key)):
        return
    if key not in out:
        out[key] = Candidate(key=key, display=display, channel=channel, acronym=acronym)


def _extract_technical(out: Dict[str, Candidate], runs: Sequence[Tuple[List[str], bool]], excl: Optional[Exclusions], neutralise_caps: bool) -> None:
    for toks, sentence_start in runs:
        flags = [_technical(t, sentence_start and i == 0, neutralise_caps) for i, t in enumerate(toks)]
        i = 0
        while i < len(toks):
            if not flags[i]:
                i += 1
                continue
            j = i
            while j < len(toks) and flags[j]:
                j += 1
            run = toks[i:j]
            if 2 <= len(run) <= 4:
                _emit(out, run, "term", excl)
            elif len(run) > 4:
                for k in range(0, len(run) - 3):
                    _emit(out, run[k : k + 4], "term", excl)
            i = j


def _best_long_form(short: str, long: str) -> Optional[str]:
    """Schwartz & Hearst (2003): walk the acronym backwards through the preceding text."""
    s_index = len(short) - 1
    l_index = len(long) - 1
    while s_index >= 0:
        cur = short[s_index].lower()
        if not cur.isalnum():
            s_index -= 1
            continue
        while (l_index >= 0 and long[l_index].lower() != cur) or (s_index == 0 and l_index > 0 and long[l_index - 1].isalnum()):
            l_index -= 1
        if l_index < 0:
            return None
        l_index -= 1
        s_index -= 1
    return long[l_index + 1 :].strip()


def _extract_acronyms(out: Dict[str, Candidate], text: str, excl: Optional[Exclusions]) -> None:
    for m in _ACRONYM_RE.finditer(text):
        acr = m.group(1)
        letters = [c for c in acr if c.isalpha()]
        if len(letters) < 2 or sum(1 for c in letters if c.isupper()) < 2:
            continue
        if acr.lower() in STOP_WORDS or _is_unit(acr):
            continue
        before = text[: m.start()]
        cut = max(before.rfind(c) for c in ".;:()[]\n!?") if before else -1
        before = before[cut + 1 :]
        toks = _TOKEN_RE.findall(before)
        max_tokens = min(len(acr) + 5, len(acr) * 2)
        window = toks[-max_tokens:] if max_tokens > 0 else []
        if not window:
            continue
        long = _best_long_form(acr, " ".join(window))
        if not long:
            continue
        lf_tokens = long.split()
        # drop a leading determiner the acronym did not need ("the Cancer Genome Atlas (TCGA)" keeps "the", fine)
        while lf_tokens and normalize_token(lf_tokens[0]) in STOP_WORDS and len(lf_tokens) > 1 and lf_tokens[0][0].lower() != acr[0].lower():
            lf_tokens = lf_tokens[1:]
        if not (2 <= len(lf_tokens) <= 4):
            continue
        if excl is not None and excl.blocks(normalize_term(acr)):
            continue
        _emit(out, lf_tokens, "acronym", excl, acronym=acr)


def _seed_flag_allows_single(flag: Any, anchor_norm: str) -> bool:
    if flag == "omics":
        last = anchor_norm.split()[-1] if anchor_norm else ""
        return last not in GENERIC_OMICS
    return bool(flag)


def _extract_seeds(out: Dict[str, Candidate], runs: Sequence[Tuple[List[str], bool]], excl: Optional[Exclusions]) -> None:
    for toks, _ in runs:
        norm = [normalize_token(t) for t in toks]
        if not any(norm):
            continue
        # offsets of each token in the joined normalised string
        starts: List[int] = []
        pos = 0
        for n in norm:
            starts.append(pos)
            pos += len(n) + 1
        joined = " ".join(norm)
        ends = [s + len(n) for s, n in zip(starts, norm)]
        for pat, flag in _SEEDS:
            for m in pat.finditer(joined):
                if flag == "omics" and m.group(0).split()[-1] in NOT_OMICS:
                    continue
                # token span covering the match
                try:
                    i = starts.index(m.start())
                except ValueError:
                    i = max([k for k, s in enumerate(starts) if s <= m.start()] or [0])
                j = i
                while j < len(toks) and ends[j] < m.end():
                    j += 1
                j = min(j + 1, len(toks))
                anchor = toks[i:j]
                anchor_norm = " ".join(norm[i:j])
                allow_single = _seed_flag_allows_single(flag, anchor_norm) or anchor_norm in SINGLETON_OK
                if allow_single:
                    _emit(out, anchor, "seed", excl, allow_single=True)
                # modifiers precede the head noun in English: extend to the left only. One plain content word
                # ("brain organoids"); a second only when it is itself technical ("CRISPR-based perturbation screens").
                if i > 0 and _content(toks[i - 1]):
                    _emit(out, toks[i - 1 : j], "seed", excl)
                    if i > 1 and (j - i + 2) <= 4 and _content(toks[i - 2]) and _technical(toks[i - 2], False, False):
                        _emit(out, toks[i - 2 : j], "seed", excl)


def extract_candidates(title: str, abstract: str = "", methods: str = "", excl: Optional[Exclusions] = None) -> Dict[str, Candidate]:
    """Return {normalised key: Candidate} for one paper."""
    out: Dict[str, Candidate] = {}
    title = title or ""
    abstract = abstract or ""
    methods = methods or ""
    if title:
        runs = _runs(title)
        _extract_technical(out, runs, excl, neutralise_caps=_is_title_case(title))
        _extract_seeds(out, runs, excl)
        _extract_acronyms(out, title, excl)
    if abstract:
        runs = _runs(abstract)
        _extract_technical(out, runs, excl, neutralise_caps=False)
        _extract_seeds(out, runs, excl)
        _extract_acronyms(out, abstract, excl)
    if methods:
        # Methods name reagents, buffers, statistics and software far more than ideas, so the capitalised-run
        # channel is skipped there; seed anchors and acronym definitions still count.
        text = methods[:20000]
        _extract_seeds(out, _runs(text), excl)
        _extract_acronyms(out, text, excl)
    return out


def extract_terms(text: str, excl: Optional[Exclusions] = None) -> Dict[str, str]:
    """Convenience: {key: display} for a blob of text treated as an abstract."""
    return {k: c.display for k, c in extract_candidates("", text, "", excl).items()}


# ---- weeks --------------------------------------------------------------------------------
def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_key(date_str: str) -> str:
    d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    return week_start(d).isoformat()


def weeks_ending(until: str, n: int) -> List[str]:
    """`n` ISO-week Monday dates (ascending) ending with the week that contains `until`."""
    last = week_start(datetime.strptime(until[:10], "%Y-%m-%d").date())
    return [(last - timedelta(weeks=n - 1 - i)).isoformat() for i in range(n)]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dicts(cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def ensure_schema(store: Store) -> None:
    store.conn.executescript(SCHEMA)
    store.conn.commit()


# ---- counting -----------------------------------------------------------------------------
def _paper_rows(store: Store, first_week: str, last_week: str) -> List[Dict[str, Any]]:
    last_day = (datetime.strptime(last_week, "%Y-%m-%d").date() + timedelta(days=6)).isoformat()
    cur = store.conn.execute(
        """SELECT id, title, abstract, sections_json, pub_date, first_seen FROM papers
           WHERE (pub_date >= ? AND pub_date <= ?)
              OR (pub_date = '' AND substr(first_seen,1,10) >= ? AND substr(first_seen,1,10) <= ?)""",
        (first_week, last_day, first_week, last_day),
    )
    return _dicts(cur)


def _effective_date(row: Dict[str, Any]) -> str:
    return (row.get("pub_date") or "")[:10] or (row.get("first_seen") or "")[:10]


def compute_week_counts(store: Store, weeks: Sequence[str], excl: Optional[Exclusions]) -> Dict[str, Any]:
    """Recompute concept_counts / concept_papers for the given weeks (Monday keys). Returns stats."""
    ensure_schema(store)
    weeks = sorted(set(weeks))
    if not weeks:
        return {"weeks": [], "papers": 0, "terms": 0}
    rows = _paper_rows(store, weeks[0], weeks[-1])
    wanted = set(weeks)
    per_week_papers: Dict[str, List[Tuple[str, str, str, str]]] = defaultdict(list)
    for r in rows:
        d = _effective_date(r)
        if not d:
            continue
        try:
            wk = week_key(d)
        except ValueError:
            continue
        if wk not in wanted:
            continue
        methods = ""
        if r.get("sections_json"):
            try:
                methods = (json.loads(r["sections_json"]) or {}).get("methods") or ""
            except ValueError:
                methods = ""
        per_week_papers[wk].append((r["id"], r.get("title") or "", r.get("abstract") or "", methods))
    now = _now()
    n_terms = 0
    n_papers = 0
    for wk in weeks:
        store.conn.execute("DELETE FROM concept_counts WHERE week=?", (wk,))
        store.conn.execute("DELETE FROM concept_papers WHERE week=?", (wk,))
        counts: Dict[str, Set[str]] = defaultdict(set)
        displays: Dict[str, Counter] = defaultdict(Counter)
        link_rows: List[Tuple[str, str, str, str]] = []
        for pid, title, abstract, methods in per_week_papers.get(wk, []):
            n_papers += 1
            cands = extract_candidates(title, abstract, methods, excl)
            for key, c in cands.items():
                counts[key].add(pid)
                displays[key][c.display] += 1
                link_rows.append((key, pid, wk, c.channel))
        store.conn.executemany("INSERT OR REPLACE INTO concept_papers (term, paper_id, week, channel) VALUES (?,?,?,?)", link_rows)
        store.conn.executemany(
            "INSERT OR REPLACE INTO concept_counts (term, week, papers, display, computed_at) VALUES (?,?,?,?,?)",
            [(key, wk, len(pids), displays[key].most_common(1)[0][0], now) for key, pids in counts.items()],
        )
        n_terms += len(counts)
        store.conn.commit()
    return {"weeks": weeks, "papers": n_papers, "terms": n_terms}


def ensure_counts(
    store: Store,
    until: str,
    weeks_total: int = BASELINE_WEEKS + CURRENT_WEEKS,
    excl: Optional[Exclusions] = None,
    refresh_weeks: int = 2,
    force: bool = False,
) -> Dict[str, Any]:
    """Incremental: compute weeks that have no counts yet, plus the latest `refresh_weeks` (papers still arrive)."""
    ensure_schema(store)
    weeks = weeks_ending(until, weeks_total)
    have = {r[0] for r in store.conn.execute("SELECT DISTINCT week FROM concept_counts WHERE week >= ? AND week <= ?", (weeks[0], weeks[-1]))}
    todo: List[str] = []
    for i, wk in enumerate(weeks):
        recent = i >= len(weeks) - refresh_weeks
        if force or recent or wk not in have:
            if not force and not recent and wk not in have:
                # skip empty weeks cheaply: nothing to count when the corpus has no paper that week
                last_day = (datetime.strptime(wk, "%Y-%m-%d").date() + timedelta(days=6)).isoformat()
                row = store.conn.execute(
                    "SELECT 1 FROM papers WHERE (pub_date >= ? AND pub_date <= ?) OR (pub_date='' AND substr(first_seen,1,10) >= ? AND substr(first_seen,1,10) <= ?) LIMIT 1",
                    (wk, last_day, wk, last_day),
                ).fetchone()
                if row is None:
                    continue
            todo.append(wk)
    stats = compute_week_counts(store, todo, excl) if todo else {"weeks": [], "papers": 0, "terms": 0}
    stats["window_weeks"] = weeks
    return stats


# ---- burst detection ----------------------------------------------------------------------
def score_term(current_total: int, baseline_series: Sequence[int], n_current_active: int, is_new: bool,
               std_floor: float = STD_FLOOR, novelty_bonus: float = NOVELTY_BONUS) -> Dict[str, float]:
    """z-score of the current window against the weekly baseline, plus a novelty bonus.

    expected = baseline weekly mean x active current weeks; the std is floored at a Poisson-style
    sqrt(expected) so a baseline of all zeros does not produce infinite z-scores."""
    n = len(baseline_series)
    mean = (sum(baseline_series) / float(n)) if n else 0.0
    var = (sum((x - mean) ** 2 for x in baseline_series) / float(n)) if n else 0.0
    std = math.sqrt(var)
    k = max(1, n_current_active)
    expected = mean * k
    sd = max(std * math.sqrt(k), math.sqrt(max(expected, 1.0)), std_floor)
    z = (current_total - expected) / sd
    score = z + (novelty_bonus if is_new else 0.0)
    return {
        "baseline_mean": round(mean, 3), "baseline_std": round(std, 3), "expected": round(expected, 2),
        "z": round(z, 2), "ratio": round(current_total / max(expected, 1.0), 2), "score": round(score, 2),
    }


def _venue(row: Dict[str, Any]) -> str:
    v = row.get("journal") or row.get("source") or ""
    return f"{v} (preprint)" if row.get("kind") == "preprint" else v


def _term_details(store: Store, term: str, cur_weeks: Sequence[str], excl: Optional[Exclusions], n_examples: int = 3) -> Dict[str, Any]:
    ph = ",".join("?" * len(cur_weeks))
    ex = _dicts(store.conn.execute(
        f"""SELECT p.title, p.journal, p.source, p.kind, p.url, p.pub_date
            FROM concept_papers cp JOIN papers p ON p.id = cp.paper_id LEFT JOIN scores s ON s.paper_id = p.id
            WHERE cp.term = ? AND cp.week IN ({ph})
            ORDER BY s.final DESC NULLS LAST, p.pub_date DESC LIMIT ?""",
        [term, *cur_weeks, n_examples],
    ))
    examples = [{"title": r["title"], "venue": _venue(r), "url": r["url"], "date": r.get("pub_date") or ""} for r in ex]
    co = _dicts(store.conn.execute(
        f"""SELECT m.kind, m.entity_id, m.entity_name, COUNT(DISTINCT m.paper_id) AS papers
            FROM concept_papers cp JOIN mentions m ON m.paper_id = cp.paper_id
            WHERE cp.term = ? AND cp.week IN ({ph})
            GROUP BY m.kind, m.entity_id ORDER BY papers DESC, m.entity_name""",
        [term, *cur_weeks],
    ))
    instruments = [{"name": r["entity_name"], "papers": r["papers"]} for r in co if r["kind"] == "instrument"][:6]
    companies = [{"name": r["entity_name"], "papers": r["papers"]} for r in co if r["kind"] == "company" and not str(r["entity_id"]).startswith("unlisted:")][:6]
    subgroups = excl.subgroups_for(term) if excl is not None else []
    return {"examples": examples, "instruments": instruments, "companies": companies, "subgroups": subgroups}


def burst_report(
    store: Store,
    until: str,
    baseline_weeks: int = BASELINE_WEEKS,
    current_weeks: int = CURRENT_WEEKS,
    min_papers: int = MIN_PAPERS,
    min_new_papers: int = MIN_NEW_PAPERS,
    novelty_weeks: int = NOVELTY_WEEKS,
    top: int = 30,
    excl: Optional[Exclusions] = None,
    with_details: bool = True,
) -> Dict[str, Any]:
    """Rank bursting and new terms from concept_counts. Does not recompute counts (see ensure_counts)."""
    ensure_schema(store)
    all_weeks = weeks_ending(until, baseline_weeks + current_weeks)
    cur_weeks = all_weeks[-current_weeks:]
    base_weeks = all_weeks[:-current_weeks]
    novelty_cut = weeks_ending(until, novelty_weeks)[0]
    rows = _dicts(store.conn.execute(
        "SELECT term, week, papers, display FROM concept_counts WHERE week >= ? AND week <= ?", (all_weeks[0], all_weeks[-1])
    ))
    first_seen: Dict[str, str] = {}
    for term, wk in store.conn.execute("SELECT term, MIN(week) FROM concept_counts GROUP BY term"):
        first_seen[term] = wk
    active_weeks = {r["week"] for r in rows}
    base_active = [w for w in base_weeks if w in active_weeks]
    cur_active = [w for w in cur_weeks if w in active_weeks]
    counts: Dict[str, Dict[str, int]] = defaultdict(dict)
    display: Dict[str, str] = {}
    for r in rows:
        counts[r["term"]][r["week"]] = int(r["papers"])
        if r["week"] in cur_weeks or r["term"] not in display:
            display[r["term"]] = r["display"] or r["term"]
    scored: List[Dict[str, Any]] = []
    new_terms: List[Dict[str, Any]] = []
    cur_set = set(cur_weeks)
    for term, by_week in counts.items():
        cur_total = sum(v for w, v in by_week.items() if w in cur_set)
        fs = first_seen.get(term, "")
        is_new_month = bool(fs) and fs >= cur_weeks[0]
        if is_new_month and cur_total >= min_new_papers:
            new_terms.append({"term": term, "display": display.get(term, term), "papers": cur_total, "first_seen": fs})
        if cur_total < min_papers:
            continue
        series = [by_week.get(w, 0) for w in base_active]
        is_new = bool(fs) and fs >= novelty_cut
        s = score_term(cur_total, series, len(cur_active) or 1, is_new)
        s.update({
            "term": term, "display": display.get(term, term), "papers": cur_total, "first_seen": fs, "is_new": is_new,
            "series": [by_week.get(w, 0) for w in all_weeks],
        })
        scored.append(s)
    scored.sort(key=lambda d: (-d["score"], -d["papers"], d["term"]))
    new_terms.sort(key=lambda d: (-d["papers"], d["term"]))
    risers = scored[:top]
    riser_terms = {d["term"] for d in risers}
    n_new_total = len(new_terms)
    new_terms = [d for d in new_terms if d["term"] not in riser_terms][:top]  # risers carry is_new themselves
    if with_details:
        for d in risers:
            d.update(_term_details(store, d["term"], cur_weeks, excl))
        for d in new_terms:
            d.update(_term_details(store, d["term"], cur_weeks, excl))
    defs = load_definitions(store, [d["term"] for d in risers] + [d["term"] for d in new_terms])
    for d in risers + new_terms:
        if d["term"] in defs:
            d["definition"] = defs[d["term"]]
    return {
        "until": until, "current_weeks": cur_weeks, "baseline_weeks": base_weeks,
        "baseline_active_weeks": len(base_active), "current_active_weeks": len(cur_active),
        "weak_baseline": len(base_active) < WEAK_BASELINE_WEEKS,
        "params": {"min_papers": min_papers, "min_new_papers": min_new_papers, "novelty_weeks": novelty_weeks, "std_floor": STD_FLOOR, "novelty_bonus": NOVELTY_BONUS},
        "candidates": len(scored), "risers": risers, "new_terms": new_terms, "new_terms_total": n_new_total,
        "weeks": all_weeks,
    }


# ---- optional Claude pass -----------------------------------------------------------------
VENDOR_CATEGORIES = [
    "sequencing", "single_cell", "spatial", "proteomics", "synthesis", "gene_editing_and_delivery", "cell_therapy",
    "diagnostics", "imaging_and_structural", "software_and_ai", "lab_automation", "pharma_modality", "other", "none",
]

LLM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "the candidate term exactly as given"},
                    "definition": {"type": "string", "description": "<= 30 words, plain language, what it is and what it is for"},
                    "status": {"type": "string", "enum": ["new_concept", "renamed_or_rebranded", "established", "not_a_concept"],
                               "description": "new_concept = a genuinely new idea/method; renamed_or_rebranded = an older concept under a new name; established = well known for years; not_a_concept = noise (a product, a place, a generic phrase)"},
                    "older_name": {"type": "string", "description": "the established name this is a rename of, or empty"},
                    "vendor_categories": {"type": "array", "items": {"type": "string", "enum": VENDOR_CATEGORIES}},
                    "commercial_relevance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "relevance for life-science tools and diagnostics vendors: 1 none .. 5 direct, near-term"},
                    "rationale": {"type": "string", "description": "<= 25 words"},
                },
                "required": ["term", "definition", "status", "older_name", "vendor_categories", "commercial_relevance", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["concepts"],
    "additionalProperties": False,
}

LLM_SYSTEM = """You classify candidate technical concepts extracted automatically from new life-science papers and preprints (titles, abstracts, Methods). Each candidate comes with its paper count over the last four weeks, when it was first seen in the corpus, and up to three example titles.

For every candidate return: a one-line plain-language definition; whether it is a genuinely new concept, an older concept under a new name (give the older name), an established concept, or not a concept at all (a product name, a place, boilerplate); which vendor categories it affects; and a 1-5 commercial-relevance score for companies selling life-science tools and diagnostics (1 = none, 3 = changes demand for an identifiable product class, 5 = direct, near-term revenue or competitive impact).

Be strict and consistent. Do not invent meaning for noise: label noise not_a_concept with relevance 1. Return only the JSON object."""

LLM_CHUNK = 20


def _llm_user_message(cands: Sequence[Dict[str, Any]]) -> str:
    lines = ["CANDIDATE CONCEPTS (term | papers in last 4 weeks | first seen week | example titles):", ""]
    for i, c in enumerate(cands, 1):
        titles = "; ".join((e.get("title") or "").replace("\n", " ") for e in (c.get("examples") or [])[:3])
        lines.append(f"{i}. {c.get('display') or c['term']} | {c.get('papers', '?')} | {c.get('first_seen') or '?'} | {titles or '(no examples)'}")
    lines.append("")
    lines.append("Return one entry per candidate, with `term` copied exactly from the list.")
    return "\n".join(lines)


def estimate_cost_usd(n_items: int, model: str, use_batch: bool) -> float:
    prices = {  # $ per million tokens (input, output); mirrors litradar/rank/llm.py
        "claude-opus-5-5": (4.0, 20.0),
        "claude-sonnet-5-5": (2.0, 10.0),
        "claude-haiku-5-5": (0.10, 0.50),
        "claude-fable-5-1": (10.0, 50.0),
    }
    pin, pout = prices.get(model, (4.0, 20.0))
    n_requests = max(1, math.ceil(n_items / float(LLM_CHUNK)))
    tokens_in = n_requests * 450 + n_items * 110
    tokens_out = n_items * 90
    cost = (tokens_in * pin + tokens_out * pout) / 1e6
    return round(cost * (0.5 if use_batch else 1.0), 3)


def define_concepts(
    candidates: Sequence[Dict[str, Any]],
    model: str = "claude-opus-5-5",
    api_key: str = "",
    use_batch: bool = False,
    poll_seconds: int = 30,
    max_wait_seconds: int = 3600,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Return {term: definition dict or None if refused/failed}. Needs the optional `anthropic` package."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pip install 'lit-radar[llm]' to enable the Claude concept pass") from exc

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    system = [{"type": "text", "text": LLM_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    output_config = {"format": {"type": "json_schema", "schema": LLM_SCHEMA}}
    chunks = [list(candidates[i : i + LLM_CHUNK]) for i in range(0, len(candidates), LLM_CHUNK)]
    results: Dict[str, Optional[Dict[str, Any]]] = {}

    def _parse(message: Any, chunk: Sequence[Dict[str, Any]]) -> None:
        if getattr(message, "stop_reason", None) == "refusal":
            for c in chunk:
                results[c["term"]] = None
            return
        text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            for c in chunk:
                results[c["term"]] = None
            return
        by_term: Dict[str, Dict[str, Any]] = {}
        for item in data.get("concepts") or []:
            t = str(item.get("term") or "")
            by_term[t.lower().strip()] = item
            by_term[normalize_term(t)] = item
        for c in chunk:
            item = by_term.get(c["term"]) or by_term.get((c.get("display") or "").lower().strip()) or by_term.get(normalize_term(c.get("display") or ""))
            results[c["term"]] = item

    if not use_batch or len(chunks) <= 1:
        for chunk in chunks:
            try:
                msg = client.messages.create(
                    model=model,
                    max_tokens=8192,
                    system=system,
                    messages=[{"role": "user", "content": _llm_user_message(chunk)}],
                    output_config=output_config,
                )
                _parse(msg, chunk)
            except anthropic.RateLimitError:
                time.sleep(20)
                for c in chunk:
                    results[c["term"]] = None
            except anthropic.APIStatusError as exc:
                log.warning("concept definitions failed: %s", exc)
                for c in chunk:
                    results[c["term"]] = None
        for c in candidates:
            results.setdefault(c["term"], None)
        return results

    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    requests: List[Request] = []
    for i, chunk in enumerate(chunks):
        requests.append(
            Request(
                custom_id=f"concepts-{i}",
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=8192,
                    system=system,
                    messages=[{"role": "user", "content": _llm_user_message(chunk)}],
                    output_config=output_config,
                ),
            )
        )
    batch = client.messages.batches.create(requests=requests)
    log.info("submitted batch %s with %d requests", batch.id, len(requests))
    waited = 0
    while waited < max_wait_seconds:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(poll_seconds)
        waited += poll_seconds
    if batch.processing_status != "ended":
        log.warning("batch %s still %s after %ss; results will be missing", batch.id, batch.processing_status, waited)
        return {c["term"]: None for c in candidates}
    for res in client.messages.batches.results(batch.id):
        try:
            idx = int(str(res.custom_id).rsplit("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if idx >= len(chunks):
            continue
        if res.result.type == "succeeded":
            _parse(res.result.message, chunks[idx])
        else:
            for c in chunks[idx]:
                results[c["term"]] = None
    for c in candidates:
        results.setdefault(c["term"], None)
    return results


def save_definitions(store: Store, defs: Dict[str, Optional[Dict[str, Any]]], displays: Dict[str, str], model: str) -> int:
    ensure_schema(store)
    n = 0
    now = _now()
    for term, d in defs.items():
        if not d:
            continue
        store.conn.execute(
            "INSERT OR REPLACE INTO concepts (term, display, definition_json, model, scored_at) VALUES (?,?,?,?,?)",
            (term, displays.get(term, term), json.dumps(d, ensure_ascii=False), model, now),
        )
        n += 1
    store.conn.commit()
    return n


def load_definitions(store: Store, terms: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ensure_schema(store)
    terms = list(dict.fromkeys(terms))
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(terms), 500):
        chunk = terms[i : i + 500]
        ph = ",".join("?" * len(chunk))
        for term, dj, scored_at in store.conn.execute(f"SELECT term, definition_json, scored_at FROM concepts WHERE term IN ({ph})", chunk):
            try:
                d = json.loads(dj) if dj else None
            except ValueError:
                d = None
            if d:
                d["scored_at"] = scored_at
                out[term] = d
    return out


# ---- rendering ----------------------------------------------------------------------------
def _link(title: str, url: str) -> str:
    title = (title or "(untitled)").replace("|", "/").replace("\n", " ").strip()
    return f"[{title}]({url})" if url else title


def _row(cells: List[Any]) -> str:
    return "| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in cells) + " |"


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_nothing in this window_\n"
    out = [_row(headers), "|" + "|".join("---" for _ in headers) + "|"]
    out += [_row(r) for r in rows]
    return "\n".join(out) + "\n"


def _spark(series: Sequence[int]) -> str:
    bars = "▁▂▃▄▅▆▇█"
    if not series:
        return ""
    hi = max(series) or 1
    return "".join(bars[min(len(bars) - 1, int(round((v / float(hi)) * (len(bars) - 1))))] for v in series)


def render_report_markdown(report: Dict[str, Any], top: int = 15, heading: str = "## Emerging concepts", with_examples: bool = True) -> str:
    md: List[str] = [heading + "\n"]
    cw = report["current_weeks"]
    window_end = (datetime.strptime(cw[-1], "%Y-%m-%d").date() + timedelta(days=6)).isoformat()
    md.append(
        f"Technical phrases (methods, assays, modalities, model classes) extracted from titles, abstracts and parsed Methods with no ML, "
        f"counted as distinct papers per week and compared with a trailing {len(report['baseline_weeks'])}-week baseline. "
        f"Product and company names from the gazetteers are excluded. Current window: {cw[0]} to {window_end} ({len(cw)} weeks); "
        f"baseline weeks with data: {report['baseline_active_weeks']}. Score = z-score against the baseline plus a novelty bonus for terms first seen in the last {report['params']['novelty_weeks']} weeks.\n"
    )
    if report.get("weak_baseline"):
        md.append(
            f"_Weak baseline: only {report['baseline_active_weeks']} baseline week(s) hold papers, so the z-scores mostly reflect raw counts and almost every term counts as new. "
            f"Treat this section as a candidate list until the corpus holds {WEAK_BASELINE_WEEKS}+ weeks._\n"
        )
    risers = report["risers"][:top]
    rows = []
    for d in risers:
        subs = ", ".join(d.get("subgroups") or []) or "—"
        co = ", ".join(x["name"] for x in (d.get("instruments") or [])[:3] + (d.get("companies") or [])[:2]) or "—"
        flag = "new" if d.get("is_new") else ""
        rows.append([d["display"], d["papers"], f"{d['baseline_mean']:.1f}", d["score"], flag, _spark(d.get("series") or []), subs, co])
    md.append("### Top risers\n")
    md.append(_table(["Concept", "Papers (window)", "Baseline /wk", "Score", "", "Trend", "Subgroups", "Co-mentioned"], rows))
    new_terms = report["new_terms"][:top]
    md.append("### New this month\n")
    n_new_in_risers = sum(1 for d in risers if d.get("first_seen") and d["first_seen"] >= cw[0])
    if new_terms:
        lead = f"{n_new_in_risers} of the risers above are first appearances; further terms " if n_new_in_risers else "Terms "
        md.append(lead + "whose first appearance in the corpus falls inside the current window: " + ", ".join(f"{d['display']} ({d['papers']})" for d in new_terms) + "\n")
    elif n_new_in_risers:
        md.append(f"_{n_new_in_risers} of the risers above are first appearances; no further new terms above the paper threshold_\n")
    else:
        md.append("_no new terms above the paper threshold_\n")
    if with_examples and risers:
        md.append("### Examples\n")
        for d in risers:
            line = f"- **{d['display']}**"
            dfn = d.get("definition")
            if dfn:
                status = {"new_concept": "new concept", "renamed_or_rebranded": f"rename of {dfn.get('older_name') or '?'}", "established": "established", "not_a_concept": "not a concept"}.get(dfn.get("status"), dfn.get("status"))
                line += f" — {dfn.get('definition', '')} <sub>{status} · commercial relevance {dfn.get('commercial_relevance')}/5 · {', '.join(dfn.get('vendor_categories') or [])}</sub>"
            ex = " · ".join(f"{_link(e['title'][:90], e['url'])} <sub>{e['venue']}</sub>" for e in (d.get("examples") or []))
            if ex:
                line += f"  \n  {ex}"
            md.append(line)
        md.append("")
    return "\n".join(md)


def render_concepts_markdown(store: Store, since: str, until: str, top: int = 15, config_dir: Optional[Path] = None) -> str:
    """Digest section. The current window is the digest window widened to at least 4 whole weeks; counts are
    computed incrementally on first use (one pass over papers the store has not counted yet)."""
    try:
        d0 = datetime.strptime(since[:10], "%Y-%m-%d").date()
        d1 = datetime.strptime(until[:10], "%Y-%m-%d").date()
        span_weeks = max(1, int(math.ceil(((d1 - d0).days + 1) / 7.0)))
    except ValueError:
        span_weeks = 1
    current_weeks = max(CURRENT_WEEKS, span_weeks)
    cfg = Path(config_dir) if config_dir else Path(__file__).resolve().parent.parent / "config"
    excl = load_exclusions(cfg)
    ensure_counts(store, until, weeks_total=BASELINE_WEEKS + current_weeks, excl=excl)
    report = burst_report(store, until, baseline_weeks=BASELINE_WEEKS, current_weeks=current_weeks, top=top, excl=excl)
    return render_report_markdown(report, top=top)
