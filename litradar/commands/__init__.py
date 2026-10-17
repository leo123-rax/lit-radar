"""Plugin commands. Each module defines register(subparsers) and adds one or more subcommands.

Example:

    def register(sub):
        p = sub.add_parser("hello", help="say hello")
        p.set_defaults(func=lambda args: print("hello") or 0)
"""
