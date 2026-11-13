"""Run from the repository root:

    PYTHONPATH=.github/hooks python3 -m unittest discover -s .github/hooks/tests -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from agentwf import ci, config, hook, ticket as tk

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

PLAN = """---
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
| ID | Decision | Options | Recommendation | Approver's answer |
| --- | --- | --- | --- | --- |
| D-01 | Reject or default? | reject; default | reject | {answer} |
"""

REVIEW = """# Review: ABC-1
## Plan objections
| ID | Objection | Disposition | Reason or owner |
| --- | --- | --- | --- |
| O-01 | No timeout scenario | {disposition} | {note} |
"""


def plan(tier="T1", answer="Reject"):
    return PLAN.format(tier=tier, answer=answer)


def review(disposition="accepted", note=""):
    return REVIEW.format(disposition=disposition, note=note)


class ConfigTest(unittest.TestCase):
    def test_parses_the_real_settings_file(self):
        settings = config.load_settings(REPO)
        self.assertEqual(settings.slice_cap, 100)
        self.assertEqual(settings.artifact_root, "docs/agent-work")
        self.assertEqual(settings.production_hosts, [])
        self.assertTrue(any(r.get("new_files_only") for r in settings.tier_rules))

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
        self.assertEqual(floor((".github/hooks/agentwf/hook.py", False)), "T2")
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

    def test_untested_requirement_and_missing_change_map(self):
        text = plan().replace("| SC-02 | Integration | OrderFlowIT.storesOrder |\n", "")
        text = text.replace("### Change map", "### Notes")
        errors = tk.check_plan_structure(tk.Plan(text))
        self.assertIn("Test matrix: scenario SC-02 has no test level and named test", errors)
        self.assertIn("Requirement R-02 maps to no test", errors)
        self.assertIn("Design: `### Change map` is missing or empty", errors)

    def test_blocking_question_and_t2_alternatives(self):
        text = plan("T2") + (
            "\n## Open questions\n| ID | Question | Owner | Blocking | Answer source | Status |\n"
            "| --- | --- | --- | --- | --- | --- |\n| Q-01 | Idempotent? | pay team | yes | pay team | {s} |\n"
        )
        errors = tk.check_plan_structure(tk.Plan(text.format(s="open")))
        self.assertEqual(errors, ["Open question Q-01 is blocking and unresolved",
                                  "T2 plan needs a non-empty `## Alternatives` section"])
        ok = text.format(s="risk-accepted") + "\n## Alternatives\nPolling; lost on latency.\n"
        self.assertEqual(tk.check_plan_structure(tk.Plan(ok)), [])

    def test_templates_are_not_ready_for_approval(self):
        for name, tier in (("plan.md", "T1"), ("task-note.md", "T0")):
            with open(os.path.join(REPO, "docs/agent-work/_templates", name)) as fh:
                p = tk.Plan(fh.read().replace("{{TIER}}", tier))
            self.assertTrue(tk.check_plan_structure(p), name)

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
        rows = "".join("| O-0%d | x | accepted | |\n" % i for i in range(2, 7))
        self.assertIn("limit is 5", check(review() + rows)[0])

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
        ):
            self.assertFalse(self.check(command), command)
        self.assertFalse(self.check("curl https://api.prod.example.com/x", ["api.prod.example.com"]))

    def test_allowed(self):
        for command in (
            "git push origin ABC-1-orders", "git push -u origin feature/main-fix", "git status",
            "./gradlew test --tests '*OrderServiceTest'", "gh pr view --json number,reviews",
            "gh pr create --draft --fill", "git commit -m 'S-01: validation'", "ls src/main/environment",
            "kubectl get pods", "curl http://localhost:8080/orders",
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
        for key in ("AGENT_TICKET", "AGENT_WORKFLOW_OVERRIDE", "AGENT_ROLE"):
            os.environ.pop(key, None)
        self.addCleanup(os.environ.pop, "AGENT_WORKFLOW_OVERRIDE", None)

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

    def pre(self, tool, args, approved=None, **extra):
        """Run the preToolUse hook; returns the deny reason or None."""
        original = tk.approving_reviews
        tk.approving_reviews = lambda root: (approved, None) if approved is not None else (None, "no PR")
        hook.check_write.__defaults__ = (
            lambda root, t: tk.plan_approval(root, t, fetch=tk.approving_reviews, cache_ttl=0),
            hook.check_write.__defaults__[1])
        try:
            payload = dict(toolName=tool, toolArgs=json.dumps(args), cwd=self.root, sessionId="s1", **extra)
            decision = hook.pre_tool_use(payload)
        finally:
            tk.approving_reviews = original
        return decision and decision["permissionDecisionReason"]

    SRC = "src/main/java/app/OrderService.java"

    # ---- hook -------------------------------------------------------------

    def test_source_edit_needs_a_ticket(self):
        self.git("checkout", "-q", "-b", "no-ticket")
        self.assertIn("[agent-workflow:ticket]", self.pre("create", {"path": self.SRC}))
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
        self.ticket(tier="T0")
        self.assertIsNone(self.pre("edit", {"path": self.SRC}))
        self.assertIn("tier-floor", self.pre("edit", {"path": "build.gradle"}))

    def test_override_is_allowed_and_logged(self):
        self.ticket(log="state: IMPLEMENTING\n- 2026-11-13 slice S-01 started\n")
        os.environ["AGENT_WORKFLOW_OVERRIDE"] = "plan-approval: approver on leave, agreed in standup"
        self.assertIsNone(self.pre("edit", {"path": self.SRC}))
        self.assertIsNone(self.pre("edit", {"path": self.SRC}))
        log = tk.Ticket(self.root, self.settings, "ABC-1").read("log.md")
        self.assertEqual(log.count("override plan-approval by Test Driver: approver on leave"), 1)
        # the override names one gate only; the others still apply
        self.assertIn("slice-paths", self.pre("edit", {"path": "src/main/java/app/Other.java"}))
        self.assertIn("tier-floor", self.pre("create", {"path": "k8s/deploy.yml"}))
        os.environ["AGENT_WORKFLOW_OVERRIDE"] = "all: x"
        self.assertIn("dangerous-action", self.pre("bash", {"command": "gh pr merge 3"}))

    def test_agents_cannot_write_override_lines_or_outside_the_repo(self):
        self.ticket()
        args = {"path": "docs/agent-work/ABC-1/log.md", "new_str": "- 2026-11-13 override tier-floor by Sam: fine"}
        self.assertIn("written by a human", self.pre("edit", args))
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
        with open(os.path.join(self.root, ".git/agent-workflow/audit.jsonl")) as fh:
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
            ["python3", "-m", "agentwf", "hook", "pre"], input=json.dumps(payload), cwd=self.root, env=env,
            stdout=subprocess.PIPE, universal_newlines=True)
        denied = run({"toolName": "bash", "toolArgs": json.dumps({"command": "git push --force"}), "cwd": self.root})
        self.assertEqual(denied.returncode, 0)
        self.assertEqual(json.loads(denied.stdout)["permissionDecision"], "deny")
        allowed = run({"toolName": "bash", "toolArgs": {"command": "git status"}, "cwd": self.root})
        self.assertEqual((allowed.returncode, allowed.stdout), (0, ""))

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
        self.assertEqual({s for s, _ in rows.values()}, {ci.PASS})

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
        self.assertIn("101 production lines", rows["slice-size"][1][0])
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
                   "state: IN_REVIEW\n- 2026-11-13 override tier-floor by Sam Lee: comment-only manifest change\n")
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


if __name__ == "__main__":
    unittest.main()
