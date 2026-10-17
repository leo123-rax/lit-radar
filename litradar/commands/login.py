"""`litradar login nature|science [--wait 300]` and `litradar login --status`: personal subscription sessions."""
from __future__ import annotations

import json
import sys

from .. import subscription
from ..config import Settings


def cmd_login(args) -> int:
    settings = Settings.from_env(config_dir=args.config_dir)
    if args.status or not args.publisher:
        st = subscription.status(settings)
        if args.json:
            print(json.dumps(st))
        else:
            for k, v in st.items():
                print(f"{k:8} {'signed in' if v else 'not signed in'}")
        return 0 if any(st.values()) else 1
    rep = subscription.login(settings, args.publisher, wait_s=args.wait)
    print(json.dumps(rep) if args.json else (f"{rep['publisher']}: {'signed in' if rep['logged_in'] else 'NOT signed in (timed out)'} after {rep['seconds']} s"))
    return 0 if rep["logged_in"] else 2


def register(sub) -> None:
    p = sub.add_parser("login", help="sign in to a personal publisher subscription in a real browser window (you type the password; the code never sees it)")
    p.add_argument("publisher", nargs="?", choices=sorted(subscription.PUBLISHERS), help="nature or science")
    p.add_argument("--status", action="store_true", help="report which publishers have a live session")
    p.add_argument("--wait", type=int, default=subscription.DEFAULT_WAIT_S, help="seconds to wait for you to finish signing in (default 300)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_login)
    sys.modules.setdefault("litradar.commands.login", sys.modules[__name__])
