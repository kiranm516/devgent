"""Run from the repository root:

    PYTHONPATH=.github/hooks python3 -m unittest discover -s .github/hooks/tests -v
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from devgent import ci, config, doctor, flow, hook, install, ticket as tk, view

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

PLAN = """---
ticket: ABC-1
tier: {tier}
plan_version: 1
base_commit: abc123
---
# Plan: ABC-1

## Functional Scenarios

### Problem and outcome
Reject orders without a customer id.

### Requirements
- R-01: A request without a customer id is rejected with 400.
- R-02: A valid request is stored.

### Scenarios
| ID | Scenario | Covers | Expected result |
| --- | --- | --- | --- |
| SC-01 | Missing customer id | R-01 | 400 |
| SC-02 | Valid order | R-02 | 201 |

## High-Level Design

### Current flow
Orders are stored unchecked (`OrderService.java:12`).

### Proposed flow
The service validates first.

## Low-Level Design

### Change map
| File | Change | Reason |
| --- | --- | --- |
| src/main/java/app/OrderService.java | validate | R-01 |

### Test matrix
| Scenario | Level | Test |
| --- | --- | --- |
| SC-01 | Unit | OrderServiceTest.rejectsMissingCustomer |
| SC-02 | Integration | OrderFlowIT.storesOrder |

### Slices
#### S-01: validation
- Covers: R-01, SC-01
- Paths: `src/main/java/app/OrderService.java`, `src/test/java/app/**`
- Exit check: `./gradlew test` passes

#### S-02: storage
- Covers: R-02
- Paths:
  - `src/main/java/app/OrderRepository.java`

## Decision list
### D-01: Reject or default?
- Layer: functional
- Why a human: changes what clients see
- A: Reject with 400.
- B: Default to the guest customer,
  which hides bad clients.
- Recommendation: A, because no caller omits the id today
- Reversible: yes
- Answer: {answer}
"""

# The layout of plans written before the three layers: still valid, because an approved plan is frozen.
FLAT_PLAN = """---
ticket: ABC-1
tier: {tier}
plan_version: 1
base_commit: abc123
---
# Plan: ABC-1

## Problem and outcome
Reject orders without a customer id.

## Requirements
- R-01: A request without a customer id is rejected with 400.
- R-02: A valid request is stored.

## Scenarios and API contract
| ID | Scenario | Covers | Expected result |
| --- | --- | --- | --- |
| SC-01 | Missing customer id | R-01 | 400 |
| SC-02 | Valid order | R-02 | 201 |

## Design
Current flow, proposed flow.

### Change map
| File | Change | Reason |
| --- | --- | --- |
| src/main/java/app/OrderService.java | validate | R-01 |

## Test matrix
| Scenario | Level | Test |
| --- | --- | --- |
| SC-01 | Unit | OrderServiceTest.rejectsMissingCustomer |
| SC-02 | Integration | OrderFlowIT.storesOrder |

## Slices
### S-01: validation
- Covers: R-01, SC-01
- Paths: `src/main/java/app/OrderService.java`, `src/test/java/app/**`
- Exit check: `./gradlew test` passes

### S-02: storage
- Covers: R-02
- Paths:
  - `src/main/java/app/OrderRepository.java`

## Decision list
### D-01: Reject or default?
- Why a human: changes what clients see
- A: Reject with 400.
- B: Default to the guest customer,
  which hides bad clients.
