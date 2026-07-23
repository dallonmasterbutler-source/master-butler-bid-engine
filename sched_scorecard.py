"""
SHADOW SCHEDULING SCORECARD — proof before power.

Captures the date the shadow scheduler WOULD have offered each customer, at
the moment it's generated (no hindsight), then later matches it against the
day the office ACTUALLY booked in Jobber. Over real weeks this answers the
only question that earns trust:

  · COVERAGE  — of customers who wanted a date, how often did it offer a firm
                one (vs punting to an Area-Week / Tom standby)?
  · AGREEMENT — of the ones the office then booked, how often did its date
                land in the SAME WEEK the office chose?
  · EFFICIENCY— average drive-minutes of its picks (low = tight routes).

Nothing here schedules, reserves, or sends anything. It only observes.
"""
import json
import re
from datetime import date
from pathlib import Path

BLOB = "sched_scorecard"
DATA = Path(__file__).parent / "data"


def _load():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob(BLOB) or {}
    except Exception:
        pass
    p = DATA / "sched_scorecard.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _save(d):
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob(BLOB, d)
            return
    except Exception:
        pass
    DATA.mkdir(exist_ok=True)
    (DATA / "sched_scorecard.json").write_text(json.dumps(d))


def capture(key, name, address, offer, today=None):
    """Record the offer the FIRST time we see this customer wanting a date —
    keep that original decision, don't overwrite it on later renders."""
    if not key or not offer:
        return
    log = _load()
    if key in log:
        return
    log[key] = {
        "name": name,
        "address": address,
        "kind": offer.get("kind"),                 # date / week / standby
        "offered_date": offer.get("date"),
        "offered_truck": offer.get("truck"),       # the tech it would pick
        "mins": offer.get("mins"),
        "why": (offer.get("why") or "")[:200],
        "first_seen": (today or date.today()).isoformat(),
        "actual_date": None,                        # filled by match()
    }
    _save(log)


_MONTHS = {m[:3].lower(): i for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}


