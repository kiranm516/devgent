"""Ticket artifacts (plan.md, log.md, review.md), git helpers and plan approval."""
import json
import os
import re
import subprocess
import time

STATES = (
    "TRIAGED", "PLANNING", "PLAN_APPROVED", "IMPLEMENTING", "IN_REVIEW",
    "APPROVED", "DONE", "BLOCKED", "ABANDONED",
)
GATES = ("ticket", "tier-floor", "plan-approval", "slice-paths", "slice-size", "structure")


class GitError(Exception):
    pass


def git(root, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", root, "-c", "core.quotePath=false"] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    if check and proc.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_root(cwd):
    try:
        return git(cwd or ".", "rev-parse", "--show-toplevel") or None
    except (GitError, OSError):
        return None


def state_dir(root):
    """Local, never-committed directory for the approval cache and audit log."""
    common = git(root, "rev-parse", "--git-common-dir")
    path = os.path.join(common if os.path.isabs(common) else os.path.join(root, common), "agent-workflow")
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# Markdown helpers
# --------------------------------------------------------------------------


def strip_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def sections(text, level=2):
    """Map of lower-cased heading -> body for headings of exactly `level`."""
    out, current, in_fence = {}, None, False
    marker = "#" * level + " "
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith(marker):
            current = line[len(marker):].strip().lower()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def table_rows(text):
    """Rows of the first markdown table in `text`, as dicts keyed by header."""
    header, rows = None, []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if header is None:
            header = [c.lower() for c in cells]
        elif all(re.fullmatch(r":?-+:?", c) for c in cells if c) and not rows:
            continue
        else:
            cells += [""] * (len(header) - len(cells))
            rows.append(dict(zip(header, cells)))
    return rows


def _blank(cell):
    return cell.strip().strip("_*`").strip().lower() in ("", "tbd", "todo", "?", "-", "n/a", "pending")


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


class Plan:
    def __init__(self, text):
        self.meta = {}
        body = text
        m = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
        if m:
            body = text[m.end():]
            for line in m.group(1).splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    self.meta[key.strip()] = value.strip().strip("\"'")
        self.body = strip_comments(body)
        self.sections = sections(self.body)
        self.tier = self.meta.get("tier")
        self.version = self.meta.get("plan_version")
        self.slices = self._slices()

    def _slices(self):
        out = {}
        for title, body in sections(self.sections.get("slices", ""), level=3).items():
            m = re.match(r"(s-\d+)\b", title)
            if not m:
                continue
            paths, indent = [], None
            for line in body.splitlines():
                bullet = re.match(r"(\s*)[-*]\s+(.*)", line)
                if bullet:
                    lead = len(bullet.group(1))
                    if re.match(r"\**paths\**\s*:", bullet.group(2), flags=re.I):
                        indent = lead
                    elif indent is not None and lead <= indent:
                        indent = None
                if indent is not None:
                    paths += re.findall(r"`([^`]+)`", line)
            out[m.group(1).upper()] = paths
        return out


class Log:
    ENTRY = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (\w[\w-]*) (.*)$")

    def __init__(self, text):
        lines = strip_comments(text).splitlines()
        m = re.match(r"state:\s*(\w+)\s*$", lines[0]) if lines else None
        self.state = m.group(1) if m else None
        self.slice_status = {}
        self.overrides = {}
        for line in lines[1:]:
            m = self.ENTRY.match(line)
            if not m:
                continue
            kind, rest = m.group(2), m.group(3)
            if kind == "slice":
                sm = re.match(r"(S-\d+) (started|awaiting-review|accepted)\b", rest)
                if sm:
                    self.slice_status[sm.group(1)] = sm.group(2)
            elif kind == "override":
                om = re.match(r"([a-z-]+) by ([^:]+): (\S.*)$", rest)
                if om:
                    self.overrides[om.group(1)] = "%s: %s" % (om.group(2).strip(), om.group(3))

    def active_slices(self):
        return [s for s, st in self.slice_status.items() if st == "started"]

    def awaiting_review(self):
        return [s for s, st in self.slice_status.items() if st == "awaiting-review"]




class Ticket:
    def __init__(self, root, settings, ticket_id):
        self.root, self.id = root, ticket_id
        self.dir = "%s/%s" % (settings.artifact_root, ticket_id)

    def rel(self, name):
        return "%s/%s" % (self.dir, name)

    def read(self, name, rev=None):
        if rev:
            text = git(self.root, "show", "%s:%s" % (rev, self.rel(name)), check=False) or None
        else:
            try:
                with open(os.path.join(self.root, self.rel(name)), encoding="utf-8", newline="") as fh:
                    text = fh.read()
            except OSError:
                return None
        return text.replace("\r\n", "\n") if text else text

    def plan(self, rev=None):
        text = self.read("plan.md", rev)
        return Plan(text) if text is not None else None

    def log(self, rev=None):
        return Log(self.read("log.md", rev) or "")

    def append_log(self, entry):
        path = os.path.join(self.root, self.rel("log.md"))
        existing = self.read("log.md") or ""
        with open(path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")  # never glue an entry onto an unterminated line
            fh.write("- %s %s\n" % (time.strftime("%Y-%m-%d"), entry))

    def highest_declared_tier(self, revs):
        """Highest tier plan.md declared in any of `revs` (commits)."""
        best = None
        for rev in revs:
            plan = self.plan(rev)
            if plan and plan.tier in ("T0", "T1", "T2") and (best is None or plan.tier > best):
                best = plan.tier
        return best

    def plan_history(self, base=None, head="HEAD"):
        """Commits that changed plan.md, on this branch only when `base` is given."""
        span = "%s..%s" % (base, head) if base else head
        return git(self.root, "log", "--format=%H", span, "--", self.rel("plan.md"), check=False).split()


def list_tickets(root, settings):
    base = os.path.join(root, settings.artifact_root)
    try:
        return sorted(
            d for d in os.listdir(base)
            if not d.startswith(("_", ".")) and os.path.isdir(os.path.join(base, d))
        )
    except OSError:
        return []


def default_base(root):
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        if git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False):
            return ref
    return None


def changed_tickets(root, settings, base, head="HEAD", added_only=False):
    """Tickets whose folder the branch touches; with `added_only`, only those
    whose plan.md the branch adds, so an old ticket's folder cannot be reused."""
    args = ["diff", "--name-only", "--no-renames", "%s...%s" % (base, head)]
    if added_only:
        args.insert(2, "--diff-filter=A")
    names = git(root, *args, check=False).splitlines()
    if added_only:
        names = [n for n in names if n.endswith("/plan.md")]
    return sorted({t for t in (settings.ticket_of(n) for n in names) if t})


def _names(ticket_id, branch):
    """True if `branch` contains `ticket_id` as a whole token (ABC-12 is not in ABC-123)."""
    return re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(ticket_id), branch, flags=re.I) is not None