- Recommendation: A, because no caller omits the id today
- Reversible: yes
- Answer: {answer}
"""

REVIEW = """# Review: ABC-1
## Plan objections
| ID | Layer | Objection | Disposition | Reason or owner |
| --- | --- | --- | --- | --- |
| O-01 | functional | No timeout scenario | {disposition} | {note} |
"""


def plan(tier="T1", answer="A"):
    return PLAN.format(tier=tier, answer=answer)


def flat_plan(tier="T1", answer="A"):
    return FLAT_PLAN.format(tier=tier, answer=answer)


def review(disposition="accepted", note=""):
    return REVIEW.format(disposition=disposition, note=note)


class ConfigTest(unittest.TestCase):
    def test_parses_the_real_settings_file(self):
        settings = config.load_settings(REPO)
        self.assertEqual(settings.slice_cap, 100)
        self.assertEqual(settings.artifact_root, "docs/agent-work")
        self.assertEqual(settings.production_hosts, [])
        self.assertTrue(any(r.get("new_files_only") for r in settings.tier_rules))

    def test_worktrees_setting(self):
        with open(os.path.join(REPO, config.SETTINGS_PATH)) as fh:
            good = fh.read()
        self.assertIn("worktrees:\n  dir:\n", good)
        self.assertIsNone(config.load_settings(REPO, good).worktrees_dir)
        self.assertIsNone(config.load_settings(REPO, good.replace("worktrees:\n  dir:\n", "")).worktrees_dir)
        self.assertEqual(config.load_settings(REPO, good.replace("  dir:\n", "  dir: ../tickets\n")).worktrees_dir,
                         "../tickets")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_settings(REPO, good.replace("worktrees:\n  dir:\n", "worktrees: 3\n"))
        self.assertIn("worktrees", str(ctx.exception))

    def test_yaml_subset(self):
        data = config.parse_yaml(
            "a: 1 # note\nb:\n  c: \"x # y\"\n  d: [p, 'q']\nitems:\n- name: n1\n  on: true\n- name: n2\n"
        )
        self.assertEqual(data, {"a": 1, "b": {"c": "x # y", "d": ["p", "q"]},
                                "items": [{"name": "n1", "on": True}, {"name": "n2"}]})
        with self.assertRaises(config.ConfigError):
            config.parse_yaml("a: &anchor 1")
        with self.assertRaises(config.ConfigError):
            config.parse_yaml("a: 1\n   b: 2")

    def test_settings_mistakes_are_errors_not_silent(self):
        with open(os.path.join(REPO, config.SETTINGS_PATH)) as fh:
            good = fh.read()
        for bad in (
            good.replace('    production_paths:\n    - "**/src/main/**"', '    production_paths: "**/src/main/**"')
            if '    production_paths:\n    - ' in good else good.replace('  production_paths:\n    - "**/src/main/**"',
                                                                            '  production_paths: "**/src/main/**"'),
            good.replace("new_files_only: true", "new_files_only: no"),
            good.replace("tier_rules:", "tier_rules:\n  - just-a-string", 1),
            good.replace('    patterns:\n      - "**/security/**"', '    patterns: "**/security/**"\n    x:\n      - "**/security/**"'),
            "- a\n- b\n",
        ):
            self.assertNotEqual(bad, good)
            with self.assertRaises(config.ConfigError):
                config.load_settings(REPO, bad)

    def test_slice_paths_parsing(self):
        text = ("---\ntier: T1\n---\n## Slices\n### S-01: x\n  - Covers: R-01\n  - Paths: `src/main/java/app/A.java`\n"
                "  - Tests: `**/*Test.java`\n  - Exit check: `./gradlew test`\n### S-02: y\n- Paths: `src/test/java/app/`\n")
        slices = tk.Plan(text).slices
        self.assertEqual(slices["S-01"], ["src/main/java/app/A.java"])
        self.assertTrue(config.matches("src/test/java/app/deep/XTest.java", slices["S-02"]))

    def test_globs(self):
        m = config.matches
        self.assertTrue(m("src/main/resources/db/migration/V2__x.sql", ["**/resources/db/migration/**"]))
        self.assertTrue(m("Dockerfile", ["**/Dockerfile"]))
        self.assertTrue(m("svc/Dockerfile", ["**/Dockerfile"]))
        self.assertTrue(m("src/main/java/a/WebSecurityConfig.java", ["**/*Security*Config*.java"]))
        self.assertFalse(m("src/main/java/a/Order.java", ["**/*Controller.java"]))
        self.assertFalse(m("src/main/java/a/b/Order.java", ["src/main/java/*/Order.java"]))
        self.assertTrue(m("settings.gradle.kts", ["settings.gradle*"]))

    def test_tier_floor(self):
        s = config.load_settings(REPO)
        floor = lambda *c, **k: s.tier_floor(list(c), **k)[0]
        self.assertEqual(floor(("src/main/java/app/OrderService.java", False)), "T0")
        self.assertEqual(floor(("src/main/java/app/OrderController.java", False)), "T1")
        self.assertEqual(floor(("src/main/java/app/OrderController.java", False), breaking_api_change=True), "T2")
        self.assertEqual(floor(("build.gradle", False)), "T1")
        self.assertEqual(floor(("src/main/java/app/client/Pay.java", False)), "T0")
        self.assertEqual(floor(("src/main/java/app/client/Pay.java", True)), "T2")
        self.assertEqual(floor(("src/main/resources/db/migration/V2__a.sql", True)), "T2")
        self.assertEqual(floor((".github/hooks/devgent/hook.py", False)), "T2")
        self.assertEqual(floor(("CODEOWNERS", False)), "T2")
        # a ticket's own artifacts never raise the tier
        self.assertEqual(floor(("docs/agent-work/ABC-1/plan.md", True)), "T0")


class StructureTest(unittest.TestCase):
    def setUp(self):
        self.settings = config.load_settings(REPO)

    def test_complete_plan_passes(self):
        p = tk.Plan(plan())
        self.assertEqual(tk.check_front_matter(p, "ABC-1"), [])
        self.assertEqual(tk.check_plan_structure(p), [])
        self.assertEqual(p.slices["S-01"], ["src/main/java/app/OrderService.java", "src/test/java/app/**"])
        self.assertEqual(p.slices["S-02"], ["src/main/java/app/OrderRepository.java"])

    def test_unanswered_decision_fails(self):
        for answer in ("", "TBD"):
            errors = tk.check_plan_structure(tk.Plan(plan(answer=answer)))
            self.assertEqual(errors, ["Decision D-01 has no approver's answer"])

    def test_decision_blocks(self):
        d = tk.Plan(plan()).decisions[0]
        self.assertEqual((d["id"], d["title"], d["answer"]), ("D-01", "Reject or default?", "A"))
        self.assertEqual(d["options"], [["A", "Reject with 400."],
                                        ["B", "Default to the guest customer, which hides bad clients."]])
        text = plan().replace("- B: Default to the guest customer,\n  which hides bad clients.\n", "")
        text = text.replace("- Recommendation: A, because no caller omits the id today\n", "")
        self.assertEqual(tk.check_plan_structure(tk.Plan(text)), [
            "Decision D-01 needs at least two options (`- A: ...`, `- B: ...`)",
            "Decision D-01 has no recommendation and rationale"])
        table = plan().split("### D-01")[0] + "| ID | Decision |\n| --- | --- |\n| D-01 | Reject? |\n"
        self.assertIn("not a table row", tk.check_plan_structure(tk.Plan(table))[0])

    def test_untested_requirement_and_missing_change_map(self):
        untested = plan().replace("| SC-02 | Integration | OrderFlowIT.storesOrder |\n", "")
        errors = tk.check_plan_structure(tk.Plan(untested))
        self.assertEqual(errors, ["Test matrix: scenario SC-02 has no test level and named test",
                                  "Requirement R-02 maps to no test"])
        self.assertEqual(tk.check_plan_structure(tk.Plan(plan().replace("### Change map", "### Notes"))),
                         ["Low-Level Design: `### Change map` is missing or empty"])

    def test_blocking_question_and_t2_alternatives(self):
        text = plan("T2") + (
            "\n## Open questions\n| ID | Question | Layer | Owner | Blocking | Answer source | Status |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n| Q-01 | Idempotent? | {layer} | pay team | yes | pay team | {s} |\n"
        )
        errors = tk.check_plan_structure(tk.Plan(text.format(s="open", layer="High level")))
        self.assertEqual(errors, ["Open question Q-01 is blocking and unresolved",
                                  "T2 plan needs a non-empty `### Alternatives` section in the High-Level Design"])
        ok = text.replace("## Low-Level Design", "### Alternatives\nPolling; lost on latency.\n\n## Low-Level Design")
        self.assertEqual(tk.check_plan_structure(tk.Plan(ok.format(s="risk-accepted", layer="high-level"))), [])
        self.assertEqual(tk.check_plan_structure(tk.Plan(ok.format(s="answered", layer="the pay team"))),
                         ["Open question Q-01 needs its layer in a `Layer` column (functional, high-level or low-level)"])
        # T2 shows the current flow as well as the proposed one
        no_current = ok.format(s="answered", layer="low-level").replace("### Current flow", "### Background")
        self.assertEqual(tk.check_plan_structure(tk.Plan(no_current)),
                         ["High-Level Design: a T2 plan needs a non-empty `### Current flow`"])

    def test_each_layer_is_required_and_holds_its_own_level(self):
        check = lambda text: tk.check_plan_structure(tk.Plan(text))
        p = tk.Plan(plan())
        self.assertTrue(p.layered)
        self.assertEqual(sorted(p.layers), ["functional", "high-level", "low-level"])
        self.assertEqual(p.slice_covers, {"S-01": {"R-01", "SC-01"}, "S-02": {"R-02"}})
        self.assertEqual(check(plan().replace("## High-Level Design", "## Notes")),
                         ["plan.md is missing a non-empty `## High-Level Design` layer"])
        # headings alone are not a design, and the functional parts live in the functional layer
        self.assertEqual(check(plan().replace("The service validates first.\n", "")),
                         ["High-Level Design: `### Proposed flow` is missing or empty"])
        moved = plan().replace("### Requirements", "### Rules").replace("### Current flow", "### Requirements")
        self.assertEqual(check(moved), ["Functional Scenarios: `### Requirements` is missing or empty"])
        # the functional layer describes behaviour: a file reference belongs to the design
        leak = plan().replace("| Missing customer id |", "| `OrderController.java:40` gets no id, see src/main/java/app |")
        self.assertEqual(check(leak), [
            "Functional Scenarios: names implementation files (`OrderController.java:40`, `src/main/java/app`); "
            "describe observable behaviour here and keep files and classes in the design layers"])
        self.assertEqual(check(plan().replace("rejected with 400", "rejected with 400 as `application/problem+json`")), [])
        # every scenario reaches a slice, by its own id or through a requirement it covers
        self.assertEqual(check(plan().replace("- Covers: R-02\n", "- Covers: nothing yet\n")),
                         ["Scenario SC-02 reaches no slice: name it, or a requirement it covers, in a slice's `- Covers:`"])
        # slices sit one level below `### Slices`
        self.assertEqual(check(plan().replace("#### S-0", "### S-0")),
                         ["Low-Level Design: `### Slices` is missing or empty"])
        # a decision says which layer it belongs to, so the driver knows who can clarify it
        for layer in ("", "business"):
            self.assertEqual(check(plan().replace("- Layer: functional", "- Layer: " + layer)),
                             ["Decision D-01 needs its layer (`- Layer: functional, high-level or low-level`)"])
        self.assertEqual(check(plan().replace("- Layer: functional", "- Layer: **Low level**")), [])

    def test_a_plan_in_the_flat_layout_stays_valid(self):
        p = tk.Plan(flat_plan())
        self.assertFalse(p.layered)
        self.assertEqual(tk.check_plan_structure(p), [])
        self.assertEqual(p.slices["S-02"], ["src/main/java/app/OrderRepository.java"])
        text = flat_plan().replace("| SC-02 | Integration | OrderFlowIT.storesOrder |\n", "")
        errors = tk.check_plan_structure(tk.Plan(text.replace("### Change map", "### Notes")))
        self.assertIn("Requirement R-02 maps to no test", errors)
        self.assertIn("Design: `### Change map` is missing or empty", errors)
        self.assertEqual(tk.check_plan_structure(tk.Plan(flat_plan("T2"))),
                         ["T2 plan needs a non-empty `## Alternatives` section"])
        self.assertEqual(tk.check_plan_structure(tk.Plan(flat_plan().replace("## Design", "## Sketch"))),
                         ["plan.md is missing a non-empty `## Design` section"])

    def test_templates_are_not_ready_for_approval(self):
        for name, tier in (("plan.md", "T1"), ("task-note.md", "T0")):
            with open(os.path.join(REPO, "docs/agent-work/_templates", name)) as fh:
                p = tk.Plan(fh.read().replace("{{TIER}}", tier))
            self.assertTrue(tk.check_plan_structure(p), name)

    def test_the_documented_example_plan_passes_once_answered(self):
        with open(os.path.join(REPO, "docs/examples.md")) as fh:
            text = re.search(r"````markdown\n(---\nticket: ORD-142.*?)````", fh.read(), flags=re.S).group(1)
        p = tk.Plan(text)
        self.assertEqual(tk.check_front_matter(p, "ORD-142"), [])
        self.assertEqual(tk.check_plan_structure(p), ["Decision D-01 has no approver's answer"])
        self.assertEqual(tk.check_plan_structure(tk.Plan(text.replace("- Answer:", "- Answer: A"))), [])

    def test_task_note(self):
        note = "---\ntier: T0\n---\n## Goal\nfix\n## Files\n`A.java`\n## Checks\n`./gradlew test`\n"
        self.assertEqual(tk.check_plan_structure(tk.Plan(note)), [])

    def test_review_dispositions(self):
        check = lambda text: tk.check_review_structure(text, self.settings)
        self.assertEqual(check(review()), [])
        self.assertEqual(check(review("deferred", "Priya")), [])
        self.assertTrue(check(None))
        self.assertTrue(check(review("")))
        self.assertTrue(check(review("rejected")))
        self.assertTrue(check(review("deferred")))
        rows = "".join("| O-0%d | low-level | x | accepted | |\n" % i for i in range(2, 7))
        self.assertIn("limit is 5", check(review() + rows)[0])
        # an objection names the layer it is about; a review written before the column existed has none
        self.assertEqual(check(review().replace("| functional |", "| design |")),
                         ["Objection O-01 needs its layer: functional, high-level or low-level"])
        old = "## Plan objections\n| ID | Objection | Disposition | Reason or owner |\n| --- | --- | --- | --- |\n"
        self.assertEqual(check(old + "| O-01 | No timeout scenario | accepted | |\n"), [])

    def test_log(self):
        with open(os.path.join(REPO, "docs/agent-work/_templates/log.md")) as fh:
            template = fh.read()
        log = tk.Log(template)
        self.assertEqual(log.state, "TRIAGED")
        self.assertEqual((log.slice_status, log.overrides), ({}, {}))  # examples are comments
        log = tk.Log(
            "state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n- 2026-11-13 slice S-01 awaiting-review\n"
            "- 2026-11-14 slice S-01 accepted\n- 2026-11-14 slice S-02 started\n"
            "- 2026-11-14 override slice-size by Sam Lee: generated mapper\n"
        )
        self.assertEqual(log.active_slices(), ["S-02"])
        self.assertEqual(log.overrides, {"slice-size": "Sam Lee: generated mapper"})


class ShellTest(unittest.TestCase):
    def check(self, command, hosts=()):
        settings = config.load_settings(REPO)
        settings.production_hosts = list(hosts)
        try:
            hook.check_shell(command, settings)
            return True
        except hook.Deny:
            return False

    def test_blocked(self):
        for command in (
            "git push --force origin feature", "git push -f", "git push origin +feature",
            "git push --force-with-lease", "git push origin main", "git push origin HEAD:master",
            "gh pr merge 12 --squash", "gh pr review 12 --approve", "gh api repos/o/r/pulls/1/reviews -f event=APPROVE",
            "cd x && kubectl apply -f k8s/", "helm upgrade app ./chart", "terraform apply",
            "gh workflow run deploy.yml", "./gradlew publish", "gh auth token", "gh secret list",
            "cat .env", "cat ~/.ssh/id_ed25519", "cat config/.env.production", "cat ~/.aws/credentials",
            "git push origin HEAD:refs/heads/main", "git push --mirror", "git push --delete origin x",
            "git push origin :old-branch", "gh api graphql -f query='mutation { mergePullRequest(input:{}) }'",
            "type $HOME\\.aws\\credentials", "./gradlew :app:publish", "mvn deploy", "./gradlew release",
            "git commit -m \"$(git push -f)\"",
        ):
            self.assertFalse(self.check(command), command)
        self.assertFalse(self.check("curl https://api.prod.example.com/x", ["api.prod.example.com"]))

    def test_allowed(self):
        for command in (
            "git push origin ABC-1-orders", "git push -u origin feature/main-fix", "git status",
            "./gradlew test --tests '*OrderServiceTest'", "gh pr view --json number,reviews",
            "gh pr create --draft --fill", "git commit -m 'S-01: validation'", "ls src/main/environment",
            "kubectl get pods", "curl http://localhost:8080/orders",
            "./gradlew test --tests 'com.acme.orders.publisher.*'", "./gradlew :publisher-service:test",
            "./gradlew build -Pprofile=release", "./gradlew publishToMavenLocal", "mvn -pl deploy-tools test",
            "git push origin main-fix", "git commit -m 'fix: handle push to main gracefully'",
            "git commit -m 'docs: devgent update notes'", "cat .env.example", "gh api repos/o/r/pulls/1/reviews",
        ):
            self.assertTrue(self.check(command), command)


class RepoTest(unittest.TestCase):
    """End to end against a throwaway git repository."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(os.path.join(REPO, ".github"), os.path.join(self.root, ".github"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(REPO, "docs"), os.path.join(self.root, "docs"))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test Driver")
        self.git("config", "user.email", "driver@example.com")
        self.commit("baseline")
        self.git("checkout", "-q", "-b", "ABC-1-orders")
        self.settings = config.load_settings(self.root)
        for key in ("DEVGENT_TICKET", "DEVGENT_OVERRIDE", "DEVGENT_ROLE"):
            os.environ.pop(key, None)
        self.addCleanup(os.environ.pop, "DEVGENT_OVERRIDE", None)
        # `start` puts ticket worktrees next to the checkout by default
        self.worktrees = self.root + ".worktrees"
        self.addCleanup(shutil.rmtree, self.worktrees, True)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.root] + list(args), check=True,
                              stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def ticket(self, tier="T1", log="state: PLANNING\n", **kw):
        self.write("docs/agent-work/ABC-1/plan.md", plan(tier, **kw))
        self.write("docs/agent-work/ABC-1/log.md", log)
        self.write("docs/agent-work/ABC-1/review.md", review())
        return self.commit("ABC-1 plan")

    def pre(self, tool, args, approved=None, cwd=None, **extra):
        """Run the preToolUse hook; returns the deny reason or None."""
        original = tk.approving_reviews
        tk.approving_reviews = lambda root: (approved, None) if approved is not None else (None, "no PR")
        hook.check_write.__defaults__ = (
            lambda root, t: tk.plan_approval(root, t, fetch=tk.approving_reviews, cache_ttl=0),
            hook.check_write.__defaults__[1])
        try:
            payload = dict(toolName=tool, toolArgs=json.dumps(args), cwd=cwd or self.root, sessionId="s1", **extra)
            decision = hook.pre_tool_use(payload)
        finally:
            tk.approving_reviews = original
        return decision and decision["permissionDecisionReason"]

    SRC = "src/main/java/app/OrderService.java"

    # ---- hook -------------------------------------------------------------

    def test_source_edit_needs_a_ticket(self):
        self.git("checkout", "-q", "-b", "no-ticket")
        self.assertIn("[devgent:ticket]", self.pre("create", {"path": self.SRC}))
        # but starting a ticket's artifact folder is always allowed
        self.assertIsNone(self.pre("create", {"path": "docs/agent-work/XYZ-9/plan.md"}))

    def test_source_edit_blocked_until_plan_is_approved(self):
        plan_commit = self.ticket(log="state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n")
        self.assertIn("plan-approval", self.pre("edit", {"path": self.SRC}))
        self.assertIn("plan-approval", self.pre("edit", {"path": self.SRC}, approved=[]))
        self.assertIsNone(self.pre("edit", {"path": self.SRC}, approved=[plan_commit]))
        # absolute paths and patch-style tools resolve to the same file
        self.assertIsNone(self.pre("edit", {"path": os.path.join(self.root, self.SRC)}, approved=[plan_commit]))
        patch = "*** Begin Patch\n*** Update File: src/main/java/app/Other.java\n@@\n-a\n+b\n*** End Patch"
        self.assertIn("slice-paths", self.pre("apply_patch", {"input": patch}, approved=[plan_commit]))

    def test_any_plan_change_cancels_the_approval(self):
        plan_commit = self.ticket(log="state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n")
        self.write("docs/agent-work/ABC-1/plan.md", plan() + "\nextra\n")
        self.assertIn("uncommitted", self.pre("edit", {"path": self.SRC}, approved=[plan_commit]))
        self.commit("plan v2")
        self.assertIn("no approving review covers", self.pre("edit", {"path": self.SRC}, approved=[plan_commit]))
        # a later commit that leaves plan.md alone keeps the approval
        self.write("docs/agent-work/ABC-1/plan.md", plan())
        self.commit("revert plan")
        self.assertIsNone(self.pre("edit", {"path": self.SRC}, approved=[plan_commit]))

    def test_writes_stay_inside_the_one_active_slice(self):
        c = self.ticket(log="state: IMPLEMENTING\n")
        self.assertIn("exactly one slice", self.pre("edit", {"path": self.SRC}, approved=[c]))
        self.write("docs/agent-work/ABC-1/log.md", "state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n")
        self.assertIsNone(self.pre("create", {"path": "src/test/java/app/OrderServiceTest.java"}, approved=[c]))
        self.assertIn("outside the paths of active slice S-01",
                      self.pre("edit", {"path": "src/main/java/app/OrderRepository.java"}, approved=[c]))
        self.write("docs/agent-work/ABC-1/log.md",
                   "state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n- 2026-11-13 slice S-01 awaiting-review\n")
        self.assertIn("S-01 is awaiting review", self.pre("edit", {"path": self.SRC}, approved=[c]))

    def test_tier_floor_in_the_hook(self):
        c = self.ticket(log="state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n")
        reason = self.pre("create", {"path": "src/main/resources/db/migration/V2__a.sql"}, approved=[c])
        self.assertIn("tier-floor", reason)

    def test_t0_ticket_in_the_hook(self):
        self.ticket(tier="T0")
        self.assertIsNone(self.pre("edit", {"path": self.SRC}))
        self.assertIn("tier-floor", self.pre("edit", {"path": "build.gradle"}))
        self.assertIn("inside .git", self.pre("create", {"path": ".git/hooks/pre-commit"}))

    def test_lowering_the_tier_needs_a_human(self):
        c = self.ticket(log="state: IMPLEMENTING\n")
        self.write("docs/agent-work/ABC-1/plan.md", plan("T0"))  # agent edit, not even committed
        reason = self.pre("edit", {"path": self.SRC}, approved=[c])
        self.assertIn("declared T1 earlier and now says T0", reason)
        self.commit("lower")
        self.assertIn("declared T1 earlier", self.pre("edit", {"path": self.SRC}))
        rows, failed = self.result()
        self.assertEqual(rows["tier-floor"][0], ci.FAIL)
        self.assertIn("only a human may lower a tier", " ".join(rows["tier-floor"][1]))
        flow.override(self.root, "tier-floor", "scope cut to a typo fix")
        self.assertIsNone(self.pre("edit", {"path": self.SRC}))
        self.commit("override")
        self.assertEqual(self.result()[0]["tier-floor"][0], ci.OVERRIDDEN)

    def test_override_is_allowed_and_logged(self):
        self.ticket(log="state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n")
        os.environ["DEVGENT_OVERRIDE"] = "plan-approval: approver on leave, agreed in standup"
        self.assertIsNone(self.pre("edit", {"path": self.SRC}))
        self.assertIsNone(self.pre("edit", {"path": self.SRC}))
        log = tk.Ticket(self.root, self.settings, "ABC-1").read("log.md")
        self.assertEqual(log.count("override plan-approval by Test Driver: approver on leave"), 1)
        # the override names one gate only; the others still apply
        self.assertIn("slice-paths", self.pre("edit", {"path": "src/main/java/app/Other.java"}))
        self.assertIn("tier-floor", self.pre("create", {"path": "k8s/deploy.yml"}))
        os.environ["DEVGENT_OVERRIDE"] = "all: x"
        self.assertIn("dangerous-action", self.pre("bash", {"command": "gh pr merge 3"}))

    def test_agents_cannot_write_override_lines_or_outside_the_repo(self):
        self.ticket()
        args = {"path": "docs/agent-work/ABC-1/log.md", "new_str": "- 2026-11-13 override tier-floor by Sam: fine"}
        self.assertIn("written by a human", self.pre("edit", args))
        for text in ("- 2026-11-13 slice S-01 accepted", "- 2026-11-13 Xoverride slice-size by Bob: fine",
                     "override", "accepted"):  # including the halves of a two-step edit
            self.assertIn("written by a human", self.pre("edit", dict(args, new_str=text)), text)
        for command in ("echo '- 2026-11-13 override tier-floor by Bob: ok' >> docs/agent-work/ABC-1/log.md",
                        "printf -- '- 2026-11-13 slice S-01 accepted\\n' >> docs/agent-work/ABC-1/log.md"):
            self.assertIn("human-only log entry", self.pre("bash", {"command": command}))
        # a diff quoted in a document is not a write to the files it names
        note = {"path": "docs/agent-work/ABC-1/review.md", "content": "+++ b/src/main/java/app/Z.java\n+x"}
        self.assertIsNone(self.pre("create", note))
        self.assertIn("credential", self.pre("grep", {"pattern": "KEY", "path": ".env"}))
        self.assertIsNone(self.pre("view", {"path": ".env.example"}))
        self.assertIsNone(self.pre("grep", {"pattern": "id_rsa", "path": "src"}))
        args["new_str"] = "- 2026-11-13 slice S-01 started"
        self.assertIsNone(self.pre("edit", args))
        self.assertIn("outside-repo", self.pre("create", {"path": "/opt/elsewhere/x.txt"}))
        self.assertIn("unknown-write", self.pre("edit", {"content": "x"}))
        self.assertIn("credential", self.pre("view", {"path": ".env"}))

    def test_roles(self):
        c = self.ticket(log="state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n")
        self.assertIn("role", self.pre("edit", {"path": self.SRC}, approved=[c], agentName="architect"))
        self.assertIsNone(self.pre("edit", {"path": "docs/agent-work/ABC-1/plan.md"}, agentName="architect"))
        self.assertIn("role", self.pre("edit", {"path": "docs/agent-work/ABC-1/plan.md"}, agentName="reviewer"))
        self.assertIsNone(self.pre("edit", {"path": "docs/agent-work/ABC-1/review.md"}, agentName="reviewer"))
        self.assertIn("role", self.pre("edit", {"path": "docs/agent-work/ABC-1/plan.md"}, agentName="implementer"))
        self.assertIsNone(self.pre("edit", {"path": self.SRC}, approved=[c], agentName="implementer"))

    def test_audit_log_has_no_content(self):
        self.ticket()
        self.pre("bash", {"command": "gh pr merge 3 --body SECRET"})
        hook.post_tool_use({"toolName": "edit", "cwd": self.root, "sessionId": "s1",
                            "toolArgs": {"path": self.SRC, "new_str": "SECRET"},
                            "toolResult": {"resultType": "success"}})
        with open(os.path.join(self.root, ".git/devgent/audit.jsonl")) as fh:
            text = fh.read()
        records = [json.loads(line) for line in text.splitlines()]
        self.assertNotIn("SECRET", text)
        self.assertEqual(records[0]["event"], "denied")
        self.assertEqual(records[1]["paths"], [self.SRC])
        self.assertEqual((records[1]["ticket"], records[1]["tier"], records[1]["plan_version"]), ("ABC-1", "T1", "1"))

    def test_hook_process_contract(self):
        self.ticket()
        env = dict(os.environ, PYTHONPATH=os.path.join(self.root, ".github/hooks"))
        run = lambda payload: subprocess.run(
            ["python3", "-m", "devgent", "hook", "pre"], input=json.dumps(payload), cwd=self.root, env=env,
            stdout=subprocess.PIPE, universal_newlines=True)
        denied = run({"toolName": "bash", "toolArgs": json.dumps({"command": "git push --force"}), "cwd": self.root})
        self.assertEqual(denied.returncode, 0)
        self.assertEqual(json.loads(denied.stdout)["permissionDecision"], "deny")
        allowed = run({"toolName": "bash", "toolArgs": {"command": "git status"}, "cwd": self.root})
        self.assertEqual((allowed.returncode, allowed.stdout), (0, ""))
        for bad in ([], "x", {"toolName": "edit", "toolArgs": "[1]", "cwd": self.root}):
            out = run(bad)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(json.loads(out.stdout)["permissionDecision"], "deny", bad)
        # the registered command works from a subdirectory too
        with open(os.path.join(self.root, ".github/hooks/devgent.json")) as fh:
            command = json.load(fh)["hooks"]["preToolUse"][0]["bash"]
        os.makedirs(os.path.join(self.root, "src"), exist_ok=True)
        out = subprocess.run(["bash", "-c", command], cwd=os.path.join(self.root, "src"), stdout=subprocess.PIPE,
                             input=json.dumps({"toolName": "bash", "toolArgs": {"command": "git push -f"},
                                               "cwd": self.root}), universal_newlines=True)
        self.assertEqual(json.loads(out.stdout)["permissionDecision"], "deny")

    # ---- CI ---------------------------------------------------------------

    def result(self, **kw):
        report = ci.run(self.root, "main", **kw)
        return {gate: (status, details) for gate, status, details in report.rows}, report.failed

    def slice_commit(self, rel, lines, trailer="S-01"):
        self.write(rel, "".join("line %d\n" % i for i in range(lines)))
        return self.commit("work\n\nSlice: %s" % trailer if trailer else "work")

    def test_ci_passes_a_well_formed_ticket(self):
        self.ticket()
        self.slice_commit(self.SRC, 100)
        self.slice_commit("src/test/java/app/OrderServiceTest.java", 400)
        rows, failed = self.result()
        self.assertFalse(failed, rows)
        self.assertEqual(rows.pop("review")[0], ci.SKIP)  # still PLANNING, and a draft
        self.assertEqual({s for s, _ in rows.values()}, {ci.PASS})

    def test_ci_judges_a_pr_by_the_base_branch_rules(self):
        self.ticket(tier="T0")
        self.write(".github/devgent.yml", self.read(".github/devgent.yml").split("tier_rules:")[0]
                   + "tier_rules:\n  - name: none\n    min_tier: T1\n    patterns: [\"nothing\"]\n")
        self.write("src/main/resources/db/migration/V2__x.sql", "alter table x;\n")
        self.commit("weaken the rules and add a migration")
        rows, failed = self.result()
        self.assertEqual(rows["tier-floor"][0], ci.FAIL)
        self.assertIn("needs at least T2", rows["tier-floor"][1][0])

    def test_ci_counts_deleted_and_non_ascii_files(self):
        self.git("checkout", "-q", "main")
        self.write("src/main/java/app/security/AuthFilter.java", "class AuthFilter {}\n")
        self.commit("existing filter")
        self.git("checkout", "-q", "-b", "ABC-1-cleanup")
        self.ticket(tier="T0")
        os.remove(os.path.join(self.root, "src/main/java/app/security/AuthFilter.java"))
        self.commit("remove filter")
        self.assertIn("needs at least T2", self.result()[0]["tier-floor"][1][0])
        self.git("checkout", "-q", "ABC-1-orders")
        self.ticket(tier="T0")
        self.write("src/main/java/app/\u00dcberController.java", "class X {}\n")
        self.commit("unicode")
        self.assertIn("needs at least T1", self.result()[0]["tier-floor"][1][0])

    def test_ci_slice_cap_is_per_slice_not_per_commit(self):
        self.ticket()
        for name in ("A", "B", "C"):  # 270 lines split over three commits of one slice
            self.slice_commit("src/test/java/app/%s.java" % name, 1)
        self.assertEqual(self.result()[0]["slice-size"][0], ci.PASS)
        self.slice_commit(self.SRC, 90)
        self.write(self.SRC, "".join("fixed %d\n" % i for i in range(90)))
        self.commit("fix round\n\nSlice: S-01")  # rewrites the same lines: net change stays 90
        self.assertEqual(self.result()[0]["slice-size"][0], ci.PASS)
        self.write(self.SRC, "".join("fixed %d\n" % i for i in range(120)))
        self.commit("more\n\nSlice: S-01")
        self.assertIn("slice S-01 changes 120 production lines", self.result()[0]["slice-size"][1][0])

    def test_ci_checks_code_added_in_a_merge_commit(self):
        self.ticket()
        self.git("checkout", "-q", "main")
        self.write("README.md", "upstream\n")
        self.commit("upstream change")
        self.git("checkout", "-q", "ABC-1-orders")
        self.git("merge", "-q", "--no-commit", "--no-ff", "main")
        self.write("src/main/java/app/Smuggled.java", "x\n" * 400)
        self.commit("merge main")
        rows, failed = self.result()
        self.assertTrue(failed)
        self.assertIn("without a `Slice: S-nn` trailer", " ".join(rows["slice-paths"][1]))
        self.assertIn("400 production lines", " ".join(rows["slice-size"][1]))

    def test_an_old_ticket_cannot_be_reused(self):
        self.git("checkout", "-q", "main")
        self.write("docs/agent-work/ABC-12/plan.md", plan("T0").replace("ABC-1", "ABC-12"))
        self.write("docs/agent-work/ABC-12/log.md", "state: DONE\n")
        self.commit("old merged ticket")
        self.git("checkout", "-q", "-b", "feature/ABC-123-new")
        self.assertIsNone(tk.resolve_ticket(self.root, self.settings))
        self.assertIn("[devgent:ticket]", self.pre("create", {"path": self.SRC}))
        self.write("docs/agent-work/ABC-12/log.md", "state: DONE\n- 2026-11-13 note\n")
        self.slice_commit(self.SRC, 5, trailer=None)
        rows, failed = self.result()
        self.assertTrue(failed)
        self.assertIn("found: none", rows["ticket"][1][0])
        # a finished ticket named by the branch is not picked up either
        self.git("checkout", "-q", "-b", "ABC-12-again")
        self.assertIsNone(tk.resolve_ticket(self.root, self.settings))

    def test_slice_review_survives_a_log_without_trailing_newline(self):
        self.ticket(log="state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n- 2026-11-13 report S-01: 3 tests pass")
        flow.slice_step(self.root, "review", "S-01")
        ticket = tk.Ticket(self.root, self.settings, "ABC-1")
        self.assertEqual(ticket.log().slice_status, {"S-01": "awaiting-review"})
        self.assertIn("3 tests pass\n- ", ticket.read("log.md"))

    def test_structure_needs_a_real_plan_challenge_and_tolerates_crlf(self):
        self.ticket()
        with open(os.path.join(REPO, "docs/agent-work/_templates/review.md")) as fh:
            self.write("docs/agent-work/ABC-1/review.md", fh.read())
        self.write("docs/agent-work/ABC-1/plan.md", plan().replace("\n", "\r\n"))
        self.commit("crlf plan, untouched review template")
        rows, _ = self.result()
        self.assertEqual(rows["ticket"][0], ci.PASS)
        self.assertEqual(len(rows["structure"][1]), 1)
        self.assertIn("no plan challenge yet", rows["structure"][1][0])
        self.write("docs/agent-work/ABC-1/review.md", "## Plan objections\n\nNo objections.\n")
        self.commit("challenge done")
        self.assertEqual(self.result()[0]["structure"][0], ci.PASS)

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as fh:
            return fh.read()

    def test_ci_requires_exactly_one_ticket(self):
        self.slice_commit(self.SRC, 5)
        rows, failed = self.result()
        self.assertTrue(failed)
        self.assertIn("found: none", rows["ticket"][1][0])

    def test_ci_slice_size_and_paths(self):
        self.ticket()
        self.slice_commit(self.SRC, 101)
        self.slice_commit("src/main/java/app/Stray.java", 3)
        self.slice_commit("src/main/java/app/OrderRepository.java", 3, trailer=None)
        self.slice_commit("src/main/java/app/OrderService.java", 3, trailer="S-09")
        rows, failed = self.result()
        self.assertTrue(failed)
        self.assertEqual(rows["slice-size"][0], ci.FAIL)
        self.assertIn("slice S-01 changes 104 production lines", rows["slice-size"][1][0])
        details = " ".join(rows["slice-paths"][1])
        self.assertIn("Stray.java, outside the slice's paths", details)
        self.assertIn("without a `Slice: S-nn` trailer", details)
        self.assertIn("S-09, which the plan does not define", details)

    def test_ci_tier_floor_and_override(self):
        self.ticket()
        self.write("k8s/deploy.yml", "kind: Deployment\n")
        self.commit("manifest")
        rows, failed = self.result()
        self.assertTrue(failed)
        self.assertIn("needs at least T2", rows["tier-floor"][1][0])
        self.write("docs/agent-work/ABC-1/log.md",
                   "state: IMPLEMENTING\n- 2026-11-13 override tier-floor by Sam Lee: comment-only manifest change\n")
        self.commit("override")
        rows, failed = self.result()
        self.assertFalse(failed, rows)
        self.assertEqual(rows["tier-floor"][0], ci.OVERRIDDEN)

    def test_ci_breaking_change_and_new_client(self):
        self.ticket()
        self.slice_commit("src/main/java/app/OrderController.java", 3)
        self.assertEqual(self.result()[0]["tier-floor"][0], ci.PASS)
        self.assertEqual(self.result(breaking_api_change=True)[0]["tier-floor"][0], ci.FAIL)
        self.slice_commit("src/main/java/app/client/PayClient.java", 3)
        self.assertEqual(self.result()[0]["tier-floor"][0], ci.FAIL)

    def test_ci_structure(self):
        self.ticket(answer="")
        self.write("docs/agent-work/ABC-1/review.md", review("rejected"))
        self.commit("review")
        rows, failed = self.result()
        self.assertTrue(failed)
        self.assertEqual(rows["structure"][1], ["Decision D-01 has no approver's answer",
                                                "Objection O-01 is rejected without a reason"])

    def reviewed(self, sha, brief="## Review brief\nBuild and gates pass. Nothing needs your judgement.\n"):
        self.write("docs/agent-work/ABC-1/review.md",
                   review() + "## Diff findings\n### Round 1\n\nReviewed head: `%s`\n\n%s" % (sha, brief))
        return self.commit("diff review")

    def test_ci_review_is_bound_to_the_reviewed_commit(self):
        self.ticket()
        code = self.slice_commit(self.SRC, 10)
        self.assertEqual(self.result()[0]["review"][0], ci.SKIP)
        # a pull request that is ready for review, or a ticket in review, needs the diff review
        self.assertIn("no diff review", self.result(ready=True)[0]["review"][1][0])
        self.write("docs/agent-work/ABC-1/log.md", "state: IN_REVIEW\n")
        self.commit("in review")
        rows, failed = self.result()
        self.assertTrue(failed)
        self.assertEqual(len(rows["review"][1]), 2)  # no reviewed head, no brief
        self.reviewed(code[:10])
        self.assertEqual(self.result()[0]["review"], (ci.PASS, []))
        # merging the base branch in does not change what was reviewed
        self.git("checkout", "-q", "main")
        self.write("README.md", "unrelated\n")
        self.commit("unrelated change on main")
        self.git("checkout", "-q", "ABC-1-orders")
        self.git("merge", "-q", "--no-edit", "main")
        self.assertEqual(self.result()[0]["review"][0], ci.PASS)
        # a code change after the review needs a new round
        self.slice_commit(self.SRC, 11)
        self.assertIn("changed after the reviewed commit", self.result()[0]["review"][1][0])
        self.reviewed(self.git("rev-parse", "HEAD"), brief="")
        self.assertEqual(self.result()[0]["review"][1], ["review.md has no review brief for the human diff review"])
        self.reviewed("0123456789")
        self.assertIn("not a commit on this branch", self.result()[0]["review"][1][0])
        self.write("docs/agent-work/ABC-1/log.md",
                   "state: IN_REVIEW\n- 2026-11-13 override review by Ana: reviewed in a pairing session\n")
        self.commit("override")
        self.assertEqual(self.result()[0]["review"][0], ci.OVERRIDDEN)

    def test_ci_t0(self):
        self.write("docs/agent-work/ABC-1/plan.md",
                   "---\nticket: ABC-1\ntier: T0\nplan_version: 1\nbase_commit: x\n---\n"
                   "## Goal\nfix\n## Files\n`A.java`\n## Checks\n`./gradlew test`\n")
        self.commit("note")
        self.slice_commit(self.SRC, 20, trailer=None)
        rows, failed = self.result()
        self.assertFalse(failed, rows)
        self.slice_commit("build.gradle", 2, trailer=None)
        self.assertEqual(self.result()[0]["tier-floor"][0], ci.FAIL)

    def test_new_ticket_scaffold(self):
        self.git("checkout", "-q", "-b", "XYZ-7-thing")
        ci.new_ticket(self.root, "XYZ-7", "T1")
        ticket = tk.resolve_ticket(self.root, self.settings)
        self.assertEqual(ticket.id, "XYZ-7")
        self.assertEqual(tk.check_front_matter(ticket.plan(), "XYZ-7"), [])
        self.assertEqual(ticket.log().state, "TRIAGED")
        self.assertTrue(os.path.exists(os.path.join(self.root, "docs/agent-work/XYZ-7/review.md")))

    # ---- driver commands ----------------------------------------------

    def test_slice_and_state_commands(self):
        self.ticket(log="state: PLAN_APPROVED\n\n## Entries\n")
        ticket = tk.Ticket(self.root, self.settings, "ABC-1")
        with self.assertRaises(flow.FlowError):
            flow.slice_step(self.root, "start", "S-09")
        with self.assertRaises(flow.FlowError):
            flow.slice_step(self.root, "review", "S-01")
        flow.slice_step(self.root, "start", "s-01")
        self.assertEqual((ticket.log().state, ticket.log().active_slices()), ("IMPLEMENTING", ["S-01"]))
        with self.assertRaises(flow.FlowError):  # one slice at a time
            flow.slice_step(self.root, "start", "S-02")
        flow.slice_step(self.root, "review", "S-01")
        with self.assertRaises(flow.FlowError):  # WIP limit: review first
            flow.slice_step(self.root, "start", "S-02")
        flow.slice_step(self.root, "start", "S-01")  # fix round
        flow.slice_step(self.root, "review", "S-01")
        flow.slice_step(self.root, "accept", "S-01")
        flow.slice_step(self.root, "start", "S-02")
        self.assertEqual(ticket.log().slice_status, {"S-01": "accepted", "S-02": "started"})
        with self.assertRaises(flow.FlowError):
            flow.set_state(self.root, "BLOCKED", "")
        flow.set_state(self.root, "blocked", "Q-01 waiting on the pay team")
        log = ticket.read("log.md")
        self.assertTrue(log.startswith("state: BLOCKED\n"))
        self.assertIn("state IMPLEMENTING -> BLOCKED: Q-01 waiting on the pay team", log)

    def test_override_command(self):
        self.ticket()
        with self.assertRaises(flow.FlowError):
            flow.override(self.root, "dangerous-action", "no")
        flow.override(self.root, "slice-size", "generated mapper")
        ticket = tk.Ticket(self.root, self.settings, "ABC-1")
        self.assertEqual(ticket.log().overrides, {"slice-size": "Test Driver: generated mapper"})
        for command in ("./devgent override slice-size because", "./devgent slice accept S-01",
                        "./devgent update --force", "python3 -m devgent override x y"):
            self.assertIn("dangerous-action", self.pre("bash", {"command": command}), command)
        self.assertIsNone(self.pre("bash", {"command": "./devgent slice review S-01"}))
        self.assertIsNone(self.pre("bash", {"command": "./devgent status"}))

    def test_answer_command(self):
        self.ticket(answer="")
        ticket = tk.Ticket(self.root, self.settings, "ABC-1")
        with self.assertRaises(flow.FlowError):
            flow.answer(self.root, "D-09", "A")
        flow.answer(self.root, "d-01", "B, for guests only")
        self.assertEqual(ticket.plan().decisions[0]["answer"], "B, for guests only")
        self.assertEqual(tk.check_plan_structure(ticket.plan()), [])
        # no answer line yet, and the template's commented example is not a decision
        text = "<!-- example:\n### D-01: x\n- Answer:\n-->\n" + plan().replace("- Answer: A\n", "") + "\n## Alternatives\nNone.\n"
        self.write("docs/agent-work/ABC-1/plan.md", text)
        flow.answer(self.root, "D-01", "A")
        raw = ticket.read("plan.md")
        self.assertTrue(raw.startswith("<!-- example:\n### D-01: x\n- Answer:\n-->"))
        self.assertIn("- Reversible: yes\n- Answer: A\n\n## Alternatives", raw)
        self.assertIn("dangerous-action", self.pre("bash", {"command": "./devgent answer D-01 A"}))

    def test_view_is_generated_from_the_ticket_files(self):
        self.ticket(answer="")
        self.write("docs/agent-work/ABC-1/plan.md", plan(answer="").replace(
            "| SC-02 | Integration | OrderFlowIT.storesOrder |\n", "").replace(
            "The service validates first.", "```mermaid\nsequenceDiagram\n  A->>B: <order>\n```"))
        self.write("docs/agent-work/ABC-1/review.md", review("") + (
            "## Diff findings\n### Round 1\n\nReviewed head: `abc1234def`\n\n"
            "| ID | Severity | File:line | Failure scenario | Recommended fix | Type | Action | Status |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| F-01 | high | `A.java:3` | <b>null</b> id | check | defect | decide | open |\n"))
        flow.slice_step(self.root, "start", "S-01")
        flow.slice_step(self.root, "review", "S-01")
        ticket = tk.Ticket(self.root, self.settings, "ABC-1")
        model = view.Model(self.settings, ticket)
        self.assertEqual([(kind, ident) for kind, ident, _, _ in model.queue()],
                         [("decision", "D-01"), ("objection", "O-01"), ("slice", "S-01"), ("finding", "F-01")])
        page = view.render(self.settings, ticket)
        self.assertIn('data-decision="D-01"', page)
        self.assertIn("recommended", page)
        self.assertIn("1 gap", page)                       # SC-02 lost its test
        self.assertIn("A-&gt;&gt;B: &lt;order&gt;", page)  # ticket text is escaped, never markup
        self.assertNotIn("<b>null</b>", page)
        self.assertEqual(view.run(self.root), 0)
        self.assertEqual(self.git("status", "--porcelain", "--ignored").count(".html"), 0)

    def test_view_follows_the_layers_of_the_plan(self):
        self.ticket()
        self.write(self.SRC, "class OrderService {}\n")
        self.write("docs/agent-work/ABC-1/plan.md", plan() + (
            "\n## Open questions\n| ID | Question | Layer | Owner | Blocking | Answer source | Status |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| Q-01 | Do guests count as customers? | functional | Priya | yes | | open |\n"
            "| Q-02 | Is the pool large enough? | high-level | Sam | no | | open |\n"))
        self.commit("source file and questions")
        ticket = tk.Ticket(self.root, self.settings, "ABC-1")
        page = view.render(self.settings, ticket)
        order = [page.index('<h2 id="%s">' % anchor)
                 for anchor in ("decisions", "functional", "high-level", "low-level", "questions", "review", "references")]
        self.assertEqual(order, sorted(order))              # behaviour first, files last
        for anchor in ("functional", "high-level", "low-level", "references"):
            self.assertIn('<a href="#%s">' % anchor, page)  # and a way to jump between them
        self.assertIn("Orders are stored unchecked", page)  # the design reads as written
        self.assertIn('<li id="R-01">', page)
        self.assertIn('<a class="ref" href="#R-01">R-01</a>', page)   # ids link to where they are shown
        self.assertIn('<span class="pill layer">functional</span>', page)
        # a short file name resolves when one tracked file ends with it
        refs = dict((ref, link) for ref, link, _ in view.references(self.root, ticket.plan()))
        self.assertEqual(refs["OrderService.java:12"], "file://" + os.path.join(self.root, self.SRC))
        self.assertIsNone(refs["src/main/java/app/OrderRepository.java"])   # not written yet
        self.assertEqual(view._web_base(self.root), None)                   # no hosted remote here
        self.git("remote", "add", "origin", "git@github.example.com:acme/orders.git")
        self.assertEqual(view._web_base(self.root), "https://github.example.com/acme/orders")
        base = self.git("rev-parse", "HEAD")
        hosted = tk.Plan(plan().replace("base_commit: abc123", "base_commit: " + base))
        self.assertEqual(dict((r, l) for r, l, _ in view.references(self.root, hosted))["OrderService.java:12"],
                         "https://github.example.com/acme/orders/blob/%s/%s#L12" % (base, self.SRC))
        self.git("remote", "remove", "origin")
        self.assertNotIn("src/test/java/app/**", refs)

        functional = view.render_functional(self.settings, ticket)
        self.assertIn("Reject orders without a customer id.", functional)
        self.assertIn("<td>Unit</td>", functional)          # how a scenario is checked, not by which class
        for hidden in ("OrderServiceTest", "OrderService.java", "S-01", "Q-02", "<nav>", "<input", "devgent answer"):
            self.assertNotIn(hidden, functional)
        self.assertIn("Do guests count as customers?", functional)
        self.assertIn("Reject or default?", functional)     # the decision tagged functional, read-only
        self.assertEqual(view.run(self.root, functional=True), 0)
        self.assertTrue(os.path.exists(os.path.join(self.root, ".git/devgent/view/ABC-1-functional.html")))
        self.assertIn("Orders are stored unchecked",        # the flat layout draws its design too
                      view.render(self.settings, self._flat_ticket()).replace("Current flow, proposed flow.",
                                                                              "Orders are stored unchecked"))

    def _flat_ticket(self):
        self.write("docs/agent-work/ABC-1/plan.md", flat_plan())
        return tk.Ticket(self.root, self.settings, "ABC-1")

    def test_ci_passes_a_plan_in_the_flat_layout(self):
        self.ticket()
        self.write("docs/agent-work/ABC-1/plan.md", flat_plan())
        self.commit("plan written before the layers")
        self.slice_commit(self.SRC, 10)
        rows, failed = self.result()
        self.assertFalse(failed, rows)
        self.assertEqual(rows["structure"][0], ci.PASS)

    def next(self, approved=None):
        ticket = tk.Ticket(self.root, self.settings, "ABC-1")
        approval = (True, "ok") if approved else (False, "no review yet")
        return flow.next_step(self.root, self.settings, ticket, ticket.plan(), ticket.log(), approval)

    def test_next_step_walks_the_workflow(self):
        self.ticket(answer="")
        self.assertIn("not ready for approval", self.next())
        self.ticket()
        self.assertIn("gh pr create --draft", self.next())
        self.assertIn("slice start S-01", self.next(approved=True))
        flow.slice_step(self.root, "start", "S-01")
        self.assertIn("slice review S-01", self.next(approved=True))
        flow.slice_step(self.root, "review", "S-01")
        self.assertIn("slice accept S-01", self.next(approved=True))
        flow.slice_step(self.root, "accept", "S-01")
        self.assertIn("slice start S-02", self.next(approved=True))
        for step in ("start", "review", "accept"):
            flow.slice_step(self.root, step, "S-02")
        self.assertIn("state IN_REVIEW", self.next(approved=True))
        flow.set_state(self.root, "IN_REVIEW", "")
        self.assertIn("review the diff", self.next(approved=True))

    # ---- worktrees: one checkout per ticket ---------------------------------

    def start(self, ticket_id, tier="T1", **kw):
        """Run `start` from the main checkout on `main`; returns (printed text, worktree path)."""
        if self.git("rev-parse", "--abbrev-ref", "HEAD") != "main":
            self.git("checkout", "-q", "main")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            flow.start(self.root, ticket_id, tier, **kw)
        return out.getvalue(), os.path.join(self.worktrees, ticket_id)

    def branch_of(self, path):
        return subprocess.run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], check=True,
                              stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()

    def test_start_creates_worktree_for_ticket(self):
        text, wt = self.start("XYZ-7", "T0")
        self.assertIn("created worktree %s (new branch XYZ-7 from main)" % wt, text)
        self.assertIn("`cd %s`" % wt, text)
        self.assertEqual(self.branch_of(wt), "XYZ-7")
        ticket = tk.resolve_ticket(wt, self.settings)
        self.assertEqual((ticket.id, ticket.plan().tier), ("XYZ-7", "T0"))
        self.assertIsNone(ticket.read("review.md"))
        # the checkout `start` ran in is untouched
        self.assertEqual(self.branch_of(self.root), "main")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIsNone(tk.resolve_ticket(self.root, self.settings))
        self.assertEqual([b for _, b in tk.worktrees(self.root)], ["main", "XYZ-7"])
        self.assertTrue(tk.is_linked_worktree(wt))
        self.assertFalse(tk.is_linked_worktree(self.root))
        self.assertEqual(os.path.realpath(tk.main_worktree(wt)), self.root)

    def test_two_tickets_get_two_worktrees(self):
        _, wt7 = self.start("XYZ-7")
        _, wt8 = self.start("XYZ-8", "T0")
        self.assertEqual(tk.resolve_ticket(wt7, self.settings).id, "XYZ-7")
        self.assertEqual(tk.resolve_ticket(wt8, self.settings).id, "XYZ-8")
        self.assertEqual(sorted(t for t, _, _ in tk.ticket_worktrees(self.root, self.settings)), ["XYZ-7", "XYZ-8"])
        self.assertEqual([t for t, _, _ in tk.ticket_worktrees(wt7, self.settings)], ["XYZ-8"])

    def test_worktrees_dir_setting_places_the_worktree(self):
        tickets = os.path.join(os.path.dirname(self.root), os.path.basename(self.root) + "-tickets")
        self.addCleanup(shutil.rmtree, tickets, True)
        self.git("checkout", "-q", "main")
        self.write(config.SETTINGS_PATH, self.read(config.SETTINGS_PATH).replace(
            "worktrees:\n  dir:\n", "worktrees:\n  dir: ../%s\n" % os.path.basename(tickets)))
        self.commit("settings")
        settings = config.load_settings(self.root)
        self.assertEqual(settings.worktrees_dir, "../" + os.path.basename(tickets))
        self.assertEqual(tk.worktree_path(self.root, settings, "XYZ-7"), os.path.join(tickets, "XYZ-7"))
        text, _ = self.start("XYZ-7", "T0")
        self.assertTrue(os.path.isfile(os.path.join(tickets, "XYZ-7", "docs/agent-work/XYZ-7/plan.md")))
        # from inside a linked worktree the path is still relative to the main checkout
        self.assertEqual(tk.worktree_path(os.path.join(tickets, "XYZ-7"), settings, "XYZ-8"),
                         os.path.join(tickets, "XYZ-8"))

    def test_start_path_option(self):
        dest = os.path.join(tempfile.mkdtemp(), "mine", "XYZ-7")
        self.addCleanup(shutil.rmtree, os.path.dirname(os.path.dirname(dest)), True)
        text, _ = self.start("XYZ-7", "T0", path=dest)
        self.assertIn("created worktree %s" % dest, text)
        self.assertEqual(self.branch_of(dest), "XYZ-7")
        self.assertFalse(os.path.exists(self.worktrees))

    def test_start_on_ticket_branch_scaffolds_in_place(self):
        # setUp left us on ABC-1-orders, which names the ticket
        flow.start(self.root, "ABC-1", "T1")
        self.assertEqual(self.branch_of(self.root), "ABC-1-orders")
        self.assertEqual(tk.resolve_ticket(self.root, self.settings).id, "ABC-1")
        self.assertEqual(len(tk.worktrees(self.root)), 1)

    def test_start_here_switches_in_place(self):
        self.git("checkout", "-q", "main")
        flow.start(self.root, "XYZ-7", "T0", here=True)
        self.assertEqual(self.branch_of(self.root), "XYZ-7")
        ticket = tk.resolve_ticket(self.root, self.settings)
        self.assertEqual((ticket.id, ticket.plan().tier), ("XYZ-7", "T0"))
        self.assertIsNone(ticket.read("review.md"))
        self.assertEqual(len(tk.worktrees(self.root)), 1)

    def test_start_refuses_second_worktree_for_same_ticket(self):
        _, wt = self.start("XYZ-7")
        with self.assertRaises(flow.FlowError) as ctx:
            flow.start(self.root, "XYZ-7", "T1")
        self.assertIn("already checked out at %s" % wt, str(ctx.exception))
        self.assertEqual(len(tk.worktrees(self.root)), 2)

    def test_start_refuses_existing_destination(self):
        self.git("checkout", "-q", "main")
        os.makedirs(os.path.join(self.worktrees, "XYZ-7"))
        with self.assertRaises(flow.FlowError) as ctx:
            flow.start(self.root, "XYZ-7", "T1")
        self.assertIn(os.path.join(self.worktrees, "XYZ-7"), str(ctx.exception))
        self.assertEqual(len(tk.worktrees(self.root)), 1)
        self.assertEqual(self.git("branch", "--list", "XYZ-7"), "")

    def test_start_refuses_existing_ticket_folder(self):
        self.ticket()
        with self.assertRaises(flow.FlowError) as ctx:
            flow.start(self.root, "ABC-1", "T1")
        self.assertIn("already exists", str(ctx.exception))

    def test_start_reuses_existing_branch_and_folder(self):
        self.git("checkout", "-q", "-b", "XYZ-7", "main")
        self.write("docs/agent-work/XYZ-7/plan.md", plan().replace("ABC-1", "XYZ-7"))
        self.write("docs/agent-work/XYZ-7/log.md", "state: PLANNING\n")
        self.commit("XYZ-7 plan")
        text, wt = self.start("XYZ-7")
        self.assertIn("(existing branch XYZ-7)", text)
        self.assertIn("already there; nothing scaffolded", text)
        self.assertEqual(tk.resolve_ticket(wt, self.settings).log().state, "PLANNING")

    def test_start_tracks_remote_branch(self):
        remote = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, remote, True)
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        self.git("remote", "add", "origin", remote)
        self.git("branch", "XYZ-7", "main")
        self.git("push", "-q", "origin", "XYZ-7")
        self.git("branch", "-D", "XYZ-7")
        text, wt = self.start("XYZ-7", "T0")
        self.assertIn("(origin/XYZ-7)", text)
        self.assertEqual(self.branch_of(wt), "XYZ-7")
        upstream = subprocess.run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "XYZ-7@{upstream}"], check=True,
                                  stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        self.assertEqual(upstream, "origin/XYZ-7")
        self.assertTrue(os.path.isfile(os.path.join(wt, "docs/agent-work/XYZ-7/plan.md")))

    def test_approval_cache_is_per_ticket(self):
        commit1 = self.ticket()
        self.write("docs/agent-work/ABC-2/plan.md", plan().replace("ABC-1", "ABC-2"))
        self.write("docs/agent-work/ABC-2/log.md", "state: PLANNING\n")
        commit2 = self.commit("ABC-2 plan")
        one, two = tk.Ticket(self.root, self.settings, "ABC-1"), tk.Ticket(self.root, self.settings, "ABC-2")
        self.assertTrue(tk.plan_approval(self.root, one, fetch=lambda r: ([commit1], None))[0])
        self.assertTrue(tk.plan_approval(self.root, two, fetch=lambda r: ([commit2], None))[0])
        state = tk.state_dir(self.root)
        self.assertEqual(sorted(f for f in os.listdir(state) if f.startswith("approval")),
                         ["approval-ABC-1.json", "approval-ABC-2.json"])
        # the second lookup did not evict the first: ABC-1 is still approved without asking GitHub
        self.assertEqual(tk.plan_approval(self.root, one, fetch=lambda r: (None, "offline"))[1], "approved (cached)")

    def test_state_dir_is_shared_by_worktrees(self):
        _, wt = self.start("XYZ-7")
        self.assertEqual(os.path.realpath(tk.state_dir(wt)), os.path.realpath(tk.state_dir(self.root)))
        self.assertTrue(tk.state_dir(wt).startswith(os.path.join(self.root, ".git")))
        self.assertEqual(view.run(wt), 0)
        self.assertTrue(os.path.isfile(os.path.join(tk.state_dir(self.root), "view", "XYZ-7.html")))

    def test_status_lists_ticket_worktrees(self):
        _, wt7 = self.start("XYZ-7")
        _, wt8 = self.start("XYZ-8", "T0")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(flow.status(self.root), 1)
        text = out.getvalue()
        self.assertIn("no active ticket", text)
        self.assertIn("tickets in other worktrees", text)
        self.assertRegex(text, r"XYZ-7\s+TRIAGED\s+%s" % re.escape(wt7))
        self.assertRegex(text, r"XYZ-8\s+TRIAGED\s+%s" % re.escape(wt8))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(flow.status(wt7), 0)
        self.assertNotIn("other worktrees", out.getvalue())

    def test_done_in_worktree_suggests_removal(self):
        _, wt = self.start("XYZ-7")
        ticket = tk.Ticket(wt, self.settings, "XYZ-7")
        with open(os.path.join(wt, ticket.rel("log.md")), "w") as fh:
            fh.write("state: DONE\n")
        hint = flow.next_step(wt, self.settings, ticket, ticket.plan(), ticket.log(), (True, ""))
        self.assertIn("git -C %s worktree remove %s" % (self.root, wt), hint)
        self.ticket(log="state: DONE\n")
        self.assertNotIn("worktree remove", self.next(approved=True))

    def test_hook_refuses_write_into_another_worktree(self):
        _, wt7 = self.start("XYZ-7")
        _, wt8 = self.start("XYZ-8", "T0")
        # the test repositories live in the system temp folder, where the hook lets scratch
        # writes through; point it elsewhere so these checkouts count as ordinary folders
        tempfile.tempdir, saved = os.path.join(self.root, "scratch"), tempfile.tempdir
        os.makedirs(tempfile.tempdir)
        self.addCleanup(setattr, tempfile, "tempdir", saved)
        self.assertIn("[devgent:outside-repo]", self.pre("create", {"path": os.path.join(wt7, self.SRC)}, cwd=wt8))
        self.assertIn("[devgent:outside-repo]", self.pre("create", {"path": os.path.join(self.root, self.SRC)}, cwd=wt8))
        self.assertIsNone(self.pre("create", {"path": "docs/agent-work/XYZ-8/plan.md"}, cwd=wt8))

    def test_hook_judges_write_by_worktree_root(self):
        _, wt = self.start("XYZ-7")
        # inside the worktree the T1 ticket applies: a source write waits for plan approval
        self.assertIn("[devgent:plan-approval]", self.pre("create", {"path": self.SRC}, cwd=wt))
        # the main checkout on `main` has no ticket at all
        self.assertIn("[devgent:ticket]", self.pre("create", {"path": self.SRC}, cwd=self.root))
        # and the worktree's own .git (a file) is still off limits
        self.assertIn("[devgent:dangerous-action]", self.pre("create", {"path": ".git"}, cwd=wt))


