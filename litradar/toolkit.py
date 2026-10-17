"""Raw access to every source adapter without the pipeline: the registry behind `litradar fetch`.

    litradar fetch --list                                   describe every source, its arguments, access and pacing
    litradar fetch biorxiv --days 7 --categories genomics   normalised records as JSON lines on stdout
    litradar fetch europepmc --section METHODS --terms "NovaSeq X" --since 2026-10-01 --out hits.jsonl
    litradar fetch sra --days 7 --limit 2000 --raw          the API's own payloads, one HTTP response per line

Each entry in REGISTRY is a thin wrapper around one module in `litradar.sources`: it maps command-line
options onto the module's existing functions and yields what they yield (`Paper`, `ArchiveRecord` or
`NewsItem` dataclasses, or plain dicts for enrichment sources). Nothing here pages, parses or
normalises; the description shown by `--list` is the module's own docstring and the pacing comes from
`litradar.http.DEFAULT_MIN_INTERVAL`, so a new adapter needs a single `SourceSpec` to become a
command-line tool.

`--raw` wraps the HTTP client in `RecordingHttp`, which hands every response body to the writer as it
arrives. That gives the API's own payload for every source, including adapters that build records
inline, without duplicating any request logic.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from itertools import islice
from types import ModuleType
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, TextIO, Tuple

from .config import Settings
from .http import DEFAULT_MIN_INTERVAL, Http
from .models import Paper
from .sources import (
    arxiv,
    biorxiv,
    cellxgene,
    clinicaltrials,
    crossref,
    ena,
    europepmc,
    geo,
    nih_reporter,
    openalex,
    openfda,
    pride,
    rss,
    semantic_scholar,
    sra,
)

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------- option catalogue
# Every option `litradar fetch` understands, defined once. A SourceSpec lists the names it uses, and the
# parser is built from the union, so `--list` and the parser can never disagree.


@dataclasses.dataclass(frozen=True)
class Opt:
    flag: str  # "--categories"
    help: str
    kind: str = "str"  # str | int | flag | list (comma-separated) | multi (repeatable)
    metavar: str = ""
    default: Any = None

    @property
    def dest(self) -> str:
        return self.flag.lstrip("-").replace("-", "_")


OPTS: Dict[str, Opt] = {
    o.flag: o
    for o in (
        Opt("--since", "window start, YYYY-MM-DD (default: --days ago)", metavar="DATE"),
        Opt("--until", "window end, YYYY-MM-DD, inclusive (default: today, UTC)", metavar="DATE"),
        Opt("--days", "look-back window when --since is not given (default 7)", kind="int", metavar="N"),
        Opt("--limit", "stop after N records (also caps the adapter's own paging where it supports a cap)", kind="int", metavar="N"),
        Opt("--categories", "comma-separated subject categories (bioRxiv/medRxiv spelling, or arXiv codes like q-bio.GN)", kind="list", metavar="A,B"),
        Opt("--fulltext", "also download open full text and fill `sections` (bioRxiv/medRxiv JATS, Europe PMC XML)", kind="flag"),
        Opt("--query", "a verbatim query in the source's own syntax (Europe PMC) or a title/abstract search (OpenAlex)", metavar="Q"),
        Opt("--issn", "journal ISSN; repeat the flag or separate with commas", kind="multi", metavar="ISSN"),
        Opt("--section", "Europe PMC section field: METHODS, BODY, COMP_INT, ACK_FUND, AFF or ABSTRACT (use with --terms)", metavar="FIELD"),
        Opt("--terms", "comma-separated phrases: section terms (Europe PMC), free text (ClinicalTrials.gov, NIH RePORTER)", kind="list", metavar="A,B"),
        Opt("--all-access", "Europe PMC section search: do not restrict to the open-access subset", kind="flag"),
        Opt("--filter", "OpenAlex works filter as key=value; repeatable (e.g. primary_topic.id=T10001)", kind="multi", metavar="K=V"),
        Opt("--company-authors", "OpenAlex: only works with a company-typed author institution", kind="flag"),
        Opt("--mode", "NIH RePORTER query family: s10 (default), keyword or sbir", metavar="MODE", default="s10"),
        Opt("--activity-codes", "NIH activity codes, comma-separated (e.g. R01,R44)", kind="list", metavar="A,B"),
        Opt("--org", "NIH awardee organisation name (substring match); repeatable", kind="multi", metavar="NAME"),
        Opt("--sponsor", "ClinicalTrials.gov sponsor/collaborator terms, comma-separated", kind="list", metavar="A,B"),
        Opt("--intervention", "ClinicalTrials.gov intervention terms, comma-separated", kind="list", metavar="A,B"),
        Opt("--kinds", "openFDA decision types, comma-separated: 510k (includes De Novo), pma (default: both)", kind="list", metavar="A,B"),
        Opt("--ivd", "openFDA: keep only in-vitro-diagnostic advisory committees (CH, HE, IM, MI, PA, TX, MG)", kind="flag"),
        Opt("--include-revisions", "CELLxGENE: also datasets whose latest revision landed in the window", kind="flag"),
        Opt("--extra-term", "GEO: extra esearch clause, e.g. '\"high-throughput sequencing\"[PTYP]'", metavar="TERM"),
        Opt("--doi", "a DOI to look up; repeatable, comma-separated, or '-' to read one DOI per line from stdin", kind="multi", metavar="DOI"),
        Opt("--url", "feed URL (RSS 2.0, RSS 1.0 or Atom)", metavar="URL"),
        Opt("--name", "feed name used in the record's `source`/`feed` field (default: the URL's host)", metavar="NAME"),
        Opt("--as", "RSS record type: news (default) or papers", metavar="KIND", default="news"),
    )
}

WINDOW = ("--since", "--until", "--days")

# ----------------------------------------------------------------------------- request context


@dataclasses.dataclass
class FetchRequest:
    """Everything a wrapper needs: the window, the cap, the parsed options and the configured identity/keys."""

    since: str
    until: str
    limit: Optional[int]
    opts: argparse.Namespace
    settings: Settings

    def opt(self, name: str, default: Any = None) -> Any:
        return getattr(self.opts, name, default)

    @property
    def max_records(self) -> Optional[int]:
        return self.limit if self.limit and self.limit > 0 else None


class UsageError(ValueError):
    """The source needs an option the caller did not give (exit status 2, message on stderr)."""


def window(days: int = 7) -> Tuple[str, str]:
    end = datetime.now(timezone.utc).date()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


def _list(value: Any) -> List[str]:
    """`--issn a --issn b,c` and `--terms "a, b"` both become ['a', 'b', 'c']."""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: List[str] = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _dois(req: FetchRequest) -> List[str]:
    raw = _list(req.opt("doi"))
    if "-" in raw:
        raw.remove("-")
        raw.extend(line.strip() for line in sys.stdin if line.strip())
    if not raw:
        raise UsageError("semantic-scholar needs --doi (repeatable, comma-separated, or '-' for stdin)")
    return raw


# ----------------------------------------------------------------------------- wrappers


def _biorxiv(server: str) -> Callable[[Http, FetchRequest], Iterator[Any]]:
    def run(http: Http, req: FetchRequest) -> Iterator[Any]:
        kw: Dict[str, Any] = {"server": server, "categories": _list(req.opt("categories")) or None}
        if req.max_records:
            kw["max_records"] = req.max_records
        for paper in biorxiv.fetch(http, req.since, req.until, **kw):
            if req.opt("fulltext"):
                biorxiv.attach_fulltext(http, paper)
            yield paper

    return run


def _europepmc(http: Http, req: FetchRequest) -> Iterator[Any]:
    query = req.opt("query")
    issns = _list(req.opt("issn"))
    section, terms = req.opt("section"), _list(req.opt("terms"))
    explicit_window = bool(req.opt("since") or req.opt("until") or req.opt("days"))
    if query:
        q = f"({query}) AND {europepmc.date_clause(req.since, req.until)}" if explicit_window else query
    elif issns:
        q = europepmc.journal_query(issns, req.since, req.until)
    elif section and terms:
        q = europepmc.section_query(section.upper(), terms, req.since, req.until, open_access_only=not req.opt("all_access"))
    else:
        raise UsageError("europepmc needs --query, --issn, or --section FIELD --terms A,B")
    log.info("europepmc query: %s", q)
    kw: Dict[str, Any] = {}
    if req.max_records:
        kw["max_records"] = req.max_records
    for rec in europepmc.search(http, q, **kw):
        paper = europepmc.to_paper(rec)
        if req.opt("fulltext"):
            europepmc.attach_fulltext(http, paper)
        yield paper


def _crossref(http: Http, req: FetchRequest) -> Iterator[Any]:
    issns = _list(req.opt("issn"))
    if not issns:
        raise UsageError("crossref needs --issn (repeatable or comma-separated)")
    kw: Dict[str, Any] = {"mailto": req.settings.contact_email}
    if req.max_records:
        kw["max_records"] = req.max_records
    for issn in issns:
        for item in crossref.journal_works(http, issn, req.since, req.until, **kw):
            yield crossref.to_paper(item)


def _openalex(http: Http, req: FetchRequest) -> Iterator[Any]:
    filters: Dict[str, str] = {"from_publication_date": req.since, "to_publication_date": req.until}
    for kv in _list(req.opt("filter")):
        if "=" not in kv:
            raise UsageError(f"openalex --filter expects key=value, got {kv!r}")
        k, _, v = kv.partition("=")
        filters[k.strip()] = v.strip()
    if req.opt("query"):
        filters["title_and_abstract.search"] = req.opt("query")
    issns = _list(req.opt("issn"))
    if issns:
        filters["primary_location.source.issn"] = openalex.or_join(issns)
    if req.opt("company_authors"):
        filters["authorships.institutions.type"] = "company"
    kw: Dict[str, Any] = {"mailto": req.settings.contact_email, "api_key": req.settings.openalex_api_key}
    if req.max_records:
        kw["max_records"] = req.max_records
    for work in openalex.works(http, filters, **kw):
        yield openalex.to_paper(work)


def _arxiv(http: Http, req: FetchRequest) -> Iterator[Any]:
    cats = _list(req.opt("categories")) or ["q-bio.GN", "q-bio.QM"]
    kw: Dict[str, Any] = {}
    if req.max_records:
        kw["max_records"] = req.max_records
    return arxiv.fetch(http, req.since, req.until, cats, **kw)


def _ena(http: Http, req: FetchRequest) -> Iterator[Any]:
    return ena.fetch(http, req.since, req.until, limit=req.max_records or 0)


def _sra(http: Http, req: FetchRequest) -> Iterator[Any]:
    kw: Dict[str, Any] = {"api_key": req.settings.ncbi_api_key}
    if req.max_records:
        kw["max_records"] = req.max_records
    return sra.fetch(http, req.since, req.until, **kw)


def _geo(http: Http, req: FetchRequest) -> Iterator[Any]:
    kw: Dict[str, Any] = {"api_key": req.settings.ncbi_api_key, "extra_term": req.opt("extra_term") or ""}
    if req.max_records:
        kw["max_records"] = req.max_records
    return geo.fetch(http, req.since, req.until, **kw)


def _pride(http: Http, req: FetchRequest) -> Iterator[Any]:
    return pride.fetch(http, req.since, req.until, max_projects=req.max_records or 0)


def _cellxgene(http: Http, req: FetchRequest) -> Iterator[Any]:
    return cellxgene.fetch(http, req.since, req.until, include_revisions=bool(req.opt("include_revisions")), max_records=req.max_records or 0)


def _nih_reporter(http: Http, req: FetchRequest) -> Iterator[Any]:
    mode = (req.opt("mode") or "s10").lower()
    terms = _list(req.opt("terms"))
    codes = _list(req.opt("activity_codes"))
    orgs = _list(req.opt("org"))
    kw: Dict[str, Any] = {}
    if req.max_records:
        kw["max_records"] = req.max_records
    if mode == "s10":
        return nih_reporter.s10_awards(http, req.since, req.until, terms=terms or None, **kw)
    if mode == "sbir":
        return nih_reporter.sbir_awards(http, req.since, req.until, org_names=orgs or None, terms=terms or None, **kw)
    if mode == "keyword":
        if not (terms or codes or orgs):
            raise UsageError("nih-reporter --mode keyword needs --terms, --activity-codes or --org")
        return nih_reporter.keyword_awards(http, req.since, req.until, terms, activity_codes=codes or None, org_names=orgs or None, **kw)
    raise UsageError(f"nih-reporter --mode must be s10, keyword or sbir (got {mode!r})")


def _clinicaltrials(http: Http, req: FetchRequest) -> Iterator[Any]:
    kw: Dict[str, Any] = {}
    if req.max_records:
        kw["max_records"] = req.max_records
    return clinicaltrials.new_studies(
        http,
        req.since,
        req.until,
        sponsor_terms=_list(req.opt("sponsor")) or None,
        intervention_terms=_list(req.opt("intervention")) or None,
        terms=_list(req.opt("terms")) or None,
        **kw,
    )


def _openfda(http: Http, req: FetchRequest) -> Iterator[Any]:
    kinds = _list(req.opt("kinds")) or ["510k", "pma"]
    kw: Dict[str, Any] = {"kinds": kinds}
    if req.opt("ivd"):
        kw["advisory_committees"] = openfda.IVD_ADVISORY_COMMITTEES
    if req.max_records:
        kw["max_records"] = req.max_records
    return openfda.fetch(http, req.since, req.until, **kw)


def _semantic_scholar(http: Http, req: FetchRequest) -> Iterator[Any]:
    dois = _dois(req)
    key = req.settings.semantic_scholar_api_key
    for start in range(0, len(dois), semantic_scholar.BATCH_LIMIT):
        chunk = dois[start : start + semantic_scholar.BATCH_LIMIT]
        found = semantic_scholar.fetch_batch(http, chunk, api_key=key)
        if found is None:
            # the batch request itself failed (throttled anonymous pool, network); say so rather than "not found"
            for doi in chunk:
                yield {"doi": doi, "found": None, "error": "batch request failed (throttled or unreachable); retry later or set SEMANTIC_SCHOLAR_API_KEY"}
            continue
        for doi in chunk:
            item = found.get(doi) or found.get(doi.lower())
            yield dict({"doi": doi, "found": item is not None}, **(item or {}))


def _rss(http: Http, req: FetchRequest) -> Iterator[Any]:
    url = req.opt("url")
    if not url:
        raise UsageError("rss needs --url")
    from urllib.parse import urlsplit

    name = req.opt("name") or urlsplit(url).netloc or "feed"
    since = req.since if (req.opt("since") or req.opt("days")) else None
    if (req.opt("as") or "news").lower().startswith("paper"):
        return rss.journal_papers(http, name, url, name, since=since)
    return rss.news_items(http, name, url, since=since)


# ----------------------------------------------------------------------------- registry


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    name: str
    module: ModuleType
    record: str  # Paper | ArchiveRecord | NewsItem | dict
    run: Callable[[Http, FetchRequest], Iterable[Any]]
    options: Tuple[str, ...]
    access: str
    hosts: Tuple[str, ...]
    example: str
    requires: str = ""  # which of the options is mandatory, in words

    @property
    def summary(self) -> str:
        """First paragraph of the module docstring, whitespace-collapsed."""
        doc = (self.module.__doc__ or "").strip()
        first = doc.split("\n\n", 1)[0]
        return " ".join(first.split())

    def pacing(self) -> str:
        parts = []
        for host in self.hosts:
            gap = DEFAULT_MIN_INTERVAL.get(host)
            if gap:
                rate = 1.0 / gap
                parts.append(f"{host} >= {gap:g} s between requests (~{rate:.3g}/s)")
            else:
                parts.append(f"{host}: no client-side gap; retries with backoff, 429s honoured")
        return "; ".join(parts) or "n/a"


_PREPRINT_OPTS = WINDOW + ("--categories", "--limit", "--fulltext")

REGISTRY: Tuple[SourceSpec, ...] = (
    SourceSpec(
        "biorxiv", biorxiv, "Paper", _biorxiv("biorxiv"), _PREPRINT_OPTS,
        access="free, no key", hosts=("api.biorxiv.org", "www.biorxiv.org"),
        example="litradar fetch biorxiv --days 7 --categories genomics,bioinformatics --out preprints.jsonl",
    ),
    SourceSpec(
        "medrxiv", biorxiv, "Paper", _biorxiv("medrxiv"), _PREPRINT_OPTS,
        access="free, no key", hosts=("api.biorxiv.org", "www.medrxiv.org"),
        example='litradar fetch medrxiv --days 7 --categories "genetic and genomic medicine,oncology"',
    ),
    SourceSpec(
        "europepmc", europepmc, "Paper", _europepmc,
        WINDOW + ("--query", "--issn", "--section", "--terms", "--all-access", "--limit", "--fulltext"),
        access="free, no key", hosts=("www.ebi.ac.uk",),
        example='litradar fetch europepmc --section METHODS --terms "NovaSeq X,Xenium" --days 7 --fulltext',
        requires="one of --query, --issn, or --section with --terms",
    ),
    SourceSpec(
        "crossref", crossref, "Paper", _crossref, WINDOW + ("--issn", "--limit"),
        access="free; LITRADAR_CONTACT_EMAIL is sent as mailto for the polite pool", hosts=("api.crossref.org",),
        example="litradar fetch crossref --issn 1548-7091 --days 14", requires="--issn",
    ),
    SourceSpec(
        "openalex", openalex, "Paper", _openalex,
        WINDOW + ("--filter", "--query", "--issn", "--company-authors", "--limit"),
        access="free API key required (OPENALEX_API_KEY; anonymous traffic is metered per IP); LITRADAR_CONTACT_EMAIL as mailto",
        hosts=("api.openalex.org",),
        example='litradar fetch openalex --company-authors --query "spatial transcriptomics" --days 30',
    ),
    SourceSpec(
        "arxiv", arxiv, "Paper", _arxiv, WINDOW + ("--categories", "--limit"),
        access="free, no key; arXiv asks for one request every 3 s", hosts=("export.arxiv.org",),
        example="litradar fetch arxiv --categories q-bio.GN,q-bio.QM --days 7",
    ),
    SourceSpec(
        "ena", ena, "ArchiveRecord", _ena, WINDOW + ("--limit",),
        access="free, no key", hosts=("www.ebi.ac.uk",),
        example="litradar fetch ena --days 7 --out ena.jsonl",
    ),
    SourceSpec(
        "sra", sra, "ArchiveRecord", _sra, WINDOW + ("--limit",),
        access="free; NCBI_API_KEY lifts E-utilities from 3 to 10 requests/s", hosts=("eutils.ncbi.nlm.nih.gov",),
        example="litradar fetch sra --days 7 --limit 5000 --out sra-sample.jsonl",
    ),
    SourceSpec(
        "geo", geo, "ArchiveRecord", _geo, WINDOW + ("--extra-term", "--limit"),
        access="free; NCBI_API_KEY lifts E-utilities from 3 to 10 requests/s", hosts=("eutils.ncbi.nlm.nih.gov",),
        example="litradar fetch geo --days 7 --extra-term '\"high-throughput sequencing\"[PTYP]' --limit 2000",
    ),
    SourceSpec(
        "pride", pride, "ArchiveRecord", _pride, WINDOW + ("--limit",),
        access="free, no key", hosts=("www.ebi.ac.uk",),
        example="litradar fetch pride --days 7",
    ),
    SourceSpec(
        "cellxgene", cellxgene, "ArchiveRecord", _cellxgene, WINDOW + ("--include-revisions", "--limit"),
        access="free, no key (the public dataset index is one ~10 MB request)", hosts=("api.cellxgene.cziscience.com",),
        example="litradar fetch cellxgene --days 30",
    ),
    SourceSpec(
        "nih-reporter", nih_reporter, "NewsItem", _nih_reporter,
        WINDOW + ("--mode", "--terms", "--activity-codes", "--org", "--limit"),
        access="free, no key; the API asks for at most one request per second", hosts=("api.reporter.nih.gov",),
        example="litradar fetch nih-reporter --mode s10 --since 2026-07-01 --until 2026-09-30",
    ),
    SourceSpec(
        "clinicaltrials", clinicaltrials, "NewsItem", _clinicaltrials,
        WINDOW + ("--terms", "--sponsor", "--intervention", "--limit"),
        access="free, no key; ~50 requests/minute per IP", hosts=("clinicaltrials.gov",),
        example="litradar fetch clinicaltrials --terms ctDNA --since 2026-10-01",
    ),
    SourceSpec(
        "openfda", openfda, "NewsItem", _openfda, WINDOW + ("--kinds", "--ivd", "--limit"),
        access="free; OPENFDA_API_KEY raises the daily quota from 1,000 to 120,000 requests", hosts=("api.fda.gov",),
        example="litradar fetch openfda --kinds 510k --ivd --days 45",
    ),
    SourceSpec(
        "semantic-scholar", semantic_scholar, "dict", _semantic_scholar, ("--doi",),
        access="free; SEMANTIC_SCHOLAR_API_KEY recommended (anonymous requests share one throttled pool)",
        hosts=("api.semanticscholar.org",),
        example="litradar fetch semantic-scholar --doi 10.1038/s41592-024-02201-0,10.1038/s41586-020-2308-7",
        requires="--doi",
    ),
    SourceSpec(
        "rss", rss, "NewsItem or Paper", _rss, ("--url", "--name", "--as", "--since", "--days"),
        access="depends on the feed host (several publisher and trade feeds answer 403 to scripts)", hosts=(),
        example="litradar fetch rss --url https://www.nature.com/nmeth.rss --as papers --days 14",
        requires="--url",
    ),
)

BY_NAME: Dict[str, SourceSpec] = {s.name: s for s in REGISTRY}


def get(name: str) -> SourceSpec:
    key = name.lower().replace("_", "-")
    if key not in BY_NAME:
        raise UsageError(f"unknown source {name!r}; choose from: {', '.join(BY_NAME)}")
    return BY_NAME[key]


# ----------------------------------------------------------------------------- raw mode


class RecordingHttp:
    """Proxy around an Http client that hands every response payload to `sink` as it arrives.

    Only the methods the adapters call are intercepted (get, get_json, get_text, post_json); everything
    else (stats, min_interval, blocked_hosts ...) is forwarded to the wrapped client.
    """

    def __init__(self, inner: Http, sink: Callable[[Dict[str, Any]], None]) -> None:
        self._inner = inner
        self._sink = sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, cache: bool = False) -> Optional[Any]:
        data = self._inner.get_json(url, params=params, cache=cache)
        self._sink({"method": "GET", "url": url, "params": params, "payload": data})
        return data

    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None, cache: bool = False) -> Optional[str]:
        text = self._inner.get_text(url, params=params, cache=cache)
        self._sink({"method": "GET", "url": url, "params": params, "payload": text})
        return text

    def post_json(self, url: str, json_body: Any, headers: Optional[Dict[str, str]] = None, ok_statuses: tuple = (200,)) -> Optional[Any]:
        data = self._inner.post_json(url, json_body, headers=headers, ok_statuses=ok_statuses)
        self._sink({"method": "POST", "url": url, "body": json_body, "payload": data})
        return data

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, cache: bool = False, ok_statuses: tuple = (200,)) -> Any:
        resp = self._inner.get(url, params=params, headers=headers, cache=cache, ok_statuses=ok_statuses)
        payload: Any = None
        status = None
        if resp is not None:
            status = getattr(resp, "status_code", None)
            try:
                payload = resp.json()
            except ValueError:
                try:
                    payload = resp.text
                except Exception:  # noqa: BLE001 - raw mode must never break the fetch it observes
                    payload = None
        self._sink({"method": "GET", "url": url, "params": params, "status": status, "payload": payload})
        return resp


# ----------------------------------------------------------------------------- running


def to_jsonable(rec: Any) -> Any:
    if dataclasses.is_dataclass(rec) and not isinstance(rec, type):
        return dataclasses.asdict(rec)
    return rec


def make_http(settings: Settings, browser: bool = False) -> Http:
    """The client `litradar fetch` uses; tests replace this to run the registry offline."""
    kw = {"contact_email": settings.contact_email, "cache_dir": settings.cache_dir, "ncbi_api_key": settings.ncbi_api_key}
    if browser:
        from .browser import BrowserHttp

        return BrowserHttp.from_settings(settings, **kw)
    return Http(**kw)


def records(spec: SourceSpec, http: Http, req: FetchRequest) -> Iterator[Any]:
    """Normalised records from one source, capped at req.limit."""
    it = iter(spec.run(http, req))
    return islice(it, req.limit) if req.limit and req.limit > 0 else it


def run_fetch(
    source: str,
    http: Http,
    settings: Settings,
    opts: argparse.Namespace,
    out: TextIO,
    raw: bool = False,
) -> int:
    """Stream one source to `out` as JSON lines; returns the number of lines written."""
    spec = get(source)
    since, until = _resolve_window(opts)
    req = FetchRequest(since=since, until=until, limit=getattr(opts, "limit", None), opts=opts, settings=settings)
    n = 0

    def write(obj: Any) -> None:
        nonlocal n
        out.write(json.dumps(to_jsonable(obj), ensure_ascii=False, default=str))
        out.write("\n")
        n += 1

    if raw:
        client: Any = RecordingHttp(http, write)
        for _ in records(spec, client, req):
            pass
    else:
        for rec in records(spec, http, req):
            write(rec)
    out.flush()
    return n


def _resolve_window(opts: argparse.Namespace) -> Tuple[str, str]:
    since, until, days = getattr(opts, "since", None), getattr(opts, "until", None), getattr(opts, "days", None)
    if since and until:
        return since, until
    if since:
        return since, datetime.now(timezone.utc).date().isoformat()
    if until:
        end = datetime.strptime(until, "%Y-%m-%d").date()
        return (end - timedelta(days=days or 7)).isoformat(), until
    return window(days or 7)


# ----------------------------------------------------------------------------- `--list`


def describe(spec: SourceSpec) -> Dict[str, Any]:
    return {
        "source": spec.name,
        "record": spec.record,
        "module": spec.module.__name__,
        "summary": spec.summary,
        "options": list(spec.options) + ["--raw", "--out"],
        "requires": spec.requires,
        "access": spec.access,
        "pacing": spec.pacing(),
        "example": spec.example,
    }


def format_list(specs: Sequence[SourceSpec] = REGISTRY) -> str:
    lines = [f"litradar fetch: raw access to {len(specs)} sources. Output is JSON lines (one record per line; --raw: one HTTP response per line).", ""]
    for s in specs:
        d = describe(s)
        lines.append(f"{s.name:17} {s.record:17} {d['summary']}")
        lines.append(f"{'':17} options:  {' '.join(d['options'])}")
        if d["requires"]:
            lines.append(f"{'':17} requires: {d['requires']}")
        lines.append(f"{'':17} access:   {d['access']}")
        lines.append(f"{'':17} pacing:   {d['pacing']}")
        lines.append(f"{'':17} example:  {d['example']}")
        lines.append("")
    lines.append("Window: --since/--until (YYYY-MM-DD, inclusive) or --days N (default 7, ending today UTC).")
    lines.append("Keys and identity are read from .env / the environment (see .env.example); none are required to start.")
    lines.append("--browser routes allow-listed hosts (bioRxiv/medRxiv full text) through a real browser; needs `pip install 'lit-radar[browser]'` (docs/browser.md).")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- argparse wiring


def add_fetch_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "fetch",
        help="raw access to one source without the pipeline; JSON lines out (see `litradar fetch --list`)",
        description="Stream normalised records (or --raw API payloads) from one source adapter. `--list` describes every source.",
    )
    p.add_argument("source", nargs="?", help="one of: " + ", ".join(BY_NAME))
    p.add_argument("--list", action="store_true", help="describe every source: arguments, access requirements, pacing")
    p.add_argument("--json", action="store_true", help="with --list: machine-readable JSON")
    p.add_argument("--out", default="-", help="output file (JSON lines) or '-' for stdout (default)")
    p.add_argument("--raw", action="store_true", help="emit the API's own payloads, one HTTP response per line, instead of records")
    p.add_argument("--browser", action="store_true", help="route allow-listed hosts through a real browser (optional extra; docs/browser.md)")
    used: Dict[str, List[str]] = {}
    for s in REGISTRY:
        for flag in s.options:
            used.setdefault(flag, []).append(s.name)
    for flag, opt in OPTS.items():
        if flag not in used:
            continue
        sources = used[flag]
        suffix = "" if len(sources) == len(REGISTRY) or flag in WINDOW or flag == "--limit" else f" [{', '.join(sources)}]"
        kw: Dict[str, Any] = {"help": opt.help + suffix, "dest": opt.dest}
        if opt.kind == "flag":
            kw["action"] = "store_true"
        else:
            kw["metavar"] = opt.metavar or None
            if opt.kind == "int":
                kw["type"] = int
            if opt.kind == "multi":
                kw["action"] = "append"
            if opt.default is not None:
                kw["default"] = opt.default
        p.add_argument(flag, **kw)
    p.set_defaults(func=cmd_fetch)
    return p


def cmd_fetch(args: argparse.Namespace) -> int:
    if args.list or not args.source:
        if args.json:
            print(json.dumps([describe(s) for s in REGISTRY], indent=1))
        else:
            print(format_list())
        return 0 if args.list else 2
    settings = Settings.from_env(config_dir=getattr(args, "config_dir", None))
    try:
        spec = get(args.source)
    except UsageError as exc:
        print(f"litradar fetch: {exc}", file=sys.stderr)
        return 2
    try:
        http = make_http(settings, browser=bool(args.browser))
    except Exception as exc:  # noqa: BLE001 - a missing optional extra is a usage problem, not a traceback
        print(f"litradar fetch: {exc}", file=sys.stderr)
        return 2
    out_path = args.out or "-"
    fh: TextIO = sys.stdout if out_path == "-" else open(out_path, "w", encoding="utf-8")  # noqa: SIM115 - closed below
    try:
        n = run_fetch(spec.name, http, settings, args, fh, raw=bool(args.raw))
    except UsageError as exc:
        print(f"litradar fetch {spec.name}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"litradar fetch {spec.name}: interrupted; partial output kept in {out_path}", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - adapters raise for closed budgets and dead hosts; report, do not traceback
        print(f"litradar fetch {spec.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if fh is not sys.stdout:
            fh.close()
        close = getattr(http, "close", None)
        if callable(close):
            close()
    what = "responses" if args.raw else spec.record + " records"
    print(f"litradar fetch {spec.name}: {n:,} {what} -> {out_path if out_path != '-' else 'stdout'}; http {dict(http.stats)}", file=sys.stderr)
    return 0


__all__ = [
    "REGISTRY", "BY_NAME", "SourceSpec", "FetchRequest", "RecordingHttp", "UsageError",
    "get", "records", "run_fetch", "describe", "format_list", "add_fetch_parser", "cmd_fetch", "make_http", "window", "Paper",
]
