"""
MASTER BUTLER — OFFICE DASHBOARD (local prototype)

Built to the letter of docs/DASHBOARD_DESIGN_BRIEF.md (the office's own
questionnaire answers):

  * Top band = NEEDS ATTENTION (emergent items), then the bid queue
    OLDEST FIRST with age timers that go red near the 24-hour SLA.
  * One bid = one screen: photos used, measurements, confidence, ALL
    notes in ONE stack (Martha's #1 pain), similar-homes history from
    the reconciler sweep.
  * Reason buttons are THE OFFICE'S OWN WORDS. A tap + optional note
    feeds the learning loop; never required, always welcomed.
  * Escalations and photo requests come out as standardized drafts
    (LaRee's template rule + Martha's trust condition). NOTHING SENDS.
  * Fast and light: stdlib only, no frameworks, no build step — it runs
    on Martha's slow machine.

Run:  python3 dashboard.py     then open  http://localhost:8765
Local prototype — reads/writes the repo's data/ folder only.
"""

import json
import re
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import templates

BASE = Path(__file__).parent
SHADOW = BASE / "data" / "shadow_bids"
REVIEW_LOG = BASE / "data" / "review_log.json"
RECON = BASE / "data" / "discount_reconciliation.json"
AERIAL = BASE / "data" / "aerial"

PORT = 8765
SLA_HOURS = 24

# THE OFFICE'S OWN WORDS (questionnaire Q9) — same list as schema.sql
REASONS = ["specialty_windows", "heavy_tree_coverage", "difficult_roof",
           "rate_pricing_update", "new_info_photos",
           "tech_adjustment_last_job", "last_quote_too_old",
           "underbid_on_review", "difficult_customer_premium", "other"]


# ── data access ──────────────────────────────────────────────

def load_reviews():
    if REVIEW_LOG.exists():
        return json.loads(REVIEW_LOG.read_text())
    return []


def save_review(entry):
    reviews = load_reviews()
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    reviews.append(entry)
    REVIEW_LOG.write_text(json.dumps(reviews, indent=1))


def load_bids():
    """Every shadow record, oldest first, with age + review status."""
    reviewed = {r["stamp"] for r in load_reviews() if r.get("stamp")}
    bids = []
    for p in sorted(SHADOW.glob("*.json")):
        rec = json.loads(p.read_text())
        rec["stamp"] = p.stem
        rec["reviewed"] = p.stem in reviewed
        try:
            t = datetime.strptime(p.stem, "%Y%m%d-%H%M%S")
            rec["age_hours"] = (datetime.now() - t).total_seconds() / 3600
        except ValueError:
            rec["age_hours"] = 0
        out = rec.get("pipeline_output", "")
        m = re.findall(r"\$\s?([\d,]+)(?:\.\d+)?", out)
        rec["total_guess"] = m[-1] if m else None
        bids.append(rec)
    return bids


def similar_history(services):
    """Reconciler history for the same kind of work — 'what did we charge
    similar homes' (brief requirement, powered by the 5,000-invoice sweep)."""
    if not RECON.exists() or not services:
        return []
    words = set()
    for s in services:
        words.update(s.replace("_", " ").split())
    hits = []
    for f in json.loads(RECON.read_text()):
        if "honor" not in f.get("categories", []):
            continue
        text = " ".join(d["text"].lower() for d in f["discounts"])
        if any(w in text for w in words):
            hits.append(f)
    return hits[:5]


# ── html helpers (no frameworks — Martha's machine is slow) ──

