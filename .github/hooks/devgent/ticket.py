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
GATES = ("ticket", "tier-floor", "plan-approval", "slice-paths", "slice-size", "structure", "review")


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
    """Local, never-committed directory for the approval cache and audit log.
    It lives in the main checkout's .git, so every worktree of the repository shares it."""
    common = git(root, "rev-parse", "--git-common-dir")
    path = os.path.join(common if os.path.isabs(common) else os.path.join(root, common), "devgent")
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# Worktrees: one checkout per ticket, so a developer can work several tickets at once
# --------------------------------------------------------------------------


def worktrees(root):
    """[(path, branch or None)] for every checkout of this repository, the main one first."""
    out, path, branch = [], None, None
    for line in git(root, "worktree", "list", "--porcelain", check=False).splitlines() + [""]:
        if line.startswith("worktree "):
            path, branch = line[len("worktree "):], None
        elif line.startswith("branch "):
            branch = line[len("branch "):]
            branch = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
        elif not line and path:
            out.append((path, branch))
            path = None
    return out


def main_worktree(root):
    """Root of the main checkout, the one that owns .git; `root` itself unless it is a linked worktree."""
    listed = worktrees(root)
    return listed[0][0] if listed else root


def is_linked_worktree(root):
    return os.path.realpath(main_worktree(root)) != os.path.realpath(root)


def worktree_path(root, settings, ticket_id):
    """Where `start` puts the ticket's checkout: `worktrees.dir` from the settings (relative to
    the main checkout), else a `<repo>.worktrees` folder next to it; the ticket id names the folder."""
    main = main_worktree(root)
    base = settings.worktrees_dir or os.path.join("..", os.path.basename(main) + ".worktrees")
    return os.path.normpath(os.path.join(main, base, ticket_id))


def ticket_worktrees(root, settings):
    """[(ticket_id, path, state)] for the tickets checked out in this repository's other worktrees."""
    out = []
    for path, branch in worktrees(root):
        if not branch or os.path.realpath(path) == os.path.realpath(root):
            continue
        try:
            other = resolve_ticket(path, settings, branch=branch)
        except GitError:
            other = None
        if other is not None:
            out.append((other.id, path, other.log().state))
    return out


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


def decisions(text):
    """`### D-01: question` blocks of a decision list. Each is a dict with id, title,
    options ([(letter, text)] from `- A: ...` bullets) and the other bullets by
    lower-cased key: why a human, recommendation, reversible, answer."""
    out, current, key = [], None, None
    for line in text.splitlines():
        head = re.match(r"###\s+(D-\d+)\b[\s:.-]*(.*)", line)
        bullet = re.match(r"[-*]\s+\**([A-Za-z][A-Za-z' ]{0,30}?)\**\s*:\**\s*(.*)", line)
        if head:
            current = {"id": head.group(1), "title": head.group(2).strip(), "options": []}
            out.append(current)
            key = None
        elif current is None:
            continue
        elif bullet:
            key, value = bullet.group(1).strip().lower(), bullet.group(2).strip()
            if len(key) == 1:
                current["options"].append([key.upper(), value])
            else:
                current[key] = value
        elif key and line.strip():  # continuation of the bullet above
            if len(key) == 1:
                current["options"][-1][1] += " " + line.strip()
            else:
                current[key] += " " + line.strip()
    return out


# --------------------------------------------------------------------------
# Layers: a plan reads at three levels of abstraction, each for its own reader
# --------------------------------------------------------------------------

# (tag, `##` heading, the `###` sections the layer must carry)
LAYERS = (
    ("functional", "Functional Scenarios", ("problem and outcome", "requirements", "scenarios")),
    ("high-level", "High-Level Design", ("proposed flow",)),
    ("low-level", "Low-Level Design", ("change map", "test matrix", "slices")),
)
LAYER_TAGS = tuple(tag for tag, _, _ in LAYERS)
LAYER_CHOICES = ", ".join(LAYER_TAGS[:-1]) + " or " + LAYER_TAGS[-1]

_SOURCE = "java|kt|kts|groovy|scala|py|js|jsx|ts|tsx|go|rb|cs|sql|xml|yml|yaml|json|properties|gradle|sh"