def resolve_ticket(root, settings):
    """Active ticket for a local session: env, then branch name, then branch diff."""
    tickets = list_tickets(root, settings)
    env = os.environ.get("AGENT_TICKET")
    if env:
        return Ticket(root, settings, env) if env in tickets else None
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    named = [t for t in tickets if _names(t, branch)
             and Ticket(root, settings, t).log().state not in ("DONE", "ABANDONED")]
    if named:
        return Ticket(root, settings, max(named, key=len))
    base = default_base(root)
    if base and branch not in ("main", "master", "HEAD"):
        changed = changed_tickets(root, settings, base, added_only=True)
        if len(changed) == 1 and changed[0] in tickets:
            return Ticket(root, settings, changed[0])
    return None


# --------------------------------------------------------------------------
# Plan approval: a human GitHub approving review on the current plan.md
# --------------------------------------------------------------------------


def _gh(root, *args):
    try:
        proc = subprocess.run(
            ["gh"] + list(args), cwd=root, timeout=8,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "gh unavailable (%s)" % type(exc).__name__
    if proc.returncode != 0:
        return None, (proc.stderr.strip().splitlines() or ["gh failed"])[-1]
    return proc.stdout, None


def approving_reviews(root):
    """Commit ids of standing approvals on the current branch's PR, or (None, error)."""
    number, err = _gh(root, "pr", "view", "--json", "number", "-q", ".number")
    if err:
        return None, "no pull request found for this branch (%s)" % err
    out, err = _gh(
        root, "api", "--paginate",
        "repos/{owner}/{repo}/pulls/%s/reviews" % number.strip(),
        "-q", ".[] | {state, commit_id, user: .user.login}",
    )
    if err:
        return None, "could not list reviews (%s)" % err
    latest = {}
    for line in out.splitlines():
        if line.strip():
            review = json.loads(line)
            if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[review["user"]] = review
    return [r["commit_id"] for r in latest.values() if r["state"] == "APPROVED"], None


def plan_approval(root, ticket, fetch=approving_reviews, cache_ttl=600):
    """(approved, reason). Approved means a standing approving review exists on a
    commit whose plan.md is byte-identical to the committed, unmodified plan.md.
    Any later change to plan.md therefore cancels the approval."""
    plan_rel = ticket.rel("plan.md")
    if git(root, "status", "--porcelain", "--", plan_rel, check=False):
        return False, "%s has uncommitted changes; commit it and get it approved" % plan_rel
    blob = git(root, "rev-parse", "--verify", "--quiet", "HEAD:" + plan_rel, check=False)
    if not blob:
        return False, "%s is not committed" % plan_rel

    cache_file = os.path.join(state_dir(root), "approval.json")
    try:
        with open(cache_file) as fh:
            cache = json.load(fh)
        if cache.get("blob") == blob and time.time() - cache.get("ts", 0) < cache_ttl:
            return True, "approved (cached)"
    except (OSError, ValueError):
        pass

    commits, err = fetch(root)
    if err:
        return False, "cannot verify plan approval: " + err
    for commit in commits:
        reviewed = git(root, "rev-parse", "--verify", "--quiet", "%s:%s" % (commit, plan_rel), check=False)
        if not reviewed:
            out, _ = _gh(root, "api", "repos/{owner}/{repo}/contents/%s?ref=%s" % (plan_rel, commit), "-q", ".sha")
            reviewed = (out or "").strip()
        if reviewed == blob:
            with open(cache_file, "w") as fh:
                json.dump({"blob": blob, "ts": time.time()}, fh)
            return True, "approved at %s" % commit[:10]
    return False, (
        "no approving review covers the current %s. Open a draft PR and ask the "
        "approver to answer the decision list and submit an approving review" % plan_rel
    )


# --------------------------------------------------------------------------
# Structure checks ("ready for approval")
# --------------------------------------------------------------------------

CORE_SECTIONS = (
    "problem and outcome", "requirements", "scenarios and api contract",
    "design", "test matrix", "slices", "decision list",
)
TASK_NOTE_SECTIONS = ("goal", "files", "checks")
DISPOSITIONS = ("accepted", "rejected", "deferred")


def check_front_matter(plan, ticket_id):
    errors = []
    for key in ("ticket", "tier", "plan_version", "base_commit"):
        if not plan.meta.get(key):
            errors.append("plan.md front matter is missing `%s`" % key)
    if plan.meta.get("ticket") and plan.meta["ticket"] != ticket_id:
        errors.append("plan.md `ticket` (%s) does not match its folder (%s)" % (plan.meta["ticket"], ticket_id))
    if plan.tier and plan.tier not in ("T0", "T1", "T2"):
        errors.append("plan.md `tier` must be T0, T1 or T2")
    return errors


def check_plan_structure(plan):
    errors = []
    sec = plan.sections
    if plan.tier == "T0":
        return ["task note is missing a `## %s` section" % s.capitalize()
                for s in TASK_NOTE_SECTIONS if not sec.get(s)]

    for name in CORE_SECTIONS:
        if not sec.get(name):
            errors.append("plan.md is missing a non-empty `## %s` section" % name.capitalize())
    if errors:
        return errors

    reqs = sorted(set(re.findall(r"^\s*[-*]\s+\**(R-\d+)\b", sec["requirements"], flags=re.M)))
    if not reqs:
        errors.append("Requirements: no numbered criteria (`- R-01: ...`)")
    scenarios = {}
    for row in table_rows(sec["scenarios and api contract"]):
        m = re.search(r"SC-\d+", row.get("id", ""))
        if m:
            scenarios[m.group(0)] = set(re.findall(r"R-\d+", " ".join(row.values())))
    if not scenarios:
        errors.append("Scenarios: no scenario table rows with an `SC-nn` id")
    tested = set()
    for row in table_rows(sec["test matrix"]):
        ids = set(re.findall(r"SC-\d+", row.get("scenario", "")))
        if ids and not _blank(row.get("level", "")) and not _blank(row.get("test", "")):
            tested |= ids
    for sc in sorted(set(scenarios) - tested):
        errors.append("Test matrix: scenario %s has no test level and named test" % sc)
    for req in reqs:
        covering = {sc for sc, covered in scenarios.items() if req in covered}
        if not covering:
            errors.append("Requirement %s maps to no scenario" % req)
        elif not covering & tested:
            errors.append("Requirement %s maps to no test" % req)

    change_map = sections(sec["design"], level=3).get("change map", "")
    if not table_rows(change_map) and not re.search(r"^\s*[-*]\s+\S", change_map, flags=re.M):
        errors.append("Design: `### Change map` is missing or empty")

    if not plan.slices:
        errors.append("Slices: no slice defined (`### S-01: goal`)")
    for slice_id, paths in sorted(plan.slices.items()):
        if not paths:
            errors.append("Slice %s lists no allowed paths (`- Paths: `...``)" % slice_id)

    for row in table_rows(sec["decision list"]):
        m = re.search(r"D-\d+", row.get("id", ""))
        if m and _blank(row.get("approver's answer", row.get("approver answer", ""))):
            errors.append("Decision %s has no approver's answer" % m.group(0))

    for row in table_rows(sec.get("open questions", "")):
        blocking = row.get("blocking", "").strip().lower().startswith("y")
        status = row.get("status", "").strip().lower()
        if blocking and status not in ("answered", "closed", "risk-accepted"):
            errors.append("Open question %s is blocking and unresolved" % (row.get("id") or "?"))

    if plan.tier == "T2" and not sec.get("alternatives"):
        errors.append("T2 plan needs a non-empty `## Alternatives` section")
    return errors


def check_review_structure(text, settings):
    if text is None:
        return ["review.md is missing: the reviewer must challenge the plan before approval"]
    sec = sections(strip_comments(text))
    if "plan objections" not in sec:
        return ["review.md is missing a `## Plan objections` section"]
    errors = []
    rows = [r for r in table_rows(sec["plan objections"]) if re.search(r"O-\d+", r.get("id", ""))]
    if not rows and not re.search(r"\bno objections\b", sec["plan objections"], flags=re.I):
        errors.append("review.md has no plan challenge yet: the reviewer records objections "
                      "or writes \"No objections.\"")
    if len(rows) > settings.max_objections:
        errors.append("review.md has %d plan objections; the limit is %d" % (len(rows), settings.max_objections))
    for row in rows:
        oid = re.search(r"O-\d+", row["id"]).group(0)
        disposition = row.get("disposition", "").strip().strip("*_`").lower()
        note = row.get("reason or owner", "")
        if disposition not in DISPOSITIONS:
            errors.append("Objection %s needs a disposition: accepted, rejected or deferred" % oid)
        elif disposition == "rejected" and _blank(note):
            errors.append("Objection %s is rejected without a reason" % oid)
        elif disposition == "deferred" and _blank(note):
            errors.append("Objection %s is deferred without a named person" % oid)
    return errors
