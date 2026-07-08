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

# ── QUEUE HYGIENE (Dallon's rule, Jul 7) ─────────────────────
# The office queue is for CUSTOMERS. Mail from Dallon/Tom/the company
# itself, and robot mail, goes to a collapsed drawer instead — shown,
# never dropped. Add more internal senders in data/internal_senders.txt
# (one email or domain per line).
INTERNAL_DEFAULT = ["masterbutlerinc.com", "dallon.masterbutler@gmail.com"]
NOISE_SENDERS = ["no-reply", "noreply", "donotreply", "marketing@",
                 "accounts.google.com", "notifications@", "newsletter"]


_SENDERS_CACHE = {"at": 0.0, "list": None}


def _internal_senders():
    """Cached 60s — classify_row runs per queue row; without the cache
    every row would be a database query (Martha's-machine rule)."""
    import time
    now = time.monotonic()
    if _SENDERS_CACHE["list"] is not None and now - _SENDERS_CACHE["at"] < 60:
        return _SENDERS_CACHE["list"]
    out = list(INTERNAL_DEFAULT)
    extra = BASE / "data" / "internal_senders.txt"
    if extra.exists():
        out += [l.strip().lower() for l in extra.read_text().splitlines()
                if l.strip() and not l.startswith("#")]
    if clouddb.available():
        try:
            out += [s.lower() for s in
                    (clouddb.get_blob("internal_senders") or [])]
        except Exception:
            pass
    _SENDERS_CACHE.update(at=now, list=out)
    return out


def classify_row(rec):
    """'main' (customer work) or ('aside', reason) for the drawer."""
    # Jobber EVENTS outrank every filter: an approval or change-request
    # is action for the office; receipts and the rest go to the drawer.
    ev = rec.get("jobber_event")
    if ev:
        if ev.get("event") in ("quote_approved", "changes_requested",
                               "request_received"):
            return "main", None
        return "aside", f"jobber event: {ev.get('event')}"
    sender = (rec.get("from") or "").lower()
    for s in _internal_senders():
        if s in sender:
            return "aside", "internal (Dallon/Tom/company)"
    for s in NOISE_SENDERS:
        if s in sender:
            return "aside", "robot mail"
    if "Spam" in (rec.get("folder") or "") and rec.get("kind") != "new_request":
        return "aside", "spam folder, not a request"
    # Anyone else is an OUTSIDE HUMAN — they stay in front of the office
    # no matter how the classifier labeled them (replies and follow-ups
    # often come through as 'other'; hiding a customer is the one
    # unforgivable failure).
    return "main", None


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


def quote_numbers():
    """stamp -> Jobber quote # — from the scoreboard's office matches and
    from any approve-push results. The office's 'verify in Jobber' link."""
    out = {}
    if clouddb.available():
        sb = clouddb.get_blob("scoreboard")
    else:
        p = BASE / "data" / "scoreboard.json"
        sb = json.loads(p.read_text()) if p.exists() else None
    for r in (sb or {}).get("rows", []):
        if r.get("office_quote"):
            out[r["stamp"]] = r["office_quote"]
    for r in load_reviews():
        if r.get("jobber_quote") and r.get("stamp"):
            out[r["stamp"]] = r["jobber_quote"]
    for stamp, rec in _shadow_source():     # a record can carry its own #
        if rec.get("jobber_quote"):
            out[stamp] = rec["jobber_quote"]
    return out


def quote_urls():
    """quote number -> its Jobber admin web link (jobberWebUri)."""
    urls = {}
    if clouddb.available():
        sb = clouddb.get_blob("scoreboard")
    else:
        p = BASE / "data" / "scoreboard.json"
        sb = json.loads(p.read_text()) if p.exists() else None
    for r in (sb or {}).get("rows", []):
        if r.get("office_quote") and r.get("jobber_url"):
            urls[r["office_quote"]] = r["jobber_url"]
    for stamp, rec in _shadow_source():
        if rec.get("jobber_quote") and rec.get("jobber_url"):
            urls[rec["jobber_quote"]] = rec["jobber_url"]
    return urls


