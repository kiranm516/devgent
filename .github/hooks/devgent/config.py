"""Settings loader, glob matching and tier path rules.

Standard library only: hooks run on developer machines where PyYAML may be
missing, so `.github/devgent.yml` is read with a small parser that
accepts the subset of YAML the settings file uses and rejects anything else.
"""
import os
import re

SETTINGS_PATH = ".github/devgent.yml"
TIERS = ("T0", "T1", "T2")


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------
# YAML subset: nested maps, block lists, flow lists of scalars, scalars.
# --------------------------------------------------------------------------

_KEY = re.compile(r"^([A-Za-z_][\w-]*):(?:\s+(.*))?$")


def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p) for p in _split_flow(inner)] if inner else []
    if text and text[0] in "[{&*!|>":
        raise ConfigError("unsupported YAML syntax: %r" % text)
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _split_flow(inner):
    parts, cur, quote = [], "", None
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def parse_yaml(text):
    lines = []
    for raw in text.splitlines():
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ConfigError("tabs are not allowed for indentation")
        body = _strip_comment(raw).rstrip()
        if body.strip():
            lines.append([len(body) - len(body.lstrip()), body.strip()])
    if not lines:
        return {}
    value, i = _block(lines, 0, lines[0][0])
    if i != len(lines):
        raise ConfigError("unexpected indentation near %r" % lines[i][1])
    return value


def _block(lines, i, indent):
    if lines[i][1] == "-" or lines[i][1].startswith("- "):
        return _list(lines, i, indent)
    return _map(lines, i, indent)


def _list(lines, i, indent):
    out = []
    while i < len(lines) and lines[i][0] == indent and (
        lines[i][1] == "-" or lines[i][1].startswith("- ")
    ):
        text = lines[i][1]
        rest = text[1:].lstrip()
        if not rest:
            if i + 1 >= len(lines) or lines[i + 1][0] <= indent:
                raise ConfigError("empty list item")
            value, i = _block(lines, i + 1, lines[i + 1][0])
        elif _KEY.match(rest):
            lines[i] = [indent + len(text) - len(rest), rest]
            value, i = _map(lines, i, lines[i][0])
        else:
            value, i = _scalar(rest), i + 1
        out.append(value)
    return out, i


def _map(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        m = _KEY.match(lines[i][1])
        if not m:
            raise ConfigError("expected 'key: value', got %r" % lines[i][1])
        key, rest = m.group(1), m.group(2)
        if key in out:
            raise ConfigError("duplicate key %r" % key)
        if rest is None or not rest.strip():
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            if nxt and nxt[0] > indent:
                out[key], i = _block(lines, i + 1, nxt[0])
            elif nxt and nxt[0] == indent and (nxt[1] == "-" or nxt[1].startswith("- ")):
                out[key], i = _list(lines, i + 1, indent)
            else:
                out[key], i = None, i + 1
        else:
            out[key], i = _scalar(rest), i + 1
    return out, i


# --------------------------------------------------------------------------
# Globs: `**` crosses directories, `*` and `?` do not.
# --------------------------------------------------------------------------

_glob_cache = {}


def glob_regex(pattern):
    rx = _glob_cache.get(pattern)
    if rx is None:
        source, out, i = pattern, "", 0
        if pattern.endswith("/"):  # a directory means everything under it
            pattern += "**"
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out += "(?:.*/)?"
                i += 3
            elif pattern.startswith("**", i):
                out += ".*"
                i += 2
            elif pattern[i] == "*":
                out += "[^/]*"
                i += 1
            elif pattern[i] == "?":
                out += "[^/]"
                i += 1
            else:
                out += re.escape(pattern[i])
                i += 1
        rx = _glob_cache[source] = re.compile(out + r"\Z")
    return rx


def matches(path, patterns):
    return any(glob_regex(p).match(path) for p in patterns)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


class Settings:
    def __init__(self, data):
        try:
            self.artifact_root = str(data["artifact_root"]).strip("/")
            self.slice_cap = int(data["slice"]["max_production_lines"])
            self.production_paths = data["slice"]["production_paths"]
            self.max_objections = int(data["review"]["max_plan_objections"])
            self.max_fix_rounds = int(data["review"]["max_fix_rounds"])
            self.wip = dict(data.get("wip") or {})
            self.production_hosts = data.get("production_hosts") or []
            self.tier_rules = data["tier_rules"]
            if not isinstance(self.tier_rules, list):
                raise TypeError("tier_rules")
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("invalid %s: %r" % (SETTINGS_PATH, exc))
        _strings("slice.production_paths", self.production_paths)
        _strings("production_hosts", self.production_hosts)
        for rule in self.tier_rules:
            if not isinstance(rule, dict) or rule.get("min_tier") not in TIERS or not rule.get("name"):
                raise ConfigError("invalid tier rule: %r" % (rule,))
            _strings("patterns of tier rule %s" % rule["name"], rule.get("patterns"))
            if rule.get("when") not in (None, "breaking-api-change"):
                raise ConfigError("unknown 'when' in tier rule %s" % rule["name"])
            if rule.get("new_files_only") not in (None, True, False):
                raise ConfigError("new_files_only in tier rule %s must be true or false" % rule["name"])

    def is_production(self, path):
        return matches(path, self.production_paths)

    def ticket_of(self, path):
        """Ticket id if `path` is inside a ticket's artifact folder, else None."""
        prefix = self.artifact_root + "/"
        if not path.startswith(prefix):
            return None
        parts = path[len(prefix):].split("/")
        if len(parts) < 2 or parts[0].startswith("_") or not parts[0]:
            return None
        return parts[0]

    def tier_floor(self, changes, breaking_api_change=False):
        """Minimum tier for `changes`, an iterable of (path, is_new_file).

        Returns (tier, reasons); reasons name the rule and path that set it.
        """
        floor, reasons = "T0", []
        for path, is_new in changes:
            if self.ticket_of(path):
                continue
            for rule in self.tier_rules:
                if rule.get("new_files_only") and not is_new:
                    continue
                if rule.get("when") == "breaking-api-change" and not breaking_api_change:
                    continue
                if not matches(path, rule["patterns"]):
                    continue
                tier = rule["min_tier"]
                if TIERS.index(tier) > TIERS.index(floor):
                    floor, reasons = tier, []
                if tier == floor and floor != "T0":
                    reasons.append("%s: %s (%s)" % (tier, path, rule["name"]))
        return floor, reasons


def _strings(what, value):
    if not isinstance(value, list) or not value and what.startswith("patterns") \
            or not all(isinstance(v, str) and v for v in value):
        raise ConfigError("%s must be a list of strings" % what)


def load_settings(root, text=None):
    """Settings from the working tree, or from `text` (e.g. a base revision's copy)."""
    if text is None:
        path = os.path.join(root, SETTINGS_PATH)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise ConfigError("cannot read %s: %s" % (SETTINGS_PATH, exc))
    data = parse_yaml(text)
    if not isinstance(data, dict):
        raise ConfigError("%s must be a map of settings" % SETTINGS_PATH)
    return Settings(data)


def tier_at_least(tier, floor):
    return TIERS.index(tier) >= TIERS.index(floor)
