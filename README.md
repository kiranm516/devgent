# devgent

An implementation of the Agentic Development Workflow Spec v0.2 for Java / Spring Boot REST
services, driven from GitHub Copilot CLI.

Every ticket follows one path: triage, plan, human-answered decisions, small slices, independent
review, human-approved diff. The gates are enforced by a Copilot CLI hook, a CI job and branch
protection, not by prompt instructions.

## What is in here

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | Short entry point every agent reads |
| `.github/copilot-instructions.md` | Repository conventions (fill in the TODOs per service) |
| `.github/agents/` | `scout`, `architect`, `implementer`, `reviewer` |
| `.github/skills/` | `spring-rest-api-design`, `spring-boot-testing`, `secure-api-review` |
| `.github/agent-workflow.yml` | Tunable settings: slice cap, WIP limits, tier path rules |
| `.github/hooks/agent-workflow.json` | Registers the Copilot CLI hooks |
| `.github/hooks/agentwf/` | The enforcement code, shared by hooks and CI (Python 3, standard library only) |
| `.github/workflows/agent-gates.yml` | CI job that runs the gates on every pull request |
| `.github/CODEOWNERS` | Owners for sensitive paths (placeholders, commented out) |
| `docs/agent-work/_templates/` | Templates for `plan.md`, the T0 task note, `log.md`, `review.md` |

## Install in a service repository

1. Copy `AGENTS.md`, `.github/` and `docs/agent-work/_templates/` into the repository.
2. Fill in the TODOs in `.github/copilot-instructions.md`.
3. Tune `tier_rules` and `slice.production_paths` in `.github/agent-workflow.yml` to the repository's layout.
4. Replace the placeholders in `.github/CODEOWNERS`.
5. Protect the default branch (repository admin):
   - require a pull request with at least one approving review, and dismiss stale approvals on new commits
   - require the `gates` status check
   - block force pushes and direct pushes
   - Phase 2: require review from code owners
6. Each developer needs Python 3, the `gh` CLI logged in (`gh auth login`), and Copilot CLI.

Shell shortcut used below: `alias agentwf='PYTHONPATH=.github/hooks python3 -m agentwf'`

## A ticket, step by step

| Step | Who | What happens |
| --- | --- | --- |
| 1. Triage | scout, driver | `git switch -c ABC-123-short-name`. The scout proposes a tier; the path rules set a floor (`agentwf tier`). The driver confirms and runs `agentwf new ABC-123 --tier T1`. |
| 2. Plan | architect, reviewer | The architect fills `plan.md`. The reviewer records up to five objections in `review.md`. |
| 3. Approve the plan | approver | The driver commits and opens a draft PR. The approver answers every decision, gives every objection a disposition, and submits a GitHub approving review. The approver is not the driver. |
| 4. Implement | implementer | One slice at a time: log `slice S-01 started`, write code and tests inside the slice's paths, commit with a `Slice: S-01` trailer, log `slice S-01 awaiting-review`, stop. The driver logs `accepted`. |
| 5. Review | reviewer, approver | The reviewer writes findings and a one-screen brief in `review.md` (at most two fix rounds). A human approves the diff; merge follows the normal process. |

`agentwf status` shows the ticket, state, tier, approval and slice status, so a new session resumes
from the artifacts alone. T0 tickets carry only a task note in `plan.md`; the driver approves it
and normal PR review covers the diff.

The branch name must contain the ticket id (or set `AGENT_TICKET`). Any change to `plan.md` after
approval cancels the approval, including answers: commit the answers first, then approve.

## Gates

| Rule | Hook (guard rail) | CI (boundary) |
| --- | --- | --- |
| Source edits need a ticket | `ticket` | `ticket`: exactly one ticket folder per PR |
| No source edits before plan approval (T1, T2) | `plan-approval`: asks GitHub, through `gh`, for an approving review on a commit whose `plan.md` is identical to the current one | Branch protection |
| Tier floor from path rules | `tier-floor`, per file written | `tier-floor`, on the final diff |
| Writes stay inside the active slice; one slice in progress | `slice-paths` | `slice-paths`: per commit, from the `Slice:` trailer |
| Slice size cap | | `slice-size`: changed production lines per commit |
| Decisions answered, objections dispositioned, plan ready for approval | | `structure` |
| Force push, push to the default branch, merge, approve, deploy, publish, production hosts, credential files | `dangerous-action` (no override) | Branch protection |
| Role limits (scout, architect, reviewer, implementer) | `role`, when the CLI reports the agent | |
| Audit trail | `.git/agent-workflow/audit.jsonl`: ticket, plan version, role, tool, changed files. Never prompts, commands or file content. | |

Run the CI gates locally with `agentwf ci --base origin/main`.

### Overrides

Overrides are allowed and visible. Only a human sets one, and each names its gate and a reason.

- Local hook: start the CLI with `AGENT_WORKFLOW_OVERRIDE="plan-approval: approver on leave, agreed in standup" copilot`.
  The hook lets that gate pass and appends the override to `log.md`.
- CI: add `- 2026-11-13 override tier-floor by <your name>: <reason>` to `log.md`. The gate reports
  `overridden` in the job summary instead of failing. This is also how a human lowers a tier below the path floor.

Agents are blocked from writing override lines through their edit tools. Review every override in the retrospective.

## Limits and things to confirm in the pilot

- **A local hook is not a boundary.** It can be disabled, and it cannot see what a shell command
  writes: an agent using `sed -i` or a redirect bypasses the path checks. CI and branch protection catch the result.
- **Hook payload.** GitHub documents `toolName` and `toolArgs` but not the argument names of the
  file tools. The hook accepts the common ones (`path`, `file_path`, patch headers) and refuses a
  write whose target it cannot identify. Confirm on the installed CLI version.
- **Hook timeouts fail open** in Copilot CLI; a crash fails closed. The approval check allows 12 seconds per `gh` call.
- **Role and model.** The CLI reports the agent only for subagents, and never the model, so role
  limits apply only then and the audit log's `model` field is empty unless `COPILOT_MODEL` is set.
- **Per-agent `model`.** The agent files leave `model` as a comment; whether the CLI honours it for subagents is unconfirmed.
- **Stage order** rests on the gates. Nothing here makes the CLI run the agents in order.
- **Not machine-checked:** one plan awaiting approval per driver, the two-fix-round cap, the T2
  conditional sections other than Alternatives, and breaking-change detection (pass
  `--breaking-api-change` to `agentwf ci` from your API diff tool).
- **Not built (Phase 3):** dynamic workflows, usage telemetry export, metrics reporting, calibration exercises.

## Developing the gates

```
PYTHONPATH=.github/hooks python3 -m unittest discover -s .github/hooks/tests
```

Changes to hooks, agents, the settings file or CODEOWNERS are T2 by the path rules, and go through a reviewed PR like any other change.
