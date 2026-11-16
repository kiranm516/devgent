"""`./devgent view`: one generated, never-committed HTML page for a ticket.

Everything on it is derived from plan.md, log.md and review.md, so it cannot
drift from what was approved: what needs a human answer, the decisions, the
plan's Mermaid diagrams, the requirement-to-test trace, slices against the
change map, and the review findings. Agents never write this page.

`./devgent view --functional` writes a second page with the plan's functional
layer only, for the people who clarify behaviour (BA, PO, QE) and do not read
the pull request: no files, slices or review, and nothing to sign.
"""
import html
import os
import re
import webbrowser

from . import config, ticket as tk

MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"


def md(text):
    """Inline markdown only: code and bold. An id (R-01, SC-02, S-01, D-01, Q-01, O-01, F-01)
    becomes a link to where the page shows it."""
    text = html.escape(text or "")
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\b((?:R|SC|S|D|Q|O|F)-\d+)\b", r'<a class="ref" href="#\1">\1</a>', text)


def _letter(text, options):
    """The option letter a recommendation or an answer starts with, if any."""
    m = re.match(r"\W*([A-Za-z])\b", text or "")
    letter = m.group(1).upper() if m else None
    return letter if letter in [o[0] for o in options] else None


def _clean(cell):
    return cell.strip().strip("*_`").strip()


def _cells(line):
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _is_block_start(line):
    return bool(re.match(r"\s*(```|\||#{1,6}\s|([-*]|\d+[.)])\s)", line))


def _blocks(text):
    """Block markdown for a plan's prose: paragraphs, lists, tables, sub-headings,
    fenced code and Mermaid. Everything is escaped; plan text is never markup."""
    out, lines, i = [], (text or "").splitlines(), 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
        elif line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            code = html.escape("\n".join(lines[i + 1:j]))
            out.append(('<div class="card wide"><pre class="mermaid">%s</pre></div>'
                        if line[3:].strip().lower() == "mermaid" else "<pre>%s</pre>") % code)
            i = j + 1
        elif line.startswith("|"):
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            rows = [_cells(row) for row in lines[i:j]]
            body = [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r if c)]
            out.append('<div class="wide"><table><tr>%s</tr>%s</table></div>' % (
                "".join("<th>%s</th>" % md(c) for c in rows[0]),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % md(c) for c in r) for r in body)))
            i = j
        elif re.match(r"#{1,6}\s", line):
            out.append("<h3>%s</h3>" % md(line.lstrip("#").strip()))
            i += 1
        elif re.match(r"([-*]|\d+[.)])\s", line):
            tag, items = ("ol" if line[0].isdigit() else "ul"), []
            while i < len(lines) and lines[i].strip():
                bullet = re.match(r"\s*(?:[-*]|\d+[.)])\s+(.*)", lines[i])
                if bullet:
                    items.append(bullet.group(1))
                elif _is_block_start(lines[i]):
                    break
                else:  # continuation of the item above
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % md(item) for item in items), tag))
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not (para and _is_block_start(lines[i])):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>%s</p>" % md(" ".join(para)))
    return "".join(out)


def _without_first_table(text):
    """`text` minus its first markdown table, which the page draws itself."""
    out, state = [], "before"
    for line in (text or "").splitlines():
        is_row = line.strip().startswith("|")
        if state == "before" and is_row:
            state = "inside"
        elif state == "inside" and not is_row:
            state = "after"
        if state != "inside":
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------
# Model: everything the page shows, read from the three ticket files
# --------------------------------------------------------------------------