def quote_chip(qnum, urls, extra_class="", label=None):
    """A Jobber quote chip that links to the quote in Jobber when we
    have its URL (opens in a new tab)."""
    text = label or f"Jobber #{esc(qnum)}"
    url = urls.get(qnum)
    if url:
        return (f"<a href='{esc(url)}' target='_blank' rel='noopener' "
                f"class='chip win {extra_class}' style='text-decoration:none'>"
                f"{text} ↗</a>")
    return f"<span class='chip win {extra_class}'>{text}</span>"


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
:root{--green:#0b3d2e;--green2:#177245;--accent:#1e8449;--gold:#c9a227;
      --bg:#f5f6f4;--ink:#20242a;--mut:#6b7280;--line:#e5e7e3}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,
     Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink);
     font-size:15px;line-height:1.45}
header{background:linear-gradient(135deg,var(--green) 0%,#11543f 100%);
       color:#fff;padding:14px 26px;font-size:19px;font-weight:700;
       border-bottom:3px solid var(--gold);letter-spacing:.2px;
       display:flex;align-items:center;flex-wrap:wrap;gap:8px}
header small{font-weight:400;opacity:.72;font-size:12.5px;margin-left:8px}
header .nav{margin-left:auto;font-size:13.5px;font-weight:600}
header .nav a{color:#dfe7e2;padding:7px 14px;border-radius:999px}
header .nav a:hover{background:rgba(255,255,255,.12);text-decoration:none}
header .nav a.active{background:var(--gold);color:#1c2b23}
.wrap{max-width:1160px;margin:0 auto;padding:20px 18px 40px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.stat{background:#fff;border:1px solid var(--line);border-radius:12px;
      padding:12px 18px;box-shadow:0 1px 2px rgba(11,61,46,.06);
      min-width:128px;display:flex;flex-direction:column-reverse}
.stat b{display:block;font-size:24px;color:var(--green);
      font-variant-numeric:tabular-nums;line-height:1.2}
.stat span{font-size:11px;color:var(--mut);text-transform:uppercase;
      letter-spacing:.7px;font-weight:600;margin-bottom:2px}
.ring{display:inline-flex;align-items:center;justify-content:center;
      width:40px;height:40px;border-radius:50%;border:3px solid;
      font-size:11.5px;font-weight:700;font-variant-numeric:tabular-nums;
      background:#fff}
.card.dark{background:linear-gradient(150deg,var(--green),#0e4a37);
      color:#eef4f0;border:0}
.card.dark h3{color:var(--gold)}
.card.dark .big{font-size:26px;font-weight:800;color:#fff;
      font-variant-numeric:tabular-nums}
.card.dark .lbl{font-size:11px;text-transform:uppercase;
      letter-spacing:.6px;color:#a7c0b3}
.subtext{font-size:12px;color:var(--mut)}
.band{background:#fff8ec;border:1px solid #f0d9a5;border-left:5px solid
      #e0a428;border-radius:10px;padding:12px 16px;margin-bottom:16px}
.band h2{margin:0 0 6px;font-size:13px;color:#8a5a00;
      text-transform:uppercase;letter-spacing:.6px}
.band div{padding:3px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;
      padding:16px 20px;margin-bottom:16px;
      box-shadow:0 1px 3px rgba(11,61,46,.06)}
.card h2{margin:0 0 10px;font-size:17px;color:var(--green)}
.card h3{margin:0 0 8px;font-size:14px;color:var(--green);
      text-transform:uppercase;letter-spacing:.5px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--mut);font-weight:600;padding:8px;
   border-bottom:2px solid var(--line);font-size:12px;
   text-transform:uppercase;letter-spacing:.4px}
td{padding:10px 8px;border-bottom:1px solid #f0f1ee;vertical-align:top}
tr:hover td{background:#f7faf8}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.age{font-weight:700;font-variant-numeric:tabular-nums}
.age.warn{color:#c77700}.age.late{color:#c0392b}
.chip{display:inline-block;background:#eef3f0;border-radius:999px;
      padding:2px 11px;margin:2px 3px 2px 0;font-size:12px;color:#3f5147}
.flag{background:#fdecea;color:#a93226}
.win{background:#e6f4ea;color:#1e6b34;font-weight:600}
.ok{color:var(--accent);font-weight:600}
a{color:var(--green2);text-decoration:none}a:hover{text-decoration:underline}
pre{background:#f7f8f6;border:1px solid var(--line);border-radius:8px;
    padding:12px;font-size:12.5px;overflow-x:auto;white-space:pre-wrap}
.notes{background:#fffdf2;border:1px solid #ece3b8;border-radius:10px;
       padding:10px 14px}
.notes div{padding:4px 0;border-bottom:1px dashed #eee}
.notes div:last-child{border-bottom:0}
button,.btn{background:var(--green2);color:#fff;border:0;border-radius:8px;
       padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer;
       margin:3px 3px 3px 0;transition:filter .12s}
button:hover{filter:brightness(1.08)}
button.big{padding:12px 22px;font-size:15px}
button.gray{background:#8a949c}button.red{background:#b03a2e}
.reason{background:#fff;color:var(--green2);border:1.5px solid var(--green2);
        font-weight:500;padding:7px 12px}
.reason.sel{background:var(--green2);color:#fff}
input[type=text],input[type=date],select,textarea{width:100%;padding:9px;
       border:1px solid #ccd1cb;border-radius:8px;font-size:14px}
input[type=date],select{width:auto}
.grid{display:grid;grid-template-columns:5fr 3fr;gap:16px}
details.card summary{cursor:pointer;font-weight:600;color:var(--mut)}
details.card[open] summary{margin-bottom:8px}
.headline{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.headline .total{font-size:30px;font-weight:800;color:var(--green)}
.confbadge{border-radius:10px;padding:6px 14px;font-weight:700;
       font-size:15px;color:#fff}
footer{max-width:1160px;margin:8px auto 26px;padding:0 18px;
       color:#a3aaa2;font-size:12px}
@media(max-width:860px){.grid{grid-template-columns:1fr}
       header{font-size:16px}.headline .total{font-size:24px}}
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
            f"<header>🎩 Master Butler <small>Bid Review — "
            f"{'approve pushes DRAFT quotes to Jobber' if _push_enabled() else 'shadow mode · nothing sends without you'}"
            f"</small>"
            + "<span class='nav'>"
            + "".join(f"<a href='{href}' class="
                      f"'{'active' if title == t else ''}'>{label}</a> "
                      for href, label, t in (
                          ("/", "Queue", "Bid queue"),
                          ("/new", "+ New lead", "New lead"),
                          ("/drafts", "Drafts", "Drafts"),
                          ("/scoreboard", "Scoreboard", "Scoreboard"),
                          ("/brief", "Morning brief", "Morning brief")))
            + "</span></header>"
            f"<div class='wrap'>{body}</div>"
            f"<footer>Every quote is a draft until a human sends it · the "
            f"inbox is never marked read · every price traces to a real "
            f"job.</footer></body></html>").encode()


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
    quotes = quote_numbers()
    qurls = quote_urls()
    pending = [b for b in bids if not b["reviewed"]
               and b["stamp"] not in live_holds]
    queue, aside = [], []
    for b in pending:
        lane, why = classify_row(b)
        if lane == "main":
            queue.append(b)
        else:
            aside.append((b, why))
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
    reviews_all = load_reviews()
    today = datetime.now().date().isoformat()
    decided_today = [r for r in reviews_all
                     if (r.get("at") or "").startswith(today)]
    wins = sum(1 for b in bids
               if (b.get("jobber_event") or {}).get("event") == "quote_approved")
    new_reqs = [b for b in queue if b.get("kind") in ("new_request",
                                                      "phone_lead")]
    oldest = max((b["age_hours"] for b in queue), default=0)

    # the ears: is the Mac-side poller alive?
    ears = ""
    if clouddb.available():
        hb = clouddb.get_blob("poller_heartbeat") or {}
        if hb.get("at"):
            try:
                last = datetime.fromisoformat(hb["at"])
                mins = (datetime.now() - last).total_seconds() / 60
                if mins > 15:
                    attention.insert(0, ({"stamp": "", "from": "SYSTEM"},
                        f"the inbox watcher has been silent {mins:.0f} min "
                        "— new emails are NOT being captured (is Dallon's "
                        "Mac asleep?)"))
                ears = (f"<div class='stat'><b style='color:"
                        f"{'#1e8449' if mins <= 15 else '#b03a2e'}'>"
                        f"{mins:.0f}m</b><span>ears last heard</span></div>")
            except ValueError:
                pass

    stats = (f"<div class='stats'>"
             f"<div class='stat'><b>{len(queue)}</b><span>waiting</span></div>"
             f"<div class='stat'><b>{len(new_reqs)}</b><span>bid requests</span></div>"
             f"<div class='stat'><b>{oldest:.0f}h</b><span>oldest wait</span></div>"
             f"<div class='stat'><b>{len(decided_today)}</b><span>decided today</span></div>"
             f"<div class='stat'><b>{wins}</b><span>quote wins 🎉</span></div>"
             f"{ears}</div>")

    band = ""
    if attention:
        rows = "".join(
            (f"<div>⚠ <a href='/bid/{b['stamp']}'><b>{esc(b['from'])}</b>"
             f"</a> — {esc(why)}</div>") if b.get("stamp") else
            f"<div>🔴 <b>{esc(b['from'])}</b> — {esc(why)}</div>"
            for b, why in attention)
        band = f"<div class='band'><h2>Needs attention</h2>{rows}</div>"

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
        q = quotes.get(b["stamp"])
        name = esc(b["from"]).split("&lt;")[0].strip() or esc(b["from"])
        sub = (quote_chip(q, qurls) if q else
               esc(b["from"]).split("&lt;")[-1].rstrip("&gt;")[:34])
        ring = ("—" if c is None else
                f"<span class='ring' style='border-color:"
                f"{'#1e8449' if c >= 75 else '#c77700' if c >= 50 else '#b03a2e'};"
                f"color:{'#1e8449' if c >= 75 else '#c77700' if c >= 50 else '#b03a2e'}'>"
                f"{c}%</span>")
        rows += (f"<tr><td>{age_html(b['age_hours'])}</td>"
                 f"<td><a href='/bid/{b['stamp']}'><b>{name}</b></a>"
                 f"<div class='subtext'>{sub}</div></td>"
                 f"<td>{esc(b.get('kind'))}</td><td>{services}{flags}</td>"
                 f"<td>{ring}</td><td class='num'><b>{total}</b></td></tr>")
    if not rows:
        rows = "<tr><td colspan=6>Queue is empty — all caught up. ✅</td></tr>"

    aside_html = ""
    if aside:
        items = "".join(
            f"<div>· <a href='/bid/{b['stamp']}'>{esc(b['from'])[:44]}</a> "
            f"<span style='color:#888'>({esc(why)})</span></div>"
            for b, why in aside)
        aside_html = (f"<details class='card'><summary style='cursor:pointer;"
                      f"color:#666'>Internal &amp; other mail "
                      f"({len(aside)}) — not customer work</summary>"
                      f"{items}</details>")

    reviews = load_reviews()[-8:][::-1]
    rev_rows = "".join(
        f"<div>✅ {esc(r.get('action'))} — {esc(r.get('customer', r.get('stamp')))}"
        f"{(' · ' + esc(r['reason'])) if r.get('reason') else ''}</div>"
        for r in reviews) or "<div>No reviews yet.</div>"

    body = (stats + band +
        "<div class='grid'><div class='card'>"
        "<h2 style='margin-top:0'>Bid queue — oldest first</h2>"
        "<table><tr><th>Waiting</th><th>From</th><th>Kind</th>"
        "<th>Services</th><th>Conf.</th><th class='num'>Est.</th></tr>" + rows +
        "</table>" + aside_html + "</div>"
        "<div>" + scoreboard_card() + ideas_card() +
        held_card(live_holds, bids) +
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
    if rows:
        sys_t = sum(r["system_total"] for r in rows)
        off_t = sum(r["office_total"] for r in rows)
        bump = (100 * (off_t - sys_t) / sys_t) if sys_t else 0
        bcol = "#7fd6a2" if abs(bump) <= 10 else "#f0c987"
        inner = (
            "<div style='display:flex;gap:22px;margin:6px 0 10px'>"
            f"<div><div class='lbl'>System est.</div>"
            f"<div class='big'>${sys_t:,.0f}</div></div>"
            f"<div><div class='lbl'>Office final</div>"
            f"<div class='big'>${off_t:,.0f}</div></div></div>"
            f"<div class='lbl'>Gap <b style='color:{bcol}'>{bump:+.1f}%</b>"
            f" across {len(rows)} matched quote(s)</div>"
            "<div style='margin-top:8px;font-size:13px'>"
            + "".join(f"<div>{esc((r.get('customer') or '?')[:24])} "
                      f"— {r['gap_pct']:+.0f}%</div>" for r in rows[:4])
            + "</div>")
    else:
        inner = (f"<div style='color:#a7c0b3;font-size:13.5px'>{waiting} "
                 "shadow draft(s) waiting for the office to quote them — "
                 "the report card fills itself in.</div>")
    return (f"<div class='card dark'><h3 style='margin-top:0'>Shadow "
            f"scoreboard</h3>{inner}"
            "<div style='margin-top:8px'><a href='/scoreboard' "
            "style='color:#e8d9a0;font-size:13px'>Full scoreboard →</a>"
            "</div></div>")


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

    my_quote = quote_numbers().get(stamp)   # computed ONCE per page

    # ── structured draft: headline, price table, measurements ──
    d = b.get("draft") or {}
    bid_d = d.get("bid") or {}
    conf = b.get("confidence")
    conf_color = ("#1e8449" if (conf or 0) >= 75 else
                  "#c77700" if (conf or 0) >= 50 else "#b03a2e")
    draft_headline = ""
    if d.get("total") is not None:
        draft_headline = (
            "<div class='headline'><div>"
            "<div style='font-size:11px;color:var(--mut);text-transform:"
            "uppercase;letter-spacing:.7px;font-weight:600'>Total quote</div>"
            f"<span class='total'>${d['total']:,.0f}</span></div>"
            + (f"<span class='ring' style='width:48px;height:48px;"
               f"border-color:{conf_color};color:{conf_color};"
               f"font-size:13px'>{conf}%</span>" if conf is not None else "")
            + f"<span class='chip'>{esc(b.get('kind'))}</span></div>")
    price_card = ""
    if bid_d.get("services"):
        lines = "".join(
            f"<tr><td>{esc(s['name'])}</td>"
            f"<td class='num'>${s['price']:,.0f}</td></tr>"
            for s in bid_d["services"])
        price_card = (
            "<div class='card'><h3>Proposed line items</h3><table>"
            "<tr><th>Service</th><th class='num'>Price</th></tr>" + lines +
            f"<tr style='background:#f3f4f1'><td><b>Total estimate</b></td>"
            f"<td class='num'><b>${d.get('total', 0):,.0f}</b></td></tr>"
            "</table></div>")
    pi = d.get("prop_info") or {}
    measure_card = ""
    if any(pi.get(k) for k in ("sqft", "pitch", "roof_material", "stories")):
        cells = "".join(
            f"<div class='stat'><b style='font-size:16px'>{esc(v)}</b>"
            f"<span>{label}</span></div>"
            for label, v in (
                ("house sqft", f"{pi['sqft']:,}" if pi.get("sqft") else None),
                ("source", pi.get("sqft_source")),
                ("stories", pi.get("stories")),
                ("pitch", pi.get("pitch")),
                ("roof", pi.get("roof_material")))
            if v)
        measure_card = ("<div class='card'><h3>Measurements it used</h3>"
                        f"<div class='stats' style='margin:0'>{cells}"
                        "</div></div>")

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
  <h2 style='margin-top:0'>{esc(b['from'])} {age_html(b['age_hours'])}
  {quote_chip(my_quote, quote_urls(),
              label=f"Open quote #{esc(my_quote)} in Jobber")
   if my_quote else ''}</h2>
  {draft_headline}
  <div style='color:var(--mut);margin-top:6px'>
   <b>Subject:</b> {esc(b.get('subject'))} &nbsp;·&nbsp;
   <b>Address:</b> {f"<a href='/property/{_slug(b.get('address'))}'>{esc(b.get('address'))}</a>"
                    if b.get('address') else '— not found'} &nbsp;·&nbsp;
   {esc(b.get('folder', 'INBOX'))}</div>
 </div>
 {price_card}
 {measure_card}
 {f"""<div class='card' style='border-left:4px solid var(--gold);
   background:#fbfaf5'><h3>What the customer said</h3>
  <div style='font-style:italic;color:#3a4046;font-size:15px'>&ldquo;{esc(b.get('newest_message'))}&rdquo;</div>
  </div>""" if b.get('newest_message') else ''}
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
 <details class='card'><summary>Raw system output (full trace)</summary>
  <pre>{esc(b.get('pipeline_output') or '(no draft — ' +
             esc(b.get('kind')) + ')')}</pre></details>
</div><div>
 <div class='card'><h3 style='margin-top:0'>Decide</h3>
  <form method='POST' action='/review'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <input type='hidden' id='reason' name='reason' value=''>
   <div style='margin-bottom:6px'>{reasons}</div>
   <input type='text' name='note' placeholder='optional: teach it in one line'>
   <div style='margin-top:10px'>
    <button name='action' value='approve' class='big'>✓ Approve as-is</button>
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
  {f'''<form method='POST' action='/repeat_welcome' style='margin-top:6px'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <button class='gray'>Draft welcome-back reply</button>
  </form>''' if prior else ''}
 </div>
 <div class='card'><h3 style='margin-top:0'>Similar homes (honor history)</h3>
  {hist_html}</div>
</div></div>"""
    return page("Review bid", body)


MANUAL_SERVICES = [
    ("gutters", "Gutter cleaning"), ("roof", "Roof blow-off"),
    ("moss", "Moss treatment"), ("windows", "Windows (exterior)"),
    ("windows_inout", "Windows (in & out)"), ("driveway", "PW driveway"),
    ("patio", "PW patio"), ("sidewalk", "PW walkway"),
    ("house_wash", "House wash"), ("dryer_vent", "Dryer vent"),
]


def new_lead_page(msg=""):
    """The office types a lead (e.g. a tech's curbside contact); it runs
    through the full pipeline just like an inbound email."""
    checks = "".join(
        f"<label style='display:inline-block;min-width:180px;margin:4px 0'>"
        f"<input type='checkbox' name='svc' value='{k}'> {esc(v)}</label>"
        for k, v in MANUAL_SERVICES)
    banner = (f"<div class='band'><h2>Working on it</h2><div>{esc(msg)}</div>"
              "</div>" if msg else "")
    body = f"""{banner}
<div class='card' style='max-width:640px'>
 <h2 style='margin-top:0'>New lead — enter it like an email came in</h2>
 <div class='subtext' style='margin-bottom:12px'>For a tech's curbside
  contact or a phone lead. The system looks up the property from the
  satellite, prices it, and drops a draft on the queue — same as an email.</div>
 <form method='POST' action='/new'>
  <div style='margin-bottom:8px'><b>Customer name</b>
   <input type='text' name='name' required></div>
  <div style='margin-bottom:8px'><b>Property address</b>
   <input type='text' name='address' required
          placeholder='street, city, WA zip'></div>
  <div style='display:flex;gap:10px;margin-bottom:8px'>
   <div style='flex:1'><b>Phone</b><input type='text' name='phone'></div>
   <div style='flex:1'><b>Email (optional)</b><input type='text' name='email'></div>
  </div>
  <div style='margin:10px 0'><b>Services requested</b><br>{checks}</div>
  <div style='margin-bottom:8px'><b>Notes (optional)</b>
   <input type='text' name='extra'
          placeholder='e.g. heavy moss on north side, gate code 1234'></div>
  <button class='big'>Create draft from this lead</button>
 </form>
</div>"""
    return page("New lead", body)


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


IDEAS_FILE = BASE / "data" / "ideas.json"


def load_ideas():
    if clouddb.available():
        return clouddb.get_blob("ideas") or []
    if IDEAS_FILE.exists():
        return json.loads(IDEAS_FILE.read_text())
    return []


def save_ideas(ideas):
    if clouddb.available():
        clouddb.put_blob("ideas", ideas)
        return
    IDEAS_FILE.write_text(json.dumps(ideas, indent=1))


def add_idea(who, text):
    ideas = load_ideas()
    ideas.append({"at": datetime.now().isoformat(timespec="seconds"),
                  "who": who or "office", "text": text.strip(),
                  "status": "open"})
    save_ideas(ideas)


def ideas_card():
    """The office's direct line: 'Dallon, I thought of this…'"""
    ideas = load_ideas()
    open_ideas = [(i, x) for i, x in enumerate(ideas)
                  if x.get("status") == "open"]
    rows = "".join(
        f"<div style='padding:6px 0;border-bottom:1px dashed #eee'>"
        f"💡 <b>{esc(x['who'])}</b>: {esc(x['text'])[:120]}"
        f"<form method='POST' action='/idea_done' style='display:inline'>"
        f"<input type='hidden' name='idx' value='{i}'>"
        f"<button class='gray' style='padding:2px 8px;font-size:11px;"
        f"margin-left:6px'>done</button></form></div>"
        for i, x in open_ideas) or \
        "<div class='subtext'>No open ideas — the box is below.</div>"
    return f"""<div class='card'>
<h3 style='margin-top:0'>💡 Ideas for Dallon
 {f"<span class='chip win'>{len(open_ideas)} open</span>" if open_ideas else ''}</h3>
{rows}
<form method='POST' action='/idea' style='margin-top:10px'>
 <input type='text' name='who' placeholder='your name'
        style='width:38%;margin-bottom:6px'>
 <input type='text' name='text'
        placeholder='Dallon, I thought of this — can we add/take away…'>
 <button class='gray' style='margin-top:6px'>Send to Dallon</button>
</form></div>"""


def scoreboard_page():
    """Full scoreboard table — every shadow draft vs the office."""
    if clouddb.available():
        sb = clouddb.get_blob("scoreboard")
    else:
        p = BASE / "data" / "scoreboard.json"
        sb = json.loads(p.read_text()) if p.exists() else None
    if not sb:
        return page("Scoreboard", "<div class='card'>No scoreboard yet — "
                    "the night run generates it.</div>")
    rows = ""
    for r in sb["rows"]:
        if r.get("office_quote"):
            gap = r.get("gap_pct")
            color = ("#1e8449" if abs(gap) <= 10 else
                     "#c77700" if abs(gap) <= 25 else "#c0392b")
            qlink = (f"<a href='{esc(r['jobber_url'])}' target='_blank' "
                     f"rel='noopener'>#{r['office_quote']} ↗</a>"
                     if r.get("jobber_url") else f"#{r['office_quote']}")
            verdict = (f"<b style='color:{color}'>{gap:+.0f}%</b> "
                       f"(office {qlink}, {r['office_status']})")
            office = f"${r['office_total']:.0f}"
        else:
            office, verdict = "—", "<span style='color:#888'>awaiting office quote</span>"
        rows += (f"<tr><td>{esc((r.get('customer') or '?')[:36])}</td>"
                 f"<td>{', '.join(r.get('services') or [])}</td>"
                 f"<td>${r['system_total']:.0f}</td><td>{office}</td>"
                 f"<td>{verdict}</td></tr>")
    body = (f"<div class='card'><h2 style='margin-top:0'>Shadow scoreboard"
            f"</h2><div style='color:#888;font-size:13px'>Generated "
            f"{esc(sb.get('generated', ''))} — green ≤10% of the office, "
            "amber ≤25%, red beyond.</div>"
            "<table><tr><th>Customer</th><th>Services</th><th>System</th>"
            f"<th>Office</th><th>Verdict</th></tr>{rows}</table></div>")
    return page("Scoreboard", body)


def property_page(slug):
    """Everything we know about ONE ADDRESS — requests across owners,
    Must Know, imagery. LaRee's property-first rule as a page."""
    matches = [(s, r) for s, r in _shadow_source()
               if _slug(r.get("address")) == slug]
    if not matches:
        return page("Property", "<div class='card'>No records for this "
                    "property yet.</div>")
    address = matches[-1][1].get("address")
    mk = get_must_know(address)
    quotes = quote_numbers()
    rows = "".join(
        f"<tr><td>{s[:4]}-{s[4:6]}-{s[6:8]}</td>"
        f"<td><a href='/bid/{s}'>{esc(r.get('from'))[:40]}</a></td>"
        f"<td>{esc(r.get('kind'))}</td>"
        f"<td>{', '.join(r.get('services') or []) or '—'}</td>"
        f"<td>{('Jobber #' + esc(quotes[s])) if s in quotes else '—'}</td>"
        f"</tr>" for s, r in reversed(matches))
    gallery = ""
    if clouddb.available():
        for ref, kind, idx in clouddb.photos_index([slug]):
            gallery += (f"<a href='/img/{ref}/{kind}/{idx}' target='_blank'>"
                        f"<img src='/img/{ref}/{kind}/{idx}' "
                        "style='height:130px;margin:4px;border-radius:8px'>"
                        "</a>")
    body = f"""
<div class='card'><h2 style='margin-top:0'>🏠 {esc(address)}</h2>
 {f"<div class='notes'><b>📌 MUST KNOW:</b> {esc(mk)}</div>" if mk else ''}
 <form method='POST' action='/must_know' style='margin-top:8px'>
  <input type='hidden' name='address' value='{esc(address)}'>
  <input type='hidden' name='stamp' value=''>
  <input type='text' name='text' value='{esc(mk)}'
         placeholder='Must Know for this property'>
  <button class='gray' style='margin-top:4px'>Save Must Know</button>
 </form></div>
{f"<div class='card'><h3>Imagery</h3>{gallery}</div>" if gallery else ''}
<div class='card'><h3>Every request at this address (any owner)</h3>
 <table><tr><th>Date</th><th>From</th><th>Kind</th><th>Services</th>
 <th>Quote</th></tr>{rows}</table></div>"""
    return page("Property", body)


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
        if self.path == "/health":          # no auth, no data — lets the
            return self._send(b"ok")        # poller keep the service warm
        if not self._authed():
            return self._require_auth()
        if self.path == "/":
            return self._send(home_page())
        if self.path == "/scoreboard":
            return self._send(scoreboard_page())
        if self.path == "/drafts":
            return self._send(drafts_page())
        if self.path == "/brief":
            return self._send(brief_page())
        if self.path.startswith("/new"):
            m = "Lead submitted — it's running through the pipeline and will "\
                "appear on the queue in a moment." if "msg=" in self.path else ""
            return self._send(new_lead_page(m))
        m = re.match(r"^/property/([\w-]+)$", self.path)
        if m:
            return self._send(property_page(m.group(1)))
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
        if self.path == "/api/reviews":       # the Mac pulls decisions down
            return self._send(json.dumps(load_reviews()).encode(),
                              ctype="application/json")
        return self._send(b"not found", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # ── Jobber webhook (real-time events; verified by HMAC, not
        #    password — Jobber signs each delivery with the app secret).
        #    DARK until Dallon enables webhooks in the Jobber dev portal.
        if self.path == "/webhooks/jobber":
            import hashlib, hmac as _hmac, base64 as _b64, os as _os
            secret = _os.environ.get("JOBBER_CLIENT_SECRET", "")
            sig = self.headers.get("X-Jobber-Hmac-SHA256", "")
            want = _b64.b64encode(_hmac.new(secret.encode(), body,
                                            hashlib.sha256).digest()).decode()
            if not (secret and sig and _hmac.compare_digest(sig, want)):
                return self._send(b"bad signature", 401)
            try:
                ev = json.loads(body.decode())
                topic = (ev.get("data", {}).get("webHookEvent", {})
                         .get("topic", "unknown"))
                log = clouddb.get_blob("jobber_webhooks") or []
                log.append({"at": datetime.now().isoformat(timespec="seconds"),
                            "topic": topic, "raw": ev})
                clouddb.put_blob("jobber_webhooks", log[-200:])
            except Exception:
                pass
            return self._send(b"ok")

        if not self._authed():
            return self._require_auth()

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
        elif self.path == "/repeat_welcome":
            name = get("customer").split("<")[0].strip()
            promise = ""
            try:
                from promises import promises_for
                hits = promises_for(name)
                if hits:
                    promise = (f"we have your ${hits[0]['promised_price']:.0f} "
                               "price on file from last time, and we're "
                               "honoring it")
            except Exception:
                pass
            path = templates.draft_repeat_welcome(name, promise_note=promise)
            save_review({"stamp": get("stamp"), "action": "welcome_drafted",
                         "customer": get("customer"),
                         "note": f"draft: {path.name}"})
        elif self.path == "/new":
            # run the full pipeline in the background (it's ~20s); the lead
            # appears on the queue on the next auto-refresh
            import threading
            svcs = form.get("svc", [])
            kw = dict(name=get("name"), address=get("address"),
                      phone=get("phone"), email=get("email"),
                      services=svcs, extra=get("extra"),
                      entered_by="office")
            def run():
                try:
                    from manual import process_manual
                    process_manual(**kw)
                except Exception:
                    pass
            threading.Thread(target=run, daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "/new?msg=working")
            self.end_headers()
            return
        elif self.path == "/idea":
            if get("text").strip():
                add_idea(get("who"), get("text"))
        elif self.path == "/idea_done":
            ideas = load_ideas()
            i = int(get("idx") or -1)
            if 0 <= i < len(ideas):
                ideas[i]["status"] = "done"
                save_ideas(ideas)
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