def _confirm_date(text, today=None):
    """The date named in an appointment-confirmation message, or None.
    Accepts 'Tuesday, August 4(th)', 'Aug 4', '8/4', '8/4/26' — year
    inferred as the NEXT occurrence (appointments are in the future)."""
    t = today or date.today()
    m = re.search(r"confirmed\s+(?:on|for)\s+([^.!\n]{2,60})", text or "",
                  re.I)
    if not m:
        return None
    frag = m.group(1)
    mm = re.search(r"\b([A-Za-z]{3,9})\.?,?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                   frag)
    if mm and mm.group(1)[:3].lower() in _MONTHS:
        mo, day = _MONTHS[mm.group(1)[:3].lower()], int(mm.group(2))
    else:
        mm = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", frag)
        if not mm:
            return None
        mo, day = int(mm.group(1)), int(mm.group(2))
        if mm.group(3):
            yr = int(mm.group(3))
            yr += 2000 if yr < 100 else 0
            try:
                return date(yr, mo, day)
            except ValueError:
                return None
    try:
        d = date(t.year, mo, day)
    except ValueError:
        return None
    if d < t:                     # 'January 5' said in December = next year
        try:
            d = date(t.year + 1, mo, day)
        except ValueError:
            return None
    return d


def office_confirmed(key, text, today=None):
    """The office SENT a confirmation naming a date (Martha's
    appointment-confirmation quick response, Jul 23) — that date IS
    their scheduling decision, so grade the captured offer right now
    instead of waiting for the Jobber visit to land. First grade wins;
    match() still covers customers confirmed by phone."""
    if not key or not text or "(date)" in text:
        return False
    log = _load()
    v = log.get(key) or log.get((key or "").strip().lower())
    if not v or v.get("actual_date"):
        return False
    d = _confirm_date(text, today)
    if not d:
        return False
    v["actual_date"] = d.isoformat()
    v["actual_src"] = "office confirmation message"
    _save(log)
    return True


def _iso_week(d):
    try:
        return date.fromisoformat(d[:10]).isocalendar()[:2]
    except Exception:
        return None


def match(visits, today=None):
    """Pair captured offers with the office's real bookings. `visits` =
    [{'address','start','techs'}] from the live Jobber schedule. Sets
    actual_date on any captured customer now on the calendar.

    THE MOVE LEDGER (Dallon, Jul 23: 'if the office ends up moving a
    job off the day we will measure it with our own system'): once a
    customer is graded, we KEEP watching their visit. If it no longer
    sits on the recorded day, that's the office un-booking a day we
    (or they) filled — recorded with its phase:
      · before_day — moved at scheduling time (day didn't hold)
      · day_of     — moved on/after the day (a tech couldn't finish)
    This is the overbooking measurement that earns the paces their
    numbers; the safety margin in sched_offers loosens only on this
    evidence."""
    from jobber_client import _same_property
    t = today or date.today()
    log = _load()
    changed = False
    for v in log.values():
        if not v.get("address"):
            continue
        hits = [vis for vis in visits
                if _same_property(v["address"], vis.get("address") or "")]
        if not hits:
            continue
        dates = sorted({(h.get("start") or "")[:10] for h in hits})
        cur = v.get("actual_date")
        if cur and cur in dates:
            continue                      # still where we recorded it
        # prefer the visit matching today or later, else the earliest
        new_d = next((d2 for d2 in dates if d2 >= t.isoformat()),
                     dates[0])
        new_hit = next(h for h in hits
                       if (h.get("start") or "")[:10] == new_d)
        if not cur:
            v["actual_date"] = new_d
            if new_hit.get("techs"):      # grade the truck pick too
                v["actual_techs"] = new_hit["techs"]
            changed = True
            continue
        # a graded customer whose visit LEFT the recorded day = a move
        v.setdefault("moves", []).append({
            "from": cur, "to": new_d, "seen": t.isoformat(),
            "phase": ("day_of" if t.isoformat() >= cur
                      else "before_day")})
        v["actual_date"] = new_d
        if new_hit.get("techs"):
            v["actual_techs"] = new_hit["techs"]
        changed = True
    if changed:
        _save(log)
    return log


def report():
    log = _load()
    rows = list(log.values())
    firm = [r for r in rows if r.get("kind") == "date"]
    punt = [r for r in rows if r.get("kind") in ("week", "standby")]
    booked = [r for r in rows if r.get("actual_date")]
    # agreement: of firm offers the office then booked, same calendar week?
    firm_booked = [r for r in booked if r.get("kind") == "date"
                   and r.get("offered_date")]
    same_week = [r for r in firm_booked
                 if _iso_week(r["offered_date"]) == _iso_week(r["actual_date"])]
    mins = [r["mins"] for r in firm if r.get("mins")]
    # truck agreement: of graded offers that named a truck, how often
    # did the office put that very tech on the job? (the runway to
    # 'automate the trucks scheduling eventually' — Dallon, Jul 23)
    truck_rows = [r for r in booked
                  if r.get("offered_truck") and r.get("actual_techs")]
    truck_hits = [r for r in truck_rows
                  if r["offered_truck"] in r["actual_techs"]]
    # THE MOVE LEDGER (overbooking measurement): jobs the office pulled
    # off a day — before the day (didn't hold at scheduling) vs day-of
    # (a tech couldn't finish). The paces stay guesses until this says
    # otherwise.
    moves = [m for r in rows for m in (r.get("moves") or [])]
    total = len(rows)
    return {
        "moves_total": len(moves),
        "moves_before_day": sum(1 for m in moves
                                if m.get("phase") == "before_day"),
        "moves_day_of": sum(1 for m in moves
                            if m.get("phase") == "day_of"),
        "truck_graded": len(truck_rows),
        "truck_same": len(truck_hits),
        "truck_agreement_pct": (round(100 * len(truck_hits)
                                      / len(truck_rows))
                                if truck_rows else None),
        "total": total,
        "firm_offers": len(firm),
        "punts": len(punt),
        "coverage_pct": round(100 * len(firm) / total) if total else None,
        "booked_and_firm": len(firm_booked),
        "same_week": len(same_week),
        "agreement_pct": (round(100 * len(same_week) / len(firm_booked))
                          if firm_booked else None),
        "avg_drive_min": round(sum(mins) / len(mins), 1) if mins else None,
        "pending": len(firm) - len(firm_booked),
        "rows": sorted(rows, key=lambda r: r.get("first_seen") or "",
                       reverse=True)[:60],
    }


def fetch_and_match(days_back=30, days_fwd=90, verbose=False):
    """Light booking fetch + match — the grading heartbeat (Dallon,
    Jul 23: 'are you saying in a week we haven't booked 1 job to grade
    off of?' — the nightly ran match BEFORE capture, so fresh captures
    waited a full day, and the ledger itself only started Jul 22).
    Pulls just the −30d…+90d visit window (cheap) and grades whatever
    the office has booked. Called nightly AFTER the offer sweep."""
    import time
    import jobber_client as jc
    from sched_mine import VQ
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)
             ).strftime('%Y-%m-%dT08:00:00Z')
    end = (datetime.now(timezone.utc) + timedelta(days=days_fwd)
           ).strftime('%Y-%m-%dT08:00:00Z')
    was, jc.DRY_RUN = jc.DRY_RUN, False
    visits, after, throttles = [], None, 0
    try:
        while True:
            d = jc._post(VQ, {"start": start, "end": end,
                              "after": after},
                         "scorecard match (read-only)")
            if d.get("error"):
                if "Throttled" in str(d) and throttles < 30:
                    throttles += 1
                    time.sleep(30)
                    continue
                break
            page = d.get("visits") or {}
            for n in page.get("nodes") or []:
                a = (n.get("property") or {}).get("address") or {}
                addr = ", ".join(x for x in (a.get("street"),
                                             a.get("city"),
                                             a.get("postalCode")) if x)
                if addr and n.get("startAt"):
                    visits.append({"address": addr,
                                   "start": n["startAt"],
                                   "techs": [u["name"]["full"] for u in
                                             (n.get("assignedUsers") or {})
                                             .get("nodes") or []]})
            pi = page.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                break
            after = pi.get("endCursor")
    finally:
        jc.DRY_RUN = was
    log = match(visits)
    graded = sum(1 for v in log.values() if v.get("actual_date"))
    if verbose:
        print(f"scorecard match: {len(visits)} bookings scanned, "
              f"{graded} offer(s) graded so far")
    return graded
