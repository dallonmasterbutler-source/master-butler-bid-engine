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

import os

PORT = int(os.environ.get("PORT", 8765))     # Render sets PORT
HOST = os.environ.get("HOST", "127.0.0.1")   # local-only by default
SLA_HOURS = 24

# ── THE SWITCH ────────────────────────────────────────────────
# OFF: Approve only records the decision (pure shadow mode).
# ON  (PUSH_ON_APPROVE=true in .env): Approve ALSO creates a DRAFT
# quote in Jobber — still a draft, still human-sent, but real.
# Ships OFF. Dallon flips it when shadow mode has earned trust.
def _push_enabled():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("PUSH_ON_APPROVE="):
                return line.split("=", 1)[1].strip().lower() == "true"
    return False

# THE OFFICE'S OWN WORDS (questionnaire Q9) — same list as schema.sql
REASONS = ["specialty_windows", "heavy_tree_coverage", "difficult_roof",
           "rate_pricing_update", "new_info_photos",
           "tech_adjustment_last_job", "last_quote_too_old",
           "underbid_on_review", "difficult_customer_premium", "other"]


# ── data access ──────────────────────────────────────────────

import clouddb


def load_reviews():
    if clouddb.available():
        return clouddb.all_reviews()
    if REVIEW_LOG.exists():
        return json.loads(REVIEW_LOG.read_text())
    return []


def save_review(entry):
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    if clouddb.available():
        clouddb.add_review(entry)
        return
    reviews = (json.loads(REVIEW_LOG.read_text())
               if REVIEW_LOG.exists() else [])
    reviews.append(entry)
    REVIEW_LOG.write_text(json.dumps(reviews, indent=1))


HOLD_REASONS = ["standby_gutters", "seasonal_pw_windows",
                "awaiting_photos", "awaiting_customer", "other"]


def active_holds():
    """stamp -> latest hold entry, for bids still on hold. A hold whose
    resurface date has arrived STOPS hiding the bid (it pops back)."""
    holds = {}
    for r in load_reviews():
        if r.get("action") == "hold":
            holds[r["stamp"]] = r
        elif r.get("stamp") in holds and r.get("action") in DECIDED_ACTIONS:
            del holds[r["stamp"]]           # decided later — hold is over
    today = datetime.now().date().isoformat()
    live, resurfaced = {}, {}
    for stamp, h in holds.items():
        if (h.get("hold_until") or "9999") <= today:
            resurfaced[stamp] = h
        else:
            live[stamp] = h
    return live, resurfaced


DECIDED_ACTIONS = ("approve", "adjusted", "escalated", "duplicate_same")


def _shadow_source():
    """(stamp, record) pairs — database when available, files otherwise."""
    if clouddb.available():
        return clouddb.all_shadow()
    return [(p.stem, json.loads(p.read_text()))
            for p in sorted(SHADOW.glob("*.json"))]


def load_bids():
    """Every shadow record, oldest first, with age + review status."""
    decided = {r["stamp"] for r in load_reviews()
               if r.get("stamp") and r.get("action") in DECIDED_ACTIONS}
    reviewed = decided
    bids = []
    for stamp, rec in _shadow_source():
        rec["stamp"] = stamp
        rec["reviewed"] = stamp in reviewed
        try:
            t = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
            rec["age_hours"] = (datetime.now() - t).total_seconds() / 3600
        except ValueError:
            rec["age_hours"] = 0
        out = rec.get("pipeline_output", "")
        m = re.findall(r"\$\s?([\d,]+)(?:\.\d+)?", out)
        rec["total_guess"] = m[-1] if m else None
        rec["confidence"] = ((rec.get("draft") or {}).get("bid") or {}) \
            .get("confidence")
        if rec["confidence"] is None:
            mc = re.search(r"confidence (\d+)%", out)
            rec["confidence"] = int(mc.group(1)) if mc else None
        bids.append(rec)
    return bids


def similar_history(services):
    """Reconciler history for the same kind of work — 'what did we charge
    similar homes' (brief requirement, powered by the 5,000-invoice sweep)."""
    if not services:
        return []
    if clouddb.available():
        records = clouddb.get_blob("discount_reconciliation") or []
        return _history_hits(records, services)
    if not RECON.exists():
        return []
    return _history_hits(json.loads(RECON.read_text()), services)