def _file_ref(extensions):
    return re.compile(r"(?<![\w/.-])(?:(?:[\w.-]+/)*[\w-]+\.(?:%s)\b(?::\d+)?|src/[\w/*.-]+)" % extensions)


# A source file or a `path:line` reference: what the functional layer must not carry.
FILE_REF = _file_ref(_SOURCE)
# The same plus documents: everything a plan cites as evidence or lists to change.
CITED_REF = _file_ref(_SOURCE + "|md")


def layer_tag(text):
    """The layer a decision, question or objection is tagged with, or None."""
    tag = re.sub(r"[\s_]+", "-", (text or "").strip().strip("*_`").strip().lower())
    return tag if tag in LAYER_TAGS else None


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


class Plan:
    """plan.md. `sections` maps a lower-cased heading to its body whichever layout the plan
    uses: the three layers (`## Functional Scenarios`, `## High-Level Design`,
    `## Low-Level Design`, their parts one heading level down) or the flat layout of plans
    written before the layers, which stay valid because an approved plan is frozen."""

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
        self.layers = {tag: self.sections[heading.lower()] for tag, heading, _ in LAYERS
                       if heading.lower() in self.sections}
        self.layered = bool(self.layers)
        for layer in self.layers.values():
            for name, part in sections(layer, level=3).items():
                self.sections.setdefault(name, part)
        if "scenarios and api contract" in self.sections:
            self.sections.setdefault("scenarios", self.sections["scenarios and api contract"])
        if not self.layered and "design" in self.sections:
            self.sections.setdefault("change map", sections(self.sections["design"], level=3).get("change map", ""))
        self.tier = self.meta.get("tier")
        self.version = self.meta.get("plan_version")
        # slice heading -> body: one level below `Slices`, wherever the layout puts that
        self.slice_blocks = sections(self.sections.get("slices", ""), level=4 if self.layered else 3)
        self.slices, self.slice_covers = self._slices()
        self.decisions = decisions(self.sections.get("decision list", ""))

    def _slices(self):
        """({S-nn: allowed paths}, {S-nn: the R-nn and SC-nn ids its block names})."""
        out, covers = {}, {}
        for title, body in self.slice_blocks.items():
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
            covers[m.group(1).upper()] = set(re.findall(r"(?:R|SC)-\d+", body))
        return out, covers


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


def resolve_ticket(root, settings, branch=None):
    """Active ticket for a local session: env, then branch name, then branch diff.
    `branch` names the checked-out branch when the caller knows it already (another worktree);
    the DEVGENT_TICKET shortcut then does not apply, since it describes this session only."""
    tickets = list_tickets(root, settings)
    env = os.environ.get("DEVGENT_TICKET")
    if env and branch is None:
        return Ticket(root, settings, env) if env in tickets else None
    if branch is None:
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

    # one cache file per ticket: worktrees share the state directory, and a lookup for one
    # ticket must not evict another's entry
    cache_file = os.path.join(state_dir(root), "approval-%s.json" % ticket.id)
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

