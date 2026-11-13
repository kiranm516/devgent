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

SHELL_TOOLS = {"bash", "powershell", "shell", "execute"}
READ_TOOLS = {"view", "read"}
WRITE_TOOLS = {"edit", "create", "write", "multiedit", "str_replace_editor", "apply_patch", "str_replace"}
PATH_KEYS = ("path", "file_path", "filePath", "filename", "file", "target_file")

_SEG = r"[^|;&\n]*"
DANGEROUS = [
    ("force push", r"\bgit\b%s\bpush\b%s(--force|--force-with-lease|\s-[a-zA-Z]*f\b|\s\+\w)" % (_SEG, _SEG)),
    ("push to the default branch", r"\bgit\b%s\bpush\b%s[\s:](main|master)\b" % (_SEG, _SEG)),
    ("merging a pull request", r"\bgh\s+pr\s+merge\b|\bgh\s+api\b%s/merge\b" % _SEG),
    ("approving or dismissing a review", r"\bgh\s+pr\s+review\b|\bgh\s+api\b%s/(reviews|dismissals)\b" % _SEG),
    ("changing branch protection", r"\bgh\s+api\b%s/(protection|rulesets)\b" % _SEG),
    ("deploy", r"\bkubectl\s+(apply|delete|create|replace|patch|rollout|scale|set|edit|exec)\b"),
    ("deploy", r"\bhelm\s+(install|upgrade|uninstall|rollback|delete)\b"),
    ("deploy", r"\bterraform\s+(apply|destroy)\b"),
    ("deploy", r"\b(gh\s+workflow\s+run|gh\s+release\s+create|docker\s+push)\b"),
    ("publish", r"\b(gradlew?|mvnw?)\b%s\b(publish\w*|deploy|release)\b" % _SEG),
    ("secret access", r"\bgh\s+(auth\s+token|secret)\b"),
]
SECRET_PATH = re.compile(
    r"(^|[\s/\"'=<>])\.env(\.[\w-]+)*($|[\s\"';|&)])"
    r"|\.ssh/|\.aws/credentials|\.kube/config|\.netrc\b|\.gnupg/"
    r"|\bid_(rsa|dsa|ecdsa|ed25519)\b|\.(pem|p12|pfx)\b"
)
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
            return json.loads(raw), raw
        except ValueError:
            return {}, raw
    return (raw if isinstance(raw, dict) else {}), json.dumps(raw)


def write_paths(args, raw):
    paths = [args[k] for k in PATH_KEYS if isinstance(args.get(k), str)]
    for value in list(args.values()) + [raw]:
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


def check_shell(command, settings):
    for label, pattern in DANGEROUS:
        if re.search(pattern, command):
            raise Deny("dangerous-action", "blocked: %s is a human-only action" % label)
    if SECRET_PATH.search(command):
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

    if SECRET_PATH.search("/" + rel):
        deny("dangerous-action", "blocked: %s looks like a credential file" % rel)
    owner = settings.ticket_of(rel)
    ticket_file = rel.rsplit("/", 1)[-1] if owner else None

    for name, allowed, text in ROLE_RULES:
        if role and name in role.lower() and not allowed(rel, ticket_file):
            deny("role", "%s (tried to write %s)" % (text, rel))

    if owner:
        if ticket_file == "log.md" and tk.OVERRIDE_LINE.search(raw or ""):
            deny("override", "override entries in log.md are written by a human, not an agent")
        return

    ticket = tk.resolve_ticket(root, settings)
    plan = ticket.plan() if ticket else None
    if ticket is None:
        deny("ticket", (
            "no ticket for this branch. Create %s/<ticket-id>/plan.md first "
            "(`python3 -m agentwf new <ticket-id> --tier <tier>`), on a branch named after the ticket"
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
        if tool in SHELL_TOOLS:
            check_shell(str(args.get("command") or raw), settings)
        elif tool in READ_TOOLS or (tool == "str_replace_editor" and args.get("command") == "view"):
            if SECRET_PATH.search("/" + str(args.get("path") or raw)):
                raise Deny("dangerous-action", "blocked: reading a credential file")
        elif tool in WRITE_TOOLS:
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
    if tool in WRITE_TOOLS:
        record["paths"] = [p for p in (_relative(root, cwd, p) for p in write_paths(args, raw)) if p]
    elif tool in SHELL_TOOLS:
        status = tk.git(root, "status", "--porcelain", check=False).splitlines()
        record["changed_files"] = [line[3:] for line in status][:200]
    _audit(root, record)


def main(event):
    payload = json.load(sys.stdin)
    if event == "pre":
        decision = pre_tool_use(payload)
        if decision:
            print(json.dumps(decision))
    elif event in ("post", "post-failure"):
        try:
            post_tool_use(payload, failed=event == "post-failure")
        except Exception:  # the audit log must never break a session
            pass
    return 0