STYLE = """<style>
body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;
     background:#f4f5f7;color:#1a1a1a}
header{background:#0b3d2e;color:#fff;padding:14px 24px;font-size:20px;
       font-weight:700}
header small{font-weight:400;opacity:.75;margin-left:10px}
.wrap{max-width:1100px;margin:0 auto;padding:18px}
.band{background:#fff4e5;border:1px solid #f0c987;border-radius:8px;
      padding:12px 16px;margin-bottom:18px}
.band h2{margin:0 0 8px;font-size:15px;color:#8a5a00}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;
      padding:14px 18px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:#666;font-weight:600;padding:6px 8px;
   border-bottom:2px solid #eee}
td{padding:8px;border-bottom:1px solid #f0f0f0}
tr:hover td{background:#f8faf9}
.age{font-weight:700}.age.warn{color:#c77700}.age.late{color:#c0392b}
.chip{display:inline-block;background:#eef3f1;border-radius:12px;
      padding:2px 10px;margin:2px;font-size:12px}
.flag{background:#fdecea;color:#a93226}
.ok{color:#1e8449;font-weight:600}
a{color:#0b6e4f;text-decoration:none}a:hover{text-decoration:underline}
pre{background:#f7f7f7;border:1px solid #eee;border-radius:6px;
    padding:12px;font-size:12.5px;overflow-x:auto;white-space:pre-wrap}
.notes{background:#fffbe6;border:1px solid #efe3a1;border-radius:8px;
       padding:10px 14px}
.notes div{padding:3px 0;border-bottom:1px dashed #eee}
button,.btn{background:#0b6e4f;color:#fff;border:0;border-radius:6px;
       padding:8px 14px;font-size:14px;cursor:pointer;margin:3px}
button.gray{background:#7f8c8d}button.red{background:#c0392b}
.reason{background:#fff;color:#0b6e4f;border:1.5px solid #0b6e4f}
.reason.sel{background:#0b6e4f;color:#fff}
input[type=text],textarea{width:100%;padding:8px;border:1px solid #ccc;
       border-radius:6px;font-size:14px;box-sizing:border-box}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
</style>"""


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title>{STYLE}</head><body>"
            f"<header>Master Butler — Bid Review"
            f"<small>shadow mode · nothing sends without you</small></header>"
            f"<div class='wrap'>{body}</div></body></html>").encode()


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def age_html(h):
    cls = "late" if h >= SLA_HOURS else ("warn" if h >= SLA_HOURS * 0.75 else "")
    if h < 1:
        label = f"{int(h * 60)}m"
    elif h < 48:
        label = f"{h:.0f}h"
    else:
        label = f"{h / 24:.0f}d"
    return f"<span class='age {cls}'>{label}</span>"


# ── screens ──────────────────────────────────────────────────

def home_page():
    bids = load_bids()
    queue = [b for b in bids if not b["reviewed"]]
    attention = []
    for b in queue:
        if b.get("office_alert"):
            attention.append((b, b["office_alert"]))
        elif b.get("pipeline_error"):
            attention.append((b, f"pipeline error: {b['pipeline_error']}"))
        elif b["age_hours"] >= SLA_HOURS and b.get("kind") == "new_request":
            attention.append((b, f"waiting {b['age_hours']:.0f}h — past the "
                                 "24-hour promise"))
    band = ""
    if attention:
        rows = "".join(
            f"<div>⚠ <a href='/bid/{b['stamp']}'>{esc(b['from'])}</a> — "
            f"{esc(why)}</div>" for b, why in attention)
        band = f"<div class='band'><h2>NEEDS ATTENTION</h2>{rows}</div>"

    rows = ""
    for b in queue:                       # already oldest first
        services = "".join(f"<span class='chip'>{esc(s)}</span>"
                           for s in (b.get("services") or [])) or "—"
        flags = ("<span class='chip flag'>spam</span>"
                 if b.get("office_alert") else "")
        total = f"${b['total_guess']}" if b.get("total_guess") else "—"
        rows += (f"<tr><td>{age_html(b['age_hours'])}</td>"
                 f"<td><a href='/bid/{b['stamp']}'>{esc(b['from'])}</a></td>"
                 f"<td>{esc(b.get('kind'))}</td><td>{services}{flags}</td>"
                 f"<td>{total}</td></tr>")
    if not rows:
        rows = "<tr><td colspan=5>Queue is empty — all caught up. ✅</td></tr>"

    reviews = load_reviews()[-8:][::-1]
    rev_rows = "".join(
        f"<div>✅ {esc(r.get('action'))} — {esc(r.get('customer', r.get('stamp')))}"
        f"{(' · ' + esc(r['reason'])) if r.get('reason') else ''}</div>"
        for r in reviews) or "<div>No reviews yet.</div>"

    body = (band +
        "<div class='grid'><div class='card'>"
        "<h2 style='margin-top:0'>Bid queue — oldest first</h2>"
        "<table><tr><th>Waiting</th><th>From</th><th>Kind</th>"
        "<th>Services</th><th>Est.</th></tr>" + rows + "</table></div>"
        "<div><div class='card'><h3 style='margin-top:0'>Recent decisions"
        "</h3>" + rev_rows + "</div>"
        "<div class='card'><h3 style='margin-top:0'>Schedule glance</h3>"
        "<div style='color:#888'>Jobber calendar — future phase. Days fill "
        "toward the $850–1,100/tech target.</div></div></div></div>")
    return page("Bid queue", body)


