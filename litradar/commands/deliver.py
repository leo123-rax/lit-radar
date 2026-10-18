"""`litradar deliver`: push the newest digest through the channels that need no mail server or webhook.

  litradar deliver --issue [--date YYYY-MM-DD] [--dry-run] [--repo owner/name] [--branch main] [--pages-url URL]
                                                  create or update the GitHub Issue "Weekly digest <date>" via `gh`
  litradar deliver --feed site/feed.xml [--entries 12]
                                                  Atom feed of the newest digests, for the Pages site
  litradar deliver --status [--json]              which channels (e-mail, Slack, GitHub Issue, feed) are configured

The flags combine (`--status --feed site/feed.xml --issue`). Exit code 0 on success or a configured
skip, 1 when a requested delivery failed, 2 on a usage error. Under GitHub Actions a failure is also
written as a `::warning::` annotation, so a workflow step can `continue-on-error` and a delivery
hiccup never undoes the digest commit. The digest itself is never modified. See docs/delivery.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from ..config import Settings
from ..digest import deliver

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_date(text: str) -> str:
    if not _DATE_RE.match(text or ""):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a real calendar date") from None


def _repo(text: str) -> str:
    if not deliver._REPO_RE.match(text or ""):
        raise argparse.ArgumentTypeError(f"expected owner/name, got {text!r}")
    return text


def _warn(message: str) -> None:
    """Plain line locally; an annotation the run page shows when running under GitHub Actions."""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "deliver: warning: "
    print(f"{prefix}{message}")


def _print_issue_result(res: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        out = {k: v for k, v in res.items() if k != "body"}
        print(json.dumps(out, indent=1, default=str))
        return
    if res.get("action") == "skipped":
        print(f"GitHub Issue skipped: {res.get('reason')}")
        return
    if not res.get("ok"):
        return
    size = f"{res['body_chars']:,} chars" + (f" (truncated from {res['digest_chars']:,})" if res.get("truncated") else "")
    if res.get("dry_run"):
        target = f" #{res['number']}" if res.get("number") else ""
        print(f"dry run: {res['action']} issue{target} {res['title']!r} in {res['repo']}")
        print(f"  label: {res.get('label')}  body: {size}")
        print(f"  links: {res['file_url']} · {res['pages_url']}")
        if res.get("would_close"):
            print("  would close: " + ", ".join(f"#{n}" for n in res["would_close"]))
        if res.get("lookup_error"):
            print(f"  note: could not list existing issues ({res['lookup_error']}); assuming none")
        preview = (res.get("body") or "").splitlines()[:12]
        print("  --- body, first lines ---")
        for line in preview:
            print("  | " + line[:160])
        return
    where = f" #{res['number']}" if res.get("number") else ""
    print(f"issue {res['action']}{where}: {res['title']} ({size}) {res.get('url') or ''}".rstrip())
    if res.get("closed"):
        print("closed previous: " + ", ".join(f"#{n}" for n in res["closed"]))
    for ce in res.get("close_errors") or []:
        _warn(f"could not close #{ce['number']}: {ce['error']}")
    if res.get("label") is None:
        _warn("the label was not applied (see the log); the issue itself is fine")


def cmd_deliver(args) -> int:
    if not (args.issue or args.feed or args.status):
        print("litradar deliver: error: nothing to do; pass --issue, --feed PATH and/or --status", file=sys.stderr)
        return 2
    settings = Settings.from_env(config_dir=args.config_dir)
    failed = False
    if args.status:
        report = deliver.channel_status(settings)
        print(json.dumps(report, indent=1, default=str) if args.json else deliver.format_channel_status(report))
    if args.feed:
        res = deliver.write_feed(settings, Path(args.feed), repo=args.repo, branch=args.branch, pages_url=args.pages_url,
                                 entries=args.entries)
        if args.json:
            print(json.dumps(res, indent=1, default=str))
        elif res["ok"]:
            print(f"feed written to {res['path']} ({res['entries']} entries, {res['bytes']:,} bytes); published URL: {res['url']}")
        if not res["ok"]:
            _warn(f"feed not written: {res['error']}")
            failed = True
    if args.issue:
        res = deliver.deliver_github_issue(settings, date=args.date, repo=args.repo, branch=args.branch,
                                           pages_url=args.pages_url, dry_run=args.dry_run)
        _print_issue_result(res, args.json)
        if not res["ok"]:
            _warn(f"GitHub Issue delivery failed: {res['error']}")
            failed = True
    return 1 if failed else 0


def register(sub) -> None:
    p = sub.add_parser(
        "deliver",
        help="deliver the newest digest as a GitHub Issue and/or an Atom feed; --status lists configured channels",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--issue", action="store_true", help="create or update the issue 'Weekly digest <date>' with the gh CLI (GH_TOKEN)")
    p.add_argument("--feed", metavar="PATH", help="write an Atom feed of the newest digests to PATH (e.g. site/feed.xml)")
    p.add_argument("--status", action="store_true", help="print which delivery channels are configured (presence only)")
    p.add_argument("--date", type=_iso_date, help="deliver this digest (digests/YYYY-MM-DD.md) instead of the newest one")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="with --issue: look up, build and print; create nothing")
    p.add_argument("--repo", type=_repo, help="owner/name (default: GH_REPO, GITHUB_REPOSITORY, else `gh repo view`)")
    p.add_argument("--branch", help="branch the digest links point at (default: GITHUB_REF_NAME, else main)")
    p.add_argument("--pages-url", dest="pages_url", help="dashboard URL (default: LITRADAR_PAGES_URL, radar.yaml digest.delivery.pages_url, else https://<owner>.github.io/<name>/)")
    p.add_argument("--entries", type=int, help="with --feed: number of digests in the feed (default: digest.delivery.feed_entries, else 12)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_deliver)
