"""`litradar track ...` and `litradar untrack ...`: change what the index follows without editing YAML by hand.

  litradar track company "<name>" [--ticker X] [--type tools|...] [--alias A ...] [--domain D ...] [--watch-news]
  litradar track instrument "<name>" --vendor V --category C [--alias A ...] [--context T ...] [--launched YYYY]
  litradar track author "<name>" --subgroup ID [--openalex A...] [--orcid ...] [--institution ...] [--yes]
  litradar track topic <subgroup-id> [--keyword K ...] [--openalex-topic T... ...] [--name "New subgroup"]
  litradar track term "<text>"
  litradar track list [company|instrument|author|topic|term|priority] [--json]
  litradar untrack company|instrument|author|topic|term <id|name> [--subgroup ID] [--keyword K] [--openalex-topic T]

Every write backs the file up to .cache/config-backups/, re-validates it and rolls back on failure.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from .. import track as T
from ..config import Settings
from ..http import Http


def _settings(args) -> Settings:
    return Settings.from_env(config_dir=args.config_dir)


def _http(settings: Settings) -> Http:
    return Http(contact_email=settings.contact_email, cache_dir=settings.cache_dir)


def _confirm(yes: bool, prompt: str) -> bool:
    if yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower().startswith("y")
    except EOFError:
        return False


def _done(what: str, bak) -> None:
    print(what)
    if bak:
        print(f"  backup: {bak}")


def _fail(exc: Exception) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 1


# ----------------------------------------------------------------------------- track
def cmd_track_company(args) -> int:
    settings = _settings(args)
    ticker = (args.ticker or "").strip()
    if not ticker and not args.no_lookup:
        try:
            rows = T.sec_company_tickers(_http(settings), settings.cache_dir)
            best, conf, top = T.match_ticker(args.name, rows)
            if best:
                ticker = best["ticker"]
                print(f"SEC match: {best['title']} -> {ticker} (confidence {conf:.2f}, CIK {best['cik']})")
            else:
                print(f"no confident SEC match for {args.name!r} (best {conf:.2f} < {T.TICKER_MIN_CONFIDENCE}); writing ticker: private")
                for sc, r in top[:3]:
                    print(f"  candidate {sc:.2f}  {r['ticker']:<8} {r['title']}")
                if top:
                    print("  re-run with --ticker to set one of these")
        except T.TrackError as exc:
            print(f"SEC lookup skipped: {exc}")
    try:
        entry, bak = T.add_company(
            settings, args.name, ticker=ticker, type_=args.type, aliases=args.alias or [], domains=args.domain or [],
            watch_news=bool(args.watch_news), parent=args.parent or "", entry_id=args.id or "",
        )
    except T.TrackError as exc:
        return _fail(exc)
    _done(f"added to companies.yaml:\n  {T.format_flow_entry(entry, T.COMPANY_KEY_ORDER)}", bak)
    print("next: `litradar rematch --since 90d` re-annotates stored papers with the new entry")
    return 0


def cmd_track_instrument(args) -> int:
    settings = _settings(args)
    try:
        entry, bak = T.add_instrument(
            settings, args.name, vendor=args.vendor, category=args.category, aliases=args.alias or [],
            context_terms=args.context or [], ticker=args.ticker or "", launched=args.launched, entry_id=args.id or "",
        )
    except T.TrackError as exc:
        return _fail(exc)
    _done(f"added to instruments.yaml:\n  {T.format_flow_entry(entry, T.INSTRUMENT_KEY_ORDER)}", bak)
    if not entry.get("aliases"):
        print("tip: add the spellings Methods sections use with --alias (e.g. \"<name> system\", \"<vendor> <name>\")")
    print("next: `litradar rematch --since 90d`; add archive vocabulary to config/archive_models.yaml for `litradar timeseries`")
    return 0


def _pick_openalex(settings: Settings, http: Http, name: str, yes: bool) -> Optional[Dict[str, Any]]:
    cands = T.openalex_author_candidates(http, name, api_key=settings.openalex_api_key, mailto=settings.contact_email)
    if not cands:
        print("OpenAlex returned no author profile for that name")
        return None
    top = cands[0]
    print(f"OpenAlex top match: {top['display_name']} ({top['id']}): {top['works_count']} works, {top['cited_by_count']} citations, {top['institution'] or 'no institution listed'}")
    for c in cands[1:4]:
        print(f"  also: {c['display_name']} ({c['id']}): {c['works_count']} works, {c['institution'] or 'no institution'}")
    if _confirm(yes, f"record {top['id']} for {name!r}?"):
        return top
    print("not accepted: recording the name only (re-run with --openalex A... or --yes)")
    return None


def _pick_orcid(http: Http, name: str, yes: bool) -> Optional[Dict[str, Any]]:
    cands = T.orcid_candidates(http, name)
    if not cands:
        print("ORCID search returned nothing")
        return None
    top = cands[0]
    label = f"{top['given_names']} {top['family_names']} {top['orcid']} ({', '.join(top['institutions'][:2]) or 'no institution listed'})"
    exact = [c for c in cands if c["exact"]]
    if top["exact"] and len(exact) == 1:
        print(f"ORCID: {label} (single exact name match)")
        return top
    print(f"ORCID top match: {label}; {len(cands)} result(s)" + (f", {len(exact)} exact" if exact else ""))
    for c in cands[1:4]:
        print(f"  also: {c['given_names']} {c['family_names']} {c['orcid']} ({', '.join(c['institutions'][:1]) or 'no institution'})")
    if _confirm(yes, f"record ORCID {top['orcid']} for {name!r}?"):
        return top
    print("not accepted: recording the name only (re-run with --orcid ... or --yes)")
    return None


def cmd_track_author(args) -> int:
    settings = _settings(args)
    openalex_id, orcid, institution = (args.openalex or "").strip(), (args.orcid or "").strip(), (args.institution or "").strip()
    if not openalex_id and not args.no_lookup:
        http = _http(settings)
        if settings.openalex_api_key:
            pick = _pick_openalex(settings, http, args.name, args.yes)
            if pick:
                openalex_id = pick["id"]
                institution = institution or pick["institution"]
                orcid = orcid or pick["orcid"]
        else:
            print("OPENALEX_API_KEY not set: skipping the OpenAlex author lookup")
        if not openalex_id and not orcid:
            pick = _pick_orcid(http, args.name, args.yes)
            if pick:
                orcid = pick["orcid"]
                institution = institution or (pick["institutions"][0] if pick["institutions"] else "")
    try:
        author, bak = T.add_author(settings, args.subgroup, args.name, openalex_id=openalex_id, orcid=orcid, institution=institution)
    except T.TrackError as exc:
        return _fail(exc)
    _done(f"added to subgroups.yaml [{args.subgroup}]: {json.dumps(author, ensure_ascii=False)}", bak)
    if not openalex_id:
        print("note: without an OpenAlex id the author is matched by display name only (see docs/track.md)")
    return 0


def cmd_track_topic(args) -> int:
    settings = _settings(args)
    if not (args.keyword or args.openalex_topic or args.name):
        return _fail(T.TrackError("pass --keyword and/or --openalex-topic (and --name to create a new subgroup)"))
    try:
        group, bak = T.edit_subgroup(settings, args.subgroup, add_keywords=args.keyword or [], add_topics=args.openalex_topic or [], name=args.name or "")
    except T.TrackError as exc:
        return _fail(exc)
    _done(f"subgroup {group.get('id')} ({group.get('name')}): topics {group.get('openalex_topics') or []}, keywords {group.get('keywords') or []}", bak)
    return 0


def cmd_track_term(args) -> int:
    settings = _settings(args)
    try:
        term, bak = T.add_term(settings, args.text)
    except T.TrackError as exc:
        return _fail(exc)
    _done(f"added watchlist term {term!r} to radar.yaml", bak)
    return 0


def cmd_track_list(args) -> int:
    settings = _settings(args)
    summary = T.tracked_summary(settings)
    kind = (args.kind or "").rstrip("s")
    if args.json:
        print(json.dumps(summary if not kind else summary.get(kind + ("ies" if kind == "compan" else "s"), summary), indent=1, ensure_ascii=False))
        return 0
    if kind in ("", "company"):
        print(f"companies ({len(summary['companies'])}):")
        for c in summary["companies"]:
            print(f"  {c['id']:28} {str(c['name'])[:36]:36} {str(c['ticker']):10} {str(c['type']):12} {'news' if c['watch_news'] else '':5} aliases={c['aliases']}")
    if kind in ("", "instrument"):
        print(f"instruments ({len(summary['instruments'])}):")
        for i in summary["instruments"]:
            print(f"  {i['id']:28} {str(i['name'])[:36]:36} {str(i['vendor'])[:28]:28} {str(i['category']):22} {i['status']}")
    if kind in ("", "author"):
        print(f"tracked authors ({len(summary['authors'])}):")
        for a in summary["authors"]:
            print(f"  {a['subgroup']:18} {str(a['name'])[:30]:30} {a['openalex_id'] or '-':12} {a['orcid'] or '-':20} {a['institution']}")
    if kind in ("", "topic", "subgroup"):
        print(f"subgroups ({len(summary['topics'])}):")
        for t in summary["topics"]:
            print(f"  {t['id']:18} {str(t['name'])[:36]:36} topics={len(t['openalex_topics'])} keywords={len(t['keywords'])} authors={t['tracked_authors']}")
    if kind in ("", "term"):
        print(f"watchlist terms ({len(summary['terms'])}): " + ", ".join(summary["terms"]))
    if kind in ("", "priority", "prioritie"):
        pri = summary["priorities"]
        print("priorities: " + (", ".join(f"{k}.{key}={w:g}" for k, table in pri.items() for key, w in table.items()) or "none (all 1.0)"))
    return 0


# ----------------------------------------------------------------------------- untrack
def cmd_untrack(args) -> int:
    settings = _settings(args)
    try:
        if args.kind in ("company", "instrument"):
            entry, bak = T.remove_gazetteer_entry(settings, args.kind, args.target)
            _done(f"removed {args.kind} {entry.get('id')} ({entry.get('name')})", bak)
        elif args.kind == "author":
            removed, bak = T.remove_author(settings, args.target, subgroup=args.subgroup or "")
            _done("removed tracked author: " + ", ".join(f"{n} [{g}]" for g, n in removed), bak)
        elif args.kind == "topic":
            if args.keyword or args.openalex_topic:
                group, bak = T.edit_subgroup(settings, args.target, remove_keywords=args.keyword or [], remove_topics=args.openalex_topic or [])
                _done(f"subgroup {group.get('id')}: topics {group.get('openalex_topics') or []}, keywords {group.get('keywords') or []}", bak)
            else:
                group, bak = T.remove_subgroup(settings, args.target)
                _done(f"removed subgroup {group.get('id')} ({group.get('name')}) with {len(group.get('tracked_authors') or [])} tracked authors", bak)
        elif args.kind == "term":
            term, bak = T.remove_term(settings, args.target)
            _done(f"removed watchlist term {term!r}", bak)
        else:
            raise T.TrackError(f"unknown kind {args.kind}")
    except T.TrackError as exc:
        return _fail(exc)
    if args.kind in ("company", "instrument"):
        print("note: stored mentions keep the old id until `litradar rematch` runs")
    return 0


# ----------------------------------------------------------------------------- registration
def register(sub) -> None:
    p = sub.add_parser("track", help="add a company, instrument, author, topic or watchlist term to the config from the command line")
    ts = p.add_subparsers(dest="track_kind", required=True)

    c = ts.add_parser("company", help="append an entry to companies.yaml (ticker looked up in the SEC list unless given)")
    c.add_argument("name")
    c.add_argument("--ticker", help="public ticker, or 'private'; default: fuzzy match against the SEC company list")
    c.add_argument("--type", default="tools", choices=list(T.COMPANY_TYPES))
    c.add_argument("--alias", action="append", help="other spelling (repeatable)")
    c.add_argument("--domain", action="append", help="author e-mail domain (repeatable)")
    c.add_argument("--parent", help="current owner for an acquired brand")
    c.add_argument("--watch-news", action="store_true", help="also pull a Google News query for the name")
    c.add_argument("--id", help="override the kebab-case id derived from the name")
    c.add_argument("--no-lookup", action="store_true", help="skip the SEC ticker lookup")
    c.set_defaults(func=cmd_track_company)

    i = ts.add_parser("instrument", help="append an entry to instruments.yaml (ticker inferred from the vendor's company entry)")
    i.add_argument("name")
    i.add_argument("--vendor", required=True, help="company name, id, alias or ticker from companies.yaml")
    i.add_argument("--category", required=True, help="e.g. sequencer.long_read, spatial, proteomics.ms (see the header of instruments.yaml)")
    i.add_argument("--alias", action="append")
    i.add_argument("--context", action="append", help="context term required within ~250 chars (sets context_required: true)")
    i.add_argument("--ticker", help="only needed when the vendor is not in companies.yaml")
    i.add_argument("--launched", type=int)
    i.add_argument("--id")
    i.set_defaults(func=cmd_track_instrument)

    a = ts.add_parser("author", help="add a tracked author to a subgroup in subgroups.yaml")
    a.add_argument("name")
    a.add_argument("--subgroup", required=True, help="subgroup id (see `litradar track list topic`)")
    a.add_argument("--openalex", help="OpenAlex author id (A...); looked up when omitted and OPENALEX_API_KEY is set")
    a.add_argument("--orcid", help="ORCID iD; looked up via the public ORCID API when no OpenAlex id is available")
    a.add_argument("--institution")
    a.add_argument("--yes", action="store_true", help="accept the top lookup match without asking")
    a.add_argument("--no-lookup", action="store_true", help="record the name only")
    a.set_defaults(func=cmd_track_author)

    t = ts.add_parser("topic", help="add keywords / OpenAlex topics to a subgroup, or create one with --name")
    t.add_argument("subgroup", help="subgroup id")
    t.add_argument("--keyword", action="append")
    t.add_argument("--openalex-topic", action="append", help="OpenAlex topic id (T...)")
    t.add_argument("--name", help="create the subgroup with this display name if it does not exist")
    t.set_defaults(func=cmd_track_topic)

    w = ts.add_parser("term", help="append a watchlist term to radar.yaml")
    w.add_argument("text")
    w.set_defaults(func=cmd_track_term)

    ls = ts.add_parser("list", help="print what is tracked")
    ls.add_argument("kind", nargs="?", choices=["company", "companies", "instrument", "instruments", "author", "authors", "topic", "topics", "term", "terms", "priority", "priorities"])
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_track_list)

    u = sub.add_parser("untrack", help="remove a company, instrument, author, topic/subgroup or watchlist term")
    u.add_argument("kind", choices=["company", "instrument", "author", "topic", "term"])
    u.add_argument("target", help="id or name (author: name or OpenAlex id; topic: subgroup id)")
    u.add_argument("--subgroup", help="author: only this subgroup (default: every subgroup)")
    u.add_argument("--keyword", action="append", help="topic: remove only this keyword")
    u.add_argument("--openalex-topic", action="append", help="topic: remove only this topic id")
    u.set_defaults(func=cmd_untrack)


__all__: List[str] = ["register"]
