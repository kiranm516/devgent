"""Server-side gates, run on every pull request. Also usable locally."""
import os
import re

from . import config, ticket as tk

PASS, FAIL, OVERRIDDEN, SKIP = "pass", "FAIL", "overridden", "skipped"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, gate, errors, overrides=None, note=""):
        if errors and overrides and gate in overrides:
            self.rows.append((gate, OVERRIDDEN, ["override by " + overrides[gate]] + errors))
        elif errors:
            self.rows.append((gate, FAIL, errors))
        else:
            self.rows.append((gate, PASS, [note] if note else []))

    def skip(self, gate, why):
        self.rows.append((gate, SKIP, [why]))

    @property
    def failed(self):
        return any(status == FAIL for _, status, _ in self.rows)

    def text(self):
        out = []
        for gate, status, details in self.rows:
            out.append("%-14s %s" % (gate, status))
            out += ["    - " + d for d in details]
        return "\n".join(out)

    def markdown(self, title):
        out = ["### %s" % title, "", "| Gate | Result | Details |", "| --- | --- | --- |"]
        for gate, status, details in self.rows:
            out.append("| %s | %s | %s |" % (gate, status, "<br>".join(d.replace("|", "\\|") for d in details)))
        return "\n".join(out) + "\n"


def _changes(root, base, head):
    """[(path, is_new_file)] for the final diff, ignoring deleted files."""
    out = tk.git(root, "diff", "--name-status", "--no-renames", "%s...%s" % (base, head))
    changes = []
    for line in out.splitlines():
        status, path = line.split("\t", 1)
        if status != "D":
            changes.append((path, status == "A"))
    return changes


def _commit_stats(root, sha):
    """[(path, changed_lines)] for one commit."""
    out = tk.git(root, "show", "--numstat", "--no-renames", "--format=", sha)
    stats = []
    for line in out.splitlines():
        added, deleted, path = line.split("\t", 2)
        lines = 0 if added == "-" else int(added) + int(deleted)
        stats.append((path, lines))
    return stats


def check_slices(root, settings, plan, base, head):
    """Per slice commit: size cap, a `Slice:` trailer naming a planned slice,
    and files inside that slice's paths. Returns (size_errors, path_errors)."""
    size_errors, path_errors = [], []
    for sha in tk.git(root, "rev-list", "--no-merges", "--reverse", "%s..%s" % (base, head)).split():
        stats = [(p, n) for p, n in _commit_stats(root, sha) if not settings.ticket_of(p)]
        production = sum(n for p, n in stats if settings.is_production(p))
        short = sha[:10]
        if production > settings.slice_cap:
            size_errors.append("%s changes %d production lines; the cap is %d" % (short, production, settings.slice_cap))
        if not stats:
            continue
        trailer = tk.git(root, "log", "-1", "--format=%(trailers:key=Slice,valueonly)", sha).strip().upper()
        if not trailer:
            if production:
                path_errors.append("%s changes production code without a `Slice: S-nn` trailer" % short)
            continue
        allowed = plan.slices.get(trailer)
        if allowed is None:
            path_errors.append("%s names slice %s, which the plan does not define" % (short, trailer))
            continue
        for path, _ in stats:
            if not config.matches(path, allowed):
                path_errors.append("%s (%s) touches %s, outside the slice's paths" % (short, trailer, path))
    return size_errors, path_errors


def run(root, base, head="HEAD", breaking_api_change=False):
    settings = config.load_settings(root)
    report = Report()

    tickets = tk.changed_tickets(root, settings, base, head)
    if len(tickets) != 1:
        report.add("ticket", [
            "a pull request must carry exactly one ticket folder under %s/ (found: %s)"
            % (settings.artifact_root, ", ".join(tickets) or "none")
        ])
        return report
    ticket = tk.Ticket(root, settings, tickets[0])
    plan = ticket.plan(head)
    overrides = ticket.log(head).overrides
    errors = ["plan.md is missing"] if plan is None else tk.check_front_matter(plan, ticket.id)
    report.add("ticket", errors, note="%s, declared %s" % (ticket.id, plan.tier if plan else "?"))
    if errors:
        return report

    floor, reasons = settings.tier_floor(_changes(root, base, head), breaking_api_change)
    report.add(
        "tier-floor",
        [] if config.tier_at_least(plan.tier, floor) else
        ["the diff needs at least %s but the ticket declares %s; re-triage it" % (floor, plan.tier)] + reasons,
        overrides, note="path floor is %s" % floor,
    )

    if plan.tier == "T0":
        report.skip("slice-size", "T0 has no slices")
        report.skip("slice-paths", "T0 has no slices")
    else:
        size_errors, path_errors = check_slices(root, settings, plan, base, head)
        report.add("slice-size", size_errors, overrides)
        report.add("slice-paths", path_errors, overrides)

    errors = tk.check_plan_structure(plan)
    if plan.tier != "T0":
        errors += tk.check_review_structure(ticket.read("review.md", head), settings)
    report.add("structure", errors, overrides)
    return report


def publish(report, title="Agent workflow gates"):
    print(report.text())
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report.markdown(title))


def new_ticket(root, ticket_id, tier):
    settings = config.load_settings(root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", ticket_id):
        raise SystemExit("invalid ticket id: %s" % ticket_id)
    templates = os.path.join(root, settings.artifact_root, "_templates")
    target = os.path.join(root, settings.artifact_root, ticket_id)
    if os.path.exists(target):
        raise SystemExit("%s already exists" % target)
    os.makedirs(target)
    names = {"plan.md": "task-note.md" if tier == "T0" else "plan.md", "log.md": "log.md"}
    if tier != "T0":
        names["review.md"] = "review.md"
    base_commit = tk.git(root, "rev-parse", "HEAD", check=False) or "none"
    for name, template in names.items():
        with open(os.path.join(templates, template), encoding="utf-8") as fh:
            text = fh.read()
        for key, value in (("TICKET", ticket_id), ("TIER", tier), ("BASE_COMMIT", base_commit)):
            text = text.replace("{{%s}}" % key, value)
        with open(os.path.join(target, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    print("created %s/%s (%s)" % (settings.artifact_root, ticket_id, ", ".join(sorted(names))))


def status(root):
    """One screen a new session can resume from."""
    settings = config.load_settings(root)
    ticket = tk.resolve_ticket(root, settings)
    if ticket is None:
        print("no active ticket (branch name should contain the ticket id, or set AGENT_TICKET)")
        return 1
    plan, log = ticket.plan(), ticket.log()
    print("ticket        %s" % ticket.id)
    print("state         %s" % (log.state or "unknown (first line of log.md must be `state: ...`)"))
    print("tier          %s   plan version %s" % (plan and plan.tier, plan and plan.version))
    if plan and plan.tier in ("T1", "T2"):
        print("plan approval %s" % tk.plan_approval(root, ticket)[1])
        for slice_id in sorted(plan.slices):
            print("slice %-7s %s" % (slice_id, log.slice_status.get(slice_id, "not started")))
    for gate, text in sorted(log.overrides.items()):
        print("override      %s by %s" % (gate, text))
    return 0
