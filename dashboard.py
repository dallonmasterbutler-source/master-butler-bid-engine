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
# Customer-reply kill switch: Dallon flips this to True when the
# office is ready to send from the Messages page.
REPLIES_ENABLED = False


SERVICE_LABELS = {
    "gutter_cleaning": "Gutters", "roof_blow_off": "Roof blow off",
    "roof_blow_off_guards": "Roof (over guards)",
    "moss_treatment": "Moss treatment", "moss_removal": "Moss removal",
    "windows_exterior": "Windows (ext)", "windows_in_out": "Windows (in+out)",
    "windows_unspecified": "Windows", "house_wash": "House wash",
    "pw_driveway": "Driveway wash", "pw_patio": "Patio wash",
    "pw_sidewalk": "Sidewalk wash", "pw_deck": "Deck wash",
    "pressure_washing": "Pressure washing", "dryer_vent": "Dryer vent",
    "holiday_lights": "Holiday lights",
}


def svc_label(s):
    return SERVICE_LABELS.get(s, (s or "").replace("_", " ").capitalize())


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


DECIDED_ACTIONS = ("approve", "adjusted", "duplicate_same",
                   "combined")


def flagged_for_review():
    """Bids LaRee sent to Tom & Dallon, minus ones they've marked seen."""
    flagged, seen = {}, set()
    for r in load_reviews():
        if r.get("action") == "flag_review":
            flagged[r["stamp"]] = r
        elif r.get("action") == "review_seen":
            seen.add(r.get("stamp"))
    return [v for k, v in flagged.items() if k not in seen]

# ── QUEUE HYGIENE (Dallon's rule, Jul 7) ─────────────────────
# The office queue is for CUSTOMERS. Mail from Dallon/Tom/the company
# itself, and robot mail, goes to a collapsed drawer instead — shown,
# never dropped. Add more internal senders in data/internal_senders.txt
# (one email or domain per line).
INTERNAL_DEFAULT = ["masterbutlerinc.com", "dallon.masterbutler@gmail.com"]
NOISE_SENDERS = ["no-reply", "noreply", "donotreply", "marketing@",
                 "accounts.google.com", "notifications@", "newsletter",
                 "invoice+statements", "@stripe.com", "receipts@"]


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


_SPAM_CACHE = {"at": 0.0, "v": []}


def _learned_spam():
    import time as _t
    if _t.time() - _SPAM_CACHE["at"] > 60:
        _SPAM_CACHE["v"] = _blob_rw("learned_spam", [])
        _SPAM_CACHE["at"] = _t.time()
    return _SPAM_CACHE["v"]


def _looks_solicitation(rec):
    """Someone selling TO us, not a homeowner — the full learned filter
    lives in spam_filter.py (trained on the Jul 8 study of all 179
    account emails, locked by test_spam_filter.py)."""
    import spam_filter
    is_spam, why = spam_filter.looks_spam(
        rec.get("from"), rec.get("subject"),
        rec.get("newest_message") or "",
        has_address=bool(rec.get("address")),
        list_unsub=bool(rec.get("list_unsub")),
        kind=rec.get("kind") or "")
    return why if is_spam else ""


def classify_row(rec):
    """'main' (customer work) or ('aside', reason) for the drawer."""
    # Jobber EVENTS outrank every filter: an approval or change-request
    # is action for the office; receipts and the rest go to the drawer.
    if rec.get("merged_into"):
        return "aside", "update folded into the customer's bid"
    ev = rec.get("jobber_event")
    if ev:
        if ev.get("event") in ("quote_approved", "changes_requested",
                               "request_received"):
            return "main", None
        return "aside", f"jobber event: {ev.get('event')}"
    if rec.get("spam_auto"):          # the poller already ruled at intake
        return "aside", f"spam — {rec['spam_auto'][:80]}"
    sender = (rec.get("from") or "").lower()
    # internal FIRST: Dallon/Tom's own strategy mail must never be able
    # to trip the sales-phrase lexicon
    for s in _internal_senders():
        if s in sender:
            return "aside", "internal (Dallon/Tom/company)"
    for s in _learned_spam():
        if s and s in sender:
            return "aside", "spam (office taught me this sender)"
    why_spam = _looks_solicitation(rec)
    if why_spam:
        return "aside", f"spam — {why_spam[:80]}"
    for s in NOISE_SENDERS:
        if s in sender:
            return "aside", "robot mail"
    if "Spam" in (rec.get("folder") or "") and rec.get("kind") != "new_request":
        return "aside", "spam folder, not a request"
    # CONVERSATIONS (Dallon, Jul 8 — the Winward "Great! Thank you!😊"):
    # a customer replying pleasantries in a scheduling thread isn't a
    # bid. Visible in its own drawer — shown, never hidden — but off
    # the work queue. CONSERVATIVE: any question mark, any service, or
    # any real length keeps them on the main queue.
    if rec.get("kind") in ("scheduling", "other") \
            and not rec.get("services"):
        msg = (rec.get("newest_message") or "").strip()
        is_reply = (rec.get("subject") or "").lower().startswith("re:")
        pleasantry = re.match(
            r"^(great|perfect|awesome|sounds good|thank(s| you)|ok(ay)?|"
            r"got it|see you|will do|no problem)\b", msg.lower())
        if is_reply and "?" not in msg and (
                (pleasantry and len(msg.split()) <= 30) or len(msg) <= 60):
            return "chatter", "reply in an office thread — no ask"
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


CLAIM_FRESH_MIN = 15          # someone is "working on it" this long


def _claims():
    """{stamp: {'by','at'}} — who has a bid open right now. Stale
    claims (> 2x fresh window) are pruned on read."""
    if clouddb.available():
        c = clouddb.get_blob("bid_claims") or {}
    else:
        p = BASE / "data" / "bid_claims.json"
        c = json.loads(p.read_text()) if p.exists() else {}
    now = datetime.now()
    live = {}
    for s, v in c.items():
        try:
            age = (now - datetime.fromisoformat(v["at"])).total_seconds() / 60
            if age <= CLAIM_FRESH_MIN * 2:
                v["mins"] = age
                live[s] = v
        except Exception:
            continue
    return live


def _save_claims(c):
    c = {s: {"by": v["by"], "at": v["at"]} for s, v in c.items()}
    if clouddb.available():
        clouddb.put_blob("bid_claims", c)
    else:
        (BASE / "data" / "bid_claims.json").write_text(json.dumps(c))


def claim_bid(stamp, user, force=False):
    """Soft lock: opening a bid marks it 'user is working on this' for
    15 min. Returns the OTHER person's fresh claim if there is one.
    force=True reassigns instantly (the Take-over button)."""
    claims = _claims()
    other = claims.get(stamp)
    if (not force and other and other["by"] != user
            and other["mins"] <= CLAIM_FRESH_MIN):
        return other                      # someone else is on it — warn
    if user:
        claims[stamp] = {"by": user,
                         "at": datetime.now().isoformat(timespec="seconds")}
        _save_claims(claims)
    return None


def release_claim(stamp):
    claims = _claims()
    claims.pop(stamp, None)
    _save_claims(claims)


STATUS_STYLE = {                          # label -> (text color, bg)
    "needs review":     ("#8a5a00", "#fdf3dd"),
    "working":          ("#1d4ed8", "#e5edff"),
    "on hold":          ("#6d28d9", "#f0e9fd"),
    "with Tom & Dallon": ("#8a5a00", "#fbe9c6"),
    "approved":         ("#1e6b34", "#e6f4ea"),
    "quote sent":       ("#1e6b34", "#e6f4ea"),
    "WON ✓":            ("#ffffff", "#1e8449"),
    "archived":         ("#6b7280", "#f0f1f3"),
}


def status_pill(label, extra=""):
    fg, bg = STATUS_STYLE.get(label, ("#6b7280", "#f0f1f3"))
    return (f"<span style='display:inline-block;background:{bg};color:{fg};"
            f"border-radius:999px;padding:3px 12px;font-size:11.5px;"
            f"font-weight:700;white-space:nowrap'>{esc(label)}"
            + (f" · {esc(extra)}" if extra else "") + "</span>")


def bid_status(b, holds, flags_open, sb_status, claims):
    """One glanceable state per bid. Priority: hold > flagged > someone
    working > decided/office outcome > needs review."""
    stamp = b["stamp"]
    if b.get("dns_match"):
        return ("<span style='display:inline-block;background:#1c1c1c;"
                "color:#ff6b5e;border-radius:999px;padding:3px 12px;"
                "font-size:11.5px;font-weight:800'>⛔ DO NOT SERVICE</span>")
    if stamp in holds:
        return status_pill("on hold")
    if stamp in flags_open:
        return status_pill("with Tom & Dallon")
    cl = claims.get(stamp)
    if cl and cl["mins"] <= CLAIM_FRESH_MIN:
        return status_pill("working", cl["by"])
    js = (sb_status.get(stamp) or "").lower()
    if js in ("approved", "converted"):
        return status_pill("WON ✓")
    if js == "awaiting_response":
        return status_pill("quote sent")
    if js == "archived":
        return status_pill("archived")
    if b.get("reviewed") or js == "draft":
        return status_pill("approved")
    if stamp in getattr(bid_status, "_sl", {}):
        return ("<span style='display:inline-block;background:#f0e9fd;"
                "color:#6d28d9;border-radius:999px;padding:3px 12px;"
                "font-size:11.5px;font-weight:800'>🔍 second look</span>")
    return status_pill("needs review")


def second_looks():
    """Open office second-look requests: stamp -> (question, who asked).
    Cleared naturally when the bid gets a real decision."""
    out = {}
    for r in load_reviews():
        if r.get("action") == "escalated" and r.get("stamp"):
            out[r["stamp"]] = (r.get("note") or "(no question written)",
                               r.get("by") or "the office")
    return out


def scoreboard_status():
    """stamp -> Jobber quoteStatus for matched office quotes."""
    if clouddb.available():
        sb = clouddb.get_blob("scoreboard")
    else:
        p = BASE / "data" / "scoreboard.json"
        sb = json.loads(p.read_text()) if p.exists() else None
    return {r["stamp"]: r.get("office_status") for r in (sb or {}).get("rows", [])
            if r.get("office_quote")}


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
@import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;600;700;800&family=Inter:wght@400;500;600;700&display=swap');
:root{--green:#0b3d2e;--green2:#177245;--accent:#1e8449;--gold:#c9a227;
      --bg:#f8f9fa;--ink:#171b21;--mut:#6b7280;--line:#f0f1f3;
      --card:#ffffff;--soft:#fafbfb}
*{box-sizing:border-box}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',
     sans-serif;margin:0;background:var(--bg);color:var(--ink);
     font-size:14.5px;line-height:1.5}
h1,h2,h3,.total,.stat b{font-family:'Hanken Grotesk','Inter',sans-serif}
.rail{position:fixed;left:0;top:0;bottom:0;width:212px;z-index:60;
      background:linear-gradient(175deg,var(--green),#08301f);
      display:flex;flex-direction:column;padding:20px 12px}
.rail .brand{color:#fff;font-family:'Hanken Grotesk',sans-serif;
      font-weight:800;font-size:17px;padding:2px 10px 18px;
      letter-spacing:-.2px}
.rail .brandsub{color:#7ea892;font-size:9.5px;font-weight:700;
      text-transform:uppercase;letter-spacing:1.6px;margin-top:3px}
.rail nav{display:flex;flex-direction:column;gap:3px;flex:1}
.rail nav a{color:#cfe0d6;font-size:13.5px;font-weight:600;
      padding:10px 12px;border-radius:10px;display:flex;
      align-items:center;gap:10px}
.rail nav a:hover{background:rgba(255,255,255,.09);color:#fff;
      text-decoration:none}
.rail nav a.active{background:var(--gold);color:var(--green);
      font-weight:800}
.rail .ico{width:20px;text-align:center}
.rail .railfoot{color:#5f8a74;font-size:10.5px;padding:12px 10px 2px;
      line-height:1.45;border-top:1px solid rgba(255,255,255,.08)}
.main{margin-left:212px;min-height:100vh;display:flex;
      flex-direction:column}
header{background:#fff;color:var(--green);padding:0 26px;min-height:56px;
       font-size:17px;font-weight:800;letter-spacing:-.2px;
       border-bottom:1px solid var(--line);
       box-shadow:0 1px 2px rgba(16,24,40,.04);
       display:flex;align-items:center;flex-wrap:wrap;gap:8px;
       position:sticky;top:0;z-index:50;
       font-family:'Hanken Grotesk',sans-serif}
header #who{color:var(--mut)}
header #who b{color:var(--green)}
.wrap{max-width:1180px;margin:0;padding:24px 24px 48px;flex:1}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;
      padding:10px 16px;box-shadow:0 1px 2px rgba(16,24,40,.04);
      min-width:118px;display:flex;flex-direction:column-reverse}
.stat b{display:block;font-size:22px;color:var(--green);font-weight:800;
      font-variant-numeric:tabular-nums;line-height:1.2}
.stat span{font-size:10px;color:var(--mut);text-transform:uppercase;
      letter-spacing:.8px;font-weight:700;margin-bottom:2px}
