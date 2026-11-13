"""`agentwf doctor`: find what is wrong or unfinished in a repository's setup."""
import json
import os
import shutil
import subprocess
import sys

from . import config, install, ticket as tk

OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "info"
REQUIRED_CHECK = "gates"  # the job id in .github/workflows/agent-gates.yml


class Doctor:
    def __init__(self, root, offline=False):
        self.root, self.offline = root, offline
        self.results = []  # (section, level, message, fix)
        self.section = ""

    def add(self, level, message, fix=None):
        self.results.append((self.section, level, message, fix))

    def _run(self, argv, stdin=None, timeout=15):
        try:
            proc = subprocess.run(argv, cwd=self.root, input=stdin, timeout=timeout,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            return proc.returncode, proc.stdout, proc.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, "", str(exc)

    # ---- checks -----------------------------------------------------------

    def kit(self):
        self.section = "Kit"
        manifest = install.read_manifest(self.root)
        if manifest is None:
            if os.path.isdir(os.path.join(self.root, ".github/hooks/tests")):
                self.add(INFO, "this is the kit's own repository (no install manifest)")
            else:
                self.add(FAIL, "no install manifest found", "run the kit's `./agentwf install <this repo>`")
            return
        self.add(OK, "devgent %s installed" % manifest.get("version"))
        changed = install.modified_shared_files(self.root)
        for rel, what in changed:
            self.add(FAIL if what == "missing" else WARN, "shared file %s: %s" % (what, rel),
                     "run `./agentwf update --force` to restore it; change shared files in the kit, not here")
        if not changed:
            self.add(OK, "%d shared files match the installed version" % len(manifest.get("files", {})))
        if self.offline or not manifest.get("commit") or os.path.isdir(manifest.get("source") or ""):
            return
        code, out, _ = self._run(["git", "ls-remote", manifest["source"], "HEAD"], timeout=10)
        latest = out.split()[0] if code == 0 and out.split() else None
        if latest is None:
            self.add(WARN, "cannot reach the kit at %s to check for updates" % manifest["source"])
        elif latest != manifest["commit"]:
            self.add(WARN, "a newer kit version is available (%s)" % latest[:10], "run `./agentwf update`")
        else:
            self.add(OK, "kit is up to date")

    def settings(self):
        self.section = "Configuration"
        try:
            loaded = config.load_settings(self.root)
            self.add(OK, "%s loads (slice cap %d, %d tier rules)"
                     % (config.SETTINGS_PATH, loaded.slice_cap, len(loaded.tier_rules)))
        except config.ConfigError as exc:
            self.add(FAIL, str(exc), "fix %s; the kit's default is a working example" % config.SETTINGS_PATH)
            loaded = None
        if loaded and not loaded.production_hosts:
            self.add(WARN, "production_hosts is empty, so no production endpoint is blocked",
                     "list your production host names in %s" % config.SETTINGS_PATH)
        text = self._read(".github/copilot-instructions.md")
        if text is None:
            self.add(FAIL, ".github/copilot-instructions.md is missing", "run `./agentwf update`")
        elif "TODO" in text:
            self.add(WARN, ".github/copilot-instructions.md still has %d TODO placeholders" % text.count("TODO"),
                     "fill in the stack, database and error-handling conventions")
        else:
            self.add(OK, ".github/copilot-instructions.md is filled in")
        # GitHub uses the first CODEOWNERS it finds, in this order
        place = next((p for p in install.CODEOWNERS_PLACES if self._read(p) is not None), None)
        owners = self._read(place) if place else ""
        active = [l for l in owners.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        shadowed = [p for p in install.CODEOWNERS_PLACES if p != place and self._read(p) is not None]
        if shadowed:
            self.add(WARN, "%s takes precedence, so GitHub ignores %s" % (place, ", ".join(shadowed)),
                     "keep one CODEOWNERS file")
        if not active:
            self.add(WARN, "CODEOWNERS has no active rules (needed for T2 code-owner approval, Phase 2)",
                     "replace the placeholders in %s and uncomment the rules" % (place or ".github/CODEOWNERS"))
        elif any("@your-org/" in l for l in active):
            self.add(FAIL, "CODEOWNERS still names the placeholder @your-org teams", "replace them with real owners")
        else:
            self.add(OK, "CODEOWNERS has %d active rules" % len(active))
        return loaded

    def hook(self):
        self.section = "Local hook"
        path = ".github/hooks/agent-workflow.json"
        try:
            entries = json.loads(self._read(path) or "")["hooks"]["preToolUse"]
            command = entries[0]["bash"]
        except (ValueError, KeyError, IndexError, TypeError):
            self.add(FAIL, "%s is missing or invalid" % path, "run `./agentwf update --force`")
            return
        if os.name == "nt":
            self.add(INFO, "hook is registered (live test skipped on Windows)")
            return
        payload = json.dumps({"toolName": "bash", "cwd": self.root,
                              "toolArgs": json.dumps({"command": "git push --force origin main"})})
        code, out, err = self._run(["bash", "-c", command], stdin=payload)
        try:
            denied = json.loads(out).get("permissionDecision") == "deny"
        except ValueError:
            denied = False
        if code == 0 and denied:
            self.add(OK, "preToolUse hook runs and blocks a force push")
        else:
            self.add(FAIL, "preToolUse hook did not block a test force push (exit %d) %s" % (code, err.strip()[-200:]),
                     "run the hook command from %s by hand to see the error" % path)

    def tools(self):
        self.section = "Developer machine"
        if sys.version_info < (3, 8):
            self.add(FAIL, "Python %d.%d is too old" % sys.version_info[:2], "install Python 3.8 or newer")
        else:
            self.add(OK, "Python %d.%d" % sys.version_info[:2])
        if not shutil.which("copilot"):
            self.add(WARN, "Copilot CLI (`copilot`) is not on PATH", "install GitHub Copilot CLI")
        else:
            self.add(OK, "Copilot CLI found")
        if not shutil.which("gh"):
            self.add(FAIL, "the gh CLI is not installed; the plan-approval gate cannot work",
                     "install it from https://cli.github.com and run `gh auth login`")
            return False
        if self.offline:
            self.add(OK, "gh CLI found (login not checked: offline)")
            return False
        code, _, _ = self._run(["gh", "auth", "status"])
        if code != 0:
            self.add(FAIL, "gh is not logged in; the plan-approval gate cannot work", "run `gh auth login`")
            return False
        self.add(OK, "gh CLI is logged in")
        return True

    def github(self):
        self.section = "GitHub"
        code, out, err = self._run(["gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"])
        if code != 0:
            self.add(WARN, "this repository has no GitHub remote gh can see; branch protection not checked",
                     "push the repository to GitHub")
            return
        info = json.loads(out)
        repo, branch = info["nameWithOwner"], (info.get("defaultBranchRef") or {}).get("name") or "main"
        found = self._protection(repo, branch)
        if found is None:
            return
        label = "default branch `%s`" % branch
        where = "repository Settings > Branches (or Rules)"
        checks = (
            (found["reviews"] >= 1, "requires a pull request with an approving review",
             "require a pull request with at least 1 approval"),
            (found["dismiss_stale"], "dismisses stale approvals on new commits",
             "turn on 'Dismiss stale pull request approvals when new commits are pushed'"),
            (REQUIRED_CHECK in found["checks"], "requires the `%s` status check" % REQUIRED_CHECK,
             "add `%s` to the required status checks (it appears after the workflow has run once)" % REQUIRED_CHECK),
            (found["no_force_push"], "blocks force pushes", "block force pushes"),
        )
        for passed, text, fix in checks:
            if passed:
                self.add(OK, "%s %s" % (label, text))
            else:
                self.add(FAIL, "%s does not: %s" % (label, text), "%s: %s" % (where, fix))

    def _protection(self, repo, branch):
        """Merged view of classic branch protection and rulesets."""
        found = {"reviews": 0, "dismiss_stale": False, "checks": set(), "no_force_push": False}
        seen = False
        code, out, err = self._run(["gh", "api", "repos/%s/branches/%s/protection" % (repo, branch)])
        if code == 0:
            seen = True
            data = json.loads(out)
            reviews = data.get("required_pull_request_reviews") or {}
            found["reviews"] = reviews.get("required_approving_review_count") or 0
            found["dismiss_stale"] = bool(reviews.get("dismiss_stale_reviews"))
            status = data.get("required_status_checks") or {}
            found["checks"] |= set(status.get("contexts") or [])
            found["checks"] |= {c.get("context") for c in status.get("checks") or []}
            found["no_force_push"] = not (data.get("allow_force_pushes") or {}).get("enabled", False)
        classic_unreadable = code != 0 and "Branch not protected" not in err + out
        code, out, _ = self._run(["gh", "api", "repos/%s/rules/branches/%s" % (repo, branch)])
        if code == 0:
            for rule in json.loads(out):
                seen = True
                params = rule.get("parameters") or {}
                if rule.get("type") == "pull_request":
                    found["reviews"] = max(found["reviews"], params.get("required_approving_review_count") or 0)
                    found["dismiss_stale"] |= bool(params.get("dismiss_stale_reviews_on_push"))
                elif rule.get("type") == "required_status_checks":
                    found["checks"] |= {c.get("context") for c in params.get("required_status_checks") or []}
                elif rule.get("type") == "non_fast_forward":
                    found["no_force_push"] = True
        if not seen and classic_unreadable:
            # Without admin rights GitHub hides the protection settings. The branch
            # summary still says whether it is protected and which checks it requires.
            code, out, _ = self._run(["gh", "api", "repos/%s/branches/%s" % (repo, branch)])
            summary = json.loads(out) if code == 0 else {}
            if summary.get("protected"):
                required = ((summary.get("protection") or {}).get("required_status_checks") or {}).get("contexts") or []
                level = OK if REQUIRED_CHECK in required else FAIL
                self.add(level, "default branch `%s` is protected; `%s` check %s"
                         % (branch, REQUIRED_CHECK, "is required" if level == OK else "is not required"),
                         "add `%s` to the required status checks" % REQUIRED_CHECK)
                self.add(WARN, "cannot read the review and force-push rules for `%s` (needs admin access)" % branch,
                         "ask a repository admin to run `./agentwf doctor`")
                return None
        if not seen:
            self.add(FAIL, "default branch `%s` is not protected: CI and reviews are not enforced" % branch,
                     "protect it in repository Settings > Branches: require a pull request with 1 approval, "
                     "dismiss stale approvals, require the `%s` check, block force pushes" % REQUIRED_CHECK)
            return None
        return found

    def ticket(self, settings):
        self.section = "Current ticket"
        branch = tk.git(self.root, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "(none)"
        active = tk.resolve_ticket(self.root, settings)
        if active is None:
            self.add(INFO, "branch `%s` has no ticket" % branch)
            return
        plan, log = active.plan(), active.log()
        errors = ["plan.md is missing"] if plan is None else tk.check_front_matter(plan, active.id)
        if log.state not in tk.STATES:
            errors.append("log.md line 1 must be `state: <STATE>`")
        for error in errors:
            self.add(WARN, "%s: %s" % (active.id, error), "see docs/agent-work/_templates")
        if not errors:
            self.add(OK, "%s is %s, state %s" % (active.id, plan.tier, log.state))

    def _read(self, rel):
        try:
            with open(os.path.join(self.root, rel), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    # ---- driver -----------------------------------------------------------

    def run(self):
        self.kit()
        settings = self.settings()
        self.hook()
        if self.tools():
            self.github()
        elif not self.offline:
            self.section = "GitHub"
            self.add(WARN, "branch protection not checked (gh is unavailable)")
        if settings:
            self.ticket(settings)
        return self

    def count(self, level):
        return sum(1 for _, lvl, _, _ in self.results if lvl == level)

    def text(self):
        out, section = [], None
        for sec, level, message, fix in self.results:
            if sec != section:
                out.append("\n%s" % sec)
                section = sec
            out.append("  %-5s %s" % (level, message))
            if fix and level in (WARN, FAIL):
                out.append("        fix: %s" % fix)
        out.append("\n%d problems, %d warnings" % (self.count(FAIL), self.count(WARN)))
        return "\n".join(out).lstrip("\n")

    def todo(self):
        """Numbered list of what is left to do, problems first."""
        items = [(lvl, msg, fix) for _, lvl, msg, fix in self.results if lvl == FAIL]
        items += [(lvl, msg, fix) for _, lvl, msg, fix in self.results if lvl == WARN]
        return "\n".join("  %d. %s\n     %s" % (i, msg, fix or "") for i, (_, msg, fix) in enumerate(items, 1))