class DocsTest(unittest.TestCase):
    def test_docs_mention_worktrees_and_start_options(self):
        def read(rel):
            with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                return fh.read()
        readme = read("README.md")
        for text in ("### One worktree per ticket", "(#one-worktree-per-ticket)", "--here", "--path",
                     "worktrees.dir", "git worktree remove"):
            self.assertIn(text, readme)
        self.assertIn("worktree", read("AGENTS.md"))
        examples = read("docs/examples.md")
        for text in ("worktrees/ORD-142", "## 9. Two tickets at once", "(#9-two-tickets-at-once)"):
            self.assertIn(text, examples)
        self.assertIn("worktrees:", read(config.SETTINGS_PATH))

    def test_docs_templates_and_agents_describe_the_three_layers(self):
        def read(rel):
            with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                return fh.read()
        headings = ["## %s" % heading for _, heading, _ in tk.LAYERS]
        for rel in ("docs/agent-work/_templates/plan.md", "docs/examples.md"):
            for heading in headings:
                self.assertIn(heading + "\n", read(rel), rel)
        for rel in ("README.md", ".github/agents/architect.agent.md"):
            for _, heading, _ in tk.LAYERS:
                self.assertIn(heading, read(rel), rel)
        readme, examples = read("README.md"), read("docs/examples.md")
        for text in ("### A plan in three layers", "(#a-plan-in-three-layers)", "view --functional"):
            self.assertIn(text, readme)
        for text in ("## 10. Showing the scenarios to a BA, PO or QE", "(#10-showing-the-scenarios-to-a-ba-po-or-qe)",
                     "view --functional"):
            self.assertIn(text, examples)
        self.assertIn("| ID | Layer | Objection |", read("docs/agent-work/_templates/review.md"))
        self.assertIn("mismatch S-02 (low-level)", read("docs/agent-work/_templates/log.md"))
        for rel in (".github/agents/reviewer.agent.md", ".github/agents/implementer.agent.md"):
            for tag in tk.LAYER_TAGS:
                self.assertIn("`%s`" % tag, read(rel), rel)
        self.assertIn("view --functional", view.__doc__)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.target = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.target, True)
        subprocess.run(["git", "-C", self.target, "init", "-q", "-b", "main"], check=True)

    def read(self, rel):
        with open(os.path.join(self.target, rel)) as fh:
            return fh.read()

    def write(self, rel, text):
        with open(os.path.join(self.target, rel), "w") as fh:
            fh.write(text)

    def results(self):
        report = doctor.Doctor(self.target, offline=True).run()
        return {message: level for _, level, message, _ in report.results}, report

    def test_install_then_update_keeps_what_is_yours(self):
        first = install.install(self.target, REPO)
        self.assertIsNone(first["previous"])
        self.assertEqual(sorted(first["created"]), sorted(install.REPO_OWNED))
        self.assertTrue(os.access(os.path.join(self.target, "devgent"), os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(self.target, ".github/hooks/tests")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "README.md")))
        manifest = install.read_manifest(self.target)
        self.assertIn(".github/hooks/devgent/hook.py", manifest["files"])
        self.assertIn("AGENTS.md", manifest["files"])
        self.assertEqual(install.modified_shared_files(self.target), [])

        settings = self.read(".github/devgent.yml").replace("max_production_lines: 100", "max_production_lines: 60")
        self.write(".github/devgent.yml", settings)
        self.write(".github/agents/reviewer.agent.md", "edited locally")
        os.remove(os.path.join(self.target, ".github/agents/scout.agent.md"))
        self.write(".github/agents/my-own.agent.md", "mine")
        self.assertEqual(install.modified_shared_files(self.target),
                         [(".github/agents/reviewer.agent.md", "modified"), (".github/agents/scout.agent.md", "missing")])

        second = install.install(self.target, REPO)
        self.assertEqual(second["previous"], first["version"])
        self.assertEqual(second["kept"], [".github/devgent.yml"])
        self.assertEqual(second["updated"], [".github/agents/reviewer.agent.md"])
        self.assertIn("max_production_lines: 60", self.read(".github/devgent.yml"))
        self.assertEqual(self.read(".github/agents/my-own.agent.md"), "mine")
        self.assertEqual(install.modified_shared_files(self.target), [])

    def test_existing_agents_md_is_kept(self):
        self.write("AGENTS.md", "# ours\n")
        summary = install.install(self.target, REPO)
        self.assertEqual(summary["agents_md"], "kept")
        self.assertEqual(self.read("AGENTS.md"), "# ours\n")
        self.assertNotIn("AGENTS.md", install.read_manifest(self.target)["files"])

    def test_existing_codeowners_is_not_shadowed(self):
        self.write("CODEOWNERS", "* @acme/owners\n")
        summary = install.install(self.target, REPO)
        self.assertEqual(summary["codeowners"], "CODEOWNERS")
        self.assertFalse(os.path.exists(os.path.join(self.target, ".github/CODEOWNERS")))
        found, _ = self.results()
        self.assertEqual(found["CODEOWNERS has 1 active rules"], doctor.OK)

    def test_every_skill_an_agent_names_ships_with_the_kit(self):
        install.install(self.target, REPO)
        named = set()
        for agent in ("scout", "architect", "implementer", "reviewer"):
            named |= set(re.findall(r"`([a-z-]+)` skill", self.read(".github/agents/%s.agent.md" % agent)))
        self.assertIn("minimal-change", named)
        for skill in named:
            self.assertIn(".github/skills/%s" % skill, install.KIT_OWNED)
            self.assertIn("name: %s\n" % skill, self.read(".github/skills/%s/SKILL.md" % skill))

    def test_install_never_destroys_files_that_are_not_the_kits(self):
        os.makedirs(os.path.join(self.target, ".github/agents"))
        os.makedirs(os.path.join(self.target, ".github/skills/spring-boot-testing"))
        self.write(".github/agents/reviewer.agent.md", "our own reviewer")
        self.write(".github/skills/spring-boot-testing/notes.md", "ours")
        with self.assertRaises(install.InstallError) as caught:
            install.install(self.target, REPO)
        self.assertIn("reviewer.agent.md (already exists", str(caught.exception))
        self.assertIn("notes.md (not part of the kit", str(caught.exception))
        self.assertEqual(self.read(".github/agents/reviewer.agent.md"), "our own reviewer")
        self.assertFalse(os.path.exists(os.path.join(self.target, "devgent")))  # nothing was touched
        install.install(self.target, REPO, force=True)
        # later: a file the repository adds inside a shared directory stops an update too
        self.write("docs/agent-work/_templates/adr.md", "ours")
        with self.assertRaises(install.InstallError):
            install.install(self.target, REPO)
        self.assertEqual(self.read("docs/agent-work/_templates/adr.md"), "ours")

    def test_manifest_cannot_point_install_outside_the_kit_paths(self):
        install.install(self.target, REPO)
        victim = os.path.join(os.path.dirname(self.target), "victim-%s.txt" % os.path.basename(self.target))
        self.addCleanup(lambda: os.path.exists(victim) and os.remove(victim))
        with open(victim, "w") as fh:
            fh.write("keep")
        os.makedirs(os.path.join(self.target, "src"))
        self.write("src/App.java", "keep")
        manifest = install.read_manifest(self.target)
        manifest["files"]["../" + os.path.basename(victim)] = install.sha256(victim)
        manifest["files"]["src/App.java"] = "0" * 64
        self.write(install.MANIFEST, json.dumps(manifest))
        self.assertEqual(install.modified_shared_files(self.target), [])
        install.install(self.target, REPO, force=True)
        self.assertTrue(os.path.exists(victim))
        self.assertEqual(self.read("src/App.java"), "keep")

    def test_refuses_bad_targets(self):
        for target in (REPO, os.path.join(self.target, "nope"), tempfile.gettempdir()):
            with self.assertRaises(install.InstallError):
                install.install(target, REPO)

    def test_doctor_on_a_fresh_install(self):
        install.install(self.target, REPO)
        found, report = self.results()
        self.assertEqual(found["preToolUse hook runs and blocks a force push"], doctor.OK)
        self.assertEqual(found["CODEOWNERS has no active rules (needed for T2 code-owner approval, Phase 2)"], doctor.WARN)
        self.assertTrue(any("TODO placeholders" in m and lvl == doctor.WARN for m, lvl in found.items()))
        self.assertTrue(any("shared files match" in m for m in found))
        self.assertIn("fix:", report.text())
        self.assertIn("1. ", report.todo())

    def test_doctor_finds_broken_setups(self):
        install.install(self.target, REPO)
        self.write(".github/devgent.yml", "version: 1\n")
        self.write(".github/hooks/devgent.json", "{}")
        self.write(".github/CODEOWNERS", "/docs/ @your-org/service-owners\n")
        found, report = self.results()
        self.assertEqual(found[".github/hooks/devgent.json is missing or invalid"], doctor.FAIL)
        self.assertEqual(found["CODEOWNERS still names the placeholder @your-org teams"], doctor.FAIL)
        self.assertEqual(found["shared file modified: .github/hooks/devgent.json"], doctor.WARN)
        self.assertTrue(any(m.startswith("invalid .github/devgent.yml") and lvl == doctor.FAIL
                            for m, lvl in found.items()))
        self.assertGreaterEqual(report.count(doctor.FAIL), 3)

    def test_cli_install_update_and_guards(self):
        env = dict(os.environ)
        run = lambda cwd, *args: subprocess.run([os.path.join(cwd, "devgent")] + list(args), cwd=cwd, env=env,
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        out = run(REPO, "install", self.target)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("What is left to do", out.stdout)
        self.assertIn("devgent", run(self.target, "version").stdout)
        self.write(".github/agents/reviewer.agent.md", "edited locally")
        refused = run(self.target, "update", "--source", REPO)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("would be overwritten", refused.stdout)
        forced = run(self.target, "update", "--source", REPO, "--force")
        self.assertEqual(forced.returncode, 0, forced.stdout)
        self.assertIn("Updated devgent", forced.stdout)
        self.assertNotEqual(self.read(".github/agents/reviewer.agent.md"), "edited locally")
        self.assertIn("no active ticket", run(self.target, "status").stdout)
        self.assertFalse(any("__pycache__" in d for d, _, _ in os.walk(self.target)))


if __name__ == "__main__":
    unittest.main()
