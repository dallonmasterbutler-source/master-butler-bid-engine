"""
MASTER BUTLER — JOBBER PULSE + RECONCILER (Dallon's ruling, Jul 10:
read-both, no write-back, 'Handled in Jobber' lane).

Two jobs, both READ-ONLY against Jobber:

  pulse()          → today's appointments + the overdue/active/remaining
                     counts from the Jobber home screen, cached to the
                     'jobber_pulse' blob for the dashboard's Today strip.

  reconcile(recs)  → for every inbox record, cross-checks the CUSTOMER
                     against Jobber reality (booked visit today, live
                     quote out, won). Returns {stamp: reason} for rows
                     that are provably handled — the inbox uses it to
                     move them into the 'Handled in Jobber' lane. A row
                     with NO proof is never touched (flag-don't-guess),
                     and a new customer reply always resurfaces upstream.

Charlotte Hingle is the founding example: 'urgent' voicemail in the
New tab while Jobber showed her booked TONIGHT at $599.
"""

import datetime
import json
import re
from pathlib import Path

BASE = Path(__file__).parent

PULSE_QUERY = """
query Pulse($start: ISO8601DateTime!, $end: ISO8601DateTime!) {
  today: visits(filter: {startAt: {after: $start, before: $end}},
                first: 40, sort: {key: START_AT, direction: ASCENDING}) {
    totalCount
    nodes { title startAt isComplete
            client { name emails { address } phones { number } }
            property { address { street city } } }
  }
  late: jobs(filter: {status: late}) { totalCount }
  active: jobs(filter: {status: active}) { totalCount }
  upcoming: jobs(filter: {status: upcoming}) { totalCount }
  today_jobs: jobs(filter: {status: today}) { totalCount }
}
"""


def _digits(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())[-10:]


def _canon(addr):
    try:
        from dns_check import canon_addr
        return canon_addr(addr or "")
    except Exception:
        return re.sub(r"[^a-z0-9]", "", (addr or "").lower())[:40]


def pulse(save=True):
    """Fetch today's Jobber picture. Returns the dict (and caches it)."""
    import jobber_client as jc
    today = datetime.date.today()
    start = f"{today}T00:00:00-07:00"
    end = f"{today}T23:59:59-07:00"
    was, jc.DRY_RUN = jc.DRY_RUN, False        # read-only
    try:
        d = jc._post(PULSE_QUERY, {"start": start, "end": end},
                     "jobber pulse")
    finally:
        jc.DRY_RUN = was
    if not d or d.get("error") or d.get("dry_run"):
        return None
    visits = []
    for v in (d.get("today") or {}).get("nodes", []):
        c = v.get("client") or {}
        visits.append({
            "title": (v.get("title") or "")[:90],
            "at": v.get("startAt"),
            "done": bool(v.get("isComplete")),
            "name": c.get("name"),
            "emails": [e["address"].lower()
                       for e in (c.get("emails") or []) if e.get("address")],
            "phones": [_digits(p.get("number"))
                       for p in (c.get("phones") or []) if p.get("number")],
            "address": " ".join(filter(None, [
                ((v.get("property") or {}).get("address") or {}).get("street"),
                ((v.get("property") or {}).get("address") or {}).get("city")])),
        })
    out = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "date": str(today),
        "visits": visits,
        "counts": {
            "overdue": (d.get("late") or {}).get("totalCount", 0),
            "active": (d.get("active") or {}).get("totalCount", 0),
            "remaining": (d.get("upcoming") or {}).get("totalCount", 0),
            "today": (d.get("today_jobs") or {}).get("totalCount", 0),
        },
    }
    if save:
        try:
            import clouddb
            if clouddb.available():
                clouddb.put_blob("jobber_pulse", out)
            else:
                (BASE / "data" / "jobber_pulse.json").write_text(
                    json.dumps(out))
        except Exception:
            pass
    return out


def load_pulse():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob("jobber_pulse") or {}
    except Exception:
        pass
    p = BASE / "data" / "jobber_pulse.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _t12(iso):
    """Jobber returns UTC ('…T16:00:00Z' = 9:00 AM in Monroe) — show
    the OFFICE's clock, always (Dallon, Jul 10 pm: every visit on the
    Today strip said 4:00 PM; those were 16:00 UTC morning jobs)."""
    try:
        from zoneinfo import ZoneInfo
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.astimezone(ZoneInfo("America/Los_Angeles")) \
                .strftime("%-I:%M %p")
    except Exception:
        return ""


