"""Driver commands: start a ticket, move slices and state, record overrides,
and show where the ticket stands and what to do next."""
import os
import re
import time

from . import ci, config, ticket as tk


class FlowError(Exception):
    pass


def _active(root):
    settings = config.load_settings(root)
    ticket = tk.resolve_ticket(root, settings)
    if ticket is None:
        raise FlowError("no active ticket: the branch name must contain the ticket id "
                        "(start one with `./agentwf start <ticket-id> --tier T1`)")
    if ticket.read("log.md") is None:
        raise FlowError("%s is missing" % ticket.rel("log.md"))
    return settings, ticket


def _set_state(ticket, state, reason):
    path = os.path.join(ticket.root, ticket.rel("log.md"))
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    old = ticket.log().state or "?"
    if old == state:
        return
    if lines[0].startswith("state:"):
        lines[0] = "state: %s" % state
    else:
        lines.insert(0, "state: %s" % state)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    ticket.append_log("state %s -> %s%s" % (old, state, ": " + reason if reason else ""))


def start(root, ticket_id, tier):
    """Create the ticket's branch (unless already on one) and scaffold its folder."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", ticket_id):
        raise FlowError("invalid ticket id %r: use letters, digits, '.', '_' and '-'" % ticket_id)
    settings = config.load_settings(root)
    if os.path.exists(os.path.join(root, settings.artifact_root, ticket_id)):
        raise FlowError("%s/%s already exists" % (settings.artifact_root, ticket_id))
    branch = tk.git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if not tk._names(ticket_id, branch):
        tk.git(root, "switch", "-c", ticket_id)
        print("switched to new branch %s" % ticket_id)
    ci.new_ticket(root, ticket_id, tier)
    print("next: have the scout and architect fill %s/%s/plan.md, then run `./agentwf status`"
          % (config.load_settings(root).artifact_root, ticket_id))


def slice_step(root, action, slice_id):
    settings, ticket = _active(root)
    slice_id = slice_id.upper()
    plan, log = ticket.plan(), ticket.log()
    if plan is None or slice_id not in plan.slices:
        raise FlowError("%s is not defined in plan.md (defined: %s)"
                        % (slice_id, ", ".join(sorted(plan.slices)) if plan and plan.slices else "none"))
    current = log.slice_status.get(slice_id)
    if action == "start":
        others = [s for s in log.active_slices() if s != slice_id]
        waiting = [s for s in log.awaiting_review() if s != slice_id]
        if others:
            raise FlowError("%s is still in progress; one slice at a time" % others[0])
        if waiting:
            raise FlowError("%s is awaiting review; accept it before starting another" % waiting[0])
        if current == "started":
            raise FlowError("%s is already started" % slice_id)
        if current == "accepted":
            raise FlowError("%s is already accepted" % slice_id)
        ticket.append_log("slice %s started" % slice_id)
        if log.state in ("TRIAGED", "PLANNING", "PLAN_APPROVED", "IN_REVIEW", None):
            _set_state(ticket, "IMPLEMENTING", "")
    elif action == "review":
        if current != "started":
            raise FlowError("%s is not in progress (status: %s)" % (slice_id, current or "not started"))
        ticket.append_log("slice %s awaiting-review" % slice_id)
    else:
        if current != "awaiting-review":
            raise FlowError("%s is not awaiting review (status: %s)" % (slice_id, current or "not started"))
        ticket.append_log("slice %s accepted" % slice_id)
    expected = {"start": "started", "review": "awaiting-review", "accept": "accepted"}[action]
    now = ticket.log().slice_status.get(slice_id)
    if now != expected:
        raise FlowError("log.md did not take the entry (%s is %s, expected %s); check the end of %s"
                        % (slice_id, now, expected, ticket.rel("log.md")))
    print("%s: %s" % (slice_id, now))


def set_state(root, state, reason):
    _, ticket = _active(root)
    state = state.upper()
    if state not in tk.STATES:
        raise FlowError("unknown state %s (one of: %s)" % (state, ", ".join(tk.STATES)))
    if state in ("BLOCKED", "ABANDONED") and not reason:
        raise FlowError("%s needs a reason: name the question and its owner, or why the ticket stopped" % state)
    _set_state(ticket, state, reason)
    print("state: %s" % state)


def override(root, gate, reason):
    _, ticket = _active(root)
    if gate not in tk.GATES:
        raise FlowError("unknown gate %s (one of: %s)" % (gate, ", ".join(tk.GATES)))
    if not reason.strip():
        raise FlowError("an override needs a reason")
    who = tk.git(root, "config", "user.name", check=False) or os.environ.get("USER", "unknown")
    ticket.append_log("override %s by %s: %s" % (gate, who, reason.strip()))
    print("recorded in %s; commit it so CI sees it. It will be reviewed in the retrospective."
          % ticket.rel("log.md"))


def next_step(root, settings, ticket, plan, log, approval):
    """One sentence: what the driver should do now."""
    if plan is None:
        return "plan.md is missing; restore it or start again with `./agentwf start`"
    if log.state in ("BLOCKED", "ABANDONED", "DONE", "APPROVED"):
        return {"BLOCKED": "blocked: resolve the question in log.md, then `./agentwf state <STATE>`",
                "ABANDONED": "nothing: the ticket was abandoned",
                "APPROVED": "merge through the normal process, then write the retrospective in log.md "
                            "and run `./agentwf state DONE`",
                "DONE": "nothing: the ticket is done"}[log.state]
    errors = tk.check_plan_structure(plan)
    if plan.tier == "T0":
        if errors:
            return "fill in the task note (%s)" % "; ".join(errors)
        return "make the change, commit, and open a pull request for normal review"
    errors += tk.check_review_structure(ticket.read("review.md"), settings)
    if not approval[0]:
        if errors:
            return ("the plan is not ready for approval (%d issues; `./agentwf check` lists them). "
                    "First: %s" % (len(errors), errors[0]))
        return ("commit the plan, open a draft PR (`gh pr create --draft`) and ask the approver "
                "for an approving review. Detail: %s" % approval[1])
    active, waiting = log.active_slices(), log.awaiting_review()
    if active:
        return ("the implementer is on %s. When its commit is in: `./agentwf slice review %s`"
                % (active[0], active[0]))
    if waiting:
        return ("review %s, then `./agentwf slice accept %s` (or `./agentwf slice start %s` for a fix round)"
                % (waiting[0], waiting[0], waiting[0]))
    todo = [s for s in sorted(plan.slices) if log.slice_status.get(s) != "accepted"]
    if todo:
        return "`./agentwf slice start %s`, then hand it to the implementer" % todo[0]
    if log.state != "IN_REVIEW":
        return "all slices are accepted: `./agentwf state IN_REVIEW`, then run the reviewer on the diff"
    return ("run `./agentwf check`, mark the PR ready and ask the approver to review the diff; "
            "after approval `./agentwf state APPROVED`")


def status(root):
    """One screen a new session, or a human, can resume from."""
    settings = config.load_settings(root)
    ticket = tk.resolve_ticket(root, settings)
    if ticket is None:
        print("no active ticket on this branch")
        print("\nnext: `./agentwf start <ticket-id> --tier T0|T1|T2` "
              "(`./agentwf tier` shows the floor for work already on the branch)")
        return 1
    plan, log = ticket.plan(), ticket.log()
    print("ticket        %s" % ticket.id)
    print("state         %s" % (log.state or "unknown (line 1 of log.md must be `state: ...`)"))
    print("tier          %s   plan version %s" % (plan and plan.tier, plan and plan.version))
    approval = (True, "not needed for T0")
    if plan and plan.tier in ("T1", "T2"):
        approval = tk.plan_approval(root, ticket)
        print("plan approval %s" % ("yes" if approval[0] else "no"))
        for slice_id in sorted(plan.slices):
            print("slice %-7s %s" % (slice_id, log.slice_status.get(slice_id, "not started")))
    for gate, text in sorted(log.overrides.items()):
        print("override      %s by %s" % (gate, text))
    print("\nnext: %s" % next_step(root, settings, ticket, plan, log, approval))
    return 0
