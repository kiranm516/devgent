"""devgent: the agent workflow's command line. Run it as ./devgent from the repository root.

Every day
  status                         where the ticket stands and what to do next
  start <ticket-id> --tier T1    create the ticket's worktree, branch and folder (--here: this checkout)
  tier                           tier floor the path rules set for this branch
  slice start|review|accept S-01 move a slice forward (one at a time)
  state <STATE> [reason]         set the ticket's state
  check                          run the pull request gates locally
  view [--open]                  one generated page: what needs you, decisions, trace, slices
  view --functional [--open]     the functional scenarios alone, to show a BA, PO or QE
  answer D-01 A [note]           write the approver's answer to a decision into plan.md
  override <gate> <reason>       record a human override of one gate

Setup
  doctor                         find problems with this repository's setup
  update                         update the kit in this repository
  install <target-repo>          install the kit into another repository
  version                        installed kit version
"""
import argparse
import os
import subprocess
import sys

from . import ci, config, doctor, flow, hook, install, ticket as tk, view


def _parser():
    parser = argparse.ArgumentParser(prog="devgent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", metavar="command")
    sub.required = True
    sub.add_parser("status")
    p = sub.add_parser("start", aliases=["new"])
    p.add_argument("ticket")
    p.add_argument("--tier", choices=config.TIERS, required=True)
    p.add_argument("--here", action="store_true",
                   help="switch this checkout to the new branch instead of creating a worktree")
    p.add_argument("--path", help="folder for the ticket's worktree (default: next to the main checkout, "
                                  "see `worktrees.dir` in .github/devgent.yml)")
    for name in ("check", "ci", "tier"):
        p = sub.add_parser(name)
        p.add_argument("--base", help="ref to compare with (default: the default branch)")
        p.add_argument("--head", default="HEAD")
        p.add_argument("--breaking-api-change", action="store_true")
        p.add_argument("--ready", action="store_true",
                       help="the pull request is not a draft: require the diff review whatever the state")
    p = sub.add_parser("slice")
    p.add_argument("action", choices=["start", "review", "accept"])
    p.add_argument("slice")
    p = sub.add_parser("state")
    p.add_argument("state")
    p.add_argument("reason", nargs="*")
    p = sub.add_parser("view")
    p.add_argument("--open", action="store_true", help="open the page in the default browser")
    p.add_argument("--functional", action="store_true",
                   help="write the functional layer alone: requirements and scenarios, no files or slices")
    p = sub.add_parser("answer")
    p.add_argument("decision")
    p.add_argument("answer", nargs="+")
    p = sub.add_parser("override")
    p.add_argument("gate")
    p.add_argument("reason", nargs="+")
    p = sub.add_parser("doctor")
    p.add_argument("--offline", action="store_true", help="skip checks that need the network")
    p = sub.add_parser("update")
    p.add_argument("--source", help="kit location: a git URL or a local clone (default: where it was installed from)")
    p.add_argument("--force", action="store_true", help="overwrite shared files that were edited locally")
    p = sub.add_parser("install")
    p.add_argument("target")
    p.add_argument("--force", action="store_true", help="replace files in the target that are not the kit's")
    sub.add_parser("version")
    p = sub.add_parser("hook")
    p.add_argument("event", choices=["pre", "post", "post-failure"])
    return parser


def _finish_install(summary, kit):
    print(install.describe(summary, kit))
    report = doctor.Doctor(summary["target"]).run()
    print("\nSetup check\n")
    print(report.text())
    todo = report.todo()
    if todo:
        print("\nWhat is left to do\n")
        print(todo)
    print("\nThen: review `git status`, commit, and open a pull request. "
          "Run `./devgent doctor` any time to re-check.")


def _update(root, args):
    manifest = install.read_manifest(root)
    source = args.source or (manifest or {}).get("source")
    if not source:
        raise SystemExit("no install manifest here; pass --source <kit URL or path>")
    edited = [e for e in install.modified_shared_files(root) if e[1] == "modified"]
    if edited and not args.force:
        print("These shared files were changed in this repository and would be overwritten:")
        for rel, what in edited:
            print("  %s (%s)" % (rel, what))
        raise SystemExit("Move the change into the kit, or rerun with --force to discard it.")
    kit, cleanup = install.fetch_kit(source)
    try:
        # Run the fetched kit's own installer, so new files and rules apply.
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.path.join(kit, ".github", "hooks"))
        command = [sys.executable, "-m", "devgent", "install", root] + (["--force"] if args.force else [])
        code = subprocess.run(command, env=env).returncode
    finally:
        cleanup()
    return 1 if code else 0


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.cmd == "hook":
        return hook.main(args.event)
    if args.cmd == "install":
        kit = install.kit_root()
        _finish_install(install.install(args.target, kit, force=args.force), kit)
        return 0

    root = tk.repo_root(os.getcwd())
    if not root:
        raise SystemExit("not inside a git repository")
    if args.cmd == "version":
        manifest = install.read_manifest(root)
        print("devgent %s (from %s)" % (manifest["version"], manifest["source"]) if manifest
              else "kit source repository (not an installed copy)")
        return 0
    if args.cmd == "doctor":
        report = doctor.Doctor(root, offline=args.offline).run()
        print(report.text())
        return 1 if report.count(doctor.FAIL) else 0
    if args.cmd == "update":
        return _update(root, args)
    if args.cmd in ("check", "ci", "tier"):
        base = args.base or tk.default_base(root)
        if not base:
            raise SystemExit("no base ref found; pass --base")
        if args.cmd == "tier":
            settings = config.load_settings(root)
            floor, reasons = settings.tier_floor(ci._changes(root, base, args.head), args.breaking_api_change)
            print("path floor: %s" % floor)
            print("\n".join("  " + r for r in reasons) or "  no path rule matches the files changed so far")
            return 0
        report = ci.run(root, base, args.head, args.breaking_api_change, args.ready)
        ci.publish(report)
        return 1 if report.failed else 0
    if args.cmd in ("start", "new"):
        flow.start(root, args.ticket, args.tier, here=args.here, path=args.path)
    elif args.cmd == "slice":
        flow.slice_step(root, args.action, args.slice)
    elif args.cmd == "state":
        flow.set_state(root, args.state, " ".join(args.reason))
    elif args.cmd == "answer":
        flow.answer(root, args.decision, " ".join(args.answer))
    elif args.cmd == "view":
        return view.run(root, args.open, args.functional)
    elif args.cmd == "override":
        flow.override(root, args.gate, " ".join(args.reason))
    else:
        return flow.status(root)
    return 0


def run():
    try:
        return main()
    except (flow.FlowError, install.InstallError, config.ConfigError, tk.GitError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
