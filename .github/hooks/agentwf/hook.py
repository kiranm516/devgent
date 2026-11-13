"""Copilot CLI hooks: preToolUse gate and postToolUse audit log.

A local hook is a guard rail, not the boundary: it can be disabled on a
developer machine and it cannot see what a shell command writes. CI and
branch protection are the boundary.

Contract (Copilot CLI hooks reference): the hook reads one JSON object on
stdin and may print {"permissionDecision": "deny", "permissionDecisionReason"}.
A crash is treated by the CLI as a denial; a timeout is not.
"""
import json
import os
import re
import sys
import tempfile
import time

from . import config, ticket as tk

SHELL_WORDS = ("bash", "powershell", "shell", "execute")
WRITE_TOOLS = {"edit", "create", "write", "multiedit", "str_replace_editor", "apply_patch", "str_replace"}
PATH_KEYS = ("path", "file_path", "filePath", "filename", "file", "target_file")

_SEG = r"[^|;&\n]*"
_PUSH = r"\bgit\b%s\bpush\b%s" % (_SEG, _SEG)
_GH_WRITE = (r"(\s-X\s*(POST|PUT|PATCH|DELETE)|\s--method[\s=]+(POST|PUT|PATCH|DELETE)"
             r"|\s-[fF]\b|\s--(raw-)?field\b|\s--input\b)")
DANGEROUS = [
    ("force push", _PUSH + r"(--force|--force-with-lease|--mirror|\s-[a-zA-Z]*f\b|\s\+\w)"),
    ("deleting a remote branch", _PUSH + r"(--delete\b|\s-d\b|\s:\w)"),
    ("merging a pull request", r"\bgh\s+pr\s+merge\b|mergePullRequest|enablePullRequestAutoMerge"),
    ("merging a pull request", r"\bgh\s+api\b%s/merge\b" % _SEG),
    ("approving or dismissing a review",
     r"\bgh\s+pr\s+review\b|addPullRequestReview|submitPullRequestReview|dismissPullRequestReview"),
    ("writing a review", r"\bgh\s+api\b%s/(reviews|dismissals)\b%s%s" % (_SEG, _SEG, _GH_WRITE)),
    ("changing branch protection", r"\bgh\s+api\b%s/(protection|rulesets)\b%s%s" % (_SEG, _SEG, _GH_WRITE)),
    ("deploy", r"\bkubectl\s+(apply|delete|create|replace|patch|rollout|scale|set|edit|exec)\b"),
    ("deploy", r"\bhelm\s+(install|upgrade|uninstall|rollback|delete)\b"),
    ("deploy", r"\bterraform\s+(apply|destroy)\b"),
    ("deploy", r"\b(gh\s+workflow\s+run|gh\s+release\s+create|docker\s+push)\b"),
    # a Gradle/Maven task named publish*, deploy or release; not a package, project or property value
    ("publish", r"\b(gradlew?|mvnw?)\b%s(?<![\w.=-])(publish(?!ToMavenLocal)\w*|deploy|release)(?![\w.=-])" % _SEG),
    ("secret access", r"\bgh\s+(auth\s+token|secret)\b"),
    ("overriding a gate, accepting a slice or changing the kit",
     r"\bagentwf(\.cmd)?[\"']?\s+(override|update|install|slice\s+accept)\b"),
    ("writing a human-only log entry", r"\boverride\s+[a-z-]+\s+by\b|\bslice\s+S-\d+\s+accepted\b"),
]
_SEP = r"[\\/]"
SECRET_PATH = re.compile(
    r"(^|[\s\\/\"'=<>])\.env(\.[\w-]+)*($|[\s\"';|&)])"
    r"|\.ssh%s|\.aws%scredentials|\.kube%sconfig|\.netrc\b|\.gnupg%s" % (_SEP, _SEP, _SEP, _SEP) +
    r"|\bid_(rsa|dsa|ecdsa|ed25519)\b|\.(pem|p12|pfx)\b"
)
_SAFE_ENV = re.compile(r"\.env\.(example|sample|template|dist)\b")
# quoted commit messages are text, not commands (unless they embed a substitution)
_MESSAGE = re.compile(r"(\s-[a-zA-Z]*m|\s--message)(\s+|=)('[^']*'|\"[^\"$`]*\")")
# words an agent may not put into log.md: those entries belong to a human
HUMAN_ONLY_LOG = re.compile(r"override|accepted", flags=re.I)
ROLE_RULES = (
    # (role substring, predicate(path, ticket_file) -> allowed, description)
    ("scout", lambda p, f: False, "the scout writes nothing"),
    ("explore", lambda p, f: False, "the scout writes nothing"),
    ("architect", lambda p, f: f is not None, "the architect writes only the ticket's artifact folder"),
    ("reviewer", lambda p, f: f == "review.md", "the reviewer writes only review.md"),
    ("implementer", lambda p, f: f not in ("plan.md", "review.md"), "the implementer may not change plan.md or review.md"),
)