class Model:
    def __init__(self, settings, ticket):
        self.ticket = ticket
        self.plan, self.log = ticket.plan(), ticket.log()
        review_text = ticket.read("review.md")
        review = tk.sections(tk.strip_comments(review_text or ""))
        sec = self.plan.sections

        self.reqs = re.findall(r"^\s*[-*]\s+\**(R-\d+)\**\s*[:.]?\s*(.*)", sec.get("requirements", ""), flags=re.M)
        self.scenarios = []  # (id, text, {R ids})
        self.expected = {}   # scenario id -> expected result
        for row in tk.table_rows(sec.get("scenarios", "")):
            m = re.search(r"SC-\d+", row.get("id", ""))
            if m:
                covers = set(re.findall(r"R-\d+", " ".join(row.values())))
                self.scenarios.append((m.group(0), row.get("scenario", ""), covers))
                self.expected[m.group(0)] = row.get("expected result", "")
        self.tests = []  # ({SC ids}, level, test)
        for row in tk.table_rows(sec.get("test matrix", "")):
            ids = set(re.findall(r"SC-\d+", row.get("scenario", "")))
            if ids and not tk._blank(row.get("test", "")):
                self.tests.append((ids, _clean(row.get("level", "")), _clean(row.get("test", ""))))
        self.slices = []  # (id, goal, {R and SC ids}, paths, status)
        for title in self.plan.slice_blocks:
            m = re.match(r"(s-\d+)\b[\s:.-]*(.*)", title)
            if m:
                sid = m.group(1).upper()
                self.slices.append((sid, m.group(2), self.plan.slice_covers.get(sid, set()),
                                    self.plan.slices.get(sid, []), self.log.slice_status.get(sid, "not started")))
        self.change_map = tk.table_rows(sec.get("change map", ""))
        self.questions = tk.table_rows(sec.get("open questions", ""))
        self.objections = [r for r in tk.table_rows(review.get("plan objections", ""))
                           if re.search(r"O-\d+", r.get("id", ""))]
        self.rounds = []  # (title, reviewed head, rows)
        for title, body in tk.sections(review.get("diff findings", ""), level=3).items():
            head = re.search(r"Reviewed head:\s*`?([0-9a-f]{7,40})", body)
            self.rounds.append((title.capitalize(), head.group(1)[:10] if head else "", tk.table_rows(body)))
        self.brief = review.get("review brief", "")
        self.errors = tk.check_plan_structure(self.plan)
        if self.plan.tier != "T0":
            self.errors += tk.check_review_structure(review_text, settings)

    def queue(self):
        """What is waiting for a human, as (kind, id, text, how to answer)."""
        out = []
        for d in self.plan.decisions:
            if tk._blank(d.get("answer", "")):
                out.append(("decision", d["id"], d["title"], "pick an option below"))
        for row in self.objections:
            if _clean(row.get("disposition", "")).lower() not in tk.DISPOSITIONS:
                out.append(("objection", re.search(r"O-\d+", row["id"]).group(0), row.get("objection", ""),
                            "write accepted, rejected (with a reason) or deferred (to a person) in review.md"))
        for row in self.questions:
            if (row.get("blocking", "").strip().lower().startswith("y")
                    and row.get("status", "").strip().lower() not in ("answered", "closed", "risk-accepted")):
                layer = tk.layer_tag(row.get("layer"))
                out.append(("question", row.get("id", "?"), row.get("question", ""),
                            "owner: %s%s" % (row.get("owner") or "nobody named", ", %s layer" % layer if layer else "")))
        for sid, goal, _, _, status in self.slices:
            if status == "awaiting-review":
                out.append(("slice", sid, goal, "look at it, then `./devgent slice accept %s`" % sid))
        for title, _, rows in self.rounds[-1:]:
            for row in rows:
                if _clean(row.get("action", "")).lower() == "decide" and _clean(row.get("status", "")).lower() == "open":
                    out.append(("finding", row.get("id", "?"),
                                "%s: %s" % (row.get("file:line", ""), row.get("failure scenario", "")),
                                "write `fix` or `won't fix: reason` in its Status cell in review.md"))
        return out


# --------------------------------------------------------------------------
# References: the files the plan cites as evidence, linked where they resolve
# --------------------------------------------------------------------------