def bid_page(stamp):
    bids = {b["stamp"]: b for b in load_bids()}
    b = bids.get(stamp)
    if not b:
        return page("Not found", "<div class='card'>No such bid.</div>")

    notes = re.findall(r"⚠ ?(.+)", b.get("pipeline_output", ""))
    if b.get("office_alert"):
        notes.insert(0, b["office_alert"])
    notes_html = "".join(f"<div>⚠ {esc(n)}</div>" for n in notes) or \
                 "<div>(no flags)</div>"

    hist = similar_history(b.get("services") or [])
    hist_html = "".join(
        f"<div>#{h['invoice']} {esc(h['client'])[:24]} — honored gap "
        f"${h['honored_gap']:.0f} ({h['date'][:10]})</div>"
        for h in hist) or "<div>(no honor history for this service mix)</div>"

    reasons = "".join(
        f"<button type='button' class='reason' "
        f"onclick=\"document.getElementById('reason').value='{r}';"
        f"document.querySelectorAll('.reason').forEach(x=>x.classList.remove('sel'));"
        f"this.classList.add('sel')\">{r.replace('_', ' ')}</button>"
        for r in REASONS)

    body = f"""
<a href='/'>&larr; back to queue</a>
<div class='grid'><div>
 <div class='card'>
  <h2 style='margin-top:0'>{esc(b['from'])} {age_html(b['age_hours'])}</h2>
  <div><b>Subject:</b> {esc(b.get('subject'))}</div>
  <div><b>Address:</b> {esc(b.get('address') or '— not found')}</div>
  <div><b>Services:</b> {', '.join(b.get('services') or []) or '—'}</div>
  <div><b>Folder:</b> {esc(b.get('folder', 'INBOX'))}</div>
 </div>
 <div class='card'><h3 style='margin-top:0'>All notes — one stack</h3>
  <div class='notes'>{notes_html}</div></div>
 <div class='card'><h3 style='margin-top:0'>System draft</h3>
  <pre>{esc(b.get('pipeline_output') or '(no draft — ' +
             esc(b.get('kind')) + ')')}</pre></div>
</div><div>
 <div class='card'><h3 style='margin-top:0'>Decide</h3>
  <form method='POST' action='/review'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <input type='hidden' id='reason' name='reason' value=''>
   <div style='margin-bottom:6px'>{reasons}</div>
   <input type='text' name='note' placeholder='optional: teach it in one line'>
   <div style='margin-top:8px'>
    <button name='action' value='approve'>Approve as-is</button>
    <button name='action' value='adjusted' class='gray'>Adjusted (reason above)</button>
   </div>
  </form>
  <form method='POST' action='/escalate' style='margin-top:6px'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <input type='hidden' name='address' value='{esc(b.get('address'))}'>
   <input type='text' name='question' placeholder='the ONE question for Dallon/Tom'>
   <button class='red'>Escalate → standardized form</button>
  </form>
  <form method='POST' action='/photo_request' style='margin-top:6px'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <input type='hidden' name='services' value='{','.join(b.get('services') or [])}'>
   <button class='gray'>Draft photo-request email</button>
  </form>
 </div>
 <div class='card'><h3 style='margin-top:0'>Similar homes (honor history)</h3>
  {hist_html}</div>
</div></div>"""
    return page("Review bid", body)


# ── server ───────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _send(self, content, code=200, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/":
            return self._send(home_page())
        m = re.match(r"^/bid/([\w-]+)$", self.path)
        if m:
            return self._send(bid_page(m.group(1)))
        return self._send(b"not found", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        get = lambda k: form.get(k, [""])[0]

        if self.path == "/review":
            save_review({"stamp": get("stamp"), "action": get("action"),
                         "customer": get("customer"),
                         "reason": get("reason") or None,
                         "note": get("note") or None})
        elif self.path == "/escalate":
            path = templates.draft_escalation(
                bid_ref=get("stamp"), customer=get("customer"),
                address=get("address"),
                question=get("question") or "(no question written)",
                to="dallon")
            save_review({"stamp": get("stamp"), "action": "escalated",
                         "customer": get("customer"),
                         "note": f"form: {path.name}"})
        elif self.path == "/photo_request":
            services = [s for s in get("services").split(",") if s]
            path = templates.draft_photo_request(
                get("customer"), services,
                reason=f"dashboard request for bid {get('stamp')}")
            save_review({"stamp": get("stamp"), "action": "photo_requested",
                         "customer": get("customer"),
                         "note": f"draft: {path.name}"})
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *a):        # keep the console quiet
        pass


if __name__ == "__main__":
    print(f"Master Butler dashboard → http://localhost:{PORT}")
    print("(local prototype — reads data/shadow_bids, writes review log "
          "and draft templates. Nothing sends.)")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
