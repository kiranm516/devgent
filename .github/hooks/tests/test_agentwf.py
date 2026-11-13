"""Run from the repository root:

    PYTHONPATH=.github/hooks python3 -m unittest discover -s .github/hooks/tests -v
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from agentwf import ci, config, doctor, flow, hook, install, ticket as tk

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
            "git commit -m 'docs: agentwf update notes'", "cat .env.example", "gh api repos/o/r/pulls/1/reviews",
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
        for bad in ([], "x", {"toolName": "edit", "toolArgs": "[1]", "cwd": self.root}):
            out = run(bad)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(json.loads(out.stdout)["permissionDecision"], "deny", bad)
        # the registered command works from a subdirectory too
        with open(os.path.join(self.root, ".github/hooks/agent-workflow.json")) as fh:
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
        self.assertEqual({s for s, _ in rows.values()}, {ci.PASS})

    def test_ci_judges_a_pr_by_the_base_branch_rules(self):
        self.ticket(tier="T0")
        self.write(".github/agent-workflow.yml", self.read(".github/agent-workflow.yml").split("tier_rules:")[0]
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
        self.assertIn("[agent-workflow:ticket]", self.pre("create", {"path": self.SRC}))
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
        for command in ("./agentwf override slice-size because", "./agentwf slice accept S-01",
                        "./agentwf update --force", "python3 -m agentwf override x y"):
            self.assertIn("dangerous-action", self.pre("bash", {"command": command}), command)
        self.assertIsNone(self.pre("bash", {"command": "./agentwf slice review S-01"}))
        self.assertIsNone(self.pre("bash", {"command": "./agentwf status"}))

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

    def test_start_creates_branch_and_folder(self):
        self.git("checkout", "-q", "main")
        flow.start(self.root, "XYZ-7", "T0")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "XYZ-7")
        ticket = tk.resolve_ticket(self.root, self.settings)
        self.assertEqual((ticket.id, ticket.plan().tier), ("XYZ-7", "T0"))
        self.assertIsNone(ticket.read("review.md"))


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
        self.assertTrue(os.access(os.path.join(self.target, "agentwf"), os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(self.target, ".github/hooks/tests")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "README.md")))
        manifest = install.read_manifest(self.target)
        self.assertIn(".github/hooks/agentwf/hook.py", manifest["files"])
        self.assertIn("AGENTS.md", manifest["files"])
        self.assertEqual(install.modified_shared_files(self.target), [])

        settings = self.read(".github/agent-workflow.yml").replace("max_production_lines: 100", "max_production_lines: 60")
        self.write(".github/agent-workflow.yml", settings)
        self.write(".github/agents/reviewer.agent.md", "edited locally")
        os.remove(os.path.join(self.target, ".github/agents/scout.agent.md"))
        self.write(".github/agents/my-own.agent.md", "mine")
        self.assertEqual(install.modified_shared_files(self.target),
                         [(".github/agents/reviewer.agent.md", "modified"), (".github/agents/scout.agent.md", "missing")])

        second = install.install(self.target, REPO)
        self.assertEqual(second["previous"], first["version"])
        self.assertEqual(second["kept"], [".github/agent-workflow.yml"])
        self.assertEqual(second["updated"], [".github/agents/reviewer.agent.md"])
        self.assertIn("max_production_lines: 60", self.read(".github/agent-workflow.yml"))
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
        self.assertFalse(os.path.exists(os.path.join(self.target, "agentwf")))  # nothing was touched
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
        self.write(".github/agent-workflow.yml", "version: 1\n")
        self.write(".github/hooks/agent-workflow.json", "{}")
        self.write(".github/CODEOWNERS", "/docs/ @your-org/service-owners\n")
        found, report = self.results()
        self.assertEqual(found[".github/hooks/agent-workflow.json is missing or invalid"], doctor.FAIL)
        self.assertEqual(found["CODEOWNERS still names the placeholder @your-org teams"], doctor.FAIL)
        self.assertEqual(found["shared file modified: .github/hooks/agent-workflow.json"], doctor.WARN)
        self.assertTrue(any(m.startswith("invalid .github/agent-workflow.yml") and lvl == doctor.FAIL
                            for m, lvl in found.items()))
        self.assertGreaterEqual(report.count(doctor.FAIL), 3)

    def test_cli_install_update_and_guards(self):
        env = dict(os.environ)
        run = lambda cwd, *args: subprocess.run([os.path.join(cwd, "agentwf")] + list(args), cwd=cwd, env=env,
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
