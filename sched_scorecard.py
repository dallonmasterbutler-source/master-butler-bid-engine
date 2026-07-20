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
        "mins": offer.get("mins"),
        "why": (offer.get("why") or "")[:200],
        "first_seen": (today or date.today()).isoformat(),
        "actual_date": None,                        # filled by match()
    }
    _save(log)


def _iso_week(d):
    try:
        return date.fromisoformat(d[:10]).isocalendar()[:2]
    except Exception:
        return None


def match(visits):
    """Pair captured offers with the office's real bookings. `visits` =
    [{'address','start'}] from the live Jobber schedule (sched_mine fetches
    these). Sets actual_date on any captured customer now on the calendar."""
    from jobber_client import _same_property
    log = _load()
    changed = False
    for v in log.values():
        if v.get("actual_date") or not v.get("address"):
            continue
        for vis in visits:
            if _same_property(v["address"], vis.get("address") or ""):
                v["actual_date"] = (vis.get("start") or "")[:10]
                changed = True
                break
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
    total = len(rows)
    return {
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