def _web_base(root):
    """https://host/owner/repo for the origin remote, or None when it is not a hosted one."""
    url = tk.git(root, "remote", "get-url", "origin", check=False)
    m = re.match(r"(?:https?://(?:[^@/]+@)?|ssh://git@|git@)([A-Za-z0-9.-]+\.[A-Za-z]{2,})[/:](.+?)(?:\.git)?/?$", url)
    return "https://%s/%s" % (m.group(1), m.group(2)) if m else None


def references(root, plan):
    """[(reference as written, link or None, [sections citing it])] for every file
    the plan names. A short name (`OrderService.java:112`) resolves when exactly one tracked
    file ends with it. The link goes to the plan's base commit on the remote, where the
    line numbers were read; to the local file when there is no hosted remote."""
    cited = {}
    for name, text in tk.sections(plan.body).items():
        for ref in tk.CITED_REF.findall(text):
            if "*" not in ref and name not in cited.setdefault(ref, []):
                cited[ref].append(name)
    if not cited:
        return []
    files = tk.git(root, "ls-files", check=False).splitlines()
    web, base = _web_base(root), plan.meta.get("base_commit", "")
    out = []
    for ref, where in cited.items():
        path, _, line = ref.partition(":")
        hits = [f for f in files if f == path or f.endswith("/" + path)]
        link = None
        if len(hits) == 1:
            at_base = base and tk.git(root, "rev-parse", "--verify", "--quiet", "%s:%s" % (base, hits[0]), check=False)
            if web and at_base:
                link = "%s/blob/%s/%s%s" % (web, base, hits[0], "#L" + line if line else "")
            else:
                link = "file://" + os.path.join(os.path.abspath(root), hits[0])
        out.append((ref, link, where))
    return out


def _references_html(model):
    refs = references(model.ticket.root, model.plan)
    if not refs:
        return "<p class='muted'>The plan cites no files.</p>"
    rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (
        '<a href="%s"><code>%s</code></a>' % (html.escape(link, quote=True), html.escape(ref)) if link
        else "<code>%s</code> <small>new, or not one file in this checkout</small>" % html.escape(ref),
        html.escape(", ".join(w.capitalize() for w in where))) for ref, link, where in refs)
    return '<div class="wide"><table><tr><th>File</th><th>Cited in</th></tr>%s</table></div>' % rows


# --------------------------------------------------------------------------
# Sections of the page
# --------------------------------------------------------------------------


def _queue_html(model):
    items = model.queue()
    if not items:
        return '<p class="ok">Nothing is waiting for a human answer.</p>'
    rows = "".join(
        '<li><span class="pill warn">%s</span> <a href="#%s"><b>%s</b></a> %s<br><small>%s</small></li>'
        % (kind, html.escape(ident), html.escape(ident), md(text), md(how))
        for kind, ident, text, how in items)
    return "<ol class='queue'>%s</ol>" % rows


def _layer_pill(text):
    layer = tk.layer_tag(text)
    return ' <span class="pill layer">%s</span>' % layer if layer else ""