class Deny(Exception):
    def __init__(self, gate, reason):
        Exception.__init__(self, reason)
        self.gate, self.reason = gate, reason


def _field(payload, *names):
    for name in names:
        if payload.get(name) is not None:
            return payload[name]
    return None


def _args(payload):
    raw = _field(payload, "toolArgs", "tool_input")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return (parsed if isinstance(parsed, dict) else {}), raw
    return (raw if isinstance(raw, dict) else {}), json.dumps(raw)


def write_paths(args, raw):
    paths = [args[k] for k in PATH_KEYS if isinstance(args.get(k), str)]
    # Patch headers name the targets only when the tool gives no path argument;
    # otherwise a diff quoted inside a document would look like a write.
    for value in ([] if paths else list(args.values()) + [raw]):
        if isinstance(value, str) and ("*** " in value or "+++ " in value):
            paths += re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", value, flags=re.M)
            paths += re.findall(r"^\*\*\* Move to: (.+?)\s*$", value, flags=re.M)
            paths += re.findall(r"^\+\+\+ b/(.+?)\s*$", value, flags=re.M)
    return sorted(set(paths))


def _relative(root, cwd, path):
    full = os.path.realpath(os.path.join(cwd or root, os.path.expanduser(path)))
    rel = os.path.relpath(full, os.path.realpath(root))
    return None if rel.startswith("..") else rel.replace(os.sep, "/")


def _override(gate):
    """Reason if the human driver set AGENT_WORKFLOW_OVERRIDE="<gate>[,<gate>]: reason"."""
    value = os.environ.get("AGENT_WORKFLOW_OVERRIDE", "")
    if ":" not in value:
        return None
    gates, reason = value.split(":", 1)
    names = [g.strip() for g in gates.split(",")]
    return reason.strip() if reason.strip() and (gate in names or "all" in names) else None


def _secret(text):
    return SECRET_PATH.search(_SAFE_ENV.sub("", text)) is not None


def _protected_branches(root):
    names = {"main", "master"}
    head = tk.git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if "/" in head:
        names.add(head.split("/", 1)[1])
    return names


def check_shell(command, settings, root=None):
    command = _MESSAGE.sub(" ", command)
    for label, pattern in DANGEROUS:
        if re.search(pattern, command):
            raise Deny("dangerous-action", "blocked: %s is a human-only action" % label)
    if re.search(_PUSH, command):
        protected = _protected_branches(root) if root else {"main", "master"}
        names = "|".join(re.escape(n) for n in sorted(protected))
        current = tk.git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False) if root else ""
        if re.search(_PUSH + r"(\s|:)(refs/heads/)?(%s)(?![\w./-])" % names, command) or current in protected:
            raise Deny("dangerous-action", "blocked: pushing to the default branch is a human-only action")
    if _secret(command):
        raise Deny("dangerous-action", "blocked: the command touches a credential file")
    for host in settings.production_hosts:
        if host and host in command:
            raise Deny("dangerous-action", "blocked: %s is a production endpoint" % host)


