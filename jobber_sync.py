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
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.strftime("%-I:%M %p")
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