def _history_hits(records, services):
    words = set()
    for s in services:
        words.update(s.replace("_", " ").split())
    hits = []
    for f in records:
        if "honor" not in f.get("categories", []):
            continue
        text = " ".join(d["text"].lower() for d in f["discounts"])
        if any(w in text for w in words):
            hits.append(f)
    return hits[:5]


# ── html helpers (no frameworks — Martha's machine is slow) ──

STYLE = """<style>
:root{--green:#0b3d2e;--green2:#0b6e4f;--gold:#c9a227;--bg:#f4f5f2}
body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;
     background:var(--bg);color:#1a1a1a}
header{background:linear-gradient(135deg,var(--green) 0%,#124d3a 100%);
       color:#fff;padding:16px 24px;font-size:20px;font-weight:700;
       border-bottom:3px solid var(--gold);letter-spacing:.2px}
header small{font-weight:400;opacity:.75;margin-left:10px;font-size:13px}
.wrap{max-width:1100px;margin:0 auto;padding:18px}
.band{background:#fff4e5;border:1px solid #f0c987;border-radius:8px;
      padding:12px 16px;margin-bottom:18px}
.band h2{margin:0 0 8px;font-size:15px;color:#8a5a00}
.card{background:#fff;border:1px solid #e2e4e0;border-radius:10px;
      padding:14px 18px;margin-bottom:14px;
      box-shadow:0 1px 3px rgba(11,61,46,.07)}
.card h2,.card h3{color:var(--green)}
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


FAVICON = ("<link rel='icon' href=\"data:image/svg+xml,<svg xmlns='http://"
           "www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' "
           "font-size='90'>🎩</text></svg>\">")


def page(title, body, refresh=None):
    auto = (f"<meta http-equiv='refresh' content='{refresh}'>"
            if refresh else "")
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"{auto}{FAVICON}"
            f"<title>{title}</title>{STYLE}</head><body>"
            f"<header>🎩 Master Butler — Bid Review"
            f"<small>{'approve pushes DRAFT quotes to Jobber' if _push_enabled() else 'shadow mode · nothing sends without you'}"
            f"</small>"
            f"<span style='float:right;font-size:14px;font-weight:400'>"
            f"<a href='/' style='color:#e8d9a0'>Queue</a> &nbsp;·&nbsp; "
            f"<a href='/drafts' style='color:#e8d9a0'>Drafts</a> &nbsp;·&nbsp; "
            f"<a href='/brief' style='color:#e8d9a0'>Morning brief</a>"
            f"</span></header>"
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
    live_holds, resurfaced = active_holds()
    queue = [b for b in bids if not b["reviewed"]
             and b["stamp"] not in live_holds]
    attention = []
    for b in queue:
        if b["stamp"] in resurfaced:
            h = resurfaced[b["stamp"]]
            attention.append((b, f"BACK FROM HOLD ({h.get('hold_reason')}) — "
                                 "held bids are answered FIRST"))
        elif b.get("office_alert"):
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
        c = b.get("confidence")
        conf = ("—" if c is None else
                f"<b style='color:{'#1e8449' if c >= 75 else '#c77700' if c >= 50 else '#c0392b'}'>{c}%</b>")
        rows += (f"<tr><td>{age_html(b['age_hours'])}</td>"
                 f"<td><a href='/bid/{b['stamp']}'>{esc(b['from'])}</a></td>"
                 f"<td>{esc(b.get('kind'))}</td><td>{services}{flags}</td>"
                 f"<td>{conf}</td><td>{total}</td></tr>")
    if not rows:
        rows = "<tr><td colspan=6>Queue is empty — all caught up. ✅</td></tr>"

    reviews = load_reviews()[-8:][::-1]
    rev_rows = "".join(
        f"<div>✅ {esc(r.get('action'))} — {esc(r.get('customer', r.get('stamp')))}"
        f"{(' · ' + esc(r['reason'])) if r.get('reason') else ''}</div>"
        for r in reviews) or "<div>No reviews yet.</div>"

    body = (band +
        "<div class='grid'><div class='card'>"
        "<h2 style='margin-top:0'>Bid queue — oldest first</h2>"
        "<table><tr><th>Waiting</th><th>From</th><th>Kind</th>"
        "<th>Services</th><th>Conf.</th><th>Est.</th></tr>" + rows +
        "</table></div>"
        "<div>" + scoreboard_card() + held_card(live_holds, bids) +
        "<div class='card'><h3 style='margin-top:0'>Recent decisions"
        "</h3>" + rev_rows + "</div>"
        "<div class='card'><h3 style='margin-top:0'>Schedule glance</h3>"
        "<div style='color:#888'>Jobber calendar — future phase. Days fill "
        "toward the $850–1,100/tech target.</div></div></div></div>")
    return page("Bid queue", body, refresh=120)   # live-ish, no clicking


def scoreboard_card():
    """System vs office — the running report card."""
    if clouddb.available():
        sb = clouddb.get_blob("scoreboard")
    else:
        sb_path = BASE / "data" / "scoreboard.json"
        sb = json.loads(sb_path.read_text()) if sb_path.exists() else None
    if not sb:
        return ""
    rows = [r for r in sb["rows"] if r.get("office_quote")]
    waiting = sum(1 for r in sb["rows"] if not r.get("office_quote"))
    inner = ""
    for r in rows[:6]:
        gap = r.get("gap_pct")
        color = ("#1e8449" if gap is not None and abs(gap) <= 10
                 else "#c77700" if gap is not None and abs(gap) <= 25
                 else "#c0392b")
        inner += (f"<div>{esc((r.get('customer') or '?')[:26])} — "
                  f"sys ${r['system_total']:.0f} / office "
                  f"${r['office_total']:.0f} "
                  f"<b style='color:{color}'>{gap:+.0f}%</b></div>")
    if not inner:
        inner = (f"<div style='color:#888'>{waiting} shadow draft(s) waiting "
                 "for the office to quote them — comparisons appear here "
                 "automatically.</div>")
    return ("<div class='card'><h3 style='margin-top:0'>Shadow scoreboard"
            f"</h3>{inner}</div>")


def held_card(live_holds, bids):
    if not live_holds:
        return ""
    by_stamp = {b["stamp"]: b for b in bids}
    inner = "".join(
        f"<div>⏸ <a href='/bid/{s}'>{esc(by_stamp.get(s, {}).get('from', s))}"
        f"</a> — {esc(h.get('hold_reason'))} until {esc(h.get('hold_until'))}"
        "</div>"
        for s, h in sorted(live_holds.items(),
                           key=lambda kv: kv[1].get("hold_until") or ""))
    return ("<div class='card'><h3 style='margin-top:0'>On hold "
            "(auto-resurface)</h3>" + inner + "</div>")


def bid_photos(stamp):
    """Customer photos from the saved .eml (extracted on demand)."""
    eml = SHADOW / f"{stamp}.eml"
    if not eml.exists():
        return []
    try:
        from pipeline import extract_photos
        return extract_photos(eml)
    except Exception:
        return []


def aerial_tile_for(address):
    """Already-fetched imagery for this address: (aerial_png, street_jpg)."""
    if not address or not AERIAL.exists():
        return None, None
    slug = re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-")[:40]
    tile = street = None
    for p in AERIAL.iterdir():
        if not p.name.startswith(slug[:24]):
            continue
        if p.suffix == ".png":
            tile = p.name
        elif p.name.endswith("-street.jpg"):
            street = p.name
    return tile, street


def bid_page(stamp):
    bids = {b["stamp"]: b for b in load_bids()}
    b = bids.get(stamp)
    if not b:
        return page("Not found", "<div class='card'>No such bid.</div>")

    gallery, has_imagery = "", False
    if clouddb.available():
        slug = re.sub(r"[^a-z0-9]+", "-",
                      (b.get("address") or "").lower()).strip("-")[:60]
        colors = {"customer": "transparent", "aerial": "#0b6e4f",
                  "street": "#1a5276"}
        for ref, kind, idx in clouddb.photos_index([stamp, slug] if slug
                                                   else [stamp]):
            has_imagery = has_imagery or kind in ("aerial", "street")
            gallery += (f"<a href='/img/{ref}/{kind}/{idx}' target='_blank'>"
                        f"<img src='/img/{ref}/{kind}/{idx}' "
                        f"style='height:110px;margin:4px;border-radius:6px;"
                        f"border:2px solid {colors.get(kind)}' "
                        f"title='{kind}'></a>")
    else:
        photos = bid_photos(stamp)
        gallery = "".join(
            f"<a href='/photo/{stamp}/{i}' target='_blank'>"
            f"<img src='/photo/{stamp}/{i}' style='height:110px;margin:4px;"
            f"border-radius:6px'></a>" for i in range(len(photos)))
        tile, street = aerial_tile_for(b.get("address"))
        for extra, color, label in ((tile, "#0b6e4f", "aerial view"),
                                    (street, "#1a5276", "street view")):
            if extra:
                has_imagery = True
                gallery += (f"<a href='/aerial/{extra}' target='_blank'>"
                            f"<img src='/aerial/{extra}' style='height:110px;"
                            f"margin:4px;border-radius:6px;border:2px solid "
                            f"{color}' title='{label}'></a>")
    gallery_card = (f"<div class='card'><h3 style='margin-top:0'>Photos it "
                    f"used {'(green = aerial, blue = street)' if has_imagery else ''}</h3>"
                    f"{gallery or '<div style=color:#888>No photos on this '
                    'request — the photo-request button drafts the ask.</div>'}"
                    "</div>")

    notes = re.findall(r"⚠ ?(.+)", b.get("pipeline_output", ""))
    if b.get("office_alert"):
        notes.insert(0, b["office_alert"])
    notes_html = "".join(f"<div>⚠ {esc(n)}</div>" for n in notes) or \
                 "<div>(no flags)</div>"
    # Must Know rides at the TOP of the one stack (Martha's no-hunting rule)
    mk = get_must_know(b.get("address"))
    if mk:
        notes_html = (f"<div style='font-weight:600'>📌 MUST KNOW "
                      f"(this property): {esc(mk)}</div>") + notes_html

    prior = property_history(b.get("address"), stamp)
    history_card = ""
    if prior:
        rows = "".join(
            f"<div>🏠 <a href='/bid/{s}'>{esc(r.get('from'))[:34]}</a> — "
            f"{esc(r.get('kind'))}, {s[:8]}</div>" for s, r in prior[:5])
        history_card = ("<div class='card'><h3 style='margin-top:0'>We've "
                        "seen this home before</h3>" + rows + "</div>")

    hist = similar_history(b.get("services") or [])
    hist_html = "".join(
        f"<div>#{h['invoice']} {esc(h['client'])[:24]} — honored gap "
        f"${h['honored_gap']:.0f} ({h['date'][:10]})</div>"
        for h in hist) or "<div>(no honor history for this service mix)</div>"

    duplicate_forms = ""
    if b.get("duplicate_of"):
        duplicate_forms = f"""
  <div style='background:#fdecea;border-radius:6px;padding:8px;margin:6px 0'>
   Possible duplicate of <a href='/bid/{esc(b["duplicate_of"])}'>
   {esc(b["duplicate_of"])}</a> — is it the same job?
   <form method='POST' action='/duplicate' style='display:inline'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='customer' value='{esc(b['from'])}'>
    <input type='hidden' name='linked' value='{esc(b["duplicate_of"])}'>
    <button name='verdict' value='duplicate_same' class='gray'>Same job
    (link &amp; close)</button>
    <button name='verdict' value='duplicate_new'>New job (keep)</button>
   </form></div>"""

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
 {gallery_card}
 {history_card}
 <div class='card'><h3 style='margin-top:0'>All notes — one stack</h3>
  <div class='notes'>{notes_html}</div>
  {"<form method='POST' action='/must_know' style='margin-top:8px'>"
   f"<input type='hidden' name='stamp' value='{stamp}'>"
   f"<input type='hidden' name='address' value='{esc(b.get('address'))}'>"
   f"<input type='text' name='text' value='{esc(mk)}' placeholder="
   "'Must Know for this property (gate code, dog, sprinklers…)'>"
   "<button class='gray' style='margin-top:4px'>Save Must Know</button>"
   "</form>" if b.get("address") else
   "<div style='color:#888;font-size:13px;margin-top:8px'>Must Know "
   "needs an address on the request — none was parsed here.</div>"}</div>
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
  {duplicate_forms}
  <form method='POST' action='/hold' style='margin-top:6px'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <select name='hold_reason' style='padding:7px;border-radius:6px'>
    {''.join(f"<option value='{r}'>{r.replace('_', ' ')}</option>"
             for r in HOLD_REASONS)}
   </select>
   until <input type='date' name='hold_until'
                style='padding:6px;border-radius:6px'>
   <button class='gray'>Hold (auto-resurfaces)</button>
   <div style='font-size:12px;color:#888'>Hold parks the WORK, never the
   reply — customer still gets answered with the timeline.</div>
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


def drafts_page():
    """Everything the system wrote FOR the office to copy out by hand."""
    import templates as T
    sections = ""
    for title, folder, hint in (
            ("Photo requests & replies", T.OUTBOX,
             "Copy into Gmail if you like it — the system never sends."),
            ("Escalations to Dallon/Tom", T.ESCALATIONS,
             "Standardized form, same fields every time.")):
        files = (sorted(folder.glob("*.txt"), reverse=True)
                 if folder.exists() else [])
        items = ""
        for f in files[:20]:
            items += (f"<details style='margin:6px 0'><summary>{esc(f.name)}"
                      f"</summary><pre>{esc(f.read_text())}</pre></details>")
        sections += (f"<div class='card'><h3 style='margin-top:0'>{title}"
                     f"</h3><div style='color:#888;font-size:13px'>{hint}"
                     f"</div>{items or '<div>(none yet)</div>'}</div>")
    return page("Drafts", sections)


def _slug(address):
    return re.sub(r"[^a-z0-9]+", "-", (address or "").lower()).strip("-")[:60]


MUSTKNOW_FILE = BASE / "data" / "must_know.json"


def get_must_know(address):
    """Per-PROPERTY standing notes (LaRee's rule: keyed to the address,
    not the customer — survives owner changes)."""
    slug = _slug(address)
    if not slug:
        return ""
    if clouddb.available():
        return clouddb.get_blob(f"mustknow:{slug}") or ""
    if MUSTKNOW_FILE.exists():
        return json.loads(MUSTKNOW_FILE.read_text()).get(slug, "")
    return ""


def set_must_know(address, text):
    slug = _slug(address)
    if not slug:
        return
    if clouddb.available():
        clouddb.put_blob(f"mustknow:{slug}", text)
        return
    data = (json.loads(MUSTKNOW_FILE.read_text())
            if MUSTKNOW_FILE.exists() else {})
    data[slug] = text
    MUSTKNOW_FILE.write_text(json.dumps(data, indent=1))


def property_history(address, current_stamp):
    """Have we seen THIS HOME before? Prior requests at the same address,
    regardless of who owned it (LaRee's property-first rule)."""
    slug = _slug(address)
    if not slug:
        return []
    hits = []
    for stamp, rec in _shadow_source():
        if stamp == current_stamp:
            continue
        if _slug(rec.get("address")) == slug:
            hits.append((stamp, rec))
    return hits


def brief_page():
    """Latest morning brief — cloud blob first, local file fallback."""
    text = None
    if clouddb.available():
        text = clouddb.get_blob("brief")
    if not text:
        briefs = sorted((BASE / "data" / "briefs").glob("brief-*.txt")) \
            if (BASE / "data" / "briefs").exists() else []
        text = briefs[-1].read_text() if briefs else None
    body = (f"<div class='card'><pre style='font-size:14px'>{esc(text)}</pre>"
            "</div>" if text else
            "<div class='card'>No brief yet — the night run writes one "
            "each evening.</div>")
    return page("Morning brief", body)


# ── server ───────────────────────────────────────────────────

def _password():
    """DASHBOARD_PASSWORD in .env or environment. Unset = local-only
    mode, no login (the default today). REQUIRED before this ever runs
    on the internet — deploy docs enforce it."""
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DASHBOARD_PASSWORD="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("DASHBOARD_PASSWORD", "")


class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        pw = _password()
        if not pw:
            return HOST in ("127.0.0.1", "localhost")   # no pw = local only
        import base64
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                got = base64.b64decode(hdr[6:]).decode()
                return got.split(":", 1)[-1] == pw
            except Exception:
                return False
        return False

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="Master Butler office"')
        self.end_headers()
        self.wfile.write(b"login required")

    def _send(self, content, code=200, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if not self._authed():
            return self._require_auth()
        if self.path == "/":
            return self._send(home_page())
        if self.path == "/drafts":
            return self._send(drafts_page())
        if self.path == "/brief":
            return self._send(brief_page())
        m = re.match(r"^/bid/([\w-]+)$", self.path)
        if m:
            return self._send(bid_page(m.group(1)))
        m = re.match(r"^/photo/([\w-]+)/(\d+)$", self.path)
        if m:
            photos = bid_photos(m.group(1))
            i = int(m.group(2))
            if 0 <= i < len(photos):
                return self._send(photos[i].read_bytes(),
                                  ctype="image/jpeg")
        m = re.match(r"^/aerial/([\w.-]+)$", self.path)
        if m and (AERIAL / m.group(1)).exists():
            f = AERIAL / m.group(1)
            ctype = "image/jpeg" if f.suffix == ".jpg" else "image/png"
            return self._send(f.read_bytes(), ctype=ctype)
        m = re.match(r"^/img/([\w.-]+)/(\w+)/(\d+)$", self.path)
        if m and clouddb.available():
            data = clouddb.get_photo(m.group(1), m.group(2), int(m.group(3)))
            if data:
                return self._send(data, ctype="image/jpeg")
        return self._send(b"not found", 404)

    def do_POST(self):
        if not self._authed():
            return self._require_auth()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # ── JSON ingest API (the poller on Dallon's Mac pushes here) ──
        if self.path == "/api/ingest":
            try:
                payload = json.loads(body.decode())
                n = 0
                for item in payload.get("records", []):
                    clouddb.ingest_shadow(item["stamp"], item["record"])
                    n += 1
                for k, v in (payload.get("blobs") or {}).items():
                    clouddb.put_blob(k, v)
                    n += 1
                import base64 as _b64
                for ph in payload.get("photos", []):
                    clouddb.put_photo(ph["ref"], ph["kind"], ph["idx"],
                                      _b64.b64decode(ph["b64"]))
                    n += 1
                return self._send(json.dumps({"ok": True, "count": n}).encode(),
                                  ctype="application/json")
            except Exception as e:
                return self._send(json.dumps({"ok": False,
                                              "error": str(e)[:200]}).encode(),
                                  code=500, ctype="application/json")

        form = urllib.parse.parse_qs(body.decode())
        get = lambda k: form.get(k, [""])[0]

        if self.path == "/review":
            entry = {"stamp": get("stamp"), "action": get("action"),
                     "customer": get("customer"),
                     "reason": get("reason") or None,
                     "note": get("note") or None}
            if get("action") == "approve" and _push_enabled():
                rec_path = SHADOW / f"{get('stamp')}.json"
                rec = (json.loads(rec_path.read_text())
                       if rec_path.exists() else {})
                d = rec.get("draft")
                if d:
                    import jobber_client as jc
                    res = jc.push_approved_bid(d["customer"], d["bid"],
                                               d.get("prop_info"))
                    q = (res.get("quoteCreate", {}) or {}).get("quote", {})
                    entry["jobber_quote"] = q.get("quoteNumber") or str(res)[:120]
                else:
                    entry["jobber_quote"] = ("no structured draft on this "
                                             "record — re-run needed")
            save_review(entry)
        elif self.path == "/must_know":
            set_must_know(get("address"), get("text").strip())
        elif self.path == "/duplicate":
            save_review({"stamp": get("stamp"), "action": get("verdict"),
                         "customer": get("customer"),
                         "note": f"linked to {get('linked')}"})
        elif self.path == "/hold":
            save_review({"stamp": get("stamp"), "action": "hold",
                         "customer": get("customer"),
                         "hold_reason": get("hold_reason"),
                         "hold_until": get("hold_until") or None})
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
    if HOST not in ("127.0.0.1", "localhost") and not _password():
        raise SystemExit("REFUSING to serve beyond localhost without "
                         "DASHBOARD_PASSWORD set. Add it to .env first.")
    print(f"Master Butler dashboard → http://{HOST}:{PORT}"
          + ("  (password-protected)" if _password() else "  (local only)"))
    print("(reads data/shadow_bids, writes review log and draft templates. "
          "Nothing sends.)")
    HTTPServer((HOST, PORT), Handler).serve_forever()