def _decisions_html(model, layer=None):
    """Decision cards. With `layer`, only that layer's decisions, to read and not to answer."""
    decisions = [d for d in model.plan.decisions if layer is None or tk.layer_tag(d.get("layer")) == layer]
    if not decisions:
        return "<p class='muted'>The plan lists no decisions.</p>"
    cards, pending = [], False
    for d in decisions:
        answered = not tk._blank(d.get("answer", ""))
        pending = pending or not answered
        rec, chosen = _letter(d.get("recommendation"), d["options"]), _letter(d.get("answer"), d["options"])
        options = []
        for letter, text in d["options"]:
            marks = ('<span class="pill rec">recommended</span>' if letter == rec else "") + (
                '<span class="pill ok">chosen</span>' if answered and letter == chosen else "")
            pick = "" if answered or layer else '<input type="radio" name="%s" value="%s"> ' % (d["id"], letter)
            options.append('<label class="option%s">%s<b>%s</b> %s %s</label>'
                           % (" chosen" if answered and letter == chosen else "", pick, letter, md(text), marks))
        facts = "".join(
            "<dt>%s</dt><dd>%s</dd>" % (label, md(d[key]))
            for key, label in (("why a human", "Why a human"), ("recommendation", "Recommendation"),
                               ("reversible", "Reversible")) if d.get(key))
        tail = ('<p class="answer"><b>Answer:</b> %s</p>' % md(d["answer"]) if answered else
                '<p class="muted">Not answered yet.</p>' if layer else
                '<input type="text" placeholder="Optional note for the record" aria-label="Note for %s">' % d["id"])
        cards.append('<section class="card%s" id="%s" data-decision="%s"><h3>%s <span>%s</span>%s</h3>%s<dl>%s</dl>%s</section>'
                     % ("" if answered else " open", d["id"], "" if answered or layer else d["id"], d["id"],
                        md(d["title"]), "" if layer else _layer_pill(d.get("layer")), "".join(options), facts, tail))
    if pending and not layer:
        cards.append(
            '<div class="card"><p>Run these from the repository root, commit <code>plan.md</code>, then approve. '
            'Or type the letter on each <code>- Answer:</code> line yourself.</p>'
            '<pre id="cmds">Pick an option above.</pre><button id="copy" type="button">Copy commands</button></div>')
    return "".join(cards)


def _trace_svg(model):
    """Requirements -> scenarios -> tests as three columns joined by curves.
    A node with nothing downstream is a gap and is drawn as one."""
    if not (model.reqs or model.scenarios):
        return "<p class='muted'>No requirements or scenarios yet.</p>"
    w, h, gap, xs = 300, 30, 8, (0, 330, 660)

    def slices_for(ident):
        return [s[0] for s in model.slices if ident in s[2]]

    cols = [
        [(r, text, any(r in sc[2] for sc in model.scenarios), slices_for(r)) for r, text in model.reqs],
        [(sc, text, any(sc in t[0] for t in model.tests), slices_for(sc)) for sc, text, _ in model.scenarios],
        [(level or "test", name, True, []) for _, level, name in model.tests],
    ]
    y = lambda i: 24 + i * (h + gap)
    edges = []
    for i, (r, _) in enumerate(model.reqs):
        edges += [(0, i, j) for j, sc in enumerate(model.scenarios) if r in sc[2]]
    for i, (sc, _, _) in enumerate(model.scenarios):
        edges += [(1, i, j) for j, t in enumerate(model.tests) if sc in t[0]]
    parts = []
    for col, i, j in edges:
        x1, y1, x2, y2 = xs[col] + w, y(i) + h / 2, xs[col + 1], y(j) + h / 2
        parts.append('<path d="M%d %.0f C%d %.0f %d %.0f %d %.0f"/>' % (x1, y1, x1 + 15, y1, x2 - 15, y2, x2, y2))
    for x, title in zip(xs, ("Requirements", "Scenarios", "Tests")):
        parts.append('<text class="head" x="%d" y="12">%s</text>' % (x, title))
    for col, nodes in enumerate(cols):
        for i, (ident, text, linked, owners) in enumerate(nodes):
            label = "%s  %s" % (ident, text)
            tag = " ".join(owners)
            room = 40 - len(tag) - (1 if tag else 0)
            short = label if len(label) <= room else label[:room - 1] + "…"
            parts.append(
                '<g class="%s"><title>%s</title><rect x="%d" y="%d" width="%d" height="%d" rx="5"/>'
                '<text x="%d" y="%d">%s</text>%s</g>'
                % ("node" if linked else "node gap", html.escape(label + ("" if linked else " (nothing covers this)")),
                   xs[col], y(i), w, h, xs[col] + 8, y(i) + 19, html.escape(short),
                   '<text class="tag" x="%d" y="%d" text-anchor="end">%s</text>' % (xs[col] + w - 8, y(i) + 19, tag)
                   if tag else ""))
    height = y(max(len(c) for c in cols)) + 4
    gaps = sum(1 for c in cols for n in c if not n[2])
    note = ('<p class="bad">%d gap%s: a requirement with no scenario, or a scenario with no test.</p>'
            % (gaps, "" if gaps == 1 else "s")) if gaps else ""
    return '%s<svg class="trace" viewBox="0 0 960 %d" role="img" aria-label="Requirement to test trace">%s</svg>' % (
        note, height, "".join(parts))


