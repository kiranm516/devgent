"""Install or update the kit in a target repository.

Shared files are replaced on every run; per-repository files are created once
and never overwritten. A manifest records what was installed, from where, and
each shared file's hash, so `doctor` can spot local edits and `update` knows
where to fetch from.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import ticket as tk

MANIFEST = ".github/hooks/devgent/MANIFEST.json"
AGENTS_MARKER = "devgent:agents-md"

# Replaced on every install. Directories are replaced whole.
KIT_OWNED = (
    "devgent",
    "devgent.cmd",
    ".github/hooks/devgent.json",
    ".github/hooks/devgent",
    ".github/agents/scout.agent.md",
    ".github/agents/architect.agent.md",
    ".github/agents/implementer.agent.md",
    ".github/agents/reviewer.agent.md",
    ".github/skills/spring-rest-api-design",
    ".github/skills/spring-boot-testing",
    ".github/skills/secure-api-review",
    ".github/skills/minimal-change",
    ".github/workflows/agent-gates.yml",
    "docs/agent-work/_templates",
)
# Created if missing, then left alone: each repository tunes these.
REPO_OWNED = (
    ".github/devgent.yml",
    ".github/copilot-instructions.md",
    ".github/CODEOWNERS",
)


class InstallError(Exception):
    pass


def kit_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read_manifest(root):
    try:
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _kit_files(kit):
    """Relative paths of every shared file in the kit."""
    out = []
    for path in KIT_OWNED:
        full = os.path.join(kit, path)
        if os.path.isfile(full):
            out.append(path)
        for base, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                rel = os.path.relpath(os.path.join(base, name), kit).replace(os.sep, "/")
                if rel != MANIFEST and not name.endswith(".pyc"):
                    out.append(rel)
    return sorted(out)


def _origin(kit):
    """Where this kit came from: carried over from its own manifest when the
    kit is itself an installed copy, otherwise read from git."""
    own = read_manifest(kit)
    if own:
        return own.get("source"), own.get("commit"), own.get("version")
    source = tk.git(kit, "remote", "get-url", "origin", check=False) or kit
    source = re.sub(r"^(\w+://)[^/@]+@", r"\1", source)  # never record credentials
    commit = tk.git(kit, "rev-parse", "HEAD", check=False) or None
    version = tk.git(kit, "describe", "--tags", "--always", "--dirty", check=False) or "unknown"
    return source, commit, version


CODEOWNERS_PLACES = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def _owned(rel):
    """True if `rel` is a safe, kit-owned path: the only kind install may delete."""
    if os.path.isabs(rel) or ".." in rel.split("/"):
        return False
    return rel == "AGENTS.md" or any(rel == p or rel.startswith(p + "/") for p in KIT_OWNED)


def conflicts(target, kit, previous):
    """Files install would destroy that the kit did not put there: on a first
    install, anything already at a kit-owned path with different content; on any
    install, files the repository added inside a kit-owned directory."""
    known = set((previous or {}).get("files", {}))
    shipped = set(_kit_files(kit))
    out = []
    for path in KIT_OWNED:
        full = os.path.join(target, path)
        if os.path.islink(full):
            out.append((path, "is a symbolic link"))
            continue
        found = [path] if os.path.isfile(full) else []
        for base, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            found += [os.path.relpath(os.path.join(base, n), target).replace(os.sep, "/")
                      for n in files if not n.endswith(".pyc")]
        for rel in found:
            if rel == MANIFEST or rel in known:
                continue
            if rel not in shipped:
                out.append((rel, "not part of the kit; it would be deleted"))
            elif sha256(os.path.join(target, rel)) != sha256(os.path.join(kit, rel)):
                out.append((rel, "already exists with different content; it would be replaced"))
    return sorted(out)


def install(target, kit=None, force=False):
    """Copy the kit into `target`. Returns a summary dict."""
    kit = kit or kit_root()
    top = tk.repo_root(target) if os.path.isdir(target) else None
    if not top:
        raise InstallError("%s is not a git repository" % target)
    target = top
    if os.path.realpath(target) == os.path.realpath(kit):
        raise InstallError("the target is the kit itself")

    previous = read_manifest(target)
    clashes = conflicts(target, kit, previous)
    if clashes and not force:
        raise InstallError(
            "these files in %s are not the kit's and would be lost:\n%s\n"
            "Move or commit them elsewhere, or rerun with --force to let the kit replace them."
            % (target, "\n".join("  %s (%s)" % c for c in clashes)))
    old_hashes = {}
    for rel in set(_kit_files(kit)) | set((previous or {}).get("files", {})):
        full = os.path.join(target, rel)
        if os.path.isfile(full):
            old_hashes[rel] = sha256(full)

    for path in KIT_OWNED:
        dest = os.path.join(target, path)
        if os.path.islink(dest) or os.path.isfile(dest):
            os.remove(dest)
        elif os.path.isdir(dest):
            shutil.rmtree(dest)
    for rel in (previous or {}).get("files", {}):  # files the kit no longer ships
        full = os.path.join(target, rel)
        if _owned(rel) and rel != "AGENTS.md" and os.path.isfile(full):
            os.remove(full)

    files = {}
    for rel in _kit_files(kit):
        dest = os.path.join(target, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(os.path.join(kit, rel), dest)
        files[rel] = sha256(dest)

    summary = {"created": [], "kept": [], "agents_md": "installed"}
    for rel in REPO_OWNED:
        dest = os.path.join(target, rel)
        if rel.endswith("CODEOWNERS") and not os.path.exists(dest):
            # GitHub reads the first of .github/, root, docs/: never shadow an existing one
            existing = [p for p in CODEOWNERS_PLACES if os.path.exists(os.path.join(target, p))]
            if existing:
                summary["codeowners"] = existing[0]
                continue
        if os.path.exists(dest):
            if sha256(dest) != sha256(os.path.join(kit, rel)):
                summary["kept"].append(rel)
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(kit, rel), dest)
            summary["created"].append(rel)

    # AGENTS.md is shared only if the kit wrote it (it carries the marker).
    agents = os.path.join(target, "AGENTS.md")
    existing = None
    if os.path.exists(agents):
        with open(agents, encoding="utf-8") as fh:
            existing = fh.read()
    if existing is None or AGENTS_MARKER in existing:
        shutil.copy2(os.path.join(kit, "AGENTS.md"), agents)
        files["AGENTS.md"] = sha256(agents)
    else:
        summary["agents_md"] = "kept"

    source, commit, version = _origin(kit)
    manifest = {"version": version, "source": source, "commit": commit, "files": files}
    with open(os.path.join(target, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")

    summary.update(
        target=target, version=version,
        previous=previous and previous.get("version"),
        added=sorted(f for f in files if f not in old_hashes),
        updated=sorted(f for f in files if f in old_hashes and old_hashes[f] != files[f]),
        removed=sorted(f for f in old_hashes if f not in files),
    )
    return summary


def describe(summary, kit=None):
    kit = kit or kit_root()
    out = []
    if summary["previous"]:
        out.append("Updated devgent %s -> %s in %s" % (summary["previous"], summary["version"], summary["target"]))
    else:
        out.append("Installed devgent %s in %s" % (summary["version"], summary["target"]))
    out.append("  shared files: %d added, %d updated, %d removed"
               % (len(summary["added"]), len(summary["updated"]), len(summary["removed"])))
    for rel in summary["updated"] + summary["removed"]:
        out.append("    %s %s" % ("updated" if rel in summary["updated"] else "removed", rel))
    for rel in summary["created"]:
        out.append("  created %s (yours from now on)" % rel)
    for rel in summary["kept"]:
        out.append("  kept    %s (yours; it differs from the kit's default)" % rel)
    if summary.get("codeowners"):
        out.append("  kept    %s (yours; add owners for the paths listed in %s)"
                   % (summary["codeowners"], os.path.join(kit, ".github/CODEOWNERS")))
    if summary["agents_md"] == "kept":
        out.append("  kept    AGENTS.md (not written by the kit; merge in the rules from %s)"
                   % os.path.join(kit, "AGENTS.md"))
    return "\n".join(out)


def fetch_kit(source):
    """Directory holding the kit at `source` (a local path or a git URL), and a
    cleanup callable."""
    if os.path.isdir(source):
        return os.path.abspath(source), lambda: None
    tmp = tempfile.mkdtemp(prefix="devgent-")
    proc = subprocess.run(["git", "clone", "--quiet", "--depth", "1", source, tmp],
                          stderr=subprocess.PIPE, universal_newlines=True)
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError("cannot fetch the kit from %s: %s" % (source, proc.stderr.strip()))
    return tmp, lambda: shutil.rmtree(tmp, ignore_errors=True)


def modified_shared_files(root):
    """Shared files edited or deleted since the kit was installed."""
    manifest = read_manifest(root) or {}
    out = []
    for rel, digest in sorted(manifest.get("files", {}).items()):
        if not _owned(rel):
            continue
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            out.append((rel, "missing"))
        elif sha256(full) != digest:
            out.append((rel, "modified"))
    return out