def reconcile(records, pulse_data=None):
    """{stamp: plain-English reason} for records PROVABLY handled in
    Jobber. Proof = a visit on today's schedule, or a LIVE quote out
    (sent/approved). No proof → not in the result → row stays visible."""
    p = pulse_data if pulse_data is not None else load_pulse()
    visits = p.get("visits") or []
    v_email, v_phone, v_addr = {}, {}, {}
    for v in visits:
        for e in v["emails"]:
            v_email[e] = v
        for ph in v["phones"]:
            if len(ph) == 10:
                v_phone[ph] = v
        if v.get("address"):
            v_addr[_canon(v["address"])] = v

    out = {}
    for stamp, rec in records:
        if rec.get("merged_into") or rec.get("spam_auto") \
                or rec.get("tech_sender") or rec.get("dns_match"):
            continue
        m = re.search(r"<([^>]+)>", rec.get("from") or "")
        email = (m.group(1).lower() if m else "") or \
            ((rec.get("caller_id") or {}).get("email") or "")
        phone = _digits(rec.get("phone"))
        addr = _canon(rec.get("address"))

        v = (v_email.get(email) or (v_phone.get(phone) if phone else None)
             or (v_addr.get(addr) if addr else None))
        if v:
            when = _t12(v["at"])
            out[stamp] = (f"✓ done today {when}" if v["done"]
                          else f"booked today {when}")
            continue
        oq = rec.get("open_quote_ctx") or {}
        st = (oq.get("status") or "").lower()
        # RECENCY GUARD: an old converted quote is HISTORY, not proof
        # this message is handled (the Kevin-Pham context quotes go back
        # 13 months). The quote must be from around this conversation:
        # created no more than 45 days before the record, or after it.
        created = oq.get("created") or ""
        rec_day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" \
            if len(stamp) >= 8 and stamp[:8].isdigit() else ""
        recent = False
        if created and rec_day:
            try:
                dq = datetime.date.fromisoformat(created)
                dr = datetime.date.fromisoformat(rec_day)
                recent = (dr - dq).days <= 45
            except ValueError:
                recent = False
        if not recent:
            continue
        if st in ("approved", "converted"):
            out[stamp] = f"won — quote #{oq.get('number')}"
        elif st == "awaiting_response":
            out[stamp] = f"quote sent — #{oq.get('number')}"
    return out


# ── UI: the Today strip (drops into the dark queue, above the list) ──
def today_strip_html():
    """Self-contained (inline styles on the design tokens) so it slots
    into the new dark queue regardless of final class names."""
    p = load_pulse()
    if not p:
        return ""
    c = p.get("counts") or {}
    chips = "".join(
        f"<span style='font-size:10px;font-weight:800;letter-spacing:.8px;"
        f"text-transform:uppercase;padding:5px 12px;border-radius:999px;"
        f"border:1px solid rgba(201,162,39,.18);background:#112921;"
        f"color:{color}'>{label} {c.get(key, 0)}</span>"
        for key, label, color in (
            ("overdue", "Overdue", "#fca5a5" if c.get("overdue") else "#a3adab"),
            ("today", "Today", "#e8c56a"),
            ("active", "Active", "#a3adab"),
            ("remaining", "Remaining", "#a3adab")))
    visits = ""
    for v in (p.get("visits") or [])[:14]:
        when = _t12(v.get("at") or "")
        done = "✓ " if v.get("done") else ""
        visits += (
            f"<span style='flex:none;font-size:11.5px;padding:6px 12px;"
            f"border-radius:10px;border:1px solid rgba(201,162,39,.14);"
            f"background:rgba(17,41,33,.7);color:"
            f"{'#5fbd85' if v.get('done') else '#e2e8f0'}'>"
            f"{done}{when} · {(v.get('name') or '?')[:20]}</span>")
    return (
        "<div style='margin:0 0 14px'>"
        "<div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;"
        "margin-bottom:8px'>"
        "<span style='font-size:10px;font-weight:800;letter-spacing:1.4px;"
        "text-transform:uppercase;color:#a3adab'>Jobber · today</span>"
        + chips +
        f"<span style='font-size:10px;color:#a3adab;margin-left:auto'>"
        f"as of {(p.get('at') or '')[11:16]} · read-only</span></div>"
        "<div style='display:flex;gap:8px;overflow-x:auto;padding-bottom:4px'>"
        + (visits or "<span style='font-size:11.5px;color:#a3adab'>no "
           "appointments today</span>") + "</div></div>")