def _slices_html(model):
    if not model.slices:
        return "<p class='muted'>No slices yet.</p>"
    cls = {"accepted": "ok", "started": "rec", "awaiting-review": "warn"}
    cards = "".join(
        '<li id="%s"><span class="pill %s">%s</span> <b>%s</b> %s<br><small>%s</small></li>'
        % (sid, cls.get(status, ""), status, sid, md(goal), " ".join("<code>%s</code>" % html.escape(p) for p in paths))
        for sid, goal, _, paths, status in model.slices)
    rows = []
    for row in model.change_map:
        path = _clean(row.get("file", ""))
        owners = [s[0] for s in model.slices if config.matches(path, s[3])]
        rows.append("<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (
            html.escape(path), md(row.get("change", "")),
            " ".join(owners) or '<span class="bad">no slice may write this</span>'))
    table = ("<table><tr><th>File in the change map</th><th>Change</th><th>Slice</th></tr>%s</table>" % "".join(rows)
             if rows else "")
    return "<ol class='queue'>%s</ol>%s" % (cards, table)


def _review_html(model):
    out = []
    if model.objections:
        rows = "".join(
            '<tr id="%s"><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td></tr>' % (
                html.escape(re.search(r"O-\d+", r["id"]).group(0)), md(r["id"]), md(r.get("objection", "")),
                _layer_pill(r.get("layer")),
                md(r.get("disposition", "")) or '<span class="pill warn">needs you</span>',
                md(r.get("reason or owner", "")))
            for r in model.objections)
        out.append("<h3>Plan objections</h3><table><tr><th>ID</th><th>Objection</th><th>Disposition</th>"
                   "<th>Reason or owner</th></tr>%s</table>" % rows)
    for title, head, rows in model.rounds:
        body = "".join(
            '<tr id="%s"><td>%s</td><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                html.escape(r.get("id", "")), md(r.get("id", "")), md(r.get("severity", "")),
                html.escape(_clean(r.get("file:line", ""))), md(r.get("failure scenario", "")),
                md(r.get("recommended fix", "")), md(r.get("action", "")), md(r.get("status", "")))
            for r in rows)
        out.append("<h3>%s%s</h3>%s" % (
            html.escape(title), " <small>reviewed at <code>%s</code></small>" % head if head else "",
            "<table><tr><th>ID</th><th>Severity</th><th>Where</th><th>Failure scenario</th><th>Recommended fix</th>"
            "<th>Action</th><th>Status</th></tr>%s</table>" % body if body else "<p class='muted'>No findings.</p>"))
    if model.brief:
        out.append("<h3>Review brief</h3><pre>%s</pre>" % html.escape(model.brief))
    return "".join(out) or "<p class='muted'>No review yet.</p>"


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2330;--muted:#667085;--line:#d8dde6;--accent:#2f5fd0;
--ok:#1b7f4b;--warn:#a15c00;--bad:#b42318;--soft:#eef2fb}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--ink:#e6e9ef;--muted:#98a2b3;
--line:#2e3644;--accent:#8fb0ff;--ok:#5fd39a;--warn:#f0b35a;--bad:#ff8a80;--soft:#232b3a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:36px 0 10px}h3{font-size:15px;margin:18px 0 8px}
h3 span{font-weight:400}
a{color:var(--accent)}small,.muted{color:var(--muted)}
code,pre{font:13px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}
code{background:var(--soft);padding:1px 4px;border-radius:4px}
pre{background:var(--soft);padding:12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;margin:8px 0}
.meta{color:var(--muted);margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:10px 0}
.card.open{border-left:4px solid var(--warn)}
.card h3{margin:0 0 8px}
.option{display:block;padding:7px 10px;border:1px solid var(--line);border-radius:6px;margin:6px 0}
.option.chosen{border-color:var(--ok)}
.card.open .option{cursor:pointer}.card.open .option:hover{background:var(--soft)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:10px 0 6px}
dt{color:var(--muted)}dd{margin:0}
.pill{display:inline-block;font-size:12px;padding:0 7px;border-radius:10px;border:1px solid currentColor;color:var(--muted)}
.pill.ok,.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.pill.rec{color:var(--accent)}.bad{color:var(--bad)}
.pill.layer{border-style:dashed}.note{border-left:4px solid var(--accent);padding:6px 12px;background:var(--soft);border-radius:4px}
h1 span{font-weight:400;color:var(--muted)}ul,ol{padding-left:22px}
nav{position:sticky;top:0;z-index:1;display:flex;flex-wrap:wrap;gap:4px 16px;padding:8px 16px;font-size:14px;
background:var(--card);border-bottom:1px solid var(--line)}
nav a{text-decoration:none}nav a:hover{text-decoration:underline}
h2,h3,[id]{scroll-margin-top:56px}:target{outline:2px solid var(--accent);outline-offset:3px}
a.ref{text-decoration:none;border-bottom:1px dotted currentColor}
input[type=text]{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
button{font:inherit;padding:6px 12px;border:1px solid var(--accent);border-radius:6px;background:var(--accent);color:var(--card);cursor:pointer}
.queue{padding-left:22px;margin:8px 0}.queue li{margin:8px 0}
.wide{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin:8px 0;background:var(--card)}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{font-size:13px;color:var(--muted);font-weight:600}
.trace{width:100%;min-width:720px;height:auto;display:block}
.trace path{fill:none;stroke:var(--muted);stroke-width:1.2;opacity:.7}
.trace rect{fill:var(--card);stroke:var(--line)}
.trace text{fill:var(--ink);font:12px ui-monospace,SFMono-Regular,Consolas,monospace}
.trace .head{fill:var(--muted);font:600 12px system-ui,sans-serif}
.trace .tag{fill:var(--accent)}
.trace .gap rect{stroke:var(--bad);stroke-width:2;stroke-dasharray:4 3}
pre.mermaid{background:none;padding:0}
details{margin:8px 0}summary{cursor:pointer}
"""

JS = """
const out = document.getElementById('cmds');
function refresh() {
  const lines = [];
  document.querySelectorAll('[data-decision]').forEach(card => {
    const pick = card.querySelector('input[type=radio]:checked');
    if (!card.dataset.decision || !pick) return;
    const note = card.querySelector('input[type=text]').value.trim();
    lines.push('./devgent answer ' + card.dataset.decision + ' ' + pick.value +
      (note ? " '" + note.replace(/'/g, "'\\\\''") + "'" : ''));
  });
  out.textContent = lines.join('\\n') || 'Pick an option above.';
}
if (out) {
  document.addEventListener('input', refresh);
  document.getElementById('copy').addEventListener('click', e => {
    const done = () => { e.target.textContent = 'Copied'; };
    if (navigator.clipboard) navigator.clipboard.writeText(out.textContent).then(done, () => {});
    else { getSelection().selectAllChildren(out); document.execCommand('copy'); done(); }
  });
}
"""


def _page(title, body, js=JS):
    body = "".join(body)
    script = (
        '<script type="module">import mermaid from "%s";mermaid.initialize({startOnLoad:true,'
        'theme:matchMedia("(prefers-color-scheme: dark)").matches?"dark":"default"});</script>' % MERMAID
    ) if 'class="mermaid"' in body else ""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>%s</title><style>%s</style></head><body>%s<script>%s</script>%s</body></html>"
            % (html.escape(title), CSS, body, js, script))


def _functional_html(model, h="h2"):
    """Problem, requirements and scenarios as someone outside the code would check them:
    test levels are shown, test names and files are not."""
    plan, sec = model.plan, model.plan.sections
    head = lambda title: "<%s>%s</%s>" % (h, title, h)
    out = [head("Problem and outcome"),
           _blocks(sec.get("problem and outcome", "")) or "<p class='muted'>Not written yet.</p>",
           head("Requirements")]
    if model.reqs:
        out.append("<ul>%s</ul>" % "".join(
            '<li id="%s"><b>%s</b> %s <small>%s</small></li>' % (
                r, r, md(text), md(" ".join(sc for sc, _, covers in model.scenarios if r in covers)) or "no scenario yet")
            for r, text in model.reqs))
    else:
        out.append("<p class='muted'>No requirements yet.</p>")
    out += [head("Scenarios"), _blocks(_without_first_table(sec.get("scenarios", "")))]
    if model.scenarios:
        rows = []
        for sc, text, covers in model.scenarios:
            levels = sorted({level for ids, level, _ in model.tests if sc in ids and level})
            rows.append('<tr id="%s"><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                sc, sc, md(text), md(model.expected.get(sc, "")), md(" ".join(sorted(covers))),
                html.escape(", ".join(levels)) or '<span class="bad">no test yet</span>'))
        out.append('<div class="wide"><table><tr><th>ID</th><th>Scenario</th><th>Expected result</th>'
                   "<th>Covers</th><th>Checked by</th></tr>%s</table></div>" % "".join(rows))
    else:
        out.append("<p class='muted'>No scenarios yet.</p>")
    known = ("problem and outcome", "requirements", "scenarios", "scenarios and api contract")
    for name, text in tk.sections(plan.layers.get("functional", ""), level=3).items():
        if name not in known and text:
            out += [head(html.escape(name.capitalize())), _blocks(text)]
    return "".join(out)


def _questions_html(questions, layers=True):
    rows = "".join('<tr id="%s"><td>%s</td><td>%s%s</td><td>%s</td><td>%s%s</td><td>%s</td></tr>' % (
        html.escape(_clean(q.get("id", "")), quote=True), html.escape(_clean(q.get("id", ""))), md(q.get("question", "")),
        _layer_pill(q.get("layer")) if layers else "", md(q.get("owner", "")), md(q.get("status", "")),
        ' <span class="pill warn">blocking</span>' if q.get("blocking", "").strip().lower().startswith("y") else "",
        md(q.get("answer source", ""))) for q in questions)
    return ('<div class="wide"><table><tr><th>ID</th><th>Question</th><th>Owner</th><th>Status</th>'
            "<th>Answer source</th></tr>%s</table></div>" % rows)


def _design_prose(plan):
    """The high-level design as written. A plan in the flat layout keeps it in `## Design`
    (minus the change map, which the page draws) and in the conditional sections."""
    if plan.layered:
        return plan.layers.get("high-level", "")
    design = re.split(r"^### +change map\b.*$", plan.sections.get("design", ""), flags=re.M | re.I)[0]
    extra = ["### %s\n\n%s" % (name.capitalize(), plan.sections[name])
             for name in ("data and migration", "reliability and operations", "security and privacy", "alternatives")
             if plan.sections.get(name)]
    return "\n\n".join([design] + extra)


def render_functional(settings, ticket):
    """The functional layer on its own, to show someone who does not read the pull request."""
    model = Model(settings, ticket)
    plan = model.plan
    body = [
        "<main>",
        "<h1>%s <span>functional scenarios</span></h1>" % html.escape(ticket.id),
        '<p class="meta">state <b>%s</b> · plan version %s · generated from <code>%s</code></p>'
        % tuple(html.escape(str(v)) for v in (model.log.state or "unknown", plan.version, ticket.rel("plan.md"))),
        '<p class="note">For discussion and clarification. Nothing here needs a sign-off: questions and '
        "corrections go to the ticket's driver, who records them in the plan.</p>",
        _functional_html(model),
    ]
    questions = [q for q in model.questions if tk.layer_tag(q.get("layer")) == "functional"]
    decisions = any(tk.layer_tag(d.get("layer")) == "functional" for d in plan.decisions)
    if questions or decisions:
        body.append("<h2>Open points</h2>")
    if questions:
        body.append(_questions_html(questions, layers=False))
    if decisions:
        body.append(_decisions_html(model, layer="functional"))
    body.append("</main>")
    return _page("%s functional scenarios" % ticket.id, body, js="")  # nothing on it is answered here


def render(settings, ticket):
    """The whole ticket for the approver, in the plan's own order: what needs an answer,
    then the three layers from behaviour down to files, then the review and the references."""
    model = Model(settings, ticket)
    plan, log = model.plan, model.log
    nav, body = [("needs-you", "Needs you")], []

    def section(anchor, title, *parts):
        nav.append((anchor, title))
        body.append('<h2 id="%s">%s</h2>' % (anchor, title))
        body.extend(parts)

    body += [
        "<h1>%s</h1>" % html.escape(ticket.id),
        '<p class="meta">state <b>%s</b> · tier <b>%s</b> · plan version %s · generated from <code>%s</code></p>'
        % tuple(html.escape(str(v)) for v in (log.state or "unknown", plan.tier, plan.version, ticket.dir)),
        '<h2 id="needs-you">Needs you</h2>', _queue_html(model),
    ]
    if model.errors:
        body.append("<details><summary>%d issue%s <code>./devgent check</code> would report</summary><ul>%s</ul></details>" % (
            len(model.errors), "" if len(model.errors) == 1 else "s",
            "".join("<li>%s</li>" % md(e) for e in model.errors)))
    if plan.tier == "T0":
        for name in tk.TASK_NOTE_SECTIONS:
            section(name, name.capitalize(), "<pre>%s</pre>" % html.escape(plan.sections.get(name, "")))
    else:
        section("decisions", "Decisions", _decisions_html(model))
        section("functional", "Functional scenarios", _functional_html(model, "h3"))
        section("high-level", "High-level design",
                _blocks(_design_prose(plan)) or "<p class='muted'>Not written yet.</p>")
        low = ["<h3>Trace: requirement → scenario → test</h3>", '<div class="card wide">%s</div>' % _trace_svg(model),
               "<h3>Slices and the files they may write</h3>", '<div class="wide">%s</div>' % _slices_html(model)]
        for name, text in tk.sections(plan.layers.get("low-level", ""), level=3).items():
            if name not in ("change map", "test matrix", "slices") and text:
                low += ["<h3>%s</h3>" % html.escape(name.capitalize()), _blocks(text)]
        section("low-level", "Low-level design", *low)
        if model.questions:
            section("questions", "Open questions", _questions_html(model.questions))
        section("review", "Review", '<div class="wide">%s</div>' % _review_html(model))
        section("references", "References", _references_html(model))
    links = "".join('<a href="#%s">%s</a>' % pair for pair in nav)
    return _page(ticket.id, ["<nav><b>%s</b>%s</nav><main>" % (html.escape(ticket.id), links)] + body + ["</main>"])


def run(root, open_browser=False, functional=False):
    settings = config.load_settings(root)
    ticket = tk.resolve_ticket(root, settings)
    if ticket is None or ticket.plan() is None:
        print("no active ticket with a plan.md on this branch")
        return 1
    folder = os.path.join(tk.state_dir(root), "view")
    os.makedirs(folder, exist_ok=True)
    if functional and ticket.plan().tier == "T0":
        print("a T0 task note has no functional layer; `./devgent view` shows the note")
        return 1
    path = os.path.join(folder, ticket.id + ("-functional" if functional else "") + ".html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write((render_functional if functional else render)(settings, ticket))
    print(path)
    if open_browser:
        webbrowser.open("file://" + path)
    return 0
