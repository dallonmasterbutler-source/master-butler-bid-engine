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
# FLIPPED Jul 14 2026 — Dallon: "wire up the send button" after the
# Gmail-API test send was confirmed From customercare@. Send is a named
# human clicking; drafts pre-fill for three safe reply types; every
# edit is graded and taught. Send-only OAuth scope.
REPLIES_ENABLED = True


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
    import os
    # Render provides real env vars (no .env file in the cloud) — the
    # old file-only read meant the global switch could never turn on
    if os.environ.get("PUSH_ON_APPROVE", "").lower() == "true":
        return True
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
    """Bids LaRee sent to Tom & Dallon, minus ones they've marked seen.
    AUTO flags are excluded (LaRee, Jul 13: every failed-transcription
    voicemail wore 'with Dallon & Tom' — that badge means a HUMAN
    escalated it; the machine's own retry notes don't count)."""
    flagged, seen = {}, set()
    for r in load_reviews():
        if r.get("action") == "flag_review":
            if (r.get("by") or "").strip().lower() == "auto":
                continue
            flagged[r["stamp"]] = r
        elif r.get("action") == "review_seen":
            seen.add(r.get("stamp"))
    return [v for k, v in flagged.items() if k not in seen]

# ── QUEUE HYGIENE (Dallon's rule, Jul 7) ─────────────────────
# The office queue is for CUSTOMERS. Mail from Dallon/Tom/the company
# itself, and robot mail, goes to a collapsed drawer instead — shown,
# never dropped. Add more internal senders in data/internal_senders.txt
# (one email or domain per line).
INTERNAL_DEFAULT = ["masterbutlerinc.com", "dallon.masterbutler@gmail.com",
                    # Tom + Kate (Dallon, Jul 9: Tom's office back-and-
                    # forth was sitting on the queue as customer bids)
                    "tomfricke2007@gmail.com", "frickefamily07@gmail.com"]
NOISE_SENDERS = ["no-reply", "noreply", "donotreply", "marketing@",
                 "accounts.google.com", "notifications@", "newsletter",
                 "mailer@", "google-maps-platform",
                 "invoice+statements", "@stripe.com", "receipts@",
                 # Jessica, Jul 9: "Facebook should always be removed"
                 "facebookmail.com", "@facebook.com", "@instagram.com",
                 "nicejob", "paystone"]     # review-platform marketing


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
    if rec.get("tech_sender"):        # field traffic (Jessica, Jul 9) —
        return "main", None           # tagged 👷, visible, never a bid
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
        d_bid = ((rec.get("draft") or {}).get("bid") or {})
        if rec.get("draft") is not None and not d_bid.get("services"):
            # a draft with NO priced lines must not "guess" a total from
            # note text (Tillie: a lights request showed Est. $80 lifted
            # from an aerial pressure-washing menu note)
            rec["total_guess"] = None
        else:
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
    for r in load_reviews():          # approve-pushed quotes carry their
        if r.get("jobber_quote") and r.get("jobber_url"):   # own link
            urls[r["jobber_quote"]] = r["jobber_url"]
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
      --goldbg:#fdf4dd;--goldink:#7a5300;--alarm:#b03a2e;
      /* whites warmed a step — Jessica, Jul 9: 'the white against the
         green is really aggressive on the eyes' */
      --bg:#f2f3ee;--ink:#1a1e1c;--mut:#6b736e;--line:#e4e6e0;
      --card:#fcfcf9;--soft:#eef0ea;
      --bluebg:#e5edff;--blueink:#1d4ed8;
      --purplebg:#f0e9fd;--purpleink:#6d28d9;--heading:#0b3d2e}
/* the mockup's GREEN-SCALE dark theme (Dallon: 'the background is
   green as well') — follows the machine's appearance setting */
@media (prefers-color-scheme: dark){
  :root{--bg:#12211a;--card:#1a2f25;--line:#2f4a3c;--ink:#eaf2ec;
        --mut:#a3bcae;--soft:#24402f;--goldbg:#3d3213;--goldink:#eccf7e;
        --accent:#6cc794;--green2:#5abd85;
        --bluebg:#1d2c4d;--blueink:#a3c0f7;
        --purplebg:#2c2050;--purpleink:#cdbaf5;--heading:#8fd8b0}
}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,
     sans-serif;margin:0;background:var(--bg);color:var(--ink);
     font-size:15px;line-height:1.55}
.rail{position:fixed;left:0;top:0;bottom:0;width:212px;z-index:60;
      background:linear-gradient(175deg,var(--green),#08301f);
      display:flex;flex-direction:column;padding:20px 12px}
.rail .brand{color:#fff;
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
       position:sticky;top:0;z-index:50}
header #who{color:var(--mut)}
header #who b{color:var(--green)}
.wrap{max-width:1440px;margin:0 auto;padding:24px 24px 48px;flex:1}
.pagewrap{max-width:1440px;margin:0 auto}
/* BIG SCREENS (Dallon, Jul 14: 'so much wasted space — half the
   page for the list, half for the customer'). Under 1700px nothing
   changes; above it the page uses nearly the full width and the
   list defaults to ~45%% (drag handle still wins). */
@media(min-width:1700px){
 .pagewrap,.wrap{max-width:calc(100vw - 150px)}
 .inboxgrid{--ilistw:45%}}
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
th{text-align:left;color:var(--mut);font-weight:700;padding:10px 8px;
   border-bottom:1px solid var(--line);font-size:10px;
   text-transform:uppercase;letter-spacing:1px}
td{padding:13px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:var(--soft)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td b{color:var(--green)}
.age{font-weight:700;font-variant-numeric:tabular-nums}
.age.warn{color:#c77700}.age.late{color:var(--alarm)}
.chip{display:inline-block;background:var(--soft);border-radius:999px;
      padding:3px 12px;margin:2px 3px 2px 0;font-size:12px;
      color:var(--ink);font-weight:500}
.flag{background:#fdecea;color:#a93226}
.win{background:#e6f4ea;color:#1e6b34;font-weight:600}
@media (prefers-color-scheme: dark){
  .win{background:#173525;color:#7fd6a2}
  .flag{background:#3a1713;color:#f1998e}
}
.ok{color:var(--accent);font-weight:600}
a{color:var(--green2);text-decoration:none}a:hover{text-decoration:underline}
pre{background:var(--soft);border:1px solid var(--line);border-radius:12px;
    padding:14px;font-size:12.5px;overflow-x:auto;white-space:pre-wrap}
.notes{background:#fffdf5;border:1px solid #efe6c8;border-radius:12px;
       padding:12px 16px}
.notes div{padding:4px 0;border-bottom:1px dashed #eee}
.notes div:last-child{border-bottom:0}
button,.btn{background:var(--green);color:#fff;border:0;border-radius:11px;
       padding:9px 18px;font-size:14px;font-weight:700;cursor:pointer;
       margin:3px 3px 3px 0;transition:transform .1s,filter .12s;
       font-family:inherit}
button:hover{filter:brightness(1.12)}
button:active{transform:scale(.96)}
button.big{padding:12px 22px;font-size:14.5px;background:var(--green);
       color:#fff;font-weight:800}
button.gray{background:var(--soft);color:var(--ink);
       border:1px solid var(--line);font-weight:600}
button.red{background:#b03a2e}
.reason{background:var(--card);color:var(--green2);border:1.5px solid var(--green2);
        font-weight:500;padding:7px 12px}
.reason.sel{background:var(--green2);color:#fff}
input[type=text],input[type=date],select,textarea{width:100%;padding:10px 12px;
       border:1px solid var(--line);border-radius:10px;font-size:14px;
       font-family:inherit;background:var(--card);color:var(--ink)}
select option{background:var(--card);color:var(--ink)}
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
/* ——— THE INBOX (mockup-exact, Jul 9) ——— */
.mock{background:var(--card);border:1px solid var(--line);
  border-radius:16px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.05);
  margin-top:6px}
.chrome{background:var(--green);color:#e9efe9;padding:11px 20px;
  font-size:13.5px;display:flex;gap:16px;align-items:center;
  flex-wrap:wrap}
.chrome > b{white-space:nowrap}
.chrome b{font-size:14.5px;color:#fff}
.chrome .navr{margin-left:auto;display:flex;gap:4px;align-items:center}
.chrome .navr a{color:#cfe0d6;text-decoration:none;padding:5px 12px;
  border-radius:8px;font-weight:600;font-size:13px;white-space:nowrap}
.chrome .navr a:hover{color:#fff;text-decoration:none}
.chrome .navr a.on{background:rgba(255,255,255,.14);color:#fff}
.chrome .whobox{margin-left:14px;padding-left:14px;
  border-left:1px solid rgba(255,255,255,.25);opacity:.95;font-size:13px}
.inboxgrid{display:grid;grid-template-columns:var(--ilistw,330px) 6px 1fr;
  min-height:640px}
.iresize{cursor:col-resize;background:transparent;position:relative}
.iresize:hover,.iresize.on{background:rgba(201,162,39,.35)}
@media(max-width:840px){.inboxgrid{grid-template-columns:1fr}
  .iresize{display:none}}
.ilist{border-right:1px solid var(--line);padding:8px;overflow-y:auto;
  max-height:calc(100vh - 140px)}
.idetail{display:flex;flex-direction:column;max-height:calc(100vh - 140px);
  overflow-y:auto}
.ifolds{padding:8px 18px 22px}

.ihead{padding:12px 12px 4px;font-size:11px;font-weight:800;color:var(--mut);
  text-transform:uppercase;letter-spacing:1px}
.irow{display:block;padding:12px;border-radius:10px;cursor:pointer;
  margin:1px 0;text-decoration:none;color:var(--ink)}
.irow:hover{background:var(--soft);text-decoration:none}
.irow.sel{background:var(--soft);outline:2px solid var(--green2)}
.irow .nm{font-size:14.5px;font-weight:600}
.irow.unread .nm{font-weight:800}
.irow.readq{opacity:.62}
.irowwrap{display:flex;align-items:flex-start;gap:0;position:relative}
.irowwrap .irow{flex:1;min-width:0}
.rowdone{position:absolute;right:8px;top:50%;transform:translateY(-50%);
  width:34px;height:34px;border-radius:9px;border:1px solid
  rgba(201,162,39,.35);background:rgba(17,41,33,.9);color:#c9a227;
  font-size:16px;font-weight:800;cursor:pointer;display:none;
  padding:0;margin:0;line-height:1}
.irowwrap:hover .rowdone{display:block}
.rowdone:hover{background:#c9a227;color:#0b3d2e}
.rowsel{flex:none;width:16px;height:16px;margin:14px 4px 0 4px;cursor:pointer;
  accent-color:var(--green2)}
.bulkbar{display:none;position:sticky;top:0;z-index:6;align-items:center;
  gap:12px;flex-wrap:wrap;background:var(--goldbg);border:1px solid var(--gold);
  border-radius:10px;padding:8px 12px;margin:0 0 10px}
.bulkbar.show{display:flex}
#bulkcount{font-weight:800;color:var(--goldink);font-size:12.5px}
.bulkbar .bulkgo{background:var(--green);color:#fff;border:none;border-radius:8px;
  padding:6px 14px;font-weight:800;cursor:pointer;font-size:12.5px}
.bulkbar .bulklink{background:none;border:none;color:var(--green2);
  font-weight:700;cursor:pointer;font-size:12.5px;text-decoration:underline;
  padding:0}
.irow .pv{color:var(--mut);font-size:12.5px;white-space:normal;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;margin-top:1px}
.irow .meta{display:flex;justify-content:space-between;margin-top:3px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
  background:var(--gold);margin-right:9px;vertical-align:1px}
.word{font-size:11px;font-weight:700;letter-spacing:.4px;color:var(--mut);
  text-transform:uppercase}
.word.act{color:var(--green2)}
.iage{color:var(--mut);font-variant-numeric:tabular-nums;font-size:12.5px}
.iage.alarm{color:var(--alarm);font-weight:700}
.msgbody{white-space:pre-wrap;overflow:hidden;display:-webkit-box;
  -webkit-box-orient:vertical;-webkit-line-clamp:3;line-clamp:3}
.msgbody.open{-webkit-line-clamp:unset;line-clamp:unset;display:block}
.msgmore{background:none;border:none;padding:3px 0 0;margin:0;font:inherit;
  font-size:11px;font-weight:700;cursor:pointer;color:inherit;opacity:.72;
  text-decoration:underline}
.pinned{padding:20px 26px 16px;border-bottom:2px solid var(--line);
  background:var(--card)}
.pin-top{display:flex;justify-content:space-between;gap:18px;
  align-items:flex-start;flex-wrap:wrap}
.pin-top h2{margin:0;font-size:22px;letter-spacing:-.4px;color:var(--ink)}
.pin-top .paddr{color:var(--mut);font-size:13.5px;margin-top:2px}
.money{text-align:right}
.money .ptotal{font-size:34px;font-weight:800;letter-spacing:-1.5px;
  color:var(--green2);line-height:1}
.money .conf{font-size:12px;font-weight:700;color:var(--mut);margin-top:2px}
.say{background:var(--soft);border-radius:11px;padding:10px 14px;
  font-style:italic;font-size:14px;margin-top:12px}
.pchips{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.pchips .chip{background:var(--goldbg);color:var(--goldink);
  border-radius:999px;padding:3px 12px;font-size:12px;font-weight:700}
.pchips .chip.blue{background:var(--bluebg);color:var(--blueink)}
.pchips .chip.purple{background:var(--purplebg);color:var(--purpleink)}
.actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.readbtn{border:1px solid var(--line);background:none;border-radius:7px;
  color:var(--mut);font-size:11px;padding:3px 9px;cursor:pointer;
  vertical-align:4px;margin-left:8px;font-weight:600}
.ifold{border:1px solid var(--line);border-radius:12px;margin-top:10px;
  background:var(--card);padding:0}
.ifold summary{cursor:pointer;font-weight:800;font-size:16px;
  padding:16px 18px;display:flex;align-items:center;gap:10px;
  list-style:none;color:var(--ink)}
.ifold summary::-webkit-details-marker{display:none}
.ifold summary::before{content:"▸";color:var(--green2);font-size:15px;
  transition:transform .12s}
.ifold[open] summary::before{transform:rotate(90deg)}
.ifold .peek{color:var(--mut);font-weight:500;font-size:12.5px;
  margin-left:auto;text-align:right}
.ifold .fcount{background:var(--soft);border-radius:999px;font-size:11.5px;
  padding:1px 9px;color:var(--mut);font-weight:700}
.ifold .fbody{padding:4px 20px 18px;border-top:1px dashed var(--line)}
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
/* ——— STITCH PAGE PATTERN (Settings + Win-back ports, Jul 10 pm) ——— */
.pghead{display:flex;justify-content:space-between;align-items:flex-end;
  gap:16px;flex-wrap:wrap;border-bottom:1px solid rgba(201,162,39,.14);
  padding-bottom:16px;margin-bottom:18px}
.pghead h1{margin:0;font-size:30px;font-weight:900;letter-spacing:-.6px;
  color:var(--heading)}
.pghead .sub{color:var(--mut);font-size:14px;margin-top:3px}
.statchips{display:flex;gap:10px;flex-wrap:wrap}
.statchip{background:var(--card);border:1px solid rgba(201,162,39,.22);
  border-radius:12px;padding:8px 16px;display:flex;
  flex-direction:column;align-items:flex-end;min-width:110px}
.statchip .l{font-size:9px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.4px;color:var(--mut)}
.statchip .v{font-size:21px;font-weight:800;color:var(--ink);
  font-variant-numeric:tabular-nums;line-height:1.25}
.statchip.gold{background:#c9a227;border-color:#c9a227}
.statchip.gold .l{color:rgba(11,61,46,.65)}
.statchip.gold .v{color:#0b3d2e}
.schead{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.schead svg{width:26px;height:26px;color:#c9a227;flex:none}
.schead h2{margin:0;font-size:20px;font-weight:800;color:var(--heading)}
.wbrow{display:flex;align-items:center;gap:14px;background:var(--card);
  border:1px solid rgba(201,162,39,.15);border-radius:13px;
  padding:12px 16px;margin-bottom:8px;transition:transform .12s}
.wbrow:hover{transform:translateX(4px);
  border-color:rgba(201,162,39,.4)}
.avat{width:46px;height:46px;flex:none;border-radius:50%;
  background:linear-gradient(135deg,#0b3d2e,#155e49);
  border:1px solid rgba(201,162,39,.35);color:#e8c56a;font-weight:800;
  display:flex;align-items:center;justify-content:center;font-size:15px}
.wbrow .cols{flex:1;display:grid;
  grid-template-columns:2fr 1fr 1fr auto;gap:12px;align-items:center;
  min-width:0}
.wbrow .klabel{font-size:9px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.2px;color:var(--mut)}
.wbrow .kval{font-weight:800;color:var(--ink);
  font-variant-numeric:tabular-nums}
.pill{border-radius:9px;padding:7px 12px;font-size:10px;font-weight:800;
  text-transform:uppercase;letter-spacing:1.1px;cursor:pointer;
  white-space:nowrap;margin:0}
.pill.go{background:#173525;color:#7fd6a2;
  border:1px solid rgba(127,214,162,.35)}
.pill.dim{background:var(--soft);color:var(--mut);
  border:1px solid var(--line)}
.pill:hover{filter:brightness(1.25)}
.renewgrid{display:grid;grid-template-columns:repeat(auto-fill,
  minmax(260px,1fr));gap:10px}
.renewgrid .renew{margin-bottom:0}
.renew{background:var(--card);border:1px solid rgba(201,162,39,.15);
  border-left:4px solid var(--line);border-radius:13px;
  padding:16px 18px;margin-bottom:10px}
.renew.hot{border-left-color:#c9a227}
.renew .due{border-radius:999px;padding:2px 10px;font-size:10px;
  font-weight:800;background:var(--soft);color:var(--mut);
  border:1px solid var(--line)}
.renew.hot .due{background:rgba(201,162,39,.16);color:#e8c56a;
  border-color:rgba(201,162,39,.4)}
/* NIGHTLY REPORT SHELF (Dallon, Jul 12): chips swap one compact card */
.rchips{display:flex;flex-wrap:wrap;gap:7px;margin-top:16px}
.rchip{border:1px solid rgba(201,162,39,.18);background:rgba(17,41,33,.5);
  color:var(--mut);border-radius:11px;padding:9px 14px;cursor:pointer;
  font-weight:800;font-size:12.5px;font-family:inherit}
.rchip:hover{border-color:rgba(201,162,39,.45)}
.rchip.on{background:#c9a227;color:#0b3d2e;border-color:#c9a227}
.rcard{background:rgba(17,41,33,.55);border:1px solid
  rgba(201,162,39,.16);border-radius:14px;padding:16px 20px;
  margin-top:8px;display:none}
.rcard.on{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.rcard .rhead{font-size:34px;font-weight:900;color:#c9a227;
  font-variant-numeric:tabular-nums;line-height:1.05;
  text-shadow:0 0 12px rgba(201,162,39,.35)}
.rcard .rsub{font-size:12px;color:var(--mut);margin-top:3px;
  max-width:220px}
.rcard .rlines{font-size:13px;line-height:1.7;flex:1;min-width:200px}
.rcard .rbars{display:flex;gap:4px;align-items:flex-end;height:44px}
.rcard .rbars i{display:block;width:14px;border-radius:3px 3px 0 0;
  background:rgba(201,162,39,.5)}
.rcard .rbars i:last-child{background:#c9a227}
.wchips{display:flex;flex-wrap:wrap;gap:8px}
.wchip{display:inline-flex;align-items:center;gap:9px;
  background:rgba(17,41,33,.55);border:1px solid rgba(201,162,39,.16);
  border-radius:999px;padding:8px 15px;font-weight:700;font-size:13px;
  color:var(--ink);text-decoration:none}
.wchip:hover{border-color:rgba(201,162,39,.45);text-decoration:none}
.wchip b{color:#c9a227;font-variant-numeric:tabular-nums;
  text-shadow:0 0 8px rgba(201,162,39,.3)}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;
  margin-top:16px}
.tiles.five{grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:1000px){.tiles.five{grid-template-columns:1fr 1fr}}
@media(max-width:760px){.tiles,.tiles.five{grid-template-columns:1fr}
  .wbrow .cols{grid-template-columns:1fr auto}}
.tile{background:rgba(17,41,33,.5);border:1px solid rgba(201,162,39,.14);
  border-radius:16px;padding:18px;display:flex;gap:14px;
  align-items:center}
.tile .ticon{background:rgba(201,162,39,.16);border-radius:12px;
  padding:10px;color:#c9a227;display:flex}
.tile .ticon svg{width:26px;height:26px}
.tile .tl{font-size:10px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.4px;color:var(--mut)}
.tile .tv{font-size:23px;font-weight:900;color:var(--ink);
  font-variant-numeric:tabular-nums;line-height:1.2}
.tile .ts{font-size:10px;font-weight:700;color:var(--green2)}
.bento{display:grid;grid-template-columns:2fr 1fr;gap:16px;
  align-items:start}
@media(max-width:960px){.bento{grid-template-columns:1fr}}
.healthcard{background:linear-gradient(150deg,#0b3d2e,#0e4a37);
  border:1px solid rgba(201,162,39,.25);border-radius:16px;
  padding:20px 22px;color:#eef4f0}
.healthcard h3{margin:0 0 14px;color:#e8c56a;font-size:15px;
  font-weight:800}
.knobgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:700px){.knobgrid{grid-template-columns:1fr}}
.knob{display:block;background:rgba(17,41,33,.5);
  border:1px solid rgba(201,162,39,.14);border-radius:13px;
  padding:12px 14px}
.knob.set{border-color:rgba(201,162,39,.5)}
.knob .kl{display:block;font-size:10px;font-weight:800;
  text-transform:uppercase;letter-spacing:1.1px;color:var(--mut)}
.knob .kv{display:block;font-size:24px;font-weight:900;
  color:#c9a227;font-variant-numeric:tabular-nums;margin:2px 0 8px;
  text-shadow:0 0 10px rgba(201,162,39,.25)}
.knob .kv i{font-style:normal;font-size:9px;font-weight:800;
  letter-spacing:1.2px;text-transform:uppercase;color:var(--mut);
  margin-left:8px;vertical-align:4px}
.knob.set .kv i{color:#e8c56a}
.knob input{width:100%;text-align:right}
.qrgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:700px){.qrgrid{grid-template-columns:1fr}}
.qrcard{background:rgba(17,41,33,.5);border:1px solid
  rgba(201,162,39,.14);border-radius:13px;padding:12px 14px;margin:0}
.qrcard:hover{border-color:rgba(201,162,39,.4)}
.qrcard summary{cursor:pointer;list-style:none}
.qrcard summary::-webkit-details-marker{display:none}
.qrcard summary b{display:block;color:var(--ink);margin:4px 0 3px}
.qrcard .qtag{font-size:9px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.3px;color:#e8c56a}
.qrcard .qpeek{display:block;font-size:12px;color:var(--mut);
  line-height:1.45;overflow:hidden;display:-webkit-box;
  -webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2}
.hbar{width:100%;height:4px;background:rgba(255,255,255,.12);
  border-radius:99px;overflow:hidden;margin:6px 0 12px}
.hbar i{display:block;height:100%;background:#c9a227}
/* dark component overrides LAST — cascade order beats the light rules */
@media (prefers-color-scheme: dark){
  .ring{background:var(--card)}
  header{background:var(--card);color:var(--ink)}
  td b{color:var(--accent)}
  a{color:var(--green2)}
  .card h2,.card h3{color:var(--accent)}
  .headline .total,.stat b{color:var(--accent)}
  .money .ptotal{color:var(--accent)}
  .notes{background:var(--soft);border-color:var(--line)}
  .band{background:var(--soft);border-color:var(--line)}
  .win{background:#173525;color:#7fd6a2}
  .flag{background:#3a1713;color:#f1998e}
}
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



def _my_signature_html(user):
    """Per-person signature (Dallon, Jul 13: 'saveable for each account
    like quick responses — some people have tags: office manager,
    coordinator'). Overrides the shared one when set; blank = use
    shared. {name} still auto-fills."""
    if not user:
        return ("<div style='margin-top:14px;border-top:1px solid "
                "rgba(201,162,39,.14);padding-top:12px'><div class='subtext'>"
                "★ Pick your name in the top bar to set your own signature "
                "(with a title like Office Manager).</div></div>")
    import mailer as _ml
    mine = (_blob_rw("email_signatures_personal", {})).get(user, "")
    shared = _blob_rw("email_signature", "") or _ml.DEFAULT_SIGNATURE
    starter = mine or shared.replace(
        "— {name}", "— {name}, Office Manager")
    preview = starter.replace("{name}", user)
    star = "★ using your own" if mine else "using the shared one"
    return (
        f"<div style='margin-top:14px;border-top:1px solid "
        f"rgba(201,162,39,.14);padding-top:12px'>"
        f"<div style='font-weight:800;color:var(--heading);"
        f"font-size:14px;margin-bottom:4px'>★ {esc(user)}'s own signature"
        f"</div>"
        f"<div class='subtext' style='margin-bottom:8px'>Add your title "
        f"here (Office Manager, Coordinator…). This overrides the shared "
        f"signature for <b>your</b> replies only — right now you're "
        f"<b>{star}</b>.</div>"
        f"<form method='POST' action='/signature_save'>"
        f"<input type='hidden' name='mine' value='1'>"
        f"<textarea name='signature' rows='4' style='font-family:"
        f"ui-monospace,Menlo,monospace;font-size:13px'>{esc(starter)}"
        f"</textarea>"
        f"<div style='margin-top:10px;background:rgba(17,41,33,.6);"
        f"border:1px solid rgba(201,162,39,.16);border-radius:10px;"
        f"padding:12px 14px'><div style='font-size:10px;font-weight:800;"
        f"letter-spacing:1.2px;text-transform:uppercase;color:var(--mut);"
        f"margin-bottom:6px'>Preview</div><div style='white-space:"
        f"pre-wrap;font-size:13.5px;color:var(--ink)'>{esc(preview)}</div>"
        f"</div>"
        f"<button style='margin-top:10px'>Save my signature</button>"
        + (f" <button name='clear' value='1' class='gray' "
           f"onclick=\"return confirm('Use the shared signature "
           f"instead?')\">Use shared instead</button>" if mine else "")
        + "</form></div>")


def _roof_label(raw):
    """County assessor roof codes → plain English (Dallon, Jul 13:
    'Comp Sh To 235#' means composition shingle, 235-lb standard
    asphalt — show it readable). Clean categories pass through."""
    s = (str(raw) or "").lower()
    if s in ("standard", "composition", "comp"):
        return "Composition (standard)"
    if "shake" in s or "wood" in s or "cedar" in s:
        return "Cedar shake"
    if "tile" in s:
        return "Tile"
    if "metal" in s or "standing seam" in s:
        return "Metal"
    if s.startswith("comp") or "shingle" in s or "asphalt" in s or "#" in s:
        return "Composition (standard)"     # 'comp sh to 235#' etc.
    return str(raw).title()


def _svg_icon(name):
    d = {"queue": '<path d="M3 5h18v14H3z"/><path d="M3 13h5l2 3h4l2-3h5"/>',
         "people": '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c.6-3 '
                   '2.8-4.5 5.5-4.5S13.9 16 14.5 19"/><circle cx="17" '
                   'cy="9" r="2.6"/><path d="M15.5 14.8c2.4.2 4.2 1.6 '
                   '4.9 4.2"/>',
         "van": '<path d="M2 7h11v9H2z"/><path d="M13 10h4l3 3v3h-7"/>'
                '<circle cx="6.5" cy="17.5" r="1.7"/>'
                '<circle cx="16.5" cy="17.5" r="1.7"/>',
         "chart": '<path d="M4 20V10"/><path d="M10 20V4"/>'
                  '<path d="M16 20v-7"/><path d="M21 20H3"/>',
         "board": '<rect x="3" y="3" width="18" height="18" rx="2"/>'
                  '<rect x="6.5" y="6.5" width="4.5" height="11" rx="1"/>'
                  '<rect x="13" y="6.5" width="4.5" height="6" rx="1"/>',
         "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 '
                 '1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06'
                 'a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 '
                 '2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 '
                 '0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 '
                 '1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 '
                 '1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-'
                 '1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 '
                 '0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 '
                 '0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 '
                 '1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 '
                 '1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 '
                 '2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
         "phone": '<path d="M5 4h4l2 5-2.5 1.5a12 12 0 0 0 5 5L15 13l5 '
                  '2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z"/>',
         "help": '<circle cx="12" cy="12" r="9"/><path d="M9.4 9a2.8 '
                 '2.8 0 1 1 4 2.6c-1 .5-1.4 1.1-1.4 2.1"/>'
                 '<circle cx="12" cy="16.8" r="1.15" '
                 'fill="currentColor" stroke="none"/>',
         # card-header icons for the Stitch page pattern (Jul 10 pm)
         "tune": '<path d="M4 7h9M19 7h1M4 17h3M13 17h7"/>'
                 '<circle cx="16" cy="7" r="2.4"/>'
                 '<circle cx="10" cy="17" r="2.4"/>',
         "chat": '<path d="M4 5h16v11H10l-6 4z"/>'
                 '<path d="M8 9h8M8 12h5"/>',
         "percent": '<path d="M19 5 5 19"/>'
                    '<circle cx="7.5" cy="7.5" r="2.4"/>'
                    '<circle cx="16.5" cy="16.5" r="2.4"/>',
         "repeat": '<path d="M17 2l4 4-4 4"/>'
                   '<path d="M3 11V9a4 4 0 0 1 4-4h14"/>'
                   '<path d="M7 22l-4-4 4-4"/>'
                   '<path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
         "trend": '<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>',
         "check": '<circle cx="12" cy="12" r="9"/>'
                  '<path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
         # site-specifications rail icons (Stitch Bid Review, Jul 10 pm)
         "sq": '<rect x="4" y="4" width="16" height="16" rx="2"/>'
               '<path d="M9 4v3M14 4v3M4 9h3M4 14h3"/>',
         "height": '<path d="M12 4v16M8.5 7.5L12 4l3.5 3.5'
                   'M8.5 16.5L12 20l3.5-3.5"/>',
         "pitch": '<path d="M4 19h16M4 19L16 7l4 4"/>',
         "home": '<path d="M3 11l9-7 9 7"/><path d="M6 9.5V19h12V9.5"/>',
         "leaf": '<path d="M5 19C5 11 11 5 19 5c0 8-6 14-14 14z"/>'
                 '<path d="M5 19c3-5 6-8 10-10"/>',
         "info": '<circle cx="12" cy="12" r="9"/>'
                 '<path d="M12 11v5"/><circle cx="12" cy="8" r="1.1" '
                 'fill="currentColor" stroke="none"/>'}
    return ('<svg width="22" height="22" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="1.8" stroke-linecap="round"'
            ' stroke-linejoin="round">' + d.get(name, "") + "</svg>")


_DARKROOM_CSS = """<style>
body{background:#05140f!important}
.mock.dkroom{--bg:#05140f;--card:rgba(17,41,33,.72);--soft:#112921;
 --line:rgba(201,162,39,.18);--ink:#e2e8f0;--mut:#a3adab;
 --gold:#c9a227;--goldbg:rgba(201,162,39,.13);--goldink:#e8c56a;
 --heading:#e8c56a;--alarm:#fca5a5;--green2:#5fbd85;
 background:#05140f;border-color:rgba(201,162,39,.2);
 color:var(--ink)}
.mock.dkroom .chrome{background:#082d22;border-bottom:1px solid
 rgba(201,162,39,.18)}
.mock.dkroom .chrome .navr > a{display:none}
.mock.dkroom .chrome .whobox a{display:inline;color:#123527;
 font-weight:800;text-decoration:underline}
.mock.dkroom .chrome .whobox{background:#c9a227;color:#123527;
 border-radius:99px;padding:4px 12px;font-weight:800}
.dkshell{display:flex;min-height:70vh}
.dkrail{width:56px;flex:none;background:#082d22;border-right:1px solid
 rgba(201,162,39,.18);display:flex;flex-direction:column;align-items:center;
 gap:6px;padding:14px 0}
.dkrail a{width:38px;height:38px;flex:none;display:flex;align-items:center;
 justify-content:center;border-radius:10px;color:#9db3a7;
 border:1px solid transparent}
.dkrail a.on{background:#c9a227;color:#123527}
.dkrail a:hover{border-color:rgba(201,162,39,.3)}
.dkrail .sp{flex:1}
.mock.dkroom .irow{background:rgba(17,41,33,.5);border:1px solid
 rgba(201,162,39,.10);border-radius:12px;margin:4px 0}
.mock.dkroom .irow:hover{border-color:rgba(201,162,39,.4);
 background:rgba(17,41,33,.8)}
.mock.dkroom .irow.sel{outline:none;border-left:3px solid #c9a227;
 background:rgba(17,41,33,.95)}
.mock.dkroom .irow .nm{color:#fff}
.mock.dkroom .dot{box-shadow:0 0 8px rgba(201,162,39,.7);
 background:#c9a227}
.mock.dkroom .ihead{color:#a3adab;letter-spacing:1.6px}
.mock.dkroom .pinned,.mock.dkroom .card{background:rgba(17,41,33,.72);
 border:1px solid rgba(201,162,39,.18);border-radius:14px}
.mock.dkroom .money{color:#c9a227;
 text-shadow:0 0 12px rgba(201,162,39,.4)}
.mock.dkroom .big{background:#c9a227;color:#0b3d2e;font-weight:800}
.mock.dkroom input[type=text],.mock.dkroom textarea,.mock.dkroom select{
 background:rgba(0,0,0,.35);border:1px solid rgba(201,162,39,.18);
 color:#e2e8f0}
.mock.dkroom .bulkbar{background:rgba(201,162,39,.12);
 border-color:rgba(201,162,39,.45)}
.mock.dkroom .qhero{position:relative;border-radius:14px;overflow:hidden;
 border:1px solid rgba(201,162,39,.2);aspect-ratio:5/2;min-height:210px;
 max-height:360px;margin-bottom:12px;
 background:linear-gradient(150deg,#14402f,#0a2b1f 60%,#07231a)}
.mock.dkroom .qhero img{position:absolute;inset:0;width:100%;height:100%;
 object-fit:cover;object-position:center 32%}
.mock.dkroom .qhero .shade{position:absolute;inset:0;background:
 linear-gradient(to top,rgba(5,20,15,.92),rgba(5,20,15,.18) 55%)}
.mock.dkroom .qhero .foot{position:absolute;left:16px;right:16px;bottom:12px}
.mock.dkroom .qhero .lbl{font-size:9px;font-weight:800;letter-spacing:2px;
 text-transform:uppercase;color:rgba(255,255,255,.72)}
.mock.dkroom .qhero .pr{font-size:38px;font-weight:800;color:#c9a227;
 text-shadow:0 0 14px rgba(201,162,39,.45);line-height:1.05}
.mock.dkroom .qhero .cf{margin-left:12px;font-size:12.5px;color:#9fdcb9;
 font-weight:800;vertical-align:10px}
.mock.dkroom .fixfacts summary::-webkit-details-marker{display:none}
.mock.dkroom .fixfacts summary::marker{content:''}
.mock.dkroom .fixfacts[open] summary span:first-child{transform:
 rotate(90deg);display:inline-block}
/* THE LANES (Dallon, Jul 12) — two per row, big and even (his 2×2
   rule: bigger/clearer over clever) */
.mock.dkroom .lanechips{display:grid;grid-template-columns:1fr 1fr;
 gap:7px;padding:2px 2px 8px}
.mock.dkroom .lanechip{border:1px solid rgba(201,162,39,.3);
 background:#1c3a2c;color:#dfe8e2;border-radius:12px;
 padding:12px 13px;cursor:pointer;font-weight:800;font-size:13.5px;
 display:flex;align-items:center;gap:7px;font-family:inherit;
 width:100%;text-align:left}
.mock.dkroom .lanechip .ln{margin-left:auto}
.mock.dkroom .lanechip:hover{background:#244a38;
 border-color:rgba(201,162,39,.5)}
.mock.dkroom .lanechip .ln{background:rgba(0,0,0,.35);
 border-radius:999px;padding:0 8px;font-size:11px;color:#e2e8f0;
 font-variant-numeric:tabular-nums}
.mock.dkroom .lanechip.on{background:#c9a227;color:#0b3d2e;
 border-color:#c9a227}
.mock.dkroom .lanechip.on .ln{background:rgba(11,61,46,.25);
 color:#0b3d2e}
.mock.dkroom .lanechip .lnew{background:#fca5a5;color:#5c1410;
 border-radius:999px;padding:0 7px;font-size:9.5px;font-weight:800}
.mock.dkroom .lanechip .ltot{background:#3a4a42;color:#cdd8d2;
 border-radius:999px;padding:0 7px;font-size:9.5px;font-weight:800}
.mock.dkroom .laneclear{padding:4px 4px 10px;text-align:right}
.mock.dkroom .laneclear button{background:none;border:1px solid #3a5a48;
 color:#8fc7a6;border-radius:8px;padding:5px 12px;font-size:12px;
 font-weight:700;cursor:pointer}
.mock.dkroom .laneclear button:hover{background:#173226;color:#c9f0d8}
.mock.dkroom .lanesub{color:#a3adab;font-size:11.5px;padding:0 4px 8px}
/* SITE SPECIFICATIONS rail beside the hero (Stitch Bid Review) */
.mock.dkroom .pingrid{display:grid;grid-template-columns:1fr 290px;
 gap:16px;align-items:start}
@media(max-width:1080px){.mock.dkroom .pingrid{grid-template-columns:1fr}}
.mock.dkroom .specrail{background:rgba(17,41,33,.55);
 border:1px solid rgba(201,162,39,.16);border-radius:13px;
 padding:14px 16px}
.mock.dkroom .specrail .sptitle{display:flex;align-items:center;gap:8px;
 font-size:11px;font-weight:800;letter-spacing:1.8px;
 text-transform:uppercase;color:#e8c56a;border-bottom:1px solid
 rgba(201,162,39,.14);padding-bottom:9px;margin-bottom:4px}
.mock.dkroom .specrail .sptitle svg{width:16px;height:16px;
 color:#c9a227}
.mock.dkroom .sprow{display:flex;justify-content:space-between;
 align-items:center;gap:10px;padding:8px 0;
 border-bottom:1px dashed rgba(201,162,39,.1)}
.mock.dkroom .sprow:last-of-type{border-bottom:0}
.mock.dkroom .sprow .sl{display:flex;align-items:center;gap:9px;
 color:#a3adab;font-size:12.5px;font-weight:600}
.mock.dkroom .sprow .sl svg{width:15px;height:15px;color:#c9a227;
 flex:none}
.mock.dkroom .sprow .sv{font-weight:800;color:#c9a227;font-size:17px;
 font-variant-numeric:tabular-nums;text-align:right}
.mock.dkroom .sprow .sv small{font-size:9px;font-weight:800;
 letter-spacing:1px;color:#a3adab;margin-left:4px}
.mock.dkroom .spnote{margin-top:10px;padding:10px 12px;border-radius:10px;
 background:rgba(201,162,39,.07);border-left:3px solid
 rgba(201,162,39,.4);font-size:11px;color:#a3adab;font-style:italic;
 line-height:1.5}
</style>"""


_DARK_FORCE_CSS = '''<style>
/* ONE DARK ROOM SITE-WIDE (Dallon, Jul 10 pm: "i dont like that the
   other colors arent in unison. id like the darker colors to go
   throughout") — the cream light pages are gone; every page wears the
   Bid Queue's emerald-and-gold, regardless of machine appearance. */
:root{--bg:#05140f!important;--card:#0d231b!important;
 --soft:#112921!important;--line:rgba(201,162,39,.18)!important;
 --ink:#e2e8f0!important;--mut:#a3adab!important;
 --goldbg:rgba(201,162,39,.14)!important;--goldink:#e8c56a!important;
 --heading:#e8c56a!important;--accent:#6cc794!important;
 --green2:#5fbd85!important;--alarm:#fca5a5!important;
 --bluebg:#132c46!important;--blueink:#a3c0f7!important;
 --purplebg:#261d4b!important;--purpleink:#cdbaf5!important}
body{background:#05140f!important}
.mock{background:#071b14;border-color:rgba(201,162,39,.2);box-shadow:none}
.chrome{background:#082d22;border-bottom:1px solid rgba(201,162,39,.18)}
header{background:#082d22;color:#e8c56a;box-shadow:none;
 border-bottom:1px solid rgba(201,162,39,.18)}
header #who b{color:#e8c56a}
.ring{background:var(--card)}
td b{color:var(--accent)}
.card,.pinned{background:rgba(17,41,33,.72);
 border:1px solid rgba(201,162,39,.18);border-radius:13px}
.card h2,.card h3{color:#e8c56a}
.card.dark{background:linear-gradient(150deg,#0b3d2e,#0e4a37)}
.headline .total,.stat b{color:#c9a227;
 text-shadow:0 0 12px rgba(201,162,39,.35)}
.money .ptotal{color:#c9a227;text-shadow:0 0 12px rgba(201,162,39,.4)}
.notes{background:rgba(17,41,33,.6);border-color:rgba(201,162,39,.18)}
.notes div{border-bottom-color:rgba(201,162,39,.14)}
.band{background:rgba(201,162,39,.08);border-color:rgba(201,162,39,.3)}
.band h2{color:#e8c56a}
.win{background:#173525;color:#7fd6a2}
.flag{background:#3a1713;color:#f1998e}
button,.btn{background:#0b3d2e;border:1px solid rgba(201,162,39,.35)}
button.big{background:#c9a227;color:#0b3d2e;border-color:#c9a227}
button.gray{background:#112921;color:#e2e8f0;
 border:1px solid rgba(201,162,39,.18)}
.reason{background:transparent}
input[type=text],input[type=date],select,textarea{
 background:rgba(0,0,0,.35);border-color:rgba(201,162,39,.18);
 color:#e2e8f0}
pre{background:rgba(0,0,0,.3);border-color:rgba(201,162,39,.14)}
/* hand-inlined pastel callouts → their dark-room equivalents */
[style*="#fdf4dd"]{background:rgba(201,162,39,.14)!important;
 color:#e8c56a!important;border-color:rgba(201,162,39,.4)!important}
[style*="#f7dfa0"]{background:rgba(201,162,39,.2)!important;
 color:#e8c56a!important;border-color:#c9a227!important}
[style*="#f0e9fd"]{background:#261d4b!important;
 color:#cdbaf5!important;border-color:#4b3a86!important}
[style*="#fdecea"]{background:#3a1713!important;
 color:#f1998e!important;border-color:#6e2a22!important}
[style*="#fffbeb"],[style*="#fbfaf5"],[style*="#f3f4f1"],
[style*="#f2f5f3"],[style*="#fffaf0"]{background:#112921!important;
 color:#e2e8f0!important;border-color:rgba(201,162,39,.18)!important}
</style>'''

_GLOBAL_RAIL_CSS = """<style>
.gr{position:fixed;left:0;top:0;bottom:0;width:86px;z-index:80;
 background:#082d22;border-right:1px solid rgba(201,162,39,.25);
 display:flex;flex-direction:column;align-items:center;gap:8px;
 padding:16px 0}
.gr a{width:60px;height:60px;flex:none;display:flex;align-items:center;
 justify-content:center;border-radius:13px;color:#9db3a7;
 border:1px solid transparent}
.gr a svg{width:28px;height:28px}
.gr a.on{background:#c9a227;color:#123527}
.gr a:hover{border-color:rgba(201,162,39,.35)}
.gr .sp{flex:1}
body{padding-left:102px!important}
.chrome .navr > a{display:none}
.chrome .whobox{background:#c9a227;color:#123527;border-radius:99px;
 padding:4px 12px;font-weight:800}
.chrome .whobox a{display:inline;color:#123527;font-weight:800;
 text-decoration:underline}
@media(max-width:700px){.gr{width:56px}.gr a{width:42px;height:42px}
 .gr a svg{width:24px;height:24px}body{padding-left:66px!important}}
</style>"""


def _rail_html(active="/"):
    links = [("/", "queue", "Bid Queue"), ("/customers", "people",
              "Customers"), ("/routes", "van", "Routes"),
             ("/scoreboard", "chart", "Scoreboard"),
             ("/winback", "phone", "Win-back"),
             ("/working", "board", "Build board — what's being built")]
    out = "<div class='dkrail'>"
    for href, ic, title in links:
        cls = " class='on'" if href == active else ""
        out += (f"<a href='{href}'{cls} title='{title}'>"
                f"{_svg_icon(ic)}</a>")
    out += ("<div class='sp'></div>"
            f"<a href='/settings' title='Settings'>{_svg_icon('gear')}</a>"
            f"<a href='/guide' title='Guide'>{_svg_icon('help')}</a></div>")
    return out


def _chrome_dark():
    """Dark-room top bar: logo + who pill ONLY (one nav = the rail)."""
    return _chrome_bar("Bids")   # nav links hidden by dkroom CSS


def _chrome_bar(active=""):
    """The framed window's green top bar — one for every page."""
    navr = "".join(
        f"<a href='{href}' class='{'on' if t == active else ''}'>{label}</a>"
        for href, label, t in (("/", "📥 Bids", "Bids"),
                               # Dallon Jul 9pm: the file cabinet lives
                               # on its OWN tab, not inside Bids
                               ("/customers", "👥 Customers", "Customers"),
                               ("/routes", "🚐 Routes", "Routes"),
                               ("/brief", "📋 Brief", "Brief"),
                               ("/scoreboard", "📊 Scoreboard", "Scoreboard"),
                               ("/winback", "📞 Win-back", "Win-back"),
                               ("/settings", "⚙️ Settings", "Settings"),
                               ("/guide", "❓ Guide", "Guide"),
                               # Jessica, Jul 9: idea button up top too
                               ("/guide#idea", "💡 Idea", "Idea")))
    return ("<div class='chrome'><b>🎩 Master Butler</b>"
            f"<div class='navr'>{navr}"
            "<span id='who' class='whobox'></span></div></div>"
            + """<script>
(function(){
  var m=document.cookie.match(/office_user=([^;]+)/);
  var el=document.getElementById('who');
  function set(n){document.cookie='office_user='+encodeURIComponent(n)
    +';path=/;max-age=31536000';location.reload();}
  if(m && /^(office|admin|masterbutler|master butler|mb|user)$/i
        .test(decodeURIComponent(m[1]).trim())){
    document.cookie='office_user=;path=/;max-age=0'; m=null;
  }
  if(m){var n=decodeURIComponent(m[1]);
    el.innerHTML='👤 <b>'+n+'</b> <a href="#" style="opacity:.6;color:#cfe0d6">change</a>';
    el.querySelector('a').onclick=function(e){e.preventDefault();
      document.cookie='office_user=;path=/;max-age=0';location.reload();};
  } else {
    el.innerHTML='Who’s working? ';
    ['LaRee','Jessica','Martha','Dallon','Tom'].forEach(function(n){
      var a=document.createElement('a');a.href='#';a.textContent=n;
      a.style.cssText='margin:0 5px;color:#123527;font-weight:800;'
        +'text-decoration:underline';
      a.onclick=function(e){e.preventDefault();set(n);};
      el.appendChild(a);});
  }
})();
</script>""")


_RESIZE_JS = """<script>
(function(){
  var h=document.querySelector('.iresize');
  var g=document.querySelector('.inboxgrid');
  if(!h||!g) return;
  try{var p=localStorage.getItem('ilistpct');
      if(p) g.style.setProperty('--ilistw', p+'%');
      else{var w=localStorage.getItem('ilistw');
        if(w) g.style.setProperty('--ilistw', w+'px');}}catch(e){}
  var drag=false;
  h.addEventListener('mousedown',function(e){
    drag=true;h.classList.add('on');e.preventDefault();
    document.body.style.userSelect='none';});
  document.addEventListener('mousemove',function(e){
    if(!drag) return;
    var r=g.getBoundingClientRect();
    var w=Math.min(r.width*0.68,Math.max(240,e.clientX-r.left));
    g.style.setProperty('--ilistw',Math.round(w)+'px');});
  document.addEventListener('mouseup',function(){
    if(!drag) return;
    drag=false;h.classList.remove('on');
    document.body.style.userSelect='';
    try{var lw=document.querySelector('.ilist')
        .getBoundingClientRect().width;
      var gw=g.getBoundingClientRect().width;
      localStorage.setItem('ilistpct',(lw/gw*100).toFixed(1));
      localStorage.removeItem('ilistw');}catch(e){}
  });
  h.addEventListener('dblclick',function(){
    g.style.removeProperty('--ilistw');
    try{localStorage.removeItem('ilistw');
      localStorage.removeItem('ilistpct');}catch(e){}});
})();
</script>"""


def page(title, body, refresh=None, chrome="rail"):
    auto = (f"<meta http-equiv='refresh' content='{refresh}'>"
            if refresh else "")
    if chrome == "bare":
        # PERSISTENT ICON RAIL (Dallon, Jul 10 pm: 'the bar on the left
        # needs to be persistent through the whole site') — injected
        # here so every framed page carries it; it is the ONE nav.
        railmap = {"Bids": "/", "Customers": "/customers",
                   "Routes": "/routes", "Scoreboard": "/scoreboard",
                   "Win-back": "/winback", "Settings": "/settings",
                   "Guide": "/guide", "Brief": "/brief"}
        rail_css = _GLOBAL_RAIL_CSS
        active = railmap.get(title, "")
        rail = _rail_html(active).replace("class='dkrail'", "class='gr'")
        tone = "" if "dkroom" in body else _DARK_FORCE_CSS
        return (f"<!doctype html><html><head><meta charset='utf-8'>"
                f"<meta name='viewport' content='width=device-width,"
                f"initial-scale=1'>{auto}{FAVICON}"
                f"<title>{title}</title>{STYLE}{rail_css}{tone}</head>"
                f"<body style='padding:14px 16px 0'>{rail}"
                f"<div class='pagewrap'>{body}"
                f"<footer style='padding:10px 4px'>Every quote is a draft "
                f"until a human sends it · bold = nobody's seen it · "
                f"every price traces to a real job.</footer>"
                f"</div>" + _RESIZE_JS + "</body></html>").encode()
    if chrome == "top":
        # THE INBOX CHROME (Dallon Jul 9): no left rail — pages live
        # top-right, the left side of the screen is only ever the list.
        nav = "".join(
            f"<a href='{href}' style='color:{'#fff' if title == t else '#cfe0d6'};"
            f"text-decoration:none;padding:6px 13px;border-radius:8px;"
            f"font-weight:600;font-size:13.5px;"
            f"{'background:rgba(255,255,255,.14)' if title == t else ''}'>"
            f"{label}</a>"
            for href, label, t in (("/", "📥 Bids", "Bids"),
                                   ("/scoreboard", "📊 Scoreboard", "Scoreboard"),
                                   ("/winback", "📞 Win-back", "Win-back"),
                                   ("/settings", "⚙️ Settings", "Settings")))
        return (f"<!doctype html><html><head><meta charset='utf-8'>"
                f"<meta name='viewport' content='width=device-width,"
                f"initial-scale=1'>{auto}{FAVICON}"
                f"<title>{title}</title>{STYLE}</head><body>"
                f"<div class='main' style='margin-left:0'>"
                f"<header style='background:var(--green);color:#e9efe9'>"
                f"<b style='font-size:15px'>🎩 Master Butler</b>"
                f"<span style='margin-left:auto;display:flex;gap:2px;"
                f"align-items:center'>{nav}"
                + """<span id='who' style='margin-left:14px;padding-left:14px;
border-left:1px solid rgba(255,255,255,.25);font-size:13px'></span></span>
<script>
(function(){
  var m=document.cookie.match(/office_user=([^;]+)/);
  var el=document.getElementById('who');
  function set(n){document.cookie='office_user='+encodeURIComponent(n)
    +';path=/;max-age=31536000';location.reload();}
  if(m && /^(office|admin|masterbutler|master butler|mb|user)$/i
        .test(decodeURIComponent(m[1]).trim())){
    document.cookie='office_user=;path=/;max-age=0'; m=null;
  }
  if(m){var n=decodeURIComponent(m[1]);
    el.innerHTML='👤 <b>'+n+'</b> <a href="#" style="opacity:.6;color:#cfe0d6">change</a>';
    el.querySelector('a').onclick=function(e){e.preventDefault();
      document.cookie='office_user=;path=/;max-age=0';location.reload();};
  } else {
    el.innerHTML='Who’s working? ';
    ['LaRee','Jessica','Martha','Dallon','Tom'].forEach(function(n){
      var a=document.createElement('a');a.href='#';a.textContent=n;
      a.style.cssText='margin:0 5px;color:#123527;font-weight:800;'
        +'text-decoration:underline';
      a.onclick=function(e){e.preventDefault();set(n);};
      el.appendChild(a);});
  }
})();
</script></header>"""
                f"<div class='wrap'>{body}</div>"
                f"<footer>Every quote is a draft until a human sends it · "
                f"bold = unread, shared by the whole office · every price "
                f"traces to a real job.</footer></div>"
                + _RESIZE_JS + "</body></html>"

                ).encode()
    # EVERY page lives in the framed green window now (Dallon Jul 9:
    # the left rail's links were dead — deleted; colors match the Inbox)
    inbox_titles = {"Bids", "Bid queue", "Customers", "Messages"}
    active = title if title in ("Scoreboard", "Win-back", "Settings",
                                "Guide") \
        else ("Bids" if title in inbox_titles else "")
    railmap2 = {"Bids": "/", "Bid queue": "/", "Customers": "/customers",
                "Messages": "/customers", "Routes": "/routes",
                "Scoreboard": "/scoreboard", "Win-back": "/winback",
                "Settings": "/settings", "Guide": "/guide"}
    rail2 = _rail_html(railmap2.get(title, "")).replace(
        "class='dkrail'", "class='gr'")
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,"
            f"initial-scale=1'>{auto}{FAVICON}"
            f"<title>{title}</title>{STYLE}{_GLOBAL_RAIL_CSS}"
            f"{_DARK_FORCE_CSS}</head>"
            f"<body style='padding:14px 16px 0'>{rail2}"
            f"<div class='pagewrap'>"
            f"<div class='mock'>{_chrome_bar(active)}"
            f"<div style='padding:18px 22px'>"
            + (f"<div style='font-size:11px;font-weight:800;"
               f"text-transform:uppercase;letter-spacing:1.2px;"
               f"color:var(--mut);margin-bottom:10px'>{title}</div>"
               if not active else "")
            + f"{body}</div></div>"
            f"<footer style='padding:10px 4px'>Every quote is a draft "
            f"until a human sends it · every price traces to a real job."
            f"</footer></div>"
            + """<script>
document.querySelectorAll('tr[data-href]').forEach(function(t){
  t.style.cursor='pointer';
  t.addEventListener('click', function(e){
    if (e.target.closest('a,button,form,input,select,textarea,details')) return;
    location = t.dataset.href;
  });
});
</script>"""
            + _RESIZE_JS
            + "</body></html>").encode()


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _num(v):
    """A record's number, or None if it's junk — so one corrupted
    confidence/total can't crash a page for the whole office (Jul 10
    shadow-test finding: a string confidence took down the Inbox)."""
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _money(v):
    """'$1,234' from any record value; '$?' if it's not a number — a
    corrupt line-item price never crashes a card (Jul 10)."""
    n = _num(v)
    return f"${n:,.0f}" if n is not None else "$?"


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
        c = _num(b.get("confidence"))    # str/garbage → None, no crash
        conf = ("—" if c is None else
                f"<b style='color:{'#1e8449' if c >= 75 else '#c77700' if c >= 50 else '#c0392b'}'>{c:.0f}%</b>")
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
        n_decided = decided_rows.count("<tr>")
        decided_html = (
            f"<details class='card'><summary style='cursor:pointer;"
            f"font-weight:700;color:var(--mut)'>✅ Recently decided "
            f"({n_decided}) — open if you need to double-check one · "
            f"<a href='/history'>full history</a></summary>"
            "<table style='margin-top:8px'><tr><th>Customer</th>"
            "<th>Status</th><th>Decision</th><th>When</th></tr>"
            + decided_rows + "</table></details>")

    aside_html = ""
    # spam gets its own drawer — filtered, never vanished: the office
    # can rescue a mistake with one look (never-hide-a-customer rule)
    spam_pile = [(b, w) for b, w in aside
                 if "spam" in (w or "") or "solicitation" in (w or "")]
    aside = [(b, w) for b, w in aside if (b, w) not in spam_pile]
    if spam_pile:
        items = "".join(
            f"<div>🚫 <a href='/bid/{b['stamp']}'>{esc(b['from'])[:44]}</a> "
            f"<span style='color:var(--mut)'>({esc(why)})</span></div>"
            for b, why in spam_pile)
        aside_html += (
            f"<details class='card'><summary style='cursor:pointer;"
            f"color:var(--mut)'>🚫 Filtered as spam ({len(spam_pile)}) — "
            f"glance occasionally; open one if it's actually a customer"
            f"</summary>{items}</details>")
    if aside:
        items = "".join(
            f"<div>· <a href='/bid/{b['stamp']}'>{esc(b['from'])[:44]}</a> "
            f"<span style='color:var(--mut)'>({esc(why)})</span></div>"
            for b, why in aside)
        aside_html += (f"<details class='card'><summary style='cursor:pointer;"
                       f"color:var(--mut)'>Internal &amp; other mail "
                       f"({len(aside)}) — not customer work</summary>"
                       f"{items}</details>")
    if chatter:
        items = "".join(
            f"<div>💬 <a href='/bid/{b['stamp']}'>{esc(b['from'])[:44]}</a> "
            f"<span style='color:var(--mut)'>&ldquo;{esc((b.get('newest_message') or '')[:60])}"
            f"&rdquo;</span></div>"
            for b, why in chatter)
        aside_html += (f"<details class='card'><summary style='cursor:"
                       f"pointer;color:var(--mut)'>Conversations ({len(chatter)}) "
                       f"— customers replying in office threads, no action "
                       f"needed</summary>{items}</details>")

    reviews = load_reviews()[-8:][::-1]
    rev_rows = "".join(
        f"<div>✅ {esc(r.get('action'))} — {esc(r.get('customer', r.get('stamp')))}"
        f"{(' · ' + esc(r['reason'])) if r.get('reason') else ''}"
        f"{(' <span style=color:var(--mut)>· by ' + esc(r['by']) + '</span>') if r.get('by') else ''}</div>"
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
        "<div style='color:var(--mut)'>Jobber calendar — future phase. Days fill "
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


def other_homes_card(b):
    """MULTI-HOME CLIENTS (Martha + LaRee's questionnaire): same person,
    different addresses — link every property so notes stay per-home."""
    m = re.search(r"<([^>]+)>", b.get("from") or "")
    email = (m.group(1).lower() if m else None)
    if not email or not b.get("address"):
        return ""
    mine = _canon_addr(b["address"])
    others = {}
    for s, r in _shadow_source():
        rm = re.search(r"<([^>]+)>", r.get("from") or "")
        if rm and rm.group(1).lower() == email and r.get("address") \
                and _canon_addr(r["address"]) != mine:
            others[_canon_addr(r["address"])] = r["address"]
    if not others:
        return ""
    links = " · ".join(
        f"<a href='/property/{_slug(a)}'>{esc(a)[:40]}</a>"
        for a in others.values())
    return (f"<div style='margin-top:6px;display:inline-block;"
            f"background:#fdf4dd;color:#7a5300;border-radius:999px;"
            f"padding:4px 13px;font-size:12.5px;font-weight:700'>"
            f"🏘 Same customer, other home(s): {links} — notes stay "
            f"per-home</div>")


def bid_page(stamp, user=None, draft=""):
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
        colors = {"customer": "transparent", "aerial": "#0b6e4f",
                  "street": "#1a5276"}
        for ref, kind, idx in clouddb.photos_index(
                _photo_refs(stamp, b.get("address"))):
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
            # REPLY WITHOUT LEAVING THE BID (Martha, Jul 9): quick
            # responses + ✨ draft + the same locked Send as Messages.
            last_subject = next((m_.get("subject") for m_ in reversed(th)
                                 if m_.get("subject")), "") or b.get("subject") or ""
            reply_subject = (last_subject if last_subject.lower()
                             .startswith("re:") else f"Re: {last_subject}"
                             if last_subject else "Master Butler")
            _cn_json = _canned_payload()
            reply_ui = f"""
  <div style='border-top:1px solid var(--line);margin-top:10px;
       padding-top:10px'>
   <div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px'>
    <form method='POST' action='/msg_draft' style='display:inline'>
     <input type='hidden' name='to' value='{esc(cust_email)}'>
     <input type='hidden' name='back' value='bid:{stamp}'>
     <button class='gray' style='border-color:var(--gold);color:var(--goldink)'>
      ✨ Draft a reply for me</button>
    </form>
    <select id='bidcanned' style='max-width:280px'>
     <option value=''>Quick responses…</option></select>
   </div>
   <form method='POST' action='/msg_send'>
    <input type='hidden' name='to' value='{esc(cust_email)}'>
    <input type='hidden' name='subject' value='{esc(reply_subject)}'>
    <input type='hidden' name='back' value='/bid/{stamp}'>
    <textarea id='bidreply' name='body' rows='3' style='min-height:76px'
     placeholder='Reply to {esc(cust_email)}'>{esc(draft)}</textarea>
    <div style='display:flex;justify-content:space-between;
                align-items:center;margin-top:6px'>
     <span class='subtext'>{"Sends as customercare@ · your edits teach "
                            "the brain" if REPLIES_ENABLED else
                            "Sending stays locked until Dallon flips "
                            "it on."}</span>
     {f"<button class='big' onclick=\"return confirm('Send this reply "
      f"to {esc(cust_email)}?')\">Send reply</button>"
      if REPLIES_ENABLED else
      "<button class='big' type='button' onclick=\"alert('Sending is "
      "switched OFF while we test — copy the text into Gmail for "
      "now.')\">Send reply</button>"}
    </div>
   </form></div>
<script>
{_CANNED_MERGE_JS}
var BC = mergeCanned({_cn_json});
var _bs = document.getElementById('bidcanned');
Object.keys(BC).forEach(function(k){{
  var o = document.createElement('option'); o.value = k; o.textContent = k;
  _bs.appendChild(o);
}});
_bs.onchange = function(){{
  if (!_bs.value) return;
  var t = document.getElementById('bidreply');
  t.value = BC[_bs.value]; t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight + 6, 420) + 'px'; t.focus();
}};
</script>"""
            convo_card = (
                f"<div class='card'><h3 style='margin-top:0'>Conversation "
                f"&amp; reply <a style='font-weight:400;font-size:12px' "
                f"href='/messages?t={urllib.parse.quote(cust_email)}'>"
                f"full thread →</a></h3>"
                + (bubbles or "<div class='subtext'>No messages logged "
                              "yet — replying starts the thread.</div>")
                + reply_ui + "</div>")
    except Exception:
        convo_card = ""

    # MORE VIEWS (Jul 9, Dallon: "google isn't enough sometimes"):
    # 3D flyover (every side of the house) + one-click listing lookups
    more_views = ""
    if b.get("address"):
        try:
            from aerial_view import listing_links
            links = "".join(
                f"<a href='{esc(u)}' target='_blank' rel='noopener' "
                f"class='chip' style='text-decoration:none'>🏠 {n} ↗</a> "
                for n, u in listing_links(b["address"]))
        except Exception:
            links = ""
        more_views = (
            f"<div style='margin-top:8px'>"
            f"<a href='/flyover?addr={urllib.parse.quote(b['address'])}' "
            f"target='_blank' class='chip' style='text-decoration:none;"
            f"background:#e5edff;color:#1d4ed8;font-weight:700'>"
            f"🎥 3D flyover — every side of the house</a> {links}</div>")

    gallery_card = (f"<div class='card'><h3 style='margin-top:0'>Photos it "
                    f"used {'(green = aerial, blue = street)' if has_imagery else ''}</h3>"
                    f"{gallery or '<div style=color:var(--mut)>No photos on this '
                    'request — the photo-request button drafts the ask.</div>'}"
                    f"{more_views}</div>")

    notes = re.findall(r"⚠ ?(.+)", b.get("pipeline_output", ""))
    if b.get("office_alert"):
        notes.insert(0, b["office_alert"])
    notes_html = "".join(
        f"<div style='display:flex;gap:8px;align-items:flex-start;"
        f"color:var(--goldink);padding:4px 0'><span>⚠</span>"
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
    m2 = re.search(r"DRY-DAY OPTION[^:]*: roof lane \$(\d+)[^$]*\$(\d+)",
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
    # LOW-CONFIDENCE RANGE (Dallon Jul 9): below 50% sure, a single
    # number over-promises — show the honest band so the office reacts
    # to a range and the calibration ledger learns from where they land.
    range_html = ""
    if conf is not None and conf < 50 and (d.get("total") or 0) > 0:
        from bid_engine import round_to_5 as _r5
        lo, hi = _r5(d["total"] * 0.8), _r5(d["total"] * 1.2)
        range_html = (f"<div style='font-size:13px;font-weight:700;"
                      f"color:#c77700'>likely ${lo:,.0f}–${hi:,.0f} "
                      f"<span style='font-weight:400;color:var(--mut)'>"
                      f"(below 50% sure — treat the number as a middle "
                      f"guess)</span></div>")
    draft_headline = ""
    if d.get("total") is not None:
        draft_headline = (
            "<div class='headline'><div>"
            "<div style='font-size:11px;color:var(--mut);text-transform:"
            "uppercase;letter-spacing:.7px;font-weight:600'>Total quote</div>"
            f"<span class='total'>${d['total']:,.0f}</span>{range_html}</div>"
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
        editable = not b.get("reviewed") and not b.get("dns_match")
        lines = ""
        for i, s in enumerate(bid_d["services"]):
            past = hist.get(_service_key(s["name"]) or "") or []
            recent = sorted(past, reverse=True)[:3]
            cells = " · ".join(f"{dt[:7]} ${p:,.0f}" for dt, p in recent)
            hint = ""
            if recent and s["price"] < recent[0][1]:
                hint = (" <b style='color:#b03a2e'>⬆ below last paid "
                        f"(${recent[0][1]:,.0f})</b>")
            was = (f"<div class='subtext' style='font-size:10.5px'>system "
                   f"said ${s['orig_price']:,.0f}</div>"
                   if s.get("orig_price") not in (None, s["price"]) else "")
            price_cell = (
                f"<td class='num'>$<input type='number' name='p_{i}' "
                f"value='{s['price']:g}' step='any' min='0' "
                f"style='width:78px;text-align:right;font-weight:700;"
                f"border:1px solid var(--line);border-radius:6px;"
                f"padding:4px'>{was}</td>"
                if editable else f"<td class='num'>${s['price']:,.0f}</td>")
            lines += (f"<tr><td>{esc(s['name'])}</td>{price_cell}"
                      f"<td class='subtext'>{cells or '—'}{hint}</td></tr>")
        # WHY chips ride WITH the price edit — one tap teaches the system
        reason_chips = "".join(
            f"<button type='button' class='reason' "
            f"onclick=\"document.getElementById('editreason').value='{r}';"
            f"document.querySelectorAll('#pricecard .reason').forEach("
            f"x=>x.classList.remove('sel'));this.classList.add('sel')\">"
            f"{r.replace('_', ' ')}</button>" for r in REASONS)
        edit_controls = (f"""
  <div style='margin-top:8px;border-top:1px dashed var(--line);
       padding-top:8px'>
   <div class='subtext' style='margin-bottom:4px'>Changed a number?
   Tap why (teaches the system), then save:</div>
   <input type='hidden' id='editreason' name='reason' value=''>
   <div style='margin-bottom:6px'>{reason_chips}</div>
   <button class='gray' style='font-weight:700'>💾 Save my prices</button>
  </div>""" if editable else "")
        price_card = (
            f"<form method='POST' action='/edit_prices' id='pricecard'>"
            f"<input type='hidden' name='stamp' value='{stamp}'>"
            f"<input type='hidden' name='customer' value='{esc(b['from'])}'>"
            "<div class='card'><h3>Line items — "
            + ("type right on a price to fix it"
               if editable else "as decided") + "</h3><table>"
            "<tr><th>Service</th><th class='num'>Price</th>"
            "<th>Past at this property</th></tr>" + lines +
            f"<tr style='background:#f3f4f1'><td><b>Total estimate</b></td>"
            f"<td class='num'><b>${d.get('total', 0):,.0f}</b></td>"
            "<td></td></tr>"
            "</table>" + edit_controls + "</div></form>")
    price_card += add_service_card(b)
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
    <span class='subtext'> · {esc(cust_email or '')}</span>
    {(f" <a href='{esc(b['jobber_client_url'])}' target='_blank' "
      f"rel='noopener' class='chip win' style='text-decoration:none'>"
      f"👤 Jobber profile ↗</a>") if b.get('jobber_client_url') else ''}
   </div>
   {(f"<div style='margin-top:6px;display:inline-block;background:#e5edff;"
     f"color:#1d4ed8;border-radius:999px;padding:4px 13px;font-size:12.5px;"
     f"font-weight:700'>📅 Asked about timing: &ldquo;{esc(b['sched_pref'])}"
     f"&rdquo;</div>") if b.get('sched_pref') else ''}
   {(f"<div style='margin-top:6px;display:inline-block;background:#f0e9fd;"
     f"color:#6d28d9;border-radius:999px;padding:4px 13px;font-size:12.5px;"
     f"font-weight:700'>👷 Mentions a tech: &ldquo;{esc(b['tech_request'])}"
     f"&rdquo; — book them if possible</div>")
    if b.get('tech_request') else ''}
   {other_homes_card(b)}
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
   "<div style='color:var(--mut);font-size:13px;margin-top:8px'>Must Know "
   "needs an address on the request — none was parsed here.</div>"}</div>
 <details class='card'><summary>Raw system output (full trace)</summary>
  <pre>{esc(b.get('pipeline_output') or '(no draft — ' +
             esc(b.get('kind')) + ')')}</pre></details>
</div><div style='position:sticky;top:70px;max-height:calc(100vh - 84px);
     overflow-y:auto;border-radius:16px'>
 <div class='card'><h3 style='margin-top:0'>Decide — 3 choices</h3>
  <div class='subtext' style='margin-bottom:8px'>Wrong number? Fix it
  right on the line items (left) and tap 💾 first.</div>
  <form method='POST' action='/review'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <button name='action' value='approve' class='big' style='width:100%'>
    ✓ Price is right — approve</button>
   <div class='subtext' style='margin:3px 0 0'>
    {"Creates the DRAFT quote in Jobber (photos attach to the client profile). Nothing emails the customer." if _push_enabled() or stamp in _blob_rw("push_allow", []) else "Records your OK. (Jobber push is off for this bid — shadow mode.)"}
   </div>
  </form>
  {duplicate_forms}
  <details style='margin-top:12px'>
   <summary style='cursor:pointer;font-weight:700'>⏸ Not now — park it
    (comes back by itself)</summary>
  <form method='POST' action='/hold' style='margin-top:6px'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(b['from'])}'>
   <select name='hold_reason' style='padding:7px;border-radius:6px'>
    {''.join(f"<option value='{r}'>{r.replace('_', ' ')}</option>"
             for r in HOLD_REASONS)}
   </select>
   until <input type='date' name='hold_until' id='holddate'
                style='padding:6px;border-radius:6px'>
   <div style='margin-top:4px'>
    <button type='button' class='gray' style='padding:3px 10px;font-size:11.5px'
     onclick="qh(7)">+1 week</button>
    <button type='button' class='gray' style='padding:3px 10px;font-size:11.5px'
     onclick="qh(14)">+2 weeks</button>
    <button type='button' class='gray' style='padding:3px 10px;font-size:11.5px'
     onclick="qh('aug')">Dry season (Aug 1)</button>
   </div>
   <button class='gray' style='margin-top:6px'>⏸ Park it</button>
   <script>
   function qh(d){{
     var t = new Date();
     if (d === 'aug') {{
       t = new Date(t.getFullYear() + (t.getMonth() >= 7 ? 1 : 0), 7, 1);
     }} else {{ t.setDate(t.getDate() + d); }}
     document.getElementById('holddate').value = t.toISOString().slice(0,10);
   }}
   </script>
   <div style='font-size:12px;color:var(--mut)'>Parking hides the WORK until
   the date — still answer the customer with the timeline.</div>
  </form>
  </details>
  <details style='margin-top:8px'>
   <summary style='cursor:pointer;font-weight:700'>🙋 Not sure — ask
    for help</summary>
   <form method='POST' action='/escalate' style='margin-top:8px'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='customer' value='{esc(b['from'])}'>
    <input type='hidden' name='address' value='{esc(b.get('address'))}'>
    <input type='text' name='question' placeholder='your question, one line'>
    <button style='background:#6d28d9'>🔍 Ask the office — stays on the
     queue with your question</button>
   </form>
   <form method='POST' action='/flag_review' style='margin-top:6px'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='customer' value='{esc(b['from'])}'>
    <input type='hidden' name='total' value='{d.get('total') or ''}'>
    <button style='background:var(--gold);color:#1c2b23'>🚩 Still stuck —
     email Dallon &amp; Tom</button>
   </form>
  </details>
  <details style='margin-top:8px;border-top:1px solid var(--line);
    padding-top:8px'>
   <summary style='cursor:pointer;color:var(--mut);font-size:12.5px;
    font-weight:700'>More actions (photos · spam · welcome-back)</summary>
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
 <details class='card' ontoggle="if(this.open&&!this.dataset.c){{this.dataset.c=1;
  fetch('/fold_click',{{method:'POST',headers:{{'Content-Type':
  'application/x-www-form-urlencoded'}},body:'name=similar_homes'}});}}">
  <summary style='cursor:pointer;font-weight:700;
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
                     f"</h3><div style='color:var(--mut);font-size:13px'>{hint}"
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
            f"{'background:var(--soft);color:var(--ink)' if inbound else 'background:#0b3d2e;color:#eef4f0'}'>"
            f"<div style='font-size:10px;font-weight:700;opacity:.65;"
            f"margin-bottom:3px'>"
            f"{esc(m.get('name') or '') if inbound else 'Master Butler' + (' · ' + esc(m['by']) if m.get('by') else '')}"
            f" · {esc(_pt(m['at']))}</div>"
            f"<div style='white-space:pre-wrap;font-size:13.5px'>"
            f"{esc(msglog.clean_body(m.get('body') or '') or m.get('subject') or '')}</div>"
            + (f"<div style='margin-top:4px'><a href='/bid/{m['stamp']}' "
               f"style='font-size:11px;color:{'#177245' if inbound else '#c9a227'}'>"
               f"open the bid →</a></div>" if m.get("stamp") else "")
            + "</div></div>")
    canned_json = _canned_payload()
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
{_CANNED_MERGE_JS}
var CANNED = mergeCanned({canned_json});
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



def _pt(iso):
    """Display any stored timestamp in PACIFIC (Dallon: 'timestamps are
    out of whack') — storage stays UTC/original for honest sorting."""
    try:
        from datetime import datetime as _d, timezone as _z
        from zoneinfo import ZoneInfo
        t = _d.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=_z.utc)
        return t.astimezone(ZoneInfo("America/Los_Angeles")) \
                .strftime("%b %d, %-I:%M %p")
    except Exception:
        return (iso or "")[:16].replace("T", " ")


def _bid_email(b):
    e = ((b.get("draft") or {}).get("customer") or {}).get("email")
    if not e:
        m = re.search(r"<([^>]+)>", b.get("from") or "")
        e = m.group(1) if m else None
    return (e or "").strip().lower() or None


_ALIAS_CACHE = {"at": 0.0, "v": {}}


def _canon_email(e):
    """Resolve an email to the customer's PRIMARY address so someone
    who wrote from two accounts groups as ONE card (Dallon, Jul 13:
    Natallie Buxton). Aliases come from customer_dedup — only confident
    same-person matches, so it never fuses two different people."""
    if not e:
        return e
    import time as _t
    if _t.time() - _ALIAS_CACHE["at"] > 60:
        try:
            _ALIAS_CACHE["v"] = _blob_rw("customer_aliases", {})
        except Exception:
            _ALIAS_CACHE["v"] = {}
        _ALIAS_CACHE["at"] = _t.time()
    return _ALIAS_CACHE["v"].get(e, e)


def _stamp_utc(stamp):
    """Bid stamp (local time) -> UTC iso, comparable with msglog times."""
    from datetime import timezone
    try:
        return (datetime.strptime(stamp, "%Y%m%d-%H%M%S").astimezone()
                .astimezone(timezone.utc).isoformat(timespec="seconds"))
    except ValueError:
        return ""


def _latest_msg_utc(msgs):
    """Latest message time in a thread as a UTC iso string, by REAL
    parsed time (msglog carries each message's OWN Date header). Two
    reasons this, not the record's stamp, is the truth for ordering:
      · the stamp is only WHEN WE POLLED the email — a re-sweep
        re-stamps a record and rockets it to the top of the queue even
        though the customer wrote hours earlier. That was the Gmail
        desync (Dallon, Jul 13: Anna Tang stamped 10:20 but emailed
        9:55 sat ABOVE Jeffrey who emailed 10:46).
      · customers' Date offsets differ (Eastern '-04:00' vs Pacific
        '-07:00'), so a naive STRING max picks the wrong message; we
        parse to real UTC and take the true max.
    Returns '' when no message carries a parseable time (voicemails,
    Jobber leads) — the caller then falls back to the stamp."""
    from datetime import datetime as _d, timezone as _z
    latest = None
    for _m in msgs or []:
        try:
            _t = _d.fromisoformat(_m["at"])
        except Exception:
            continue
        if _t.tzinfo is None:
            _t = _t.replace(tzinfo=_z.utc)
        if latest is None or _t > latest:
            latest = _t
    return (latest.astimezone(_z.utc).isoformat(timespec="seconds")
            if latest else "")


def _latest_in_utc(msgs):
    """Latest INBOUND message time (UTC iso) — when the CUSTOMER last
    contacted us. The queue orders and ages by THIS, not by the latest
    message overall, so our own outbound replies never reorder a
    customer or bump them to the top (Dallon, Jul 13: LaRee answered Dan
    Delorey's Jul 11 question today and it shot him to the top, breaking
    the flow — a reply going OUT is us handling it, not new activity
    coming IN). Returns '' when the thread has no parseable inbound."""
    from datetime import datetime as _d, timezone as _z
    latest = None
    for _m in msgs or []:
        if _m.get("dir") != "in":
            continue
        try:
            _t = _d.fromisoformat(_m["at"])
        except Exception:
            continue
        if _t.tzinfo is None:
            _t = _t.replace(tzinfo=_z.utc)
        if latest is None or _t > latest:
            latest = _t
    return (latest.astimezone(_z.utc).isoformat(timespec="seconds")
            if latest else "")


_SVC_KEYWORDS = {
    "gutter": ("gutter",),
    "roof": ("roof", "blow off", "blow-off"),
    "moss": ("moss",),
    "window": ("window",),
    "pw": ("pressure wash", "house wash", "wash", "driveway", "patio",
           "sidewalk", "walkway", "pathway", "deck", "concrete", "paver",
           "curb"),
    "dryer": ("dryer",),
    "light": ("light", "trimlight", "gemstone", "govee"),
}


def _svc_category(token):
    """A parser service token → coarse category for old-quote matching."""
    t = (token or "").lower()
    if t.startswith("pw_") or "wash" in t:
        return "pw"
    if "gutter" in t:
        return "gutter"
    if "roof" in t:
        return "roof"
    if "moss" in t:
        return "moss"
    if "window" in t:
        return "window"
    if "dryer" in t:
        return "dryer"
    if "light" in t:
        return "light"
    return None


def _quote_service_mismatch(oq, req_services):
    """Is a customer's existing quote for a DIFFERENT service than what
    they're asking for NOW? (Jeff Hill, Jul 13: a Holiday-Lights quote
    flagged against a new pressure-washing request.) A different-service
    quote is unrelated to this request REGARDLESS of age. Returns a
    plain-English label for the old quote's service when it clearly does
    NOT overlap the new request; '' when it overlaps or we can't tell."""
    cats = {c for c in (_svc_category(s) for s in (req_services or [])) if c}
    lines = (oq or {}).get("lines") or []
    if not cats or not lines:
        return ""                       # can't compare → don't guess
    text = " ".join((li.get("name") or "").lower() for li in lines)
    for cat in cats:
        if any(kw in text for kw in _SVC_KEYWORDS.get(cat, ())):
            return ""                   # overlap → same job, not a mismatch
    # no overlap — name the old quote from its first real line item
    for li in lines:
        nm = (li.get("name") or "").strip()
        low = nm.lower()
        if nm and not any(w in low for w in
                          ("discount", "tax", "product", "material", "fee")):
            return re.split(r"\s*[:\-]", nm)[0].strip()[:28]
    return "a different service"


def _office_drafting(oq, stamp):
    """True when the OFFICE has a live DRAFT quote in Jobber for this
    record — i.e. they're building the quote there RIGHT NOW, so our
    dashboard should stop nagging for a price and must never fire a
    second quote over theirs (Dallon, Jul 13: Kate Murray / Jessica
    Jensen sat in our Drafts with a total to approve while the office
    already had a Jobber draft). 'Draft' status + created within ~45
    days of the record (a months-old draft is stale history, not
    active work). Returns the quote number, or None."""
    oq = oq or {}
    if (oq.get("status") or "").lower() != "draft":
        return None
    created = oq.get("created") or ""
    if not (created and len(stamp) >= 8 and stamp[:8].isdigit()):
        return None
    try:
        from datetime import date as _date
        dq = _date.fromisoformat(created)
        dr = _date.fromisoformat(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}")
        if -3 <= (dr - dq).days <= 45:
            return oq.get("number")
    except ValueError:
        pass
    return None


def _primary_bid(bids):
    """The record that should be a card's FACE. Normally the newest, but
    a customer often fires off several emails about ONE request in a
    burst — a website form, then a photo, then 'I still need it' — and
    the NEWEST can be a photo-only or empty follow-up that carries no
    request. Crowning that as the headline made the card read 'no info'
    while the real request hid a row below (Natallie Buxton, Jul 13). So
    the face is the newest record that actually CARRIES a request: a
    priced draft, parsed services, an address, or a real message body. A
    hollow record (nothing parsed, empty body) never becomes the face —
    its photos still surface through the card's pooled gallery. Falls
    back to the plain newest when every record is hollow."""
    if not bids:
        return None
    ordered = sorted(bids, key=lambda b: b.get("stamp") or "")

    def hollow(b):
        d = (b.get("draft") or {}).get("bid") or {}
        return not (d.get("services") or b.get("services")
                    or b.get("address")
                    or (b.get("newest_message") or "").strip())

    substantive = [b for b in ordered if not hollow(b)]
    return (substantive or ordered)[-1]


def _all_photo_refs(c):
    """Photo refs pooled across ALL of a customer's records — so a photo
    that arrived on a SEPARATE email (a photo-only follow-up) shows on
    the combined card, not only the face record's own photos."""
    refs = []
    for b in (c.get("bids") or []):
        for r in _photo_refs(b.get("stamp"), b.get("address")):
            if r not in refs:
                refs.append(r)
    return refs


SLA_WORD = {"dns": "do not service", "hold": "parked",
            "flag": "with Dallon & Tom", "sl": "question for office",
            "won": "won ✓", "sent": "quote sent", "ok": "approved"}


def _sounds_urgent(text):
    """Customer-worry language that must float to the very top (Jessica,
    Jul 9). Returns the matched phrase (shown to the office) or None.

    'damaged/broke' are only urgent as a COMPLAINT ABOUT US — never when
    the customer is asking us to repair something already damaged
    (Dallon, Jul 13: Kenneth Pai wants a quote to replace a vent 'that
    was damaged' — a service request, not 'you damaged my property')."""
    t = (text or "").lower()
    # unambiguous urgency — these are almost never a service request
    for p in ("urgent", "asap", "as soon as possible", "emergency",
              "no show", "no-show", "didn't show", "did not show",
              "hasn't shown", "never showed", "not here yet",
              "still waiting", "leak", "leaking", "upset", "frustrated",
              "disappointed", "unacceptable", "refund", "complaint",
              "wrong house", "made a mess", "left a mess"):
        if p in t:
            return p
    # physical-damage words: urgent ONLY when they blame us. "your tech
    # damaged", "damaged my deck", "you broke" → complaint. "can you
    # fix / quote to replace / help me repair … damaged" → a job.
    if re.search(r"\b(damaged?|broke|broken|ruined|destroyed)\b", t):
        blame = re.search(
            r"(you|your|the tech|technician|the crew|the guys?|"
            r"the company|master ?butler)[^.!?]{0,45}"
            r"(damaged?|broke|broken|ruined|destroyed|mess)"
            r"|(damaged?|broke|broken|ruined|destroyed)[^.!?]{0,45}"
            r"(my|our)\s+(deck|driveway|yard|roof|property|home|house|"
            r"gutter|window|lawn|car|fence|patio)", t)
        request = re.search(
            r"\b(quote|estimate|help me|can you|could you|would like|"
            r"want (to|a)|replace|repair|fix|redo|install|get (a|an))\b",
            t)
        if blame and not request:
            return "possible damage complaint"
    return None


def _status_word(nb, holds, flags_open, sbs, claims):
    """The quiet one-word status for an Inbox row (no pill zoo)."""
    if not nb:
        return "reply", ""
    s = nb["stamp"]
    if nb.get("dns_match"):
        return "do not service", "color:var(--alarm);font-weight:800"
    if nb.get("tech_sender"):         # field traffic, never a bid
        return "👷 tech note", "color:#6d28d9;font-weight:700"
    if s in holds:
        return "parked", ""
    if s in flags_open:
        return "with Dallon & Tom", ""
    cl = claims.get(s)
    if cl and cl["mins"] <= CLAIM_FRESH_MIN:
        return f"working · {cl['by']}", "color:#1d4ed8"
    js = (sbs.get(s) or "").lower()
    if js in ("approved", "converted"):
        return "won ✓", "color:var(--green2)"
    if js == "awaiting_response":
        return "quote sent", ""
    if s in getattr(bid_status, "_sl", {}):
        return "question for office", "color:#6d28d9"
    if nb.get("reviewed") or js in ("draft", "archived"):
        return "done", ""
    if nb.get("kind") == "phone_lead":
        return "listen / review", "color:var(--green2);font-weight:800"
    c = _num(nb.get("confidence"))       # str/garbage → None, never crash
    if c is not None and c >= 75 and (_num((nb.get("draft") or {})
                                           .get("total")) or 0) > 0:
        return "ready to approve", "color:var(--green2);font-weight:800"
    return "review", "color:var(--green2)"


def inbox_page(sel=None, draft="", user=None, pushed=None):
    """THE INBOX (Dallon picked direction A, Jul 9): bids + messages,
    one list, read/unread shared office-wide, pinned critical info,
    big folds. Scoreboard/Win-back/Settings live top-right."""
    import msglog
    bids = load_bids()
    live_holds, resurfaced = active_holds()
    quotes = quote_numbers()
    qurls = quote_urls()
    claims = _claims()
    flags_open = {f.get("stamp") for f in flagged_for_review()}
    sbs = scoreboard_status()
    bid_status._sl = second_looks()
    read_marks = _msg_read()
    # HANDLED IN JOBBER (Dallon's ruling: read-both, no write-back) —
    # rows provably handled (booked today / recent quote out / won)
    # move to their own lane with the reason shown
    handled_jb = {}
    try:
        import jobber_sync
        handled_jb = jobber_sync.reconcile(
            [(b["stamp"], b) for b in bids])
    except Exception:
        pass

    # ── merge: one entry per customer (same engine as Customers view) ──
    cust, order = {}, []

    def entry(key):
        if key not in cust:
            cust[key] = {"name": "", "email": None, "bids": [],
                         "msgs": [], "vm": None}
            order.append(key)
        return cust[key]

    # human lane placements (Move ▾) — loaded once; rule 1 (a newer
    # customer message) releases them at render time
    manual_lanes = _blob_rw("manual_lanes", {})
    # visible handoffs (🚶 stepped away) — shown until re-claimed or 24h
    handoffs = _blob_rw("handoffs", {})
    # STICKY 'Clear all' set (Dallon, Jul 13 done-feel): keys the office
    # zeroed out stay cleared — the walk-away net won't creep them back —
    # until a NEW inbound message (last_at beats the cleared time).
    cleared_blob = _blob_rw("cleared", {})
    # per-customer flags (bad payer / watch / VIP) — Garrett Mydland
    cust_flags = _blob_rw("customer_flags", {})
    # LaRee's Gmail signals (Jul 14 call): trash = DONE, greyed (read,
    # still in inbox) = being worked. Display-only — a chip and a sink
    # to the bottom of the group, never a cleared row (Jul-13 kill
    # switch stands). A new inbound message always outranks it.
    gmail_state = _blob_rw("gmail_state", {})
    # 🏜 STANDBY HOMES LIVE IN TOM'S FOLD, NOT THE INBOX (Dallon,
    # Jul 15: 'michelle yelle is in inbox AND in the tom only') —
    # a home waiting on Tom's dry window isn't office to-do. It leaves
    # the working lanes; a new customer message brings it right back.
    tom_standby_at = {}
    for c2 in (_blob_rw("tom_standby", {}) or {}).get("customers") or []:
        _e2 = (c2.get("email") or "").lower()
        _s2 = c2.get("stamp") or ""
        if _e2 and re.match(r"\d{8}-\d{6}$", _s2):
            tom_standby_at[_e2] = (f"{_s2[:4]}-{_s2[4:6]}-{_s2[6:8]}"
                                   f"T{_s2[9:11]}:{_s2[11:13]}:{_s2[13:15]}")

    for b in bids:
        if b.get("merged_into") or classify_row(b)[0] == "aside":
            continue
        e = _canon_email(_bid_email(b))
        if b.get("kind") == "jobber_event" and (not e or "getjobber" in e):
            c = entry("stamp:" + b["stamp"])
            # a dressed event knows its PERSON — the subject line is
            # the fallback, never the name (Jessica Lundeen showed as
            # 'Quote #34293 is approved!', Jul 10 pm)
            _disp = (b.get("from") or "").split("<")[0].strip()
            c["name"] = (_disp if _disp and _disp.lower() != "jobber"
                         else (b.get("subject") or "Jobber event")[:40])
            c["bids"].append(b)
            continue
        lead = b.get("lead") or {}
        if lead or "copycall" in (e or ""):
            # ONE ENTRY PER CALL (Dallon's catch: every voicemail merged
            # into one 'messages@copycall' person, wearing the wrong
            # number) — key by caller+time so double-processed
            # notifications collapse too
            vkey = ("vm:" + (lead.get("caller") or b.get("phone") or "?")
                    + "|" + (lead.get("when") or b["stamp"]))
            c = entry(vkey)
            dur = lead.get("duration") or "?"
            # A PERSON, not 'voicemail' (Jessica, Jul 9): when Jobber
            # knows the number, the entry wears the caller's name
            _cid = b.get("caller_id") or {}
            c["name"] = ((f"☎ {_cid['name']}" if _cid.get("name") else
                          f"☎ Voicemail · "
                          f"{lead.get('caller') or b.get('phone') or 'unknown'}"))[:38]
            c["vm"] = {"dur": dur, "when": lead.get("when"),
                       "caller": lead.get("caller") or b.get("phone")}
            c["bids"].append(b)
            continue
        key = e or ("stamp:" + b["stamp"])
        c = entry(key)
        c["email"] = c["email"] or e
        nm = (b.get("from") or "").split("<")[0].strip()
        if nm and nm.lower() not in ("none", "none none") and not c["name"]:
            c["name"] = nm
        c["bids"].append(b)
    import spam_filter
    _skip = (list(_learned_spam())
             + list(NOISE_SENDERS) + list(spam_filter.KNOWN_SPAM_DOMAINS))
    _internal = list(_internal_senders())
    for addr, name, msgs in msglog.threads():
        # INTERNAL mail (Dallon/Tom/the company) used to be skipped
        # entirely — but Dallon's emails ARE the office's questions back
        # to him (pricing, bid questions), and LaRee needs them SEEN
        # (Jul 13 call). They ride the Techs lane: internal, never a bid.
        is_internal = any(s and s in addr for s in _internal)
        if addr not in cust and not is_internal \
                and (any(s and s in addr for s in _skip)
                     or spam_filter.looks_spam(
                addr, msgs[-1].get("subject"), msgs[-1].get("body"))[0]):
            continue
        c = entry(_canon_email(addr))
        c["email"] = c["email"] or addr
        if name and name != addr and not c["name"]:
            c["name"] = name
        c["msgs"] = msgs
        if is_internal:
            c["internal"] = True

    roster = []
    for key in order:
        c = cust[key]
        c["bids"].sort(key=lambda b: b["stamp"])
        nb = _primary_bid(c["bids"])     # newest record that CARRIES a
        # request — not an empty photo-only follow-up (Natallie, Jul 13)
        # ORDER BY THE EMAIL'S OWN TIME, exactly like Gmail — NOT the
        # record's poll stamp (which a re-sweep rewrites, scrambling the
        # queue against Gmail; Dallon, Jul 13). Fall back to the stamp
        # only for rows that never came through Gmail (voicemails,
        # Jobber leads) and so carry no real message time.
        _msg_latest = _latest_msg_utc(c["msgs"])
        _msg_in = _latest_in_utc(c["msgs"])
        # POSITION + AGE by the customer's LAST INBOUND, not the latest
        # message — our outbound reply must not bump them up (Dan
        # Delorey, Jul 13). Fall back to any message, then the stamp.
        last_at = _msg_in or _msg_latest or (_stamp_utc(nb["stamp"])
                                             if nb else "")
        # ANSWERED = the newest message in the thread is OURS (we replied
        # after their last note). Such a row is handled — it must not
        # bold or scream for attention. A later inbound flips this back.
        answered = bool(_msg_latest and _msg_in and _msg_latest > _msg_in)
        unread = (last_at > read_marks.get(key, "")) and not answered
        # ACKNOWLEDGED = someone explicitly marked it seen and nothing
        # newer has come in since. The walk-away net below may still
        # re-bold it for attention, but an acknowledged item must NOT
        # scream 'urgent' again (Charlotte Hingle, Jul 10: a voicemail
        # opening with the word 'Urgent' rocketed back to the top every
        # 30 min after being marked done). A genuinely NEW message
        # clears this — last_at then beats the mark, so unread is True.
        acknowledged = bool(read_marks.get(key)) and not unread
        word, wstyle = _status_word(nb, live_holds, flags_open, sbs, claims)
        needs = (nb and not nb["reviewed"] and not sbs.get(nb["stamp"])
                 and nb["stamp"] not in live_holds
                 and nb["stamp"] not in flags_open)
        # WALK-AWAY NET (Dallon's concern, Jul 9): opened but still
        # undecided after 30 min -> it re-bolds itself for everyone.
        # Nobody has to remember to mark-unread after stepping away.
        # EXCEPT a row the office explicitly 'Clear all'-ed: that stays
        # cleared (done-feel) until a new inbound beats the cleared time.
        _clr = cleared_blob.get(key)
        _sticky_cleared = bool(_clr and not (last_at and last_at > _clr))
        if not unread and needs and read_marks.get(key) \
                and not _sticky_cleared:
            try:
                from datetime import datetime as _d3, timezone as _z3
                rt = _d3.fromisoformat(read_marks[key])
                if rt.tzinfo is None:
                    rt = rt.replace(tzinfo=_z3.utc)
                if (_d3.now(_z3.utc) - rt).total_seconds() > 1800:
                    unread = True
            except ValueError:
                pass
        new_msg = bool(c["msgs"]) and c["msgs"][-1]["dir"] == "in" \
            and c["msgs"][-1]["at"] > read_marks.get(key, "") \
            and not answered      # we replied since → not new (Dan Delorey)
        # the customer's Jobber quote may live on an EARLIER record
        # (the Mia lesson): look across all their bids, newest first
        oq = next((b2.get("open_quote_ctx") for b2 in reversed(c["bids"])
                   if b2.get("open_quote_ctx")), None)
        qno = next((quotes.get(b2["stamp"]) for b2 in reversed(c["bids"])
                    if quotes.get(b2["stamp"])), None)
        won = any((sbs.get(b2["stamp"]) or "").lower() in
                  ("approved", "converted")
                  or (b2.get("jobber_event") or {}).get("event")
                  == "quote_approved" for b2 in c["bids"])
        # STALE QUOTE (Dallon, Jul 13): a returning customer asking for a
        # NEW quote must not inherit a stale old one. If their most recent
        # quote is 3+ months OLDER than their newest inbound message, flag
        # it for REVIEW and treat this as a fresh request — drop the
        # open-quote / won signals so the row behaves as new. Jobber's API
        # has NO quote-archive (verified: 109 mutations, clientArchive &
        # requestArchive exist but no quoteArchive; quoteEdit has no status
        # field) — the office archives the old quote by hand in Jobber, so
        # the note SAYS so. Only fires when they actually wrote recently.
        stale_note = ""
        if oq and (oq.get("created") or ""):
            # (a) different SERVICE than they're asking for now = unrelated,
            #     retire regardless of age (Jeff Hill, Jul 13: a Holiday-
            #     Lights quote flagged against a pressure-washing request).
            _mm = _quote_service_mismatch(oq, (nb.get("services") or [])
                                          if nb else [])
            _agedays = 0
            if _msg_in:
                try:
                    from datetime import date as _sqd, datetime as _sqdt
                    _agedays = ((_sqdt.fromisoformat(_msg_in).date()
                                 - _sqd.fromisoformat(oq["created"])).days)
                except (ValueError, TypeError):
                    _agedays = 0
            if _mm:
                stale_note = (f"🔀 last quote #{oq.get('number')} was for "
                              f"{_mm} — this is a new request")
                oq = None
                qno = None
                won = False
            elif _msg_in and _agedays >= 90:  # (b) 3+ months old → review
                stale_note = (f"⏰ review — last quote #{oq.get('number')} "
                              f"is ~{_agedays // 30} mo old; treat as new "
                              "request & archive it in Jobber")
                oq = None
                qno = None
                won = False
        # TECHS get their own lane ABOVE New (Dallon, Jul 10 pm: 'a
        # tech tab above New with a notification when tech messages
        # come through') — field mail never mixes with customer bids
        if (nb and nb.get("tech_sender")) or c.get("internal"):
            grp = -1                 # field mail OR office↔Dallon internal
        elif nb and nb.get("dns_match"):
            grp = 0
        # HANDLED IN JOBBER (proven: booked today / recent quote / won):
        # its own lane with the reason — unless the customer wrote back
        elif (nb and nb["stamp"] in handled_jb and not new_msg
              and not unread and nb["stamp"] not in live_holds
              and nb["stamp"] not in flags_open):
            grp = 3
        # already-quoted + someone marked it seen = handled → Done & quiet
        # (Dallon, Jul 10: clean up the ones already worked). A WON quote
        # still needs scheduling, and a new customer reply always
        # resurfaces, so both are excluded — nothing real gets buried.
        elif ((oq or qno) and not won and not unread and not new_msg
              and nb and nb["stamp"] not in live_holds
              and nb["stamp"] not in flags_open):
            grp = 4
        # MARKED DONE = OFF THE QUEUE (Dallon, Jul 10 pm: 'they're used
        # to clearing out the gmail till it's empty'). A decision
        # (reviewed) OR an explicit ✓ from the office (acknowledged),
        # with nothing new since → the drawer. ANY new message
        # resurfaces them (the branch below wins) — nobody stays buried.
        elif (nb and (nb.get("reviewed") or acknowledged)
              and not unread and not new_msg
              and nb["stamp"] not in live_holds
              and nb["stamp"] not in flags_open):
            grp = 4
        elif new_msg or needs or (nb and (nb.get("office_alert")
                                  or nb["stamp"] in bid_status._sl)):
            grp = 0                                # new — needs a person
        elif nb and (nb["stamp"] in live_holds
                     or nb["stamp"] in flags_open
                     or claims.get(nb["stamp"])):
            grp = 1                                # in someone's hands
        elif nb and (sbs.get(nb["stamp"]) or "").lower() \
                == "awaiting_response":
            grp = 2                                # waiting on customers
        else:
            grp = 4                                # done / quiet
        if grp == 3 and nb:
            word = "📋 " + handled_jb[nb["stamp"]]
            wstyle = "color:var(--goldink)"
        # AGE FROM THE EMAIL'S OWN TIME too (same reason as last_at): the
        # "16m / 49m" a person reads must be time-since-they-wrote, not
        # time-since-we-polled. Real message time when we have one; the
        # stamp age only for non-Gmail rows (voicemails, Jobber leads).
        _age_basis = _msg_in or _msg_latest
        if _age_basis:
            from datetime import datetime as _dtm, timezone as _tz
            age_h = ((_dtm.now(_tz.utc)
                      - _dtm.fromisoformat(_age_basis)).total_seconds()
                     / 3600)
        else:
            age_h = nb["age_hours"] if nb else 0
        # 🔧 SERVICE FOLLOW-UP (Dallon, Jul 10 pm: Vadim wrote about a
        # screen after his job was DONE — 'label, don't filter'): a
        # customer with a converted/won job writing about the work
        # itself needs a person, not a bid. Stays in New, tagged.
        followup = False
        # CONVERTED only — approved-but-not-done still needs its
        # 'won — schedule it' push; follow-up means the crew already went
        if grp == 0 and nb and (oq or {}).get("status", "") \
                .lower() == "converted":
            # the job is DONE in Jobber and they wrote. But WANTING MORE
            # WORK beats everything (Karen R, Jul 14: Adam finished her
            # walkway that morning, she wrote 'I'd like to go ahead and
            # schedule the driveway' — WITH praise for Adam in the same
            # message. That's a NEW BID, not a fix-it, even though the
            # service category matches the finished job). Only a message
            # with no forward-looking work intent is a follow-up.
            _fu_txt = (nb.get("newest_message") or "").lower()
            _new_work = re.search(
                r"go ahead (and|with)|i('|’)?d like to (schedule|book|"
                r"add|get|do)|schedule (the|a|my|us|me)|can you (also )?"
                r"(add|do|come (out|back) (and|to)|quote)|(another|a|"
                r"new) (quote|estimate)|how much (would|is|for)|"
                r"interested in|next (visit|time) (can|could|please)|"
                r"add (the|just the|a) \w+ to"
                # …and SCHEDULING/APPROVAL replies (the Jul-14 lane
                # read-through: Tammy 'The 15th would work', Angela
                # 'I will take it', Prash 'July 15th is okay. What
                # time?' — money asking to be booked, not fix-its)
                r"|(would|will|that) works?\b|is (fine|good|okay|ok)\b|"
                r"i('|’)?ll take it|i will take it|works for (me|us)|"
                r"what time", _fu_txt)
            followup = not _new_work
        if followup:
            word = "🔧 follow-up on completed work — no bid"
            wstyle = "color:var(--goldink);font-weight:800"
        elif won and grp == 0:
            word, wstyle = ("won — schedule it",
                            "color:var(--green2);font-weight:800")
        elif (oq or qno) and grp == 0 and word == "review":
            word = "has a quote — see it"
        # URGENT-SOUNDING mail floats to the very top (Jessica, Jul 9:
        # 'a customer worried about their tech not showing up on time
        # needs to be brought to the top')
        urgent = None
        if grp == 0 and unread and not acknowledged:
            _lastin = next((m for m in reversed(c["msgs"])
                            if m["dir"] == "in"), None) if c["msgs"] else None
            urgent = _sounds_urgent(
                ((_lastin.get("body") or "") if _lastin else "")
                + " " + ((nb.get("newest_message") or "") if nb else ""))
            if urgent:
                word = f"⚠ urgent — “{urgent}”"
                wstyle = "color:var(--alarm);font-weight:800"
        # ── LANE LADDER (Dallon, Jul 12): first match wins; ambiguity
        # sorts UP to Inbox — the failure mode is 'seen twice', never
        # 'missed'. Order: customer's new message > human placement >
        # Jobber facts > engine draft > quiet timer. ──
        mv = manual_lanes.get(key)
        if mv:
            placed = mv.get("at") or ""
            if (unread or new_msg) and str(last_at) > placed:
                mv = None            # rule 1: their new message wins
            elif mv.get("lane") == "later":
                try:
                    from datetime import (datetime as _dl,
                                          timezone as _tzl,
                                          timedelta as _tdl)
                    if _dl.now(_tzl.utc) > _dl.fromisoformat(
                            placed) + _tdl(days=7):
                        mv = None    # the week is up — resurface
                except Exception:
                    mv = None
        oq_status = ((oq or {}).get("status") or "").lower()
        # Jobber's word beats our guesswork (Dallon, Jul 14 trust fix):
        # a tracked/backfilled quote that's APPROVED counts as won even
        # when the scoreboard never matched our stamp to it.
        if oq_status == "approved":
            won = True
        # the office is drafting THIS in Jobber right now → pull it out
        # of our Inbox/Drafts into its own section (Dallon, Jul 13)
        office_draft_no = _office_drafting(oq, nb["stamp"]) if nb else None
        # IS THIS A PRICED DRAFT AWAITING A YES? — judged from the FACTS,
        # not the status word. Opening a draft CLAIMS it, which rewrote
        # the word to 'working·X' and used to knock the draft clean out
        # of the Drafts lane into Inbox (Dallon, Jul 13: Sanjeev
        # Balarajan vanished from Drafts on click). A claim means someone
        # is LOOKING at it, not that it stopped being a draft — the claim
        # still shows as the row badge below, but it no longer moves the
        # card. Mirrors _status_word's 'ready to approve' path minus the
        # claim check.
        ready_draft = bool(
            nb and not nb.get("reviewed") and not nb.get("dns_match")
            and not nb.get("tech_sender") and nb.get("kind") != "phone_lead"
            and nb["stamp"] not in live_holds
            and nb["stamp"] not in flags_open
            and nb["stamp"] not in getattr(bid_status, "_sl", {})
            and (sbs.get(nb["stamp"]) or "").lower() not in
            ("approved", "converted", "awaiting_response", "draft", "archived")
            # Jobber already has this one past the draft stage (tracked
            # OR backfilled by email) → nothing left to draft. The row
            # still surfaces in Inbox/Fix-its if the customer wrote —
            # it just stops padding the Drafts count (Dallon, Jul 14:
            # the office sees '20 drafts', trusts nothing).
            and oq_status not in ("approved", "converted",
                                  "awaiting_response", "archived")
            and (_num(nb.get("confidence")) or 0) >= 75
            and (_num((nb.get("draft") or {}).get("total")) or 0) > 0)
        if grp == -1:
            # a ✓'d tech note leaves the lane like everything else
            # (Dallon, Jul 12: 'once read it will say just tech') —
            # a new tech message brings it right back
            lane = ("drawer" if (acknowledged and not unread
                                 and not new_msg) else "techs")
            if c.get("internal") and lane == "techs":
                word = "📨 internal — office ↔ Dallon & Tom"
                wstyle = "color:#6d28d9;font-weight:700"
        elif urgent:
            lane = "inbox"           # a worried customer outranks all
        elif mv and mv.get("lane") == "done":
            lane = "drawer"
        elif mv and mv.get("lane") == "declined":
            lane = "nudge"
            word = f"🚫 declined · {mv.get('by') or 'office'}"
            wstyle = "color:var(--alarm);font-weight:800"
        elif mv and mv.get("lane") == "later":
            lane = "nudge"
            word, wstyle = ("⏰ parked — resurfaces in a week",
                            "color:var(--goldink);font-weight:700")
        elif mv and mv.get("lane") == "needs_reply":
            lane = "inbox"
            word, wstyle = ("✉️ needs reply",
                            "color:var(--goldink);font-weight:800")
        elif mv and mv.get("lane") == "fixits":
            lane = "fixits"
            word = "🔧 follow-up on completed work — no bid"
            wstyle = "color:var(--goldink);font-weight:800"
        elif won and grp != 4 \
                and not ((gmail_state.get(key) or {}).get("state")
                         == "done"):
            # a WON approval the office HASN'T trashed still needs
            # scheduling; a trashed one falls through to 🗑 below
            # (trash means they already booked it in Jobber)
            lane = "won"
        elif ((gmail_state.get(key) or {}).get("state") == "done"
              and not (last_at and last_at >
                       ((gmail_state.get(key) or {}).get("at") or ""))):
            # 🗑 THE OFFICE TRASHED IT IN GMAIL = DONE (LaRee's recorded
            # doctrine, Jul 14 call: 'deleted… 99.9% of the time' —
            # an explicit act, unlike the ARCHIVED-guessing the Jul-13
            # kill switch rightly stopped). 20 of the 28 stale inbox
            # rows on Jul 15 were already finished in Gmail. A newer
            # customer message still resurfaces the row.
            lane = "drawer"
            word = "🗑 done in Gmail"
            wstyle = "color:var(--mut);font-weight:700"
        elif grp == 3:
            lane = "handled"
        elif followup:
            lane = "fixits"          # Jobber fact: job done + they wrote
        elif won and grp != 4:
            lane = "won"             # Jobber fact: approved — a claim
            # must not knock it out of Won either (same bug as Drafts);
            # grp!=4 keeps an already-scheduled/done won in the drawer
        elif key in tom_standby_at and not (
                last_at and last_at > tom_standby_at[key]):
            # waiting on Tom's weather window — the 🏜 fold is their
            # home; they never gum up the working lanes (Dallon, Jul 15:
            # 'michelle yelle is in inbox AND in the tom only'). A
            # customer message NEWER than their intake resurfaces them;
            # a Jobber approval (Won) still outranks this.
            lane = "standby"
        elif oq_status == "awaiting_response" or grp == 2:
            # Jobber fact: quote out, ball in the customer's court —
            # EVEN if the office archived the thread (money still
            # waits). Quiet 10+ days = the follow-up list.
            lane = "nudge" if (age_h or 0) >= 240 else "waiting"
            if lane == "nudge" and not (unread or new_msg):
                word, wstyle = ("⏰ gone quiet — worth a nudge",
                                "color:var(--goldink);font-weight:800")
        elif office_draft_no and not (unread or new_msg):
            # the office is building this quote in Jobber — out of our
            # Inbox/Drafts so nobody double-works or double-quotes it,
            # but kept VISIBLE in its own section (a new customer message
            # still resurfaces it above). (Dallon, Jul 13)
            lane = "officedraft"
            word = f"🖊️ office is drafting this in Jobber · #{office_draft_no}"
            wstyle = "color:#8a5a00;font-weight:800"
        elif oq_status in ("converted", "archived") \
                and not (unread or new_msg):
            # the office already quoted AND closed this in Jobber (the
            # Jul-14 audit: Tammy Jett sat in Drafts while her job was
            # DONE) — off the working lanes; a new message resurfaces it
            lane = "drawer"
            word = (f"✅ handled in Jobber · #{(oq or {}).get('number')} "
                    f"{oq_status}")
            wstyle = "color:var(--green2);font-weight:700"
        elif grp == 4:
            lane = "drawer"
        elif ready_draft:
            lane = "drafts"          # the engine asking for a yes —
            # stays put even when claimed/opened (Sanjeev, Jul 13)
        else:
            lane = "inbox"           # when in doubt, sort UP
        # WHAT KIND OF ASK IS THIS? (LaRee + Dallon, Jul 13: 'train the
        # system on questions vs job requests' — a header TAG on the row,
        # not a separate tab). Three kinds:
        #   📅 about their visit — they're already booked (Jobber visit /
        #      handled match) and wrote in (Kim Doolittle's 'I think we
        #      are scheduled for this morning')
        #   📋 bid request — services were parsed; they want a price
        #   💬 question — they wrote words, no service ask; answer them
        qtag = None
        _lastin_b = ""
        if c["msgs"]:
            _li = next((m for m in reversed(c["msgs"])
                        if m.get("dir") == "in"), None)
            _lastin_b = ((_li or {}).get("body") or "").lower()
        # ✅ approved-and-asking-for-a-date — the #3 most common customer
        # message (33 in the last 4 weeks; Dallon's exact example):
        # 'I approve the quote. What day will you come?'
        if re.search(r"(approve|approved|go ahead|accept)", _lastin_b)                 and re.search(r"(what day|when (can|will|could)|which day|"
                              r"schedule|come out|how soon|next opening)",
                              _lastin_b) and (new_msg or unread):
            qtag = ("✅ approved — wants a date", "#8fc7a6")
        # ➕ SAME-DAY ADD-ON (Jennie Lee, Jul 15): booked customer
        # adding a service to an EXISTING visit — bundled price, no
        # minimum, add the line to the visit, never re-quote the rest
        elif re.search(r"(already (have|scheduled)|is scheduled|on the "
                       r"books)[\s\S]{0,120}(add|include|also do|same "
                       r"(day|visit|trip))|add [\s\S]{0,60}same "
                       r"(day|visit|trip)", _lastin_b) \
                and (new_msg or unread):
            qtag = ("➕ add-on to booked visit", "#e8c76a")
        elif lane in ("inbox", "drafts", "fixits"):
            _hjb = handled_jb.get(nb["stamp"], "") if nb else ""
            # a real VISIT on the schedule only — not merely a quote out
            if ("booked" in _hjb or "done" in _hjb) and (new_msg or unread):
                qtag = ("📅 about their visit", "#79aede")
            elif nb and nb.get("kind") == "phone_lead":
                pass                    # voicemail rows already say it
            elif (nb and nb.get("services")) and lane != "fixits":
                qtag = ("📋 bid request", "#8fc7a6")
            elif c["msgs"] and any(m.get("dir") == "in"
                                   for m in c["msgs"]):
                qtag = ("💬 question", "#e8c76a")
        # a retired-stale-quote note is worth showing on the row
        if stale_note:
            word, wstyle = stale_note, "color:var(--goldink);font-weight:700"
        # ACTIVE CLAIM / HANDOFF ALWAYS SHOW (Dallon, Jul 13) — the lane
        # words above were clobbering both. Claim (someone on it now)
        # wins over handoff (someone left it for the next person).
        _cl = claims.get(nb["stamp"]) if nb else None
        if _cl and _cl.get("mins", 99) <= CLAIM_FRESH_MIN:
            word = f"🔵 {_cl['by']} is working this"
            wstyle = "color:#79aede;font-weight:800"
        else:
            _ho = handoffs.get(key)
            if _ho:
                try:
                    from datetime import (datetime as _dh,
                                          timezone as _tzh,
                                          timedelta as _tdh)
                    fresh = _dh.now(_tzh.utc) < _dh.fromisoformat(
                        _ho["at"]) + _tdh(hours=24)
                except Exception:
                    fresh = False
                if fresh and (unread or new_msg):
                    word = f"🚶 {_ho.get('by', 'someone')} stepped away " \
                           "— pick up"
                    wstyle = "color:var(--goldink);font-weight:800"
                    lane = "inbox"          # warm pick-up belongs up top
        roster.append({"key": key, "c": c, "nb": nb, "unread": unread,
                       "grp": grp, "at": last_at, "word": word,
                       "wstyle": wstyle, "age": age_h or 0,
                       "new_msg": new_msg, "oq": oq, "qno": qno,
                       "won": won, "urgent": bool(urgent),
                       "lane": lane, "act": _msg_latest,
                       "cflag": cust_flags.get(key), "qtag": qtag,
                       "answered": answered,
                       "gmail": ((gmail_state.get(key) or {}).get("state")
                                 if not (unread or new_msg) else None)})
    # GMAIL MIRROR (Jessica, Jul 9: office works Gmail + dashboard side
    # by side for a while) — inside each section the order is pure
    # newest-activity-first, exactly like the Gmail list; bold marks
    # unread but does NOT reorder. Urgent still outranks everything.
    # THE 5-BUBBLE COLLAPSE (Dallon approved the plan Jul 13, built in
    # the Jul-14 night batch): label, don't filter —
    #   · Fix-its → Inbox (rows keep their 🔧 word — it's a message
    #     needing an answer, so it lives with the messages)
    #   · Nudge → Waiting (⏰ gone-quiet / 🚫 declined words stay)
    #   · In-Jobber → the Handled fold (🖊️ word stays; any new customer
    #     message still resurfaces it per the standing rule)
    # Plus SELF-ZEROING INBOX: we replied and nothing new came in →
    # the row clears itself to Done & quiet. Inbox trends to zero the
    # way Gmail does, without anyone clicking ✓.
    for r in roster:
        if r["lane"] == "fixits":
            r["lane"] = "inbox"
        elif r["lane"] == "nudge":
            r["lane"] = "waiting"
        elif r["lane"] == "officedraft":
            r["lane"] = "handled"
        if r["lane"] == "inbox" and r.get("answered") \
                and not r["unread"] and not r["new_msg"] \
                and not r["urgent"] and r["grp"] != 0:
            r["lane"] = "drawer"
    roster.sort(key=lambda r: r["at"], reverse=True)
    roster.sort(key=lambda r: (r["grp"], not r["urgent"],
                               r.get("gmail") == "done"))

    cur = next((r for r in roster if r["key"] == sel), None)
    convo_open = bool(cur and cur["new_msg"])
    if cur:
        # OPENING NO LONGER GREYS IT (Dallon: 'if someone clicks out and
        # needs to go back in quickly, it stays'). Grey happens on the
        # ✓ Done button, or automatically with any real decision.
        if cur["nb"] and not cur["nb"].get("reviewed") and user:
            claim_bid(cur["nb"]["stamp"], user)
            # opening it IS the pick-up — clear any 'stepped away' flag
            if handoffs.pop(cur["key"], None) is not None:
                _blob_save("handoffs", handoffs)

    # ── left list ──
    def row(r):
        c, nb = r["c"], r["nb"]
        active = r["key"] == sel
        box = ("background:var(--soft);outline:2px solid var(--green2)"
               if active else "")
        op = "" if (r["unread"] or active or r["grp"] == 0) else "opacity:.62;"
        if r.get("gmail") == "done" and not active:
            op = "opacity:.45;"       # office trashed it in Gmail = done
        nm_w = "font-weight:800" if r["unread"] else "font-weight:600"
        dot = "<span class='dot'></span>" if r["unread"] else ""
        if c.get("vm"):
            vm = c["vm"]
            pv = (f"{vm['dur']} voicemail"
                  + (" — hang-up, nothing to hear"
                     if vm["dur"] in ("0:00", "0:01", "0:02")
                     else " — transcript on the card"
                     if "🎙" in (nb.get("newest_message") or "")
                     else " — no audio attached, dial in to hear it"))
        elif c["msgs"] and (not nb or c["msgs"][-1]["at"]
                            >= _stamp_utc(nb["stamp"])):
            last = c["msgs"][-1]
            arrow = "←" if last["dir"] == "in" else "→"
            pv = f"{arrow} {(last.get('body') or last.get('subject') or '')[:400]}"
        elif nb:
            pv = (nb.get("newest_message") or nb.get("subject") or "")[:400]
        else:
            pv = ""
        alarm = (r["grp"] == 0 and r["age"] >= SLA_HOURS)
        age = (f"{r['age']*60:.0f}m" if r["age"] < 1 else
               f"{r['age']:.0f}h" if r["age"] < 48 else
               f"{r['age']/24:.0f}d")
        # ACTIVE vs QUIET (Dallon, Jul 13): Jobber's 'awaiting'/'won' is
        # blunt — cross it with the real conversation. A quote out or won
        # WITH recent back-and-forth is being worked (leave it); one gone
        # silent needs a nudge. Shown only where it matters.
        actchip = ""
        # Gmail's own verdict, LaRee's reading (Jul 14 call, corrected
        # Jul 15 from the recording): trash = done; UNREAD is the
        # office's own "actively working / come back to this" flag;
        # READ = looked at, not an immediate concern. Chips only — the
        # ✓ button (or a new message) still decides the row's fate.
        if r.get("gmail") == "done":
            actchip += ("<span style='color:var(--mut);font-weight:700;"
                        "font-size:11px'>🗑 done in Gmail</span> ")
        elif r.get("gmail") == "unread":
            actchip += ("<span style='color:#e8c76a;font-weight:700;"
                        "font-size:11px'>🖐 office on it — flagged to "
                        "revisit</span> ")
        elif r.get("gmail") == "working":
            actchip += ("<span style='color:var(--mut);font-weight:700;"
                        "font-size:11px'>👀 office has seen it</span> ")
        if r.get("qtag"):
            actchip += (f"<span style='color:{r['qtag'][1]};font-weight:800;"
                        f"font-size:11px'>{r['qtag'][0]}</span> ")
        _cfr = r.get("cflag")
        if _cfr:
            _cft = {"bad_payer": ("⚠️ bad payer", "#f2b8b5"),
                    "watch": ("👀 watch", "#e8c76a"),
                    "vip": ("⭐ VIP", "#8fc7a6"),
                    "realtor": ("🏘 realtor", "#79aede"),
                    # Diwali-timed lights homes (Dallon, Jul 14): the
                    # week of Diwali brings a surge — offer these homes
                    # EARLY-OCTOBER install proactively
                    "diwali": ("🪔 Diwali lights", "#e8c76a")}.get(
                        _cfr.get("label"), ("⚠️ flagged", "#f2b8b5"))
            actchip += (f"<span style='color:{_cft[1]};font-weight:800;"
                        f"font-size:11px'>{_cft[0]}</span> ")
        if r["lane"] in ("waiting", "nudge", "won") and r.get("act"):
            from datetime import datetime as _ad, timezone as _az
            try:
                _actd = ((_ad.now(_az.utc)
                          - _ad.fromisoformat(r["act"])).total_seconds()
                         / 86400)
                actchip = (
                    "<span style='color:#1e8449;font-weight:700;"
                    "font-size:11px'>🟢 active</span> " if _actd <= 3
                    else f"<span style='color:var(--mut);font-size:11px'>"
                         f"🔕 quiet {_actd:.0f}d</span> ")
            except (ValueError, TypeError):
                pass
        cls = ("irow sel" if active else
               "irow unread" if r["unread"] else
               "irow" if r["grp"] == 0 else "irow readq")
        # quiet price on the row (Jessica: 'I liked seeing that')
        rp = _num((nb.get("draft") or {}).get("total") if nb else None)
        price = (f"<span style='float:right;font-weight:700;"
                 f"color:var(--mut);font-size:12.5px'>${rp:,.0f}</span>"
                 if rp else "")
        # already-worked signal: the office has a Jobber quote out for
        # them (sent / won / on an earlier record) — safe to bulk-clear
        quoted = "1" if (r.get("oq") or r.get("qno") or r.get("won")) else "0"
        # one-click ✓ = Gmail's archive (Dallon GO, Jul 10 pm): clears
        # the row for the whole office; any new message resurfaces it
        donebtn = ("" if r["grp"] > 3 else
                   f"<button class='rowdone' type='button' "
                   f"data-k='{esc(r['key'])}' title='Done — clear from "
                   f"the queue' onclick='rowDone(event,this)'>✓</button>")
        return (
            f"<div class='irowwrap'>"
            f"<input type='checkbox' class='rowsel' name='keys' "
            f"value='{esc(r['key'])}' data-quoted='{quoted}' "
            f"onclick='bulkSync(event)'>"
            f"<a href='/?c={urllib.parse.quote(r['key'])}' class='{cls}'>"
            f"<div class='nm'>{dot}"
            f"{esc(c['name'] or c['email'] or '(no name)')[:30]}{price}</div>"
            f"<div class='pv'>{esc(pv)}</div>"
            f"<div class='meta'>{actchip}"
            f"<span class='word' style='{r['wstyle']}'>"
            f"{esc(r['word'])}</span>"
            f"<span class='iage{' alarm' if alarm else ''}'>{age}</span>"
            f"</div></a>{donebtn}</div>")

    sec_names = {-1: "👷 Techs",
                 0: "New — needs a person", 1: "In someone's hands",
                 2: "Waiting on customers",
                 3: "Handled in Jobber — verified"}
    counts = {g: sum(1 for r in roster if r["grp"] == g)
              for g in (-1, 0, 1, 2, 3, 4)}
    tech_new = sum(1 for r in roster if r["grp"] == -1 and r["unread"])
    # after an office approve pushed a Jobber quote, show a clickable
    # confirmation right away (LaRee, Jul 10: 'when you click approved the
    # page should refresh so you can click the Jobber quote')
    push_banner = ""
    if pushed:
        _pn = quote_numbers().get(pushed)
        _pu = quote_urls().get(pushed)
        if _pn:
            _lk = (f" — <a href='{esc(_pu)}' target='_blank' rel='noopener' "
                   f"style='color:#fff;text-decoration:underline;"
                   f"font-weight:800'>open in Jobber ↗</a>" if _pu else "")
            push_banner = (f"<div style='background:var(--green);color:#fff;"
                           f"border-radius:10px;padding:11px 14px;"
                           f"margin:0 0 10px;font-weight:700'>✅ Quote "
                           f"#{esc(str(_pn))} created{_lk}</div>")
    lst = (push_banner
           + f"<div style='display:flex;gap:8px;align-items:center;"
           f"padding:2px 4px 10px'>"
           f"<a href='/new' style='background:var(--green);color:#fff;"
           f"border-radius:9px;padding:7px 14px;text-decoration:none;"
           f"font-weight:700;font-size:13px'>➕ New lead</a>"
           f"<span class='subtext'>"
           f"{sum(1 for r in roster if r['lane'] == 'inbox')} in the "
           f"inbox"
           + (f" · oldest {max((r['age'] for r in roster if r['lane'] == 'inbox'), default=0):.0f}h"
              if any(r['lane'] == 'inbox' for r in roster)
              else " — all caught up ✅") + "</span></div>"
           # search (Jessica, Jul 9) — filters the list as you type
           "<input id='isearch' placeholder='🔎 Find a customer…' "
           "style='width:100%;margin:0 0 10px;padding:8px 12px;"
           "border-radius:9px;border:1px solid var(--line);"
           "background:var(--card);color:var(--ink);font-size:13.5px'>"
           """<script>
function rowDone(ev, btn){
  ev.stopPropagation(); ev.preventDefault();
  var f = document.createElement('form');
  f.method = 'POST'; f.action = '/mark_done';
  var a = document.createElement('input');
  a.name = 'addr'; a.value = btn.dataset.k; f.appendChild(a);
  var b = document.createElement('input');
  b.name = 'back'; b.value = '/'; f.appendChild(b);
  document.body.appendChild(f);
  if (window.__saveScroll) window.__saveScroll();
  f.submit();
}
function laneShow(lid){
  document.querySelectorAll('.lanechip').forEach(function(c){
    c.classList.toggle('on', c.dataset.l === lid);});
  document.querySelectorAll('.lanebody').forEach(function(b){
    b.style.display = (b.id === 'lane-' + lid) ? '' : 'none';});
  var sub = document.getElementById('lanesub');
  if (sub && window.LANE_SUBS) sub.textContent = LANE_SUBS[lid] || '';
  try { sessionStorage.setItem('lane', lid); } catch(e){}
}
function laneSwap(btn){ laneShow(btn.dataset.l); }
function laneClear(lid){
  var body = document.getElementById('lane-' + lid);
  if(!body) return;
  var boxes = body.querySelectorAll('.rowsel');
  if(!boxes.length) return;
  if(!confirm('Clear all ' + boxes.length + ' from this list? They move '
    + 'to Done & quiet and STAY cleared — only a new customer message '
    + 'brings one back.')) return;
  var f = document.createElement('form');
  f.method = 'POST'; f.action = '/lane_clear';
  boxes.forEach(function(b){
    var i = document.createElement('input');
    i.name = 'keys'; i.value = b.value; f.appendChild(i);
  });
  document.body.appendChild(f);
  if (window.__saveScroll) window.__saveScroll();
  f.submit();
}
document.addEventListener('DOMContentLoaded', function(){
  var lid = 'inbox';
  try { lid = sessionStorage.getItem('lane') || 'inbox'; } catch(e){}
  if (!document.getElementById('lane-' + lid)) lid = 'inbox';
  laneShow(lid);
  var s = document.getElementById('isearch');
  if (!s) return;
  s.addEventListener('input', function(){
    var q = s.value.trim().toLowerCase();
    // searching looks across EVERY lane; clearing restores the tab
    document.querySelectorAll('.lanebody').forEach(function(b){
      b.style.display = q ? '' : 'none';});
    if (!q) {
      var cur = 'inbox';
      try { cur = sessionStorage.getItem('lane') || 'inbox'; } catch(e){}
      laneShow(cur);
    }
    document.querySelectorAll('.irowwrap').forEach(function(r){
      r.style.display = (!q || r.textContent.toLowerCase()
                         .indexOf(q) >= 0) ? '' : 'none';
    });
    document.querySelectorAll('.ihead').forEach(function(h){
      h.style.display = q ? 'none' : '';
    });
  });
});
</script>""")
    # ── bulk "mark seen" (Dallon, Jul 10: 'mark many complete at a
    #    time, like email... cross-reference which ones are already
    #    worked'). Checking a row shows this bar. It ONLY sets the grey
    #    seen flag (reversible, office-wide) — never a decision, so it
    #    can't touch the scoreboard, the learning loop, or Jobber.
    lst += ("<form id='bulkform' method='POST' action='/mark_seen_bulk'>"
            "<div class='bulkbar' id='bulkbar'>"
            "<span id='bulkcount'>0 selected</span>"
            "<button type='submit' class='bulkgo'>✓ Done — clear</button>"
            "<button type='button' class='bulklink' onclick='bulkQuoted()'>"
            "Select all already-quoted (<span id='bulkqn'>0</span>)</button>"
            "<button type='button' class='bulklink' onclick='bulkClear()'>"
            "Clear</button></div>")
    # ── THE LANES (Dallon, Jul 12; per the approved mockup): one row
    # of tap-chips, each swapping the list below. NO CAP inside a lane
    # — hiding a customer is the unforgivable failure. ──
    # FIVE BUBBLES (Dallon's approved plan, built Jul 14 night): every
    # row still carries its old identity as a word/tag — 🔧 fix-its
    # live in Inbox, ⏰ nudges live in Waiting, 🖊️ in-Jobber rows live
    # in the Handled fold. Fewer boxes, nothing hidden.
    LANES = [("inbox", "📬 Inbox",
              "Matches Gmail — work bottom to top, oldest first. New "
              "messages, replies, voicemails, and 🔧 fix-its. Answered "
              "rows clear themselves."),
             ("drafts", "🤖 Drafts",
              "The engine's quotes waiting for a human yes. Burn down "
              "when there's time."),
             ("won", "📅 Won",
              "They said yes — get them on the schedule."),
             ("waiting", "📤 Waiting",
              "Quotes out, ball in the customer's court — including ⏰ "
              "gone-quiet nudges and 🚫 declines. Moves by itself."),
             ("techs", "👷 Techs",
              "Field mail from our own techs — never a bid.")]
    chips = ""
    for lid, label, _sub in LANES:
        n = sum(1 for r in roster if r["lane"] == lid)
        if lid == "techs" and n == 0:
            continue
        # ONE number per chip (Dallon, Jul 12: 'confusing seeing 2
        # numbers') — the red unseen count. All read = just the name.
        # EXCEPT 'In Jobber': those rows are read by nature, so show the
        # TOTAL (a muted count) or you'd never know any are in there.
        if lid == "officedraft":
            newb = f" <span class='ltot'>{n}</span>" if n else ""
        else:
            newn = sum(1 for r in roster
                       if r["lane"] == lid and r["unread"])
            newb = f" <span class='lnew'>{newn}</span>" if newn else ""
        chips += (f"<button type='button' class='lanechip' data-l='{lid}'"
                  f" onclick='laneSwap(this)'>{label}{newb}</button>")
    subs_json = json.dumps({lid: sub for lid, _l, sub in LANES}) \
        .replace("</", "<\\/")
    lst += (f"<div class='lanechips' id='lanechips'>{chips}</div>"
            f"<div class='lanesub' id='lanesub'></div>"
            + f"<script>var LANE_SUBS = {subs_json};</script>")
    for lid, _label, _sub in LANES:
        # GMAIL ORDER inside every lane (Dallon, Jul 13: 'they're out of
        # order from Gmail, scrolling back and forth'). Sort by the
        # actual LAST-MESSAGE age (newest first) — NOT last_at, which
        # was polluted by the record's re-processing stamp and shoved
        # freshly-swept rows to the top. age matches the label shown on
        # each row, so the order the office reads IS the order they see.
        rows_l = sorted((r for r in roster if r["lane"] == lid),
                        key=lambda r: r["age"])
        # ZERO-IT-OUT (Dallon, Jul 13: 'the office wants the daily done
        # feel, to work a lane down to empty like Gmail'). One click marks
        # everything in the lane seen → Done & quiet → "All caught up ✅".
        # Reversible; any new customer message brings a row right back.
        # Not on 'In Jobber' (that's the office's Jobber work, not ours).
        clearbar = ""
        if rows_l and lid != "officedraft":
            clearbar = (f"<div class='laneclear'><button type='button' "
                        f"onclick='laneClear(\"{lid}\")'>✓ Clear all "
                        f"{len(rows_l)} — done for now</button></div>")
        lst += (f"<div class='lanebody' id='lane-{lid}' "
                f"style='display:none'>" + clearbar
                + ("".join(row(r) for r in rows_l)
                   or "<div style='padding:26px;text-align:center;"
                      "color:var(--green2);font-weight:800'>All caught "
                      "up ✅</div>")
                + "</div>")
    # CLEAN BOTTOM (LaRee, Jul 13: 'they work the list oldest-to-newest
    # and NOTHING else is below it') — everything already-handled lives
    # in thin COLLAPSED folds, never an open list under the lane.
    # 🏜 TOM'S DRY-DAY STANDBY (Dallon, Jul 15: a persistent folder that
    # never gets lost). Weather-window work, any season, his call —
    # a dry stretch in March counts as much as August.
    _ts = _blob_rw("tom_standby", {}) or {}
    _tsc = _ts.get("customers") or []
    if _tsc:
        _tsrows = "".join(
            f"<a href='/?c={urllib.parse.quote(c.get('email') or '')}' "
            f"class='irow' style='opacity:.85'><div class='nm'>🏜 "
            f"{esc(c.get('name') or c.get('email') or '?')}</div>"
            f"<div class='pv'>{esc(c.get('address') or '')} · "
            f"{esc(c.get('quote_status'))}</div></a>"
            for c in _tsc)
        lst += (f"<details style='margin-top:12px'><summary style="
                f"'cursor:pointer;color:var(--goldink);font-size:12.5px;"
                f"font-weight:800;padding:0 8px'>🏜 Tom's dry-day "
                f"standby ({len(_tsc)}) — any dry window, his call"
                f"</summary><div class='subtext' style='padding:4px 8px'>"
                f"High-risk/Tom-only homes waiting for dry weather — "
                f"March or June counts as much as August. This list "
                f"never loses anyone; jobs drop off when converted."
                f"</div>{_tsrows}</details>")
    handled_rows = [r for r in roster if r["lane"] == "handled"]
    if handled_rows:
        lst += (f"<details style='margin-top:12px'><summary style='cursor:"
                f"pointer;color:var(--mut);font-size:12.5px;font-weight:700;"
                f"padding:0 8px'>Handled in Jobber — verified "
                f"({len(handled_rows)})</summary>"
                + "".join(row(r) for r in handled_rows) + "</details>")
    # 🗑 WHERE THE GMAIL-SYNCED ROWS WENT (Martha + Jessica, Jul 15,
    # within hours of the trash=done rule: "I can't find a lot of
    # emails… is the automatic clean up clearing too much?"). Nothing
    # is deleted — everything the sync cleared sits HERE, named, with
    # the search box above finding anyone instantly.
    gm_rows = [r for r in roster if r["lane"] == "drawer"
               and r.get("gmail") == "done"]
    if gm_rows:
        lst += (f"<details style='margin-top:12px'><summary style="
                f"'cursor:pointer;color:var(--mut);font-size:12.5px;"
                f"font-weight:800;padding:0 8px'>🗑 Cleared by the "
                f"Gmail sync ({len(gm_rows)}) — they're in your Gmail "
                f"trash, so the dashboard filed them too</summary>"
                f"<div class='subtext' style='padding:4px 8px'>Nothing "
                f"is deleted — open any row, or use 🔍 find customer "
                f"above. A new message from them pops the row right "
                f"back into the Inbox.</div>"
                + "".join(row(r) for r in gm_rows[:40]) + "</details>")
    done_rows = [r for r in roster if r["lane"] == "drawer"
                 and r.get("gmail") != "done"]
    lst += (f"<details style='margin-top:6px'><summary style='cursor:"
            f"pointer;color:var(--mut);font-size:12.5px;font-weight:700;"
            f"padding:0 8px'>Done &amp; quiet ({len(done_rows)}) · "
            f"<a href='/queue'>old queue</a> · <a href='/history'>history"
            f"</a></summary>"
            + "".join(row(r) for r in done_rows[:30]) + "</details>")
    lst += "</form>"
    lst += """<script>
(function(){
  function boxes(){return [].slice.call(document.querySelectorAll('.rowsel'));}
  window.bulkSync = function(e){
    if (e) e.stopPropagation();
    var n = boxes().filter(function(x){return x.checked;}).length;
    var bar = document.getElementById('bulkbar');
    var lbl = document.getElementById('bulkcount');
    if (lbl) lbl.textContent = n + ' selected';
    if (bar) bar.classList.toggle('show', n > 0);
  };
  window.bulkQuoted = function(){
    boxes().forEach(function(x){
      if (x.getAttribute('data-quoted') === '1') x.checked = true; });
    window.bulkSync();
  };
  window.bulkClear = function(){
    boxes().forEach(function(x){ x.checked = false; });
    window.bulkSync();
  };
  document.addEventListener('DOMContentLoaded', function(){
    var q = boxes().filter(function(x){
      return x.getAttribute('data-quoted') === '1'; }).length;
    var el = document.getElementById('bulkqn');
    if (el) el.textContent = q;
  });
})();
</script>"""

    # ── right: pinned card + folds ──
    if not cur:
        detail = ("<div style='display:flex;align-items:center;"
                  "justify-content:center;min-height:420px;flex:1;"
                  "color:var(--mut)'><div style='text-align:center'>"
                  "<div style='font-size:36px'>📥</div><b>Pick a customer"
                  "</b><div class='subtext'>Bold = nobody's handled it "
                  "yet. Opening never greys it — the ✓ Done button does."
                  "</div></div></div>")
    else:
        detail = _inbox_detail(cur, quotes, qurls, live_holds, flags_open,
                               sbs, claims, draft, convo_open, user)

    # ── NEW DESIGN (Dallon's Stitch system, Jul 10): the Bid Queue is
    # the DARK EMERALD ROOM — glass cards, gold accents, slim icon rail
    # as the ONLY nav, logo + user pill up top. Scoped overrides keep
    # every existing class/behavior intact (trials stay green).
    # Today strip REMOVED (Dallon, Jul 10 pm: "this whole thing just
    # needs to go away… it's not a scheduling app"). The pulse data
    # still feeds the Handled-in-Jobber lane — only the chips line died.
    body = (_DARKROOM_CSS + f"<div class='mock dkroom'>{_chrome_dark()}"
            + f"<div class='inboxgrid'>"
            f"<div class='ilist'>{lst}</div>"
            "<div class='iresize' title='Drag to widen the list'></div>"
            f"<div class='idetail'>{detail}</div>"
            f"</div></div>")
    body += """
<script>
// KEEP MY PLACE ACROSS AUTO-REFRESH (Dallon Jul 10: 'working at the
// bottom and it refreshes and pulls me to the top'). Save where the
// list + detail panes are scrolled, restore after the reload.
(function(){
  var KEY = 'scroll:' + location.pathname + location.search;
  // the LIST keeps its place across DIFFERENT selections too (Dallon,
  // Jul 10 pm: 'clicking on a name snaps the scroll to the top') —
  // /?c=alice and /?c=bob are different URLs but the same list, so
  // the list pane gets its own key WITHOUT the query string
  var LKEY = 'scroll-list:' + location.pathname;
  function panes(){ return {
    list: document.querySelector('.ilist'),
    detail: document.querySelector('.idetail')}; }
  // ANCHOR ON A ROW, NOT A PIXEL (Dallon, Jul 15: 'working from the
  // bottom up, it continues to refresh to the top'). A refresh adds/
  // removes rows ABOVE where you're working, so a pixel offset lands
  // on the wrong row — remember WHICH customer was at the top of the
  // pane instead, and put that same customer back in the same spot.
  function anchor(p){
    if (!p.list) return null;
    var lt = p.list.getBoundingClientRect().top;
    var rows = p.list.querySelectorAll('.irowwrap');
    for (var i = 0; i < rows.length; i++){
      var r = rows[i].getBoundingClientRect();
      if (r.bottom > lt + 4){
        var k = rows[i].querySelector('.rowsel');
        return {k: k ? k.value : null, off: Math.round(r.top - lt)};
      }
    }
    return null;
  }
  function save(){
    var p = panes();
    try { sessionStorage.setItem(KEY, JSON.stringify({
      w: window.scrollY,
      list: p.list ? p.list.scrollTop : 0,
      detail: p.detail ? p.detail.scrollTop : 0})); } catch(e) {}
    try { if (p.list)
      sessionStorage.setItem(LKEY, JSON.stringify({
        top: p.list.scrollTop, a: anchor(p)})); } catch(e) {}
  }
  window.__saveScroll = save;
  // save the moment a row is clicked, before the browser navigates
  document.addEventListener('click', function(ev){
    if (ev.target.closest && ev.target.closest('.irow')) save();
  }, true);
  // restore the list position on EVERY load (retry until laid out)
  try {
    var lraw = sessionStorage.getItem(LKEY) || '';
    var lsav = null;
    try { lsav = JSON.parse(lraw); } catch(e){}
    if (typeof lsav === 'number') lsav = {top: lsav, a: null};
    if (lsav && (lsav.top > 0 || (lsav.a && lsav.a.k))) {
      var ltries = 0;
      (function lapply(){
        var p = panes();
        if (p.list) {
          var done = false;
          if (lsav.a && lsav.a.k) {
            var sel = p.list.querySelector(
              ".rowsel[value='" + lsav.a.k.replace(/'/g, "\\'") + "']");
            var wrap = sel && sel.closest('.irowwrap');
            if (wrap) {
              var lt = p.list.getBoundingClientRect().top;
              p.list.scrollTop += (wrap.getBoundingClientRect().top - lt)
                                  - (lsav.a.off || 0);
              done = true;
            }
          }
          if (!done) p.list.scrollTop = lsav.top;
          var want = done ? p.list.scrollTop : lsav.top;
          var lok = done || Math.abs(p.list.scrollTop - lsav.top) < 3
                    || p.list.scrollHeight - p.list.clientHeight
                       <= lsav.top + 3;
          if (!lok && ltries++ < 12) setTimeout(lapply, 80);
        } else if (ltries++ < 12) setTimeout(lapply, 80);
      })();
    }
  } catch(e) {}
  // restore on load — RETRY until the panes have laid out, because
  // setting scrollTop before the list has its full height silently
  // clamps to 0 (that was the 'jumps to top' bug, Jul 10)
  try {
    var s = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    if (s) {
      sessionStorage.removeItem(KEY);
      var tries = 0;
      (function apply(){
        var p = panes();
        // the LIST is handled by the row-anchored restore above —
        // pixel-setting it here would undo the anchor (Jul 15)
        if (p.detail) p.detail.scrollTop = s.detail;
        window.scrollTo(0, s.w);
        // if the target didn't stick (content not tall enough yet),
        // try again for up to ~1s
        var ok = (!p.detail || Math.abs(p.detail.scrollTop - s.detail) < 3
                  || p.detail.scrollHeight - p.detail.clientHeight
                     <= s.detail + 3);
        if (!ok && tries++ < 12) setTimeout(apply, 80);
      })();
    }
  } catch(e) {}
  // also save on every scroll, so a manual reload keeps the place too
  var t; ['scroll'].forEach(function(ev){
    document.addEventListener(ev, function(){
      clearTimeout(t); t = setTimeout(save, 150);
    }, true);
  });
})();
(function(){
  var last = null;
  function bump(){
    fetch('/api/pulse').then(function(r){return r.json();}).then(function(d){
      if (last === null) { last = d.t; return; }
      var rb = document.getElementById('bidreply') ||
               document.getElementById('inboxreply');
      // NEVER reload out from under an open fact edit (Dallon, Jul 13:
      // 'if i edit and close it, the fix doesnt go away') — an open
      // Fix-the-facts panel counts as an active edit, same as a
      // half-typed reply.
      var ff = document.getElementById('fixfacts');
      // WIDER GUARD (Dallon, Jul 14: 'if they are mid draft and it
      // refreshes, they ruin the work') — ANY typed-but-unsent text
      // in ANY box, or a cursor sitting in a field, blocks the reload.
      // The numbers update on the NEXT quiet pulse instead.
      var ae = document.activeElement;
      var typing = ae && (ae.tagName === 'TEXTAREA' ||
                          ae.tagName === 'SELECT' ||
                          (ae.tagName === 'INPUT' &&
                           ae.type !== 'checkbox' &&
                           ae.type !== 'button'));
      var dirty = false;
      document.querySelectorAll('textarea').forEach(function(t){
        if (t.value.trim()) dirty = true;
      });
      // HALF-DONE EDITS COUNT TOO (Jessica/Tracy Van Horn, Jul 15:
      // her ✕ line-removals and price changes kept silently vanishing
      // — the refresh only respected textareas). A checked box or a
      // changed input blocks the reload the same as a typed reply.
      document.querySelectorAll('input').forEach(function(t){
        if (t.type === 'checkbox' || t.type === 'radio') {
          if (t.checked !== t.defaultChecked) dirty = true;
        } else if (t.type !== 'hidden' && t.type !== 'button'
                   && t.value !== t.defaultValue) dirty = true;
      });
      var editing = (rb && rb.value.trim()) || (ff && ff.open)
                    || typing || dirty;
      if (d.t !== last && !editing) {
        if (window.__saveScroll) window.__saveScroll();
        location.reload();
      }
      last = d.t;
    }).catch(function(){});
  }
  setInterval(bump, 30000); bump();
})();
// FOLDS SURVIVE REFRESH (Jessica, Jul 9: 'refreshing closes all the
// drop down tabs... it would mess up flow') — remember which folds are
// open per customer, restore them after any reload.
(function(){
  var key = 'folds:' + (new URLSearchParams(location.search).get('c') || '');
  var open = [];
  try { open = JSON.parse(sessionStorage.getItem(key) || '[]'); }
  catch(e) {}
  var folds = document.querySelectorAll('details.ifold, #addsvcfold');
  folds.forEach(function(f, i){
    if (open.indexOf(i) >= 0) f.open = true;
    f.addEventListener('toggle', function(){
      var now = [];
      folds.forEach(function(g, j){ if (g.open) now.push(j); });
      try { sessionStorage.setItem(key, JSON.stringify(now)); }
      catch(e) {}
    });
  });
})();
</script>"""
    return page("Bids", body, chrome="bare")


_MSG_MORE_JS = """<script>
window.mbToggle=function(b){var x=b.previousElementSibling;
 var o=x.classList.toggle('open');b.textContent=o?'Show less':'Show more';};
(function(){function w(){document.querySelectorAll('.msgbody').forEach(function(b){
 if(b.dataset.mb)return;b.dataset.mb='1';var t=b.nextElementSibling;
 if(t&&t.classList.contains('msgmore')&&b.scrollHeight-b.clientHeight>2)
  t.style.display='inline-block';});}
 if(document.readyState!=='loading')w();else document.addEventListener('DOMContentLoaded',w);})();
</script>"""


def _expandable(text):
    """Full message body, clamped to ~3 lines with a Show-more toggle when
    it overflows (Dallon, Jul 10: 'we need all info… if it's more than 3
    lines make it expandable'). Nothing is truncated — the whole message
    is in the DOM, just visually collapsed until opened."""
    return (f"<div class='msgbody'>{esc(text or '')}</div>"
            f"<button type='button' class='msgmore' style='display:none' "
            f"onclick='mbToggle(this)'>Show more</button>")


def _tech_about(nb):
    """Best-effort: which customer/job is a tech email about? Match an
    address or a strong 2-word name from the thread against our records.
    Returns (label, url) or None. NEVER forces a wrong link — no
    confident match returns None and the office picks (Dallon, Jul 13:
    'the office needs to know which customer the tech is replying to')."""
    text = ((nb.get("newest_message") or "") + " "
            + (nb.get("subject") or ""))
    if not text.strip():
        return None
    import re as _re
    recs = [(s, r) for s, r in _shadow_source()
            if not r.get("tech_sender") and not r.get("spam_auto")
            and not r.get("merged_into")]
    # 1) address match (strongest signal)
    am = _re.search(r"\b(\d{2,6})\s+([A-Za-z][\w'.-]*(?:\s+[A-Za-z][\w'.-]*)"
                    r"{0,3})", text)
    if am:
        num, street = am.group(1), am.group(2).lower()
        for s, r in recs:
            a = (r.get("address") or "").lower()
            if a and num in a and any(w in a for w in street.split()[:2]):
                nm = (r.get("from") or "").split("<")[0].strip() \
                    or r.get("address")
                m2 = _re.search(r"<([^>]+)>", r.get("from") or "")
                key = m2.group(1).lower() if m2 else f"stamp:{s}"
                return (nm, f"/?c={urllib.parse.quote(key)}")
    # 2) strong name match: a Capitalized First Last that IS a customer
    for nm_m in _re.finditer(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b", text):
        cand = f"{nm_m.group(1)} {nm_m.group(2)}".lower()
        for s, r in recs:
            disp = (r.get("from") or "").split("<")[0].strip().lower()
            if disp and cand == disp:
                m2 = _re.search(r"<([^>]+)>", r.get("from") or "")
                key = m2.group(1).lower() if m2 else f"stamp:{s}"
                return ((r.get("from") or "").split("<")[0].strip(),
                        f"/?c={urllib.parse.quote(key)}")
    return None


def _inbox_detail(cur, quotes, qurls, live_holds, flags_open, sbs,
                  claims, draft, convo_open, user):
    """Pinned critical card + the big folds for one Inbox entry."""
    import msglog
    c, nb = cur["c"], cur["nb"]
    key = cur["key"]
    back = f"/?c={urllib.parse.quote(key)}"
    d = (nb.get("draft") or {}) if nb else {}
    bid_d = d.get("bid") or {}
    stamp = nb["stamp"] if nb else None

    # banners: collision / dns / second look
    banners = ""
    # CUSTOMER FLAG (Dallon, Jul 13 — Garrett Mydland: a bad-payer
    # CUSTOMER who sent junk mail; spam would erase the history we need
    # to remember. The flag sticks to the PERSON forever, separate from
    # spam (hides mail) and DNS (won't serve them at all).
    _cf = None
    if c.get("email"):
        _cf = _blob_rw("customer_flags", {}).get(_canon_email(c["email"]))
    if _cf:
        _cfl = {"bad_payer": "⚠️ BAD PAYER", "watch": "👀 WATCH",
                "vip": "⭐ VIP",
                "realtor": "🏘 REALTOR — price PER HOUSE"}.get(
                    _cf.get("label"), "⚠️ FLAGGED")
        _lbl2 = _cf.get("label")
        _cfbg = {"vip": "#1c2a13", "realtor": "#16283a"}.get(_lbl2, "#2a1313")
        _cfink = {"vip": "#b8f2c0", "realtor": "#79aede"}.get(_lbl2, "#f2b8b5")
        banners += (
            f"<div class='band' style='background:{_cfbg};border-color:"
            f"{_cfink};border-left-color:{_cfink};margin:0 0 10px'>"
            f"<b style='color:{_cfink}'>{_cfl}</b> — "
            f"{esc(_cf.get('note') or '')} "
            f"<span style='color:var(--mut);font-size:11.5px'>(flagged by "
            f"{esc(_cf.get('by') or 'office')})</span>"
            f"<form method='POST' action='/flag_customer_clear' "
            f"style='display:inline;margin-left:8px'>"
            f"<input type='hidden' name='email' value='{esc(c['email'])}'>"
            f"<input type='hidden' name='back' value='{esc(back)}'>"
            f"<button style='padding:2px 9px;font-size:11px;background:none;"
            f"border:1px solid {_cfink};color:{_cfink}'>remove</button>"
            f"</form></div>")
    if nb:
        other = claims.get(stamp)
        if other and other.get("by") != user \
                and other["mins"] <= CLAIM_FRESH_MIN:
            banners += (
                f"<div class='band' style='background:#e5edff;border-color:"
                f"#b9ccf5;border-left-color:#1d4ed8;margin:0 0 10px'>"
                f"<b style='color:#1d4ed8'>👥 {esc(other['by'])} is on this "
                f"({other['mins']:.0f} min)</b> — check with them first. "
                f"<form method='POST' action='/claim_take' style='display:"
                f"inline'><input type='hidden' name='stamp' value='{stamp}'>"
                f"<button style='padding:3px 10px;font-size:11.5px;"
                f"background:#1d4ed8'>🤝 Take over</button></form></div>")
        if nb.get("dns_match"):
            h = nb["dns_match"]
            banners += (
                f"<div class='band' style='background:#1c1c1c;border-color:"
                f"#000;border-left-color:#ff6b5e;margin:0 0 10px'>"
                f"<b style='color:#ff6b5e'>⛔ DO NOT SERVICE</b> "
                f"<span style='color:#ddd'>— matches “{esc(h['name'])}” "
                f"({esc(h['matched_by'])}). Don't quote or schedule.</span>"
                f"</div>")
        sl = getattr(bid_status, "_sl", {}).get(stamp)
        if sl and not nb.get("reviewed"):
            banners += (
                f"<div class='band' style='background:#f0e9fd;border-color:"
                f"#d8c7f7;border-left-color:#6d28d9;margin:0 0 10px'>"
                f"<b style='color:#6d28d9'>🔍 {esc(sl[1])} asked:</b> "
                f"“{esc(sl[0][:160])}”</div>")
        if nb.get("tech_sender"):
            banners += (
                f"<div class='band' style='background:var(--purplebg,"
                f"#f0e9fd);border-color:#d8c7f7;border-left-color:#6d28d9;"
                f"margin:0 0 10px'><b style='color:#6d28d9'>👷 Field mail "
                f"from {esc(nb['tech_sender'])}</b> — one of our techs. "
                f"Never gets a bid; if it's about a customer, handle it on "
                f"that customer's thread.</div>")
        # TOM ONLY must SHOUT (Jessica, Jul 9: Becky Pohlman's 11/12
        # pitch was in the data but nobody saw it on the card)
        _pi0 = (nb.get("draft") or {}).get("prop_info") or {}
        try:
            from jobber_client import _is_tom_only as _tomchk
            _tomjob = _tomchk(_pi0)
        except Exception:
            _tomjob = _pi0.get("pitch") == "tom_only"
        if _tomjob and not nb.get("tech_sender"):
            _why_tom = ("steep pitch (Tom-tier)" if _pi0.get("pitch")
                        == "tom_only" else
                        f"{_pi0.get('roof_material')} roof"
                        if any(m in str(_pi0.get("roof_material")).lower()
                               for m in ("shake", "tile", "metal"))
                        else f"{_pi0.get('stories')} stories")
            banners += (
                f"<div class='band' style='background:#8a1f13;border-color:"
                f"#6d160c;border-left-color:#ffb4a8;margin:0 0 10px'>"
                f"<b style='color:#fff'>🧗 TOM ONLY JOB</b> "
                f"<span style='color:#ffd9d2'>— {esc(_why_tom)}. Do not "
                f"assign or schedule other techs on this roof.</span></div>")

    # pinned card pieces
    conf = _num(nb.get("confidence") if nb else None)
    cc = ("#1e8449" if (conf or 0) >= 75 else
          "#c77700" if (conf or 0) >= 50 else "#b03a2e")
    total_html = "<div class='subtext'>no priced draft</div>"
    _total = _num(d.get("total"))
    if _total:
        d = dict(d, total=_total)     # numeric for the format calls below
        rng = ""
        if conf is not None and conf < 50:
            from bid_engine import round_to_5 as _r5
            rng = (f"<div style='font-size:11.5px;font-weight:700;"
                   f"color:#c77700'>likely ${_r5(d['total']*0.8):,.0f}–"
                   f"${_r5(d['total']*1.2):,.0f}</div>")
        total_html = (f"<div class='ptotal'>${d['total']:,.0f}</div>{rng}"
                      + (f"<div class='conf' style='color:{cc}'>{conf}% "
                         f"sure · engine-priced</div>"
                         if conf is not None else ""))
    say = ""
    last_in = next((m for m in reversed(c["msgs"]) if m["dir"] == "in"),
                   None) if c["msgs"] else None
    if c.get("vm"):
        vm = c["vm"]
        msgtxt = (nb.get("newest_message") or "") if nb else ""
        if "🎙" in msgtxt:
            say_txt = msgtxt                      # the real transcript
        elif vm["dur"] in ("0:00", "0:01", "0:02"):
            say_txt = (f"{vm['dur']} voicemail from {vm['caller']} — a "
                       "hang-up; nothing recorded, nothing to do.")
        else:
            say_txt = (f"{vm['dur']} voicemail from {vm['caller']} "
                       f"({vm.get('when') or ''}) — CopyCall attached no "
                       "audio, so no transcript. Dial the mailbox to "
                       "hear it, then reply by email.")
    else:
        say_txt = ((msglog.clean_body(last_in.get("body") or "")
                    or last_in.get("subject")) if last_in
                   else (nb.get("newest_message") if nb else "")) or ""
    if say_txt:
        # voicemail transcripts show IN FULL (Jessica, Jul 9: 'transcript
        # section needs to be bigger so the office can read' it all)
        cut = 2000 if (c.get("vm") and "🎙" in say_txt) else 260
        say = f"<div class='say'>“{esc(say_txt[:cut])}”</div>"
    chips = ""
    specs_html = ""
    if nb:
        # SITE SPECIFICATIONS rail (Stitch Bid Review — Dallon, Jul 10
        # pm: 'all the stats on the right of the picture — what
        # happened to those'; supersedes Jessica's house-fact chips)
        _pih = (nb.get("draft") or {}).get("prop_info") or {}

        def _sprow(icon, label, val, unit=""):
            u = f"<small>{unit}</small>" if unit else ""
            return (f"<div class='sprow'><span class='sl'>"
                    f"{_svg_icon(icon)}{label}</span>"
                    f"<span class='sv'>{val}{u}</span></div>")

        _rows = ""
        if _pih.get("sqft"):
            _rows += _sprow("sq", "Total area", f"{_pih['sqft']:,}",
                            "SQFT")
        if _pih.get("stories"):
            _rows += _sprow("height", "Stories",
                            esc(str(_pih["stories"]).replace("_", " ")))
        # PITCH always shows (Dallon, Jul 13: 'it never measured the
        # pitch' — the row was just hidden when absent). The system
        # can't read pitch reliably from the sky (flag-don't-guess), so
        # a missing pitch means it's on the SAFE default until someone
        # confirms — say so, don't leave a blank.
        _pv = {"mild": "Mild", "moderate": "Moderate", "steep": "STEEP",
               "tom_only": "TOM ONLY"}.get(_pih.get("pitch"))
        if _pv:
            _rows += _sprow("pitch", "Pitch of roof", _pv)
        else:
            _rows += (f"<div class='sprow'><span class='sl'>"
                      f"{_svg_icon('pitch')}Pitch of roof</span>"
                      f"<span class='sv' style='color:var(--mut);"
                      f"font-size:12.5px'>assumed moderate<small>"
                      f"SET BELOW</small></span></div>")
        if _pih.get("roof_material"):
            _rows += _sprow("home", "Roof type",
                            esc(_roof_label(_pih["roof_material"])))
        _deb = _pih.get("debris_read") or _pih.get("debris")
        if _deb:
            _rows += _sprow("leaf", "Debris level",
                            esc(str(_deb).title()))
        if _pih.get("basement_sqft"):
            _rows += _sprow("home", "Walkout bsmt",
                            f"{_pih['basement_sqft']:,}", "SQFT")
        if _pih.get("garage_sqft"):
            _rows += _sprow("home", "Garage",
                            f"{_pih['garage_sqft']:,}", "SQFT")
        _note = ""
        if _pih.get("sqft_source"):
            _note = esc(_pih["sqft_source"])
        try:
            import facts_edit as _fe
            _ov = _fe.overrides_for(nb.get("address"))
            _ovs = ", ".join(f"{k} = {v}" for k, v in _ov.items()
                             if not k.startswith("_"))
            if _ovs:
                _note += ((" · " if _note else "")
                          + f"office corrections on file: {_ovs}")
            _editor = _fe.editor_html(nb, stamp, back)
        except Exception:
            _editor = ""
        if _rows or _editor:
            specs_html = (
                f"<aside class='specrail'><div class='sptitle'>"
                f"{_svg_icon('info')}Site Specifications</div>"
                + (_rows or "<div class='subtext'>No house facts yet — "
                            "corrections welcome below.</div>")
                + (f"<div class='spnote'>{_note}</div>" if _note else "")
                + _editor + "</aside>")
        if nb.get("sched_pref"):
            chips += (f"<span class='chip blue'>📅 "
                      f"{esc(nb['sched_pref'][:60])}</span> ")
        if nb.get("tech_request"):
            chips += (f"<span class='chip purple'>👷 "
                      f"{esc(nb['tech_request'][:60])}</span> ")
        if nb.get("office_alert"):
            chips += (f"<span class='chip'>⚠ "
                      f"{esc(nb['office_alert'][:90])}</span> ")
        chips += other_homes_card(nb)
    q = quotes.get(stamp) if stamp else None
    jobber_bits = ""
    if nb and nb.get("customer_status"):
        jobber_bits += f" · {esc(nb['customer_status'])}"
    ident_links = ""
    if nb and nb.get("jobber_client_url"):
        ident_links += (f" <a href='{esc(nb['jobber_client_url'])}' "
                        f"target='_blank' rel='noopener' class='chip win' "
                        f"style='text-decoration:none'>👤 Jobber ↗</a>")
    if q:
        ident_links += " " + quote_chip(q, qurls)

    actionable = (nb and not nb.get("reviewed") and not sbs.get(stamp)
                  and not nb.get("dns_match") and stamp not in live_holds)
    mark_unread = ""
    if c["msgs"] or nb:
        if cur.get("unread"):
            mark_unread = (
                f"<form method='POST' action='/mark_done' "
                f"style='display:inline;margin-left:8px'>"
                f"<input type='hidden' name='addr' value='{esc(key)}'>"
                f"<input type='hidden' name='back' value='/'>"
                f"<button class='readbtn' style='background:var(--goldbg);"
                f"border-color:var(--gold);color:var(--goldink);font-weight:800'>"
                f"✓ Done — seen it</button></form>"
                # LaRee, Jul 10: a Spam button ON the inbox that the
                # program learns from (email senders only — voicemail/
                # form pipes are refused by the handler too)
                + ((f"<form method='POST' action='/mark_spam' "
                    f"style='display:inline;margin-left:6px' "
                    f"onsubmit=\"return confirm('File this sender as spam"
                    f" — never show them again?')\">"
                    f"<input type='hidden' name='stamp' value='{stamp or ''}'>"
                    f"<input type='hidden' name='sender' "
                    f"value='{esc(nb.get('from') if nb else c['email'] or '')}'>"
                    f"<button class='readbtn' style='color:var(--alarm)'>"
                    f"🚫 Spam</button></form>")
                   if (c.get('email') and not c.get('vm')) else "")
                # Jessica, Jul 9: an explicit way to hand it back when
                # you have to step away mid-review — stays bold for all
                + f"<form method='POST' action='/step_away' "
                f"style='display:inline;margin-left:6px'>"
                f"<input type='hidden' name='addr' value='{esc(key)}'>"
                f"<input type='hidden' name='stamp' "
                f"value='{stamp or ''}'>"
                f"<button class='readbtn' title='Releases it — stays "
                f"bold, next person picks it up'>🚶 Stepping away"
                f"</button></form>")
        else:
            mark_unread = (
                f"<form method='POST' action='/msg_unread' "
                f"style='display:inline;margin-left:8px'>"
                f"<input type='hidden' name='addr' value='{esc(key)}'>"
                f"<input type='hidden' name='back' value='/'>"
                f"<button class='readbtn'>↩ mark unread</button></form>")
    actions = ""
    if actionable:
        actions = f"""
  <div class='actions'>
   <form method='POST' action='/review' style='flex:2;min-width:220px'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='customer' value='{esc(nb.get("from") or "")}'>
    <input type='hidden' name='back' value='{esc(back)}'>
    <button name='action' value='approve' class='big' style='width:100%'>
     ✓ Price is right — approve</button>
   </form>
   <button class='gray' style='flex:1' onclick="tg('parkbox')">⏸ Not now</button>
   <button class='gray' style='flex:1' onclick="tg('helpbox')">🙋 Help</button>
  </div>
  <div id='parkbox' style='display:none;margin-top:8px;background:var(--soft);
       border-radius:10px;padding:10px'>
   <form method='POST' action='/hold'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='customer' value='{esc(nb.get("from") or "")}'>
    <input type='hidden' name='back' value='/'>
    <select name='hold_reason'>{''.join(f"<option value='{r}'>{r.replace('_', ' ')}</option>" for r in HOLD_REASONS)}</select>
    until <input type='date' name='hold_until' id='holddate'>
    <button type='button' class='gray' onclick='qh(7)'>+1wk</button>
    <button type='button' class='gray' onclick='qh(14)'>+2wk</button>
    <button type='button' class='gray' onclick="qh('aug')">Aug 1</button>
    <button class='gray' style='font-weight:700'>⏸ Park it</button>
   </form></div>
  <div id='helpbox' style='display:none;margin-top:8px;background:var(--soft);
       border-radius:10px;padding:10px'>
   <form method='POST' action='/escalate' style='margin-bottom:6px'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='customer' value='{esc(nb.get("from") or "")}'>
    <input type='hidden' name='address' value='{esc(nb.get("address") or "")}'>
    <input type='hidden' name='back' value='/'>
    <input type='text' name='question' placeholder='your question, one line'>
    <button style='background:#6d28d9'>🔍 Ask the office</button>
   </form>
   <form method='POST' action='/flag_review'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='customer' value='{esc(nb.get("from") or "")}'>
    <input type='hidden' name='total' value='{d.get("total") or ""}'>
    <input type='hidden' name='back' value='/'>
    <button style='background:var(--gold);color:#1c2b23'>🚩 Still stuck —
     email Dallon &amp; Tom</button>
   </form>
   <form method='POST' action='/idea_send' style='margin-top:10px;
        border-top:1px dashed var(--line);padding-top:10px'>
    <input type='hidden' name='context'
     value='while working on {esc((nb.get("from") or "").split("<")[0].strip())} — open them: https://masterbutler-dashboard.onrender.com/?c={urllib.parse.quote(key)}'>
    <input type='hidden' name='back' value='{esc(back)}'>
    <input type='text' name='text' placeholder='💡 Idea to make this better? Tell Dallon &amp; Claude — one line is plenty'>
    <button class='gray'>💡 Send the idea</button>
    <div class='subtext' style='margin-top:3px'>Emails Dallon instantly;
     Claude reads every idea overnight and pre-plans the fix.</div>
   </form></div>"""
    else:
        # 🙋 HELP NEVER DISAPPEARS (Jessica, Jul 15: 'the help button
        # disappears for me after I approve the quote') — approved/
        # handled cards keep a slim escalate + idea row.
        actions = f"""
  <div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:10px'>
   <form method='POST' action='/escalate' style='flex:1;min-width:200px'>
    <input type='hidden' name='stamp' value='{stamp}'>
    <input type='hidden' name='customer' value='{esc(nb.get("from") or "") if nb else ""}'>
    <input type='hidden' name='back' value='/'>
    <button class='gray' style='width:100%'>🙋 Help — email Dallon &amp;
     Tom about this one</button>
   </form>
   <form method='POST' action='/idea_send' style='flex:2;min-width:240px;
        display:flex;gap:6px'>
    <input type='hidden' name='context'
     value='while working on {esc((nb.get("from") or "").split("<")[0].strip()) if nb else ""} — open them: https://masterbutler-dashboard.onrender.com/?c={urllib.parse.quote(key)}'>
    <input type='hidden' name='back' value='{esc(back)}'>
    <input type='text' name='text' style='flex:1' placeholder='💡 Idea or
 problem? Tells Dallon instantly'>
    <button class='gray'>💡 Send</button>
   </form>
  </div>"""

    # THE CUSTOMER'S EXISTING JOBBER QUOTE (Dallon, the Mia rule):
    # read the past, find the quote, put ALL of it in front of the
    # office — never draft a second one.
    oq = cur.get("oq")
    qno = cur.get("qno")
    quote_panel = ""
    if oq:
        st = (oq.get("status") or "").replace("_", " ")
        stc = ("var(--green2)" if oq.get("status") == "approved"
               else "#7a5300")
        qlines = "".join(
            f"<div style='display:flex;justify-content:space-between;"
            f"font-size:13px;padding:2px 0'><span>{esc(li['name'])}"
            f"</span><b>${(li.get('price') or 0):,.0f}</b></div>"
            for li in (oq.get("lines") or []))
        quote_panel = (
            f"<div style='background:var(--goldbg);border:1px solid "
            f"var(--gold);border-radius:12px;padding:12px 16px;"
            f"margin-top:10px'><div style='display:flex;justify-content:"
            f"space-between;align-items:center'><b>📋 Their Jobber quote "
            f"#{esc(oq['number'])}"
            + (f" <a href='{esc(oq['url'])}' target='_blank' "
               f"rel='noopener'>open ↗</a>" if oq.get("url") else "")
            + f"</b><span style='font-weight:800;color:{stc}'>"
            f"{esc(st)} · ${oq['total']}</span></div>{qlines}"
            f"<div class='subtext' style='margin-top:4px'>"
            + ("This quote was ARCHIVED — usually a postponement "
               "(read their conversation). Revive it in Jobber rather "
               "than quoting blind."
               if oq.get("status") == "archived" else
               "Their last job — context for whatever they're asking now."
               if oq.get("status") == "converted" else
               "Work from THIS quote — don't make a second one. Add "
               "lines in Jobber if they asked for more.")
            + "</div></div>")
    elif qno:
        quote_panel = (
            f"<div style='background:var(--goldbg);border:1px solid "
            f"var(--gold);border-radius:12px;padding:10px 16px;"
            f"margin-top:10px'><b>📋 Their Jobber quote:</b> "
            f"{quote_chip(qno, qurls)}"
            + (" <span style='font-weight:800;color:var(--green2)'>— "
               "APPROVED, schedule it 🎉</span>" if cur.get("won") else "")
            + "</div>")

    addr_line = ""
    if nb and nb.get("address"):
        _tax_svcs = ([s.get("name") for s in
                      (((nb.get("draft") or {}).get("bid") or {})
                       .get("services") or [])]
                     or (nb.get("services") or []))
        addr_line = (f"<a href='/property/{_slug(nb['address'])}'>"
                     f"{esc(nb['address'])}</a> "
                     + _tax_glance(nb["address"], _tax_svcs))
    elif c["email"]:
        addr_line = esc(c["email"])
    # HERO (Dallon's design + his photo rule): the house photo with the
    # price + confidence ON it — street view → best tech photo → aerial
    hero_html = ""
    try:
        if nb and (nb.get("address") or stamp):
            by_kind = {}
            for ref_, kind_, i_ in clouddb.photos_index(
                    _all_photo_refs(c)):
                if kind_ != "eml":
                    by_kind.setdefault(kind_, []).append((ref_, i_))
            hurl = None
            for kind_ in ("street", "jobber", "customer", "aerial"):
                if by_kind.get(kind_):
                    r0, i0 = by_kind[kind_][0]
                    hurl = f"/img/{r0}/{kind_}/{i0}"
                    break
            d_ = (nb.get("draft") or {})
            tt = _num(d_.get("total"))
            hero_lbl = "Estimated total"
            if not tt:
                # no engine draft — the office's live Jobber quote still
                # belongs ON the picture (Dallon, Jul 10 pm: 'put the
                # total price over the picture like in the stitch')
                tt = _num((nb.get("open_quote_ctx") or {}).get("total"))
                if tt:
                    hero_lbl = "Quote total — in Jobber"
            cf = _num(nb.get("confidence"))
            # the HOUSE always shows when we have its picture (Dallon,
            # Jul 10 pm: 'the home isn't coming up on top' — it was
            # gated behind a priced draft); the price overlay is the
            # optional part, not the photo
            if hurl:
                foot = ""
                if tt:
                    foot = (f"<div class='foot'><div class='lbl'>"
                            f"{hero_lbl}</div>"
                            f"<span class='pr tab'>${tt:,.0f}</span>"
                            + (f"<span class='cf'>● {cf:.0f}% confident"
                               f"</span>" if cf is not None
                               and hero_lbl == "Estimated total" else "")
                            + "</div>")
                elif nb.get("address"):
                    foot = (f"<div class='foot'><div class='lbl'>"
                            f"{esc(nb['address'])[:60]}</div></div>")
                hero_html = (
                    f"<div class='qhero'><img src='{esc(hurl)}' "
                    f"loading='lazy'><div class='shade'></div>"
                    f"{foot}</div>")
    except Exception:
        pass
    # MOVE ▾ (Dallon, Jul 12): the human override on the lane ladder —
    # filing only; never touches prices, the scoreboard, or Jobber
    move_html = (
        f"<div style='position:relative;display:inline-block'>"
        f"<button type='button' class='readbtn' style='font-size:12px;"
        f"padding:6px 12px' onclick=\"var m=document.getElementById("
        f"'movemenu');m.style.display=m.style.display==='none'?'':'none'\">"
        f"Move ▾</button>"
        f"<div id='movemenu' style='display:none;position:absolute;"
        f"right:0;left:auto;top:36px;z-index:500;background:#0d231b;"
        f"border:1px solid "
        f"rgba(201,162,39,.4);border-radius:12px;box-shadow:0 12px 30px "
        f"rgba(0,0,0,.5);min-width:235px;overflow:hidden'>"
        + "".join(
            f"<form method='POST' action='/move_lane' style='margin:0'>"
            f"<input type='hidden' name='key' value='{esc(key)}'>"
            f"<input type='hidden' name='lane' value='{lv}'>"
            f"<input type='hidden' name='back' value='{esc(back)}'>"
            f"<button style='display:block;width:100%;text-align:left;"
            f"background:none;border:0;border-top:1px solid "
            f"rgba(201,162,39,.12);padding:11px 15px;font-weight:700;"
            f"font-size:13px;color:var(--ink);cursor:pointer;margin:0;"
            f"border-radius:0'>{lt}<span style='display:block;"
            f"color:var(--mut);font-weight:500;font-size:10.5px'>{ls}"
            f"</span></button></form>"
            for lv, lt, ls in (
                ("declined", "🚫 Declined",
                 "they said no — the engine learns from it"),
                ("later", "⏰ Follow up later",
                 "parks it, resurfaces in a week"),
                ("needs_reply", "✉️ Needs reply",
                 "decided — still owe them words"),
                ("fixits", "🔧 Fix-it",
                 "it's about completed work — no bid"),
                ("done", "✓ Done",
                 "finished — off the queue"),
                ("auto", "↩ Let the system sort it",
                 "clears any manual filing")))
        + (f"<div style='border-top:1px solid rgba(201,162,39,.25);"
           f"padding:11px 15px'>"
           f"<form method='POST' action='/flag_customer' style='margin:0'>"
           f"<input type='hidden' name='email' value='{esc(c.get('email') or '')}'>"
           f"<input type='hidden' name='back' value='{esc(back)}'>"
           f"<div style='font-weight:700;font-size:13px;margin-bottom:6px'>"
           f"⚠️ Flag this customer</div>"
           f"<select name='label' style='width:100%;margin-bottom:6px;"
           f"padding:6px;border-radius:8px'>"
           f"<option value='bad_payer'>⚠️ Bad payer</option>"
           f"<option value='watch'>👀 Watch — be careful</option>"
           f"<option value='vip'>⭐ VIP — treat extra well</option>"
           f"<option value='realtor'>🏘 Realtor / property manager — "
           f"price per house</option></select>"
           f"<input name='note' placeholder='why — e.g. didn&#39;t pay for "
           f"July gutter job' style='width:100%;margin-bottom:6px;"
           f"padding:6px;border-radius:8px'>"
           f"<button style='width:100%;padding:7px;font-size:12.5px'>"
           f"Save flag — sticks to them forever</button></form></div>"
           if c.get("email") else "")
        + "</div></div>")
    pin_main = (f"{hero_html}"
                f"<div class='pin-top'><div>"
                f"<h2>{esc(c['name'] or c['email'] or '')}{mark_unread} "
                f"{move_html}</h2>"
                f"<div class='paddr'>{addr_line}{jobber_bits}{ident_links}"
                f"</div></div>"
                f"<div class='money'>{total_html}</div></div>"
                f"{say}{quote_panel}"
                + (f"<div class='pchips'>{chips}</div>" if chips else "")
                + actions)
    # picture + facts side by side (Stitch: specs live NEXT TO the
    # photo, not buried in a fold)
    pinned = (f"<div class='pinned'>{banners}"
              + (f"<div class='pingrid'><div>{pin_main}</div>"
                 f"{specs_html}</div>" if specs_html else pin_main)
              + "</div>")

    # ── folds ──
    def fold(title, peek, inner, open_=False, count=None):
        cnt = (f"<span class='fcount'>{count}</span>" if count else "")
        return (f"<details class='ifold' {'open' if open_ else ''}>"
                f"<summary>{title} {cnt}"
                f"<span class='peek'>{peek}</span></summary>"
                f"<div class='fbody'>{inner}</div></details>")

    folds = ""
    # line items (editable, same endpoint)
    if nb and bid_d.get("services"):
        hist = _history_entry(
            nb.get("address"),
            (d.get("customer") or {}).get("name")
            or (nb.get("from") or "").split("<")[0].strip()) or {}
        try:
            from store import _service_key
        except Exception:
            def _service_key(n):
                return None
        editable = actionable
        lines = ""
        for i, s in enumerate(bid_d["services"]):
            # sanitize this line's numbers once so a corrupt price
            # (string/None) can't crash the format calls below (Jul 10)
            s = dict(s, price=(_num(s.get("price")) or 0))
            for _k in ("low", "high", "orig_price"):
                if s.get(_k) is not None:
                    s[_k] = _num(s[_k])
            past = hist.get(_service_key(s["name"]) or "") or []
            recent = sorted(past, reverse=True)[:2]
            cells = " · ".join(f"{dt[:7]} ${p:,.0f}" for dt, p in recent)
            was = (f"<div class='subtext' style='font-size:10.5px'>system "
                   f"said ${s['orig_price']:,.0f}</div>"
                   if s.get("orig_price") not in (None, s["price"]) else "")
            # step='any' + true value (LaRee, Jul 15: the $14.50 moss
            # product made step='5' invalid, so the browser refused the
            # WHOLE save — "change the moss pricing to $15" — and the
            # .0f render was silently shaving the 50¢ to '14')
            pc = (f"<td class='num'>$<input type='number' name='p_{i}' "
                  f"value='{s['price']:g}' step='any' min='0' "
                  f"style='width:76px;text-align:right;font-weight:700;"
                  f"border:1px solid var(--line);border-radius:6px;"
                  f"padding:4px'>{was}</td>" if editable
                  else f"<td class='num'>${s['price']:,.0f}</td>")
            rng = (f"<div class='subtext' style='font-size:10.5px'>"
                   f"range ${s['low']:,.0f}–${s['high']:,.0f}</div>"
                   if s.get("low") and s.get("high")
                   and s["low"] != s["high"] else "")
            # ✕ remove a line (LaRee, Jul 13: 'can't delete line items') —
            # checked = dropped on save; the note records who removed what
            rmc = (f"<td style='text-align:center'><label class='subtext' "
                   f"style='font-size:10px;cursor:pointer'>"
                   f"<input type='checkbox' name='rm_{i}' "
                   f"style='vertical-align:middle'> ✕</label></td>"
                   if editable else "<td></td>")
            lines += (f"<tr><td>{esc(s['name'])}{rng}</td>{pc}"
                      f"<td class='subtext'>{cells or '—'}</td>{rmc}</tr>")
        reason_chips = "".join(
            f"<button type='button' class='reason' onclick=\""
            f"document.getElementById('ireason').value='{r}';"
            f"document.querySelectorAll('#ipc .reason').forEach("
            f"x=>x.classList.remove('sel'));this.classList.add('sel')\">"
            f"{r.replace('_', ' ')}</button>" for r in REASONS)
        edit_ctl = (f"<input type='hidden' id='ireason' name='reason' "
                    f"value=''><div style='margin:6px 0'>{reason_chips}"
                    f"</div><button class='gray' style='font-weight:700'>"
                    f"💾 Save my prices</button>" if editable else "")
        inner = (f"<form method='POST' action='/edit_prices' id='ipc'>"
                 f"<input type='hidden' name='stamp' value='{stamp}'>"
                 f"<input type='hidden' name='customer' "
                 f"value='{esc(nb.get('from') or '')}'>"
                 f"<input type='hidden' name='back' value='{esc(back)}'>"
                 f"<table><tr><th>Service</th><th class='num'>Price</th>"
                 f"<th>Past here</th><th style='font-size:10px'>remove"
                 f"</th></tr>{lines}</table>{edit_ctl}</form>"
                 + add_service_card(nb, back=back))
        folds += fold("Line items",
                      "tap a price to fix it" if editable else "as decided",
                      inner, open_=not convo_open,
                      count=len(bid_d["services"]))
    elif nb:
        folds += fold("Line items", "no priced draft — office quotes this",
                      f"<div class='subtext'>{esc((nb.get('pipeline_output') or '')[-300:])}</div>"
                      + add_service_card(nb, back=back))

    # HOW IT WAS PRICED (Jessica, Jul 9: 'classic bid view is better for
    # the two prices, measurements used etc — combine them, do away with
    # the separate pages'): the classic page's pricing detail, as a fold
    if nb and bid_d.get("services"):
        pi_x = d.get("prop_info") or {}
        priced_inner = pricing_explainer_card(pi_x)
        _all_notes = (" ".join(bid_d.get("notes") or [])
                      + " " + (nb.get("pipeline_output") or ""))
        m2 = re.search(r"DRY-DAY OPTION[^:]*: roof lane \$(\d+)[^$]*\$(\d+)",
                       _all_notes)
        if m2:
            dry, std = m2.group(1), m2.group(2)
            priced_inner += f"""<div style='border-left:4px solid
  var(--green2);background:var(--soft);border-radius:12px;
  padding:12px 16px;margin-top:10px'>
  <b>Two prices — customer's choice</b>
  <div style='display:flex;gap:26px;margin-top:6px'>
   <div><div style='color:var(--mut);font-size:11px;
     text-transform:uppercase'>Their date (standard)</div>
    <div style='font-size:22px;font-weight:800'>${std}</div></div>
   <div><div style='color:var(--mut);font-size:11px;
     text-transform:uppercase'>Our dry day (flexible)</div>
    <div style='font-size:22px;font-weight:800;color:var(--green2)'>
     ${dry}</div></div></div>
  <div class='subtext' style='margin-top:6px'>Standard is the true price
   for records. Offer the dry-day price on a price objection ONLY —
   if they take it, hold it weather-pending.</div></div>"""
        surf = pi_x.get("aerial_surfaces") or {}
        if surf:
            priced_inner += ("<div class='subtext' style='margin-top:8px'>"
                             "📐 Aerial-measured: " + " · ".join(
                                 f"{k} ~{v:,.0f} sqft"
                                 for k, v in surf.items()) + "</div>")
        if priced_inner:
            # (the fix-the-facts editor moved UP into the Site
            # Specifications rail beside the photo, Jul 10 pm)
            folds += fold("How it was priced",
                          "size · multipliers · two prices",
                          priced_inner)

    # photos & flyover
    if nb:
        gallery = ""
        if clouddb.available():
            # tech note text rides under Jobber photos (LaRee, Jul 10:
            # 'photo notes need to port WITH the photos')
            try:
                _pcaps = clouddb.get_blob("photo_captions") or {}
            except Exception:
                _pcaps = {}
            for ref, kind, idx in clouddb.photos_index(
                    _all_photo_refs(c)):
                if kind == "eml":
                    continue
                lbl = {"aerial": ("Aerial", "#1e8449"),
                       "street": ("Street", "#1d4ed8"),
                       "customer": ("Customer", "#8a5a00")}.get(
                           kind, (kind.title(), "#6b7280"))
                _cap = ""
                if kind == "jobber":
                    _ct = (_pcaps.get(ref) or {}).get(str(idx))
                    if _ct:
                        _cap = (f"<span style='display:block;width:170px;"
                                f"font-size:10.5px;color:var(--mut);"
                                f"line-height:1.4;padding:3px 2px 0;"
                                f"white-space:normal'>📝 "
                                f"{esc(_ct)[:120]}</span>")
                gallery += (
                    f"<a href='/img/{ref}/{kind}/{idx}' target='_blank' "
                    f"style='position:relative;display:inline-block;"
                    f"margin:4px;vertical-align:top;text-decoration:none'>"
                    f"<img src='/img/{ref}/{kind}/{idx}' "
                    f"style='width:170px;height:108px;object-fit:cover;"
                    f"border-radius:10px;border:2px solid {lbl[1]}55'>"
                    f"<span style='position:absolute;top:6px;left:6px;"
                    f"background:{lbl[1]};color:#fff;font-size:9px;"
                    f"font-weight:800;padding:2px 7px;border-radius:6px'>"
                    f"{lbl[0]}</span>{_cap}</a>")
        extra = ""
        if nb.get("address"):
            try:
                from aerial_view import listing_links
                extra = (f"<div style='margin-top:8px'>"
                         f"<a href='/flyover?addr="
                         f"{urllib.parse.quote(nb['address'])}' "
                         f"target='_blank' class='chip' style='background:"
                         f"#e5edff;color:#1d4ed8;font-weight:700;"
                         f"text-decoration:none'>🎥 3D flyover</a> "
                         + "".join(
                             f"<a href='{esc(u)}' target='_blank' "
                             f"rel='noopener' class='chip' "
                             f"style='text-decoration:none'>🏠 {n} ↗</a> "
                             for n, u in listing_links(nb["address"]))
                         + "</div>")
            except Exception:
                pass
        folds += fold("Photos &amp; flyover", "aerial · street · 🎥",
                      (gallery or "<div class='subtext'>no photos yet"
                       "</div>") + extra)

    # conversation & reply — emails AND voicemails in one thread (LaRee,
    # Jul 10: 'voicemails belong in the conversation history just like
    # emails — that's critical customer info'). Full message, expandable.
    convo = []
    for m_ in (c["msgs"] or []):
        convo.append((m_["at"], m_["dir"],
                      (m_.get("name") or c["name"] or "Customer"),
                      m_.get("by"),
                      msglog.clean_body(m_.get("body") or "")
                      or m_.get("subject") or "",
                      m_.get("subject") or ""))
    seen_bodies = {(b or "")[:80] for _, _, _, _, b, *_ in convo}
    for b2 in c["bids"]:
        nm = b2.get("newest_message") or ""
        is_vm = bool(b2.get("lead")) or b2.get("kind") == "phone_lead" \
            or "🎙" in nm
        if is_vm and nm[:80] not in seen_bodies:
            seen_bodies.add(nm[:80])
            convo.append((_stamp_utc(b2["stamp"]), "in",
                          "☎ " + (c["name"] or "Voicemail"), None,
                          nm or "☎ Voicemail — dial in to hear it", ""))
    convo.sort(key=lambda x: x[0])
    bubbles = ""
    # THE SANDWICH FIX (Dallon, Jul 14: the office asks him about 3
    # different homes in ONE thread — 'how do they know what price I
    # said to their question'). On INTERNAL threads, every topic change
    # gets a divider with the subject, and a quote# in the subject
    # links straight to that customer's card. Answers stop floating.
    _internal = bool(c.get("internal"))
    _byq = {}
    if _internal:
        for _b3 in load_bids():
            _q3 = str(((_b3.get("open_quote_ctx") or {}).get("number")
                       or ""))
            if _q3 and _q3 not in _byq:
                _m3 = re.search(r"<([^>]+)>", _b3.get("from") or "")
                _byq[_q3] = ((_b3.get("client_name")
                              or (_b3.get("from") or "").split("<")[0])
                             .strip()[:28],
                             _m3.group(1) if _m3 else "")
    _last_subj = None
    for at, dr, who_, by_, body_, subj_ in convo[-12:]:
        if _internal:
            _ns = re.sub(r"^\s*((re|fwd?)\s*:\s*)+", "",
                         (subj_ or ""), flags=re.I).strip().lower()
            if _ns and _ns != _last_subj:
                _last_subj = _ns
                _qm = re.search(r"#?\s?(3[0-9]{4})",
                                subj_ + " " + body_[:120])
                _who_link = ""
                if _qm and _qm.group(1) in _byq:
                    _cn2, _ce2 = _byq[_qm.group(1)]
                    _who_link = (f" · <a href='/?c="
                                 f"{urllib.parse.quote(_ce2)}' "
                                 f"style='font-weight:800'>→ {esc(_cn2)}"
                                 f"</a>" if _ce2 else f" · {esc(_cn2)}")
                bubbles += (
                    f"<div style='display:flex;align-items:center;"
                    f"gap:10px;margin:18px 0 8px'>"
                    f"<div style='flex:1;height:2px;"
                    f"background:rgba(201,162,39,.35)'></div>"
                    f"<div style='background:rgba(201,162,39,.14);"
                    f"border:1px solid rgba(201,162,39,.4);"
                    f"border-radius:20px;padding:5px 16px;font-size:13px;"
                    f"font-weight:800;color:var(--goldink);"
                    f"white-space:nowrap;max-width:75%;overflow:hidden;"
                    f"text-overflow:ellipsis'>📋 {esc(_ns[:60])}"
                    f"{_who_link}</div>"
                    f"<div style='flex:1;height:2px;"
                    f"background:rgba(201,162,39,.35)'></div></div>")
        inn = dr == "in"
        who = esc(who_) if inn else \
            ("Master Butler" + ((" · " + esc(by_)) if by_ else ""))
        bubbles += (
            f"<div style='display:flex;justify-content:"
            f"{'flex-start' if inn else 'flex-end'};margin:5px 0'>"
            f"<div style='max-width:82%;padding:8px 12px;border-radius:12px;"
            f"font-size:13px;"
            f"{'background:var(--soft)' if inn else 'background:#0b3d2e;color:#eef4f0'}'>"
            f"<div style='font-size:10px;font-weight:700;opacity:.6'>"
            f"{who} · {esc(_pt(at))}</div>"
            f"{_expandable(body_)}"
            f"</div></div>")
    if bubbles:
        bubbles += _MSG_MORE_JS
    reply_ui = ""
    if c["email"]:
        _cn_json = _canned_payload()
        last_subject = next((m_.get("subject") for m_ in
                             reversed(c["msgs"] or []) if m_.get("subject")),
                            "") or (nb.get("subject") if nb else "") or ""
        reply_subject = (last_subject if last_subject.lower()
                         .startswith("re:") else f"Re: {last_subject}"
                         if last_subject else "Master Butler")
        # STAGE 2 PRE-FILL (Tom's ask; Dallon's go, Jul 14): a genuine
        # customer inbound of a SAFE type arrives with the reply already
        # in the box — office edits anything, then sends. The ✨ button
        # and quick responses still override; every send is graded.
        _pre = None
        if not draft:
            try:
                import autorespond as _ar
                _cand = _ar.build_draft(
                    nb, c["msgs"], user,
                    _blob_rw("office_voice", {}) or {},
                    auto=_blob_rw("reply_templates_auto", {}) or {})
                if _cand and _cand["type"] in ("thanks_ack",
                                               "date_confirm",
                                               "approve_wants_date"):
                    _pre = _cand
            except Exception:
                _pre = None
        _box_text = draft or (_pre or {}).get("draft", "")
        _pre_banner = (
            "<div style='background:rgba(201,162,39,.12);color:"
            "var(--goldink);font-size:12px;font-weight:800;"
            "border-radius:8px;padding:6px 10px;margin-bottom:6px'>"
            "✨ DRAFT READY — written the way the office writes "
            f"({esc(_pre['why'])}). Edit anything, then send.</div>"
            if _pre else "")
        _send_note = ("Sends as customercare@ · your edits teach the "
                      "brain" if REPLIES_ENABLED else
                      "Sending stays locked until Dallon flips it on.")
        _send_btn = (
            f"<button class='big' onclick=\"return confirm("
            f"'Send this reply to {esc(c['email'])}?')\">Send reply"
            f"</button>" if REPLIES_ENABLED else
            "<button class='big' type='button' onclick=\"alert('Sending "
            "is switched OFF while we test — copy the text into Gmail "
            "for now.')\">Send reply</button>")
        reply_ui = f"""
 <div style='border-top:1px solid var(--line);margin-top:8px;padding-top:8px'>
  <div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px'>
   <form method='POST' action='/msg_draft' style='display:inline'>
    <input type='hidden' name='to' value='{esc(c["email"])}'>
    <input type='hidden' name='back' value='inbox:{esc(key)}'>
    <button class='gray' style='border-color:var(--gold);color:var(--goldink)'>
     ✨ Draft a reply for me</button>
   </form>
   <select id='inboxcanned' style='max-width:280px'>
    <option value=''>Quick responses…</option></select>
  </div>
  {_pre_banner}
  <form method='POST' action='/msg_send'>
   <input type='hidden' name='to' value='{esc(c["email"])}'>
   <input type='hidden' name='subject' value='{esc(reply_subject)}'>
   <input type='hidden' name='back' value='{esc(back)}'>
   <input type='hidden' name='prefill_kind' value='{esc((_pre or {}).get("type",""))}'>
   <input type='hidden' name='prefill' value='{esc((_pre or {}).get("draft",""))}'>
   <textarea id='inboxreply' name='body' rows='3' style='min-height:76px'
    placeholder='Reply to {esc(c["email"])}'>{esc(_box_text)}</textarea>
   <div style='display:flex;justify-content:space-between;align-items:center;
        margin-top:6px'>
    <span class='subtext'>{_send_note}</span>
    {_send_btn}
   </div></form></div>
<script>
{_CANNED_MERGE_JS}
var IC = mergeCanned({_cn_json});
var _is = document.getElementById('inboxcanned');
Object.keys(IC).forEach(function(k){{
  var o = document.createElement('option'); o.value = k; o.textContent = k;
  _is.appendChild(o);
}});
_is.onchange = function(){{
  if (!_is.value) return;
  var t = document.getElementById('inboxreply');
  t.value = IC[_is.value]; t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight + 6, 420) + 'px'; t.focus();
}};
</script>"""
    folds += fold("Conversation &amp; reply",
                  "quick responses · ✨ draft",
                  (bubbles or "<div class='subtext'>No messages logged — "
                   "replying starts the thread.</div>") + reply_ui,
                  open_=convo_open, count=len(convo) or None)

    # history & must-know
    if nb and nb.get("address"):
        prior = property_history(nb.get("address"), stamp)
        hist_rows = "".join(
            f"<div>🏠 <a href='/bid/{s}'>{esc(r.get('from'))[:34]}</a> — "
            f"{esc(r.get('kind'))}, {s[:8]}</div>" for s, r in prior[:5]) \
            if prior else ""
        mk = get_must_know(nb.get("address"))
        mk_form = (f"<form method='POST' action='/must_know' "
                   f"style='margin-top:8px'>"
                   f"<input type='hidden' name='stamp' value='{stamp}'>"
                   f"<input type='hidden' name='address' "
                   f"value='{esc(nb.get('address'))}'>"
                   f"<input type='hidden' name='back' value='{esc(back)}'>"
                   f"<input type='text' name='text' value='{esc(mk)}' "
                   f"placeholder='Must Know for this home (gate code, dog…)'>"
                   f"<button class='gray' style='margin-top:4px'>Save "
                   f"Must Know</button></form>")
        folds += fold("History at this home",
                      ("📌 " + esc(mk[:50])) if mk else "past visits · Must Know",
                      (service_history_card(nb.get("address"),
                       (d.get("customer") or {}).get("name")
                       or (nb.get("from") or "").split("<")[0].strip())
                       or "") + hist_rows + mk_form)

    # warnings
    if nb:
        notes = re.findall(r"⚠ ?(.+)", nb.get("pipeline_output", ""))
        notes += [n for n in (bid_d.get("notes") or [])
                  if n not in notes][:10]
        n_html = "".join(
            f"<div style='display:flex;gap:8px;align-items:flex-start;"
            f"color:var(--goldink);padding:3px 0'><span>⚠</span>"
            f"<span>{esc(n)}</span></div>" for n in notes[:14]) or \
            "<div class='subtext'>(no warnings)</div>"
        folds += fold("Warnings", notes[0][:60] if notes else "none",
                      n_html, count=len(notes) or None)

    # more actions
    if nb:
        more = f"""
  <form method='POST' action='/photo_request' style='display:inline'>
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='customer' value='{esc(nb.get("from") or "")}'>
   <input type='hidden' name='services' value='{','.join(nb.get('services') or [])}'>
   <input type='hidden' name='back' value='/'>
   <button class='gray'>Draft photo-request</button></form>
  {(f"""<form method='POST' action='/mark_spam' style='display:inline'
    onsubmit="return confirm('File this sender as spam — never show them again?')">
   <input type='hidden' name='stamp' value='{stamp}'>
   <input type='hidden' name='sender' value='{esc(nb.get("from") or "")}'>
   <button class='gray'>🚫 Spam</button></form>""")
   if not c.get('vm') else ''}
"""
        # classic /bid link RETIRED from the UI (LaRee, Jul 10: 'there
        # should not be a classic customer page — combine them'); the
        # route still answers for old deep links, but nothing points
        # there anymore.
        folds += fold("More", "photo request · spam", more)

    scripts = """
<script>
function tg(id){var e=document.getElementById(id);
  e.style.display = e.style.display==='none'?'block':'none';}
function qh(d){
  var t = new Date();
  if (d === 'aug') { t = new Date(t.getFullYear() + (t.getMonth() >= 7 ? 1 : 0), 7, 1); }
  else { t.setDate(t.getDate() + d); }
  document.getElementById('holddate').value = t.toISOString().slice(0,10);
}
</script>"""
    # ── TECH CONVERSATION (Dallon, Jul 13): a tech email is an INTERNAL
    # pricing Q&A ("Mark, how much for the driveway now the patio's
    # done?"), NOT a customer bid. Never show the house/specs/approve
    # apparatus — show the conversation, and a best-effort link to the
    # customer it's about so the office keeps the thread straight.
    if nb and nb.get("tech_sender"):
        about = _tech_about(nb)
        about_html = (
            f"<a href='{about[1]}' style='display:inline-flex;"
            f"align-items:center;gap:6px;background:rgba(201,162,39,.14);"
            f"border:1px solid rgba(201,162,39,.4);border-radius:999px;"
            f"padding:6px 14px;color:var(--goldink);font-weight:800;"
            f"font-size:13px;text-decoration:none'>📎 About: "
            f"{esc(about[0])} — open their card ↗</a>"
            if about else
            "<div class='subtext'>🔍 Not sure which customer — use the "
            "search up top to open their card, then handle the pricing "
            "there.</div>")
        tech_card = (
            f"<div class='pinned'>"
            f"<div style='display:flex;align-items:center;gap:10px;"
            f"flex-wrap:wrap'>"
            f"<h2 style='margin:0'>👷 {esc(nb.get('tech_sender'))}</h2>"
            f"<span style='background:#3a2a5a;color:#cdbaf5;border:1px "
            f"solid #6d28d9;border-radius:999px;padding:3px 12px;"
            f"font-size:11px;font-weight:800'>TECH — internal, not a "
            f"bid</span>"
            f"<span class='readbtn' style='margin-left:auto'>"
            f"Move ▾</span></div>"
            f"<div class='subtext' style='margin:6px 0 12px'>A pricing "
            f"question or field note from your tech — answer it like a "
            f"chat. The system never bids to a tech.</div>"
            f"<div style='margin-bottom:12px'>{about_html}</div>"
            # PICTURES RIDE THE TECH CARD (Dallon, Jul 14: 'the email
            # comes in, bringing all pertinent data with it') — every
            # photo from this tech's emails, right above the chat
            + (lambda _ph: (
                "<div style='margin-bottom:12px'><div class='subtext' "
                "style='font-weight:800;font-size:11px;text-transform:"
                "uppercase;letter-spacing:1px;margin-bottom:4px'>📷 "
                "Photos they sent</div>"
                + "".join(
                    f"<a href='/img/{_r}/{_k}/{_i}' target='_blank'>"
                    f"<img src='/img/{_r}/{_k}/{_i}' "
                    f"style='width:170px;height:108px;object-fit:cover;"
                    f"margin:4px;border-radius:10px'></a>"
                    for _r, _k, _i in _ph)
                + "</div>") if _ph else "")(
                [(_r, _k, _i) for _r, _k, _i in
                 (clouddb.photos_index(_all_photo_refs(c))
                  if clouddb.available() else [])
                 if _k != "eml"])
            + f"<div style='background:rgba(17,41,33,.5);border:1px solid "
            f"rgba(201,162,39,.14);border-radius:12px;padding:10px 8px'>"
            f"{bubbles or '<div class=subtext>No message body.</div>'}"
            f"</div>{reply_ui}</div>")
        return f"{tech_card}{scripts}"

    return f"{pinned}<div class='ifolds'>{folds}</div>{scripts}"


def customers_tab_page(sel=None, q="", user=None, draft=""):
    """👥 CUSTOMERS — the office's file cabinet (Dallon's spec, Jul 9pm:
    separate tab; profiles matched like quoting — address + names +
    emails; several people at one address SHARE a profile and either
    name finds it; everything reads like a text thread, oldest at top,
    newest at bottom; photos + info up top)."""
    import customers as cst
    import msglog

    # bids that describe real customers (no spam/robots/internal/techs)
    _skip = (list(_internal_senders()) + list(_learned_spam())
             + list(NOISE_SENDERS))
    rows = []
    for b in load_bids():
        if b.get("spam_auto") or b.get("tech_sender"):
            continue
        sender = (b.get("from") or "").lower()
        if any(s and s in sender for s in _skip):
            continue
        rows.append(b)
    import spam_filter
    threads = [(a, n, ms) for a, n, ms in msglog.threads()
               if not any(s and s in a for s in
                          _skip + list(spam_filter.KNOWN_SPAM_DOMAINS))]
    profiles, by_email = cst.build_profiles(rows, threads)

    # freshest activity per profile (records give stamps; threads/hist
    # already set 'last')
    for p in profiles.values():
        for s in p["stamps"]:
            p["last"] = max(p["last"], _stamp_utc(s))
    roster = sorted((p for p in profiles.values() if cst.matches(p, q)),
                    key=lambda p: p["last"], reverse=True)

    # ── left: search + roster ──
    lst = (f"<form method='GET' action='/customers' style='padding:2px "
           f"4px 10px'><input name='q' value='{esc(q)}' placeholder='🔎 "
           f"Search any name, address, email, phone…' style='width:100%;"
           f"padding:8px 12px;border-radius:9px;border:1px solid "
           f"var(--line);background:var(--card);color:var(--ink);"
           f"font-size:13.5px'></form>"
           f"<div class='subtext' style='padding:0 6px 8px'>"
           f"{len(roster)} customer file(s)"
           + (f" matching “{esc(q)}”" if q else "") + "</div>")
    for p in roster[:250]:
        active = p["key"] == sel
        nm = (" & ".join(p["names"][:3])
              or (p["emails"][0] if p["emails"] else "")
              or ("☎ " + p["phones"][0] if p["phones"] else "(no name)"))
        box = "background:var(--soft);outline:2px solid var(--green2);" \
            if active else ""
        lst += (
            f"<a href='/customers?c={urllib.parse.quote(p['key'])}"
            + (f"&q={urllib.parse.quote(q)}" if q else "") + "' "
            f"class='irow' style='{box}'>"
            f"<div class='nm' style='font-weight:700'>{esc(nm)[:38]}</div>"
            f"<div class='pv'>{esc(p.get('addr') or p['emails'][0] if p['emails'] else '')[:52]}</div>"
            f"<div class='meta'><span class='word'>"
            f"{len(p['stamps'])} request(s)</span>"
            f"<span class='iage'>{esc(_pt(p['last'])[:12]) if p['last'] else ''}</span>"
            f"</div></a>")
    if len(roster) > 250:
        lst += (f"<div class='subtext' style='padding:8px'>…and "
                f"{len(roster)-250} more — narrow the search.</div>")

    # ── right: one customer's whole file ──
    if sel and sel not in profiles:
        sel = by_email.get((sel or "").lower()) or sel   # email deep-link
    if not sel or sel not in profiles:
        detail = ("<div style='display:flex;align-items:center;"
                  "justify-content:center;min-height:420px;flex:1;"
                  "color:var(--mut)'><div style='text-align:center'>"
                  "<div style='font-size:36px'>👥</div><b>Pick a customer "
                  "file</b><div class='subtext'>Search works on any name "
                  "at the address — spouses share one file.</div>"
                  "</div></div>")
    else:
        p = profiles[sel]
        recs = {b["stamp"]: b for b in rows}
        newest = next((recs[s] for s in sorted(p["stamps"], reverse=True)
                       if s in recs), None)

        # header: names, address, contacts, house facts, must-know
        names_h = " &amp; ".join(esc(n) for n in p["names"][:4]) \
            or esc(p["emails"][0] if p["emails"] else sel)
        addr_h = ""
        if p.get("addr"):
            addr_h = (f"<a href='/property/{_slug(p['addr'])}'>"
                      f"{esc(p['addr'])}</a> " + _tax_glance(p["addr"]))
        contacts = " · ".join(esc(x) for x in (p["emails"][:3]
                                               + p["phones"][:2]))
        jb = (f" <a class='chip win' style='text-decoration:none' "
              f"href='{esc(p['jobber_url'])}' target='_blank' "
              f"rel='noopener'>👤 Jobber ↗</a>" if p.get("jobber_url")
              else "")
        # ONE CLICK from the file to the money (Dallon, Jul 9 pm:
        # 'if you search in customers, and then want to bust over to
        # the bid, you can do it in a click')
        quotes = quote_numbers()
        oq_ = next((recs[s].get("open_quote_ctx")
                    for s in sorted(p["stamps"], reverse=True)
                    if s in recs and recs[s].get("open_quote_ctx")), None)
        qno_ = next((quotes.get(s)
                     for s in sorted(p["stamps"], reverse=True)
                     if quotes.get(s)), None)
        if oq_:
            st_ = (oq_.get("status") or "").replace("_", " ")
            jb += (f" <a class='chip win' style='text-decoration:none' "
                   + (f"href='{esc(oq_['url'])}' target='_blank' "
                      f"rel='noopener'" if oq_.get("url") else "")
                   + f">📋 Open quote #{esc(oq_['number'])} · {esc(st_)}"
                   f" · ${oq_['total']} ↗</a>")
        elif qno_:
            jb += " " + quote_chip(qno_, quote_urls())
        if newest:
            _lead = newest.get("lead") or {}
            if p["emails"]:
                bidkey = p["emails"][0]
            elif _lead or newest.get("phone"):
                bidkey = ("vm:" + (_lead.get("caller")
                                   or newest.get("phone") or "?")
                          + "|" + (_lead.get("when") or newest["stamp"]))
            else:
                bidkey = "stamp:" + newest["stamp"]
            jb += (f" <a class='chip blue' style='text-decoration:none' "
                   f"href='/?c={urllib.parse.quote(bidkey)}'>📥 Open on "
                   f"Bids</a>")
        facts = ""
        _pi = ((newest or {}).get("draft") or {}).get("prop_info") or {}
        for f_ in (f"{_pi['sqft']:,} sqft" if _pi.get("sqft") else None,
                   f"{_pi['stories']} story" if _pi.get("stories") else None,
                   f"{_pi['roof_material']} roof"
                   if _pi.get("roof_material") not in (None, "standard")
                   else None):
            if f_:
                facts += f"<span class='chip blue'>🏠 {esc(f_)}</span> "
        mk = get_must_know(p.get("addr")) if p.get("addr") else None

        # photos strip — every ref this profile owns
        gallery = ""
        if clouddb.available():
            refs = list(dict.fromkeys(
                (_photo_refs(None, p.get("addr")) if p.get("addr") else [])
                + p["stamps"]))
            for ref, kind, idx in clouddb.photos_index(refs):
                if kind == "eml":
                    continue
                gallery += (f"<a href='/img/{ref}/{kind}/{idx}' "
                            f"target='_blank'><img src='/img/{ref}/{kind}/"
                            f"{idx}' style='width:120px;height:78px;"
                            f"object-fit:cover;border-radius:8px;margin:3px;"
                            f"border:1px solid var(--line)' "
                            f"title='{esc(kind)}'></a>")

        # THE THREAD — history port + live log + bid milestones, merged,
        # oldest at top (Dallon: 'like reading a text message')
        tl = []
        seen_m = set()

        def add_msg(at, dr, body, subject=""):
            k = ((at or "")[:16], dr, (body or "")[:80])
            if k in seen_m or not (body or subject):
                return
            seen_m.add(k)
            inn = dr == "in"
            tl.append((at, (
                f"<div style='display:flex;justify-content:"
                f"{'flex-start' if inn else 'flex-end'}'>"
                f"<div style='max-width:74%;margin:3px 0;padding:8px 13px;"
                f"border-radius:14px;font-size:13.5px;white-space:pre-wrap;"
                + ("background:var(--soft);border:1px solid var(--line)"
                   if inn else
                   "background:var(--green);color:#eef4f0")
                + "'>"
                f"<div style='font-size:10px;font-weight:700;opacity:.65'>"
                f"{'Customer' if inn else 'Master Butler'} · "
                f"{esc(_pt(at))}</div>"
                f"{_expandable(body or subject)}</div></div>")))

        for e in p["emails"]:
            for m in cst.hist_msgs(e):
                add_msg(m["at"], m["dir"], m.get("body"), m.get("subject"))
        tmap = {a: ms for a, n, ms in threads}
        for e in p["emails"]:
            for m in tmap.get(e, []):
                add_msg(m["at"], m["dir"],
                        msglog.clean_body(m.get("body") or ""),
                        m.get("subject"))
        # what the record itself heard — voicemail transcripts + the
        # original email when it predates the live log (dedup by body)
        seen_bodies = {k[2] for k in seen_m}
        for s in p["stamps"]:
            b = recs.get(s)
            nm_ = (b or {}).get("newest_message") or ""
            body_ = msglog.clean_body(nm_)
            if body_ and body_[:80] not in seen_bodies:
                seen_bodies.add(body_[:80])
                add_msg(_stamp_utc(s), "in", body_, b.get("subject"))
        for s in p["stamps"]:
            b = recs.get(s)
            if not b:
                continue
            d_ = (b.get("draft") or {})
            if d_.get("total"):
                tl.append((_stamp_utc(s),
                           f"<div class='subtext' style='text-align:center;"
                           f"margin:8px 0'>— 🤖 system drafted "
                           f"${d_['total']:,.0f} · <a href='/?c="
                           f"{urllib.parse.quote(by_email.get(p['emails'][0], '') if p['emails'] else '')}'>"
                           f"open the bid</a> —</div>"))
            if quotes.get(s):
                tl.append((_stamp_utc(s) + "~",
                           f"<div class='subtext' style='text-align:center;"
                           f"margin:8px 0'>— 📋 Jobber "
                           f"{quote_chip(quotes[s], quote_urls())} —</div>"))
        tl.sort(key=lambda x: x[0])
        thread_html = ("".join(h for _, h in tl) + _MSG_MORE_JS) if tl else \
            ("<div class='subtext'>No conversation on file yet.</div>")

        # compact reply (locked send, same rails as everywhere) — with
        # the ✨ drafter + quick responses the office lives on
        reply_html = ""
        if p["emails"]:
            e0 = p["emails"][0]
            _cnp = _canned_payload()
            reply_html = f"""
 <div style='border-top:1px solid var(--line);margin-top:10px;
      padding-top:10px'>
  <div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px'>
   <form method='POST' action='/msg_draft' style='display:inline'>
    <input type='hidden' name='to' value='{esc(e0)}'>
    <input type='hidden' name='back'
     value='custtab:{esc(sel)}'>
    <button class='gray' style='border-color:var(--gold);
     color:var(--goldink)'>✨ Draft a reply for me</button>
   </form>
   <select id='custcanned' style='max-width:280px'>
    <option value=''>Quick responses…</option></select>
  </div>
  <form method='POST' action='/msg_send'>
   <input type='hidden' name='to' value='{esc(e0)}'>
   <input type='hidden' name='subject' value='Master Butler'>
   <input type='hidden' name='back'
    value='/customers?c={urllib.parse.quote(sel)}'>
   <textarea id='custreply' name='body' rows='2' style='min-height:56px'
    placeholder='Reply to {esc(e0)} — sends as customercare@'
    >{esc(draft)}</textarea>
   <div style='display:flex;justify-content:space-between;
        align-items:center;margin-top:6px'>
    <span class='subtext'>{"Sends for real, as customercare@ — same as"
     " the Inbox reply box." if REPLIES_ENABLED else
     "Sending stays locked until Dallon flips it on."}</span>
    {f'''<button class='big' onclick="return this.form.body.value.trim()
 ? confirm('Send this to {esc(e0)}?') : false">✉️ Send to customer
    </button>''' if REPLIES_ENABLED else
     '''<button class='big' type='button' onclick="alert('Sending is
 switched OFF — copy the text into Gmail for now.')">Send</button>'''}
   </div></form>
  <form method='POST' action='/idea_send' style='margin-top:10px;
       border-top:1px dashed var(--line);padding-top:10px'>
   <input type='hidden' name='context'
    value='while working on {esc(", ".join(p["names"][:2]) or sel)} — open them: https://masterbutler-dashboard.onrender.com/customers?c={urllib.parse.quote(sel)}'>
   <input type='hidden' name='back' value='/customers?c={urllib.parse.quote(sel)}'>
   <input type='text' name='text' placeholder='💡 Need help / have an idea? This goes to DALLON (never the customer), tagged with this profile'>
   <button class='gray'>💡 Ask Dallon</button>
  </form></div>
<script>
{_CANNED_MERGE_JS}
var CC = mergeCanned({_cnp});
var _ccs = document.getElementById('custcanned');
Object.keys(CC).forEach(function(k){{
  var o = document.createElement('option'); o.value = k; o.textContent = k;
  _ccs.appendChild(o);
}});
_ccs.onchange = function(){{
  if (!_ccs.value) return;
  var t = document.getElementById('custreply');
  t.value = CC[_ccs.value]; t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight + 6, 420) + 'px'; t.focus();
}};
</script>"""

        detail = (
            f"<div class='pinned'>"
            f"<div class='pin-top'><div><h2>{names_h}</h2>"
            f"<div class='paddr'>{addr_h}{jb}</div>"
            f"<div class='subtext' style='margin-top:3px'>{contacts}</div>"
            f"</div></div>"
            + (f"<div class='pchips'>{facts}</div>" if facts else "")
            + (f"<div class='notes' style='margin-top:8px'><b>📌 MUST "
               f"KNOW:</b> {esc(mk)}</div>" if mk else "")
            + (f"<div style='margin-top:8px'>{gallery}</div>"
               if gallery else "")
            + "</div>"
            f"<div id='custthread' style='max-height:520px;overflow-y:auto;"
            f"padding:10px 4px'>{thread_html}</div>" + reply_html
            + """<script>
var t = document.getElementById('custthread');
if (t) t.scrollTop = t.scrollHeight;
</script>""")

    # KEEP THE LIST'S PLACE (LaRee, Jul 10: 'scrolling down and clicking
    # on someone resets the list to the top — we have to scroll all the
    # way down again'). Keyed by PATH only, so picking a different
    # customer keeps the roster where it was; the detail pane still
    # starts at the top for each new person, as it should. Restore
    # RETRIES until layout (same lesson as the inbox fix).
    keep_js = """<script>
(function(){
  var KEY='scroll:/customers:list';
  function pane(){return document.querySelector('.ilist');}
  try{
    var y=parseInt(sessionStorage.getItem(KEY)||'');
    if(!isNaN(y)&&y>0){
      var tries=0;
      (function apply(){
        var p=pane(); if(p)p.scrollTop=y;
        var ok=(p&&(Math.abs(p.scrollTop-y)<3
                 ||p.scrollHeight-p.clientHeight<=y+3));
        if(!ok&&tries++<12)setTimeout(apply,80);
      })();
    }
  }catch(e){}
  var t;
  document.addEventListener('scroll',function(e){
    if(!pane()||e.target!==pane())return;
    clearTimeout(t);
    t=setTimeout(function(){
      try{sessionStorage.setItem(KEY,pane().scrollTop);}catch(e){}
    },120);
  },true);
})();
</script>"""
    body = (f"<div class='mock'>{_chrome_bar('Customers')}"
            f"<div class='inboxgrid'>"
            f"<div class='ilist'>{lst}</div>"
            "<div class='iresize' title='Drag to widen the list'></div>"
            f"<div class='idetail'>{detail}{keep_js}</div>"
            f"</div></div>")
    return page("Customers", body, chrome="bare")


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
        e = _canon_email(_bid_email(b))
        # an UNMATCHED Jobber event (didn't fold into a real customer's
        # bid) is its own entry — the subject carries the customer's name;
        # keying by noreply@ would lump every event into one fake person
        if b.get("kind") == "jobber_event" and (not e or "getjobber" in e):
            key = "stamp:" + b["stamp"]
            c = entry(key)
            _disp = (b.get("from") or "").split("<")[0].strip()
            c["name"] = (_disp if _disp and _disp.lower() != "jobber"
                         else (b.get("subject") or "Jobber event")[:40])
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
        c = entry(_canon_email(addr))
        c["email"] = c["email"] or addr
        if name and name != addr and not c["name"]:
            c["name"] = name
        c["msgs"] = msgs

    # ── sort: needs-a-person first, WONs and quiet threads sink ──
    roster = []
    for key in order:
        c = cust[key]
        c["bids"].sort(key=lambda b: b["stamp"])
        nb = _primary_bid(c["bids"])     # face = newest record that
        # carries a request, not an empty follow-up (Natallie, Jul 13)
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
                f"{'background:var(--soft);color:var(--ink)' if inn else 'background:#0b3d2e;color:#eef4f0'}'>"
                f"<div style='font-size:10px;font-weight:700;opacity:.65;"
                f"margin-bottom:2px'>{who} · "
                f"{esc(_pt(m_['at']))}</div>"
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

        canned_json = _canned_payload()
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
     placeholder='Reply as customercare@'>{esc(draft)}</textarea>
    <div style='display:flex;justify-content:space-between;
                align-items:center;margin-top:6px'>
     <span class='subtext'>{"Sends as customercare@ · your edits teach "
      "the brain" if REPLIES_ENABLED else
      "Sending stays locked until Dallon flips it on."}</span>
     {f"<button class='big' onclick=\"return confirm('Send this reply "
      f"to {esc(c['email'])}?')\">Send reply</button>"
      if REPLIES_ENABLED else
      "<button class='big' type='button' onclick=\"alert('Sending is "
      "switched OFF while we test — copy the text into Gmail for "
      "now.')\">Send reply</button>"}
    </div>
   </form></div>
<script>
{_CANNED_MERGE_JS}
var CANNED = mergeCanned({canned_json});
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
            rng = ""
            if conf is not None and conf < 50 and (d.get("total") or 0) > 0:
                from bid_engine import round_to_5 as _r5
                rng = (f"<div style='font-size:11.5px;font-weight:700;"
                       f"color:#c77700'>likely ${_r5(d['total'] * 0.8):,.0f}"
                       f"–${_r5(d['total'] * 1.2):,.0f}</div>")
            head = (
                f"<div style='display:flex;justify-content:space-between;"
                f"align-items:center'><div>"
                f"<div style='font-size:10px;font-weight:800;letter-spacing:"
                f"1.1px;text-transform:uppercase;color:var(--mut)'>The bid"
                f" · {nb['stamp'][4:6]}/{nb['stamp'][6:8]}</div>"
                + (f"<div style='font-size:28px;font-weight:800;"
                   f"letter-spacing:-1px;color:var(--green2)'>"
                   f"${d['total']:,.0f}</div>{rng}"
                   if d.get("total") is not None
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
                           f"font-size:12px;color:var(--goldink);font-weight:600;"
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
            if nb.get("jobber_client_url"):
                qchip += (f" <a href='{esc(nb['jobber_client_url'])}' "
                          f"target='_blank' rel='noopener' class='chip win' "
                          f"style='text-decoration:none'>👤 Jobber ↗</a>")
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


ADD_MENU = [("gutter_cleaning", "Gutter cleaning"),
            ("roof_blow_off", "Roof blow off"),
            ("moss_treatment", "Moss treatment"),
            ("windows_exterior", "Windows — exterior"),
            ("windows_in_out", "Windows — in & out"),
            ("house_wash", "House wash"),
            ("dryer_vent", "Dryer vent cleaning")]


def price_one_service(rec, svc):
    """Engine-price ONE service from the property facts already on the
    record — powers the add-to-quote menu (Dallon Jul 9: customer asks
    for gutters, then dryer vent, then PW — office shouldn't re-run
    the whole system every time). Returns line dicts or None.
    Uses the REAL debris/buildup reads persisted at intake when the
    record has them; PW surfaces price from aerial-measured areas."""
    pi = ((rec.get("draft") or {}).get("prop_info")) or {}
    if not pi.get("sqft"):
        return None
    from pipeline import SERVICE_TO_ENGINE
    from bid_engine import calculate_bid
    if svc not in SERVICE_TO_ENGINE:
        return None
    key, val = SERVICE_TO_ENGINE[svc]
    surfaces = {}
    if key in ("driveway", "patio", "sidewalk", "deck"):
        area = (pi.get("aerial_surfaces") or {}).get(key)
        if not area:
            return None               # no measured area — never guess
        surfaces = {key: area}
    # BUNDLE CONTEXT: if the draft already has lines, the new service is
    # an ADD-ON — dryer vent drops to its add-on rate, windows use the
    # bundled floor. Price it alongside a companion, keep only its lines.
    has_lines = bool(((rec.get("draft") or {}).get("bid") or {})
                     .get("services"))
    services = {key: val}
    if has_lines and key != "gutters":
        services["gutters"] = True
    prop = {"sqft": pi["sqft"], "stories": str(pi.get("stories") or "2"),
            "pitch": pi.get("pitch") or "moderate",
            "debris": pi.get("debris_read") or "moderate",
            "buildup": pi.get("buildup_read") or "clean",
            "gutter_type": "standard",
            "roof_material": pi.get("roof_material") or "standard",
            "access": "normal", "window_style": "standard",
            "window_condition": "normal", "window_access": "standard",
            "french_pane": "none", "surfaces": surfaces,
            "services": services}
    try:
        results, _notes, _conf = calculate_bid(prop)
        if "gutters" in services and key != "gutters":
            gutter_only, _n, _c = calculate_bid(dict(prop,
                services={"gutters": True}))
            companion = {li["name"] for li in gutter_only}
            results = [li for li in results if li["name"] not in companion]
        return results or None
    except Exception:
        return None


def add_service_card(b, back=""):
    """Pre-priced menu of the services NOT on this bid — one click adds
    the line. Prices come from the same engine + property record."""
    d = b.get("draft") or {}
    if not (d.get("prop_info") or {}).get("sqft") or b.get("reviewed") \
            or b.get("dns_match"):
        return ""
    try:
        from store import _service_key
    except Exception:
        def _service_key(n):
            return (n or "").lower()
    have = {_service_key(s["name"]) for s in
            (d.get("bid") or {}).get("services") or []}
    pi = d.get("prop_info") or {}
    asf = pi.get("aerial_surfaces") or {}
    menu = list(ADD_MENU) + [
        (f"pw_{k}", f"Pressure wash {k} (~{a:,} sqft, aerial-measured)")
        for k, a in sorted(asf.items()) if a]
    # MULTI-SELECT + LIVE TOTAL (Jessica, Jul 9: 'can't click on
    # multiple, and clicking on one doesn't do anything to the price')
    rows = ""
    for svc, label in menu:
        lines = price_one_service(b, svc)
        if not lines:
            continue
        if any(_service_key(li["name"]) in have for li in lines):
            continue                        # already on the quote
        price = sum(li["price"] for li in lines)
        rows += (
            # explicit colors — these rows inherited a dark ink and were
            # unreadable on the dark theme (Tom + LaRee, Jul 13)
            f"<label style='display:flex;align-items:center;gap:10px;"
            f"padding:7px 10px;border:1px solid var(--line);"
            f"border-radius:10px;margin:4px 0;cursor:pointer;"
            f"color:var(--ink)'>"
            f"<input type='checkbox' name='svc' value='{svc}' "
            f"data-price='{price:.0f}' class='addsvc' "
            f"style='width:17px;height:17px'>"
            f"<span style='flex:1;color:var(--ink)'>{esc(label)}</span>"
            f"<b style='color:var(--green2)'>${price:,.0f}</b></label>")
    if not rows:
        return ""
    cur_total = (b.get("draft") or {}).get("total") or 0
    debris_line = (f"debris/buildup priced from this home's imagery reads "
                   f"({esc(pi.get('debris_read'))})"
                   if pi.get("debris_read") else "standard debris assumed")
    measure_btn = ""
    if not asf and b.get("address"):
        measure_btn = (
            f"<form method='POST' action='/measure_surfaces' "
            f"style='margin-top:8px'>"
            f"<input type='hidden' name='stamp' value='{b['stamp']}'>"
            f"<input type='hidden' name='customer' value='{esc(b.get('from') or '')}'>"
            f"<button class='gray' style='font-size:12px'>📐 Measure "
            f"driveway/patio/sidewalk from the sky (~2¢) — unlocks "
            f"pre-priced pressure washing</button></form>")
    return (
        # id joins the fold-persistence script — the office kept losing
        # this open on the 2-min refresh (LaRee, Jul 13)
        "<details class='card' id='addsvcfold'><summary style='cursor:"
        "pointer;font-weight:700;color:var(--green2)'>➕ Add more services "
        "— check any, watch the total, add them all at once</summary>"
        f"<form method='POST' action='/add_service' id='addsvcform' "
        f"style='margin-top:8px'>"
        f"<input type='hidden' name='stamp' value='{b['stamp']}'>"
        f"<input type='hidden' name='back' value='{esc(back)}'>"
        f"<input type='hidden' name='customer' "
        f"value='{esc(b.get('from') or '')}'>"
        + rows +
        f"<div style='display:flex;justify-content:space-between;"
        f"align-items:center;margin-top:10px'>"
        f"<b id='addsvctotal' data-base='{cur_total:.0f}' "
        f"style='color:var(--green2)'>quote stays ${cur_total:,.0f}</b>"
        f"<button class='gray' style='font-weight:700' disabled "
        f"id='addsvcbtn'>➕ Add checked services</button></div></form>"
        """<script>
(function(){
  var boxes = document.querySelectorAll('#addsvcform .addsvc');
  var tot = document.getElementById('addsvctotal');
  var btn = document.getElementById('addsvcbtn');
  if (!tot) return;
  var base = parseFloat(tot.getAttribute('data-base')) || 0;
  function upd(){
    var add = 0, n = 0;
    boxes.forEach(function(b){ if (b.checked){
      add += parseFloat(b.getAttribute('data-price')) || 0; n++; } });
    btn.disabled = n === 0;
    tot.textContent = n === 0
      ? 'quote stays $' + base.toLocaleString()
      : '+$' + add.toLocaleString() + ' → new total $'
        + (base + add).toLocaleString();
  }
  boxes.forEach(function(b){ b.addEventListener('change', upd); });
  upd();
})();
</script>"""
        f"<div class='subtext' style='margin-top:6px'>Engine prices from "
        f"this property's record ({debris_line}). PW lines: office rule "
        f"still applies — verify surfaces/pictures before booking.</div>"
        + measure_btn + "</details>")


GUIDE_FAQ = [
 ("The 30-second morning",
  "Open <b>Bids</b>. Anything <b>bold with a gold dot</b> hasn't been "
  "handled by anyone yet — work top to bottom. The line above the list "
  "tells you how many need a person and the oldest wait. That's it."),
 ("What does bold / grey mean?",
  "<b>Bold</b> = nobody has dealt with it. It stays bold even after you "
  "open it — clicking around never loses anything. It turns grey when "
  "someone presses <b>✓ Done — seen it</b> or makes a real decision "
  "(approve / park). Grey is shared: one person handling it means the "
  "whole office sees grey. Pressed Done but never decided? It re-bolds "
  "itself after 30 minutes. <b>↩ mark unread</b> hands it back."),
 ("One customer = one screen",
  "Click a name: the top card pins the essentials — price, how sure the "
  "system is, what they wrote, timing/tech asks — and the folds below "
  "hold everything else (line items, photos, conversation, history, "
  "warnings). New message from them? They jump to the top of the list "
  "and the Conversation fold opens by itself."),
 ("Deciding — the three choices",
  "<b>✓ Price is right — approve</b> records your OK (on bids Dallon "
  "has switched on, it also creates the DRAFT quote in Jobber — never "
  "sends anything to the customer). <b>⏸ Not now</b> parks it until a "
  "date you pick, then it comes back by itself. <b>🙋 Help</b> asks the "
  "office a question (stays on the list), escalates to Dallon &amp; Tom "
  "when truly stuck, or sends an idea."),
 ("A price looks wrong",
  "Type the right number straight over it in the Line items fold, tap "
  "WHY (one tap teaches the system), press <b>💾 Save my prices</b>, "
  "then approve. The system remembers what it originally guessed, so "
  "it learns from your correction."),
 ("Customer asks for MORE services mid-conversation",
  "Open their entry → Line items fold → <b>➕ Add another service</b>. "
  "Every service is pre-priced for that exact home; one click adds the "
  "line. Pressure washing appears when the sky-measurement exists — "
  "the 📐 button fetches it. Still verify PW with pictures before "
  "booking (office rule)."),
 ("They already HAVE a quote",
  "A gold <b>📋 Their Jobber quote</b> panel shows on the pinned card — "
  "number, status, total, the lines, and an open-in-Jobber link. Work "
  "from THAT quote; the system physically refuses to create a second "
  "one for them."),
 ("Replying to a customer",
  "Conversation fold → the box is often PRE-FILLED in our own words "
  "(or pick a Quick Response / press <b>✨ Draft a reply for me</b>). "
  "Edit anything, hit <b>Send</b> and confirm — it goes out from "
  "customercare@, same as Gmail. Your edits teach the system; every "
  "send is scored on the Scoreboard's 🎯 wheel."),
 ("Voicemails",
  "Each call is its own entry. If audio came with it, the words appear "
  "as a transcript. '0:00 — hang-up' means nothing was recorded. "
  "'No audio attached' means dial the mailbox (press * during the "
  "greeting, passcode 1234), then reply by email as usual."),
 ("Where did an email go?",
  "When you TRASH a thread in Gmail, the dashboard files it too — "
  "look in the <b>🗑 Cleared by the Gmail sync</b> fold at the bottom "
  "of the list, or type the name in <b>🔍 find customer</b>. Nothing "
  "is ever deleted, and a new message from that customer pops the row "
  "straight back into the Inbox."),
 ("⛔ DO NOT SERVICE showed up",
  "The system matched them to a do-not-service marker in Jobber — by "
  "email, phone, ADDRESS, or name (it catches new-email tricks). Don't "
  "quote or schedule; questions go to Dallon/Tom."),
 ("Who's working on what?",
  "Opening a bid quietly claims it for 15 minutes — others see "
  "<b>working · your name</b> on the row and a banner on the bid. "
  "Need it anyway? <b>🤝 Take over</b> (it's logged). Walking away? "
  "Release it or just let the claim expire."),
 ("What do Scoreboard and Win-back mean?",
  "<b>Scoreboard</b>: the system's draft vs what the office actually "
  "quoted — green means within 10%; it auto-reviews its own misses and "
  "learns. <b>Win-back</b>: past customers worth a call, best first — "
  "tap the outcome after each call."),
 ("Settings — yours to change",
  "Quick Responses and every pricing number/multiplier are editable by "
  "the office, live immediately, logged with your name. 'Reset "
  "everything to defaults' un-does all pricing changes in one click."),
 ("💡 I have an idea for the system",
  "Help fold → the idea box. One line. It emails Dallon instantly and "
  "Claude (the builder) reads every idea overnight and pre-plans the "
  "fix. This page you're reading came from one of those."),
 ("Something looks broken",
  "Nothing here can lose work — every quote is a draft until a human "
  "sends it. If a page glitches, go back to Bids and tell Dallon (or "
  "drop it in the idea box). The old layout still exists at /queue if "
  "you ever feel lost."),
 ("Finding a customer fast",
  "Type in the <b>🔎 Find a customer</b> box above the list — it "
  "filters as you type, across every section. Clear it to get the "
  "sections back."),
 ("Have to step away mid-review?",
  "Press <b>🚶 Stepping away</b> on the customer's card. It releases "
  "your claim and keeps the entry bold, so the next person naturally "
  "picks it up. Nothing to remember."),
 ("Why is something marked ⚠ urgent at the very top?",
  "The system reads for worry words — no-show, leak, damage, upset, "
  "refund and the like — and floats those above everything, naming the "
  "phrase it saw. Handle these first."),
 ("My own quick responses (★)",
  "Settings → <b>★ your-name's quick responses</b>. Ones you add there "
  "show only in YOUR dropdown, marked ★. The shared set stays "
  "everyone's — edit those in the section above it."),
 ("Adding several services at once",
  "In <b>➕ Add more services</b>, check as many as they asked for — "
  "the running total updates live ('+$150 → new total $595') — then "
  "one click adds them all."),
 ("Holiday lights — whose schedule?",
  "Office rule from Dallon &amp; Jessica (Jul 2026): a NEW install can "
  "go on anyone's schedule, but that season's <b>takedown belongs to "
  "the installer</b>. The following year the home goes back into the "
  "normal route rotation."),
 ("What's the 🧾 tax % by the address?",
  "The exact sales-tax rate for that address, straight from the WA "
  "Department of Revenue — the same rate the quote will charge. If it "
  "ever looks wrong for the area, tell Dallon."),
 ("👥 The Customers tab",
  "One file per household. Search ANY name, address, email, or phone "
  "(formatting doesn't matter) — spouses share a file and either name "
  "finds it. The conversation reads oldest-to-newest like texting, "
  "with a year of history. The 📋 chip opens their quote in Jobber; "
  "📥 jumps to their live entry on Bids."),
 ("🚐 The Routes tab",
  "Pick a date → each tech's real day from the Jobber schedule, stops "
  "in the best driving order with arrival estimates and total miles. "
  "Switch the dropdown to <b>Tasks</b> for takedown days in January. "
  "It's READ-ONLY — it never changes the schedule; it just shows the "
  "smart order. ↻ refresh re-reads Jobber right now."),
 ("📋 The Brief tab",
  "The morning brief, live: the pinned notes for the office, the "
  "queue count, scoreboard, and standing flags. The same brief lands "
  "in Dallon's email every night at 9."),
]


def routes_page(date_str=None, kind="visits", fresh=False):
    """🚐 LIVE ROUTES (Dallon, Jul 9 pm: 'build the live route system…
    and the takedown schedule the same way'). Reads the REAL Jobber
    schedule for any day — visits, or TASKS for takedown season —
    groups by assigned tech, and orders each day from the shop with
    the Google Routes API. Read-only; the schedule is untouched."""
    import routing
    from datetime import date as _date
    date_str = date_str or _date.today().isoformat()
    # must be a REAL calendar date — '2026-13-45' matches the shape but
    # crashes the date math downstream (Jul 10 shadow-test)
    try:
        _date.fromisoformat((date_str or "")[:10])
    except (ValueError, TypeError):
        date_str = _date.today().isoformat()
    kind = "tasks" if kind == "tasks" else "visits"
    day = routing.build_day(date_str, kind,
                            max_age_min=0 if fresh else 15)

    picker = f"""
<div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap;
     margin-bottom:14px'>
 <form method='GET' action='/routes' style='display:flex;gap:8px;
      align-items:center'>
  <input type='date' name='d' value='{esc(date_str)}'
   style='padding:7px 10px;border-radius:9px;border:1px solid
   var(--line);background:var(--card);color:var(--ink)'>
  <select name='k' style='max-width:180px'>
   <option value='visits' {'selected' if kind == 'visits' else ''}>
    Jobs on the schedule</option>
   <option value='tasks' {'selected' if kind == 'tasks' else ''}>
    Tasks (takedowns)</option>
  </select>
  <button>Show routes</button>
  <button name='fresh' value='1' class='gray'
   title='Recompute from Jobber right now'>↻ refresh</button>
 </form>
 <button class='gray' onclick='window.print()'
  title='Prints one clean page per tech — no maps, no buttons'>
  🖨 Print day sheets</button>
 <span class='subtext'>computed
  {esc(_pt(day.get("computed_at") or ""))} · order optimized from the
  Monroe shop · read-only</span>
</div>"""

    if day.get("error"):
        return page("Routes", f"<div class='mock'>{_chrome_bar('Routes')}"
                    f"<div style='padding:20px 26px'>{picker}"
                    f"<div class='card'>Jobber said: "
                    f"{esc(day['error'][:160])}</div></div></div>",
                    chrome="bare")
    if not day["techs"]:
        return page("Routes", f"<div class='mock'>{_chrome_bar('Routes')}"
                    f"<div style='padding:20px 26px'>{picker}"
                    "<div class='card'>Nothing with an address on the "
                    "schedule that day.</div></div></div>", chrome="bare")

    sections, mapjs = "", ""
    for ti, (tech, t) in enumerate(day["techs"].items()):
        rows = "".join(
            f"<tr{' style=opacity:.55' if s['done'] else ''}>"
            f"<td><b>#{s['n']}</b></td><td>{esc(s['arrive'])}</td>"
            f"<td>{esc((s['title'] or '')[:44])}"
            + (" <span title='customer confirmed' style='color:"
               "var(--green2);font-weight:800'>CCC</span>"
               if s.get("confirmed") else "")
            + f"<div class='subtext'>{esc((s['address'] or '')[:52])}</div>"
            + (f"<div class='subtext' style='color:var(--goldink)'>📝 "
               f"{esc(s['instructions'][:110])}</div>"
               if s.get("instructions") else "")
            + f"</td>"
            f"<td class='subtext'>{('+' + str(s['drive_min']) + 'm') if s['drive_min'] else '·'}</td>"
            f"<td>{'✓' if s['done'] else ''}</td></tr>"
            for s in t["stops"])
        sections += f"""
<div class='card'>
 <h2 style='margin-top:0'>👷 {esc(tech)}
  <span class='subtext' style='font-weight:400'>· {len(t['stops'])}
  stop(s) · {t['drive_min']} min / {t['drive_mi']} mi driving ·
  back {esc(t['back_at'])}</span></h2>
 <div style='display:grid;grid-template-columns:1fr 1fr;gap:14px'
      class='routegrid'>
  <div id='map{ti}' style='height:420px;border-radius:12px'></div>
  <div style='overflow-x:auto;max-height:420px;overflow-y:auto'>
   <table><tr><th></th><th>Est.</th><th>Stop</th><th>Drive</th><th></th>
   </tr>{rows}</table></div>
 </div></div>"""
        mapjs += f"""
(function(){{
 var m = L.map('map{ti}');
 L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{maxZoom: 18, attribution: '© OpenStreetMap'}}).addTo(m);
 var poly = {json.dumps(t["poly"])};
 if (poly.length) L.polyline(poly, {{color: '#177245', weight: 4,
  opacity: .75}}).addTo(m);
 var stops = {json.dumps([[s["lat"], s["lng"], s["n"],
                           (s["title"] or "")[:40], s["arrive"]]
                          for s in t["stops"]])};
 var bounds = [];
 stops.forEach(function(s){{
  bounds.push([s[0], s[1]]);
  L.marker([s[0], s[1]], {{icon: L.divIcon({{className: '',
   html: "<div style='background:#0b3d2e;color:#fff;border-radius:50%;"
     + "width:24px;height:24px;line-height:24px;text-align:center;"
     + "font-weight:800;font-size:12px;border:2px solid #fff;"
     + "box-shadow:0 1px 4px rgba(0,0,0,.4)'>" + s[2] + "</div>",
   iconSize: [24, 24]}})}}).addTo(m)
   .bindPopup('#' + s[2] + ' ' + s[4] + ' — ' + s[3]);
 }});
 if (bounds.length) m.fitBounds(bounds, {{padding: [16, 16]}});
}})();"""

    note = (f"<div class='subtext' style='margin:4px 0 10px'>"
            f"{day['skipped_no_address']} schedule item(s) had no "
            f"property address (office reminders) — not routed.</div>"
            if day.get("skipped_no_address") else "")
    body = (f"<div class='mock'>{_chrome_bar('Routes')}"
            f"<div style='padding:20px 26px;max-width:1150px'>"
            f"<h2 style='margin:2px 0 6px'>🚐 Routes — "
            f"{'takedown tasks' if kind == 'tasks' else 'the day’s jobs'}"
            f", {esc(date_str)}</h2>{picker}{note}{sections}</div></div>"
            "<link rel='stylesheet' "
            "href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'>"
            "<script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'>"
            "</script>"
            "<style>@media(max-width:900px){.routegrid{"
            "grid-template-columns:1fr}}"
            ".leaflet-container{background:#dde5dd}"
            # 🖨 one clean page per tech: no chrome, no maps, no buttons,
            # black on white, the table full-width
            "@media print{.chrome,form,button,.leaflet-container,"
            ".subtext a{display:none !important}"
            "body,.mock{background:#fff !important;color:#000 !important}"
            ".mock{border:0 !important;box-shadow:none !important}"
            ".card{page-break-after:always;border:0 !important;"
            "box-shadow:none !important;background:#fff !important}"
            ".card h2{color:#000 !important}"
            ".routegrid{grid-template-columns:1fr !important}"
            "td,th{color:#000 !important;border-bottom:1px solid #999}"
            ".subtext{color:#333 !important}"
            "div[style*='max-height']{max-height:none !important;"
            "overflow:visible !important}}"
            "</style>"
            f"<script>{mapjs}</script>")
    return page("Routes", body, chrome="bare")


def route_demo_page():
    """🚐 ROUTE MOCKUP (Dallon, Jul 9 pm: 'a mock up for jessica and
    tom what a route would look like based on the geomapping and
    driver apis we have already installed' — his 2025 Iron Man route).
    Data is precomputed into the route_demo blob; the page just draws.
    """
    demo = _blob_rw("route_demo", None)
    if not demo:
        return page("Route mockup", "<div class='card'>Route demo data "
                    "isn't loaded yet — tell Dallon.</div>")
    t = demo["territory"]
    d = demo["day"]
    chips = " ".join(
        f"<span class='chip'>{esc(c)}: <b>{n}</b></span>"
        for c, n in t["by_city"])
    months = " · ".join(f"{m}: {n}" for m, n in t["by_month"])
    rows = "".join(
        f"<tr><td><b>#{s['n']}</b></td><td>{esc(s['arrive'])}</td>"
        f"<td>{esc(s['name'])[:30]}<div class='subtext'>"
        f"{esc(s['address'])[:48]}</div></td>"
        f"<td class='num'>${s['price']:,.0f}</td>"
        f"<td class='subtext'>{'+' + str(s['drive_min']) + ' min' if s['drive_min'] else 'next door'}</td></tr>"
        for s in d["stops"])
    body = f"""
<div class='mock'>{_chrome_bar('')}
<div style='padding:20px 26px;max-width:1150px'>
 <h2 style='margin:2px 0 4px'>🚐 Holiday-lights routing — what the
  mapping can already do</h2>
 <div class='subtext' style='margin-bottom:14px'>MOCKUP for Jessica &amp;
  Tom, built from REAL data: every 2025-season install invoice in the
  Iron Man territory + Google's route optimizer. Nothing here touches
  the schedule — it's a picture of what we could automate.</div>

 <div class='card'>
  <h2 style='margin-top:0'>The territory — Iron Man, 2025 season</h2>
  <div style='margin-bottom:8px'>{chips}
   <span class='chip win'>{t['homes']} homes</span>
   <span class='chip win'>${t['labor']:,.0f} labor invoiced</span></div>
  <div class='subtext' style='margin-bottom:8px'>Installs by month:
   {esc(months)}. Includes the handful Tom covered and Gavin's Mainvue
   pocket — postal cities, not payroll.</div>
  <div id='map_all' style='height:430px;border-radius:12px'></div>
 </div>

 <div class='card'>
  <h2 style='margin-top:0'>One printed day — Bothell, 8 installs
   (the morning sheet this could generate)</h2>
  <div class='subtext' style='margin-bottom:8px'>Order computed by the
   Google Routes API from the shop in Monroe and back. Assumes
   {d['install_min']} min per install. Total driving:
   <b>{d['drive_total_min']} min · {d['drive_total_mi']} mi</b> —
   leave 8:00 AM, back at the shop {esc(d['back_at'])}.</div>
  <div style='display:grid;grid-template-columns:1fr 1fr;gap:14px'
       class='routegrid'>
   <div id='map_day' style='height:460px;border-radius:12px'></div>
   <div style='overflow-x:auto'><table>
    <tr><th></th><th>Arrive</th><th>Customer</th>
        <th class='num'>2025 $</th><th>Drive</th></tr>
    {rows}
    <tr><td></td><td><b>{esc(d['back_at'])}</b></td>
        <td colspan=3><b>back at the shop</b></td></tr>
   </table></div>
  </div>
 </div>

 <div class='card'><h3>If we build it for real</h3>
  <div style='font-size:14px;line-height:1.7'>
   · Every morning in season: a sheet like this per tech — optimized
     stop order, times, addresses, light type, gate codes.<br>
   · Zip-day clustering (Jessica's ask): under-full days pull the
     nearest confirmed homes from the neighboring zip automatically.<br>
   · Capacity math live: confirmed installs ÷ 8-per-day against the
     calendar, so October overload shows up in September.<br>
   · New-build bookings drop onto the right day by geography instead
     of by memory.</div></div>
</div></div>
<link rel='stylesheet'
 href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'>
<script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'></script>
<style>@media(max-width:900px){{.routegrid{{grid-template-columns:1fr}}}}
.leaflet-container{{background:#dde5dd}}</style>
<script>
var T = {json.dumps(t["points"])};
var CITYCOLOR = {{"monroe":"#c0392b","sultan":"#c0392b",
 "snohomish":"#8e44ad","bothell":"#1a6b3c","woodinville":"#b8860b"}};
var m1 = L.map('map_all');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
 {{maxZoom: 18, attribution: '© OpenStreetMap'}}).addTo(m1);
var bounds = [];
T.forEach(function(p){{
  bounds.push([p[0], p[1]]);
  L.circleMarker([p[0], p[1]], {{radius: 4, weight: 1, color: '#fff',
    fillColor: CITYCOLOR[p[2]] || '#333', fillOpacity: .85}}).addTo(m1);
}});
m1.fitBounds(bounds, {{padding: [18, 18]}});

var D = {json.dumps({"stops": [[s["lat"], s["lng"], s["n"],
                                s["name"], s["arrive"]]
                               for s in d["stops"]],
                     "poly": d["poly"]})};
var m2 = L.map('map_day');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
 {{maxZoom: 18, attribution: '© OpenStreetMap'}}).addTo(m2);
L.polyline(D.poly, {{color: '#177245', weight: 4, opacity: .75}}).addTo(m2);
D.stops.forEach(function(s){{
  L.marker([s[0], s[1]], {{icon: L.divIcon({{className: '',
    html: "<div style='background:#0b3d2e;color:#fff;border-radius:50%;"
      + "width:24px;height:24px;line-height:24px;text-align:center;"
      + "font-weight:800;font-size:12px;border:2px solid #fff;"
      + "box-shadow:0 1px 4px rgba(0,0,0,.4)'>" + s[2] + "</div>",
    iconSize: [24, 24]}})}}).addTo(m2)
   .bindPopup('#' + s[2] + ' ' + s[4] + ' — ' + s[3]);
}});
m2.fitBounds(L.polyline(D.poly).getBounds(), {{padding: [16, 16]}});
</script>"""
    return page("Route mockup", body, chrome="bare")


def working_page():
    """🔭 The build board (Tom, Jul 14: 'the office should see what
    Dallon's worked on, what he's doing'). Renders the working_board
    blob — Claude keeps it current alongside the running-list artifact.
    Read-only for the office; sections: now / shipped / office / next."""
    wb = _blob_rw("working_board", {}) or {}
    upd = (wb.get("updated") or "")[:10]

    def sec(title, sub, items, icon):
        rows = "".join(
            f"<div style='padding:9px 14px;border-top:1px solid "
            f"var(--line);font-size:13.5px;color:var(--ink)'>{esc(i)}"
            f"</div>" for i in (items or []))
        return (f"<div class='card' style='margin-bottom:14px'>"
                f"<div class='schead'>{icon}<h2>{esc(title)}</h2></div>"
                f"<div class='subtext' style='margin:0 0 6px'>{esc(sub)}"
                f"</div>{rows or '<div class=subtext>nothing here</div>'}"
                f"</div>")

    body = (
        "<div style='max-width:820px'>"
        "<h2 style='margin:4px 0 2px;font-size:22px'>🔭 The build board"
        "</h2>"
        f"<div class='subtext' style='margin-bottom:14px'>What Dallon "
        f"and Claude are building on this system — updated {esc(upd) or 'recently'}.</div>"
        # idea box AT THE TOP (Dallon, Jul 14) — see the lists, then send
        + "<div id='idea' style='margin:0 0 14px;background:var(--soft);"
          "border:1px solid var(--line);border-radius:12px;"
          "padding:14px 16px'>"
          "<b>💡 Send Dallon an idea</b>"
          "<div class='subtext' style='margin:4px 0 8px'>Check the lists "
          "below first — if it's not already there, send it. Emails him "
          "instantly and lands on this board.</div>"
          "<form method='POST' action='/idea_send' style='display:flex;"
          "gap:8px'><input type='hidden' name='back' value='/working'>"
          "<input name='text' required placeholder='What should this "
          "dashboard do better?' style='flex:1'>"
          "<button>Send</button></form></div>"
        # THE HOURLY REVIEW (Dallon, Jul 14): what the auto-reviewer
        # caught this hour — problems surfaced before the office trips
        # on them. Flags only; a human decides.
        + (lambda lr: (
            "<div class='card' style='margin-bottom:14px'>"
            "<div class='schead'>🕵️<h2>Hourly review — needs a look</h2>"
            f"</div><div class='subtext' style='margin:0 0 6px'>every "
            f"row on every tab, re-checked hourly · last pass "
            f"{esc((lr.get('at') or '')[11:16])} UTC</div>"
            + "".join(
                f"<div style='padding:8px 14px;border-top:1px solid "
                f"var(--line);font-size:13px'>{f.get('sev')} "
                f"<b>{esc(f.get('name') or f.get('email'))}</b> "
                f"<span class='subtext'>{esc(f.get('check'))}</span><br>"
                f"{esc(f.get('note'))}</div>"
                for f in (lr.get("findings") or [])[:12])
            + "</div>") if (lr.get("findings")) else "")(
                _blob_rw("lane_review", {}) or {})
        + sec("Ideas from the office", "sent from the box above — "
              "Dallon gets each one by email instantly",
              wb.get("ideas"), "💡")
        # THE AUTO-RESPOND PLAN, WHOLE (Dallon, Jul 14: "add this entire
        # widget to the build board") — iframed so its own styling stays
        # isolated; the doc lives in the autorespond_plan blob.
        + ("<div class='card' style='margin-bottom:14px'>"
           "<details open><summary style='cursor:pointer;padding:4px 0;"
           "font-weight:800;font-size:16px'>✨ Auto-Respond — Plan of "
           "Attack <span class='subtext' style='font-weight:400'>"
           "(stage 1 SHADOW is live — <a href='/autodrafts'>the grading "
           "room</a>; nothing sends itself)</span>"
           "</summary>"
           "<iframe src='/plan_autorespond' title='Auto-Respond plan' "
           "style='width:100%;height:1500px;border:0;border-radius:8px;"
           "margin-top:8px;background:transparent'></iframe>"
           "</details></div>"
           if _blob_rw("autorespond_plan", "") else "")
        + sec("Building now", "in progress this week",
              wb.get("now"), "🔨")
        + sec("Just shipped", "landed recently — already live for you",
              wb.get("shipped"), "✅")
        + sec("Waiting on the office", "a person's answer unblocks these",
              wb.get("office"), "🙋")
        + sec("Up next / pipeline", "queued and future ideas",
              wb.get("pipeline"), "🗺")
        + "</div>")
    return page("Build board", body)


def autodrafts_page(user=None):
    """AUTO-RESPOND STAGE 1 — the shadow review page (Dallon's GO,
    Jul 14). Shows what the pre-filled reply box WOULD say: live
    proposals for threads awaiting us now, and a retro reel of recent
    (customer → office) exchanges with our draft beside what the office
    actually sent. Nothing here sends; the office isn't linked here."""
    import msglog
    import autorespond
    voice = _blob_rw("office_voice", {}) or {}

    # newest queue record per customer email (gates + facts live there)
    recs = {}
    for b in load_bids():
        m = re.search(r"<([^>]+)>", b.get("from") or "")
        e = m.group(1).lower() if m else None
        if e and (e not in recs or b["stamp"] > recs[e]["stamp"]):
            recs[e] = b

    def card(title, inner):
        return (f"<div class='card' style='margin-bottom:14px'>"
                f"<div class='schead'><h2>{title}</h2></div>{inner}</div>")

    def block(label, text, color="var(--ink)"):
        return (f"<div style='margin:6px 0'><div class='subtext' "
                f"style='font-weight:800'>{label}</div>"
                f"<div style='white-space:pre-wrap;font-size:13.5px;"
                f"color:{color};background:var(--soft);border-radius:8px;"
                f"padding:10px 12px'>{esc(text)}</div></div>")

    from datetime import datetime as _dt, timezone as _tz, timedelta
    now = _dt.now(_tz.utc)

    def _utc(at):
        try:
            t = _dt.fromisoformat(at)
            return t.replace(tzinfo=_tz.utc) if t.tzinfo is None else t
        except (ValueError, TypeError):
            return None

    live_rows, retro_rows, gated = [], [], 0
    learn_store = {}          # rebuilt fresh each render → idempotent
    pairs = []                # (kind, office_sent) → adopt_templates
    accs = []                 # per-pair accuracy scores → the wheel
    # OUR OWN MAIL IS NOT A CUSTOMER (Jul 15: the office training email,
    # customercare→customercare, landed in the reel as a 4%-match
    # "customer" and dragged the accuracy wheel). Internal senders —
    # our domain, Dallon, Tom — never draft and never grade.
    _internal = ("@masterbutlerinc.com", "dallon.masterbutler@gmail.com",
                 "tomfricke2007@gmail.com")
    threads = [(a, n, m) for a, n, m in msglog.threads()
               if not any((a or "").lower().endswith(x) for x in _internal)]

    # PASS 1 — LEARN from every in→out pair of the last 90 days: the
    # diary (wording gaps) AND the auto-adopted templates (Dallon's
    # ruling, Jul 14: "automate the wording change… it's easier
    # learned"). 90 days so the adoption bar (≥3 repeats) has teeth.
    for addr, name, msgs in threads:
        rec = recs.get((addr or "").lower())
        for i in range(1, len(msgs)):
            if msgs[i].get("dir") == "out" and msgs[i-1].get("dir") == "in":
                t = _utc(msgs[i].get("at"))
                if not t or now - t > timedelta(days=90):
                    continue
                d = autorespond.build_draft(rec, msgs[:i], user, voice)
                if not d:
                    continue
                sent = msgs[i].get("body") or ""
                pairs.append((d["type"], sent))
                acc = autorespond.accuracy(d["draft"], sent)
                accs.append(acc)
                learn_store = autorespond.fold_learning(
                    learn_store, autorespond.learn_gap(
                        d["type"], d["draft"], sent))
                if len(retro_rows) < 20 and now - t <= timedelta(days=30):
                    _ac = ("var(--green2)" if acc >= 85 else
                           "#e8c76a" if acc >= 50 else "#f2b8b5")
                    retro_rows.append(
                        f"<div style='border-top:1px solid var(--line);"
                        f"padding:10px 0'><b>{esc(name or addr)}</b> "
                        f"<span class='subtext'>{esc(d['type'])}</span> "
                        f"<span style='float:right;font-weight:800;"
                        f"color:{_ac}'>{acc}% match</span>"
                        + block("← customer wrote",
                                (msgs[i-1].get('body') or '')[:400])
                        + block("✨ we would have drafted", d["draft"],
                                "var(--green2)")
                        + block("→ the office actually sent", sent[:600])
                        + "</div>")

    tpl_off = set(_blob_rw("reply_templates_off", []) or [])
    adopted = autorespond.adopt_templates(pairs, off=tpl_off)

    # PASS 2 — LIVE proposals, drafted WITH the adopted office wording
    pulse_live = []       # compact copy for the Scoreboard pulse card
    for addr, name, msgs in threads:
        e = (addr or "").lower()
        last = msgs[-1] if msgs else None
        if last and last.get("dir") == "in":
            t = _utc(last.get("at"))
            if t and now - t <= timedelta(days=14):
                d = autorespond.build_draft(recs.get(e), msgs, user,
                                            voice, auto=adopted)
                if d:
                    # 🗓 STAGE 2 SHADOW (Dallon's go, Jul 15): what date
                    # the box WOULD offer — graded here, never shown to
                    # the office until the offers prove out
                    _off_html = ""
                    if d["type"] in ("approve_wants_date",
                                     "approval_only"):
                        try:
                            import sched_offers
                            _off = sched_offers.offer(recs.get(e) or {})
                        except Exception:
                            _off = None
                        if _off:
                            _off_html = block(
                                "🗓 stage-2 shadow — the date it would "
                                "offer", _off.get("why") or "",
                                "#e8c76a")
                    pulse_live.append({
                        "name": name or e, "type": d["type"],
                        "auto": bool(d.get("auto")),
                        "offer": (_off.get("why") if _off_html and _off
                                  else None)})
                    live_rows.append(
                        f"<div style='border-top:1px solid var(--line);"
                        f"padding:10px 0'><b>{esc(name or e)}</b> "
                        f"<span class='chip' style='background:var(--soft);"
                        f"border-radius:12px;padding:1px 9px;font-size:11.5px'>"
                        f"{esc(d['type'])}{' · 📖 auto' if d.get('auto') else ''}"
                        f"</span> <span class='subtext'>"
                        f"{esc(d['why'])}</span>"
                        + block("← customer", (last.get('body') or '')[:400])
                        + block("✨ the box would say", d["draft"],
                                "var(--green2)")
                        + _off_html
                        + "</div>")
                else:
                    gated += 1

    body = (
        "<div style='max-width:860px'>"
        "<h2 style='margin:4px 0 2px;font-size:22px'>✨ Auto-respond — "
        "shadow drafts (stage 1)</h2>"
        "<div class='subtext' style='margin-bottom:14px'>Nothing on this "
        "page sends or touches the inbox — it's the grading room. "
        "Drafts appear ONLY for genuine customer inbounds; complaints "
        "and price talk never draft. Full plan: "
        "<a href='/plan_autorespond'>the plan of attack</a>.</div>"
        + (lambda _s: _reply_wheel(
            [x["acc"] for x in _s] if _s else accs, live=bool(_s)))(
            _blob_rw("draft_sends", []) or [])
        + card(f"Would draft RIGHT NOW ({len(live_rows)}) — threads "
               f"awaiting a reply · {gated} inbound threads correctly "
               f"left blank",
               "".join(live_rows) or "<div class='subtext'>nothing "
               "awaiting us matches a safe template right now</div>")
        + card(f"Grading reel — last 30 days, our draft vs the office "
               f"({len(retro_rows)})",
               "".join(retro_rows) or "<div class='subtext'>no recent "
               "customer→office pairs matched a template</div>")
        + card("📖 Auto-adopted wording — the office trained these "
               "(hands-off, Dallon's ruling Jul 14)",
               _adopted_rows(adopted, tpl_off) or "<div class='subtext'>"
               "no reply type has hit the adoption bar yet (an office "
               "shape must repeat ≥3× and cover ≥half of that type's "
               "graded replies). The built-in templates hold until "
               "then.</div>")
        + card("📖 What the office keeps changing (the learning diary)",
               _learn_rows(learn_store) or "<div class='subtext'>no "
               "wording gaps yet — drafts match how the office writes"
               "</div>")
        + "</div>")
    try:
        _blob_save("draft_learnings", learn_store)
        _blob_save("reply_templates_auto", adopted)
        # THE PULSE (Dallon, Jul 15: 'watch this on the scoreboard') —
        # a compact summary the Scoreboard card renders; refreshed on
        # every grading-room open + hourly by the poller.
        _sends = _blob_rw("draft_sends", []) or []
        _pa = [x["acc"] for x in _sends] if _sends else accs
        _blob_save("autorespond_pulse", {
            "at": now.isoformat(timespec="seconds"),
            "live": pulse_live[:8], "gated": gated,
            "n": len(_pa), "live_sends": bool(_sends),
            "good": sum(1 for a in _pa if a >= 85),
            "avg": round(sum(_pa) / len(_pa), 1) if _pa else 0})
    except Exception:
        pass
    return page("Auto-respond shadow", body)


def _reply_wheel(accs, live=False):
    """Reply accuracy, wheel-style (Dallon, Jul 14: 'almost like the
    +/- 10% score we have'). ≥85% similarity = the office sent it
    essentially as drafted. Today it grades retro pairs; the moment
    messaging turns on, real sends feed the same numbers."""
    if not accs:
        return ""
    n = len(accs)
    good = sum(1 for a in accs if a >= 85)
    avg = sum(accs) / n
    pct = good / n * 100
    circ = 2 * 3.14159 * 44
    dash = circ * (1 - pct / 100)
    return f"""
<div class='card' style='margin-bottom:14px'>
 <div class='schead'><h2>🎯 Reply accuracy</h2>
  <span class='subtext'>{"LIVE SENDS — pre-filled box vs what actually "
  "went out" if live else "how close our drafts land to what the "
  "office actually sends"} (dates/prices excluded from the grade)
  </span></div>
 <div style='display:flex;gap:24px;align-items:center;flex-wrap:wrap'>
  <div style='flex:none;position:relative;width:110px;height:110px'>
   <svg width='110' height='110'>
    <circle cx='55' cy='55' r='44' fill='none' stroke='var(--soft)'
     stroke-width='10'/>
    <circle cx='55' cy='55' r='44' fill='none' stroke='#8fc7a6'
     stroke-width='10' stroke-linecap='round'
     stroke-dasharray='{circ:.0f}' stroke-dashoffset='{dash:.0f}'
     transform='rotate(-90 55 55)'/>
   </svg>
   <div style='position:absolute;inset:0;display:flex;flex-direction:
    column;align-items:center;justify-content:center'>
    <b class='tab' style='font-size:22px'>{pct:.0f}%</b>
    <span class='subtext' style='font-size:9px'>sent as drafted</span>
   </div>
  </div>
  <div style='flex:1;min-width:200px;font-size:13.5px'>
   <div><b>{good} of {n}</b> graded replies went out essentially as
   drafted (≥85% match).</div>
   <div class='subtext' style='margin-top:4px'>Average similarity:
   <b>{avg:.0f}%</b>. What the office changed — and what the system
   learned from it — is itemized in the two 📖 sections below.</div>
  </div>
 </div>
</div>"""


def _adopted_rows(adopted, off):
    rows = []
    for kind, a in sorted(adopted.items(), key=lambda kv: -kv[1]["count"]):
        rows.append(
            f"<div style='border-top:1px solid var(--line);padding:10px 0'>"
            f"<b>{esc(kind)}</b> <span class='subtext'>their wording "
            f"repeated {a['count']}× — {int(a['share']*100)}% of "
            f"{a['sample_n']} graded replies</span>"
            f"<div style='white-space:pre-wrap;font-size:13.5px;"
            f"background:var(--soft);border-radius:8px;padding:10px 12px;"
            f"margin:6px 0'>{esc(a['template'])}</div>"
            f"<form method='POST' action='/autodraft_tpl_off' "
            f"style='display:inline'><input type='hidden' name='kind' "
            f"value='{esc(kind)}'><button class='gray' style='font-size:"
            f"12px'>Turn this one off</button></form></div>")
    for kind in sorted(off):
        rows.append(
            f"<div style='border-top:1px solid var(--line);padding:10px 0'>"
            f"<b>{esc(kind)}</b> <span class='subtext'>auto-wording OFF "
            f"(built-in template in use)</span> "
            f"<form method='POST' action='/autodraft_tpl_off' "
            f"style='display:inline'><input type='hidden' name='kind' "
            f"value='{esc(kind)}'><input type='hidden' name='on' "
            f"value='1'><button class='gray' style='font-size:12px'>"
            f"Re-enable</button></form></div>")
    return "".join(rows)


def _learn_rows(store):
    """draft_learnings → readable rows: per reply type, the sentences
    the office repeatedly ADDS that our template lacks (template-change
    candidates — adopted only via Dallon, policy doctrine) and ours
    they drop. Dates/times/prices already normalized out."""
    rows = []
    for kind, k in sorted(store.items(), key=lambda kv: -kv[1]["pairs"]):
        adds = [f"<li>{esc(s)} <span class='subtext'>×{c}</span></li>"
                for s, c in list(k["added"].items())[:5] if c >= 2]
        drops = [f"<li>{esc(s)} <span class='subtext'>×{c}</span></li>"
                 for s, c in list(k["dropped"].items())[:3] if c >= 2]
        if not adds and not drops:
            continue
        rows.append(
            f"<div style='border-top:1px solid var(--line);padding:10px "
            f"0'><b>{esc(kind)}</b> <span class='subtext'>"
            f"({k['pairs']} graded pairs)</span>"
            + (f"<div class='subtext' style='margin-top:4px'>the office "
               f"adds:</div><ul style='margin:4px 0 0 18px'>"
               + "".join(adds) + "</ul>" if adds else "")
            + (f"<div class='subtext' style='margin-top:4px'>ours they "
               f"drop:</div><ul style='margin:4px 0 0 18px'>"
               + "".join(drops) + "</ul>" if drops else "")
            + "</div>")
    return "".join(rows)


# ── THE STEP-BY-STEP TRAINING (Dallon, Jul 14: "a page of training,
# pictures, mock ups, examples — step by step how to use this website").
# Every mockup below is drawn with the dashboard's own styling so what
# the office reads looks exactly like what they click. Printable.
def _t_step(n, title, body):
    return (f"<div class='card' style='margin:0 0 14px'>"
            f"<div style='display:flex;gap:12px;align-items:baseline'>"
            f"<div style='flex:none;width:30px;height:30px;border-radius:"
            f"50%;background:#0b3d2e;color:#fff;display:flex;align-items:"
            f"center;justify-content:center;font-weight:800;font-size:14px"
            f"'>{n}</div><h3 style='margin:0;font-size:16.5px'>{title}"
            f"</h3></div><div style='margin-top:8px;font-size:14px;"
            f"line-height:1.6'>{body}</div></div>")


def _t_mock(inner):
    return (f"<div style='border:1px dashed rgba(201,162,39,.5);"
            f"border-radius:10px;padding:12px;margin:10px 0;"
            f"background:var(--soft)'>"
            f"<div style='font-size:9.5px;font-weight:800;letter-spacing:"
            f"1px;text-transform:uppercase;color:var(--goldink);"
            f"margin-bottom:6px'>example — this is what it looks like"
            f"</div>{inner}</div>")


def _guide_training():
    chip = lambda t, c="#e8c76a": (f"<span style='color:{c};font-weight:"
                                   f"800;font-size:11px'>{t}</span>")
    row_mock = _t_mock(
        "<div style='padding:10px;border-radius:10px;background:"
        "var(--card)'><div style='font-size:14.5px;font-weight:800'>"
        "<span style='display:inline-block;width:8px;height:8px;"
        "border-radius:50%;background:#c9a227;margin-right:6px'></span>"
        "Karen R</div><div style='margin:2px 0'>"
        + chip("📋 bid request", "#8fc7a6") + " "
        + chip("🏘 realtor", "#79aede") + " "
        + chip("🪔 Diwali lights") + " "
        + chip("🗑 done in Gmail", "var(--mut)")
        + "<span style='float:right;font-weight:700;color:var(--mut)'>"
        "$225</span></div>"
        "<div style='color:var(--mut);font-size:12.5px'>← Hi Martha, I "
        "think I'd like to go ahead and schedule the driveway…</div>"
        "</div>")
    reply_mock = _t_mock(
        "<div style='background:rgba(201,162,39,.12);color:var(--goldink);"
        "font-size:12px;font-weight:800;border-radius:8px;padding:6px "
        "10px;margin-bottom:6px'>✨ DRAFT READY — written the way the "
        "office writes. Edit anything, then send.</div>"
        "<div style='background:var(--card);border-radius:8px;padding:"
        "10px;font-size:13px;white-space:pre-wrap'>Great!  We have your "
        "appointment confirmed on July 22nd for a gutter cleaning.  "
        "Thank you for booking with us.  We look forward to servicing "
        "your home!\n\nAt your service,\nLaRee</div>"
        "<div style='display:flex;justify-content:space-between;"
        "margin-top:6px;align-items:center'><span class='subtext'>Sends "
        "as customercare@ · your edits teach the brain</span>"
        "<button class='big' type='button'>Send reply</button></div>")
    divider_mock = _t_mock(
        "<div style='display:flex;align-items:center;gap:10px'>"
        "<div style='flex:1;height:2px;background:rgba(201,162,39,.35)'>"
        "</div><div style='background:rgba(201,162,39,.14);border:1px "
        "solid rgba(201,162,39,.4);border-radius:20px;padding:5px 16px;"
        "font-size:13px;font-weight:800;color:var(--goldink)'>📋 "
        "following up on quote #36433 · <u>→ Karen R</u></div>"
        "<div style='flex:1;height:2px;background:rgba(201,162,39,.35)'>"
        "</div></div>")
    return (
        "<div style='display:flex;justify-content:space-between;"
        "align-items:center;margin:18px 0 10px'>"
        "<h2 style='margin:0;font-size:19px'>📚 Step-by-step: working a "
        "day in the dashboard</h2>"
        "<button class='gray' type='button' onclick='window.print()' "
        "style='font-size:12px'>🖨 Print / save as PDF</button></div>"

        + _t_step(1, "Start in 📬 Inbox — it mirrors Gmail",
            "Bold rows with a gold dot are <b>unhandled</b> — same as "
            "unread in Gmail. <b>Work bottom to top — oldest first</b>, "
            "exactly like you did in Gmail, so nobody waits longest. "
            "The chips on each row tell you what you're walking into "
            "before you click:"
            + row_mock +
            "<b>📋 bid request</b> = they want a quote · <b>💬 question"
            "</b> = answer, don't quote · <b>✅ approved — wants a date"
            "</b> = money asking to be scheduled · <b>🏘 realtor</b> = "
            "price PER HOUSE, ask their deadline · <b>🪔 Diwali lights"
            "</b> = offer early-October install · <b>🗑 done in Gmail"
            "</b> = someone already finished this in Gmail (it sinks to "
            "the bottom by itself) · <b>🖐 office on it</b> = a teammate "
            "is working it.")

        + _t_step(2, "Open the customer — everything lives on one card",
            "The conversation (emails AND voicemails), their draft "
            "quote, photos and 3D flyover, their history at this house, "
            "and the must-know pins. <b>If a fact is wrong</b> (stories, "
            "roof, debris) use <b>Fix the facts</b> — the price recomputes "
            "itself. You edit <i>facts</i>, never prices; prices come "
            "from the calibrated engine and the customer's own history "
            "(never quote a returning customer below their last invoice).")

        + _t_step(3, "Reply — the box may already be written",
            "When a customer's message matches a safe pattern, the reply "
            "is <b>pre-filled in their file, in our office voice</b>:"
            + reply_mock +
            "Read it, change anything you like, hit <b>Send reply</b> — "
            "it sends as customercare@ instantly. Your edits literally "
            "train tomorrow's drafts. No pre-fill? Use the <b>Quick "
            "responses</b> dropdown (Ctrl/Cmd + 1–9 are hotkeys) or ✨ "
            "<b>Draft a reply for me</b>. Nothing ever sends without a "
            "person clicking.")

        + _t_step(4, "🤖 Drafts — the engine asking for a yes",
            "Each draft shows its price, the customer's last-paid "
            "anchors, and any ⚠ amber warnings (seasonal rules, Tom-only "
            "roofs, guard exceptions). Approve to push it into Jobber as "
            "a draft quote — a human still sends it from Jobber. If the "
            "price looks off, fix the FACTS and let it reprice, or 🚩 "
            "flag it for Dallon &amp; Tom.")

        + _t_step(5, "Asking Dallon about a property — one habit",
            "Email him like always, but <b>keep the quote # in the "
            "subject</b> (e.g. “Following up on quote #36433”). His "
            "answer then files itself onto that customer's card as a 💰 "
            "note, and your internal thread splits by topic so three "
            "houses never blur together:"
            + divider_mock +
            "Click the <b>→ name</b> to jump straight to that customer.")

        + _t_step(6, "👷 Tech mail — internal, never a bid",
            "Emails from our techs get their own purple card: the "
            "conversation as a chat, <b>every photo they sent right "
            "above it</b>, and a 📎 link to the customer it's about. "
            "Answer it like a text message. If a tech's question is "
            "really about pricing a customer, open that customer's card "
            "and handle it there.")

        + _t_step(7, "The five bubbles, Move ▾, and ✓ Done",
            "Just five boxes now: <b>📬 Inbox</b> (messages, voicemails, "
            "and 🔧 fix-its — answered rows clear themselves), <b>🤖 "
            "Drafts</b>, <b>📅 Won</b>, <b>📤 Waiting</b> (including ⏰ "
            "gone-quiet nudges and 🚫 declines), and <b>👷 Techs</b>. "
            "Rows a teammate is already quoting in Jobber live in the "
            "<b>Handled in Jobber</b> fold at the bottom — don't "
            "double-quote. <b>✓ Done</b> clears a row for the whole "
            "office (a new customer message always brings it back — "
            "nothing is ever lost). <b>Move ▾</b> files it, and every "
            "move teaches the sorter. <b>🏜 Tom's dry-day standby</b> "
            "(bottom fold) holds every high-risk/Tom-only home waiting "
            "for a dry window — any season, his call; nobody falls out "
            "of it until the job converts. Tom works it from his own "
            "board (link on the fold): he picks a date, the office gets "
            "notified to book it in Jobber. The 🕵️ hourly review re-checks "
            "every bubble and posts anything odd on the "
            "<a href='/working'>build board</a>.")

        + "<div class='card' style='background:rgba(11,61,46,.06);"
          "border-left:4px solid #0b3d2e'><b>The golden rules</b>"
          "<ul style='margin:8px 0 0 18px;font-size:14px'>"
          "<li><b>Bold = nobody's handled it.</b></li>"
          "<li><b>Nothing sends to a customer without a human click."
          "</b></li><li>Fix <b>facts</b>, not prices — the engine "
          "reprices itself.</li>"
          "<li>Label every discount with its one-word reason.</li>"
          "<li>Quote # in the subject when you ask Dallon.</li>"
          "<li>When in doubt: open the card and read the conversation "
          "— everything is there.</li></ul></div>")


def tom_standby_page(user=None):
    """🏜 TOM'S BOARD (Dallon, Jul 15: 'make Tom's standby bigger so
    Tom can work through that too — or throw them on a day when he
    wants, directly from the dashboard'). His weather-window work, his
    call: the full standby list grouped by area, and a one-click 'put
    them on this day' that stamps the record, tells the office, and
    preps the customer email. No Jobber writes — the office books it;
    Tom's pick drives it."""
    ts = _blob_rw("tom_standby", {}) or {}
    cust = ts.get("customers") or []
    picks = _blob_rw("tom_days", {}) or {}
    from collections import defaultdict
    bycity = defaultdict(list)
    picked_emails = {c.get("email") for d2 in picks.values()
                     for c in d2}
    for c in cust:
        city = (c.get("address") or "").split(",")[-2].strip().title() \
            if "," in (c.get("address") or "") else "Other"
        bycity[city].append(c)

    def card(c):
        e = c.get("email") or ""
        done = e in picked_emails
        return f"""
<div class='card' style='margin:0 0 10px;{'opacity:.55' if done else ''}'>
 <div style='display:flex;justify-content:space-between;align-items:
  center;flex-wrap:wrap;gap:8px'>
  <div><a href='/?c={urllib.parse.quote(e)}' style='font-weight:800;
   font-size:16px'>{esc(c.get('name') or e)}</a>
   <div class='subtext'>{esc(c.get('address') or 'no address on file')}
    · {esc(c.get('quote_status'))}</div></div>
  <form method='POST' action='/tom_pick' style='display:flex;gap:6px;
   align-items:center'>
   <input type='hidden' name='email' value='{esc(e)}'>
   <input type='hidden' name='name' value='{esc(c.get('name') or '')}'>
   <input type='hidden' name='stamp' value='{esc(c.get('stamp') or '')}'>
   <input type='date' name='day' required style='padding:6px'>
   <button class='big' onclick="return confirm('Put '
    + this.form.name.value + ' on this day? The office gets notified '
    + 'and books it in Jobber.')">📅 Put on this day</button>
  </form>
 </div>
 {f"<div class='subtext' style='margin-top:4px'>✅ already picked</div>" if done else ""}
</div>"""

    sections = "".join(
        f"<h3 style='margin:18px 0 8px'>📍 {esc(city)} "
        f"({len(cs)})</h3>" + "".join(card(c) for c in cs)
        for city, cs in sorted(bycity.items(),
                               key=lambda kv: -len(kv[1])))
    picked_html = ""
    for d2, cs in sorted(picks.items()):
        picked_html += (f"<div style='padding:8px 12px;border-top:1px "
                        f"solid var(--line)'><b>{esc(d2)}</b> — "
                        + ", ".join(esc(c.get('name') or '?')
                                    for c in cs)
                        + f" <span class='subtext'>({len(cs)} home"
                          f"{'s' if len(cs) != 1 else ''})</span></div>")
    body = f"""
<div style='max-width:820px'>
 <h2 style='margin:4px 0 2px;font-size:22px'>🏜 Tom's dry-day board</h2>
 <div class='subtext' style='margin-bottom:14px'>Every high-risk /
 Tom-only home waiting for a dry window — any season, your call.
 Pick a date and the office is notified to book it in Jobber; the
 customer's confirmation email preps itself on their card. Nobody
 leaves this list until their job converts.</div>
 {f"<div class='card'><b>📅 Days you've claimed</b>{picked_html}</div>"
  if picked_html else ""}
 {sections or "<div class='card subtext'>Standby is empty — every"
              " Tom-only home is booked or done. 🎉</div>"}
</div>"""
    return page("Tom's board", body)


def pw_winback_page():
    """💦 PW WIN-BACK LIST (Dallon's running list, Jul 15 — the
    day-matched race showed pressure washing down $4,790 this July and
    ~$50k in April). Everyone who bought PW in the last two years and
    hasn't this year, worth-most-first, from the local service archive.
    Read-only: no Jobber calls, no sends — a call sheet."""
    try:
        import winback
        W = winback.load()
    except Exception as ex:
        return page("PW win-backs", f"<div class='card'>couldn't build "
                                    f"the list: {esc(str(ex))}</div>")
    rows = W.get("rows") or []

    def row(r):
        yrs = ", ".join(str(y) for y in r["pw_years"][-4:])
        still = ("<span class='chip' style='background:#2e5c46;"
                 "color:#dff3e7;border-radius:12px;padding:1px 9px;"
                 "font-size:11.5px'>STILL A CUSTOMER — bought "
                 + esc(", ".join(r["this_year_bought"]).replace("_", " "))
                 + " this year</span>") if r["still_customer"] else ""
        return (
            f"<div style='border-top:1px solid var(--line);padding:9px 0;"
            f"display:flex;justify-content:space-between;gap:10px;"
            f"flex-wrap:wrap;align-items:center'>"
            f"<div><a href='/customers?q={urllib.parse.quote(r['who'])}'"
            f" style='font-weight:800'>{esc(r['who'])}</a>"
            f"<div class='subtext'>last PW {esc(r['last_pw'])} "
            f"({esc(r['last_pw_service'])}) · PW years: {yrs}</div>"
            f"{still}</div>"
            f"<div style='font-weight:800;font-size:17px;white-space:"
            f"nowrap'>${r['last_pw_total']:,.0f}</div></div>")

    tA = [r for r in rows if r["tier"] == "A"]
    tB = [r for r in rows if r["tier"] == "B"]
    body = f"""
<div style='max-width:820px'>
 <h2 style='margin:4px 0 2px;font-size:22px'>💦 Pressure-washing
 win-backs</h2>
 <div class='subtext' style='margin-bottom:14px'>Bought pressure
 washing in the last two years, none this year. Dollar shown = what
 their last PW visit was worth. Click a name to open their customer
 card. Built fresh from the invoice archive each time you open this
 page — nobody here has been contacted by anything automatic.</div>
 <div class='card' style='display:flex;gap:26px;flex-wrap:wrap'>
  <div><div style='font-size:26px;font-weight:800'>${W['value']:,.0f}
   </div><div class='subtext'>at last-visit prices</div></div>
  <div><div style='font-size:26px;font-weight:800'>{len(tA)}</div>
   <div class='subtext'>repeaters gone quiet</div></div>
  <div><div style='font-size:26px;font-weight:800'>{len(tB)}</div>
   <div class='subtext'>last year only</div></div>
  <div><div style='font-size:26px;font-weight:800'>
   {W['still_customer']}</div><div class='subtext'>still buy other
   work — easiest asks</div></div>
 </div>
 <div class='card'><b>🥇 Repeaters gone quiet ({len(tA)})</b>
  <div class='subtext'>They bought PW two or more years running, then
  stopped. Habit broken — one call usually restarts it.</div>
  {"".join(row(r) for r in tA)}</div>
 <div class='card'><b>🥈 Last year only ({len(tB)})</b>
  <div class='subtext'>One-time PW buyers from last season.</div>
  {"".join(row(r) for r in tB[:120])}
  {f"<div class='subtext' style='padding-top:8px'>…and "
   f"{len(tB) - 120} more (worth-most shown first).</div>"
   if len(tB) > 120 else ""}</div>
</div>"""
    return page("PW win-backs", body)


def guide_page():
    """The office manual, living where the office lives (Dallon Jul 9:
    'a tab that instructs... a FAQ type of situation'; Jul 14: full
    step-by-step training with mockups, printable/sendable)."""
    folds = "".join(
        f"<details class='ifold'><summary>{q}</summary>"
        f"<div class='fbody' style='font-size:14.5px;line-height:1.65'>"
        f"{a}</div></details>"
        for q, a in GUIDE_FAQ)
    body = (
        "<div style='max-width:760px'>"
        "<h2 style='margin:4px 0 2px;font-size:22px;letter-spacing:-.4px'>"
        "How this dashboard works</h2>"
        "<div class='subtext' style='margin-bottom:12px'>Two rules cover "
        "almost everything: <b>bold means nobody's handled it</b>, and "
        "<b>nothing sends to a customer without a human</b>. The rest is "
        "detail — the step-by-step below, then tap any question. "
        "Curious what's being built? "
        "<a href='/working' style='font-weight:700'>🔭 The build board</a>"
        "</div>"
        + _guide_training()
        # DISCOUNT LABELING TRAINING — AT THE TOP (Dallon, Jul 12:
        # 'anything critical shouldn't have to be searched for')
        + """
<div style='margin:0 0 16px;background:var(--card);border:1px solid
 rgba(201,162,39,.3);border-left:4px solid #c9a227;border-radius:12px;
 padding:16px 18px'>
 <b style='font-size:16px'>🏷 Training: label every discount — one word
 of WHY</b>
 <div style='margin:8px 0 10px;font-size:14px'>We went back through
 every discount ever written — <b>$665,013 all-time</b>. Most notes
 tell us why, and those turn into patterns we can steer by. But
 <b>$72,867 went out as just the word "Discount"</b> — money nobody can
 learn from. The fix costs one word.</div>
 <div style='font-size:14px;font-weight:800;margin-bottom:6px'>When you
 give a discount, start the note with the reason — pick one:</div>
 <table style='font-size:13.5px'>
  <tr><th>Write this…</th><th>…when it's</th></tr>
  <tr><td><b>October</b> / <b>Early install</b></td><td>the lights
   early-install rate</td></tr>
  <tr><td><b>Feb/Mar bundle</b></td><td>the 15% two-service
   Feb–March deal</td></tr>
  <tr><td><b>Aug/Sept</b></td><td>the slow-season 15%</td></tr>
  <tr><td><b>Bundle</b></td><td>multiple services, any season</td></tr>
  <tr><td><b>Neighbor</b></td><td>booked with a neighbor / same
   trip</td></tr>
  <tr><td><b>Honor</b></td><td>honoring an old or quoted
   price</td></tr>
  <tr><td><b>F&amp;F</b></td><td>friends &amp; family</td></tr>
  <tr><td><b>Senior</b> / <b>Military</b></td><td>exactly what it
   says</td></tr>
  <tr><td><b>Referral</b></td><td>thank-you for sending us
   someone — name them!</td></tr>
  <tr><td><b>Goodwill</b></td><td>making something right</td></tr>
 </table>
 <div class='subtext' style='margin-top:8px'>Example: “<b>Neighbor</b>
 — booked with the Hansens next door, 10%.” That's it. The system
 reads these notes, keeps the discount playbook current on Settings,
 and makes sure discounted visits never lower anyone's future price.
 </div>
</div>"""
        # ROW-TAGS TRAINING (Dallon, Jul 13: 'create a training of all
        # the tags so I can pass it to the office')
        + """
<div style='margin:0 0 16px;background:var(--card);border:1px solid
 rgba(201,162,39,.3);border-left:4px solid #c9a227;border-radius:12px;
 padding:16px 18px'>
 <b style='font-size:16px'>🏷 Training: what the tags on a row mean</b>
 <div class='subtext' style='margin:6px 0 10px'>Every row wears small
 colored words. Each one answers a question — read them left to right
 and you know what to do without opening the card.</div>
 <table style='width:100%;font-size:13px;line-height:1.5'>
  <tr><td colspan='2' style='padding-top:6px;color:var(--mut);
   font-size:11.5px;font-weight:800'>WHAT KIND OF ASK IS THIS?</td></tr>
  <tr><td style='white-space:nowrap;font-weight:700;color:#8fc7a6'>📋
   bid request</td><td>they named services — get them a price</td></tr>
  <tr><td style='font-weight:700;color:#e8c76a'>💬 question</td>
   <td>words, no service ask — answer them, don't quote</td></tr>
  <tr><td style='font-weight:700;color:#79aede'>📅 about their visit</td>
   <td>already booked in Jobber and wrote in — confirm the schedule</td></tr>
  <tr><td colspan='2' style='padding-top:10px;color:var(--mut);
   font-size:11.5px;font-weight:800'>WHO HAS IT</td></tr>
  <tr><td style='font-weight:700;color:#79aede'>🔵 [name] is working
   this</td><td>opened in the last 15 min — leave it or check with
   them</td></tr>
  <tr><td style='font-weight:700;color:#e8c76a'>🚶 stepped away</td>
   <td>someone started, then left — anyone can pick it up</td></tr>
  <tr><td style='font-weight:700'>with Dallon &amp; Tom</td>
   <td>a person escalated it — wait for their call</td></tr>
  <tr><td colspan='2' style='padding-top:10px;color:var(--mut);
   font-size:11.5px;font-weight:800'>KNOW THIS CUSTOMER</td></tr>
  <tr><td style='font-weight:700;color:#f2b8b5'>⚠️ bad payer</td>
   <td>flagged with a note (banner on their card) — read it before
   promising anything</td></tr>
  <tr><td style='font-weight:700;color:#e8c76a'>👀 watch</td>
   <td>be careful — the card explains why</td></tr>
  <tr><td style='font-weight:700;color:#8fc7a6'>⭐ VIP</td>
   <td>treat extra well</td></tr>
  <tr><td style='font-weight:700;color:#f2b8b5'>⛔ do not service</td>
   <td>never quote or book — approve is blocked</td></tr>
  <tr><td colspan='2' style='padding-top:10px;color:var(--mut);
   font-size:11.5px;font-weight:800'>CONVERSATION TEMPERATURE</td></tr>
  <tr><td style='font-weight:700;color:#f2b8b5'>⚠ urgent</td>
   <td>worried customer, floats to the very top — handle first</td></tr>
  <tr><td style='font-weight:700;color:#8fc7a6'>🟢 active</td>
   <td>back-and-forth within 3 days — being worked, leave it</td></tr>
  <tr><td style='font-weight:700;color:var(--mut)'>🔕 quiet Nd</td>
   <td>gone silent — worth a nudge</td></tr>
  <tr><td colspan='2' style='padding-top:10px;color:var(--mut);
   font-size:11.5px;font-weight:800'>WHERE THE QUOTE STANDS</td></tr>
  <tr><td style='font-weight:700;color:#8fc7a6'>ready to approve</td>
   <td>the engine priced it confidently — check and click approve</td></tr>
  <tr><td style='font-weight:700;color:#8fc7a6'>won — schedule it</td>
   <td>they said yes — get it on the calendar</td></tr>
  <tr><td style='font-weight:700'>quote sent</td>
   <td>ball's in the customer's court — sits in Waiting</td></tr>
  <tr><td style='font-weight:700;color:#e8c76a'>🖊️ office is
   drafting</td><td>a quote is already started in Jobber — finish it
   there, don't re-quote</td></tr>
  <tr><td style='font-weight:700;color:#e8c76a'>⏰ review — quote ~X mo
   old</td><td>stale quote — treat as a new request; archive the old
   one in Jobber</td></tr>
  <tr><td style='font-weight:700;color:#e8c76a'>🔀 last quote was for
   […]</td><td>old quote was a different job — this is a new
   request</td></tr>
  <tr><td colspan='2' style='padding-top:10px;color:var(--mut);
   font-size:11.5px;font-weight:800'>NOT A CUSTOMER BID</td></tr>
  <tr><td style='font-weight:700;color:#b79ade'>👷 tech note</td>
   <td>field mail from our crew — never a bid to send</td></tr>
  <tr><td style='font-weight:700;color:#b79ade'>📨 internal — office ↔
   Dallon &amp; Tom</td><td>our own questions to the owners — lives in
   the Techs tab</td></tr>
  <tr><td style='font-weight:700;color:#e8c76a'>🔧 follow-up on
   completed work</td><td>job's done; they wrote about the work — a
   person handles it, no bid</td></tr>
 </table>
</div>"""
        + folds +
        # the top-bar 💡 Idea button lands here (Jessica, Jul 9)
        "<div id='idea' style='margin-top:16px;background:var(--soft);"
        "border:1px solid var(--line);border-radius:12px;padding:14px 16px'>"
        "<b>💡 Send Dallon an idea</b>"
        "<div class='subtext' style='margin:4px 0 8px'>Emails him "
        "instantly, and Claude starts planning the fix overnight.</div>"
        "<form method='POST' action='/idea_send' style='display:flex;"
        "gap:8px'><input type='hidden' name='back' value='/guide#idea'>"
        "<input name='text' required placeholder='What should "
        "this dashboard do better?' style='flex:1'>"
        "<button>Send</button></form></div></div>")
    return page("Guide", body)


def flyover_page(addr):
    """Google Aerial View orbit video — every side of the house.
    (LaRee's questionnaire wish; solves 'can't find home pictures'.)"""
    if not addr:
        return page("3D flyover", "<div class='card'>No address.</div>")
    from aerial_view import lookup
    state, payload = lookup(addr)
    head = (f"<a href='javascript:history.back()'>&larr; back to the bid"
            f"</a><h2 style='margin:10px 0 4px'>🎥 {esc(addr)}</h2>")
    if state == "ACTIVE":
        mp4 = ((payload.get("MP4_HIGH") or payload.get("MP4_MEDIUM")
                or payload.get("MP4_LOW") or {}).get("landingPageUri")
               if isinstance(payload, dict) else None)
        # prefer the raw video uri when present
        for k in ("MP4_HIGH", "MP4_MEDIUM", "MP4_LOW"):
            u = (payload.get(k) or {})
            if u.get("uri"):
                mp4 = u["uri"]
                break
        if mp4:
            body = (head + f"<div class='card' style='max-width:900px'>"
                    f"<video controls autoplay muted loop "
                    f"style='width:100%;border-radius:12px' "
                    f"src='{esc(mp4)}'></video>"
                    f"<div class='subtext' style='margin-top:6px'>Google's "
                    f"3D orbit of the property — every side of the house. "
                    f"Pause on the sides the street can't see.</div></div>")
            return page("3D flyover", body)
        state = "ERROR"
    if state == "PROCESSING" or state == "NOT_FOUND":
        return page("3D flyover", head +
                    "<div class='card' style='max-width:640px'>⏳ Google is "
                    "rendering this home's flyover now — usually a few "
                    "minutes. This page refreshes itself.</div>", refresh=45)
    if state == "DISABLED":
        return page("3D flyover", head +
                    "<div class='card' style='max-width:640px'>🔒 Needs "
                    "Dallon's one-time enable (Aerial View API — same two "
                    "clicks as Speech-to-Text). Tell him it's ready to "
                    "switch on.</div>")
    return page("3D flyover", head +
                "<div class='card' style='max-width:640px'>Google doesn't "
                "have 3D coverage for this address (or the lookup "
                "hiccuped). The Zillow/Redfin links on the bid are the "
                "fallback.</div>")


def _canned_payload():
    """Quick responses for the dropdowns: the shared set + everyone's
    personal sets (Jessica, Jul 9). The page's JS picks the personal set
    matching the name cookie and marks those entries ★."""
    shared = _blob_rw("canned_replies", {})
    personal = _blob_rw("canned_replies_personal", {})
    # MOST-USED FIRST (LaRee, Jul 10: 'organize them to the top for most
    # used, least used at bottom') — every pick bumps a counter; the
    # dropdowns re-order themselves to how the office actually works.
    usage = _blob_rw("qr_usage", {})
    shared = dict(sorted(shared.items(),
                         key=lambda kv: -usage.get(kv[0], 0)))
    return json.dumps({"shared": shared,
                       "personal": personal}).replace("</", "<\\/")


_CANNED_MERGE_JS = """
function mergeCanned(payload){
  var m = document.cookie.match(/office_user=([^;]+)/);
  var mine = m ? (payload.personal[decodeURIComponent(m[1])] || {}) : {};
  var out = {};
  Object.keys(payload.shared).forEach(function(k){out[k]=payload.shared[k];});
  Object.keys(mine).forEach(function(k){out['\\u2605 '+k]=mine[k];});
  return out;
}
// HOT-KEYS (LaRee, Jul 10): Ctrl/Cmd + 1..9 drops in that numbered
// quick response — works in every reply box that has the dropdown.
// The dropdown labels show their number so there's nothing to memorize.
if (!window.__qrHotkeys) {
  window.__qrHotkeys = true;
  function __qrSelects(){
    return [].slice.call(document.querySelectorAll("select[id$='canned']"));
  }
  setTimeout(function number(){
    __qrSelects().forEach(function(s){
      for (var i = 1; i < s.options.length && i <= 9; i++) {
        if (!/^\\d \\u00b7 /.test(s.options[i].textContent))
          s.options[i].textContent = i + ' \\u00b7 ' + s.options[i].textContent;
      }
    });
  }, 400);
  // usage beacon — every pick bumps its counter so dropdowns re-order
  // to most-used first (LaRee, Jul 10)
  document.addEventListener('change', function(e){
    var t = e.target;
    if (!t.matches || !t.matches("select[id$='canned']") || !t.value) return;
    var nm = t.value.replace(/^\\u2605 /, '');
    try { navigator.sendBeacon('/qr_used',
      new Blob(['name=' + encodeURIComponent(nm)],
               {type:'application/x-www-form-urlencoded'})); } catch(x) {}
  }, true);
  document.addEventListener('keydown', function(e){
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
    var n = parseInt(e.key);
    if (!(n >= 1 && n <= 9)) return;
    var sels = __qrSelects();
    if (!sels.length) return;
    var s = sels[0];
    if (n >= s.options.length) return;
    e.preventDefault();
    s.selectedIndex = n;
    s.dispatchEvent(new Event('change'));
  });
}
"""


def _tax_glance(address, services=None):
    """Small at-a-glance sales-tax chip by the address (Jessica, Jul 9:
    'verify the tax situation... a small at a glance by the address').
    Rate comes from WA DOR's own per-address API; cached forever per
    address in the tax_glance blob so pages stay fast.

    Window cleaning is tax-exempt (Dallon, Jul 10): a windows-only job
    shows 'Tax Exempt', a mixed job notes windows are excluded — so the
    chip matches what the quote will actually charge."""
    if not address:
        return ""
    # is this job windows only / windows-inclusive?
    def _is_win(n):
        n = str(n or "").lower()
        return "window" in n or n.startswith("windows_")
    wins = [s for s in (services or []) if _is_win(s)]
    all_windows = bool(services) and len(wins) == len(services)
    slug = _slug(address)
    cache = _blob_rw("tax_glance", {})
    hit = cache.get(slug)
    # a FAILED lookup is retried after 6h, not cached forever (Terry
    # Brower's zip-less caller-ID address failed once and the chip
    # vanished permanently, Jul 10)
    import time as _t
    stale_fail = (hit is not None and not hit.get("code")
                  and _t.time() - (hit.get("at") or 0) > 6 * 3600)
    if hit is None or stale_fail:
        try:
            import jobber_client as jc
            a = jc.split_address(address)
            if (a.get("province") or "WA") != "WA":
                hit = {"code": None, "rate": None}
            else:
                code, rate = jc.wa_dor_location_code(
                    a["street1"], a["city"], a["postalCode"])
                hit = {"code": code, "rate": rate}
        except Exception:
            hit = {"code": None, "rate": None}
        hit["at"] = _t.time()
        cache[slug] = hit
        _blob_save("tax_glance", cache)
    if not hit.get("code"):
        return ""
    # Dallon's ruling (Jul 10 pm): the ADDRESS is always charged its city
    # rate — window lines are just non-taxable. So the chip always shows
    # the rate; windows-only adds '$0 — windows exempt' for clarity.
    excl = (" · $0 — windows exempt" if all_windows
            else " · windows exempt" if wins else "")
    return (f"<span class='chip' style='font-size:11.5px' title='Sales tax "
            f"straight from the WA Dept of Revenue for this exact address "
            f"(location code {esc(hit['code'])}) — what the quote will "
            f"charge{'; window cleaning is non-taxable in WA, tax applies to any other services' if wins else ''}'>"
            f"🧾 tax {hit['rate']*100:.2f}%{excl}</span>")


def _photo_refs(stamp, address):
    """Every ref a bid's photos might live under: the stamp plus address
    slugs — INCLUDING the street-only slug, because imagery saved before
    an address gets completed/corrected stays filed under the short form
    (Jessica Jensen, Jul 9: fixing her missing city orphaned her photos
    right when the first real quote pushed)."""
    refs = [stamp] if stamp else []
    a = (address or "").lower()
    for cand in (a, a.split(",")[0]):
        for cut in (60, 40):
            s = re.sub(r"[^a-z0-9]+", "-", cand).strip("-")[:cut]
            if s and s not in refs:
                refs.append(s)
    return refs


def _photo_token(ref, kind, idx):
    """Unguessable per-photo token — lets Jobber fetch bid photos
    without the office password ever leaving the building."""
    import hashlib
    import hmac as _hmac
    key = (_password() or "local").encode()
    return _hmac.new(key, f"{ref}|{kind}|{idx}".encode(),
                     hashlib.sha256).hexdigest()[:16]


def _photo_urls_for(stamp, address, host):
    """Signed public URLs for a bid's photos (customer + aerial/street)."""
    if not (clouddb.available() and host):
        return []
    urls = []
    for ref, kind, idx in clouddb.photos_index(_photo_refs(stamp, address)):
        if kind in ("eml", "jobber"):   # 'jobber' came FROM Jobber —
            continue                    # pushing it back is a loop
        # end with a real filename — Jobber names the attachment from
        # the URL tail (files landed as nameless '0' blobs without it)
        urls.append(f"https://{host}/pub/photo/"
                    f"{_photo_token(ref, kind, idx)}/{ref}/{kind}/"
                    f"{kind}-{idx}.jpg")
    return urls


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


def _discount_patterns_html():
    """The discount PLAYBOOK, mined from every discount note the
    office ever wrote (9,421 invoices — Dallon, Jul 12: 'consolidate
    those discounts into patterns'). Read-only truth beside the rules."""
    try:
        dp = _blob_rw("discount_patterns", {})
        pats = dp.get("patterns") or []
    except Exception:
        pats = []
    if not pats:
        return ""
    rows = "".join(
        f"<tr><td><b>{esc(p['pattern'])}</b></td>"
        f"<td class='num'>{p['n']:,}</td>"
        f"<td class='num'>{p['median_pct']:.0f}%</td>"
        f"<td class='num'>${p['median_amt']:,}</td>"
        f"<td class='num'>${p['total']:,}</td></tr>"
        for p in pats)
    return (f"<details style='margin-top:14px'><summary style='cursor:"
            f"pointer;font-weight:800;color:var(--mut)'>📊 What we've "
            f"ACTUALLY given — every discount ever written, consolidated "
            f"(${dp.get('total_given', 0):,} all-time)</summary>"
            f"<table style='margin-top:8px'><tr><th>Pattern</th>"
            f"<th class='num'>Times</th><th class='num'>Typical %</th>"
            f"<th class='num'>Typical $</th><th class='num'>All-time</th>"
            f"</tr>{rows}</table>"
            f"<div class='subtext' style='margin-top:6px'>Mined "
            f"{esc(dp.get('mined', ''))} from {dp.get('invoices', 0):,} "
            f"invoices' discount notes. Price floors already ignore all "
            f"of these — customers are matched at PRE-discount prices. "
            f"'Unlabeled' is the one to shrink: the labeling guide is on "
            f"the <a href='/guide'>Guide tab</a>. (F&amp;F runs deliberate"
            f" — slow-season filler, not lost money — Dallon, Jul 12.)"
            f"</div></details>")


def settings_page(msg="", user=None):
    """The office's own control room (Dallon: 'they work on this daily,
    I don't') — quick responses and pricing knobs, no code, no Dallon."""
    import bid_engine as be
    defaults = be.factory_defaults()
    ov = be._pricing_overrides()

    banner = (f"<div class='band'>{esc(msg)}</div>" if msg else "")

    # ---- pricing knobs (Stitch look: big value tiles, Jul 10 pm —
    # the first port kept the old dense table and Dallon couldn't see
    # the redesign; the CONTENT had to change shape, not just the CSS)
    def knob(key, label, default):
        cur = ov.get(key, "")
        shown = cur or default
        return (f"<label class='knob{' set' if cur else ''}'>"
                f"<span class='kl'>{esc(label)}</span>"
                f"<span class='kv'>{esc(str(shown))}"
                + ("<i>override</i>" if cur else "<i>default</i>")
                + f"</span><input type='text' name='ov_{esc(key)}' "
                f"value='{esc(cur)}' placeholder='{esc(str(default))}'>"
                f"</label>")

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
        "GUTTER_CLEANING_MINIMUM": "Gutter cleaning minimum, Mar–Sep ($)",
        "GUTTER_CLEANING_MINIMUM_WINTER": "Gutter minimum, Oct–Feb ($)",
        "WINDOWS_MINIMUM": "Windows exterior-only minimum ($)",
        "WINDOWS_MINIMUM_BUNDLED": "Windows exterior minimum when bundled ($)",
        "WINDOWS_INOUT_MINIMUM": "Windows in & out minimum ($)",
        "DRY_SEASON_ROOF_FLOOR": "Dry-season roof floor ($)",
        "DRY_DAY_DISCOUNT": "Dry-day discount (0.27 = 27%)",
        "DRYER_VENT_ADDON": "Dryer vent — with other work ($)",
        "DRYER_VENT_ALONE": "Dryer vent — alone ($)",
        "WET_DAY_GUTTER_MULT": "Wet-day gutter multiplier",
        "PW_HOUSE_WASH_RATE": "House wash rate ($/sqft)",
    }
    knobs = "".join(knob(k, scalar_labels.get(k, k), defaults[k])
                    for k in be.EDITABLE_SCALARS)
    drows = ""
    for dname in be.EDITABLE_DICTS:
        for sub, dval in defaults[dname].items():
            drows += row(f"{dname}.{sub}",
                         f"{dname.replace('_', ' ').title()} — {sub}", dval)
    pricing_card = f"""
<div class='card'>
 <div class='schead'>{_svg_icon('tune')}<h2>Pricing knobs</h2></div>
 <div class='subtext' style='margin-bottom:12px'>Type a number to
 override the default; clear the box to go back to default. Changes
 apply to the NEXT bid priced — nothing already on the queue moves.
 Every change is logged with your name.</div>
 <form method='POST' action='/settings_save'>
 <div class='knobgrid'>{knobs}</div>
 <details style='margin-top:12px'>
  <summary style='cursor:pointer;font-weight:800;color:var(--mut)'>
  Rates &amp; multipliers (advanced — small changes move every price)
  </summary>
  <table style='margin-top:8px'>
   <tr><th>Setting</th><th class='num'>Default</th><th>Override</th></tr>
   {drows}</table>
 </details>
 <button class='big' style='margin-top:12px'>Save pricing changes</button>
 </form>
 <form method='POST' action='/settings_reset' style='margin-top:8px'
  onsubmit="return confirm('Clear ALL pricing overrides and go back to '
   + 'the calibrated defaults?')">
  <button class='gray'>↩ Reset everything to defaults</button>
 </form></div>"""

    # ---- quick responses: Stitch template cards, 2-up grid ----
    canned = _blob_rw("canned_replies", {})

    def qrcard(name, text, mine=False):
        return f"""
<details class='qrcard'>
 <summary><span class='qtag'>{"★ mine" if mine else "shared"}</span>
  <b>{esc(name)}</b>
  <span class='qpeek'>{esc(text)[:110]}…</span></summary>
 <form method='POST' action='/qr_save' style='margin-top:8px'>
  {"<input type='hidden' name='mine' value='1'>" if mine else ""}
  <input type='hidden' name='name' value='{esc(name)}'>
  <textarea name='text' rows='5'>{esc(text)}</textarea>
  <div style='margin-top:6px'>
   <button>Save</button>
   <button name='delete' value='1' class='red'
    onclick="return confirm('Delete this response?')">Delete</button>
  </div></form></details>"""

    qr = "".join(qrcard(n, t) for n, t in canned.items())
    qr_card = f"""
<div class='card'>
 <div class='schead'>{_svg_icon('chat')}<h2>Quick responses</h2></div>
 <div class='subtext' style='margin-bottom:10px'>The tap-to-fill
 replies on the Messages page. Click a card to edit — changes are live
 for everyone immediately.</div>
 <div class='qrgrid'>{qr}</div>
 <details style='padding:10px 0'>
  <summary style='cursor:pointer;font-weight:700'>➕ Add a new response
  </summary>
  <form method='POST' action='/qr_save' style='margin-top:8px'>
   <input type='text' name='name' placeholder='Name (e.g. Holiday hours)'>
   <textarea name='text' rows='4' placeholder='The reply text…'
    style='margin-top:6px'></textarea>
   <button style='margin-top:6px'>Add response</button>
  </form></details></div>"""

    # ---- MY quick responses (Jessica, Jul 9: 'make each profile be
    # able to adjust their own quick responses') ----
    if user:
        mine = (_blob_rw("canned_replies_personal", {})).get(user, {})
        mqr = "".join(qrcard(n, t, mine=True) for n, t in mine.items())
        my_card = f"""
<div class='card'><h2 style='margin-top:0'>★ {esc(user)}'s quick
 responses</h2>
 <div class='subtext' style='margin-bottom:6px'>Only you see these in
 the dropdown (marked ★). The shared set above stays everyone's.</div>
 <div class='qrgrid'>{mqr}</div>
 {"" if mqr else "<div class='subtext'>None yet.</div>"}
 <details style='padding:10px 0'>
  <summary style='cursor:pointer;font-weight:700'>➕ Add one of my own
  </summary>
  <form method='POST' action='/qr_save' style='margin-top:8px'>
   <input type='hidden' name='mine' value='1'>
   <input type='text' name='name' placeholder='Name'>
   <textarea name='text' rows='4' placeholder='The reply text…'
    style='margin-top:6px'></textarea>
   <button style='margin-top:6px'>Add</button>
  </form></details></div>"""
    else:
        my_card = ("<div class='card'><h2 style='margin-top:0'>★ My quick "
                   "responses</h2><div class='subtext'>Pick your name in "
                   "the top bar to build your own set.</div></div>")
    qr_card += my_card

    # ---- quote line descriptions (gap noted Jul 9; office-editable
    # Jul 10) — the exact text under each service on PUSHED quotes ----
    sd = _blob_rw("service_descriptions", {})
    sdrows = ""
    for name, text in sorted(sd.items()):
        sdrows += f"""
<details style='border-bottom:1px solid var(--line);padding:8px 0'>
 <summary style='cursor:pointer;font-weight:700;color:var(--heading)'>
  {esc(name)}</summary>
 <form method='POST' action='/svcdesc_save' style='margin-top:8px'>
  <input type='hidden' name='name' value='{esc(name)}'>
  <textarea name='text' rows='4'>{esc(text)}</textarea>
  <div style='margin-top:6px'><button>Save</button></div>
 </form></details>"""
    sd_card = f"""
<div class='card'>
 <div class='schead'>{_svg_icon('queue')}<h2>Quote line descriptions</h2>
 </div>
 <div class='subtext' style='margin-bottom:6px'>The exact wording that
 appears under each service on quotes the system creates in Jobber.
 Edits apply to the NEXT quote pushed — customers see this text, so
 read it twice.</div>
 {sdrows or "<div class='subtext'>None loaded yet.</div>"}</div>"""
    qr_card += sd_card

    # ---- TIMED DISCOUNTS (Dallon + LaRee, Jul 13: '15% for the 2nd
    # week of August… flexible for slow seasons') — dated windows; the
    # engine applies the largest active one to every new bid as its own
    # labeled line. Mockup approved by Dallon before build. ----
    td = _blob_rw("timed_discounts", [])
    from datetime import date as _tdd
    _today = _tdd.today()
    td_rows = ""
    for i, t in enumerate(td):
        try:
            _s = _tdd.fromisoformat(t.get("start") or "")
            _e = _tdd.fromisoformat(t.get("end") or "")
        except ValueError:
            _s = _e = None
        if _s and _e and _s <= _today <= _e:
            st = ("<span style='background:#1c3a2c;color:#8fc7a6;"
                  "border-radius:999px;padding:1px 9px;font-size:11px;"
                  f"font-weight:800'>LIVE — ends {_e.strftime('%b %-d')}"
                  "</span>")
        elif _s and _s > _today:
            st = ("<span style='background:#1f3350;color:#79aede;"
                  "border-radius:999px;padding:1px 9px;font-size:11px;"
                  f"font-weight:800'>upcoming {_s.strftime('%b %-d')}"
                  "</span>")
        else:
            st = ("<span style='color:var(--mut);font-size:11px'>ended"
                  "</span>")
        td_rows += (
            f"<tr><td style='font-weight:700'>{esc(t.get('name') or '')}"
            f"</td><td>{esc(str(t.get('pct')))}%</td>"
            f"<td class='subtext'>{esc(t.get('start') or '')} → "
            f"{esc(t.get('end') or '')}</td><td>{st}</td>"
            f"<td><form method='POST' action='/timed_discount_del' "
            f"style='margin:0'><input type='hidden' name='idx' value='{i}'>"
            f"<button class='gray' style='padding:2px 9px;font-size:11px'>"
            f"✕</button></form></td></tr>")
    timed_card = f"""
<div class='card'>
 <div class='schead'>{_svg_icon('percent')}<h2>📅 Timed discounts</h2></div>
 <div class='subtext' style='margin-bottom:8px'>A discount runs only
 between its dates — every new bid in the window gets it automatically
 as its own labeled line (true prices stay visible). Only ONE applies
 per bid: the largest wins, they never stack. Fill slow weeks without
 permanent price cuts.</div>
 <table style='width:100%;font-size:13px'>{td_rows or
     "<tr><td class='subtext'>none yet — add one below</td></tr>"}</table>
 <form method='POST' action='/timed_discount_add' style='display:grid;
  grid-template-columns:1.4fr 62px 1fr 1fr auto;gap:7px;margin-top:10px;
  align-items:end'>
  <label style='font-size:11px'>Name<input name='name'
   placeholder='August slow-week special' required></label>
  <label style='font-size:11px'>%<input name='pct' type='number' min='1'
   max='60' value='15' required></label>
  <label style='font-size:11px'>Starts<input name='start' type='date'
   required></label>
  <label style='font-size:11px'>Ends<input name='end' type='date'
   required></label>
  <button>➕ Add</button>
 </form>
 <div class='subtext' style='margin-top:6px'>Saved under your name tag
 and logged like every pricing change.</div>
</div>"""

    # ---- discount policy (Jessica, Jul 9: 'discounts need to be in
    # the settings') — feeds the ✨ drafter's house rules + the Guide ----
    dp = _blob_rw("discount_policy", {})
    disc_card = f"""
<div class='card'>
 <div class='schead'>{_svg_icon('percent')}<h2>Discounts</h2></div>
 <div class='subtext' style='margin-bottom:8px'>What the ✨ draft-a-reply
 writer is allowed to tell customers, and the internal friends-&amp;-family
 rule. Plain sentences — written exactly as you'd say them.</div>
 <form method='POST' action='/discounts_save'>
  <label style='font-weight:700;font-size:13px'>Customer discount</label>
  <textarea name='customer' rows='2'>{esc(dp.get("customer",
      "15% off services booked in the second half of August or "
      "September"))}</textarea>
  <label style='font-weight:700;font-size:13px;display:block;
   margin-top:8px'>Friends &amp; family (internal — never told to
   customers)</label>
  <textarea name='fnf' rows='2'>{esc(dp.get("fnf",
      "50% — September, February or March, when the schedule is slow "
      "(Dallon, Jul 10)"))}</textarea>
  <label style='font-weight:700;font-size:13px;display:block;
   margin-top:8px'>Other discount rules (one per line — veterans,
   seniors, multi-property, whatever the office runs)</label>
  <textarea name='extra' rows='3' placeholder='e.g. Veterans / first
 responders — 10%&#10;Bi-annual service contract — 15%'>{esc(
      dp.get("extra", ""))}</textarea>
  <button style='margin-top:8px'>Save discounts</button>
 </form>{_discount_patterns_html()}</div>"""

    # ---- email signature (Dallon, Jul 13: 'making sure the signature
    # is right — might be worth adding the option to change it') ----
    import mailer as _ml
    sig = _blob_rw("email_signature", "") or _ml.DEFAULT_SIGNATURE
    sig_preview = sig.replace("{name}", user or "LaRee")
    sig_card = f"""
<div class='card'>
 <div class='schead'>{_svg_icon('chat')}<h2>Email signature</h2></div>
 <div class='subtext' style='margin-bottom:8px'>The block that ends
 every reply the office sends from the dashboard.
 <b>{{name}}</b> fills in automatically with whoever's signed in — so
 the same signature works for LaRee, Martha, and Jessica.</div>
 <form method='POST' action='/signature_save'>
  <textarea name='signature' rows='4' style='font-family:ui-monospace,
   Menlo,monospace;font-size:13px'>{esc(sig)}</textarea>
  <div style='margin-top:10px;background:rgba(17,41,33,.6);
   border:1px solid rgba(201,162,39,.16);border-radius:10px;
   padding:12px 14px'>
   <div style='font-size:10px;font-weight:800;letter-spacing:1.2px;
    text-transform:uppercase;color:var(--mut);margin-bottom:6px'>
    Preview (as {esc(user or "LaRee")})</div>
   <div style='white-space:pre-wrap;font-size:13.5px;color:var(--ink)'
    >{esc(sig_preview)}</div></div>
  <button style='margin-top:10px'>Save the shared signature</button>
  <span class='subtext' style='margin-left:10px'>Leave the
   <b>{{name}}</b> tag in so it stays personalized.</span>
 </form>{_my_signature_html(user)}</div>"""

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

    # Butler Health (Stitch Office Configuration port, Jul 10 pm) —
    # real counts only, no invented metrics
    try:
        import facts_edit
        n_facts = len([k for k in facts_edit._blob()[0]
                       if not k.startswith("_")])
    except Exception:
        n_facts = 0
    n_changes = len([r for r in load_reviews()
                     if r.get("action") == "settings_change"])
    health = f"""
<div class='healthcard'>
 <h3>Butler health</h3>
 <div style='display:flex;justify-content:space-between;font-size:13.5px'>
  <span style='color:rgba(255,255,255,.6)'>Pricing overrides active</span>
  <b>{len(ov)}</b></div>
 <div class='hbar'><i style='width:{min(100, len(ov) * 10)}%'></i></div>
 <div style='display:flex;justify-content:space-between;font-size:13.5px'>
  <span style='color:rgba(255,255,255,.6)'>Quick responses on file</span>
  <b>{len(canned)}</b></div>
 <div class='hbar'><i style='width:{min(100, len(canned) * 8)}%'></i></div>
 <div style='display:flex;justify-content:space-between;font-size:13.5px'>
  <span style='color:rgba(255,255,255,.6)'>House-fact corrections
  remembered</span><b>{n_facts}</b></div>
 <div class='hbar'><i style='width:{min(100, n_facts * 8)}%'></i></div>
 <div style='display:flex;justify-content:space-between;font-size:13.5px'>
  <span style='color:rgba(255,255,255,.6)'>Settings changes logged</span>
  <b>{n_changes}</b></div>
</div>"""

    header = f"""
<div class='pghead'>
 <div><h1>System Settings</h1>
  <div class='sub'>The office's control room — pricing rules, responses,
  and discounts. No code, no Dallon. Every change is logged with your
  name.</div></div>
 <div class='statchips'>
  <div class='statchip'><span class='l'>Overrides</span>
   <span class='v'>{len(ov)}</span></div>
  <div class='statchip gold'><span class='l'>Responses</span>
   <span class='v'>{len(canned)}</span></div>
 </div></div>"""

    return page("Settings", banner + header
                + f"<div class='bento'><div>{pricing_card}{qr_card}</div>"
                + f"<div>{sig_card}{timed_card}{disc_card}{health}{hist}"
                  f"</div></div>")


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

    # ☎ TODAY'S TEN (Jul 10 cycle; Stitch Re-engagement Command Center
    # layout ported Jul 10 pm): the ten best uncontacted calls as call
    # cards — avatar, lifetime, last service, one-tap outcome.
    def _initials(nm):
        parts = [w for w in re.split(r"[\s&]+", nm or "") if w[:1].isalpha()]
        return ("".join(w[0] for w in parts[:2]) or "•").upper()

    todays = [r for r in rows
              if r["name"] not in done and r.get("phone")][:10]
    today_card = ""
    if todays:
        crows = "".join(
            f"<div class='wbrow'><div class='avat'>"
            f"{esc(_initials(r['name']))}</div>"
            f"<div class='cols'><div style='min-width:0'>"
            f"<div style='font-weight:800;color:var(--ink);"
            f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>"
            f"{esc(r['name'])[:34]}</div>"
            f"<div style='font-size:13px;color:var(--mut);"
            f"font-variant-numeric:tabular-nums'>{esc(r['phone'])}</div>"
            f"</div>"
            f"<div><div class='klabel'>Lifetime value</div>"
            f"<div class='kval'>${r['lifetime']:,.0f}</div></div>"
            f"<div><div class='klabel'>Last service</div>"
            f"<div class='kval'>{esc((r.get('last') or '—')[:7])}</div></div>"
            f"<form method='POST' action='/winback_done' style='margin:0;"
            f"display:flex;gap:6px'>"
            f"<input type='hidden' name='name' value='{esc(r['name'])}'>"
            f"<button class='pill go' name='outcome' value='rebooked'>"
            f"Rebooked</button>"
            f"<button class='pill dim' name='outcome' value='no answer'>"
            f"No answer</button></form></div></div>"
            for r in todays)
        today_card = f"""
<div style='margin-bottom:18px'>
 <div class='schead'>{_svg_icon('phone')}<h2>Today's ten calls</h2>
  <span class='subtext' style='margin-left:auto'>highest lifetime value,
  phone on file, not yet contacted</span></div>
 <div class='subtext' style='margin-bottom:10px'>The whole script:
 “we miss you — want your usual {datetime.now():%B} cleaning?”</div>
 {crows}</div>"""

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
    n_rebooked = sum(1 for v in done.values()
                     if v.get("outcome") == "rebooked")
    won_back = sum(r["lifetime"] for r in rows
                   if (done.get(r["name"]) or {}).get("outcome")
                   == "rebooked")

    list_card = f"""
<div class='card'>
 <div class='schead'>{_svg_icon('people')}<h2>The full list</h2></div>
 <p style='font-size:14px;margin-top:0'>Loyal customers (2+ years,
 3+ jobs) we haven't seen in 20+ months. Sorted by value: start at
 the top.
 {f"<span class='subtext'>({dns_removed} do-not-service customers removed "
  f"from this list automatically.)</span>" if dns_removed else ""}</p>
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

    # ANNUAL RENEWALS (right rail — Stitch layout): loyal ACTIVE
    # customers inside their own yearly window right now. Reach them
    # BEFORE they drift into the win-back list.
    due = _blob_rw("due_soon", [])
    due_col = ""
    if due:
        cards = ""
        for i, r in enumerate(due[:9]):
            over = r["days_since"] - r["cadence_days"]
            when = (f"{over}d overdue" if over > 0 else
                    "due now" if over > -15 else f"in {-over}d")
            cards += f"""
<div class='renew{" hot" if i == 0 else ""}'>
 <div style='display:flex;justify-content:space-between;gap:10px;
  align-items:flex-start'>
  <div><div style='font-weight:800;color:var(--ink)'>
   {esc(r['name'])[:30]}</div>
   <div class='subtext' style='font-size:11.5px'>{r['years']} yr with
   Master Butler · {r['visits']} jobs</div></div>
  <span class='due'>{esc(when)}</span></div>
 <div style='display:flex;justify-content:space-between;margin-top:10px'>
  <div><div class='klabel' style='font-size:9px;font-weight:800;
   text-transform:uppercase;letter-spacing:1.2px;color:var(--mut)'>
   Last visit</div>
   <div style='font-weight:800;font-variant-numeric:tabular-nums'>
   {esc(r['last'])}</div></div>
  <div style='text-align:right'><div style='font-size:9px;
   font-weight:800;text-transform:uppercase;letter-spacing:1.2px;
   color:var(--mut)'>Lifetime</div>
   <div style='font-weight:900;color:#c9a227;
   font-variant-numeric:tabular-nums'>${r['lifetime']:,}</div></div>
 </div>
 <div style='margin-top:8px;background:var(--soft);border-radius:9px;
  padding:6px 10px;font-size:11.5px;color:var(--mut)'>their rhythm:
  every {r['cadence_days']}d · ${r['last_total']:,} last time</div>
</div>"""
        drows = "".join(
            f"<tr><td><b>{esc(r['name'])[:30]}</b></td>"
            f"<td class='num'>{r['visits']}</td>"
            f"<td>{esc(r['last'])}</td>"
            f"<td class='num'>{r['days_since']}d</td>"
            f"<td class='num'><b>${r['lifetime']:,}</b></td></tr>"
            for r in due[9:100])
        more = (f"<details style='margin-top:8px'><summary style='cursor:"
                f"pointer;font-weight:700;color:var(--mut);font-size:13px'>"
                f"the rest — all {min(len(due), 100)} due now "
                f"(${sum(r['lifetime'] for r in due):,} lifetime)</summary>"
                f"<div style='overflow-x:auto'>"
                f"<table style='margin-top:8px'><tr><th>Customer</th>"
                f"<th class='num'>Jobs</th><th>Last</th>"
                f"<th class='num'>Ago</th><th class='num'>Lifetime</th></tr>"
                f"{drows}</table></div></details>" if len(due) > 9 else "")
        due_col = f"""
<div style='margin-top:18px'>
<div class='schead'>{_svg_icon('repeat')}<h2>Annual renewals</h2>
 <span class='subtext' style='margin-left:auto'>{len(due)} inside their
 yearly window · ${sum(r['lifetime'] for r in due):,} lifetime</span>
</div>
<div class='subtext' style='margin-bottom:10px'>ACTIVE customers inside
 their own yearly window — book them BEFORE they drift onto the
 call-back list.</div>
<div class='renewgrid'>{cards}</div>{more}</div>"""

    header = f"""
<div class='pghead'>
 <div><h1>Win-back</h1>
  <div class='sub'>Re-engaging past customers — loyal clients from the
  Monroe service region who went quiet.</div></div>
 <div class='statchips'>
  <div class='statchip'><span class='l'>Left to call</span>
   <span class='v'>{remaining}</span></div>
  <div class='statchip gold'><span class='l'>Lifetime at stake</span>
   <span class='v'>${rep.get('lost_lifetime_value', 0):,}</span></div>
 </div></div>"""

    tiles = f"""
<div class='tiles'>
 <div class='tile'><div class='ticon'>{_svg_icon('check')}</div>
  <div><div class='tl'>Rebooked</div><div class='tv'>{n_rebooked}</div>
   <div class='ts'>${won_back:,} lifetime value won back</div></div></div>
 <div class='tile'><div class='ticon'>{_svg_icon('phone')}</div>
  <div><div class='tl'>Contacted</div><div class='tv'>{len(done)}</div>
   <div class='ts'>every outcome logged with a name</div></div></div>
 <div class='tile'><div class='ticon'>{_svg_icon('trend')}</div>
  <div><div class='tl'>Still waiting</div><div class='tv'>{remaining}</div>
   <div class='ts'>highest lifetime value first</div></div></div>
</div>"""

    # full-width stacked sections (Dallon, Jul 13: annual renewals were
    # crammed in a narrow column and running off — critical info gets
    # the whole width)
    body = (header + tiles + today_card + due_col + list_card)
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

    # THE DOLLAR TRACKER (Dallon, Jul 10: 'add the dollar tracker we
    # had before — that was awesome'): money at a glance, not just counts
    won_d = sum((r.get("office_total") or 0) for r in matched
                if (r.get("office_status") or "").lower()
                in ("approved", "converted"))
    out_d = sum((r.get("office_total") or 0) for r in matched
                if (r.get("office_status") or "").lower()
                == "awaiting_response")
    wait_d = sum((r.get("system_total") or 0) for r in waiting)
    hero = (
        "<div class='stats'>"
        f"<div class='stat'><b style='color:var(--green2)' class='tab'>"
        f"${won_d:,.0f}</b><span>WON 🎉</span></div>"
        f"<div class='stat'><b class='tab'>${out_d:,.0f}</b>"
        f"<span>quotes out</span></div>"
        f"<div class='stat'><b class='tab'>${wait_d:,.0f}</b>"
        f"<span>drafts awaiting office</span></div>"
        f"<div class='stat'><b>{len(matched)}</b><span>compared</span></div>"
        f"<div class='stat'><b>{close}</b><span>within 10%</span></div>"
        f"</div>")

    # ── 📆 JULY VS LAST JULY (Tom's notes via Dallon, Jul 14: 'direct
    # comparison to last year — first 14 days') — reads the yoy_july
    # blob mined from Jobber invoices; hidden until the blob exists ──
    yoy_card = ""
    try:
        _yy = (clouddb.get_blob("yoy_july") or {}) \
            if clouddb.available() else {}
        _yrs = _yy.get("years") or ["2026", "2025"]
        _t25 = (_yy.get("totals") or {}).get(_yrs[-1]) or {}
        _t26 = (_yy.get("totals") or {}).get(_yrs[0]) or {}
        _wl = _yy.get("window_label") or "July 1–14"
        if _t25.get("invoices") or _t26.get("invoices"):
            _s25 = (_yy.get("services") or {}).get(_yrs[-1]) or {}
            _s26 = (_yy.get("services") or {}).get(_yrs[0]) or {}
            _keys = sorted(set(_s25) | set(_s26),
                           key=lambda k: -((_s26.get(k) or {}).get(
                               "revenue", 0) + (_s25.get(k) or {}).get(
                               "revenue", 0)))
            _rows = ""
            for k in _keys[:9]:
                a = _s25.get(k) or {}
                b = _s26.get(k) or {}
                d_rev = (b.get("revenue", 0) or 0) - (a.get("revenue", 0)
                                                      or 0)
                _dc = ("var(--green2)" if d_rev >= 0 else "#f2b8b5")
                _rows += (
                    f"<tr style='border-top:1px solid var(--line)'>"
                    f"<td style='padding:5px 8px;overflow:hidden;"
                    f"text-overflow:ellipsis;white-space:nowrap'>"
                    f"{esc(k.title())}"
                    f"</td><td class='tab' style='text-align:right;"
                    f"padding:5px 8px'>"
                    f"{a.get('count', 0)}× ${a.get('revenue', 0):,}</td>"
                    f"<td class='tab' style='text-align:right;"
                    f"padding:5px 8px'>"
                    f"{b.get('count', 0)}× ${b.get('revenue', 0):,}</td>"
                    f"<td class='tab' style='text-align:right;"
                    f"padding:5px 8px;"
                    f"color:{_dc};font-weight:800'>{d_rev:+,}</td></tr>")
            _dt = (_t26.get("revenue", 0) or 0) - (_t25.get("revenue", 0)
                                                   or 0)
            # MONTH-TO-MONTH (Tom, Jul 15): each of the last 12 months
            # vs the same month a year earlier, as paired mini-bars
            _month_strip = ""
            _mm = _yy.get("monthly") or {}
            if _mm:
                _ms = sorted(_mm)[-12:]
                _vals = []
                _curmo = (_yy.get("mined_at") or "")[:7]
                for m in _ms:
                    _prev = f"{int(m[:4])-1}{m[4:]}"
                    if m == _curmo:
                        # in-progress month: use the DAY-MATCHED race
                        # totals, never partial-vs-full (that lied -99k)
                        _vals.append((m + "*",
                                      _t26.get("revenue", 0) or 0,
                                      _t25.get("revenue", 0) or 0))
                    else:
                        _vals.append((m,
                                      _mm.get(m, {}).get("revenue", 0),
                                      _mm.get(_prev, {})
                                      .get("revenue", 0)))
                _mmax = max([1] + [max(c, o) for _, c, o in _vals])
                _cols = ""
                for m, _cur, _old2 in _vals:
                    _up = _cur >= _old2
                    _cols += (
                        f"<div style='flex:1;display:flex;flex-direction:"
                        f"column;align-items:center;gap:2px' title='"
                        f"{m}: ${_cur:,} vs same month LY: ${_old2:,}'>"
                        f"<div style='font-size:9.5px;font-weight:800;"
                        f"color:{'var(--green2)' if _up else '#f2b8b5'}'>"
                        f"{(_cur - _old2) / 1000:+,.0f}k</div>"
                        f"<div style='display:flex;gap:2px;align-items:"
                        f"flex-end;height:56px'>"
                        f"<div style='width:9px;height:"
                        f"{max(3, _old2 / _mmax * 54):.0f}px;background:"
                        f"rgba(140,160,150,.5);border-radius:2px'></div>"
                        f"<div style='width:9px;height:"
                        f"{max(3, _cur / _mmax * 54):.0f}px;background:"
                        f"#c9a227;border-radius:2px'></div></div>"
                        f"<div style='font-size:9px;color:var(--mut)'>"
                        f"{m[5:]}/{m[2:4]}</div></div>")
                _month_strip = (
                    "<div style='margin-top:14px'><div class='subtext' "
                    "style='font-weight:800;font-size:11px;"
                    "text-transform:uppercase;letter-spacing:1px'>"
                    "Month vs same month last year "
                    "<span style='font-weight:400;text-transform:none'>"
                    "· grey = last year, gold = this year · hover for "
                    "dollars</span></div>"
                    f"<div style='display:flex;gap:6px;margin-top:6px'>"
                    f"{_cols}</div></div>")
            yoy_card = f"""
<div class='card' style='margin:14px 0'>
 <div class='schead'>{_svg_icon('trend')}<h2>{_wl}, this year vs
 last</h2><span class='subtext'>invoiced work, straight from Jobber
 (Tom's comparison)</span></div>
 <div class='stats'>
  <div class='stat'><b class='tab'>${_t25.get('revenue', 0):,}</b>
   <span>{_yrs[-1]} · {_t25.get('invoices', 0)} invoices</span></div>
  <div class='stat'><b class='tab'>${_t26.get('revenue', 0):,}</b>
   <span>{_yrs[0]} · {_t26.get('invoices', 0)} invoices</span></div>
  <div class='stat'><b class='tab' style='color:{"var(--green2)"
   if _dt >= 0 else "#f2b8b5"}'>{_dt:+,}</b><span>year over year</span>
  </div>
 </div>
 <div class='overx' style='overflow-x:auto'><table style='width:100%;
  max-width:620px;table-layout:fixed;border-collapse:collapse;
  font-size:12.5px;margin-top:8px'>
  <colgroup><col style='width:34%'><col style='width:24%'>
   <col style='width:24%'><col style='width:18%'></colgroup>
  <tr class='subtext' style='font-size:10.5px;text-transform:uppercase;
   letter-spacing:.8px'>
   <th style='text-align:left;padding:4px 8px'>Service</th>
   <th style='text-align:right;padding:4px 8px'>Jul {_yrs[-1]}</th>
   <th style='text-align:right;padding:4px 8px'>Jul {_yrs[0]}</th>
   <th style='text-align:right;padding:4px 8px'>Δ $</th></tr>
  {_rows}</table></div>
 {_month_strip}
</div>"""
    except Exception:
        yoy_card = ""
    # ── 🎯 THE GRADED WHEEL (Tom's ask via Dallon's Stitch mockup,
    # Jul 14): alignment gauge (share of compared drafts within ±10%
    # of the office), the biggest spreads as bars, awaiting-data count,
    # and a plain-English insight. One card, nothing else moved. ──
    _pct = (close / len(matched) * 100) if matched else 0
    _circ = 2 * 3.14159 * 52
    _dash = _circ * (1 - _pct / 100)
    _spread_rows = ""
    _worst = sorted((r for r in matched if r.get("gap_pct") is not None),
                    key=lambda r: -abs(r["gap_pct"]))[:3]
    for r in _worst:
        g = r["gap_pct"]
        _clr = "#8fc7a6" if abs(g) <= 10 else ("#e8c76a" if abs(g) <= 35
                                               else "#f2b8b5")
        _w = min(100, abs(g))
        _spread_rows += (
            f"<div style='margin:7px 0'><div style='display:flex;"
            f"justify-content:space-between;font-size:11px;"
            f"color:var(--mut);font-weight:700'><span>{cname(r)}</span>"
            f"<span style='color:{_clr}'>{g:+.0f}%</span></div>"
            f"<div style='height:7px;background:var(--soft);"
            f"border-radius:6px;overflow:hidden'><div style='height:100%;"
            f"width:{_w:.0f}%;background:{_clr}'></div></div></div>")
    _insight = ""
    if med_gap_precompute := sorted(abs(r["gap_pct"]) for r in matched
                                    if r.get("gap_pct") is not None):
        _mg = med_gap_precompute[len(med_gap_precompute) // 2]
        _insight = (f"Median distance from the office's number is "
                    f"<b>{_mg:.0f}%</b> across {len(matched)} compared "
                    f"quotes. The auto-review ledger names the exact "
                    f"Settings knob when a service drifts ±10%+.")
    wheel_card = f"""
<div class='card' style='margin:14px 0'>
 <div class='schead'>{_svg_icon('trend')}<h2>Alignment wheel</h2>
  <span class='subtext'>how often our draft lands within ±10% of the
  office's final</span></div>
 <div style='display:flex;gap:26px;align-items:center;flex-wrap:wrap'>
  <div style='flex:none;position:relative;width:130px;height:130px'>
   <svg width='130' height='130'>
    <circle cx='65' cy='65' r='52' fill='none' stroke='var(--soft)'
     stroke-width='11'/>
    <circle cx='65' cy='65' r='52' fill='none' stroke='#8fc7a6'
     stroke-width='11' stroke-linecap='round'
     stroke-dasharray='{_circ:.0f}' stroke-dashoffset='{_dash:.0f}'
     transform='rotate(-90 65 65)'/>
   </svg>
   <div style='position:absolute;inset:0;display:flex;flex-direction:
    column;align-items:center;justify-content:center'>
    <b class='tab' style='font-size:26px'>{_pct:.0f}%</b>
    <span class='subtext' style='font-size:9.5px'>within 10%</span>
   </div>
  </div>
  <div style='flex:1;min-width:220px'>
   <div class='subtext' style='font-weight:800;font-size:11px;
    text-transform:uppercase;letter-spacing:1px'>Biggest spreads —
    draft vs office</div>{_spread_rows or
    "<div class='subtext'>no compared quotes yet</div>"}
  </div>
  <div style='flex:none;text-align:center;padding:0 10px'>
   <b class='tab' style='font-size:30px;color:#e8c76a'>{len(waiting)}</b>
   <div class='subtext' style='font-size:11px'>drafts awaiting the<br>
   office's number</div>
  </div>
 </div>
 {f"<div class='subtext' style='margin-top:10px'>💡 {_insight}</div>"
  if _insight else ""}
</div>"""

    # ── ✨ AUTO-RESPOND PULSE (Dallon, Jul 15: "where are you seeing
    # this auto respond shadow… i would love to watch this on the
    # scoreboard") — the grading room's summary blob, refreshed hourly
    # by the poller and on every /autodrafts open ──
    pulse_card = ""
    try:
        _pu = (clouddb.get_blob("autorespond_pulse") or {}) \
            if clouddb.available() else {}
        if _pu:
            _plive = _pu.get("live") or []
            _prows = "".join(
                f"<div style='padding:7px 12px;border-top:1px solid "
                f"var(--line);font-size:13px'><b>{esc(p.get('name'))}"
                f"</b> <span class='subtext'>{esc(p.get('type'))}"
                f"{' · 📖 auto' if p.get('auto') else ''}</span>"
                + (f"<br><span style='color:var(--goldink)'>🗓 "
                   f"{esc(p['offer'])}</span>" if p.get("offer") else "")
                + "</div>" for p in _plive)
            _pn = _pu.get("n") or 0
            _ppct = (_pu.get("good", 0) / _pn * 100) if _pn else 0
            pulse_card = f"""
<div class='card' style='margin-bottom:14px'>
 <div class='schead'>✨<h2>Auto-respond shadow — the pulse</h2>
  <span class='subtext'>what the reply box would do right now · full
  grading room: <a href='/autodrafts'>open it</a> · as of
  {esc((_pu.get('at') or '')[11:16])} UTC</span></div>
 <div style='display:flex;gap:26px;flex-wrap:wrap;margin:4px 0 6px'>
  <div><b class='tab' style='font-size:24px'>{len(_plive)}</b>
   <div class='subtext'>would draft right now</div></div>
  <div><b class='tab' style='font-size:24px'>{_pu.get('gated', 0)}</b>
   <div class='subtext'>correctly left blank</div></div>
  <div><b class='tab' style='font-size:24px'>{_ppct:.0f}%</b>
   <div class='subtext'>{"live sends" if _pu.get('live_sends') else
   "graded drafts"} sent as written ({_pn} graded)</div></div>
 </div>
 {_prows or "<div class='subtext'>nothing awaiting a reply matches a "
  "safe template right now</div>"}
 <div class='subtext' style='margin-top:6px'>🗓 gold lines = stage-2
 shadow date offers — an offer is NOT a booking; it books on the
 customer's yes, first yes wins the slot.</div>
</div>"""
    except Exception:
        pulse_card = ""

    # ── 🔍 THE SELF-REVIEW (Dallon, Jul 15: "a shadow program to
    # periodically review all our work… tell us where we are failing
    # the most. Time, bids, language, etc.") — failure_review blob,
    # refreshed hourly; verdicts worst-first, receipts underneath ──
    fail_card = ""
    try:
        _fr = (clouddb.get_blob("failure_review") or {}) \
            if clouddb.available() else {}
        if _fr.get("verdicts"):
            _ft, _fb = _fr.get("time") or {}, _fr.get("bids") or {}
            _vrows = "".join(
                f"<div style='padding:8px 12px;border-top:1px solid "
                f"var(--line);font-size:13.5px'>{esc(v)}</div>"
                for v in _fr["verdicts"])
            _slow = ", ".join(
                f"{esc(x['who'])} ({x['hours']}h)" for x in
                (_ft.get("oldest_unanswered") or [])[:4])
            fail_card = f"""
<div class='card' style='margin-bottom:14px'>
 <div class='schead'>🔍<h2>Where we're failing — the self-review</h2>
  <span class='subtext'>the system grading OUR work, worst first ·
  re-graded hourly · as of {esc((_fr.get('at') or '')[11:16])} UTC
  </span></div>
 <div style='display:flex;gap:26px;flex-wrap:wrap;margin:4px 0 6px'>
  <div><b class='tab' style='font-size:24px'>
   {_ft.get('median_h', '–')}h</b>
   <div class='subtext'>median time to first reply (30d)</div></div>
  <div><b class='tab' style='font-size:24px'>
   {_ft.get('within_24h', '–')}%</b>
   <div class='subtext'>answered within a day</div></div>
  <div><b class='tab' style='font-size:24px'>
   {_ft.get('unanswered', '–')}</b>
   <div class='subtext'>never answered (30d)</div></div>
  <div><b class='tab' style='font-size:24px'>
   {_fb.get('within_10pct', '–')}%</b>
   <div class='subtext'>bids within ±10% of the office</div></div>
 </div>
 {_vrows}
 {f"<div class='subtext' style='margin-top:6px'>oldest with no reply: "
  f"{_slow}</div>" if _slow else ""}
</div>"""
    except Exception:
        fail_card = ""

    # ── 🗺 THE MINED REPORTS (Dallon, Jul 15: "add the other things
    # like the pace, area days, lights etc") — pace, area-days, lights
    # seasons, fronts, all from the nightly mining blobs; each card
    # hides itself until its data exists ──
    mined_cards = ""
    try:
        _sk = (clouddb.get_blob("sched_knowledge") or {}) \
            if clouddb.available() else {}
        _mo = list((_sk.get("jobs_per_day_by_month") or {}).items())
        if _mo:
            _mx = max(v["avg_per_day"] for _, v in _mo) or 1
            def _py(val):
                return 100 - (val / _mx) * 70
            _pts = " ".join(
                f"{48 + i*712/(len(_mo)-1):.0f},{_py(v['avg_per_day']):.0f}"
                for i, (_, v) in enumerate(_mo))
            # Y-AXIS + VALUE LABELS (Dallon, Jul 15: 'no numbers on the
            # line — we can't know what those numbers mean')
            _step = 5 if _mx > 8 else 2
            _grid = "".join(
                f"<line x1='48' y1='{_py(g):.0f}' x2='766' "
                f"y2='{_py(g):.0f}' stroke='var(--line)' "
                f"stroke-dasharray='3,4' stroke-width='.7'/>"
                f"<text x='42' y='{_py(g)+3:.0f}' font-size='9' "
                f"fill='var(--mut)' text-anchor='end'>{g}</text>"
                for g in range(_step, int(_mx) + 1, _step))
            # what the axis MEASURES (Dallon, Jul 15)
            _grid += ("<text x='11' y='65' font-size='9.5' "
                      "font-weight='800' fill='var(--mut)' "
                      "transform='rotate(-90 11 65)' "
                      "text-anchor='middle'>AVG JOBS / DAY</text>")
            # number every yearly PEAK and the newest month
            _marks = ""
            _peak_idx = {max(range(len(_mo)),
                             key=lambda i: (_mo[i][0].startswith(y)
                                            and _mo[i][1]['avg_per_day']
                                            or -1))
                         for y in ("2023", "2024", "2025", "2026")}
            _peak_idx.add(len(_mo) - 1)
            for i in sorted(_peak_idx):
                k2, v2 = _mo[i]
                _x = 48 + i * 712 / (len(_mo) - 1)
                _marks += (
                    f"<circle cx='{_x:.0f}' cy='{_py(v2['avg_per_day']):.0f}'"
                    f" r='3.5' fill='#c9a227'/>"
                    f"<text x='{_x:.0f}' y='{_py(v2['avg_per_day'])-7:.0f}'"
                    f" font-size='10' font-weight='800' fill='#c9a227' "
                    f"text-anchor='middle'>{v2['avg_per_day']}</text>")
            _lbl = "".join(
                f"<text x='{48 + i*712/(len(_mo)-1):.0f}' y='114' "
                f"font-size='8' fill='var(--mut)' text-anchor='middle'>"
                f"{k[2:7]}</text>"
                for i, (k, v) in enumerate(_mo)
                if k.endswith(("-01", "-07")))
            _lbl = _grid + _lbl + _marks
            _g = _sk.get("drive_gaps") or {}
            mined_cards += f"""
<div class='card' style='margin:14px 0'>
 <div class='schead'>{_svg_icon('trend')}<h2>The company's pace</h2>
 <span class='subtext'>jobs/day, {len(_mo)} months · from the nightly
 route mine</span></div>
 <svg viewBox='0 0 780 132' style='width:100%;height:auto'>
  <polyline points='{_pts}' fill='none' stroke='#8fc7a6'
   stroke-width='2.5' stroke-linejoin='round'/>{_lbl}
  <text x='407' y='129' font-size='9.5' font-weight='800'
   fill='var(--mut)' text-anchor='middle'>MONTH · JAN 2023 → TODAY
   </text></svg>
 <div class='stats'>
  <div class='stat'><b class='tab'>{_g.get('median_min','?')} min</b>
   <span>median drive between stops</span></div>
  <div class='stat'><b class='tab'>{(_g.get('share_over_20min') or 0)*100:.0f}%</b>
   <span>hops over the 20-min rule</span></div>
  <div class='stat'><b class='tab'>{_mo[-1][1]['avg_per_day']}</b>
   <span>jobs/day this month</span></div>
  <div class='stat'><b class='tab'>{len(_sk.get('future_anchors') or {})}</b>
   <span>future days anchor-mapped</span></div>
 </div></div>"""
        _wd = _sk.get("weekday_city") or {}
        if _wd:
            _cc = {"Sammamish": "#c9a227", "Redmond": "#8fc7a6",
                   "Issaquah": "#79aede", "Bellevue": "#b08ed9",
                   "Monroe": "#e0a068", "Kirkland": "#7fc9c0",
                   "Woodinville": "#d9a3b2"}
            _rows = ""
            for _d2 in ("Mon", "Tue", "Wed", "Thu", "Fri"):
                _cs = _wd.get(_d2) or {}
                _t2 = sum(_cs.values()) or 1
                _seg = "".join(
                    f"<div title='{esc(c)}: {n}' style='width:"
                    f"{n/_t2*100:.1f}%;background:"
                    f"{_cc.get(c, '#9aa8a0')}'></div>"
                    for c, n in _cs.items())
                _top = max(_cs, key=_cs.get) if _cs else ""
                _rows += (f"<div style='display:flex;align-items:center;"
                          f"gap:10px;margin:6px 0'><div style='width:38px;"
                          f"font-weight:800;font-size:12px'>{_d2}</div>"
                          f"<div style='flex:1;display:flex;height:15px;"
                          f"border-radius:8px;overflow:hidden'>{_seg}"
                          f"</div><div style='width:90px;font-size:11px;"
                          f"color:var(--mut)'>{esc(_top)}</div></div>")
            _leg = " ".join(
                f"<span style='font-size:13.5px;font-weight:700;"
                f"margin-right:12px;white-space:nowrap'>"
                f"<span style='display:inline-block;width:13px;"
                f"height:13px;border-radius:3px;background:{v};"
                f"margin-right:5px;vertical-align:-1px'></span>{k}</span>"
                for k, v in _cc.items())
            mined_cards += (
                f"<div class='card' style='margin:14px 0'>"
                f"<div class='schead'>📍<h2>Area days</h2>"
                f"<span class='subtext'>where each weekday actually "
                f"goes — 3 years measured</span></div>{_rows}"
                f"<div style='margin-top:6px'>{_leg}</div></div>")
        _li = (_sk.get("lights") or {})
        _lim = _li.get("by_month_install_takedown") or {}
        if _lim:
            _keys = sorted(_lim)
            _mx3 = max(v[0] for v in _lim.values()) or 1
            _bw = 750 / len(_keys)
            _bars = "".join(
                f"<rect x='{15+i*_bw:.1f}' "
                f"y='{125-(_lim[k][0]/_mx3)*100:.1f}' "
                f"width='{_bw*0.75:.1f}' "
                f"height='{(_lim[k][0]/_mx3)*100:.1f}' rx='1.5' "
                f"fill='{'#c9a227' if _lim[k][0] > 400 else '#8fc7a6' if _lim[k][0] > 50 else 'rgba(140,160,150,.45)'}'/>"
                + (f"<text x='{15+i*_bw:.0f}' y='138' font-size='8' "
                   f"fill='var(--mut)'>{k[2:7]}</text>"
                   if k.endswith(("-01", "-07")) else "")
                for i, k in enumerate(_keys))
            _rc = _li.get("route_continuity") or {}
            _kept = _rc.get("kept_same_tech") or 0
            _chg = _rc.get("tech_changed") or 0
            _pk2 = _kept / max(_kept + _chg, 1) * 100
            mined_cards += f"""
<div class='card' style='margin:14px 0'>
 <div class='schead'>🎄<h2>Lights — three seasons, one shape</h2>
 <span class='subtext'>installs by month · Sept ramp → Oct peak → Nov
 hold</span></div>
 <svg viewBox='0 0 780 142' style='width:100%;height:auto'>{_bars}</svg>
 <div class='stats'>
  <div class='stat'><b class='tab'>{_li.get('avg_lights_jobs_per_lights_day','?')}</b>
   <span>lights jobs per lights day</span></div>
  <div class='stat'><b class='tab' style='color:#e8c76a'>{_pk2:.0f}%</b>
   <span>kept same installer yr→yr</span></div>
  <div class='stat'><b class='tab'>{(_li.get('title_codes_seen') or {}).get('ccc (return customer)','?')}</b>
   <span>ccc return customers</span></div>
  <div class='stat'><b class='tab'>{(_li.get('title_codes_seen') or {}).get('LLL (lights in our shop)','?')}</b>
   <span>LLL — lights in our shop</span></div>
 </div>
 <div class='subtext'>Route continuity is the finding: keeping the same
 tech on the same route is the speed play — {_chg:,} customers changed
 hands across the seasons.</div></div>"""
        _lp2 = (clouddb.get_blob("lights_pricing") or {}) \
            if clouddb.available() else {}
        _ff = _lp2.get("front_footage_v1") or {}
        if _ff.get("buckets"):
            _bk = _ff["buckets"]
            _mx4 = max(_bk.values()) or 1
            _fb = "".join(
                f"<div style='flex:1;display:flex;flex-direction:column;"
                f"align-items:center'><div style='width:70%;height:"
                f"{max(4, n/_mx4*70):.0f}px;background:#8fc7a6;"
                f"border-radius:3px 3px 0 0;margin-top:auto'></div>"
                f"<span style='font-size:9px;color:var(--mut)'>{b}"
                f"{'+' if int(b) >= 300 else ''}</span></div>"
                for b, n in _bk.items())
            mined_cards += (
                f"<div class='card' style='margin:14px 0'>"
                f"<div class='schead'>📏<h2>Lights front footage — "
                f"{_ff.get('n')} homes measured</h2>"
                f"<span class='subtext'>median "
                f"{_ff.get('median_ft')} ft · satellite eaves + peaks "
                f"+ side wrap (v1 estimate)</span></div>"
                f"<div style='display:flex;align-items:flex-end;gap:2px;"
                f"height:92px'>{_fb}</div></div>")
    except Exception:
        mined_cards = mined_cards or ""

    # ── 📚 WHAT THE SYSTEM IS LEARNING (Dallon, Jul 12: 'seeing the
    # reports… where things land, money we are losing') — built hourly
    # by learning_report.py ──
    # ── extra glances mined from what we already log (Dallon, Jul 12:
    # 'what other reports can you create — we've created tons') ──
    # 1) engine accuracy: median |gap| vs the office
    _gaps = sorted(abs(r["gap_pct"]) for r in matched
                   if r.get("gap_pct") is not None)
    med_gap = _gaps[len(_gaps) // 2] if _gaps else None
    # 2) the office's week, from the review log
    _wk = ""
    try:
        from datetime import timedelta as _td7
        _cut = (datetime.now() - _td7(days=7)).isoformat()
        _recent = [r for r in load_reviews() if (r.get("at") or "") >= _cut]
        _bywho = {}
        for r in _recent:
            b = (r.get("by") or "").strip()
            if b and not b.startswith("auto"):
                _bywho[b] = _bywho.get(b, 0) + 1
        _acts = {}
        _nice = {"approved": "approvals", "approve": "approvals",
                 "mark_done": "cleared", "lane_move": "filed",
                 "learned_spam": "spam taught", "fact_edit": "facts fixed",
                 "price_edit": "price edits", "flag_review": "flags",
                 "settings_change": "settings"}
        for r in _recent:
            a = _nice.get(r.get("action"))
            if a:
                _acts[a] = _acts.get(a, 0) + 1
        _who = " · ".join(f"<b>{esc(k)}</b> {v}" for k, v in
                          sorted(_bywho.items(), key=lambda kv: -kv[1])[:5])
        _what = " · ".join(f"{v} {k}" for k, v in
                           sorted(_acts.items(), key=lambda kv: -kv[1])[:5])
        if _who or _what:
            _wk = (f"<div style='margin-top:12px;padding:10px 14px;"
                   f"background:rgba(17,41,33,.5);border:1px solid "
                   f"rgba(201,162,39,.14);border-radius:12px;"
                   f"font-size:12.5px'><span style='font-size:10px;"
                   f"font-weight:800;letter-spacing:1.3px;"
                   f"text-transform:uppercase;color:var(--mut)'>This "
                   f"week in the office</span><br>"
                   + (f"{_who}" if _who else "")
                   + (f"<span class='subtext'> — {_what}</span>"
                      if _what else "") + "</div>")
    except Exception:
        pass
    # 3) request volume, last 14 days, as mini bars
    _vol = ""
    try:
        from datetime import timedelta as _tdv
        _days = [(datetime.now() - _tdv(days=i)).strftime("%Y%m%d")
                 for i in range(13, -1, -1)]
        _cnt = {d: 0 for d in _days}
        for b in load_bids():
            d = b["stamp"][:8]
            if d in _cnt and b.get("kind") in ("new_request",
                                               "phone_lead"):
                _cnt[d] += 1
        _mx = max(_cnt.values()) or 1
        _bars = "".join(
            f"<div title='{d[4:6]}/{d[6:8]}: {n}' style='width:9px;"
            f"height:{max(3, n / _mx * 30):.0f}px;background:"
            f"{'#c9a227' if i >= 12 else 'rgba(201,162,39,.45)'};"
            f"border-radius:2px'></div>"
            for i, (d, n) in enumerate(_cnt.items()))
        _vol = (f"<div style='flex:none;text-align:center'>"
                f"<div style='display:flex;gap:3px;align-items:flex-end;"
                f"height:34px'>{_bars}</div>"
                f"<div class='subtext' style='font-size:10px'>new "
                f"requests · 14 days</div></div>")
    except Exception:
        pass

    lr = (clouddb.get_blob("learning_report") or {}) \
        if clouddb.available() else {}
    learn_card = ""
    if lr:
        mo = lr.get("money") or {}
        pd = lr.get("pastdue") or {}
        le = lr.get("learning") or {}
        so = lr.get("sorting") or {}
        hist = lr.get("history") or []
        wr = mo.get("win_rate") or 0
        won_v = mo.get("won_val") or 0
        wait_v = mo.get("awaiting_val") or 0
        stale_v = mo.get("stale_val") or 0
        fresh_v = max(0, wait_v - stale_v)

        # the dollar FUNNEL: one stacked strip — won, waiting-fresh,
        # gone-stale — of the last {n} quotes, dollar-weighted
        pool = max(1, won_v + wait_v)
        seg = lambda v: max(2.5, v / pool * 100)
        funnel = f"""
<div style='margin-top:14px'>
 <div style='display:flex;height:34px;border-radius:10px;overflow:hidden;
  border:1px solid rgba(201,162,39,.2)'>
  <div style='width:{seg(won_v):.1f}%;background:#1d5c40'></div>
  <div style='width:{seg(fresh_v):.1f}%;background:#8a6b14'></div>
  <div style='width:{seg(stale_v):.1f}%;background:#7c2d24'></div>
 </div>
 <div style='display:flex;gap:18px;flex-wrap:wrap;margin-top:7px;
  font-size:12.5px'>
  <span><i style='display:inline-block;width:10px;height:10px;
   border-radius:3px;background:#1d5c40'></i> <b class='tab'
   style='color:var(--green2)'>${won_v:,}</b> won 🎉</span>
  <span><i style='display:inline-block;width:10px;height:10px;
   border-radius:3px;background:#8a6b14'></i> <b class='tab'
   style='color:var(--goldink)'>${fresh_v:,}</b> waiting
   (fresh)</span>
  <span><i style='display:inline-block;width:10px;height:10px;
   border-radius:3px;background:#7c2d24'></i> <b class='tab'
   style='color:var(--alarm)'>${stale_v:,}</b> quiet 7+ days —
   the 🚫 Nudge lane</span>
 </div></div>"""

        # sparkline: money-waiting day by day (grows as history does)
        spark = ""
        pts = [(h.get("awaiting") or 0) for h in hist]
        if len(pts) >= 2:
            top = max(pts) or 1
            W, H = 150, 34
            xy = " ".join(
                f"{i * (W / (len(pts) - 1)):.0f},"
                f"{H - (p / top) * (H - 4) - 2:.0f}"
                for i, p in enumerate(pts))
            spark = (f"<svg width='{W}' height='{H}' "
                     f"style='display:block'><polyline points='{xy}' "
                     f"fill='none' stroke='#c9a227' stroke-width='2' "
                     f"stroke-linejoin='round'/></svg>"
                     f"<div class='subtext' style='font-size:10px'>"
                     f"$ waiting, last {len(pts)} days</div>")

        # win-rate RING (pure CSS conic gradient)
        ring = f"""
<div style='flex:none;text-align:center'>
 <div style='width:104px;height:104px;border-radius:50%;margin:0 auto;
  background:conic-gradient(#5fbd85 0 {wr}%,rgba(255,255,255,.07) 0);
  display:flex;align-items:center;justify-content:center'>
  <div style='width:78px;height:78px;border-radius:50%;
   background:#0d231b;display:flex;flex-direction:column;
   align-items:center;justify-content:center'>
   <b style='font-size:24px;color:var(--green2)' class='tab'>{wr}%</b>
   <span style='font-size:8.5px;font-weight:800;letter-spacing:1.2px;
    color:var(--mut);text-transform:uppercase'>win rate</span>
  </div></div>
 <div class='subtext' style='font-size:10.5px;margin-top:5px'>
  {mo.get('won_n', 0)} won of {mo.get('quotes', 0)} quotes</div>
</div>"""

        props = "".join(
            f"<div style='background:var(--goldbg);border-left:3px solid "
            f"var(--gold);border-radius:9px;padding:9px 13px;margin-top:8px;"
            f"font-size:13px'><b>💡 Proposed rule:</b> {esc(p['text'])}"
            f"</div>" for p in (so.get("proposals") or []))
        moves = "".join(
            f"<div class='subtext' style='padding:2px 0'>"
            f"{esc(m.get('at', ''))} · {esc(m.get('by') or 'office')} filed "
            f"<b>{esc(m.get('key', ''))}</b> under {esc(m.get('to', ''))}"
            f"</div>" for m in reversed(so.get("last") or []))
        taught = "".join(
            f"<div class='tile' style='padding:12px 14px'>"
            f"<div><div class='tl'>{lbl}</div>"
            f"<div class='tv' style='font-size:19px'>{val}</div>"
            f"<div class='ts'>{sub}</div></div></div>"
            for lbl, val, sub in (
                ("Past due", f"${pd.get('val', 0):,}",
                 f"{pd.get('n', 0)} invoices waiting on payment"),
                ("House facts corrected",
                 le.get("fact_corrections", 0),
                 "remembered per address, forever"),
                ("Spam senders taught", le.get("spam_senders", 0),
                 "one office click each"),
                ("Floors raised", le.get("floors_raised", 0),
                 "never quote under last paid"),
                ("Lane moves by hand", so.get("moves_14d", 0),
                 "the sorter's report card · 2 weeks")))
        learn_card = f"""
<div class='card'>
 <div class='schead'>{_svg_icon('trend')}<h2>What the system is
 learning</h2><span class='subtext' style='margin-left:auto'>updated
 {esc((lr.get('at') or '')[:16].replace('T', ' '))} · hourly</span></div>
 <div style='display:flex;gap:22px;align-items:center;flex-wrap:wrap'>
  {ring}
  <div style='flex:1;min-width:260px'>{funnel}
   {f"<div class='subtext' style='margin-top:6px'>🎯 the engine lands a median <b style='color:var(--green2)'>{med_gap:.0f}%</b> from the office's own price</div>" if med_gap is not None else ""}
  </div>
  {_vol}
  {f"<div style='flex:none'>{spark}</div>" if spark else ""}
 </div>
 <div class='tiles five' style='margin-top:14px'>{taught}</div>
 {_wk}
 {props}
 {f"<details style='margin-top:10px'><summary style='cursor:pointer;font-weight:700;color:var(--mut);font-size:13px'>recent hand-filings</summary>{moves}</details>" if moves else ""}
</div>"""

    # ── NIGHTLY REPORT SHELF: chips swap one compact card; everything
    # pre-baked at 3 AM, page render just reads the blob ──
    shelf = (clouddb.get_blob("report_shelf") or {}) \
        if clouddb.available() else {}
    shelf_html = ""
    scards = shelf.get("cards") or []
    if scards:
        rchips = "".join(
            f"<button type='button' class='rchip{' on' if i == 0 else ''}'"
            f" data-r='{esc(c['id'])}' onclick='rShow(this.dataset.r)'>"
            f"{esc(c['title'])}</button>"
            for i, c in enumerate(scards))
        rcards = ""
        for i, c in enumerate(scards):
            lines = "".join(f"<div>· {esc(l)}</div>"
                            for l in (c.get("lines") or [])[:4])
            bars = ""
            if c.get("bars"):
                vals = [v for _l, v in c["bars"]]
                mx = max(vals) or 1
                bars = ("<div class='rbars'>" + "".join(
                    f"<i title='{esc(str(l))}: ${v:,.0f}' "
                    f"style='height:{max(3, v / mx * 44):.0f}px'></i>"
                    for l, v in c["bars"]) + "</div>")
            rcards += (
                f"<div class='rcard{' on' if i == 0 else ''}' "
                f"id='rc-{esc(c['id'])}'>"
                f"<div><div class='rhead tab'>{esc(str(c.get('head', '')))}"
                f"</div><div class='rsub'>{esc(c.get('sub', ''))}</div></div>"
                + (f"<div class='rlines'>{lines}</div>" if lines else "")
                + bars + "</div>")
        gen_at = (shelf.get("at") or "")[:10]
        shelf_html = (
            f"<div class='schead' style='margin-top:18px'>"
            f"{_svg_icon('chart')}<h2>Reports</h2>"
            f"<span class='subtext' style='margin-left:auto'>baked "
            f"nightly at 3 AM · {esc(gen_at)}</span></div>"
            f"<div class='rchips'>{rchips}</div>{rcards}"
            """<script>
function rShow(id){
  document.querySelectorAll('.rchip').forEach(function(c){
    c.classList.toggle('on', c.dataset.r === id);});
  document.querySelectorAll('.rcard').forEach(function(c){
    c.classList.toggle('on', c.id === 'rc-' + id);});
}
</script>""")

    auto = None
    if clouddb.available():
        auto = clouddb.get_blob("auto_reviews") or {}
    else:
        ap = BASE / "data" / "auto_reviews.json"
        auto = json.loads(ap.read_text()) if ap.exists() else {}
    # SLIM one-liners (Dallon, Jul 12: 'doesn't need to be that big') —
    # name · $ours → $office · gap pill · Jobber link. First 12 visible,
    # the rest fold.
    def _crow(r):
        gap = r.get("gap_pct")
        ar = auto.get(r.get("stamp"))
        tip = f" title=\"{esc(ar['summary'])}\"" if ar else ""
        if gap is None:
            gap_html = "<span class='subtext'>—</span>"
        else:
            ok = abs(gap) <= 10
            gap_html = (
                f"<span{tip} style='display:inline-block;min-width:52px;"
                f"text-align:center;border-radius:999px;padding:2px 9px;"
                f"font-size:11.5px;font-weight:800;"
                f"background:{'#173525' if ok else '#3a1713'};"
                f"color:{'#7fd6a2' if ok else '#f1998e'}'>"
                f"{gap:+.0f}%{'📖' if ar else ''}</span>")
        js = (r.get("office_status") or "").lower()
        jw, jc_ = {"approved": ("WON", "var(--green2)"),
                   "converted": ("WON", "var(--green2)"),
                   "awaiting_response": ("sent", "var(--mut)"),
                   "draft": ("draft", "var(--goldink)"),
                   "archived": ("arch", "var(--mut)")}.get(
                       js, ("—", "var(--mut)"))
        qbtn = (f"<a style='color:var(--green2);font-size:11.5px;"
                f"font-weight:700;text-decoration:none;flex:none' "
                f"href='{esc(r['jobber_url'])}' target='_blank' "
                f"rel='noopener'>#{r['office_quote']}↗</a>"
                if r.get("jobber_url") else "")
        _lnk = f"/bid/{esc(r.get('stamp') or '')}"
        return (
            f"<div style='display:flex;align-items:center;gap:10px;"
            f"padding:7px 10px;border-bottom:1px dashed "
            f"rgba(201,162,39,.1)'>"
            f"<a href='{_lnk}' style='color:var(--ink);font-weight:700;"
            f"font-size:13.5px;white-space:nowrap;overflow:hidden;"
            f"text-overflow:ellipsis;flex:1;min-width:0'>{cname(r)}</a>"
            f"<span style='font-size:11px;font-weight:800;color:{jc_};"
            f"flex:none'>{jw}</span>"
            f"<span class='tab' style='font-size:12.5px;color:var(--mut);"
            f"flex:none'>${r['system_total']:,.0f} → "
            f"<b style='color:var(--ink)'>${r['office_total']:,.0f}</b>"
            f"</span>{gap_html}{qbtn}</div>")

    # COMPACT (Dallon, Jul 13: 'make the full list compact') — the
    # whole compared list collapses; the summary carries the headline
    # (how many landed within 10%), so the scoreboard stays short and
    # the full list is one tap away.
    within = sum(1 for r in matched
                 if r.get("gap_pct") is not None and abs(r["gap_pct"]) <= 10)
    all_rows = "".join(_crow(r) for r in matched)
    matched_card = (
        f"<details class='card' style='margin-top:16px;padding:14px 18px'>"
        f"<summary style='cursor:pointer;list-style:none;display:flex;"
        f"align-items:center;gap:10px'>{_svg_icon('chart')}"
        f"<b style='font-size:15px;color:var(--goldink)'>Compared with "
        f"the office</b><span class='subtext' style='margin-left:auto'>"
        f"{within}/{len(matched)} within 10% · tap to open</span>"
        f"</summary><div style='margin-top:8px'>{all_rows}</div>"
        f"</details>" if matched else "")

    # WAITING = a cloud of name+price chips, not rows (Dallon, Jul 12:
    # 'still looks blocky') — services on hover, click opens the card
    wrows = "".join(
        f"<a class='wchip' href='/bid/{esc(r.get('stamp') or '')}' "
        f"title=\"{esc(' · '.join(svc_label(s) for s in (r.get('services') or [])[:4]))}\">"
        f"{cname(r)}<b>${r['system_total']:,.0f}</b></a>"
        for r in waiting)
    waiting_card = (
        f"<div style='margin-top:16px'><div class='schead'>"
        f"{_svg_icon('queue')}<h2>Waiting for an office quote</h2>"
        f"<span class='subtext' style='margin-left:auto'>tap a name to "
        f"open them · they move up on their own once quoted</span>"
        f"</div><div class='wchips'>{wrows}</div></div>"
        if wrows else "")

    # QUOTES GONE QUIET (Jul 10 cycle): sent, no reply, 5+ days —
    # follow-up is free money; the office nudges from here
    nrows = ""
    for r in matched:
        if (r.get("office_status") or "").lower() != "awaiting_response":
            continue
        try:
            age = (datetime.now()
                   - datetime.strptime(r["stamp"][:8], "%Y%m%d")).days
        except (KeyError, ValueError):
            continue
        if age < 5:
            continue
        qb = (f"<a class='btn' style='padding:4px 10px;font-size:11.5px;"
              f"background:var(--card);color:var(--green2);border:1px "
              f"solid var(--line)' href='{esc(r['jobber_url'])}' "
              f"target='_blank' rel='noopener'>#{r['office_quote']} ↗</a>"
              if r.get("jobber_url") else f"#{r['office_quote']}")
        nrows += (f"<tr><td><b>{cname(r)}</b></td>"
                  f"<td class='num'>${r['office_total']:,.0f}</td>"
                  f"<td class='num'>{age}d</td><td>{qb}</td></tr>")
    nudge_card = (
        "<div class='card' style='border-left:4px solid var(--gold)'>"
        "<h2 style='margin-top:0'>📤 Quotes gone quiet — worth a nudge"
        "</h2><div class='subtext' style='margin-bottom:8px'>Sent, no "
        "customer response, 5+ days since the request. A friendly "
        "follow-up closes a surprising number of these.</div>"
        "<table><tr><th>Customer</th><th class='num'>Quoted</th>"
        "<th class='num'>Waiting</th><th></th></tr>" + nrows
        + "</table></div>" if nrows else "")

    # auto-refresh through the day so approvals show up without a manual
    # reload (LaRee, Jul 10). Scroll-preserving, every 2 min; the board
    # itself is regenerated server-side on the hourly Jobber sync.
    gen = (sb.get("generated") or "")[:16].replace("T", " ")
    fresh = (f"<div class='subtext' style='margin:-2px 0 12px'>Updated "
             f"{esc(gen)} · refreshes automatically</div>") if gen else ""
    refresh_js = """<script>
(function(){var K='sb_scroll';try{var y=sessionStorage.getItem(K);
 if(y!==null){sessionStorage.removeItem(K);window.scrollTo(0,+y);}}catch(e){}
 setTimeout(function(){try{sessionStorage.setItem(K,window.scrollY);}catch(e){}
  location.reload();},120000);})();
</script>"""
    return page("Scoreboard", hero + wheel_card + pulse_card + fail_card
                + yoy_card + mined_cards + learn_card + shelf_html
                + fresh + nudge_card + matched_card + waiting_card
                + refresh_js)


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
        f"<b style='font-size:15px;color:var(--heading)'>{esc(v)}</b>"
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
        # LaRee's idea (Jul 9, closed Jul 10): EVERY visit, not just the
        # last — the chips wrap, nothing hides
        older = "".join(
            f"<span class='chip' style='font-variant-numeric:tabular-nums'>"
            f"{d[:7]} · <b>${pr:,.0f}</b></span>" for d, pr in visits[1:])
        rows += (
            f"<div style='display:flex;align-items:center;gap:14px;"
            f"padding:10px 2px;border-bottom:1px solid var(--line)'>"
            f"<div style='min-width:120px'><b style='color:var(--heading);"
            f"text-transform:capitalize'>{esc(svc)}</b></div>"
            f"<div style='min-width:120px'><b style='font-size:17px;"
            f"font-variant-numeric:tabular-nums'>${last_p:,.0f}</b>"
            f"<div class='subtext'>{last_d[:7]} (latest)</div></div>"
            f"<div style='display:flex;gap:4px;flex-wrap:wrap'>{older}"
            f"</div></div>")
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
        for ref, kind, idx in clouddb.photos_index(_photo_refs(None, address)):
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
    """📋 THE MORNING BRIEF, readable (Dallon, Jul 9 pm: 'it looks like
    a block of text') — built LIVE from the same sources as the nightly
    email, rendered as cards instead of a wall."""
    try:
        import digest
        d = digest.build_data()
    except Exception as e:
        # never a blank page: fall back to the last saved text
        text = clouddb.get_blob("brief") if clouddb.available() else None
        if not text:
            briefs = sorted((BASE / "data" / "briefs").glob("brief-*.txt")) \
                if (BASE / "data" / "briefs").exists() else []
            text = briefs[-1].read_text() if briefs else f"(no brief: {e})"
        return page("Morning brief",
                    f"<div class='card'><pre style='font-size:14px;"
                    f"white-space:pre-wrap'>{esc(text)}</pre></div>")

    cards = ""
    if d["pin"]:
        cards += ("<div class='card' style='border-left:4px solid "
                  "var(--gold);background:var(--goldbg)'>"
                  "<h2 style='margin-top:0;color:var(--goldink)'>"
                  "📌 For the office this morning</h2>"
                  "<ul style='margin:0;padding-left:20px;line-height:1.75;"
                  "font-size:14.5px'>"
                  + "".join(f"<li>{esc(b)}</li>" for b in d["pin"])
                  + "</ul></div>")
    for s in d["sections"]:
        items = ("<ul style='margin:6px 0 0;padding-left:20px;"
                 "line-height:1.7;font-size:14px'>"
                 + "".join(f"<li>{esc(i)}</li>" for i in s["items"])
                 + "</ul>") if s["items"] else ""
        cards += (f"<div class='card'><h2 style='margin:0'>"
                  f"{s['icon']} {esc(s['title'])}</h2>"
                  + (f"<div class='subtext' style='margin-top:2px'>"
                     f"{esc(s['sub'])}</div>" if s["sub"] else "")
                  + items + "</div>")

    body = (f"<div class='mock'>{_chrome_bar('Brief')}"
            f"<div style='padding:20px 26px;max-width:860px'>"
            f"<h2 style='margin:2px 0 2px'>📋 Morning brief</h2>"
            f"<div class='subtext' style='margin-bottom:14px'>"
            f"{esc(d['date'])} · built fresh just now · the same brief "
            f"lands in Dallon's email every night at 9</div>"
            + cards + "</div></div>")
    return page("Morning brief", body, chrome="bare")


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


def _auth_token(pw):
    """The signed session-cookie value for a valid login. It's an HMAC
    of the password, so it proves 'this browser knew the password'
    without ever storing the password in the cookie. Only someone who
    knows the password can produce it."""
    import hashlib
    import hmac as _hmac
    return _hmac.new(pw.encode(), b"mb-office-authed",
                     hashlib.sha256).hexdigest()


def login_page(error=False, nexturl="/"):
    """A real sign-in page (Dallon Jul 10: 'see password at sign in').
    Replaces the browser's native popup so we can offer a 👁 show-
    password toggle — typing a shared password blind on a phone was
    error-prone."""
    err = ("<div style='background:#8a1f13;color:#ffd9d2;padding:8px 12px;"
           "border-radius:8px;margin-bottom:12px;font-size:13.5px'>"
           "That password didn't match — try again.</div>"
           if error else "")
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,"
            "initial-scale=1'><title>Master Butler — Sign in</title>"
            "<style>"
            "body{margin:0;min-height:100vh;display:flex;align-items:"
            "center;justify-content:center;background:#12211a;"
            "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
            "Roboto,sans-serif;color:#eaf2ec}"
            ".box{background:#1a2f25;border:1px solid #2f4a3c;"
            "border-radius:16px;padding:30px 28px;width:min(360px,90vw);"
            "box-shadow:0 8px 30px rgba(0,0,0,.4)}"
            "h1{margin:0 0 4px;font-size:22px;color:#8fd8b0}"
            ".sub{color:#a3bcae;font-size:13px;margin-bottom:18px}"
            "label{display:block;font-size:12px;color:#a3bcae;"
            "margin:12px 0 4px;font-weight:700;text-transform:uppercase;"
            "letter-spacing:.5px}"
            "input{width:100%;box-sizing:border-box;padding:11px 12px;"
            "border-radius:9px;border:1px solid #2f4a3c;background:#12211a;"
            "color:#eaf2ec;font-size:16px}"
            ".pw{position:relative}"
            ".eye{position:absolute;right:6px;top:50%;"
            "transform:translateY(-50%);background:none;border:0;"
            "color:#a3bcae;cursor:pointer;font-size:18px;padding:6px 8px}"
            ".show{display:flex;align-items:center;gap:8px;margin-top:12px;"
            "font-size:13.5px;color:#a3bcae;cursor:pointer}"
            ".show input{width:auto}"
            "button.go{width:100%;margin-top:18px;padding:12px;"
            "border:0;border-radius:10px;background:#177245;color:#fff;"
            "font-weight:800;font-size:15px;cursor:pointer}"
            "</style></head><body><form class='box' method='POST' "
            f"action='/login'><input type='hidden' name='next' "
            f"value='{esc(nexturl)}'>"
            "<h1>🎩 Master Butler</h1>"
            "<div class='sub'>Office sign-in</div>"
            f"{err}"
            # WHO ARE YOU = BUTTONS, not typing (Dallon, Jul 12: he
            # typed 'Office' and every claim became 'office is
            # working…' — real names only, one tap)
            "<label>Who are you?</label>"
            "<input type='hidden' name='who' id='whoval'>"
            "<div id='whobtns' style='display:flex;flex-wrap:wrap;"
            "gap:8px;margin-top:6px'>"
            + "".join(
                f"<button type='button' data-n='{n}' style='flex:1;"
                f"min-width:30%;padding:12px 8px;border-radius:10px;"
                f"border:1px solid #2f4a3c;background:#16281f;"
                f"color:#cfe0d6;font-weight:800;font-size:14px;"
                f"cursor:pointer'>{n}</button>"
                for n in ("LaRee", "Martha", "Jessica", "Dallon", "Tom"))
            + "</div>"
            """<script>
document.getElementById('whobtns').addEventListener('click',function(e){
  var b = e.target.closest('button[data-n]');
  if (!b) return;
  document.getElementById('whoval').value = b.dataset.n;
  document.querySelectorAll('#whobtns button').forEach(function(x){
    x.style.background = '#16281f'; x.style.color = '#cfe0d6';});
  b.style.background = '#c9a227'; b.style.color = '#0b3d2e';
});
</script>"""
            "<label>Password</label>"
            "<div class='pw'><input id='pw' name='password' "
            "type='password' autocomplete='current-password' autofocus>"
            "<button type='button' class='eye' id='eye' "
            "title='Show password' onclick=\"var p=document."
            "getElementById('pw');var e=document.getElementById('eye');"
            "if(p.type==='password'){p.type='text';e.textContent='🙈';}"
            "else{p.type='password';e.textContent='👁';}\">👁</button></div>"
            "<label class='show'><input type='checkbox' onclick=\""
            "document.getElementById('pw').type=this.checked?'text':"
            "'password';\"> Show password</label>"
            "<button class='go' type='submit'>Sign in</button>"
            "</form></body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        pw = _password()
        if not pw:
            return HOST in ("127.0.0.1", "localhost")   # no pw = local only
        # 1) the session cookie from the login form (the normal path)
        import hmac as _hmac
        m = re.search(r"mb_auth=([0-9a-f]{64})",
                      self.headers.get("Cookie") or "")
        if m and _hmac.compare_digest(m.group(1), _auth_token(pw)):
            return True
        # 2) HTTP Basic still accepted — the poller's /api calls, old
        # bookmarks, and anything scripted keep working unchanged
        import base64
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                got = base64.b64decode(hdr[6:]).decode()
                if got.split(":", 1)[-1] == pw:
                    # upgrade this basic-auth (old bookmark) session to a
                    # cookie so background fetches stop 401-ing and the
                    # native popup never returns (office iPad, Jul 13)
                    self._basic_upgrade = _auth_token(pw)
                    return True
                return False
            except Exception:
                return False
        return False

    def _require_auth(self):
        # browsers viewing a page → the friendly login form (with the
        # show-password toggle); API/tools → plain 401
        accept = self.headers.get("Accept", "")
        if "text/html" in accept and self.command == "GET":
            nxt = self.path if self.path.startswith("/") else "/"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(login_page(nexturl=nxt))
            return
        # Native-popup fix (office iPad, Jul 13): only CHALLENGE with
        # Basic when the client already opted into it (sent an
        # Authorization header) — i.e. the poller/scripts. A browser's
        # background fetch/image that just lacks the cookie must NOT get
        # WWW-Authenticate, or Safari throws its own login popup over the
        # working page. Those get a plain 401 the page's JS swallows.
        self.send_response(401)
        if self.headers.get("Authorization", "").startswith("Basic "):
            self.send_header("WWW-Authenticate",
                             'Basic realm="Master Butler office"')
        self.end_headers()
        self.wfile.write(b"login required")

    def _send(self, content, code=200, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        tok = getattr(self, "_basic_upgrade", None)
        if tok:
            self.send_header("Set-Cookie", f"mb_auth={tok}; Path=/; "
                             "Max-Age=31536000; SameSite=Lax")
        if "text/html" in ctype:
            # never let a browser show yesterday's design (Dallon,
            # Jul 10 pm: new Settings/Win-back 'still aren't there' —
            # they were live; his browser had the old page cached)
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/health":          # no auth, no data — lets the
            return self._send(b"ok")        # poller keep the service warm
        if self.path.startswith("/login"):  # the sign-in page (no auth)
            return self._send(login_page())
        if self.path == "/logout":
            self.send_response(303)
            self.send_header("Set-Cookie",
                             "mb_auth=; Path=/; Max-Age=0")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        # SIGNED photo links (no session auth — Jobber's fetcher uses
        # these to pull bid photos onto the client profile). The HMAC
        # token makes each URL unguessable; nothing is listable.
        m = re.match(r"^/pub/photo/([0-9a-f]{16})/([\w.-]+)/(\w+)/"
                     r"(?:\w+-)?(\d+)(?:\.jpg)?$", self.path)
        if m:
            tok, ref, kind, idx = m.groups()
            if tok != _photo_token(ref, kind, idx):
                return self._send(b"bad token", 403)
            img = clouddb.get_photo(ref, kind, int(idx)) \
                if clouddb.available() else None
            if not img:
                return self._send(b"not found", 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img)))
            self.end_headers()
            self.wfile.write(img)
            return
        if not self._authed():
            return self._require_auth()
        if self.path == "/" or self.path.startswith("/?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cm = re.search(r"office_user=([^;]+)",
                           self.headers.get("Cookie") or "")
            u = urllib.parse.unquote(cm.group(1)) if cm else None
            return self._send(inbox_page(q.get("c", [None])[0], user=u,
                                         pushed=q.get("pushed", [None])[0]))
        if self.path.startswith("/newbid"):
            # NEW-DESIGN PREVIEW (Dallon's Stitch 'Unified Command
            # Center', Jul 10) — parallel route; office pages untouched
            # until the evening flip.
            m = re.match(r"^/newbid(?:/([\w-]+))?", self.path)
            want = m.group(1) if m else None
            recs = dict(_shadow_source())
            rec = recs.get(want)
            if not rec:                        # newest priced record
                for s_, r_ in sorted(recs.items(), reverse=True):
                    if ((r_.get("draft") or {}).get("total")
                            and not r_.get("merged_into")
                            and not r_.get("spam_auto")):
                        want, rec = s_, r_
                        break
            if not rec:
                return self._send(b"no records", 404)
            import command_center
            hero, purls = None, []
            try:
                idx = clouddb.photos_index(
                    _photo_refs(want, rec.get("address")))
                by_kind = {}
                for ref, kind, i_ in idx:
                    if kind == "eml":
                        continue
                    by_kind.setdefault(kind, []).append((ref, i_))
                # HERO (Dallon's confirmation, Jul 10): the street photo
                # we grab, OR the BEST photo from a tech's notes — tech/
                # customer shots outrank the aerial. 'Best' = largest
                # file (most detail), by real byte size from the DB.
                for kind in ("street", "jobber", "customer", "aerial"):
                    rows = by_kind.get(kind, [])
                    if kind in ("jobber", "customer") and len(rows) > 1:
                        try:
                            sizes = {(r_[0], r_[1], r_[2]): r_[3]
                                     for r_ in clouddb._exec(
                                "SELECT ref, kind, idx, octet_length(data) "
                                "FROM photos WHERE kind = %s AND ref = ANY(%s)",
                                (kind, [r for r, _ in rows]), fetch="all")}
                            rows = sorted(rows, key=lambda t: -sizes.get(
                                (t[0], kind, t[1]), 0))
                        except Exception:
                            pass
                    for ref, i_ in rows:
                        purls.append(f"/img/{ref}/{kind}/{i_}")
                hero = purls[0] if purls else None
            except Exception:
                pass
            hist = None
            try:
                hist = _history_entry(
                    rec.get("address"),
                    (rec.get("from") or "").split("<")[0].strip())
            except Exception:
                pass
            links = []
            try:
                if rec.get("address"):
                    from aerial_view import listing_links
                    links = [("🎥 3D flyover",
                              f"/flyover?addr="
                              f"{urllib.parse.quote(rec['address'])}")]
                    links += [(f"🏠 {n} ↗", u) for n, u in
                              listing_links(rec["address"])]
            except Exception:
                pass
            mk = ""
            try:
                mk = get_must_know(rec.get("address")) or ""
            except Exception:
                pass
            return self._send(command_center.render(
                rec, want, hero_url=hero, hist=hist, must_know=mk,
                photo_urls=purls, links=links).encode())
        if self.path == "/queue":              # the pre-Inbox layout,
            return self._send(home_page())     # kept during transition
        if self.path == "/scoreboard":
            return self._send(scoreboard_page())
        if self.path == "/drafts":
            return self._send(drafts_page())
        if self.path.startswith("/winback"):
            return self._send(winback_page("all=1" in self.path))
        if self.path.startswith("/settings"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cm = re.search(r"office_user=([^;]+)",
                           self.headers.get("Cookie") or "")
            return self._send(settings_page(
                (q.get("msg") or [""])[0],
                user=urllib.parse.unquote(cm.group(1)) if cm else None))
        if self.path == "/history":
            return self._send(history_page())
        if self.path == "/guide":
            return self._send(guide_page())
        if self.path == "/working":
            return self._send(working_page())
        if self.path == "/autodrafts":
            cm = re.search(r"office_user=([^;]+)", self.headers.get(
                "Cookie") or "")
            return self._send(autodrafts_page(
                urllib.parse.unquote(cm.group(1)) if cm else None))
        if self.path == "/pwwinback":
            # NOTE: /winback (above) is the office's lifetime-value call
            # list; this is the Jul 15 PW-specific lapsed list — related
            # but different questions, kept separate on purpose.
            return self._send(pw_winback_page())
        if self.path == "/tomstandby":
            cm = re.search(r"office_user=([^;]+)",
                           self.headers.get("Cookie") or "")
            return self._send(tom_standby_page(
                urllib.parse.unquote(cm.group(1)) if cm else None))
        if self.path == "/plan_autorespond":
            # the Auto-Respond plan of attack, embedded on the build
            # board via iframe (Dallon, Jul 14: "add this entire widget
            # to the build board"). Blob-backed so the doc updates
            # without a deploy; its styles stay isolated in the frame.
            html = _blob_rw("autorespond_plan", "") or \
                "<p style='font-family:sans-serif;padding:20px'>" \
                "Plan not loaded yet.</p>"
            return self._send(("<!doctype html><html><head>"
                               "<meta charset='utf-8'>"
                               "<meta name='viewport' content="
                               "'width=device-width,initial-scale=1'>"
                               "</head><body style='margin:0'>"
                               + html + "</body></html>").encode("utf-8"))
        if self.path == "/route_demo":
            return self._send(route_demo_page())
        if self.path.startswith("/routes"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(routes_page(
                (q.get("d") or [None])[0],
                (q.get("k") or ["visits"])[0],
                fresh=bool((q.get("fresh") or [""])[0])))
        if self.path.startswith("/flyover"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(flyover_page(q.get("addr", [""])[0]))
        if self.path.startswith("/customers"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(customers_tab_page(
                q.get("c", [None])[0], (q.get("q") or [""])[0]))
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
            # CLASSIC PAGE RETIRED (running list, Jul 12): every old
            # /bid link lands on the ONE customer card in the queue
            _bs = m.group(1)
            _bb = next((b for b in load_bids() if b["stamp"] == _bs),
                       None)
            _be = _bid_email(_bb) if _bb else None
            _key = _be or f"stamp:{_bs}"
            self.send_response(303)
            self.send_header("Location",
                             f"/?c={urllib.parse.quote(_key)}")
            self.end_headers()
            return
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
                     r"canned_replies|msg_read|jobber_tokens|ideas)$", self.path)
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

        # ── sign-in form POST (must run BEFORE the auth gate) ──
        if self.path == "/login":
            form = urllib.parse.parse_qs(body.decode())
            pw = _password()
            given = form.get("password", [""])[0]
            # iPad-proofing (office lockout, Jul 13): autocomplete adds
            # a trailing space, and copying from Messages/Notes swaps
            # the hyphen for a long dash — both invisible on screen.
            # Strip and normalize every dash variant before comparing.
            given = re.sub(r"[‐-―−]", "-", given).strip()
            nxt = form.get("next", ["/"])[0]
            if not nxt.startswith("/"):
                nxt = "/"
            import hmac as _hmac
            if pw and _hmac.compare_digest(given, pw):
                self.send_response(303)
                # 1-year session cookie; the name tag rides along too
                self.send_header("Set-Cookie",
                                 f"mb_auth={_auth_token(pw)}; Path=/; "
                                 "Max-Age=31536000; SameSite=Lax")
                who = form.get("who", [""])[0].strip()
                # generic identities can never become the name tag
                if who.lower() in ("office", "admin", "masterbutler",
                                   "master butler", "mb", "user"):
                    who = ""
                if who:
                    self.send_header(
                        "Set-Cookie",
                        f"office_user={urllib.parse.quote(who)}; Path=/; "
                        "Max-Age=31536000")
                self.send_header("Location", nxt)
                self.end_headers()
                return
            # wrong password → re-show the form with the error
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(login_page(error=True, nexturl=nxt))
            return

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

        def _mark_done_for(customer_str):
            m = re.search(r"<([^>]+)>", customer_str or "")
            if not m:
                return
            d = _msg_read()
            from datetime import timezone as _tzz
            d[m.group(1).lower()] = datetime.now(_tzz.utc).isoformat(
                timespec="seconds")
            _msg_read_save(d)

        if self.path == "/review":
            entry = {"stamp": get("stamp"), "action": get("action"),
                     "customer": get("customer"),
                     "reason": get("reason") or None,
                     "note": get("note") or None}
            # approving AFTER editing prices = an 'adjusted' decision —
            # the calibration ledger learns from the edit, automatically
            if get("action") == "approve":
                _rec0 = dict(_shadow_source()).get(get("stamp")) or {}
                if any(s.get("orig_price") is not None for s in
                       (((_rec0.get("draft") or {}).get("bid") or {})
                        .get("services") or [])):
                    entry["action"] = "adjusted"
            # PUSH: globally via PUSH_ON_APPROVE, or per-bid via the
            # push_allow blob (Dallon Jul 9: Martha's bid front-to-back,
            # nothing else). Reads the record from wherever records live
            # (the old file-only read silently no-op'd in the cloud).
            if get("action") == "approve" and (
                    _push_enabled()
                    or get("stamp") in _blob_rw("push_allow", [])):
                _rec_g = dict(_shadow_source()).get(get("stamp")) or {}
                _oqg = _rec_g.get("open_quote_ctx") or {}
                # block a second quote only when one is genuinely LIVE
                # (open or approved) — archived/converted quotes are
                # history context (Kevin Pham), not a conflict
                if _oqg and _oqg.get("status") in (
                        "draft", "awaiting_response",
                        "changes_requested", "approved"):
                    entry["note"] = (f"NOT pushed — customer already has "
                                     f"quote #{_oqg.get('number')} "
                                     f"({_oqg.get('status')}). Work from "
                                     "that one in Jobber.")
                    save_review(entry)
                    self.send_response(303)
                    back_g = get("back")
                    self.send_header("Location", back_g if
                                     back_g.startswith("/") else "/")
                    self.end_headers()
                    return
                already_q = quote_numbers().get(get("stamp"))
                if already_q:            # double-click guard: one bid,
                    entry["jobber_quote"] = already_q   # one quote, ever
                    entry["note"] = (f"already pushed as #{already_q} — "
                                     "no second quote created")
                    save_review(entry)
                    self.send_response(303)
                    self.send_header("Location", f"/bid/{get('stamp')}")
                    self.end_headers()
                    return
                rec = dict(_shadow_source()).get(get("stamp")) or {}
                d = rec.get("draft")
                if rec.get("dns_match"):     # HARD BLOCK, even when live
                    entry["note"] = ("REFUSED: do-not-service match — "
                                     "no quote pushed")
                    d = None
                _od = _office_drafting(rec.get("open_quote_ctx"),
                                       get("stamp"))
                if _od:                      # HARD BLOCK: office already
                    entry["note"] = (       # has a draft — never duplicate
                        f"REFUSED: office already has a DRAFT quote "
                        f"(#{_od}) in Jobber — finish/send it there, no "
                        "second quote created")
                    d = None
                if d:
                    import jobber_client as jc
                    jc.DRY_RUN = False       # real DRAFT quote; never sends
                    purls = _photo_urls_for(get("stamp"),
                                            rec.get("address"),
                                            self.headers.get("Host"))
                    res = jc.push_approved_bid(
                        d["customer"], d["bid"], d.get("prop_info"),
                        photo_urls=purls,
                        photo_note=f"Photos from bid {get('stamp')} "
                        "(bid system, auto-attached on approve).")
                    q = (res.get("quoteCreate", {}) or {}).get("quote", {})
                    errs = (res.get("quoteCreate", {}) or {}).get(
                        "userErrors") or []
                    if q.get("quoteNumber"):
                        entry["jobber_quote"] = q["quoteNumber"]
                        if q.get("jobberWebUri"):  # clickable # (Jessica)
                            entry["jobber_url"] = q["jobberWebUri"]
                    else:
                        # NEVER show the office raw GraphQL — plain words
                        # (Dallon saw a wall of JSON on the queue, Jul 9)
                        why = ("; ".join(e.get("message", "")
                                         for e in errs)[:150] if errs
                               else str((res.get("body") or [{}])[0]
                                        .get("message", res))[:150]
                               if res.get("error") else str(res)[:150])
                        entry["note"] = (f"⚠ QUOTE PUSH FAILED — the bid "
                                         f"is approved but NO quote was "
                                         f"made in Jobber. Tell Dallon. "
                                         f"({why})")
                else:
                    entry["jobber_quote"] = ("no structured draft on this "
                                             "record — re-run needed")
            save_review(entry)
            _mark_done_for(get("customer"))    # a decision = seen it
            # a quote was just created → land back on the card with a
            # clickable 'open in Jobber' banner (LaRee's approve refresh)
            _jq = entry.get("jobber_quote")
            if isinstance(_jq, int) or (isinstance(_jq, str) and _jq.isdigit()):
                _bk = get("back")
                _bk = _bk if _bk.startswith("/") else "/"
                _sep = "&" if "?" in _bk else "?"
                self.send_response(303)
                self.send_header("Location",
                                 f"{_bk}{_sep}pushed={get('stamp')}")
                self.end_headers()
                return
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
                # land on the INBOX card, not the classic page (LaRee:
                # 'there should not be a classic customer page')
                _k = (get("email") or "").strip().lower() \
                    or f"stamp:{stamp}"
                self.send_response(303)
                self.send_header("Location",
                                 f"/?c={urllib.parse.quote(_k)}")
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
            _back = get("back")
            if _back == "customers":
                _page = customers_page
            elif _back.startswith("custtab:"):
                _ckey = _back[8:]
                def _page(sel, draft=""):
                    return customers_tab_page(_ckey, draft=draft,
                                              user=_user)
            elif _back.startswith("bid:"):
                _bstamp = _back[4:]
                def _page(sel, draft=""):
                    return bid_page(_bstamp, draft=draft)
            elif _back.startswith("inbox:"):
                _ikey = _back[6:]
                def _page(sel, draft=""):
                    return inbox_page(_ikey, draft=draft, user=_user)
            else:
                _page = messages_page
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
                    "HOUSE RULES you may rely on (from the office's own "
                    "documents): moss removal happens only in August — "
                    "moss treatment (~$150) goes on first and needs 4-6 "
                    "weeks to work; window cleaning and pressure washing "
                    "pause each year from Oct 15 until late February; "
                    "gutters can't be cleaned while holiday lights are "
                    "up (Oct-Feb); roof blow-off is only sold together "
                    "with a gutter cleaning unless the home has gutter "
                    "guards; window tracks are NOT included in window "
                    "cleaning (we wipe the base; detailed track cleaning "
                    "is an extra the technician prices on-site); skylights "
                    "are not included on high-risk roofs that only our "
                    "lead technician can service; we carry liability "
                    "insurance with American Family Insurance; discount: "
                    + _blob_rw("discount_policy", {}).get(
                        "customer",
                        "15% off services booked in the second half of "
                        "August or September")
                    + (("; other discount rules: "
                        + _blob_rw("discount_policy", {}).get("extra", "")
                        .replace("\n", "; "))
                       if _blob_rw("discount_policy", {}).get("extra")
                       else "")
                    + "; moss product: a commercial Dalco "
                    "product we call Moss Off, billed per canister "
                    "($14.50, 1-3 typical, technician determines "
                    "on-site).\n"
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
            personal = bool(get("mine"))      # ★ my-own set (Jessica)
            if personal:
                allp = _blob_rw("canned_replies_personal", {})
                canned = allp.get(_user, {})
            else:
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
                    if personal:
                        allp[_user] = canned
                        _blob_save("canned_replies_personal", allp)
                        act += f" (personal — {_user})"
                    else:
                        _blob_save("canned_replies", canned)
                    save_review({"stamp": "", "action": "settings_change",
                                 "customer": "QUICK RESPONSES",
                                 "note": act})
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" +
                             urllib.parse.quote("Saved."))
            self.end_headers()
            return
        elif self.path == "/svcdesc_save":
            # customer-visible quote text — name tag required, logged
            if not _user:
                self.send_response(303)
                self.send_header("Location", "/settings?msg=" +
                                 urllib.parse.quote("Pick your name in the "
                                                    "top bar first."))
                self.end_headers()
                return
            sd = _blob_rw("service_descriptions", {})
            name = get("name").strip()
            if name in sd and get("text").strip():
                sd[name] = get("text").strip()
                _blob_save("service_descriptions", sd)
                save_review({"stamp": "", "action": "settings_change",
                             "customer": "QUOTE DESCRIPTIONS",
                             "note": f"'{name}' description edited"})
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" +
                             urllib.parse.quote("Saved."))
            self.end_headers()
            return
        elif self.path == "/discounts_save":
            if not _user:
                self.send_response(303)
                self.send_header("Location", "/settings?msg=" +
                                 urllib.parse.quote("Pick your name in the "
                                                    "top bar first."))
                self.end_headers()
                return
            dp = {"customer": get("customer").strip(),
                  "fnf": get("fnf").strip(),
                  "extra": get("extra").strip()}
            _blob_save("discount_policy", dp)
            save_review({"stamp": "", "action": "settings_change",
                         "customer": "DISCOUNTS",
                         "note": f"discount policy updated: customer="
                                 f"'{dp['customer'][:60]}' fnf="
                                 f"'{dp['fnf'][:40]}'"})
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" +
                             urllib.parse.quote("Saved."))
            self.end_headers()
            return
        elif self.path == "/signature_save":
            if not _user:
                self.send_response(303)
                self.send_header("Location", "/settings?msg=" +
                                 urllib.parse.quote("Pick your name in the "
                                                    "top bar first."))
                self.end_headers()
                return
            sig = get("signature").strip()
            # keep it personalizable — if they stripped {name}, add it
            if sig and "{name}" not in sig:
                sig = "— {name}\n" + sig
            if get("mine"):                    # per-person signature
                allp = _blob_rw("email_signatures_personal", {})
                if get("clear") or not sig:
                    allp.pop(_user, None)
                    msg = f"{_user}: back to the shared signature."
                else:
                    allp[_user] = sig
                    msg = f"{_user}'s own signature saved."
                _blob_save("email_signatures_personal", allp)
            else:                              # the shared office one
                _blob_save("email_signature", sig)
                msg = "Shared signature saved."
            save_review({"stamp": "", "action": "settings_change",
                         "customer": "SIGNATURE", "by": _user,
                         "note": msg})
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" +
                             urllib.parse.quote(msg))
            self.end_headers()
            return
        elif self.path == "/qr_used":
            # usage counter → dropdowns re-order most-used-first (LaRee)
            nm = get("name").strip()
            if nm:
                u = _blob_rw("qr_usage", {})
                u[nm] = (u.get(nm) or 0) + 1
                _blob_save("qr_usage", u)
            return self._send(b"ok")
        elif self.path == "/edit_facts":
            # EDITABLE 'HOW IT WAS PRICED' (LaRee, Jul 10): office
            # corrects pitch/stories/debris/roof → draft reprices AND
            # the house remembers the correction forever (facts_edit).
            stamp_ = get("stamp")
            rec_ = dict(_shadow_source()).get(stamp_)
            if not rec_:
                return self._send(b"record not found", 404)
            edits = {k: get(k) for k in
                     ("pitch", "stories", "debris", "roof_material")
                     if get(k)}
            import facts_edit
            # SQFT (Dallon, Jul 15): the box comes PREFILLED with the
            # current number — only count it as a correction when the
            # office actually changed it, or every pitch fix would also
            # 'correct' the sqft it was born with.
            _sq_new = facts_edit._clean_sqft(get("sqft"))
            _sq_cur = ((rec_.get("draft") or {}).get("prop_info")
                       or {}).get("sqft")
            if _sq_new and _sq_new != _sq_cur:
                edits["sqft"] = _sq_new
            rec_, summary_ = facts_edit.reprice(
                rec_, edits, by=_user or "office")
            try:
                if clouddb.available():
                    clouddb.ingest_shadow(stamp_, rec_)
                else:
                    (BASE / "data" / "shadow_bids" / f"{stamp_}.json"
                     ).write_text(json.dumps(rec_, indent=1))
            except Exception:
                pass
            save_review({"stamp": stamp_, "action": "fact_edit",
                         "customer": rec_.get("from"),
                         "note": summary_[:280]})
            back_f = get("back")
            self.send_response(303)
            self.send_header("Location", back_f if
                             back_f.startswith("/") else "/")
            self.end_headers()
            return
        elif self.path == "/mark_spam":
            import re as _r
            m = _r.search(r"([\w.+-]+)@([\w.-]+)", get("sender"))
            addr = (m.group(0).lower() if m else "")
            dom = (m.group(2).lower() if m else "")
            # NEVER learn the pipes the business runs on — one click
            # would file every voicemail/form/Jobber event as spam
            PROTECTED = ("copycall", "squarespace", "getjobber",
                         "masterbutlerinc", "google.com")
            # freemail domains key by FULL ADDRESS — learning 'gmail.com'
            # would hide every Gmail customer (LaRee's spam button, Jul 10)
            FREEMAIL = ("gmail.", "yahoo.", "hotmail.", "outlook.",
                        "comcast.", "icloud.", "aol.", "msn.", "live.",
                        "me.com", "att.net", "frontier.")
            if any(p in dom for p in PROTECTED):
                sender_key = None      # refuse quietly, log below
            elif any(dom.startswith(f) or f in dom for f in FREEMAIL):
                sender_key = addr      # this one person only
            else:
                sender_key = dom or get("sender").lower()[:40]
            spam = _blob_rw("learned_spam", [])
            if sender_key and sender_key not in spam:
                spam.append(sender_key)
                _blob_save("learned_spam", spam)
                _SPAM_CACHE["at"] = 0
            save_review({"stamp": get("stamp"), "action": "learned_spam",
                         "customer": get("sender"),
                         "note": (f"sender '{sender_key}' will be filed as "
                                  "spam from now on" if sender_key else
                                  "REFUSED — protected sender (voicemail/"
                                  "form/Jobber pipe), never spam-learnable")})
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        elif self.path == "/edit_prices":
            # MARTHA'S #1 ASK: fix a price ON the dashboard (no more
            # 'push to Jobber then edit there'). The system still learns:
            # the engine's original price is snapshotted on first edit,
            # so calibration compares office-final vs SYSTEM, not vs
            # the office's own edit.
            stamp = get("stamp")
            rec = dict(_shadow_source()).get(stamp)
            bid_d = ((rec or {}).get("draft") or {}).get("bid") or {}
            svcs = bid_d.get("services") or []
            changes = []
            if rec and svcs and not rec.get("dns_match"):
                # ✕ removals first (LaRee, Jul 13) — collect the checked
                # indexes, then rebuild the list without them
                rm = {i for i in range(len(svcs)) if get(f"rm_{i}")}
                if rm:
                    dropped = [svcs[i]["name"] for i in sorted(rm)]
                    changes.append("removed: " + ", ".join(dropped))
                for i, s in enumerate(svcs):
                    if i in rm:
                        continue
                    raw = get(f"p_{i}")
                    if not raw:
                        continue
                    try:
                        # keep the cents (LaRee, Jul 15: whole-dollar
                        # rounding was mangling the $14.50 moss product)
                        new = round(float(raw), 2)
                    except ValueError:
                        continue
                    cur = _num(s.get("price")) or 0   # corrupt price safe
                    if new != round(cur, 2):
                        s.setdefault("orig_price", cur)
                        changes.append(f"{s['name']}: "
                                       f"${cur:,.2f}→${new:,.2f}")
                        s["price"] = new
                if rm:
                    svcs = [s for i, s in enumerate(svcs) if i not in rm]
                    bid_d["services"] = svcs
                if changes:
                    rec["draft"]["total"] = sum(s["price"] for s in svcs)
                    reason = get("reason") or "no reason tapped"
                    bid_d.setdefault("notes", []).append(
                        f"prices edited by {_user or 'office'} "
                        f"({reason}): " + "; ".join(changes))
                    if clouddb.available():
                        clouddb.ingest_shadow(stamp, rec)
                    else:
                        (SHADOW / f"{stamp}.json").write_text(
                            json.dumps(rec, indent=1))
                        try:
                            from cloudpush import push_or_queue
                            push_or_queue(stamp, rec)
                        except Exception:
                            pass
                    save_review({"stamp": stamp, "action": "price_edited",
                                 "customer": get("customer"),
                                 "reason": reason,
                                 "note": "; ".join(changes)[:250]})
            back = get("back")
            self.send_response(303)
            self.send_header("Location", back if back.startswith("/")
                             else f"/bid/{stamp}")
            self.end_headers()
            return
        elif self.path == "/measure_surfaces":
            # ON-DEMAND aerial measure (older records lack the persisted
            # reads): one Vision survey (~2¢) → real PW areas + debris.
            stamp = get("stamp")
            rec = dict(_shadow_source()).get(stamp)
            if rec and rec.get("address"):
                try:
                    from aerial import cross_check
                    afields, _n = cross_check(
                        {"surfaces": {}, "services": {"gutters": True}},
                        rec["address"])
                    pi = rec.setdefault("draft", {}).setdefault(
                        "prop_info", {})
                    got = afields.get("aerial_surfaces") or {}
                    if got:
                        pi["aerial_surfaces"] = got
                        # 📐→💵 CLOSE THE LOOP (Anna Gal, Jul 15:
                        # pw_driveway was REQUESTED, unpriceable at
                        # intake, and stayed missing even after Jessica
                        # measured). Any requested PW service still
                        # absent from the draft gets priced and added
                        # NOW, from the areas we just measured.
                        _bidd = rec["draft"].setdefault("bid", {})
                        _lines = _bidd.setdefault("services", [])
                        _have = {(_l.get("name") or "").split("(")[0]
                                 .strip().lower() for _l in _lines}
                        for _svc in rec.get("services") or []:
                            if not _svc.startswith("pw_"):
                                continue
                            _new = price_one_service(rec, _svc)
                            for _li in _new or []:
                                if (_li["name"].split("(")[0].strip()
                                        .lower()) in _have:
                                    continue
                                _li["added_by"] = "📐 aerial measure"
                                _lines.append(_li)
                                _bidd.setdefault("notes", []).append(
                                    f"💦 {_li['name']} — requested at "
                                    "intake, priced from the aerial "
                                    "measure (was missing until the "
                                    "surfaces were measured).")
                        rec["draft"]["total"] = sum(
                            _l.get("price") or 0 for _l in _lines)
                    if afields.get("debris") or afields.get("canopy_level"):
                        pi["debris_read"] = (afields.get("debris")
                                             or ("heavy" if afields.get(
                                                 "canopy_level") == "heavy"
                                                 else pi.get("debris_read")))
                    if clouddb.available():
                        clouddb.ingest_shadow(stamp, rec)
                    else:
                        (SHADOW / f"{stamp}.json").write_text(
                            json.dumps(rec, indent=1))
                        try:
                            from cloudpush import push_or_queue
                            push_or_queue(stamp, rec)
                        except Exception:
                            pass
                    save_review({"stamp": stamp, "action": "surfaces_measured",
                                 "customer": get("customer"),
                                 "note": ("aerial: " + ", ".join(
                                     f"{k} ~{a} sqft"
                                     for k, a in got.items())
                                     if got else "no surfaces visible "
                                     "from above")})
                except Exception as e:
                    save_review({"stamp": stamp,
                                 "action": "surfaces_measure_FAILED",
                                 "customer": get("customer"),
                                 "note": str(e)[:150]})
            self.send_response(303)
            self.send_header("Location", f"/bid/{stamp}")
            self.end_headers()
            return
        elif self.path == "/add_service":
            # LINE ADD (Dallon Jul 9; multi-select per Jessica Jul 9):
            # price the checked service(s) from the record and append —
            # no full pipeline re-run.
            stamp = get("stamp")
            rec = dict(_shadow_source()).get(stamp)
            picked = [s for s in form.get("svc", []) if s]
            all_added, all_names = [], []
            if rec and picked and not rec.get("dns_match"):
                for svc in picked:
                    lines = price_one_service(rec, svc)
                    if not lines:
                        continue
                    # NEVER BELOW THEIR LAST INVOICE — the add-a-line
                    # path skipped the floor the intake enforces
                    # (Jessica/Tracy, Jul 15: added Windows In & Out
                    # priced $315; her last job was $371)
                    try:
                        import lastpaid
                        _nm3 = ((rec.get("caller_id") or {}).get("name")
                                or (rec.get("from") or "")
                                .split("<")[0].strip())
                        _fl_notes = lastpaid.apply(
                            lines, address=rec.get("address"),
                            client_name=_nm3)
                        if _fl_notes:
                            rec.setdefault("draft", {}).setdefault(
                                "bid", {}).setdefault("notes", []) \
                               .extend(_fl_notes)
                    except Exception:
                        pass
                    bid_d = rec.setdefault("draft", {}).setdefault("bid", {})
                    svcs = bid_d.setdefault("services", [])
                    names = {s["name"] for s in svcs}
                    added = [li for li in lines if li["name"] not in names]
                    if not added:
                        continue
                    for li in added:      # office's hand — survives any
                        li["added_by"] = _user or "office"  # reprice/sweep
                    svcs.extend(added)
                    all_added += added
                    all_names.append(svc)
                    rec["draft"]["total"] = sum(s["price"] for s in svcs)
                    rec["services"] = sorted(set((rec.get("services") or [])
                                                 + [svc]))
            if all_added:
                svc = ", ".join(all_names)
                added = all_added
                bid_d = rec["draft"]["bid"]
                _pi = (rec.get("draft") or {}).get("prop_info") or {}
                note = (f"{svc.replace('_', ' ')} added from the menu by "
                        f"{_user or 'the office'} — engine-priced from the "
                        "property record "
                        + (f"(debris: {_pi['debris_read']} from imagery)."
                           if _pi.get("debris_read")
                           else "(standard debris assumed).")
                        + (" PW office rule: verify surfaces/pictures "
                           "before booking." if "pw_" in svc
                           else ""))
                bid_d.setdefault("notes", []).append(note)
                rec["pipeline_output"] = (rec.get("pipeline_output", "")
                                          + f"\n      ⚠ {note}")
                if clouddb.available():
                    clouddb.ingest_shadow(stamp, rec)
                else:
                    (SHADOW / f"{stamp}.json").write_text(
                        json.dumps(rec, indent=1))
                    try:
                        from cloudpush import push_or_queue
                        push_or_queue(stamp, rec)
                    except Exception:
                        pass
                save_review({"stamp": stamp, "action": "line_added",
                             "customer": get("customer"),
                             "note": f"{svc}: +${sum(li['price'] for li in added):,.0f}"})
            elif rec and picked:
                # NEVER FAIL SILENTLY (Jessica/Anna Gal, Jul 15: her
                # add looked like it worked, nothing persisted, nothing
                # logged). Say WHY, on the card and in the log.
                _pi9 = ((rec.get("draft") or {}).get("prop_info")) or {}
                _why9 = ("no measured area for this surface — hit 📐 "
                         "Measure surfaces first"
                         if any(s.startswith("pw_") for s in picked)
                         and not _pi9.get("aerial_surfaces")
                         else "the engine couldn't price it from this "
                              "property's facts — send to Dallon/Tom")
                rec.setdefault("draft", {}).setdefault("bid", {}) \
                   .setdefault("notes", []).append(
                    f"⚠️ ADD FAILED: {', '.join(picked)} was NOT added "
                    f"— {_why9}.")
                if clouddb.available():
                    clouddb.ingest_shadow(stamp, rec)
                save_review({"stamp": stamp, "action": "line_add_FAILED",
                             "customer": get("customer"),
                             "note": f"{', '.join(picked)}: {_why9}"})
            back = get("back")
            self.send_response(303)
            self.send_header("Location", back if back.startswith("/")
                             else f"/bid/{stamp}")
            self.end_headers()
            return
        elif self.path == "/fold_click":
            # usage counter (idea E): does the office actually OPEN the
            # 'Similar homes' fold? A week of data decides its fate.
            name = re.sub(r"[^a-z_]", "", get("name"))[:30]
            if name:
                fc = _blob_rw("fold_clicks", {})
                ent = fc.get(name) or {"count": 0}
                ent["count"] += 1
                ent["last"] = datetime.now().isoformat(timespec="seconds")
                if _user:
                    ent["last_by"] = _user
                fc[name] = ent
                _blob_save("fold_clicks", fc)
            return self._send(b"ok")
        elif self.path == "/tom_pick":
            # Tom claims a standby home onto a day of HIS choosing —
            # stamps the record, notifies office via review feed +
            # internal email, groups into the tom_days blob. The office
            # books it in Jobber (no writes from here).
            e2 = get("email").strip().lower()
            day2 = get("day").strip()
            nm2 = get("name").strip()
            if e2 and day2:
                days = _blob_rw("tom_days", {}) or {}
                lst2 = days.setdefault(day2, [])
                if not any(c.get("email") == e2 for c in lst2):
                    lst2.append({"email": e2, "name": nm2,
                                 "picked_by": _user or "Tom"})
                _blob_save("tom_days", days)
                st2 = get("stamp")
                if st2:
                    rec2 = dict(_shadow_source()).get(st2)
                    if rec2:
                        (rec2.setdefault("draft", {})
                         .setdefault("notes", [])).append(
                            f"🏜 TOM PICKED {day2} for this home "
                            f"(dry-window standby) — office: book it "
                            f"in Jobber and confirm with the customer.")
                        if clouddb.available():
                            clouddb.ingest_shadow(st2, rec2)
                save_review({"stamp": st2 or "", "action": "tom_pick",
                             "customer": e2,
                             "note": f"🏜 Tom put {nm2 or e2} on {day2}"})
                try:
                    import mailer
                    mailer.send_internal(
                        f"🏜 Tom claimed a dry day: {day2}",
                        f"{nm2 or e2} → {day2}\n\nOffice: book it in "
                        f"Jobber and send the confirmation (their card "
                        f"has the note).")
                except Exception:
                    pass
            self.send_response(303)
            self.send_header("Location", "/tomstandby")
            self.end_headers()
            return
        elif self.path == "/autodraft_tpl_off":
            # kill switch per reply type for auto-adopted wording
            kind = get("kind").strip()
            if kind:
                off = set(_blob_rw("reply_templates_off", []) or [])
                if get("on"):
                    off.discard(kind)
                else:
                    off.add(kind)
                _blob_save("reply_templates_off", sorted(off))
                save_review({"stamp": "", "action": "autodraft_wording",
                             "customer": kind,
                             "note": ("auto-wording re-enabled" if
                                      get("on") else "auto-wording OFF — "
                                      "built-in template back in use")})
            self.send_response(303)
            self.send_header("Location", "/autodrafts")
            self.end_headers()
            return
        elif self.path == "/idea_send":
            # THE OFFICE'S DIRECT LINE (Dallon Jul 9): ideas go to him by
            # email INSTANTLY, land in the ideas list (Claude reads them
            # every night + every session), and show in the review feed.
            text = get("text").strip()
            if text:
                who = _user or "the office"
                full = text + (f" — {get('context')}" if get("context")
                               else "")
                add_idea(who, full)
                save_review({"stamp": "", "action": "office_idea",
                             "customer": who, "note": text[:250]})
                # straight onto the build board too (Dallon, Jul 14:
                # 'when someone puts an idea through, throw it directly
                # on the build board') — visible to the whole office
                try:
                    wb_ = _blob_rw("working_board", {})
                    wb_.setdefault("ideas", []).insert(0,
                        f"💡 {who}: {text[:160]}")
                    wb_["ideas"] = wb_["ideas"][:20]
                    _blob_save("working_board", wb_)
                except Exception:
                    pass
                try:
                    import mailer
                    ok, why = mailer.send_internal(
                        f"💡 Dashboard idea from {who}",
                        f"{who} suggested, from the dashboard:\n\n"
                        f"“{text}”\n\n({get('context')})\n\n"
                        "It's saved on the ideas list — Claude will "
                        "pre-plan a fix on the nightly run.",
                        to=[mailer.DALLON])
                except Exception:
                    pass
            back = get("back")
            self.send_response(303)
            self.send_header("Location", back if back.startswith("/")
                             else "/")
            self.end_headers()
            return
        elif self.path == "/move_lane":
            # THE HUMAN OVERRIDE on the lane ladder (Dallon, Jul 12).
            # Filing only — no prices, no Jobber, no scoreboard. Every
            # move lands in the corrections DIARY so repeated
            # disagreements become proposed rule changes (never silent
            # policy drift).
            key_ = get("key")
            lane_ = get("lane")
            cm = re.search(r"office_user=([^;]+)",
                           self.headers.get("Cookie") or "")
            who_ = urllib.parse.unquote(cm.group(1)) if cm else "office"
            from datetime import timezone as _tzm
            now_ = datetime.now(_tzm.utc).isoformat(timespec="seconds")
            ml = _blob_rw("manual_lanes", {})
            if lane_ == "auto":
                ml.pop(key_, None)
            elif lane_ in ("declined", "later", "needs_reply",
                           "fixits", "done"):
                ml[key_] = {"lane": lane_, "by": who_, "at": now_}
            _blob_save("manual_lanes", ml)
            if lane_ == "done":            # done also clears (office-wide)
                d_ = _msg_read()
                d_[key_] = now_
                _msg_read_save(d_)
            diary = _blob_rw("lane_corrections", [])
            diary.append({"at": now_, "by": who_, "key": key_,
                          "to": lane_})
            _blob_save("lane_corrections", diary[-500:])
            save_review({"action": "lane_move", "by": who_,
                         "customer": key_,
                         "note": f"filed under '{lane_}' — lane ladder "
                                 "overridden by hand"})
            back_ = get("back")
            self.send_response(303)
            self.send_header("Location",
                             back_ if back_.startswith("/") else "/")
            self.end_headers()
            return
        elif self.path == "/mark_done":
            # explicit 'seen it' (Dallon's read-flow ruling): greys the
            # entry for the whole office; decisions do this automatically
            d = _msg_read()
            from datetime import timezone as _tzd
            d[get("addr")] = datetime.now(_tzd.utc).isoformat(
                timespec="seconds")
            _msg_read_save(d)
            back = get("back")
            self.send_response(303)
            self.send_header("Location", back if back.startswith("/")
                             else "/")
            self.end_headers()
            return
        elif self.path == "/mark_seen_bulk":
            # bulk 'seen it' (Dallon: mark many complete like email).
            # Same grey flag as the ✓ Done button, applied to every
            # checked row at once — reversible, office-wide, and it
            # records NO decision, so the scoreboard/learning are untouched.
            keys = [k for k in form.get("keys", []) if k]
            if keys:
                d = _msg_read()
                from datetime import timezone as _tzb
                now = datetime.now(_tzb.utc).isoformat(timespec="seconds")
                for k in keys:
                    d[k] = now
                _msg_read_save(d)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        elif self.path == "/timed_discount_add":
            # 📅 timed discounts (Dallon + LaRee, Jul 13) — dated window,
            # logged as a settings change under the name tag
            cmt = re.search(r"office_user=([^;]+)",
                            self.headers.get("Cookie") or "")
            whot = urllib.parse.unquote(cmt.group(1)) if cmt else "office"
            try:
                pct = float(get("pct") or 0)
            except ValueError:
                pct = 0
            nm_t, s_t, e_t = (get("name") or "").strip()[:60], \
                get("start") or "", get("end") or ""
            ok = bool(nm_t and 0 < pct <= 60 and s_t and e_t
                      and s_t <= e_t)
            if ok:
                td_ = _blob_rw("timed_discounts", [])
                td_.append({"name": nm_t, "pct": pct, "start": s_t,
                            "end": e_t, "by": whot,
                            "at": datetime.now().isoformat(
                                timespec="seconds")})
                _blob_save("timed_discounts", td_)
                save_review({"stamp": "", "action": "settings_change",
                             "customer": "PRICING", "by": whot,
                             "note": f"timed discount added: {nm_t} "
                                     f"{pct:g}% {s_t}→{e_t}"})
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" +
                             urllib.parse.quote(
                                 f"Timed discount saved: {nm_t} {pct:g}% "
                                 f"({s_t} → {e_t})." if ok else
                                 "Not saved — needs a name, a % (1-60), "
                                 "and start ≤ end dates."))
            self.end_headers()
        elif self.path == "/timed_discount_del":
            cmt = re.search(r"office_user=([^;]+)",
                            self.headers.get("Cookie") or "")
            whot = urllib.parse.unquote(cmt.group(1)) if cmt else "office"
            td_ = _blob_rw("timed_discounts", [])
            try:
                i_ = int(get("idx"))
                gone = td_.pop(i_)
                _blob_save("timed_discounts", td_)
                save_review({"stamp": "", "action": "settings_change",
                             "customer": "PRICING", "by": whot,
                             "note": f"timed discount removed: "
                                     f"{gone.get('name')}"})
            except (ValueError, IndexError, TypeError):
                pass
            self.send_response(303)
            self.send_header("Location", "/settings?msg=" +
                             urllib.parse.quote("Timed discount removed."))
            self.end_headers()
        elif self.path == "/flag_customer":
            # ⚠️ persistent per-customer flag (Dallon, Jul 13 — Garrett
            # Mydland: bad payer we must REMEMBER; spam would erase him).
            # Sticks to the person's email forever; shows as a banner on
            # their card + a chip on their row. Never touches pricing.
            em = _canon_email((get("email") or "").strip().lower())
            cmf = re.search(r"office_user=([^;]+)",
                            self.headers.get("Cookie") or "")
            whof = urllib.parse.unquote(cmf.group(1)) if cmf else "office"
            if em:
                fl = _blob_rw("customer_flags", {})
                fl[em] = {"label": get("label") or "bad_payer",
                          "note": (get("note") or "")[:300],
                          "by": whof,
                          "at": datetime.now().isoformat(timespec="seconds")}
                _blob_save("customer_flags", fl)
                save_review({"action": "customer_flagged", "customer": em,
                             "by": whof,
                             "note": f"{get('label')}: {get('note') or ''}"[:200],
                             "at": datetime.now().isoformat(
                                 timespec="seconds")})
            self.send_response(303)
            back_f = get("back") or "/"
            self.send_header("Location",
                             back_f if back_f.startswith("/") else "/")
            self.end_headers()
        elif self.path == "/flag_customer_clear":
            em = _canon_email((get("email") or "").strip().lower())
            cmf = re.search(r"office_user=([^;]+)",
                            self.headers.get("Cookie") or "")
            whof = urllib.parse.unquote(cmf.group(1)) if cmf else "office"
            fl = _blob_rw("customer_flags", {})
            if em in fl:
                del fl[em]
                _blob_save("customer_flags", fl)
                save_review({"action": "customer_flag_cleared",
                             "customer": em, "by": whof,
                             "at": datetime.now().isoformat(
                                 timespec="seconds")})
            self.send_response(303)
            back_f = get("back") or "/"
            self.send_header("Location",
                             back_f if back_f.startswith("/") else "/")
            self.end_headers()
        elif self.path == "/lane_clear":
            # ZERO-IT-OUT (Dallon, Jul 13: 'the daily done feel — work a
            # lane to empty like Gmail'). Like mark-seen, but STICKY: also
            # records the key in the 'cleared' blob so the 30-min walk-away
            # net can't creep it back this workday. Only a NEW customer
            # message (last_at > cleared time) resurfaces it. Reversible,
            # office-wide, records NO decision (scoreboard/learning safe).
            keys = [k for k in form.get("keys", []) if k]
            if keys:
                from datetime import timezone as _tzc
                now = datetime.now(_tzc.utc).isoformat(timespec="seconds")
                d = _msg_read()
                for k in keys:
                    d[k] = now
                _msg_read_save(d)
                cl = _blob_rw("cleared", {})
                for k in keys:
                    cl[k] = now
                _blob_save("cleared", cl)
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
        elif self.path == "/step_away":
            # Jessica, Jul 9: leave mid-review without losing it for the
            # office — releases the 'working · NAME' claim AND keeps the
            # entry bold so the next person naturally picks it up.
            d = _msg_read()
            d.pop(get("addr"), None)
            _msg_read_save(d)
            cm = re.search(r"office_user=([^;]+)",
                           self.headers.get("Cookie") or "")
            _who = urllib.parse.unquote(cm.group(1)) if cm else "office"
            if get("stamp"):
                cl = _claims()
                if cl.pop(get("stamp"), None) is not None:
                    _save_claims(cl)
            # VISIBLE HANDOFF (Dallon, Jul 13: bring back the 'stepped
            # away' marker alongside 'working this') — the row shows who
            # left it so the next person knows it's a warm pick-up
            from datetime import timezone as _tzs
            ho = _blob_rw("handoffs", {})
            ho[get("addr")] = {"by": _who,
                               "at": datetime.now(_tzs.utc)
                               .isoformat(timespec="seconds")}
            _blob_save("handoffs", ho)
            self.send_response(303)
            self.send_header("Location", "/")
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
                    # THE LIVE GRADE (Dallon, Jul 14): a pre-filled
                    # draft was in the box — score what they actually
                    # sent against it (dates excluded) and learn the gap
                    _pk, _pf = get("prefill_kind"), get("prefill")
                    if _pk and _pf:
                        try:
                            import autorespond as _ar
                            _acc = _ar.accuracy(_pf, text)
                            _sends = _blob_rw("draft_sends", []) or []
                            _sends.append({
                                "kind": _pk, "acc": _acc,
                                "by": _user or "", "to": to,
                                "at": datetime.now().isoformat(
                                    timespec="seconds"),
                                "gap": _ar.learn_gap(_pk, _pf, text)})
                            _blob_save("draft_sends", _sends[-200:])
                        except Exception:
                            pass
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
            _mark_done_for(get("customer"))
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