# ── EVENT DRESSING (Dallon, Jul 10: 'quote approved' rows with no name
# or address, 3rd/4th recurrence). Root cause: events only attached to
# quotes OUR system drafted; office-direct quotes had no match, so the
# event row stayed anonymous. Now every event looks its quote up in
# Jobber BY NUMBER and wears the client's name/address/link — and merges
# into their customer record when one exists. ──
def dress_event(rec, all_records=None, quotes=None):
    """Give a jobber_event record its customer identity. Returns True
    when anything changed. Read-only against Jobber. Pass `quotes`
    (scoreboard.fetch_recent_quotes) when dressing many — fetch ONCE."""
    ev = rec.get("jobber_event") or {}
    qno = ev.get("quote_number")
    if rec.get("merged_into"):
        return False
    if rec.get("address") and "jobber <" not in \
            (rec.get("from") or "").lower():
        return False                     # already dressed

    # NEW-REQUEST events carry no quote number, but the notification
    # body labels everything (Dallon, Jul 10 pm: rows named 'You
    # received a new request fro…' — the customer was Zina Lee, right
    # there under 'Contact name:'). Trust the labeled fields.
    if not qno:
        import re as _r
        body = rec.get("newest_message") or ""
        def _lab(label):
            m = _r.search(label + r":?\s*([^\n]+)", body)
            return m.group(1).strip() if m else ""
        name = _r.sub(r"\s+", " ", _lab("Contact name"))
        em = _lab("Email").lower()
        ph = _lab("Phone")
        am = _r.search(r"Address:\s*\n([^\n]+)\n([^\n]+)", body)
        addr = (", ".join(x.strip() for x in am.groups())
                if am else "")
        if not (name or em):
            return False
        changed = False
        if name:
            rec["from"] = f"{name} <{em}>"
            changed = True
        if ph and not rec.get("phone"):
            rec["phone"] = ph
            changed = True
        if addr and not rec.get("address"):
            rec["address"] = addr
            changed = True
        # fold into the customer's existing record if we track them —
        # an EARLIER dressed copy of the same request counts too (the
        # same notification often arrives twice)
        if em and all_records:
            mine = next((s for s, r in all_records if r is rec), None)
            for stamp2, r2 in all_records:
                if r2 is rec or r2.get("merged_into"):
                    continue
                if r2.get("kind") == "jobber_event" \
                        and not (mine and stamp2 < mine):
                    continue
                if em in (r2.get("from") or "").lower():
                    rec["merged_into"] = stamp2
                    rec["_merge_target"] = stamp2
                    changed = True
                    break
        return changed
    if quotes is None:
        try:
            import scoreboard
            quotes = scoreboard.fetch_recent_quotes(150)
        except Exception:
            return False
    q = next((x for x in quotes
              if str(x.get("quoteNumber")) == str(qno)), None)
    if not q:
        if "older than the recent-quote window" in \
                (rec.get("office_alert") or ""):
            return False    # already said so — don't re-append hourly
        rec["office_alert"] = ((rec.get("office_alert") or "") +
            f" (quote #{qno} is older than the recent-quote window — "
            "open it in Jobber for the customer.)").strip()
        return True
    c = q.get("client") or {}
    raw_em = c.get("emails") or []
    if isinstance(raw_em, dict):
        raw_em = raw_em.get("nodes") or []
    emails = [e.get("address", "").lower() for e in raw_em
              if isinstance(e, dict) and e.get("address")]
    name = (c.get("name") or "").strip()
    prop = ((q.get("property") or {}).get("address") or {})
    addr = " ".join(filter(None, [prop.get("street"), prop.get("city")]))
    changed = False
    if name:
        rec["from"] = f"{name} <{emails[0] if emails else ''}>"
        changed = True
    if addr and not rec.get("address"):
        rec["address"] = addr
        changed = True
    if q.get("jobberWebUri") and not rec.get("open_quote_ctx"):
        rec["open_quote_ctx"] = {
            "number": q.get("quoteNumber"),
            "status": q.get("quoteStatus"),
            "total": (q.get("amounts") or {}).get("total"),
            "url": q.get("jobberWebUri"), "lines": []}
        changed = True
    # merge into the customer's existing record when we track them
    if emails and all_records:
        for stamp2, r2 in all_records:
            if r2 is rec or r2.get("kind") == "jobber_event" \
                    or r2.get("merged_into"):
                continue
            if any(e in (r2.get("from") or "").lower() for e in emails):
                rec["merged_into"] = stamp2
                r2.setdefault("events", []).append(
                    {"event": ev.get("event"), "quote": qno})
                r2["office_alert"] = ((r2.get("office_alert") or "") +
                    f" 🎉 quote #{qno} {ev.get('event', 'event')
                    .replace('_', ' ')} (auto-linked).").strip()
                rec["_merge_target"] = stamp2
                changed = True
                break
    return changed