def check_write(root, settings, rel, raw, role, approval=tk.plan_approval, waive=lambda gate: None):
    """Raise Deny unless writing repo-relative `rel` is allowed right now.

    `waive(gate)` returns a reason when a human override covers that gate; the
    remaining gates are still checked.
    """
    def deny(gate, reason):
        if gate not in tk.GATES or not waive(gate):
            raise Deny(gate, reason)

    if _secret("/" + rel):
        deny("dangerous-action", "blocked: %s looks like a credential file" % rel)
    if rel == ".git" or rel.startswith(".git/"):
        deny("dangerous-action", "blocked: agents do not write inside .git")
    owner = settings.ticket_of(rel)
    ticket_file = rel.rsplit("/", 1)[-1] if owner else None

    for name, allowed, text in ROLE_RULES:
        if role and name in role.lower() and not allowed(rel, ticket_file):
            deny("role", "%s (tried to write %s)" % (text, rel))

    if owner:
        if ticket_file == "log.md" and HUMAN_ONLY_LOG.search(raw or ""):
            deny("override", (
                "override and slice-accepted entries are written by a human (`./agentwf override`, "
                "`./agentwf slice accept`). An agent edit to log.md may not contain the words "
                "'override' or 'accepted': append your own line without quoting existing ones"
            ))
        return

    ticket = tk.resolve_ticket(root, settings)
    plan = ticket.plan() if ticket else None
    if ticket is None:
        deny("ticket", (
            "no ticket for this branch. Create %s/<ticket-id>/plan.md first "
            "(`./agentwf start <ticket-id> --tier <tier>`)"
        ) % settings.artifact_root)
        return
    if plan is None or plan.tier not in config.TIERS:
        deny("ticket", "%s is missing or has no valid `tier`" % ticket.rel("plan.md"))
        return

    floor, reasons = settings.tier_floor([(rel, not os.path.exists(os.path.join(root, rel)))])
    if not config.tier_at_least(plan.tier, floor):
        deny("tier-floor", (
            "%s requires at least %s but the ticket is %s. Stop and re-triage; "
            "only a human may lower a tier"
        ) % (reasons[0], floor, plan.tier))
    declared = ticket.highest_declared_tier(ticket.plan_history())
    if declared and declared > plan.tier and "tier-floor" not in ticket.log().overrides:
        deny("tier-floor", (
            "plan.md declared %s earlier and now says %s. Only a human may lower a tier: "
            "`./agentwf override tier-floor <reason>`"
        ) % (declared, plan.tier))
    if plan.tier == "T0":
        return

    ok, reason = approval(root, ticket)
    if not ok:
        deny("plan-approval", "no source edits before plan approval: " + reason)

    log = ticket.log()
    active = log.active_slices()
    if len(active) != 1:
        waiting = log.awaiting_review()
        if waiting and not active:
            why = "slice %s is awaiting review; do not start the next one" % waiting[0]
        else:
            why = "log.md must show exactly one slice as started (found %s)" % (", ".join(active) or "none")
        deny("slice-paths", why)
        return
    allowed = plan.slices.get(active[0])
    if allowed is None:
        deny("slice-paths", "slice %s is not defined in the approved plan" % active[0])
    elif not config.matches(rel, allowed):
        deny("slice-paths", (
            "%s is outside the paths of active slice %s. If the plan is wrong, "
            "stop and log the mismatch; do not widen the slice yourself"
        ) % (rel, active[0]))