.ring{display:inline-flex;align-items:center;justify-content:center;
      width:40px;height:40px;border-radius:50%;border:3px solid;
      font-size:11.5px;font-weight:700;font-variant-numeric:tabular-nums;
      background:#fff}
.card{background:var(--card);border:1px solid var(--line);
      border-radius:16px;padding:20px 24px;margin-bottom:16px;
      box-shadow:0 1px 3px rgba(16,24,40,.05)}
.card h2{margin:0 0 12px;font-size:19px;color:var(--green);
      font-weight:800;letter-spacing:-.3px}
.card h3{margin:0 0 10px;font-size:11px;color:var(--green);font-weight:800;
      text-transform:uppercase;letter-spacing:1.2px}
.card.dark{background:linear-gradient(150deg,var(--green),#0e4a37);
      color:#eef4f0;border:0}
.card.dark h3{color:var(--gold)}
.card.dark .big{font-size:26px;font-weight:800;color:#fff;
      font-variant-numeric:tabular-nums}
.card.dark .lbl{font-size:10px;text-transform:uppercase;
      letter-spacing:.8px;color:#a7c0b3}
.subtext{font-size:12px;color:var(--mut)}
.band{background:#fffaf0;border:1px solid #f3e3bd;border-left:4px solid
      var(--gold);border-radius:12px;padding:12px 18px;margin-bottom:14px}
.band h2{margin:0 0 6px;font-size:11px;color:#8a5a00;font-weight:800;
      text-transform:uppercase;letter-spacing:1px}
.band div{padding:3px 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:#9aa1ab;font-weight:700;padding:10px 8px;
   border-bottom:1px solid var(--line);font-size:10px;
   text-transform:uppercase;letter-spacing:1px}
td{padding:13px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:var(--soft)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td b{color:var(--green)}
.age{font-weight:700;font-variant-numeric:tabular-nums}
.age.warn{color:#c77700}.age.late{color:#c0392b}
.chip{display:inline-block;background:#f2f5f3;border-radius:999px;
      padding:3px 12px;margin:2px 3px 2px 0;font-size:12px;color:#3f5147;
      font-weight:500}
.flag{background:#fdecea;color:#a93226}
.win{background:#e6f4ea;color:#1e6b34;font-weight:600}
.ok{color:var(--accent);font-weight:600}
a{color:var(--green2);text-decoration:none}a:hover{text-decoration:underline}
pre{background:var(--soft);border:1px solid var(--line);border-radius:12px;
    padding:14px;font-size:12.5px;overflow-x:auto;white-space:pre-wrap}
.notes{background:#fffdf5;border:1px solid #efe6c8;border-radius:12px;
       padding:12px 16px}
.notes div{padding:4px 0;border-bottom:1px dashed #eee}
.notes div:last-child{border-bottom:0}
button,.btn{background:var(--green);color:#fff;border:0;border-radius:10px;
       padding:9px 18px;font-size:14px;font-weight:700;cursor:pointer;
       margin:3px 3px 3px 0;transition:transform .1s,filter .12s;
       font-family:'Inter',sans-serif}
button:hover{filter:brightness(1.12)}
button:active{transform:scale(.96)}
button.big{padding:12px 24px;font-size:15px;background:var(--gold);
       color:var(--green)}
button.gray{background:#fff;color:#4b5563;border:1px solid #e2e5e9;
       font-weight:600}
button.red{background:#b03a2e}
.reason{background:#fff;color:var(--green2);border:1.5px solid var(--green2);
        font-weight:500;padding:7px 12px}
.reason.sel{background:var(--green2);color:#fff}
input[type=text],input[type=date],select,textarea{width:100%;padding:10px 12px;
       border:1px solid #e2e5e9;border-radius:10px;font-size:14px;
       font-family:'Inter',sans-serif;background:#fff}
input[type=text]:focus,textarea:focus{outline:2px solid var(--gold);
       border-color:transparent}
input[type=date],select{width:auto}
.grid{display:grid;grid-template-columns:5fr 3fr;gap:16px;align-items:start}
details.card summary{cursor:pointer;font-weight:600;color:var(--mut);
       font-size:13px}
details.card[open] summary{margin-bottom:8px}
.headline{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.headline .total{font-size:34px;font-weight:800;color:var(--green);
       letter-spacing:-1px}
.confbadge{border-radius:10px;padding:6px 14px;font-weight:700;
       font-size:15px;color:#fff}
footer{margin:8px 0 28px;padding:0 24px;
       color:#a3aaa2;font-size:12px}
@media(max-width:860px){.grid{grid-template-columns:1fr}
       .rail{position:static;width:auto;flex-direction:row;
             align-items:center;padding:10px;overflow-x:auto}
       .rail nav{flex-direction:row;gap:2px}
       .rail nav a{padding:8px 10px;font-size:12px}
       .rail .ico{display:none}
       .rail .brand{padding:0 10px 0 4px;font-size:14px}
       .rail .brandsub,.rail .railfoot{display:none}
       .main{margin-left:0}
       header{font-size:15px;height:auto;padding:10px 16px;position:static}
       .headline .total{font-size:26px}}
</style>"""


FAVICON = ("<link rel='icon' href=\"data:image/svg+xml,<svg xmlns='http://"
           "www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' "
           "font-size='90'>🎩</text></svg>\">")


_RAIL_CACHE = {"at": 0.0, "q": 0, "m": 0}


def _rail_counts():
    """Queue items needing review + unread message threads — shown as
    badges in the rail so the office sees work from any page."""
    import time as _t
    if _t.time() - _RAIL_CACHE["at"] < 30:
        return _RAIL_CACHE["q"], _RAIL_CACHE["m"]
    q = m = 0
    try:
        holds, _ = active_holds()
        sbs = scoreboard_status()
        for b in load_bids():
            if b["reviewed"] or b["stamp"] in holds or sbs.get(b["stamp"]):
                continue
            if classify_row(b)[0] == "main":
                q += 1
        import msglog
        marks = _msg_read()
        m = sum(1 for a, n, ms in msglog.threads()
                if ms[-1]["at"] > marks.get(a, ""))
    except Exception:
        pass
    _RAIL_CACHE.update(at=_t.time(), q=q, m=m)
    return q, m


def page(title, body, refresh=None):
    auto = (f"<meta http-equiv='refresh' content='{refresh}'>"
            if refresh else "")
    qn, mn = _rail_counts()

    def badge(n):
        return (f"<span style='margin-left:auto;background:#c9a227;"
                f"color:#0b3d2e;border-radius:999px;padding:1px 8px;"
                f"font-size:11px;font-weight:800'>{n}</span>" if n else "")
    links = (("/customers", "👥", f"Customers{badge(qn + mn)}", "Customers"),
             ("/", "📥", f"Bid queue{badge(qn)}", "Bid queue"),
             ("/messages", "💬", f"Messages{badge(mn)}", "Messages"),
             ("/new", "➕", "New lead", "New lead"),
             ("/winback", "📞", "Win-back", "Win-back"),
             ("/scoreboard", "📊", "Scoreboard", "Scoreboard"),
             ("/history", "🗂", "History", "History"),
             ("/settings", "⚙️", "Settings", "Settings"),
             ("/brief", "☀️", "Morning brief", "Morning brief"))
    nav = "".join(
        f"<a href='{href}' class='{'active' if title == t else ''}'>"
        f"<span class='ico'>{ico}</span>{label}</a>"
        for href, ico, label, t in links)
    mode = ("approve pushes DRAFT quotes to Jobber" if _push_enabled()
            else "shadow mode · nothing sends without you")
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"{auto}{FAVICON}"
            f"<title>{title}</title>{STYLE}</head><body>"
            f"<aside class='rail'>"
            f"<div class='brand'>🎩 Master Butler"
            f"<div class='brandsub'>Monroe WA · Office</div></div>"
            f"<nav>{nav}</nav>"
            f"<div class='railfoot'>{mode}</div></aside>"
            f"<div class='main'>"
            f"<header><b>{title}</b>"
            + """<span id='who' style='margin-left:auto;font-size:13px'></span>
<script>
(function(){
  var m=document.cookie.match(/office_user=([^;]+)/);
  var el=document.getElementById('who');
  function set(n){document.cookie='office_user='+encodeURIComponent(n)
    +';path=/;max-age=31536000';location.reload();}
  if(m){var n=decodeURIComponent(m[1]);
    el.innerHTML='👤 <b>'+n+'</b> <a href="#" style="opacity:.6">change</a>';
    el.querySelector('a').onclick=function(e){e.preventDefault();
      document.cookie='office_user=;path=/;max-age=0';location.reload();};
  } else {
    el.innerHTML='Who\u2019s working? ';
    ['LaRee','Jessica','Dallon','Tom'].forEach(function(n){
      var a=document.createElement('a');a.href='#';a.textContent=n;
      a.style.cssText='margin:0 5px;color:#c9a227;font-weight:700';
      a.onclick=function(e){e.preventDefault();set(n);};
      el.appendChild(a);});
  }
})();
</script></header>"""
            f"<div class='wrap'>{body}</div>"
            + """<script>
document.querySelectorAll('tr[data-href]').forEach(function(t){
  t.style.cursor='pointer';
  t.addEventListener('click', function(e){
    if (e.target.closest('a,button,form,input,select,textarea,details')) return;
    location = t.dataset.href;
  });
});
</script>"""
            f"<footer>Every quote is a draft until a human sends it · the "
            f"inbox is never marked read · every price traces to a real "
            f"job.</footer></div></body></html>").encode()


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
    claims = _claims()
    flags_open = {f.get("stamp") for f in flagged_for_review()}
    sbs = scoreboard_status()
    bid_status._sl = second_looks()

    queue, aside, chatter, office_done = [], [], [], []
    for b in pending:
        # the OFFICE already quoted it -> nothing left to review here;
        # it lives in "Recently decided" with its live Jobber status.
        # EXCEPT: an open second-look question always stays visible.
        if sbs.get(b["stamp"]) and not b.get("office_alert") \
                and b["stamp"] not in bid_status._sl:
            office_done.append(b)
            continue
        lane, why = classify_row(b)
        if lane == "main":
            queue.append(b)
        elif lane == "chatter":
            chatter.append((b, why))
        else:
            aside.append((b, why))
    # No separate "needs attention" pile (Dallon, Jul 8: "all it is is a
    # short preview of what's below") — the QUEUE is the attention list.
    # Urgent facts become badges ON the row; holds float to the top; the
    # banner is reserved for SYSTEM alarms that aren't rows at all.
    attention = []
    attn_badge = {}
    for b in queue:
        if b["stamp"] in resurfaced:
            h = resurfaced[b["stamp"]]
            attn_badge[b["stamp"]] = (
                "#b03a2e", f"⏰ back from hold — {h.get('hold_reason', '')}")
        elif b.get("office_alert"):
            attn_badge[b["stamp"]] = ("#8a5a00",
                                      "⚠ " + b["office_alert"][:90])
        elif b.get("pipeline_error"):
            attn_badge[b["stamp"]] = ("#b03a2e", "🔴 pipeline error — open "
                                                 "the bid for details")
        elif b["stamp"] in bid_status._sl:
            q_, who_ = bid_status._sl[b["stamp"]]
            attn_badge[b["stamp"]] = ("#6d28d9",
                                      f"🔍 {who_} asks: “{q_[:80]}”")
        # past-SLA needs no badge: the WAITING column already burns red
    queue.sort(key=lambda b: (b["stamp"] not in resurfaced))
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
                from datetime import timezone
                last = datetime.fromisoformat(hb["at"])
                if last.tzinfo is None:      # legacy naive beat: assume UTC
                    last = last.replace(tzinfo=timezone.utc)
                mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
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
             + (f"<a href='/messages' style='text-decoration:none'>"
                f"<div class='stat' style='border-color:#c9a227'>"
                f"<b style='color:#8a5a00'>{_rail_counts()[1]}</b>"
                f"<span>unread messages</span></div></a>"
                if _rail_counts()[1] else "")
             + f"{ears}</div>")

    band = ""
    if attention:                      # SYSTEM alarms only (ears silent)
        band = "".join(
            f"<div class='band' style='background:#fdecea;border-color:"
            f"#e8b4ae;border-left-color:#b03a2e'><b style='color:#b03a2e'>"
            f"🔴 {esc(b['from'])}</b> — {esc(why)}</div>"
            for b, why in attention)

    # ONE CUSTOMER = ONE ROW on the queue too: several emails from the
    # same person collapse into their newest bid, oldest wait shown
    # (the SLA clock never lies), earlier messages linked underneath.
    grouped, order = {}, []
    for b in queue:                       # oldest first
        m = re.search(r"<([^>]+)>", b.get("from") or "")
        key = (m.group(1).lower() if m else b.get("from") or b["stamp"])
        if key not in grouped:
            grouped[key] = {"newest": b, "oldest_age": b["age_hours"],
                            "earlier": []}
            order.append(key)
        else:
            grouped[key]["earlier"].append(grouped[key]["newest"])
            grouped[key]["newest"] = b
    queue = []
    extra_links = {}
    for key in order:
        g = grouped[key]
        b = g["newest"]
        b["age_hours"] = max(b["age_hours"], g["oldest_age"])
        if g["earlier"]:
            extra_links[b["stamp"]] = g["earlier"]
        queue.append(b)

    rows = ""
    for b in queue:                       # already oldest first
        services = "".join(f"<span class='chip'>{esc(svc_label(s))}</span>"
                           for s in (b.get("services") or [])) or "—"
        flags = ""
        badge = ""
        if b["stamp"] in attn_badge:
            color, text = attn_badge[b["stamp"]]
            badge = (f"<div style='color:{color};font-size:12px;"
                     f"font-weight:700;margin-top:3px'>{esc(text)}</div>")
        if b["stamp"] in extra_links:
            links = " · ".join(
                f"<a href='/bid/{e['stamp']}'>{e['stamp'][4:8]}-{e['stamp'][9:13]}</a>"
                for e in extra_links[b["stamp"]])
            badge += (f"<div class='subtext'>+{len(extra_links[b['stamp']])} "
                      f"earlier message(s): {links}</div>")
        total = f"${b['total_guess']}" if b.get("total_guess") else "—"
        c = b.get("confidence")
        conf = ("—" if c is None else
                f"<b style='color:{'#1e8449' if c >= 75 else '#c77700' if c >= 50 else '#c0392b'}'>{c}%</b>")
        q = quotes.get(b["stamp"])
        name = esc(b["from"]).split("&lt;")[0].strip() or esc(b["from"])
        sub = (quote_chip(q, qurls) if q else
               esc(b["from"]).split("&lt;")[-1].rstrip("&gt;")[:34])
        ring = ("—" if c is None else
                f"<span class='ring' title='how sure the system is about "
                f"this price — green 75%+ is solid, orange means verify "
                f"the flagged items, red means the data was thin' "
                f"style='border-color:"
                f"{'#1e8449' if c >= 75 else '#c77700' if c >= 50 else '#b03a2e'};"
                f"color:{'#1e8449' if c >= 75 else '#c77700' if c >= 50 else '#b03a2e'}'>"
                f"{c}%</span>")
        rows += (f"<tr data-q='{esc((b.get('from') or '').lower())} "
                 f"{esc((b.get('address') or '').lower())}' "
                 f"data-href='/bid/{b['stamp']}'>"
                 f"<td>{age_html(b['age_hours'])}</td>"
                 f"<td><a href='/bid/{b['stamp']}'><b>{name}</b></a>"
                 f"<div class='subtext'>{sub}</div>{badge}</td>"
                 f"<td>{bid_status(b, live_holds, flags_open, sbs, claims)}"
                 f"</td>"
                 f"<td>{esc({'new_request': 'New request', 'phone_lead': 'Phone lead', 'scheduling': 'Scheduling', 'other': 'Other', 'jobber_event': 'Jobber'}.get(b.get('kind'), b.get('kind')))}</td><td>{services}{flags}</td>"
                 f"<td>{ring}</td><td class='num'><b>{total}</b></td></tr>")
    if not rows:
        rows = "<tr><td colspan=7>Queue is empty — all caught up. ✅</td></tr>"

    # RECENTLY DECIDED — outcomes at a glance, no clicking (Dallon Jul 8:
    # "bid in process, confirmed, needing review — visible at a glance")
    decided_rows = ""
    by_stamp = {b["stamp"]: b for b in bids}
    seen_d = set()
    for b in office_done:
        seen_d.add(b["stamp"])
        nm = esc(b.get("from", "")).split("&lt;")[0].strip()
        q = quotes.get(b["stamp"])
        decided_rows += (
            f"<tr><td><a href='/bid/{b['stamp']}'><b>{nm[:36]}</b></a>"
            + (f"<div class='subtext'>{quote_chip(q, qurls)}</div>" if q else "")
            + f"</td><td>{bid_status(b, live_holds, flags_open, sbs, claims)}"
            f"</td><td>office quoted it</td><td class='subtext'>—</td></tr>")
    for r in reversed(load_reviews()):
        s = r.get("stamp")
        if not s or s in seen_d or r.get("action") not in DECIDED_ACTIONS:
            continue
        seen_d.add(s)
        b = by_stamp.get(s)
        if not b:
            continue
        nm = esc(b.get("from", s)).split("&lt;")[0].strip()
        q = quotes.get(s)
        decided_rows += (
            f"<tr><td><a href='/bid/{s}'><b>{nm[:36]}</b></a>"
            + (f"<div class='subtext'>{quote_chip(q, qurls)}</div>" if q else "")
            + f"</td><td>{bid_status(b, live_holds, flags_open, sbs, claims)}"
            f"</td><td>{esc(r.get('action'))}"
            + (f" <span class='subtext'>by {esc(r['by'])}</span>"
               if r.get("by") else "")
            + f"</td><td class='subtext'>{esc((r.get('at') or '')[:10])}</td></tr>")
        if len(seen_d) >= 8:
            break
    decided_html = ""
    if decided_rows:
        decided_html = (
            "<div class='card'><h2 style='margin-top:0'>Recently decided"
            "</h2><table><tr><th>Customer</th><th>Status</th>"
            "<th>Decision</th><th>When</th></tr>"
            + decided_rows + "</table></div>")

    aside_html = ""
    # spam gets its own drawer — filtered, never vanished: the office
    # can rescue a mistake with one look (never-hide-a-customer rule)
    spam_pile = [(b, w) for b, w in aside
                 if "spam" in (w or "") or "solicitation" in (w or "")]
    aside = [(b, w) for b, w in aside if (b, w) not in spam_pile]
    if spam_pile:
        items = "".join(
            f"<div>🚫 <a href='/bid/{b['stamp']}'>{esc(b['from'])[:44]}</a> "
            f"<span style='color:#888'>({esc(why)})</span></div>"
            for b, why in spam_pile)
        aside_html += (
            f"<details class='card'><summary style='cursor:pointer;"
            f"color:#666'>🚫 Filtered as spam ({len(spam_pile)}) — "
            f"glance occasionally; open one if it's actually a customer"
            f"</summary>{items}</details>")
    if aside:
        items = "".join(
            f"<div>· <a href='/bid/{b['stamp']}'>{esc(b['from'])[:44]}</a> "
            f"<span style='color:#888'>({esc(why)})</span></div>"
            for b, why in aside)
        aside_html += (f"<details class='card'><summary style='cursor:pointer;"
                       f"color:#666'>Internal &amp; other mail "
                       f"({len(aside)}) — not customer work</summary>"
                       f"{items}</details>")
    if chatter:
        items = "".join(
            f"<div>💬 <a href='/bid/{b['stamp']}'>{esc(b['from'])[:44]}</a> "
            f"<span style='color:#888'>&ldquo;{esc((b.get('newest_message') or '')[:60])}"
            f"&rdquo;</span></div>"
            for b, why in chatter)
        aside_html += (f"<details class='card'><summary style='cursor:"
                       f"pointer;color:#666'>Conversations ({len(chatter)}) "
                       f"— customers replying in office threads, no action "
                       f"needed</summary>{items}</details>")

    reviews = load_reviews()[-8:][::-1]
    rev_rows = "".join(
        f"<div>✅ {esc(r.get('action'))} — {esc(r.get('customer', r.get('stamp')))}"
        f"{(' · ' + esc(r['reason'])) if r.get('reason') else ''}"
        f"{(' <span style=color:#888>· by ' + esc(r['by']) + '</span>') if r.get('by') else ''}</div>"
        for r in reviews) or "<div>No reviews yet.</div>"

    body = (stats + band +
        "<div class='grid'><div><div class='card'>"
        "<h2 style='margin-top:0'>Bid queue — oldest first</h2>"
        "<input type='text' placeholder='find a customer…' "
        "style='max-width:280px;margin-bottom:10px' oninput=\"var v=this.value"
        ".toLowerCase();document.querySelectorAll('table tr[data-q]').forEach("
        "function(t){t.style.display=t.dataset.q.indexOf(v)>=0?'':'none';});\">"
        "<table><tr><th>Waiting</th><th>From</th><th>Status</th><th>Kind</th>"
        "<th>Services</th><th>Conf.</th><th class='num'>Est.</th></tr>" + rows +
        "</table>" + aside_html + "</div>" + decided_html + "</div>"
        "<div>" + scoreboard_card() + review_card() + ideas_card() +
        held_card(live_holds, bids) +
        "<div class='card'><h3 style='margin-top:0'>Recent decisions"
        "</h3>" + rev_rows + "</div>"
        "<div class='card'><h3 style='margin-top:0'>Schedule glance</h3>"
        "<div style='color:#888'>Jobber calendar — future phase. Days fill "
        "toward the $850–1,100/tech target.</div></div></div></div>")
    body += """
<script>
(function(){
  var last = null;
  function bump(){
    fetch('/api/pulse').then(function(r){return r.json();}).then(function(d){
      if (last === null) { last = d.t; return; }
      if (d.t !== last) location.reload();
    }).catch(function(){});
  }
  setInterval(bump, 30000); bump();
})();
</script>"""
    return page("Bid queue", body)        # reloads only when data changes


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
            "<div style='margin-top:10px;font-size:13px'>"
            + "".join(
                f"<div style='display:flex;justify-content:space-between;"
                f"padding:3px 0;border-bottom:1px solid rgba(255,255,255,.08)'>"
                f"<span>{esc((r.get('customer') or '?').split('<')[0].strip())[:22]}</span>"
                f"<b style='color:"
                f"{'#7fd6a2' if abs(r.get('gap_pct') or 0) <= 10 else '#f0c987'}'>"
                f"{(r.get('gap_pct') or 0):+.0f}%</b></div>" for r in rows[:4])
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


def review_card():
    """LaRee's one-click channel: bids flagged for Tom & Dallon."""
    flagged = flagged_for_review()
    if not flagged:
        return ""
    rows = "".join(
        f"<div>🚩 <a href='/bid/{esc(f['stamp'])}'>{esc(f.get('customer'))[:34]}"
        f"</a><form method='POST' action='/review_seen' style='display:inline'>"
        f"<input type='hidden' name='stamp' value='{esc(f['stamp'])}'>"
        f"<button class='gray' style='padding:2px 8px;font-size:11px;"
        f"margin-left:6px'>seen</button></form></div>"
        for f in flagged)
    return (f"<div class='card' style='border-left:4px solid var(--gold)'>"
            f"<h3 style='margin-top:0'>🚩 For Tom &amp; Dallon "
            f"<span class='chip win'>{len(flagged)}</span></h3>{rows}</div>")


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


def bid_page(stamp, user=None):
    bids = {b["stamp"]: b for b in load_bids()}
    b = bids.get(stamp)
    if not b:
        return page("Not found", "<div class='card'>No such bid.</div>")

    # MULTI-PERSON GUARD: opening a bid claims it for 15 min; if someone
    # else already has it open, say so LOUDLY before any buttons.
    sl = second_looks().get(stamp)
    sl_banner = ""
    if sl and not b.get("reviewed"):
        sl_banner = (
            f"<div class='band' style='background:#f0e9fd;border-color:"
            f"#d8c7f7;border-left-color:#6d28d9'><b style='color:#6d28d9'>"
            f"🔍 {esc(sl[1])} asked for a second look:</b> "
            f"“{esc(sl[0][:200])}” — answer it with a decision below, or "
            f"use the 🚩 if the office can't settle it.</div>")
    other = claim_bid(stamp, user)
    mine = (_claims().get(stamp) or {}).get("by") == user if user else False
    collision = ""
    if mine and not other:
        collision += (
            f"<div class='subtext' style='margin:4px 0 8px'>You're on this "
            f"bid. Walking away? "
            f"<form method='POST' action='/claim_release' "
            f"style='display:inline'>"
            f"<input type='hidden' name='stamp' value='{stamp}'>"
            f"<button class='gray' style='padding:2px 10px;font-size:11px'>"
            f"Release it</button></form></div>")
    if b.get("dns_match"):
        h = b["dns_match"]
        collision += (
            f"<div class='band' style='background:#1c1c1c;border-color:"
            f"#000;border-left-color:#ff6b5e'><b style='color:#ff6b5e'>"
            f"⛔ DO NOT SERVICE</b> <span style='color:#ddd'>— matches "
            f"&ldquo;{esc(h['name'])}&rdquo; in Jobber (by "
            f"{esc(h['matched_by'])}). Do not quote or schedule; "
            f"questions go to Dallon/Tom.</span></div>")
    if other:
        collision = (
            f"<div class='band' style='background:#e5edff;border-color:"
            f"#b9ccf5;border-left-color:#1d4ed8'><b style='color:#1d4ed8'>"
            f"👥 {esc(other['by'])} opened this bid "
            f"{other['mins']:.0f} min ago</b> — check with them before "
            f"deciding, so you don't both answer the same customer. "
            f"<form method='POST' action='/claim_take' style='display:inline'>"
            f"<input type='hidden' name='stamp' value='{stamp}'>"
            f"<button style='padding:4px 12px;font-size:12px;"
            f"background:#1d4ed8'>🤝 Take over this bid</button></form></div>")

    gallery, has_imagery = "", False
    if clouddb.available():
        slug = re.sub(r"[^a-z0-9]+", "-",
                      (b.get("address") or "").lower()).strip("-")[:60]
        colors = {"customer": "transparent", "aerial": "#0b6e4f",
                  "street": "#1a5276"}
        for ref, kind, idx in clouddb.photos_index([stamp, slug] if slug
                                                   else [stamp]):
            has_imagery = has_imagery or kind in ("aerial", "street")
            lbl = {"aerial": ("Aerial", "#1e8449"),
                   "street": ("Street", "#1d4ed8"),
                   "customer": ("Customer", "#8a5a00")}.get(
                       kind, (kind.title(), "#6b7280"))
            gallery += (
                f"<a href='/img/{ref}/{kind}/{idx}' target='_blank' "
                f"style='position:relative;display:inline-block;margin:4px'>"
                f"<img src='/img/{ref}/{kind}/{idx}' "
                f"style='width:190px;height:120px;object-fit:cover;"
                f"border-radius:10px;border:2px solid {lbl[1]}55'>"
                f"<span style='position:absolute;top:7px;left:7px;"
                f"background:{lbl[1]};color:#fff;font-size:9.5px;"
                f"font-weight:800;padding:2px 8px;border-radius:6px'>"
                f"{lbl[0]}</span></a>")
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
    cust_email = ((b.get("draft") or {}).get("customer") or {}).get("email")
    if not cust_email:
        m_ce = re.search(r"<([^>]+)>", b.get("from") or "")
        cust_email = m_ce.group(1) if m_ce else None
    convo_card = ""
    try:
        if cust_email:
            import msglog
            th = next((ms for a, n, ms in msglog.threads()
                       if a == cust_email.lower()), [])
            if len(th) > 1:
                bubbles = ""
                for m_ in th[-3:]:
                    inn = m_["dir"] == "in"
                    bubbles += (
                        f"<div style='display:flex;justify-content:"
                        f"{'flex-start' if inn else 'flex-end'};margin:5px 0'>"
                        f"<div style='max-width:82%;padding:7px 12px;"
                        f"border-radius:12px;font-size:12.5px;"
                        f"{'background:#f2f5f3' if inn else 'background:#0b3d2e;color:#eef4f0'}'>"
                        f"{esc(msglog.clean_body(m_.get('body') or '')[:160] or m_.get('subject') or '')}"
                        f"</div></div>")
                convo_card = (
                    f"<div class='card'><h3 style='margin-top:0'>Recent "
                    f"conversation <a style='font-weight:400;font-size:12px' "
                    f"href='/messages?t={urllib.parse.quote(cust_email)}'>"
                    f"open full thread →</a></h3>{bubbles}</div>")
    except Exception:
        convo_card = ""

    gallery_card = (f"<div class='card'><h3 style='margin-top:0'>Photos it "
                    f"used {'(green = aerial, blue = street)' if has_imagery else ''}</h3>"
                    f"{gallery or '<div style=color:#888>No photos on this '
                    'request — the photo-request button drafts the ask.</div>'}"
                    "</div>")

    notes = re.findall(r"⚠ ?(.+)", b.get("pipeline_output", ""))
    if b.get("office_alert"):
        notes.insert(0, b["office_alert"])
    notes_html = "".join(
        f"<div style='display:flex;gap:8px;align-items:flex-start;"
        f"color:#7a5300;padding:4px 0'><span>⚠</span>"
        f"<span>{esc(n)}</span></div>" for n in notes) or \
        "<div class='subtext'>(no flags)</div>"
    # Must Know rides at the TOP of the one stack (Martha's no-hunting rule)
    mk = get_must_know(b.get("address"))
    if mk:
        notes_html = (
            f"<div style='background:#f7dfa0;border-left:4px solid #c9861a;"
            f"border-radius:8px;padding:9px 12px;margin-bottom:8px;"
            f"font-weight:700;color:#6b4a00'>📌 MUST KNOW (this property): "
            f"{esc(mk)}</div>") + notes_html

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

    # TWO-PRICE panel (Tom's wet/dry): parse the DRY-DAY OPTION note
    all_notes_text = " ".join((b.get("draft") or {}).get("bid", {})
                              .get("notes", []) or []) \
        + " " + (b.get("pipeline_output") or "")
    two_price = ""
    m2 = re.search(r"DRY-DAY OPTION: roof lane \$(\d+)[^$]*\$(\d+)",
                   all_notes_text)
    if m2:
        dry, std = m2.group(1), m2.group(2)
        two_price = f"""<div class='card' style='border-left:4px solid
  var(--green2)'><h3>Two prices — customer's choice</h3>
  <div style='display:flex;gap:26px'>
   <div><div class='lbl' style='color:var(--mut);font-size:11px;
     text-transform:uppercase'>Their date (standard)</div>
    <div style='font-size:24px;font-weight:800'>${std}</div></div>
   <div><div class='lbl' style='color:var(--mut);font-size:11px;
     text-transform:uppercase'>Our dry day (flexible)</div>
    <div style='font-size:24px;font-weight:800;color:var(--green2)'>
     ${dry}</div></div></div>
  <div class='subtext' style='margin-top:6px'>Standard is the true price
   for records. The dry-day price trades savings for scheduling
   flexibility — if they take it, hold it weather-pending.</div></div>"""

    # COMBINE: does this customer already have an OPEN quote in Jobber?
    combine_card = ""
    # (cust_email now defined earlier, before the cards)
    bid_lines = ((b.get("draft") or {}).get("bid") or {}).get("services")
    if cust_email and bid_lines and not b.get("reviewed"):
        try:
            from jobber_client import find_open_quote
            oq = find_open_quote(cust_email)
        except Exception:
            oq = None
        if oq and oq.get("quoteNumber") != my_quote:
            act = (f"""<form method='POST' action='/combine'
    style='display:inline'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='quote_id' value='{esc(oq['id'])}'>
    <input type='hidden' name='quote_number' value='{esc(oq['quoteNumber'])}'>
    <input type='hidden' name='customer' value='{esc(b['from'])}'>
    <button>Combine into #{esc(oq['quoteNumber'])}</button></form>"""
                   if _push_enabled() else
                   "<span class='subtext'>(combining activates when "
                   "Jobber-push is switched on)</span>")
            combine_card = (
                f"<div class='card' style='border-left:4px solid #c77700'>"
                f"<h3>Customer has an OPEN quote</h3>"
                f"<div>Quote <a href='{esc(oq.get('jobberWebUri') or '#')}' "
                f"target='_blank'>#{esc(oq['quoteNumber'])} ↗</a> "
                f"(${oq['amounts']['total']}, {esc(oq['quoteStatus'])}) — "
                f"these {len(bid_lines)} new line(s) can be ADDED to it "
                f"instead of making a second quote. {act}</div></div>")

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
            + f"<span class='chip'>{esc(b.get('kind'))}</span>"
            + (("<span class='chip' style='background:#eaf5ec;color:#1e8449;"
                "font-weight:700'>🆕 FIRST JOB — add the &ldquo;new customer"
                "&rdquo; note in Jobber</span>")
               if b.get("customer_status") == "new" else
               (f"<span class='chip'>{esc(b['customer_status'])}</span>"
                if b.get("customer_status") else ""))
            + "</div>")
    price_card = ""
    if bid_d.get("services"):
        # LaRee: each proposed line shows what THIS property actually
        # paid for THAT service before — review the bid service by
        # service, and see at a glance if the price should move up.
        hist = _history_entry(
            b.get("address"),
            (b.get("draft") or {}).get("customer", {}).get("name")
            or b["from"].split("<")[0].strip()) or {}
        try:
            from store import _service_key
        except Exception:
            def _service_key(n):
                return None
        lines = ""
        for s in bid_d["services"]:
            past = hist.get(_service_key(s["name"]) or "") or []
            recent = sorted(past, reverse=True)[:3]
            cells = " · ".join(f"{dt[:7]} ${p:,.0f}" for dt, p in recent)
            hint = ""
            if recent and s["price"] < recent[0][1]:
                hint = (" <b style='color:#b03a2e'>⬆ below last paid "
                        f"(${recent[0][1]:,.0f})</b>")
            lines += (
                f"<tr><td>{esc(s['name'])}</td>"
                f"<td class='num'>${s['price']:,.0f}</td>"
                f"<td class='subtext'>{cells or '—'}{hint}</td></tr>")
        price_card = (
            "<div class='card'><h3>Proposed line items</h3><table>"
            "<tr><th>Service</th><th class='num'>Price</th>"
            "<th>Past at this property</th></tr>" + lines +
            f"<tr style='background:#f3f4f1'><td><b>Total estimate</b></td>"
            f"<td class='num'><b>${d.get('total', 0):,.0f}</b></td>"
            "<td></td></tr>"
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
{(f" · <a href='/messages?t={urllib.parse.quote(cust_email)}'>💬 view conversation</a>") if cust_email else ""}
{collision}
{sl_banner}
<div class='grid'><div>
 <div class='card' style='display:flex;justify-content:space-between;
      gap:18px;flex-wrap:wrap;align-items:flex-start'>
  <div>
   <div style='font-size:10px;font-weight:800;letter-spacing:1.4px;
        text-transform:uppercase;color:var(--green2)'>Active bid review
        · {age_html(b['age_hours'])}</div>
   <h2 style='margin:4px 0 2px;font-size:26px;letter-spacing:-.5px'>
    {esc((b.get('from') or '').split('<')[0].strip())}</h2>
   <div style='color:var(--mut)'>
    {f"<a href='/property/{_slug(b.get('address'))}'>{esc(b.get('address'))}</a>"
     if b.get('address') else '— address not found'}
    <span class='subtext'> · {esc(cust_email or '')}</span></div>
   <div class='subtext' style='margin-top:4px'>
    {esc(b.get('subject'))} · {esc(b.get('folder', 'INBOX'))}
    {quote_chip(my_quote, quote_urls(),
                label=f"Open quote #{esc(my_quote)} in Jobber")
     if my_quote else ''}</div>
  </div>
  <div style='text-align:right'>{draft_headline}</div>
 </div>
 {two_price}
 {combine_card}
 {price_card}
 {measure_card}
 {f"""<div class='card' style='border-left:4px solid var(--gold);
   background:#fbfaf5'><h3>What the customer said</h3>
  <div style='font-style:italic;color:#3a4046;font-size:15px'>&ldquo;{esc(b.get('newest_message'))}&rdquo;</div>
  </div>""" if b.get('newest_message') else ''}
 {convo_card}
 {gallery_card}
 {pricing_explainer_card(pi)}
 {service_history_card(b.get('address'),
                       (b.get('draft') or {}).get('customer', {}).get('name')
                       or esc(b['from']).split('&lt;')[0].strip())}
 {history_card}
 <div class='card' style='background:#fffbeb;border-color:#f3e3bd'>
  <h3 style='margin-top:0;color:#8a5a00'>⚠ All notes — one stack</h3>
  <div>{notes_html}</div>
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
</div><div style='position:sticky;top:70px;max-height:calc(100vh - 84px);
     overflow-y:auto;border-radius:16px'>
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
   until <input type='date' name='hold_until' id='holddate'
                style='padding:6px;border-radius:6px'>
   <button class='gray'>Hold (auto-resurfaces)</button>
   <div style='margin-top:4px'>
    <button type='button' class='gray' style='padding:3px 10px;font-size:11.5px'
     onclick="qh(7)">+1 week</button>
    <button type='button' class='gray' style='padding:3px 10px;font-size:11.5px'
     onclick="qh(14)">+2 weeks</button>
    <button type='button' class='gray' style='padding:3px 10px;font-size:11.5px'
     onclick="qh('aug')">Dry season (Aug 1)</button>
   </div>
   <script>
   function qh(d){{
     var t = new Date();
     if (d === 'aug') {{
       t = new Date(t.getFullYear() + (t.getMonth() >= 7 ? 1 : 0), 7, 1);
     }} else {{ t.setDate(t.getDate() + d); }}
     document.getElementById('holddate').value = t.toISOString().slice(0,10);
   }}
   </script>
   <div style='font-size:12px;color:#888'>Hold parks the WORK, never the
   reply — customer still gets answered with the timeline.</div>
  </form>
  <form method='POST' action='/flag_review' style='margin-top:6px'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <input type='hidden' name='total' value='{d.get('total') or ''}'>
   <button style='background:var(--gold);color:#1c2b23'>🚩 Stuck? Send to
   Dallon &amp; Tom (final step)</button>
  </form>
  <details style='margin-top:10px;border-top:1px solid var(--line);
    padding-top:8px'>
   <summary style='cursor:pointer;color:var(--mut);font-size:12.5px;
    font-weight:700'>More actions (escalate · photos · spam)</summary>
  <form method='POST' action='/escalate' style='margin-top:8px'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <input type='hidden' name='address' value='{esc(b.get('address'))}'>
   <input type='text' name='question' placeholder='your question, one line'>
   <button style='background:#6d28d9'>🔍 Ask the office for a second look</button>
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
  <form method='POST' action='/mark_spam' style='margin-top:6px'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='sender' value='{esc(b['from'])}'>
   <button class='gray'>🚫 Spam — never show this sender again</button>
  </form>
  </details>
 </div>
 <details class='card'><summary style='cursor:pointer;font-weight:700;
  color:var(--mut);font-size:12px'>Similar homes (honor history) — open if
  you want comps</summary>{hist_html}</details>
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
  <div style='margin:10px 0'><b>Services requested</b>
   <div style='display:grid;grid-template-columns:1fr 1fr;gap:2px 12px;
        margin-top:6px'>{checks}</div></div>
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


def _winback_done():
    if clouddb.available():
        return clouddb.get_blob("winback_done") or {}
    p = BASE / "data" / "winback_done.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _winback_save(d):
    if clouddb.available():
        clouddb.put_blob("winback_done", d)
    else:
        (BASE / "data" / "winback_done.json").write_text(json.dumps(d))


def _msg_read():
    """{addr: iso of last message the office has SEEN}. Opening a
    thread marks it read; 'Mark unread' hands it to the next person."""
    if clouddb.available():
        return clouddb.get_blob("msg_read") or {}
    p = BASE / "data" / "msg_read.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _msg_read_save(d):
    if clouddb.available():
        clouddb.put_blob("msg_read", d)
    else:
        (BASE / "data" / "msg_read.json").write_text(json.dumps(d))


def messages_page(sel=None, draft=""):
    """LIVE conversation center: every customer message in and out,
    cleaned up, newest thread first — reply without opening Gmail."""
    import msglog
    ts = msglog.threads()
    if not ts:
        return page("Messages", "<div class='card'>No conversations "
                    "logged yet — they build up as mail flows.</div>")
    read_marks = _msg_read()
    unread, older = [], []
    for addr, name, msgs in ts:
        if msgs[-1]["at"] > read_marks.get(addr, ""):
            unread.append((addr, name, msgs))
        else:
            older.append((addr, name, msgs))
    # NO auto-select (Dallon: landing on the page must not mark anything
    # read) — a thread opens, and gets marked read, only on a real click.
    cur = None
    if sel is not None:
        cur = next((t for t in ts if t[0] == sel), None)
        if cur:
            read_marks[sel] = cur[2][-1]["at"]
            _msg_read_save(read_marks)

    def render_items(group, is_unread):
        items = ""
        for addr, name, msgs in group[:40]:
            last = msgs[-1]
            active = addr == sel
            arrow = "←" if last["dir"] == "in" else "→"
            if active:
                box = "background:#0b3d2e;color:#fff"
                nm_c, tx_c = "color:#fff", "color:#bcd3c7"
            elif is_unread:
                # UNREAD = LOUD: gold bar, tinted card, big dot, bold ink
                box = ("background:#fdf4dd;border-left:5px solid #c9a227;"
                       "box-shadow:0 1px 3px rgba(160,110,10,.15)")
                nm_c, tx_c = "color:#0b3d2e", "color:#4b5563;font-weight:600"
            else:
                # read = quiet and grey
                box = "opacity:.55"
                nm_c = "color:#5b6570;font-weight:500"
                tx_c = "color:#9aa1ab"
            dot = ("<span style='color:#c9861a;font-size:15px'>●</span> "
                   if is_unread and not active else "")
            unread_tag = ("<span style='float:right;background:#c9a227;"
                          "color:#0b3d2e;font-size:9px;font-weight:800;"
                          "border-radius:999px;padding:1px 7px'>NEW</span>"
                          if is_unread and not active else "")
            items += (
            f"<a href='/messages?t={urllib.parse.quote(addr)}' "
            f"style='display:block;padding:11px 14px;border-radius:12px;"
            f"margin-bottom:5px;text-decoration:none;{box}'>"
            f"{unread_tag}"
            f"<b style='font-size:13.5px;{nm_c}'>{dot}{esc(name)[:24]}</b>"
            f"<div style='font-size:12px;{tx_c};"
            f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>"
            f"{arrow} {esc((last.get('body') or last.get('subject') or '')[:44])}"
            f"</div></a>")
        return items

    items = render_items(unread, True) or "<div class='subtext' style='padding:8px 14px'>All caught up ✅</div>"
    older_html = ""
    if older:
        older_html = (f"<details style='margin-top:10px'><summary "
                      f"style='cursor:pointer;color:var(--mut);font-size:12px;"
                      f"font-weight:700;padding:0 14px'>Older conversations "
                      f"({len(older)})</summary>{render_items(older, False)}</details>")
    if sel is None:
        placeholder = (
            "<div class='card' style='display:flex;align-items:center;"
            "justify-content:center;min-height:320px;color:var(--mut)'>"
            "<div style='text-align:center'><div style='font-size:34px'>💬"
            "</div><b>Pick a conversation</b><div class='subtext'>"
            "Nothing gets marked read until you open it.</div></div></div>")
        body = f"""
<div style='display:grid;grid-template-columns:290px 1fr;gap:16px;
            align-items:start'>
 <div class='card' style='padding:12px'>
  <h3 style='padding:0 6px;display:flex;align-items:center'>Needs attention
   <form method='POST' action='/msg_read_all' style='margin-left:auto'>
    <button class='gray' style='padding:2px 8px;font-size:10.5px'>mark all
    read</button></form></h3>{{items}}{{older_html}}</div>
 {{placeholder}}</div>""".format(items=render_items(unread, True) or
        "<div class='subtext' style='padding:8px 14px'>All caught up ✅</div>",
        older_html=("" if not older else
        f"<details style='margin-top:10px'><summary style='cursor:pointer;"
        f"color:var(--mut);font-size:12px;font-weight:700;padding:0 14px'>"
        f"Older conversations ({{}})</summary>{{}}</details>".format(
            len(older), render_items(older, False))),
        placeholder=placeholder)
        return page("Messages", body)

    thread_html = ""
    tname, tmsgs = sel, []
    for addr, name, msgs in ts:
        if addr == sel:
            tname, tmsgs = name, msgs
            break
    for m in tmsgs[-30:]:
        inbound = m["dir"] == "in"
        thread_html += (
            f"<div style='display:flex;"
            f"justify-content:{'flex-start' if inbound else 'flex-end'};"
            f"margin-bottom:10px'>"
            f"<div style='max-width:78%;padding:10px 14px;border-radius:14px;"
            f"{'background:#f2f5f3;color:#20242a' if inbound else 'background:#0b3d2e;color:#eef4f0'}'>"
            f"<div style='font-size:10px;font-weight:700;opacity:.65;"
            f"margin-bottom:3px'>"
            f"{esc(m.get('name') or '') if inbound else 'Master Butler' + (' · ' + esc(m['by']) if m.get('by') else '')}"
            f" · {esc(m['at'][:16].replace('T', ' '))}</div>"
            f"<div style='white-space:pre-wrap;font-size:13.5px'>"
            f"{esc(msglog.clean_body(m.get('body') or '') or m.get('subject') or '')}</div>"
            + (f"<div style='margin-top:4px'><a href='/bid/{m['stamp']}' "
               f"style='font-size:11px;color:{'#177245' if inbound else '#c9a227'}'>"
               f"open the bid →</a></div>" if m.get("stamp") else "")
            + "</div></div>")
    if clouddb.available():
        _canned = clouddb.get_blob("canned_replies") or {}
    else:
        cp = BASE / "data" / "canned_replies.json"
        _canned = json.loads(cp.read_text()) if cp.exists() else {}
    canned_json = json.dumps(_canned).replace("</", "<\\/")
    last_subject = next((m.get("subject") for m in reversed(tmsgs)
                         if m.get("subject")), "")
    reply_subject = (last_subject if last_subject.lower().startswith("re:")
                     else f"Re: {last_subject}" if last_subject
                     else "Master Butler")
    body = f"""
<div style='display:grid;grid-template-columns:290px 1fr;gap:16px;
            align-items:start'>
 <div class='card' style='padding:12px'>
  <h3 style='padding:0 6px;display:flex;align-items:center'>Needs attention
   <form method='POST' action='/msg_read_all' style='margin-left:auto'>
    <button class='gray' style='padding:2px 8px;font-size:10.5px'>mark all
    read</button></form></h3>{items}{older_html}</div>
 <div class='card'>
  <h2 style='margin-top:0;display:flex;align-items:center;gap:10px'>
   {esc(tname)}
   <span class='subtext' style='font-weight:400'>{esc(sel)}</span>
   <form method='POST' action='/msg_unread' style='margin-left:auto'>
    <input type='hidden' name='addr' value='{esc(sel)}'>
    <button class='gray' style='padding:5px 12px;font-size:12px'
     title='Hand this conversation to the next person'>
     ↩ Mark unread</button>
   </form></h2>
  <div style='max-height:520px;overflow-y:auto;padding:6px 2px'>
   {thread_html or "<div class='subtext'>No messages yet.</div>"}
  </div>
  <div style='margin-top:12px;border-top:1px solid var(--line);
       padding-top:12px'>
   <div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px'>
    <form method='POST' action='/msg_draft' style='display:inline'>
     <input type='hidden' name='to' value='{esc(sel)}'>
     <button class='gray' style='border-color:var(--gold);
             color:#8a5a00'>✨ Draft a reply for me</button>
    </form>
    <select id='canned' style='max-width:340px'>
     <option value=''>Quick responses…</option>
    </select>
   </div>
  </div>
  <form method='POST' action='/msg_send'>
   <input type='hidden' name='to' value='{esc(sel)}'>
   <input type='hidden' name='subject' value='{esc(reply_subject)}'>
   <textarea id='replybox' name='body' rows='5' style='min-height:110px'
    placeholder='Reply as customercare@ —
sends real email to {esc(tname)} when you hit Send'>{esc(draft)}</textarea>
   <div style='display:flex;justify-content:space-between;
               align-items:center;margin-top:8px'>
    <span class='subtext'>Sends from customercare@masterbutlerinc.com,
    signed with your name tag.</span>
    <button class='big' type='button' onclick="alert('Sending is switched OFF while we test — copy the text into Gmail for now. Dallon flips this on when ready.')">Send reply</button>
   </div>
  </form></div></div>
<script>
var CANNED = {canned_json};
var sel = document.getElementById('canned');
Object.keys(CANNED).forEach(function(k){{
  var o = document.createElement('option'); o.value = k; o.textContent = k;
  sel.appendChild(o);
}});
function grow(box){{
  box.style.height = 'auto';
  box.style.height = Math.min(box.scrollHeight + 6, 560) + 'px';
}}
var _rb = document.getElementById('replybox');
_rb.addEventListener('input', function(){{ grow(_rb); }});
if (_rb.value) grow(_rb);
// refresh for new messages every 60s — but NEVER while someone
// is mid-reply (a reload would eat their typing)
setInterval(function(){{
  if (!document.getElementById('replybox').value.trim()) location.reload();
}}, 60000);
sel.onchange = function(){{
  if (!sel.value) return;
  _rb.value = CANNED[sel.value];
  grow(_rb);
  _rb.focus();
}};
</script>"""
    return page("Messages", body)


def _bid_email(b):
    e = ((b.get("draft") or {}).get("customer") or {}).get("email")
    if not e:
        m = re.search(r"<([^>]+)>", b.get("from") or "")
        e = m.group(1) if m else None
    return (e or "").strip().lower() or None


def _stamp_utc(stamp):
    """Bid stamp (local time) -> UTC iso, comparable with msglog times."""
    from datetime import timezone
    try:
        return (datetime.strptime(stamp, "%Y%m%d-%H%M%S").astimezone()
                .astimezone(timezone.utc).isoformat(timespec="seconds"))
    except ValueError:
        return ""


def customers_page(sel=None, draft=""):
    """FEATURE D (Dallon, Jul 8): Messages + Queue merged into ONE
    customer view — pick a person once and see the conversation, the
    money, and the decision without tab-hopping. The old tabs stay."""
    import msglog
    bids = load_bids()
    live_holds, _resurfaced = active_holds()
    quotes = quote_numbers()
    qurls = quote_urls()
    claims = _claims()
    flags_open = {f.get("stamp") for f in flagged_for_review()}
    sbs = scoreboard_status()
    bid_status._sl = second_looks()
    read_marks = _msg_read()

    # ── one entry per customer: bids + thread merged by email ──
    cust, order = {}, []

    def entry(key):
        if key not in cust:
            cust[key] = {"name": "", "email": None, "bids": [], "msgs": []}
            order.append(key)
        return cust[key]

    for b in bids:                                  # oldest first
        if b.get("merged_into"):
            continue
        if classify_row(b)[0] == "aside":           # spam/internal/robots
            continue
        e = _bid_email(b)
        # an UNMATCHED Jobber event (didn't fold into a real customer's
        # bid) is its own entry — the subject carries the customer's name;
        # keying by noreply@ would lump every event into one fake person
        if b.get("kind") == "jobber_event" and (not e or "getjobber" in e):
            key = "stamp:" + b["stamp"]
            c = entry(key)
            c["name"] = (b.get("subject") or "Jobber event")[:40]
            c["bids"].append(b)
            continue
        key = e or ("stamp:" + b["stamp"])
        c = entry(key)
        c["email"] = c["email"] or e
        nm = (b.get("from") or "").split("<")[0].strip()
        if nm and nm.lower() not in ("none", "none none") \
                and not c["name"]:
            c["name"] = nm
        c["bids"].append(b)
    import spam_filter
    _not_customers = (list(_internal_senders()) + list(_learned_spam())
                      + list(NOISE_SENDERS)
                      + list(spam_filter.KNOWN_SPAM_DOMAINS))
    for addr, name, msgs in msglog.threads():
        # a thread with no bid gets the same spam/internal filter bids do
        if addr not in cust and any(s and s in addr
                                    for s in _not_customers):
            continue
        if addr not in cust and spam_filter.looks_spam(
                addr, msgs[-1].get("subject"), msgs[-1].get("body"))[0]:
            continue
        c = entry(addr)
        c["email"] = c["email"] or addr
        if name and name != addr and not c["name"]:
            c["name"] = name
        c["msgs"] = msgs

    # ── sort: needs-a-person first, WONs and quiet threads sink ──
    roster = []
    for key in order:
        c = cust[key]
        c["bids"].sort(key=lambda b: b["stamp"])
        nb = c["bids"][-1] if c["bids"] else None
        pill = (bid_status(nb, live_holds, flags_open, sbs, claims)
                if nb else "")
        unread = bool(c["msgs"]) and \
            c["msgs"][-1]["at"] > read_marks.get(key, "")
        last_at = max(c["msgs"][-1]["at"] if c["msgs"] else "",
                      _stamp_utc(nb["stamp"]) if nb else "")
        needs = (nb and not nb["reviewed"] and not sbs.get(nb["stamp"])
                 and nb["stamp"] not in live_holds)
        if unread or (nb and (nb.get("dns_match") or nb.get("office_alert")
                              or nb["stamp"] in bid_status._sl)) or needs:
            grp = 0                                # action needed
        elif nb and (nb["stamp"] in live_holds or nb["stamp"] in flags_open
                     or claims.get(nb["stamp"])):
            grp = 1                                # in someone's hands
        elif nb and (sbs.get(nb["stamp"]) or "").lower() == "awaiting_response":
            grp = 2                                # quote out, waiting
        else:
            grp = 3                                # WON / archived / quiet
        roster.append({"key": key, "c": c, "nb": nb, "pill": pill,
                       "unread": unread, "grp": grp, "at": last_at})
    roster.sort(key=lambda r: r["at"], reverse=True)
    roster.sort(key=lambda r: r["grp"])

    # a real click marks the thread read — never the page load
    curc = next((r for r in roster if r["key"] == sel), None)
    if curc and curc["c"]["msgs"]:
        read_marks[sel] = curc["c"]["msgs"][-1]["at"]
        _msg_read_save(read_marks)
        curc["unread"] = False

    # ── pane 1: the roster ──
    items = ""
    shown_grp = None
    grp_names = {0: "Needs something", 1: "In someone's hands",
                 2: "Quote out — waiting", 3: "Quiet · won · done"}
    for r in roster[:60]:
        c, nb = r["c"], r["nb"]
        if r["grp"] != shown_grp:
            shown_grp = r["grp"]
            items += (f"<div style='font-size:10px;font-weight:800;"
                      f"text-transform:uppercase;letter-spacing:1.1px;"
                      f"color:var(--mut);padding:10px 8px 3px'>"
                      f"{grp_names[shown_grp]}</div>")
        active = r["key"] == sel
        if active:
            box = "background:#0b3d2e;color:#fff"
            nm_c, pv_c = "color:#fff", "color:#bcd3c7"
        elif r["unread"]:
            box = ("background:#fdf4dd;border-left:5px solid #c9a227;"
                   "box-shadow:0 1px 3px rgba(160,110,10,.15)")
            nm_c, pv_c = "color:#0b3d2e", "color:#4b5563;font-weight:600"
        elif r["grp"] == 3:
            box, nm_c, pv_c = "opacity:.55", "", "color:var(--mut)"
        else:
            box, nm_c, pv_c = "", "", "color:var(--mut)"
        if c["msgs"]:
            last = c["msgs"][-1]
            arrow = "←" if last["dir"] == "in" else "→"
            pv = f"{arrow} {(last.get('body') or last.get('subject') or '')[:48]}"
        elif nb:
            pv = (nb.get("newest_message") or nb.get("subject") or "")[:48]
        else:
            pv = ""
        newtag = ("<span style='float:right;background:#c9a227;"
                  "color:#0b3d2e;font-size:9px;font-weight:800;"
                  "border-radius:999px;padding:1px 7px'>NEW</span>"
                  if r["unread"] and not active else "")
        nm = c["name"] or c["email"] or "(no name)"
        items += (
            f"<a href='/customers?c={urllib.parse.quote(r['key'])}' "
            f"style='display:block;padding:10px 12px;border-radius:11px;"
            f"margin-bottom:4px;text-decoration:none;{box}'>{newtag}"
            f"<b style='font-size:13.5px;{nm_c}'>{esc(nm)[:26]}</b>"
            f"<div style='font-size:11.5px;{pv_c};white-space:nowrap;"
            f"overflow:hidden;text-overflow:ellipsis'>{esc(pv)}</div>"
            f"<div style='margin-top:2px'>{r['pill']}</div></a>")
    if not items:
        items = "<div class='subtext' style='padding:10px'>Nothing yet.</div>"
    pane1 = (f"<div class='card' style='padding:12px;max-height:"
             f"calc(100vh - 120px);overflow-y:auto'>"
             f"<h3 style='padding:0 6px;margin:0 0 4px'>Customers</h3>"
             f"{items}</div>")

    # ── pane 2: the conversation IS the timeline ──
    if not curc:
        pane2 = ("<div class='card' style='display:flex;align-items:center;"
                 "justify-content:center;min-height:340px;color:var(--mut)'>"
                 "<div style='text-align:center'><div style='font-size:34px'>"
                 "👥</div><b>Pick a customer</b><div class='subtext'>"
                 "Conversation, bid, and decision — one screen.<br>"
                 "Nothing gets marked read until you open it.</div></div></div>")
        pane3 = ""
    else:
        c, nb = curc["c"], curc["nb"]
        tl = []                                     # (utc_at, html)
        for m_ in c["msgs"][-30:]:
            inn = m_["dir"] == "in"
            who = (esc(m_.get("name") or c["name"] or "Customer") if inn
                   else "Master Butler"
                   + (" · " + esc(m_["by"]) if m_.get("by") else ""))
            tl.append((m_["at"],
                f"<div style='display:flex;justify-content:"
                f"{'flex-start' if inn else 'flex-end'};margin:7px 0'>"
                f"<div style='max-width:78%;padding:9px 13px;"
                f"border-radius:13px;font-size:13px;"
                f"{'background:#f2f5f3;color:#20242a' if inn else 'background:#0b3d2e;color:#eef4f0'}'>"
                f"<div style='font-size:10px;font-weight:700;opacity:.65;"
                f"margin-bottom:2px'>{who} · "
                f"{esc(m_['at'][:16].replace('T', ' '))}</div>"
                f"<div style='white-space:pre-wrap'>"
                f"{esc(msglog.clean_body(m_.get('body') or '') or m_.get('subject') or '')}</div>"
                f"</div></div>"))
        def evt(at, text, stamp=None):
            link = (f" · <a href='/bid/{stamp}' style='color:inherit'>"
                    f"open the bid →</a>" if stamp else "")
            tl.append((at,
                f"<div style='display:flex;align-items:center;gap:10px;"
                f"margin:11px 0;color:var(--mut);font-size:11px;"
                f"font-weight:700'><span style='flex:1;border-top:1px "
                f"dashed var(--line)'></span><span>{text}{link}</span>"
                f"<span style='flex:1;border-top:1px dashed var(--line)'>"
                f"</span></div>"))
        for b in c["bids"][-6:]:
            d_ = b.get("draft") or {}
            day = f"{b['stamp'][4:6]}/{b['stamp'][6:8]}"
            if d_.get("total") is not None:
                evt(_stamp_utc(b["stamp"]),
                    f"{day} · 🤖 system drafted ${d_['total']:,.0f}",
                    b["stamp"])
            q = quotes.get(b["stamp"])
            if q:
                st = (sbs.get(b["stamp"]) or "").replace("_", " ")
                evt(_stamp_utc(b["stamp"]) + "~1",   # sorts just after
                    f"📋 office quote #{esc(q)}"
                    + (f" — {esc(st)}" if st else ""))
            ev = b.get("jobber_event") or {}
            if ev.get("event") == "quote_approved":
                evt(_stamp_utc(b["stamp"]) + "~2", "🎉 customer APPROVED")
        tl.sort(key=lambda x: x[0])
        thread_html = "".join(h for _, h in tl) or \
            "<div class='subtext'>No messages or bids logged yet.</div>"

        if clouddb.available():
            _canned = clouddb.get_blob("canned_replies") or {}
        else:
            cp = BASE / "data" / "canned_replies.json"
            _canned = json.loads(cp.read_text()) if cp.exists() else {}
        canned_json = json.dumps(_canned).replace("</", "<\\/")
        last_subject = next((m.get("subject") for m in reversed(c["msgs"])
                             if m.get("subject")), "")
        reply_subject = (last_subject if last_subject.lower()
                         .startswith("re:") else f"Re: {last_subject}"
                         if last_subject else "Master Butler")
        back = f"/customers?c={urllib.parse.quote(sel)}"
        reply_html = ""
        if c["email"]:
            reply_html = f"""
  <div style='margin-top:10px;border-top:1px solid var(--line);
       padding-top:10px'>
   <div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px'>
    <form method='POST' action='/msg_draft' style='display:inline'>
     <input type='hidden' name='to' value='{esc(c["email"])}'>
     <input type='hidden' name='back' value='customers'>
     <button class='gray' style='border-color:var(--gold);
             color:#8a5a00'>✨ Draft a reply for me</button>
    </form>
    <select id='canned' style='max-width:300px'>
     <option value=''>Quick responses…</option>
    </select>
   </div>
   <form method='POST' action='/msg_send'>
    <input type='hidden' name='to' value='{esc(c["email"])}'>
    <input type='hidden' name='subject' value='{esc(reply_subject)}'>
    <input type='hidden' name='back' value='{esc(back)}'>
    <textarea id='replybox' name='body' rows='4' style='min-height:90px'
     placeholder='Reply as customercare@ — or copy into Gmail while sending is off'>{esc(draft)}</textarea>
    <div style='display:flex;justify-content:space-between;
                align-items:center;margin-top:6px'>
     <span class='subtext'>Sending stays locked until Dallon flips it on.</span>
     <button class='big' type='button' onclick="alert('Sending is switched OFF while we test — copy the text into Gmail for now. Dallon flips this on when ready.')">Send reply</button>
    </div>
   </form></div>
<script>
var CANNED = {canned_json};
var _cs = document.getElementById('canned');
Object.keys(CANNED).forEach(function(k){{
  var o = document.createElement('option'); o.value = k; o.textContent = k;
  _cs.appendChild(o);
}});
function grow(box){{
  box.style.height = 'auto';
  box.style.height = Math.min(box.scrollHeight + 6, 480) + 'px';
}}
var _rb = document.getElementById('replybox');
_rb.addEventListener('input', function(){{ grow(_rb); }});
if (_rb.value) grow(_rb);
_cs.onchange = function(){{
  if (!_cs.value) return;
  _rb.value = CANNED[_cs.value]; grow(_rb); _rb.focus();
}};
</script>"""
        unread_btn = ""
        if c["msgs"]:
            unread_btn = (f"<form method='POST' action='/msg_unread' "
                          f"style='margin-left:auto'>"
                          f"<input type='hidden' name='addr' value='{esc(sel)}'>"
                          f"<input type='hidden' name='back' value='/customers'>"
                          f"<button class='gray' style='padding:4px 11px;"
                          f"font-size:11.5px' title='Hand this conversation "
                          f"to the next person'>↩ Mark unread</button></form>")
        pane2 = (
            f"<div class='card'>"
            f"<h2 style='margin-top:0;display:flex;align-items:center;"
            f"gap:10px;flex-wrap:wrap'>{esc(c['name'] or c['email'] or '')}"
            f"<span class='subtext' style='font-weight:400'>"
            f"{esc(c['email'] or '')}</span>{unread_btn}</h2>"
            f"<div style='max-height:56vh;overflow-y:auto;padding:4px 2px'>"
            f"{thread_html}</div>{reply_html}</div>")

        # ── pane 3: the bid rides shotgun ──
        pane3 = ""
        if nb:
            d = nb.get("draft") or {}
            bid_d = d.get("bid") or {}
            conf = nb.get("confidence")
            cc = ("#1e8449" if (conf or 0) >= 75 else
                  "#c77700" if (conf or 0) >= 50 else "#b03a2e")
            head = (
                f"<div style='display:flex;justify-content:space-between;"
                f"align-items:center'><div>"
                f"<div style='font-size:10px;font-weight:800;letter-spacing:"
                f"1.1px;text-transform:uppercase;color:var(--mut)'>The bid"
                f" · {nb['stamp'][4:6]}/{nb['stamp'][6:8]}</div>"
                + (f"<div style='font-size:28px;font-weight:800;"
                   f"letter-spacing:-1px;color:var(--green2)'>"
                   f"${d['total']:,.0f}</div>" if d.get("total") is not None
                   else "<div class='subtext'>no priced draft</div>")
                + "</div>"
                + (f"<span class='ring' style='border-color:{cc};color:{cc}'>"
                   f"{conf}%</span>" if conf is not None else "")
                + "</div>")
            alerts = ""
            if nb.get("dns_match"):
                alerts += ("<div style='background:#1c1c1c;color:#ff6b5e;"
                           "border-radius:9px;padding:8px 11px;font-size:12px;"
                           "font-weight:700;margin:8px 0'>⛔ DO NOT SERVICE "
                           "— do not quote or schedule.</div>")
            if nb.get("office_alert"):
                alerts += (f"<div style='background:#fdf4dd;border-left:4px "
                           f"solid #c9a227;border-radius:9px;padding:8px 11px;"
                           f"font-size:12px;color:#7a5300;font-weight:600;"
                           f"margin:8px 0'>⚠ {esc(nb['office_alert'][:180])}"
                           f"</div>")
            sl = bid_status._sl.get(nb["stamp"])
            if sl:
                alerts += (f"<div style='background:#f0e9fd;border-left:4px "
                           f"solid #6d28d9;border-radius:9px;padding:8px 11px;"
                           f"font-size:12px;color:#6d28d9;font-weight:600;"
                           f"margin:8px 0'>🔍 {esc(sl[1])} asks: "
                           f"“{esc(sl[0][:140])}”</div>")
            lines = ""
            if bid_d.get("services"):
                hist = _history_entry(
                    nb.get("address"),
                    (d.get("customer") or {}).get("name")
                    or (nb.get("from") or "").split("<")[0].strip()) or {}
                try:
                    from store import _service_key
                except Exception:
                    def _service_key(n):
                        return None
                for s in bid_d["services"]:
                    past = hist.get(_service_key(s["name"]) or "") or []
                    recent = sorted(past, reverse=True)[:2]
                    cells = " · ".join(f"{dt[:7]} ${p:,.0f}"
                                       for dt, p in recent)
                    lines += (f"<tr><td>{esc(s['name'])}"
                              + (f"<div class='subtext' style='font-size:"
                                 f"10.5px'>{cells}</div>" if cells else "")
                              + f"</td><td class='num'><b>${s['price']:,.0f}"
                              f"</b></td></tr>")
                lines = f"<table style='margin-top:8px'>{lines}</table>"
            pi = d.get("prop_info") or {}
            chips = "".join(
                f"<span class='chip'>{esc(v)}</span>" for v in (
                    f"{pi['sqft']:,} sqft · {pi.get('sqft_source') or 'listed'}"
                    if pi.get("sqft") else None,
                    f"{pi['stories']} story" if pi.get("stories") else None,
                    pi.get("pitch"), pi.get("roof_material")) if v)
            q = quotes.get(nb["stamp"])
            qchip = quote_chip(q, qurls) if q else ""
            actionable = (not nb["reviewed"] and not sbs.get(nb["stamp"])
                          and not nb.get("dns_match")
                          and nb["stamp"] not in live_holds)
            back = f"/customers?c={urllib.parse.quote(sel)}"
            decide = ""
            if actionable:
                decide = f"""
  <form method='POST' action='/review' style='margin-top:10px'>
   <input type='hidden' name='stamp' value='{nb["stamp"]}'>
   <input type='hidden' name='customer' value='{esc(nb.get("from") or "")}'>
   <input type='hidden' name='action' value='approve'>
   <input type='hidden' name='back' value='{esc(back)}'>
   <button style='width:100%'>✓ Approve as-is</button>
  </form>
  <a href='/bid/{nb["stamp"]}' style='display:block;text-align:center;
     margin-top:7px;padding:8px;border:1px solid var(--line);
     border-radius:9px;text-decoration:none;font-size:12.5px'>
   Adjusted · Hold · 🚩 — open the full bid →</a>"""
            else:
                decide = (f"<a href='/bid/{nb['stamp']}' style='display:"
                          f"block;text-align:center;margin-top:10px;"
                          f"padding:8px;border:1px solid var(--line);"
                          f"border-radius:9px;text-decoration:none;"
                          f"font-size:12.5px'>Open the full bid page "
                          f"(photos, notes, history) →</a>")
            earlier = ""
            if len(c["bids"]) > 1:
                links = " · ".join(
                    f"<a href='/bid/{e['stamp']}'>{e['stamp'][4:6]}/"
                    f"{e['stamp'][6:8]}</a>" for e in c["bids"][:-1][-4:])
                earlier = (f"<div class='subtext' style='margin-top:8px'>"
                           f"earlier bids: {links}</div>")
            pane3 = (
                f"<div class='card' style='position:sticky;top:70px'>"
                f"{head}{alerts}"
                f"<div style='margin-top:6px'>{curc['pill']} {qchip}</div>"
                f"{lines}"
                + (f"<div style='margin-top:8px'>{chips}</div>" if chips
                   else "")
                + f"{decide}{earlier}</div>")
        else:
            pane3 = ("<div class='card'><div class='subtext'>No bid on "
                     "file for this customer — conversation only.</div>"
                     "</div>")

    cols = ("270px 1fr" if not curc else "270px 1fr 330px")
    body = (f"<div style='display:grid;grid-template-columns:{cols};"
            f"gap:14px;align-items:start'>{pane1}{pane2}"
            + (f"<div>{pane3}</div>" if curc else "") + "</div>")
    body += """
<script>
(function(){
  var last = null;
  function bump(){
    fetch('/api/pulse').then(function(r){return r.json();}).then(function(d){
      if (last === null) { last = d.t; return; }
      var rb = document.getElementById('replybox');
      if (d.t !== last && (!rb || !rb.value.trim())) location.reload();
      last = d.t;
    }).catch(function(){});
  }
  setInterval(bump, 30000); bump();
})();
</script>"""
    return page("Customers", body)


def _blob_rw(key, default):
    if clouddb.available():
        return clouddb.get_blob(key) or default
    f = BASE / "data" / f"{key}.json"
    return json.loads(f.read_text()) if f.exists() else default


def _blob_save(key, val):
    if clouddb.available():
        clouddb.put_blob(key, val)
    else:
        (BASE / "data" / f"{key}.json").write_text(json.dumps(val))
        try:
            from cloudpush import push
            push(blobs={key: val})
        except Exception:
            pass


def settings_page(msg=""):
    """The office's own control room (Dallon: 'they work on this daily,
    I don't') — quick responses and pricing knobs, no code, no Dallon."""
    import bid_engine as be
    defaults = be.factory_defaults()
    ov = be._pricing_overrides()

    banner = (f"<div class='band'>{esc(msg)}</div>" if msg else "")

    # ---- pricing table ----
    def row(key, label, default):
        cur = ov.get(key, "")
        return (f"<tr><td><b>{esc(label)}</b>"
                f"<div class='subtext'>{esc(key)}</div></td>"
                f"<td class='num'>{default}</td>"
                f"<td><input type='text' name='ov_{esc(key)}' "
                f"value='{esc(cur)}' placeholder='default' "
                f"style='width:110px;text-align:right'></td></tr>")

    scalar_labels = {
        "JOB_MINIMUM": "Job minimum ($)",
        "GUTTER_CLEANING_MINIMUM": "Gutter cleaning minimum ($)",
        "WINDOWS_MINIMUM": "Windows-only minimum ($)",
        "WINDOWS_MINIMUM_BUNDLED": "Windows minimum when bundled ($)",
        "DRY_SEASON_ROOF_FLOOR": "Dry-season roof floor ($)",
        "DRY_DAY_DISCOUNT": "Dry-day discount (0.27 = 27%)",
        "DRYER_VENT_ADDON": "Dryer vent — with other work ($)",
        "DRYER_VENT_ALONE": "Dryer vent — alone ($)",
        "WET_DAY_GUTTER_MULT": "Wet-day gutter multiplier",
        "PW_HOUSE_WASH_RATE": "House wash rate ($/sqft)",
    }
    rows = "".join(row(k, scalar_labels.get(k, k), defaults[k])
                   for k in be.EDITABLE_SCALARS)
    drows = ""
    for dname in be.EDITABLE_DICTS:
        for sub, dval in defaults[dname].items():
            drows += row(f"{dname}.{sub}",
                         f"{dname.replace('_', ' ').title()} — {sub}", dval)
    pricing_card = f"""
<div class='card'><h2 style='margin-top:0'>Pricing knobs</h2>
 <div class='subtext' style='margin-bottom:10px'>Type a number to
 override the default; clear the box to go back to default. Changes
 apply to the NEXT bid priced — nothing already on the queue moves.
 Every change is logged with your name.</div>
 <form method='POST' action='/settings_save'>
 <table><tr><th>Setting</th><th class='num'>Default</th><th>Override</th></tr>
 {rows}
 <tr><td colspan=3 style='background:var(--soft)'><b>Rates &amp;
 multipliers</b> <span class='subtext'>(advanced — small changes move
 every price)</span></td></tr>
 {drows}</table>
 <button class='big' style='margin-top:10px'>Save pricing changes</button>
 </form>
 <form method='POST' action='/settings_reset' style='margin-top:8px'
  onsubmit="return confirm('Clear ALL pricing overrides and go back to '
   + 'the calibrated defaults?')">
  <button class='gray'>↩ Reset everything to defaults</button>
 </form></div>"""

    # ---- quick responses editor ----
    canned = _blob_rw("canned_replies", {})
    qr = ""
    for name, text in canned.items():
        qr += f"""
<details style='border-bottom:1px solid var(--line);padding:8px 0'>
 <summary style='cursor:pointer;font-weight:700;color:var(--green)'>
  {esc(name)}</summary>
 <form method='POST' action='/qr_save' style='margin-top:8px'>
  <input type='hidden' name='name' value='{esc(name)}'>
  <textarea name='text' rows='5'>{esc(text)}</textarea>
  <div style='margin-top:6px'>
   <button>Save</button>
   <button name='delete' value='1' class='red'
    onclick="return confirm('Delete this response?')">Delete</button>
  </div></form></details>"""
    qr_card = f"""
<div class='card'><h2 style='margin-top:0'>Quick responses</h2>
 <div class='subtext' style='margin-bottom:6px'>These are the tap-to-fill
 replies on the Messages page. Edit freely — changes are live for
 everyone immediately.</div>
 {qr}
 <details style='padding:10px 0'>
  <summary style='cursor:pointer;font-weight:700'>➕ Add a new response
  </summary>
  <form method='POST' action='/qr_save' style='margin-top:8px'>
   <input type='text' name='name' placeholder='Name (e.g. Holiday hours)'>
   <textarea name='text' rows='4' placeholder='The reply text…'
    style='margin-top:6px'></textarea>
   <button style='margin-top:6px'>Add response</button>
  </form></details></div>"""

    changes = [r for r in load_reviews()
               if r.get("action") == "settings_change"][-8:][::-1]
    hist = ""
    if changes:
        hist = ("<div class='card'><h3>Recent changes</h3>"
                + "".join(
                    f"<div style='padding:4px 0;border-bottom:1px solid "
                    f"var(--line)'><span class='subtext'>"
                    f"{esc((c.get('at') or '')[:16].replace('T', ' '))}"
                    + (f" · {esc(c['by'])}" if c.get("by") else "")
                    + f"</span><br>{esc(c.get('note') or '')[:160]}</div>"
                    for c in changes)
                + "</div>")
    return page("Settings", banner + qr_card + pricing_card + hist)


def history_page():
    """Every bid the system has ever seen, newest first — the office's
    'where did that one go?' answer. Search included."""
    bids = load_bids()[::-1]
    holds, _ = active_holds()
    flags_open = {f.get("stamp") for f in flagged_for_review()}
    sbs = scoreboard_status()
    claims = _claims()
    quotes, qurls = quote_numbers(), quote_urls()
    rows = ""
    for b in bids[:400]:
        nm = esc(b.get("from", "")).split("&lt;")[0].strip()
        q = quotes.get(b["stamp"])
        rows += (
            f"<tr data-q='{esc((b.get('from') or '').lower())} "
            f"{esc((b.get('address') or '').lower())}' "
            f"data-href='/bid/{b['stamp']}'>"
            f"<td class='subtext'>{b['stamp'][:4]}-{b['stamp'][4:6]}-"
            f"{b['stamp'][6:8]}</td>"
            f"<td><a href='/bid/{b['stamp']}'><b>{nm[:34]}</b></a>"
            + (f"<div class='subtext'>{quote_chip(q, qurls)}</div>" if q else "")
            + f"</td><td>{bid_status(b, holds, flags_open, sbs, claims)}</td>"
            f"<td>{', '.join(svc_label(s) for s in (b.get('services') or [])[:3]) or '—'}</td>"
            f"<td class='num'>{('$' + str(b['total_guess'])) if b.get('total_guess') else '—'}</td></tr>")
    body = f"""
<div class='card'><h2 style='margin-top:0'>Every bid</h2>
 <input type='text' placeholder='find a customer or address…'
  style='max-width:300px;margin-bottom:10px' oninput=\"var v=this.value
  .toLowerCase();document.querySelectorAll('tr[data-q]').forEach(
  function(t){{t.style.display=t.dataset.q.indexOf(v)>=0?'':'none';}});\">
 <table><tr><th>Date</th><th>Customer</th><th>Status</th><th>Services</th>
 <th class='num'>Est.</th></tr>{rows}</table></div>"""
    return page("History", body)


def winback_page(showall=False):
    """LaRee's call-back list: loyal clients (2+ yrs, 3+ jobs) who went
    quiet. Ranked by lifetime value; one click marks them contacted."""
    if clouddb.available():
        rep = clouddb.get_blob("churn_report") or {}
    else:
        p = BASE / "data" / "churn_report.json"
        rep = json.loads(p.read_text()) if p.exists() else {}
    rows = rep.get("loyal_then_gone") or []
    if not rows:
        return page("Win-back", "<div class='card'>No churn report yet.</div>")
    # NEVER put a do-not-service customer on the call list
    try:
        import dns_check
        dns_names = { (e.get("name") or "").lower().lstrip("*x ").strip()
                      for e in dns_check._list() }
        before = len(rows)
        rows = [r for r in rows
                if (r["name"] or "").lower().strip() not in dns_names]
        dns_removed = before - len(rows)
    except Exception:
        dns_removed = 0
    done = _winback_done()
    remaining = sum(1 for r in rows if r["name"] not in done)
    body_rows = ""
    for r in (rows if showall else rows[:200]):
        key = r["name"]
        is_done = key in done
        phone = r.get("phone") or ""
        jump = ("<span class='chip' style='background:#fdecea;color:#b03a2e'>"
                "price jumped</span>" if r.get("price_jump") else "")
        if is_done:
            d0 = done.get(key) or {}
            oc = d0.get("outcome", "contacted")
            oc_style = {"rebooked": "background:#e6f4ea;color:#1e6b34",
                        "not interested": "background:#fdecea;color:#a93226"}
            mark = (f"<span class='chip' style='{oc_style.get(oc, '')}'>"
                    f"{esc(oc)} · {esc(d0.get('at', ''))[:10]}"
                    + (f" · {esc(d0['by'])}" if d0.get("by") else "")
                    + "</span>")
        else:
            btns = "".join(
                f"<button name='outcome' value='{v}' class='gray' "
                f"style='padding:3px 9px;font-size:11.5px'>{t}</button>"
                for v, t in (("rebooked", "✓ Rebooked!"),
                             ("voicemail", "Voicemail"),
                             ("no answer", "No answer"),
                             ("not interested", "Not interested")))
            mark = (f"<form method='POST' action='/winback_done' "
                    f"style='display:inline;white-space:nowrap'>"
                    f"<input type='hidden' name='name' value='{esc(key)}'>"
                    f"{btns}</form>")
        body_rows += (
            f"<tr{' style=opacity:.45' if is_done else ''} "
            f"data-n='{esc(key.lower())}'>"
            f"<td><b style='font-size:15px'>{esc(key)}</b>"
            + (f"<br><span class='subtext'>{esc(phone)}</span>" if phone else "")
            + f"</td><td class='num'>{r['n']}</td>"
            f"<td>{esc(r['first'][:4])}–{esc(r['last'][:4])}</td>"
            f"<td>{esc(r['last'])}</td>"
            f"<td class='num'>${r.get('typical') or '—'}</td>"
            f"<td class='num'><b>${r['lifetime']:,}</b></td>"
            f"<td>{jump}</td><td>{mark}</td></tr>")
    body = f"""
<div class='card'>
 <h2 style='margin-top:0'>📞 Win-back list</h2>
 <p style='font-size:15px'>Loyal customers (2+ years, 3+ jobs) we haven't
 seen in 20+ months — worth <b>${rep.get('lost_lifetime_value', 0):,}</b>
 lifetime combined. Sorted by value: start at the top.
 <b>{remaining}</b> left to contact.
 {f"<span class='subtext'>({dns_removed} do-not-service customers removed "
  f"from this list automatically.)</span>" if dns_removed else ""}
 <b style='color:#1e6b34'>{sum(1 for v in done.values()
                              if v.get("outcome") == "rebooked")} rebooked so
 far.</b> A friendly "we miss you — want your
 usual {datetime.now():%B} cleaning?" is the whole script.</p>
 <input id='wbf' type='text' placeholder='type a name to filter…'
        style='max-width:340px' oninput="
   var v=this.value.toLowerCase();
   document.querySelectorAll('tr[data-n]').forEach(function(t){{
     t.style.display = t.dataset.n.indexOf(v)>=0 ? '' : 'none';}});">
 <table style='margin-top:10px'>
  <tr><th>Customer</th><th class='num'>Jobs</th><th>Years</th>
      <th>Last visit</th><th class='num'>Typical</th>
      <th class='num'>Lifetime</th><th></th><th></th></tr>
  {body_rows}
 </table>
 {"" if showall or len(rows) <= 200 else
  f"<a href='/winback?all=1' class='btn' style='display:inline-block;"
  f"margin-top:10px'>Show all {len(rows)} (top 200 shown)</a>"}
 </div>"""
    return page("Win-back", body)


def scoreboard_page():
    """System vs office, side by side — written for the OFFICE, not for
    engineers: names, dollars, and a plain-English verdict."""
    if clouddb.available():
        sb = clouddb.get_blob("scoreboard")
    else:
        p = BASE / "data" / "scoreboard.json"
        sb = json.loads(p.read_text()) if p.exists() else None
    if not sb:
        return page("Scoreboard", "<div class='card'>No scoreboard yet — "
                    "the night run generates it.</div>")

    def cname(r):
        return esc((r.get("customer") or "?").split("<")[0].strip())[:34]

    matched = [r for r in sb["rows"] if r.get("office_quote")]
    waiting = [r for r in sb["rows"] if not r.get("office_quote")]
    close = sum(1 for r in matched
                if r.get("gap_pct") is not None and abs(r["gap_pct"]) <= 10)

    hero = (
        "<div class='stats'>"
        f"<div class='stat'><b>{len(matched)}</b><span>compared</span></div>"
        f"<div class='stat'><b>{close}</b><span>within 10%</span></div>"
        f"<div class='stat'><b>{len(waiting)}</b>"
        f"<span>awaiting office</span></div></div>")

    auto = None
    if clouddb.available():
        auto = clouddb.get_blob("auto_reviews") or {}
    else:
        ap = BASE / "data" / "auto_reviews.json"
        auto = json.loads(ap.read_text()) if ap.exists() else {}
    rows = ""
    for r in matched:
        gap = r.get("gap_pct")
        ar = auto.get(r.get("stamp"))
        if ar:
            pill = (f"<span style='display:inline-block;background:#eaf5ec;"
                    f"color:#1e6b34;border-radius:999px;padding:3px 12px;"
                    f"font-size:11.5px;font-weight:700' "
                    f"title=\"{esc(ar['summary'])}\">📖 auto-reviewed"
                    + (f" · {gap:+.0f}%" if gap is not None else "")
                    + "</span>"
                    f"<div class='subtext' style='max-width:260px;"
                    f"margin-top:3px'>{esc(ar['summary'][:110])}</div>")
        elif gap is None:
            pill = ""
        elif abs(gap) <= 10:
            pill = status_pill("approved", f"{gap:+.0f}%")
        else:
            pill = status_pill("needs review", f"{gap:+.0f}%")
        js = (r.get("office_status") or "").lower()
        jlabel = {"approved": "WON ✓", "converted": "WON ✓",
                  "awaiting_response": "quote sent",
                  "draft": "approved", "archived": "archived"}.get(js)
        qbtn = (f"<a class='btn' style='padding:5px 12px;font-size:12px;"
                f"background:#fff;color:#177245;border:1px solid #cfe0d6' "
                f"href='{esc(r['jobber_url'])}' target='_blank' "
                f"rel='noopener'>Jobber #{r['office_quote']} ↗</a>"
                if r.get("jobber_url") else f"#{r['office_quote']}")
        svcs = "".join(f"<span class='chip'>{esc(svc_label(s))}</span>"
                       for s in (r.get("services") or [])[:4])
        sp = (f"<div class='subtext'>by {esc(r['salesperson'])}</div>"
              if r.get("salesperson") else "")
        rows += (f"<tr><td><b>{cname(r)}</b>"
                 f"<div style='margin-top:3px'>{svcs}</div></td>"
                 f"<td>{status_pill(jlabel) if jlabel else '—'}{sp}</td>"
                 f"<td class='num'>${r['system_total']:,.0f}</td>"
                 f"<td class='num'><b>${r['office_total']:,.0f}</b></td>"
                 f"<td>{pill}</td><td>{qbtn}</td></tr>")
    matched_card = (
        "<div class='card'><h2 style='margin-top:0'>Compared with the "
        "office</h2><div class='subtext' style='margin-bottom:10px'>"
        "Gap pill: green = our draft landed within 10% of what the office "
        "actually quoted. Minus means we were under.</div>"
        "<table><tr><th>Customer</th><th>Quote status</th>"
        "<th class='num'>Our draft</th><th class='num'>Office</th>"
        "<th>Gap</th><th></th></tr>" + rows + "</table></div>"
        if rows else "")

    wrows = "".join(
        f"<tr><td><b>{cname(r)}</b></td>"
        f"<td>{', '.join(svc_label(s) for s in (r.get('services') or [])[:4])}</td>"
        f"<td class='num'>${r['system_total']:,.0f}</td></tr>"
        for r in waiting)
    waiting_card = (
        "<div class='card'><h2 style='margin-top:0'>Waiting for an office "
        "quote</h2><div class='subtext' style='margin-bottom:10px'>"
        "We drafted these; the moment the office quotes them in Jobber, "
        "they move up automatically.</div>"
        "<table><tr><th>Customer</th><th>Services</th>"
        "<th class='num'>Our draft</th></tr>" + wrows + "</table></div>"
        if wrows else "")

    return page("Scoreboard", hero + matched_card + waiting_card)


_ABBR = {"se": "southeast", "sw": "southwest", "ne": "northeast",
         "nw": "northwest", "n": "north", "s": "south", "e": "east",
         "w": "west", "pl": "place", "st": "street", "ave": "avenue",
         "av": "avenue", "rd": "road", "dr": "drive", "ln": "lane",
         "ct": "court", "cir": "circle", "blvd": "boulevard",
         "hwy": "highway", "pkwy": "parkway", "ter": "terrace"}


def _canon_addr(s):
    """Jobber writes 'Southeast 7th Place'; forms write 'SE 7th Pl'.
    Same house — expand abbreviations, drop state/zip, one canon form."""
    toks = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
    out = []
    for t in toks:
        if t in ("wa", "washington", "usa") or (t.isdigit() and len(t) == 5
                                                and out):
            continue
        out.append(_ABBR.get(t, t))
    return "-".join(out)[:80]


def _history_entry(address, client_name=None):
    """Per-service {svc: [[date, price], ...]} for this property, with a
    client-name fallback. Data from the servicehistory sweep."""
    if clouddb.available():
        hist = clouddb.get_blob("service_history") or {}
    else:
        p = BASE / "data" / "service_history.json"
        hist = json.loads(p.read_text()) if p.exists() else {}
    if not hist:
        return None
    entry = None
    if address:
        want = _canon_addr(address)
        for k, v in (hist.get("by_property") or {}).items():
            if _canon_addr(k) == want:
                entry = v
                break
    if not entry and client_name:
        ckey = re.sub(r"[^a-z ]", "", client_name.lower()).strip()
        entry = (hist.get("by_client") or {}).get(ckey)
    return entry or None


def pricing_explainer_card(pi):
    """The office asked: HOW did the system get this number? Show the
    knobs that were in effect — plainly, no engine-speak."""
    if not pi or not pi.get("sqft"):
        return ""
    import bid_engine as be
    chips = []
    src_txt = pi.get("sqft_source") or "records/roof estimate"
    chips.append(("House size", f"{pi['sqft']:,} sqft", src_txt))
    s = str(pi.get("stories") or "2")
    if s in be.STORIES:
        chips.append(("Stories", f"{s}-story",
                      f"×{be.STORIES[s]} on roof & window work"))
    pitch = pi.get("pitch") or "moderate"
    if pitch in be.PITCH:
        chips.append(("Roof pitch", pitch.replace("_", " "),
                      f"×{be.PITCH[pitch]} on roof-lane prices"))
    debris = pi.get("debris") or "moderate"
    if debris in be.DEBRIS:
        chips.append(("Debris", debris, f"×{be.DEBRIS[debris]}"))
    if pi.get("roof_material"):
        chips.append(("Roof", pi["roof_material"], "material on record"))
    cells = "".join(
        f"<div style='background:var(--soft);border:1px solid var(--line);"
        f"border-radius:12px;padding:10px 14px;min-width:120px'>"
        f"<div style='font-size:10px;color:var(--mut);text-transform:"
        f"uppercase;letter-spacing:.8px;font-weight:700'>{esc(k)}</div>"
        f"<b style='font-size:15px;color:var(--green)'>{esc(v)}</b>"
        f"<div class='subtext'>{esc(why)}</div></div>"
        for k, v, why in chips)
    return (f"<div class='card'><h3>How the price was built</h3>"
            f"<div style='display:flex;gap:10px;flex-wrap:wrap'>{cells}"
            f"</div><div class='subtext' style='margin-top:8px'>"
            f"Every line = base rate × house size × these multipliers, "
            f"then service minimums. Full detail lives in the notes "
            f"below.</div></div>")


def service_history_card(address, client_name=None):
    """LaRee's #1: per-service pricing + dates at this property (client
    fallback) — no invoice digging."""
    entry = _history_entry(address, client_name)
    if not entry:
        return ""
    rows = ""
    for svc in sorted(entry):
        visits = sorted(entry[svc], reverse=True)
        last_d, last_p = visits[0]
        older = "".join(
            f"<span class='chip' style='font-variant-numeric:tabular-nums'>"
            f"{d[:7]} · <b>${pr:,.0f}</b></span>" for d, pr in visits[1:5])
        more = (f"<span class='subtext'> +{len(visits)-5} earlier</span>"
                if len(visits) > 5 else "")
        rows += (
            f"<div style='display:flex;align-items:center;gap:14px;"
            f"padding:10px 2px;border-bottom:1px solid var(--line)'>"
            f"<div style='min-width:120px'><b style='color:var(--green);"
            f"text-transform:capitalize'>{esc(svc)}</b></div>"
            f"<div style='min-width:120px'><b style='font-size:17px;"
            f"font-variant-numeric:tabular-nums'>${last_p:,.0f}</b>"
            f"<div class='subtext'>{last_d[:7]} (latest)</div></div>"
            f"<div>{older}{more}</div></div>")
    return (f"<div class='card'><h3>Service history at this property"
            f"</h3>{rows}</div>")


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
        f"<td>{', '.join(svc_label(s) for s in r.get('services') or []) or '—'}</td>"
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
{service_history_card(address)}
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
        if self.path.startswith("/winback"):
            return self._send(winback_page("all=1" in self.path))
        if self.path.startswith("/settings"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(settings_page((q.get("msg") or [""])[0]))
        if self.path == "/history":
            return self._send(history_page())
        if self.path.startswith("/customers"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(customers_page(q.get("c", [None])[0]))
        if self.path.startswith("/messages"):
            q = urllib.parse.urlparse(self.path).query
            sel = urllib.parse.parse_qs(q).get("t", [None])[0]
            return self._send(messages_page(sel))
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
            cm = re.search(r"office_user=([^;]+)",
                           self.headers.get("Cookie") or "")
            who = urllib.parse.unquote(cm.group(1)) if cm else None
            return self._send(bid_page(m.group(1), user=who))
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
        m = re.match(r"^/api/blob/(mail_outbox|pricing_overrides|"
                     r"canned_replies|msg_read|jobber_tokens)$", self.path)
        if m:                     # blobs the Mac mirrors down
            val = (clouddb.get_blob(m.group(1))
                   if clouddb.available() else None)
            return self._send(json.dumps(val).encode(),
                              ctype="application/json")
        if self.path == "/api/backup":
            # FULL cloud-memory dump (minus photo bytes — regenerable).
            # The Mac pulls this nightly: if the database ever dies, the
            # queue, decisions, and learning history restore from here.
            if not clouddb.available():
                return self._send(b"{}", ctype="application/json")
            import clouddb as cdb
            dump = {"at": datetime.now().isoformat(timespec="seconds"),
                    "shadow_records": {}, "reviews": load_reviews(),
                    "blobs": {}, "photo_index": []}
            for stamp, rec in cdb.all_shadow():
                dump["shadow_records"][stamp] = rec
            with cdb._conn() as conn:
                for (k, v) in conn.execute(
                        "select key, value from kv_blobs").fetchall():
                    if isinstance(v, str):
                        try:
                            v = json.loads(v)
                        except ValueError:
                            pass              # plain-text blob (the brief)
                    dump["blobs"][k] = v
                dump["photo_index"] = [list(r) for r in conn.execute(
                    "select ref, kind, idx from photos").fetchall()]
            return self._send(json.dumps(dump).encode(),
                              ctype="application/json")
        if self.path == "/api/pulse":
            # cheap change token: the queue page reloads ONLY when this
            # changes, so reading/scrolling is never interrupted
            try:
                stamps = [s for s, _ in _shadow_source()]
                import msglog
                token = f"{len(stamps)}:{max(stamps) if stamps else ''}:" \
                        f"{len(load_reviews())}:{len(msglog._load()[0])}"
            except Exception:
                token = "err"
            return self._send(json.dumps({"t": token}).encode(),
                              ctype="application/json")
        if self.path == "/api/records":   # slim record list for the Mac's
            slim = []                     # quote-sync (scoreboard) matching
            for stamp, r in _shadow_source():
                d = r.get("draft") or {}
                slim.append({"stamp": stamp, "from": r.get("from"),
                             "address": r.get("address"),
                             "kind": r.get("kind"),
                             "services": r.get("services"),
                             "draft": {"total": d.get("total"),
                                       "bid": {"services":
                                               (d.get("bid") or {})
                                               .get("services") or []}},
                             "pipeline_output": ""})
            return self._send(json.dumps(slim).encode(),
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

        # WHO'S WORKING: stamp every decision with the header name-tag
        # cookie, so 'who approved this?' always has an answer.
        cm = re.search(r"office_user=([^;]+)",
                       self.headers.get("Cookie") or "")
        _user = urllib.parse.unquote(cm.group(1)) if cm else None
        def save_review(d, _sr=globals()["save_review"]):
            if _user:
                d.setdefault("by", _user)
            return _sr(d)

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
                if rec.get("dns_match"):     # HARD BLOCK, even when live
                    entry["note"] = ("REFUSED: do-not-service match — "
                                     "no quote pushed")
                    d = None
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
            # light pipeline (lookup + price) is ~3s — run it now and drop
            # the office straight onto the finished bid.
            try:
                from manual import process_manual
                stamp, rec = process_manual(
                    name=get("name"), address=get("address"),
                    phone=get("phone"), email=get("email"),
                    services=form.get("svc", []), extra=get("extra"),
                    entered_by="office")
                self.send_response(303)
                self.send_header("Location", f"/bid/{stamp}")
                self.end_headers()
                return
            except Exception:
                self.send_response(303)
                self.send_header("Location", "/new?msg=error")
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
        elif self.path == "/flag_review":
            save_review({"stamp": get("stamp"), "action": "flag_review",
                         "customer": get("customer")})
            try:
                import mailer
                host = self.headers.get("Host") or ""
                link = f"https://{host}/bid/{get('stamp')}" if host else ""
                ok, why = mailer.send_review_flag(
                    {"customer": get("customer"), "total": get("total")},
                    link=link, note=f"flagged by {_user}" if _user else "")
            except Exception as e:
                ok, why = False, f"{type(e).__name__}: {e}"
            # the outcome goes in the review log — visible on the
            # dashboard and via /api/reviews, not just buried in stdout
            if ok:
                note = "emailed Tom & Dallon"
            elif "queued" in why:
                note = "email queued — the Mac relays it on its next check-in"
            else:
                note = f"EMAIL FAILED: {why}"[:200]
            save_review({"stamp": get("stamp"), "action": "flag_email",
                         "customer": get("customer"), "note": note})
        elif self.path == "/review_seen":
            save_review({"stamp": get("stamp"), "action": "review_seen",
                         "customer": get("customer")})
        elif self.path == "/msg_draft":
            # AI-drafted reply: Claude reads the thread and writes a
            # SUGGESTION into the box. A human still edits and sends.
            # (works from Messages AND the unified Customers view)
            to = get("to")
            _page = (customers_page if get("back") == "customers"
                     else messages_page)
            draft = ""
            try:
                import msglog
                thread = next((ms for a, n, ms in msglog.threads()
                               if a == to), [])
                convo = "\n".join(
                    f"{'CUSTOMER' if m['dir'] == 'in' else 'US'}: "
                    f"{msglog.clean_body(m.get('body') or '')[:400]}"
                    for m in thread[-6:])
                stamp = next((m.get("stamp") for m in reversed(thread)
                              if m.get("stamp")), None)
                ctx = ""
                if stamp:
                    rec = dict(_shadow_source()).get(stamp) or {}
                    d = rec.get("draft") or {}
                    if d.get("total"):
                        ctx = (f"Our draft quote for them totals "
                               f"${d['total']}. ")
                    if rec.get("office_alert"):
                        ctx += (f"Office context on this customer: "
                                f"{rec['office_alert'][:300]} ")
                    if rec.get("dns_match"):
                        ctx += ("WARNING: customer is marked DO NOT "
                                "SERVICE — draft a polite decline. ")
                last_in = next((m for m in reversed(thread)
                                if m["dir"] == "in"), None)
                if not last_in:
                    return self._send(_page(to, draft=(
                        "(No customer message to answer in this thread — "
                        "pick one of LaRee's templates instead.)")))
                if any(s in to for s in _internal_senders()):
                    return self._send(_page(to, draft=(
                        "(Internal thread — no customer reply needed.)")))
                system = (
                    "You draft email replies FROM the office staff of "
                    "Master Butler (home exterior cleaning: gutters, roofs, "
                    "windows, pressure washing — Monroe, WA) TO a customer. "
                    "You are ghost-writing as Master Butler; you are never "
                    "talking to the office and never mention AI, drafts, "
                    "templates, or internal systems. Voice examples of how "
                    "this office writes:\n"
                    "1) 'Thank you for reaching out to us! I\u2019ve just "
                    "sent over your quote. If you don\u2019t see it, please "
                    "check your junk folder. Please let us know of any "
                    "questions and how you\u2019d like to proceed.'\n"
                    "2) 'Thank you for approving your quote! Our next "
                    "opening in your area is [DATE]. Please let us know if "
                    "that will work for you.'\n"
                    "Rules: 2-5 sentences, plain warm English, no emojis, "
                    "never invent specific dates, prices, or promises not "
                    "present in the context. If you need a date/price the "
                    "context doesn't give, write [DATE] or [PRICE] as a "
                    "placeholder for the office to fill. Output ONLY the "
                    "reply body — no subject line, no signature, no "
                    "commentary.")
                prompt = (
                    f"{ctx}Conversation so far (oldest first):\n{convo}\n\n"
                    f"The customer's LATEST message, which you are "
                    f"replying to:\n\"{msglog.clean_body(last_in.get('body') or '')[:500]}\"\n\n"
                    "Draft Master Butler's reply.")
                import os as _os
                import urllib.request as _ur
                key = None
                envp = BASE / ".env"
                if envp.exists():
                    for ln in envp.read_text().splitlines():
                        if ln.startswith("ANTHROPIC_API_KEY="):
                            key = ln.split("=", 1)[1].strip()
                key = key or _os.environ.get("ANTHROPIC_API_KEY")
                req = _ur.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps({
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 300,
                        "system": system,
                        "messages": [{"role": "user", "content": prompt}],
                    }).encode(),
                    headers={"x-api-key": key,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"})
                r = json.load(_ur.urlopen(req, timeout=30))
                draft = r["content"][0]["text"].strip()
                # our signature is appended at send time — strip any the
                # model added by imitating the thread
                draft = re.split(
                    r"\n(?:At your service|Best regards|Sincerely|"
                    r"Warm regards|Thanks,\s*$|— *Master Butler)",
                    draft)[0].rstrip()
            except Exception as e:
                draft = f"(draft failed: {e} — just type your reply)"
            return self._send(_page(to, draft=draft))
        elif self.path == "/settings_save":
            if not _user:
                self.send_response(303)
                self.send_header("Location", "/settings?msg=" +
                                 urllib.parse.quote("Pick your name in the "
                                                    "top bar first — changes "
                                                    "must be signed."))
                self.end_headers()
                return
            import bid_engine as be
            ov_old = dict(be._pricing_overrides())
            ov_new = {}
            for k, vals in form.items():
                if not k.startswith("ov_"):
                    continue
                val = vals[0].strip()
                if val:
                    try:
                        float(val)
                        ov_new[k[3:]] = val
                    except ValueError:
                        pass
            _blob_save("pricing_overrides", ov_new)
            be._OV_CACHE["at"] = 0            # take effect immediately
            changes = []
            for k in set(ov_old) | set(ov_new):
                if ov_old.get(k) != ov_new.get(k):
                    changes.append(f"{k}: {ov_old.get(k, 'default')} → "
                                   f"{ov_new.get(k, 'default')}")
            if changes:
                save_review({"stamp": "", "action": "settings_change",
                             "customer": "PRICING",
                             "note": "; ".join(changes)[:300]})
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" + urllib.parse.quote(
                f"Saved {len(changes)} pricing change(s)." if changes
                else "No changes."))
            self.end_headers()
            return
        elif self.path == "/settings_reset":
            if _user:
                import bid_engine as be
                had = dict(be._pricing_overrides())
                _blob_save("pricing_overrides", {})
                be._OV_CACHE["at"] = 0
                if had:
                    save_review({"stamp": "", "action": "settings_change",
                                 "customer": "PRICING",
                                 "note": f"RESET to defaults (cleared "
                                         f"{len(had)} override(s))"})
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" +
                             urllib.parse.quote("All pricing back to "
                                                "calibrated defaults."))
            self.end_headers()
            return
        elif self.path == "/qr_save":
            if not _user:
                self.send_response(303)
                self.send_header("Location", "/settings?msg=" +
                                 urllib.parse.quote("Pick your name in the "
                                                    "top bar first."))
                self.end_headers()
                return
            canned = _blob_rw("canned_replies", {})
            name = get("name").strip()
            if name:
                if get("delete"):
                    canned.pop(name, None)
                    act = f"deleted quick response '{name}'"
                elif get("text").strip():
                    canned[name] = get("text").strip()
                    act = f"edited quick response '{name}'"
                else:
                    act = None
                if act:
                    _blob_save("canned_replies", canned)
                    save_review({"stamp": "", "action": "settings_change",
                                 "customer": "QUICK RESPONSES",
                                 "note": act})
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" +
                             urllib.parse.quote("Saved."))
            self.end_headers()
            return
        elif self.path == "/mark_spam":
            import re as _r
            m = _r.search(r"@([\w.-]+)", get("sender"))
            sender_key = (m.group(1).lower() if m
                          else get("sender").lower()[:40])
            spam = _blob_rw("learned_spam", [])
            if sender_key and sender_key not in spam:
                spam.append(sender_key)
                _blob_save("learned_spam", spam)
                _SPAM_CACHE["at"] = 0
            save_review({"stamp": get("stamp"), "action": "learned_spam",
                         "customer": get("sender"),
                         "note": f"sender '{sender_key}' will be filed as "
                                 "spam from now on"})
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        elif self.path == "/msg_read_all":
            import msglog
            marks = _msg_read()
            for a, n, ms in msglog.threads():
                marks[a] = ms[-1]["at"]
            _msg_read_save(marks)
            self.send_response(303)
            self.send_header("Location", "/messages")
            self.end_headers()
            return
        elif self.path == "/msg_unread":
            # HANDOFF: someone started this thread but has to leave —
            # flip it back to unread so the next person picks it up.
            d = _msg_read()
            d.pop(get("addr"), None)
            _msg_read_save(d)
            back = get("back")
            self.send_response(303)
            self.send_header("Location", back if back.startswith("/")
                             else "/messages")
            self.end_headers()
            return
        elif self.path == "/msg_send":
            # OFFICE-DRIVEN reply: a named human hits Send; nothing
            # automated ever posts here.
            import mailer
            import msglog
            to, subj = get("to"), get("subject") or "Master Butler"
            text = get("body").strip()
            if not REPLIES_ENABLED:
                save_review({"stamp": "", "action": "reply_blocked",
                             "customer": to,
                             "note": "sending disabled (REPLIES_ENABLED off)"})
                text = ""
            if text:
                ok, why = mailer.send_reply(to, subj, text, _user)
                if ok:
                    msglog.record("out", to, subject=subj, body=text,
                                  by=_user or "")
                    save_review({"stamp": "", "action": "customer_reply",
                                 "customer": to, "note": text[:120]})
                else:
                    save_review({"stamp": "", "action": "reply_FAILED",
                                 "customer": to, "note": why[:150]})
            back = get("back")
            self.send_response(303)
            self.send_header("Location",
                             back if back.startswith("/")
                             else f"/messages?t={urllib.parse.quote(to)}")
            self.end_headers()
            return
        elif self.path == "/claim_take":
            prev = (_claims().get(get("stamp")) or {}).get("by")
            if _user:
                claim_bid(get("stamp"), _user, force=True)
                save_review({"stamp": get("stamp"), "action": "handoff",
                             "customer": "",
                             "note": f"bid handed over: "
                                     f"{prev or 'unclaimed'} → {_user}"})
            self.send_response(303)
            self.send_header("Location", f"/bid/{get('stamp')}")
            self.end_headers()
            return
        elif self.path == "/claim_release":
            release_claim(get("stamp"))
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        elif self.path == "/winback_done":
            if get("name"):
                d = _winback_done()
                d[get("name")] = {"at": datetime.now().isoformat(
                    timespec="seconds"),
                    "outcome": get("outcome") or "contacted",
                    "by": _user or ""}
                _winback_save(d)
                if get("outcome") == "rebooked":
                    save_review({"stamp": "", "action": "winback_REBOOKED",
                                 "customer": get("name"),
                                 "note": "win-back call landed a booking 🎉"})
                self.send_response(303)
                self.send_header("Location", "/winback")
                self.end_headers()
                return
        elif self.path == "/combine":
            if _push_enabled():
                rec_src = dict(_shadow_source()).get(get("stamp")) or {}
                lines = ((rec_src.get("draft") or {}).get("bid") or {}) \
                    .get("services") or []
                if lines:
                    import jobber_client as jc
                    jc.DRY_RUN = False
                    res = jc.add_lines_to_quote(get("quote_id"), lines)
                    q = (res.get("quoteCreateLineItems") or {}).get("quote") or {}
                    save_review({"stamp": get("stamp"), "action": "combined",
                                 "customer": get("customer"),
                                 "jobber_quote": get("quote_number"),
                                 "note": f"added {len(lines)} line(s); new "
                                         f"total ${q.get('amounts', {}).get('total')}"})
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
                         "note": get("question") or "(no question written)"})
            # Dallon's ruling: the OFFICE reviews second-looks on the
            # dashboard; Dallon & Tom only get the 🚩 when they're stuck.
        elif self.path == "/photo_request":
            services = [s for s in get("services").split(",") if s]
            path = templates.draft_photo_request(
                get("customer"), services,
                reason=f"dashboard request for bid {get('stamp')}")
            save_review({"stamp": get("stamp"), "action": "photo_requested",
                         "customer": get("customer"),
                         "note": f"draft: {path.name}"})
        back = get("back")                # e.g. Approve on the Customers view
        self.send_response(303)
        self.send_header("Location", back if back.startswith("/") else "/")
        self.end_headers()

    def handle_one_request(self):
        # SAFETY NET: an unexpected bug shows the office a friendly
        # "hiccup" page instead of a blank screen, and logs the cause.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            import traceback
            print("UNHANDLED:\n" + traceback.format_exc()[-800:])
            try:
                self._send(
                    b"<div style='font-family:sans-serif;padding:40px'>"
                    b"<h2>\xf0\x9f\x8e\xa9 Small hiccup</h2><p>Something "
                    b"glitched loading this page. <a href='/'>Back to the "
                    b"queue</a> \xe2\x80\x94 your work is saved; nothing "
                    b"was lost.</p></div>", code=500)
            except Exception:
                pass

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
