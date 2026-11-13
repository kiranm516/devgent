# devgent

A plan-first workflow for AI coding agents on Java / Spring Boot REST services, driven from GitHub
Copilot CLI. Every ticket follows one path: triage, plan, human-answered decisions, small slices,
independent review, human-approved diff. A Copilot CLI hook, a CI job and branch protection enforce
it; prompts only guide.

This repository is the kit. You install it into each service repository and update it from here.

- [Install](#install)
- [Update](#update)
- [Doctor](#doctor)
- [What is shared and what is yours](#what-is-shared-and-what-is-yours)
- [Using it](#using-it)
- [Examples](docs/examples.md): worked use cases, from a small fix to a T2 migration
- [Gates](#gates)
- [Known limits](#known-limits)

## Install

Each developer machine needs Python 3, Copilot CLI, and the `gh` CLI logged in (`gh auth login`).

```bash
git clone https://github.com/kiranm516/devgent.git
cd /path/to/service-repo && git switch -c install-devgent
/path/to/devgent/devgent install .
```

The installer copies the kit, runs the setup check, and prints a numbered list of what is left to
do in this repository. Work through that list; `./devgent doctor` re-checks it at any time.
Typically it is:

1. **Conventions**: fill in the TODOs in `.github/copilot-instructions.md`.
2. **Path rules**: adjust `tier_rules` and `slice.production_paths` in `.github/devgent.yml` to the repository's layout.
3. **Owners**: replace the placeholders in `.github/CODEOWNERS` and uncomment the rules.
4. **Merge**: commit, open a pull request and merge it. The `gates` check fails on this first PR because it carries no ticket folder, so merge it before step 5.
5. **Branch protection** on the default branch (repository admin): require a pull request with one approving review, dismiss stale approvals on new commits, require the `gates` status check, block force pushes.

The installer never destroys what is already there. An existing `AGENTS.md` or `CODEOWNERS` (in any of GitHub's three locations) is kept, and it tells you what to merge in. If one of the kit's own paths already holds a different file, it stops and lists the files; `--force` lets the kit replace them.

## Update

From the service repository, no kit clone needed:

```bash
git switch -c update-devgent
./devgent update
```

It fetches the latest kit from where it was installed, replaces the shared files, never overwrites
yours, and runs the setup check again. `./devgent doctor` tells you when a newer version exists.

- If a shared file was edited in the service repository, `update` stops and lists it. Move the change into the kit, or pass `--force` to discard it.
- `./devgent update --source /path/to/devgent` installs from a local clone instead, for trying unreleased kit changes.
- An update changes the workflow's own gates, which the path rules class as T2. Once the `gates` check is required, the update PR needs a T2 ticket folder like any other change.

## Doctor

```bash
./devgent doctor
```

It checks, and gives a fix for each problem:

| Area | What it checks |
| --- | --- |
| Kit | installed version, shared files missing or edited locally, newer version available |
| Configuration | settings file loads, conventions still have TODOs, CODEOWNERS has real owners |
| Local hook | runs the registered hook with a test force push and expects it to be blocked |
| Developer machine | Python version, Copilot CLI, `gh` installed and logged in |
| GitHub | default branch requires a PR with an approval, dismisses stale approvals, requires `gates`, blocks force pushes |
| Current ticket | the branch's ticket folder is well formed |

It exits non-zero when there is a problem, so it can run in a setup script. `--offline` skips the checks that need the network.

## What is shared and what is yours

| | Files | On update |
| --- | --- | --- |
| **Shared** (the kit) | `devgent`, `devgent.cmd`, `.github/hooks/devgent.json`, `.github/hooks/devgent/`, the four agents in `.github/agents/`, the four skills in `.github/skills/`, `.github/workflows/agent-gates.yml`, `docs/agent-work/_templates/`, `AGENTS.md` | Replaced. Do not edit them in a service repository; change them here. |
| **Yours** (per repository) | `.github/devgent.yml` (slice cap, WIP limits, tier path rules), `.github/copilot-instructions.md` (conventions), `.github/CODEOWNERS` | Created on first install, then never touched |
| **Yours** (per ticket) | `docs/agent-work/<ticket-id>/` | Never touched |

Your own agents, skills and workflows next to the kit's are left alone. The kit's four skill folders and `docs/agent-work/_templates/` belong to it whole: if you add a file inside one, install and update stop and list it instead of deleting it.

## Using it

Everything goes through `./devgent` (`devgent` on Windows). When in doubt, run `./devgent status`:
it shows where the ticket stands and the next command to run. [docs/examples.md](docs/examples.md)
walks through eight use cases with a complete sample plan.

Two humans and four agents take part. The **driver** runs Copilot CLI and hands work to the agents
by naming one in the prompt ("Use the scout agent to triage ABC-123"). The **approver** is a second
person who answers the plan's decisions and approves on GitHub.

A ticket in commands, for a T1 feature:

```bash
./devgent start ABC-123 --tier T1        # driver: branch and ticket folder
./devgent check                          # driver: what the plan still lacks
gh pr create --draft --fill              # driver: put the plan in front of the approver
./devgent view --open                    # approver: decisions, trace and what needs an answer
./devgent answer D-01 A                  # approver: then commit, push, approve the PR
./devgent slice start S-01               # driver: then the implementer builds it
./devgent slice review S-01              # implementer: slice is committed
./devgent slice accept S-01              # driver: after reading the slice
./devgent state IN_REVIEW                # driver: all slices accepted, run the reviewer
gh pr ready                              # driver: the approver reviews the diff
```

| Step | Who | Command or action |
| --- | --- | --- |
| 1. Triage | scout, driver | Ask the `scout` for a tier. Then `./devgent start ABC-123 --tier T1` creates the branch and the ticket folder. |
| 2. Plan | architect, reviewer | The `architect` fills `plan.md`; the `reviewer` records up to five objections. `./devgent check` lists what is still missing. |
| 3. Approve the plan | approver | Commit and open a draft PR. The approver answers every decision (`./devgent view` shows them with the options and the recommendation; `./devgent answer D-01 A` writes one), gives every objection a disposition, then submits a GitHub approving review. The approver is not the driver. |
| 4. Implement | implementer, driver | Per slice: `./devgent slice start S-01`, the `implementer` builds and commits it with a `Slice: S-01` trailer, `./devgent slice review S-01`, you look at it, `./devgent slice accept S-01`. |
| 5. Review | reviewer, approver | `./devgent state IN_REVIEW`. The `reviewer` writes findings, the commit it reviewed and a one-screen brief (at most two fix rounds). Findings marked `fix` go back to the implementer; a human decides the ones marked `decide`. Mark the PR ready; a human approves the diff; merge follows the normal process. |

| Command | What it does |
| --- | --- |
| `./devgent status` | Ticket, state, tier, plan approval, slice status, and the next step |
| `./devgent start <id> --tier T0\|T1\|T2` | Creates the branch (named after the ticket) and the ticket folder from the templates |
| `./devgent tier` | Tier floor the path rules set for the files changed on this branch |
| `./devgent slice start\|review\|accept S-01` | Moves a slice forward. Refuses a second slice while one is in progress or awaiting review. |
| `./devgent state <STATE> [reason]` | Sets the state on line 1 of `log.md` and logs the change. `BLOCKED` and `ABANDONED` need a reason. |
| `./devgent check` | Runs the CI gates locally |
| `./devgent view [--open]` | Writes one HTML page for the ticket and prints its path: what is waiting for a human, the decisions, the plan's diagrams, the requirement-to-test trace, slices against the change map, and the review findings |
| `./devgent answer D-01 A [note]` | Writes the approver's answer on that decision's `- Answer:` line in `plan.md` |
| `./devgent override <gate> <reason>` | Records a human override in `log.md` |

- The branch name must contain the ticket id (or set `DEVGENT_TICKET`).
- Any change to `plan.md` cancels its approval, answers included: commit the answers first, then approve.
- A T0 ticket carries only a short task note in `plan.md`. The driver approves it and normal PR review covers the diff.
- Agents may run `status`, `check`, `tier`, `view`, `slice start` and `slice review`. The hook blocks them from `override`, `answer`, `slice accept`, `update` and `install`.
- Markdown is the record; the page from `view` is generated from it, lives under `.git/devgent/view/` and is never committed. Agents draw Mermaid diagrams in `plan.md` only when a trigger in the template applies. The page loads Mermaid from a CDN to draw them and shows their source when offline.
- A decision is a `### D-01: question` block in `plan.md` with lettered options, a recommendation and its rationale. CI needs at least two options, a recommendation and the answer.

## Gates

| Rule | Local hook | CI |
| --- | --- | --- |
| Source edits need a ticket | `ticket` | `ticket`: the PR adds exactly one ticket folder with its `plan.md`; an old ticket's folder cannot be reused |
| No source edits before plan approval (T1, T2) | `plan-approval`: asks GitHub for an approving review on a commit whose `plan.md` matches the current one | Branch protection |
| Tier floor from path rules; a tier is never lowered without a human override | `tier-floor`, per file written | `tier-floor`, on the final diff, including deleted files |
| Writes stay inside the one active slice | `slice-paths` | `slice-paths`, per commit, from the `Slice:` trailer |
| Slice size cap | | `slice-size`: net changed production lines per slice, across its commits |
| Decisions carry options and a recommendation and are answered, objections dispositioned, plan complete | | `structure` |
| The diff was reviewed, and the code has not changed since (T1, T2) | | `review`: `review.md` names the reviewed commit and carries a review brief. Checked once the PR is no longer a draft or the state is `IN_REVIEW`; skipped before that. |
| Force push, push to the default branch, merge, approve, deploy, publish, production hosts, credential files | `dangerous-action` | Branch protection |
| Role limits per agent | `role`, when the CLI reports the agent | |
| Audit trail | `.git/devgent/audit.jsonl`: ticket, plan version, role, tool, changed files; never prompts, commands or file content | |

### Overrides

Only a human sets an override. Each names one gate and a reason, and is reviewed in the retrospective.

- **CI**: `./devgent override tier-floor "comment-only migration"`, then commit `log.md`. The gate reports `overridden` instead of failing. This is also how a human lowers a tier below the path floor.
- **Local hook**: start the CLI with `DEVGENT_OVERRIDE="plan-approval: approver on leave, agreed in standup" copilot`. The hook lets that gate pass and appends the override to `log.md`.

`dangerous-action` and `role` cannot be overridden. Agents are blocked from writing override or slice-accepted entries through their edit tools, `./devgent`, and the obvious shell forms. A determined shell command can still append one, so read `log.md` in the PR diff: every override is visible there.

## Known limits

- **The local hook is a guard rail.** It can be disabled, and it cannot see what a shell command writes, so `sed -i` or a redirect bypasses its path checks. CI and branch protection catch the result.
- **Hook payload.** GitHub does not document the argument names of the CLI's file tools. The hook accepts the common ones and refuses a write whose target it cannot identify. Confirm on your CLI version.
- **Timeouts.** A hook timeout lets the tool call through; a hook crash blocks it.
- **CI judges a PR by the base branch's settings**, so a PR cannot relax the rules it is checked against; a settings change takes effect after it merges. The gate code and workflow file still come from the PR, which is why they are T2 paths with code owners.
- **Approval cache.** A plan approval is cached locally for 10 minutes, so a withdrawn approval can take that long to register in the hook.
- **Role and model.** The CLI reports the agent only for subagents and never the model, so role limits apply only then, and the audit log's `model` is empty unless `COPILOT_MODEL` is set.
- **Per-agent models.** The agent files leave `model` as a comment; whether the CLI honours it for subagents is unconfirmed.
- **Not machine-checked:** one plan awaiting approval per driver, the two-fix-round cap, and breaking API changes (pass `--breaking-api-change` to `devgent check` in the workflow file, from your own detector).
- **Not built:** dynamic workflows, telemetry export, metrics reporting, calibration exercises.

## Working on the kit

```bash
PYTHONPATH=.github/hooks python3 -m unittest discover -s .github/hooks/tests
./devgent update --source /path/to/devgent     # from a service repository, to try a change before pushing it
```

The tests stay in this repository; they are not installed into service repositories.
