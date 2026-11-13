"""Server-side gates, run on every pull request. Also usable locally."""
import os
import re
import subprocess

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
    """[(path, is_new_file)] for the final diff. Deleting a file counts as touching it."""
    out = tk.git(root, "diff", "--name-status", "--no-renames", "%s...%s" % (base, head))
    changes = []
    for line in out.splitlines():
        status, path = line.split("\t", 1)
        changes.append((path, status == "A"))
    return changes


def _numstat(text):
    stats = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            added, deleted, path = parts
            stats.append((path, 0 if added == "-" else int(added) + int(deleted)))
    return stats


def _commit_stats(root, sha):
    """[(path, changed_lines)] for one commit. For a merge commit: only what the
    merge itself changed beyond the automatic merge (conflict resolutions, extras)."""
    if len(tk.git(root, "rev-list", "--parents", "-n", "1", sha).split()) > 2:
        return _numstat(tk.git(root, "show", "--remerge-diff", "--numstat", "--no-renames", "--format=", sha))
    return _numstat(tk.git(root, "show", "--numstat", "--no-renames", "--format=", sha))


def check_slices(root, settings, plan, base, head):
    """Each commit that changes files carries a `Slice:` trailer naming a planned
    slice and stays inside that slice's paths; each slice's production change
    stays within the cap. Returns (size_errors, path_errors)."""
    size_errors, path_errors = [], []
    commits = []  # (sha, slice or None, production_lines, is_merge)
    for sha in tk.git(root, "rev-list", "--reverse", "%s..%s" % (base, head)).split():
        merge = len(tk.git(root, "rev-list", "--parents", "-n", "1", sha).split()) > 2
        stats = [(p, n) for p, n in _commit_stats(root, sha) if not settings.ticket_of(p)]
        production = sum(n for p, n in stats if settings.is_production(p))
        short = sha[:10]
        trailer = tk.git(root, "log", "-1", "--format=%(trailers:key=Slice,valueonly)", sha).strip().upper()
        commits.append((sha, trailer or None, production, merge))
        if not stats:
            continue
        if not trailer:
            if production:
                path_errors.append("%s changes production code without a `Slice: S-nn` trailer" % short)
                if production > settings.slice_cap:
                    size_errors.append("%s changes %d production lines; the cap is %d"
                                       % (short, production, settings.slice_cap))
            continue
        allowed = plan.slices.get(trailer)
        if allowed is None:
            path_errors.append("%s names slice %s, which the plan does not define" % (short, trailer))
            continue
        for path, _ in stats:
            if not config.matches(path, allowed):
                path_errors.append("%s (%s) touches %s, outside the slice's paths" % (short, trailer, path))

    for slice_id in sorted({c[1] for c in commits if c[1]}):
        mine = [i for i, c in enumerate(commits) if c[1] == slice_id and c[2]]
        if not mine:
            continue
        span = commits[mine[0]:mine[-1] + 1]
        if all(c[1] == slice_id or (not c[2] and not c[3]) for c in span):
            # contiguous: measure the slice's net change, so a fix round is not counted twice
            diff = tk.git(root, "diff", "--numstat", "--no-renames", span[0][0] + "^", span[-1][0])
            total = sum(n for p, n in _numstat(diff) if settings.is_production(p))
        else:
            total = sum(commits[i][2] for i in mine)
        if total > settings.slice_cap:
            size_errors.append("slice %s changes %d production lines; the cap is %d"
                               % (slice_id, total, settings.slice_cap))
    return size_errors, path_errors


def _own_changes(root, ticket, base, rev):
    """Patch id of what `rev` changes against `base`, outside the ticket folder.
    Line numbers do not count, so merging the base branch in keeps it stable."""
    diff = tk.git(root, "diff", "%s...%s" % (base, rev), "--", ".", ":(exclude)" + ticket.dir)
    proc = subprocess.run(["git", "-C", root, "patch-id", "--stable"], input=diff + "\n",
                          stdout=subprocess.PIPE, universal_newlines=True)
    return proc.stdout.split()[:1]


def check_diff_review(root, ticket, base, head):
    """review.md names the commit the reviewer read, and the code has not changed since."""
    sec = tk.sections(tk.strip_comments(ticket.read("review.md", head) or ""))
    errors = []
    reviewed = re.findall(r"(?im)^\s*Reviewed head:\s*`?([0-9a-f]{7,40})\b", sec.get("diff findings", ""))
    if not reviewed:
        errors.append("review.md has no diff review: the reviewer writes `Reviewed head: <commit>` "
                      "under each round in `## Diff findings`")
    else:
        sha = tk.git(root, "rev-parse", "--verify", "--quiet", reviewed[-1] + "^{commit}", check=False)
        if not sha or tk.git(root, "merge-base", sha, head, check=False) != sha:
            errors.append("reviewed head %s is not a commit on this branch" % reviewed[-1])
        elif _own_changes(root, ticket, base, sha) != _own_changes(root, ticket, base, head):
            errors.append("the code changed after the reviewed commit %s; run the reviewer again" % reviewed[-1])
    if not sec.get("review brief"):
        errors.append("review.md has no review brief for the human diff review")
    return errors


def base_settings(root, base, head):
    """Settings as they are on the base branch, so a pull request cannot relax
    the rules it is judged by. Falls back to the branch's copy on first install."""
    fork = tk.git(root, "merge-base", base, head, check=False) or base
    text = tk.git(root, "show", "%s:%s" % (fork, config.SETTINGS_PATH), check=False)
    return config.load_settings(root, text) if text else config.load_settings(root)


def run(root, base, head="HEAD", breaking_api_change=False, ready=False):
    settings = base_settings(root, base, head)
    report = Report()

    tickets = tk.changed_tickets(root, settings, base, head, added_only=True)
    if len(tickets) != 1:
        report.add("ticket", [
            "a pull request must add exactly one ticket folder under %s/ with its plan.md (found: %s)"
            % (settings.artifact_root, ", ".join(tickets) or "none")
        ])
        return report
    ticket = tk.Ticket(root, settings, tickets[0])
    plan = ticket.plan(head)
    log = ticket.log(head)
    overrides = log.overrides
    errors = ["plan.md is missing"] if plan is None else tk.check_front_matter(plan, ticket.id)
    report.add("ticket", errors, note="%s, declared %s" % (ticket.id, plan.tier if plan else "?"))
    if errors:
        return report

    changes = _changes(root, base, head)
    floor, reasons = settings.tier_floor(changes, breaking_api_change)
    errors = []
    if not config.tier_at_least(plan.tier, floor):
        errors = ["the diff needs at least %s but the ticket declares %s; re-triage it" % (floor, plan.tier)] + reasons
    declared = ticket.highest_declared_tier(ticket.plan_history(base, head))
    if declared and declared > plan.tier:
        errors.append("plan.md declared %s earlier on this branch and now says %s; only a human may lower a tier"
                      % (declared, plan.tier))
    report.add("tier-floor", errors, overrides, note="path floor is %s" % floor)

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

    if plan.tier == "T0":
        report.skip("review", "T0 is covered by normal pull request review")
    elif all(settings.ticket_of(path) for path, _ in changes):
        report.skip("review", "nothing changed outside the ticket folder yet")
    elif not ready and log.state not in ("IN_REVIEW", "APPROVED", "DONE"):
        report.skip("review", "checked once the state is IN_REVIEW or the pull request is ready for review")
    else:
        report.add("review", check_diff_review(root, ticket, base, head), overrides)
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