FLAT_SECTIONS = (  # the layout of plans written before the layers
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


def _missing_sections(plan):
    sec, errors = plan.sections, []
    if not plan.layered:
        return ["plan.md is missing a non-empty `## %s` section" % name.capitalize()
                for name in FLAT_SECTIONS if not sec.get(name)]
    for tag, heading, parts in LAYERS:
        if not plan.layers.get(tag):
            errors.append("plan.md is missing a non-empty `## %s` layer" % heading)
            continue
        own = sections(plan.layers[tag], level=3)
        errors += ["%s: `### %s` is missing or empty" % (heading, name.capitalize())
                   for name in parts if not own.get(name)]
    if plan.tier == "T2" and plan.layers.get("high-level") and not sec.get("current flow"):
        errors.append("High-Level Design: a T2 plan needs a non-empty `### Current flow`")
    if not sec.get("decision list"):
        errors.append("plan.md is missing a non-empty `## Decision list` section")
    return errors


def check_plan_structure(plan):
    sec = plan.sections
    if plan.tier == "T0":
        return ["task note is missing a `## %s` section" % s.capitalize()
                for s in TASK_NOTE_SECTIONS if not sec.get(s)]

    errors = _missing_sections(plan)
    if errors:
        return errors

    if plan.layered:
        leaks = []
        for ref in FILE_REF.findall(plan.layers["functional"]):
            if ref not in leaks:
                leaks.append(ref)
        if leaks:
            errors.append(
                "Functional Scenarios: names implementation files (%s%s); describe observable behaviour "
                "here and keep files and classes in the design layers"
                % (", ".join("`%s`" % r for r in leaks[:3]), ", ..." if len(leaks) > 3 else ""))

    reqs = sorted(set(re.findall(r"^\s*[-*]\s+\**(R-\d+)\b", sec["requirements"], flags=re.M)))
    if not reqs:
        errors.append("Requirements: no numbered criteria (`- R-01: ...`)")
    scenarios = {}
    for row in table_rows(sec["scenarios"]):
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

    change_map = sec.get("change map", "")
    if not table_rows(change_map) and not re.search(r"^\s*[-*]\s+\S", change_map, flags=re.M):
        errors.append("%s: `### Change map` is missing or empty" % ("Low-Level Design" if plan.layered else "Design"))

    if not plan.slices:
        errors.append("Slices: no slice defined (`%s S-01: goal`)" % ("####" if plan.layered else "###"))
    for slice_id, paths in sorted(plan.slices.items()):
        if not paths:
            errors.append("Slice %s lists no allowed paths (`- Paths: `...``)" % slice_id)
    if plan.layered and plan.slices:
        # a scenario reaches a slice by its own id or through a requirement it covers
        for sc, covered in sorted(scenarios.items()):
            if not any(sc in ids or covered & ids for ids in plan.slice_covers.values()):
                errors.append("Scenario %s reaches no slice: name it, or a requirement it covers, "
                              "in a slice's `- Covers:`" % sc)

    if not plan.decisions and re.search(r"^\s*\|.*\bD-\d+", sec["decision list"], flags=re.M):
        errors.append("Decision list: write each decision as a block (`### D-01: question`), not a table row")
    for d in plan.decisions:
        if len(d["options"]) < 2:
            errors.append("Decision %s needs at least two options (`- A: ...`, `- B: ...`)" % d["id"])
        if plan.layered and not layer_tag(d.get("layer")):
            errors.append("Decision %s needs its layer (`- Layer: %s`)" % (d["id"], LAYER_CHOICES))
        if _blank(d.get("recommendation", "")):
            errors.append("Decision %s has no recommendation and rationale" % d["id"])
        if _blank(d.get("answer", "")):
            errors.append("Decision %s has no approver's answer" % d["id"])

    for row in table_rows(sec.get("open questions", "")):
        if _blank(row.get("id", "")) and _blank(row.get("question", "")):
            continue
        if plan.layered and not layer_tag(row.get("layer")):
            errors.append("Open question %s needs its layer in a `Layer` column (%s)"
                          % (row.get("id") or "?", LAYER_CHOICES))
        blocking = row.get("blocking", "").strip().lower().startswith("y")
        status = row.get("status", "").strip().lower()
        if blocking and status not in ("answered", "closed", "risk-accepted"):
            errors.append("Open question %s is blocking and unresolved" % (row.get("id") or "?"))

    if plan.tier == "T2" and not sec.get("alternatives"):
        errors.append("T2 plan needs a non-empty `%s Alternatives` section%s"
                      % (("###", " in the High-Level Design") if plan.layered else ("##", "")))
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
        if "layer" in row and not layer_tag(row["layer"]):
            errors.append("Objection %s needs its layer: %s" % (oid, LAYER_CHOICES))
        if disposition not in DISPOSITIONS:
            errors.append("Objection %s needs a disposition: accepted, rejected or deferred" % oid)
        elif disposition == "rejected" and _blank(note):
            errors.append("Objection %s is rejected without a reason" % oid)
        elif disposition == "deferred" and _blank(note):
            errors.append("Objection %s is deferred without a named person" % oid)
    return errors
