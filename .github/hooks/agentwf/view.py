"""`./agentwf view`: one generated, never-committed HTML page for a ticket.

Everything on it is derived from plan.md, log.md and review.md, so it cannot
drift from what was approved: what needs a human answer, the decisions, the
plan's Mermaid diagrams, the requirement-to-test trace, slices against the
change map, and the review findings. Agents never write this page.
"""
import html
import os
import re
import webbrowser

from . import config, ticket as tk

MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"


def md(text):
    """Inline markdown only: code and bold."""
    text = html.escape(text or "")
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)


def _letter(text, options):
    """The option letter a recommendation or an answer starts with, if any."""
    m = re.match(r"\W*([A-Za-z])\b", text or "")
    letter = m.group(1).upper() if m else None
    return letter if letter in [o[0] for o in options] else None


def _clean(cell):
    return cell.strip().strip("*_`").strip()


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
        for row in tk.table_rows(sec.get("scenarios and api contract", "")):
            m = re.search(r"SC-\d+", row.get("id", ""))
            if m:
                covers = set(re.findall(r"R-\d+", " ".join(row.values())))
                self.scenarios.append((m.group(0), row.get("scenario", ""), covers))
        self.tests = []  # ({SC ids}, level, test)
        for row in tk.table_rows(sec.get("test matrix", "")):
            ids = set(re.findall(r"SC-\d+", row.get("scenario", "")))
            if ids and not tk._blank(row.get("test", "")):
                self.tests.append((ids, _clean(row.get("level", "")), _clean(row.get("test", ""))))
        self.slices = []  # (id, goal, {R and SC ids}, paths, status)
        for title, body in tk.sections(sec.get("slices", ""), level=3).items():
            m = re.match(r"(s-\d+)\b[\s:.-]*(.*)", title)
            if m:
                sid = m.group(1).upper()
                self.slices.append((sid, m.group(2), set(re.findall(r"(?:R|SC)-\d+", body)),
                                    self.plan.slices.get(sid, []), self.log.slice_status.get(sid, "not started")))
        design = tk.sections(sec.get("design", ""), level=3)
        self.change_map = tk.table_rows(design.get("change map", ""))
        self.diagrams = re.findall(r"```mermaid\n(.*?)```", self.plan.body, flags=re.S)
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
                out.append(("question", row.get("id", "?"), row.get("question", ""),
                            "owner: %s" % (row.get("owner") or "nobody named")))
        for sid, goal, _, _, status in self.slices:
            if status == "awaiting-review":
                out.append(("slice", sid, goal, "look at it, then `./agentwf slice accept %s`" % sid))
        for title, _, rows in self.rounds[-1:]:
            for row in rows:
                if _clean(row.get("action", "")).lower() == "decide" and _clean(row.get("status", "")).lower() == "open":
                    out.append(("finding", row.get("id", "?"),
                                "%s: %s" % (row.get("file:line", ""), row.get("failure scenario", "")),
                                "write `fix` or `won't fix: reason` in its Status cell in review.md"))
        return out


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


def _decisions_html(model):
    if not model.plan.decisions:
        return "<p class='muted'>The plan lists no decisions.</p>"
    cards, pending = [], False
    for d in model.plan.decisions:
        answered = not tk._blank(d.get("answer", ""))
        pending = pending or not answered
        rec, chosen = _letter(d.get("recommendation"), d["options"]), _letter(d.get("answer"), d["options"])
        options = []
        for letter, text in d["options"]:
            marks = ('<span class="pill rec">recommended</span>' if letter == rec else "") + (
                '<span class="pill ok">chosen</span>' if answered and letter == chosen else "")
            pick = "" if answered else '<input type="radio" name="%s" value="%s"> ' % (d["id"], letter)
            options.append('<label class="option%s">%s<b>%s</b> %s %s</label>'
                           % (" chosen" if answered and letter == chosen else "", pick, letter, md(text), marks))
        facts = "".join(
            "<dt>%s</dt><dd>%s</dd>" % (label, md(d[key]))
            for key, label in (("why a human", "Why a human"), ("recommendation", "Recommendation"),
                               ("reversible", "Reversible")) if d.get(key))
        tail = ('<p class="answer"><b>Answer:</b> %s</p>' % md(d["answer"]) if answered else
                '<input type="text" placeholder="Optional note for the record" aria-label="Note for %s">' % d["id"])
        cards.append('<section class="card%s" id="%s" data-decision="%s"><h3>%s <span>%s</span></h3>%s<dl>%s</dl>%s</section>'
                     % ("" if answered else " open", d["id"], "" if answered else d["id"], d["id"],
                        md(d["title"]), "".join(options), facts, tail))
    if pending:
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
            '<tr id="%s"><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                html.escape(re.search(r"O-\d+", r["id"]).group(0)), md(r["id"]), md(r.get("objection", "")),
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
    lines.push('./agentwf answer ' + card.dataset.decision + ' ' + pick.value +
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


def render(settings, ticket):
    model = Model(settings, ticket)
    plan, log = model.plan, model.log
    body = [
        "<h1>%s</h1>" % html.escape(ticket.id),
        '<p class="meta">state <b>%s</b> · tier <b>%s</b> · plan version %s · generated from <code>%s</code></p>'
        % tuple(html.escape(str(v)) for v in (log.state or "unknown", plan.tier, plan.version, ticket.dir)),
        '<h2>Needs you</h2>', _queue_html(model),
    ]
    if model.errors:
        body.append("<details><summary>%d issue%s <code>./agentwf check</code> would report</summary><ul>%s</ul></details>" % (
            len(model.errors), "" if len(model.errors) == 1 else "s",
            "".join("<li>%s</li>" % md(e) for e in model.errors)))
    if plan.tier == "T0":
        for name in tk.TASK_NOTE_SECTIONS:
            body.append("<h2>%s</h2><pre>%s</pre>" % (name.capitalize(), html.escape(plan.sections.get(name, ""))))
    else:
        body += ["<h2>Decisions</h2>", _decisions_html(model)]
        if model.diagrams:
            body.append("<h2>Flow</h2>")
            body += ['<div class="card wide"><pre class="mermaid">%s</pre></div>' % html.escape(d)
                     for d in model.diagrams]
        body += [
            "<h2>Trace: requirement → scenario → test</h2>",
            '<div class="card wide">%s</div>' % _trace_svg(model),
            "<h2>Slices and the files they may write</h2>", '<div class="wide">%s</div>' % _slices_html(model),
            "<h2>Review</h2>", '<div class="wide">%s</div>' % _review_html(model),
        ]
    mermaid = (
        '<script type="module">import mermaid from "%s";mermaid.initialize({startOnLoad:true,'
        'theme:matchMedia("(prefers-color-scheme: dark)").matches?"dark":"default"});</script>' % MERMAID
    ) if model.diagrams else ""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>%s</title><style>%s</style></head><body><main>%s</main><script>%s</script>%s</body></html>"
            % (html.escape(ticket.id), CSS, "".join(body), JS, mermaid))


def run(root, open_browser=False):
    settings = config.load_settings(root)
    ticket = tk.resolve_ticket(root, settings)
    if ticket is None or ticket.plan() is None:
        print("no active ticket with a plan.md on this branch")
        return 1
    folder = os.path.join(tk.state_dir(root), "view")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, ticket.id + ".html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(settings, ticket))
    print(path)
    if open_browser:
        webbrowser.open("file://" + path)
    return 0
