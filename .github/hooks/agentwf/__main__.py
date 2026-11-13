"""Usage (from the repository root, with PYTHONPATH=.github/hooks):

  python3 -m agentwf hook pre|post|post-failure   Copilot CLI hook; JSON on stdin
  python3 -m agentwf ci --base <ref>              run the pull request gates
  python3 -m agentwf tier --base <ref>            path floor for the current diff
  python3 -m agentwf new <ticket-id> --tier T1    scaffold a ticket folder
  python3 -m agentwf status                       state of the active ticket
"""
import argparse
import os
import sys

from . import ci, config, hook, ticket as tk


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentwf", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    sub.required = True
    p = sub.add_parser("hook")
    p.add_argument("event", choices=["pre", "post", "post-failure"])
    for name in ("ci", "tier"):
        p = sub.add_parser(name)
        p.add_argument("--base")
        p.add_argument("--head", default="HEAD")
        p.add_argument("--breaking-api-change", action="store_true")
    p = sub.add_parser("new")
    p.add_argument("ticket")
    p.add_argument("--tier", choices=config.TIERS, required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.cmd == "hook":
        return hook.main(args.event)

    root = tk.repo_root(os.getcwd())
    if not root:
        raise SystemExit("not inside a git repository")
    if args.cmd in ("ci", "tier"):
        base = args.base or tk.default_base(root)
        if not base:
            raise SystemExit("no base ref found; pass --base")
        if args.cmd == "tier":
            settings = config.load_settings(root)
            floor, reasons = settings.tier_floor(ci._changes(root, base, args.head), args.breaking_api_change)
            print("path floor: %s" % floor)
            print("\n".join("  " + r for r in reasons))
            return 0
        report = ci.run(root, base, args.head, args.breaking_api_change)
        ci.publish(report)
        return 1 if report.failed else 0
    if args.cmd == "new":
        ci.new_ticket(root, args.ticket, args.tier)
        return 0
    return ci.status(root)


if __name__ == "__main__":
    sys.exit(main())