def _audit(root, record):
    try:
        with open(os.path.join(tk.state_dir(root), "audit.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, tk.GitError):
        pass


def _context(root, settings, payload):
    """Audit fields. Never includes prompt, command or file content."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "session": _field(payload, "sessionId", "session_id"),
        "role": _field(payload, "agentName", "agentType", "agent_type") or os.environ.get("AGENT_ROLE"),
        "model": os.environ.get("COPILOT_MODEL"),
        "tool": _field(payload, "toolName", "tool_name"),
    }
    try:
        ticket = tk.resolve_ticket(root, settings)
        if ticket:
            plan = ticket.plan()
            record.update(ticket=ticket.id, tier=plan and plan.tier, plan_version=plan and plan.version)
    except tk.GitError:
        pass
    return record


def pre_tool_use(payload):
    """Returns a decision dict to print, or None for the CLI's default behaviour."""
    cwd = payload.get("cwd") or os.getcwd()
    root = tk.repo_root(cwd)
    if not root or not os.path.exists(os.path.join(root, config.SETTINGS_PATH)):
        return None
    settings = config.load_settings(root)
    tool = str(_field(payload, "toolName", "tool_name") or "").lower()
    args, raw = _args(payload)
    role = _field(payload, "agentName", "agentType", "agent_type") or os.environ.get("AGENT_ROLE")

    paths = []
    try:
        if any(word in tool for word in SHELL_WORDS):
            check_shell(str(args.get("command") or args.get("input") or raw), settings, root)
        elif tool not in WRITE_TOOLS or (tool == "str_replace_editor" and args.get("command") == "view"):
            # every other tool (view, grep, glob, ...): no credential files as targets
            targets = [args.get(k) for k in PATH_KEYS + ("paths", "glob")]
            flat = [t for v in targets for t in (v if isinstance(v, list) else [v]) if isinstance(t, str)]
            if any(_secret("/" + t) for t in flat):
                raise Deny("dangerous-action", "blocked: reading a credential file")
        else:
            paths = write_paths(args, raw)
            if not paths:
                raise Deny("unknown-write", "cannot tell which file `%s` would write; refusing" % tool)
            for path in paths:
                rel = _relative(root, cwd, path)
                if rel is None:
                    tmp = os.path.realpath(tempfile.gettempdir())
                    if not os.path.realpath(os.path.join(cwd, path)).startswith(tmp + os.sep):
                        raise Deny("outside-repo", "writes outside the repository are not allowed: %s" % path)
                    continue
                def waive(gate):
                    reason = _override(gate)
                    if reason:
                        _record_override(root, settings, gate, reason)
                    return reason
                check_write(root, settings, rel, raw, role, waive=waive)
    except Deny as deny:
        record = _context(root, settings, payload)
        record.update(event="denied", gate=deny.gate, paths=paths)
        _audit(root, record)
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": "[agent-workflow:%s] %s" % (deny.gate, deny.reason),
        }
    return None


def _record_override(root, settings, gate, reason):
    who = tk.git(root, "config", "user.name", check=False) or os.environ.get("USER", "unknown")
    _audit(root, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": "override", "gate": gate,
                  "by": who, "reason": reason})
    ticket = tk.resolve_ticket(root, settings)
    if ticket and ticket.read("log.md") is not None and gate not in ticket.log().overrides:
        ticket.append_log("override %s by %s: %s" % (gate, who, reason))


def post_tool_use(payload, failed=False):
    cwd = payload.get("cwd") or os.getcwd()
    root = tk.repo_root(cwd)
    if not root or not os.path.exists(os.path.join(root, config.SETTINGS_PATH)):
        return
    settings = config.load_settings(root)
    args, raw = _args(payload)
    record = _context(root, settings, payload)
    result = _field(payload, "toolResult", "tool_result") or {}
    record["event"] = "tool"
    record["result"] = "failure" if failed else (result.get("resultType") or result.get("result_type"))
    tool = str(record["tool"] or "").lower()
    if any(word in tool for word in SHELL_WORDS):
        status = tk.git(root, "status", "--porcelain", check=False).splitlines()
        record["changed_files"] = [line[3:] for line in status][:200]
    elif tool in WRITE_TOOLS:
        record["paths"] = [p for p in (_relative(root, cwd, p) for p in write_paths(args, raw)) if p]
    _audit(root, record)


def main(event):
    if event == "pre":
        try:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ValueError("hook input is not a JSON object")
            decision = pre_tool_use(payload)
        except Exception as exc:  # fail closed, with a reason the agent can read
            decision = {
                "permissionDecision": "deny",
                "permissionDecisionReason": "[agent-workflow:hook-error] %s: %s. Run `./agentwf doctor`."
                                            % (type(exc).__name__, exc),
            }
        if decision:
            print(json.dumps(decision))
        return 0
    if event in ("post", "post-failure"):
        try:
            post_tool_use(json.load(sys.stdin), failed=event == "post-failure")
        except Exception:  # the audit log must never break a session
            pass
    return 0
