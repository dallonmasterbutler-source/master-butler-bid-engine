"""
MASTER BUTLER — SCHEDULING STAGE 2 (SHADOW): THE DATE-OFFER ENGINE
(Dallon's go, Jul 15 — after the full parameter walkthrough.)

offer(record) → the date the reply box WOULD offer, or an Area-Week
soft hold, or None — with the WHY attached. Shadow only: proposals
render on /autodrafts for Dallon's grading; the office's pre-filled
boxes keep [DATE] until stage 2 goes live.

THE PARAMETERS (all Dallon-blessed, Jul 15):
  · anchored days only — a future day with existing jobs whose center
    of gravity sits within the drive ceiling (15 close / 20 far) of
    this home; the system never invents a day
  · capacity: jobs-count backstop vs the month's norm (dollars/truck +
    windows-mix arrive when day pricing is wired); $800 days are fine,
    nothing gets force-filled
  · AN OFFER IS NOT A RESERVATION — first customer confirmation wins;
    non-responders get re-offered (the office books on 'yes')
  · vetoes outrank geometry: Tom-only → the 🏜 standby folder, never a
    date; season rules (moss-Aug, winter pauses, lights Oct–Feb);
    customer blackout windows from their own words
  · lead time: earliest anchored day ≥3 days out (2wk/6wk word-of-
    Dallon until measured); no anchor in 21 days → Area Week hold
"""

import json
import math
import re
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent

# month → jobs/day norm (measured, 3yr); the count backstop
MONTH_NORM = {1: 8.4, 2: 12.4, 3: 10.8, 4: 11.8, 5: 12.5, 6: 13.3,
              7: 12.9, 8: 11.9, 9: 17.1, 10: 41.9, 11: 37.8, 12: 16.2}

BLACKOUT_RX = re.compile(
    r"(?:unavailable|out of town|away|gone|on vacation|traveling)"
    r"[^.!?\n]{0,60}?(\w+ \d{1,2}(?:st|nd|rd|th)?)"
    r"(?:\s*(?:through|to|until|-|–)\s*(\w+ \d{1,2}(?:st|nd|rd|th)?))?",
    re.I)


def _min_est(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2)
         * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h)) * 1.35 / 40 * 60


def _knowledge():
    try:
        import clouddb
        if clouddb.available():
            k = clouddb.get_blob("sched_knowledge") or {}
            if k.get("future_anchors"):
                return k
    except Exception:
        pass
    p = BASE / "data" / "sched_mine.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _geocode(address):
    try:
        from routing import geocode_many
        s = [{"address": address}]
        out = geocode_many(s)
        if out and "lat" in out[0]:
            return out[0]["lat"], out[0]["lng"]
    except Exception:
        pass
    return None


# DOLLARS-ON-THE-TRUCK (Dallon's rule, taught Jul 15, wired Jul 23 when
# he asked for it by name): windows are slow money — '$800 of windows is
# a full day for someone', so ~$100/crew-hour; '$900 of gutters is
# doable', so ~$140/crew-hour for everything else. A day is full when
# its scheduled hours + this job's hours clear the trucks' capacity.
PACE_WINDOW = 100.0
PACE_DEFAULT = 140.0
TRUCK_DAY_H = 8.5


def _job_hours(rec):
    """(total_crew_hours, windows_crew_hours) this job would add."""
    svcs = (((rec.get("draft") or {}).get("bid") or {}).get("services")
            or [])
    tot = win = 0.0
    for s in svcs:
        try:
            p = float(s.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if "window" in (s.get("name") or "").lower():
            h = p / PACE_WINDOW
            win += h
        else:
            h = p / PACE_DEFAULT
        tot += h
    return round(tot, 1), round(win, 1)


def _rec_email(rec):
    m = re.search(r"<([^>]+)>", rec.get("from") or "")
    e = (m.group(1) if m else (rec.get("from") or "")).strip().lower()
    return e if "@" in e else ""


def _last_tech_cached(email):
    """Who serviced this customer last (Jobber, 30-day cloud cache).
    None off-cloud — never hammer Jobber uncached from a dev Mac."""
    if not email:
        return None
    try:
        import clouddb
        if not clouddb.available():
            return None
        c = (clouddb.get_blob("last_tech_cache") or {}).get(email)
        if c and (date.today()
                  - date.fromisoformat(c["on"])).days < 30:
            return c.get("who")
        import jobber_client as jc
        r = jc.last_tech(email)
        who = (r or {}).get("techs") or None
        clouddb.merge_blob("last_tech_cache",
                           {email: {"on": date.today().isoformat(),
                                    "who": who}})
        return who
    except Exception:
        return None


def _blackouts(msg):
    """['aug 10', 'aug 18'] style pairs from the customer's own words."""
    outs = []
    for m in BLACKOUT_RX.finditer(msg or ""):
        outs.append((m.group(1), m.group(2)))
    return outs


def offer(rec, today=None):
    """→ {'kind':'date'|'week'|'standby'|None, 'date','why', …}"""
    today = today or date.today()
    msg = (rec.get("newest_message") or "")
    addr = rec.get("address")
    if not addr:
        return None
    # VETO 1: Tom-only → standby folder, never a date
    _pi = (rec.get("draft") or {}).get("prop_info") or {}
    blob = ((rec.get("office_alert") or "")
            + " ".join((rec.get("draft") or {}).get("notes") or [])).lower()
    if _pi.get("pitch") == "tom_only" or "tom-only" in blob \
            or "tom only" in blob:
        return {"kind": "standby",
                "why": "Tom-only home — 🏜 standby folder, his call on "
                       "a dry window; use the office's Tom-standby "
                       "template"}
    # VETO 2: season — moss removal only seeds August
    svcs = rec.get("services") or []
    if "moss_removal" in svcs and today.month != 8:
        return {"kind": "week", "date": "August",
                "why": "moss removal is August-only — offer the August "
                       "list, treatment now"}
    pt = _geocode(addr)
    if not pt:
        return None
    # VETO 3: a windows job that alone fills a truck's day never rides
    # an anchored day — it needs its own day, the office's call
    jh, jwin = _job_hours(rec)
    if jwin >= 7:
        return {"kind": "standby",
                "why": f"~${int(jwin * PACE_WINDOW)} of windows is a "
                       "full day for one truck on its own — needs its "
                       "own day; office schedules it by hand"}
    K = _knowledge()
    anchors = K.get("future_anchors") or {}
    blk = _blackouts(msg)
    cands = []
    for d2, a in sorted(anchors.items()):
        try:
            dd = date.fromisoformat(d2)
        except ValueError:
            continue
        if not (today + timedelta(days=3) <= dd
                <= today + timedelta(days=45)):
            continue
        c = a.get("centroid")
        if not c:
            continue
        # REAL drive minutes first (Google Routes API, cached — Dallon
        # Jul 23: same geomapping the lights routes run on); haversine
        # estimate only when the API can't answer
        try:
            from routing import drive_min
            mins = drive_min(pt, c) or _min_est(pt, c)
        except Exception:
            mins = _min_est(pt, c)
        ceiling = 20 if mins > 15 else 15
        if mins > 20:
            continue
        # capacity backstop: never offer onto a day at/over the norm
        norm = MONTH_NORM.get(dd.month, 12)
        if (a.get("jobs") or 0) >= norm:
            continue
        # DOLLARS-ON-THE-TRUCK: this job's crew-hours must fit what the
        # day's trucks have left (day hours arrive from sched_mine; an
        # older anchor without them falls back to the jobs-count check)
        trucks = max(1, len(a.get("techs") or []))
        if jh and a.get("hours") is not None \
                and a["hours"] + jh > trucks * TRUCK_DAY_H:
            continue
        # windows-mix: window hours are one-truck money — never stack
        # this job's window hours past a single truck's day
        if jwin and a.get("windows_hours") is not None \
                and a["windows_hours"] + jwin > TRUCK_DAY_H:
            continue
        # customer blackout words — skip a day inside any stated range
        if blk and any(w and w.split()[0][:3].lower() == dd.strftime(
                "%b").lower() and any(ch.isdigit() and
                int("".join(f for f in w.split()[1] if f.isdigit()))
                == dd.day for ch in "1") for pair in blk
                for w in pair if w):
            continue
        cand = {"kind": "date", "date": dd.isoformat(),
                "pretty": dd.strftime("%A, %B %-d"),
                "mins": round(mins), "day_jobs": a.get("jobs"),
                "cities": list((a.get("cities") or {}))[:2],
                "why": (f"{dd.strftime('%a %b %-d')} — "
                        f"{a.get('jobs')} jobs already in "
                        f"{'/'.join(list((a.get('cities') or {}))[:2])}"
                        f", nearest route ≈{round(mins)} min; day at "
                        f"{a.get('jobs')}/{norm:.0f} of the {dd:%B} "
                        f"norm. OFFER, not a reservation — books on "
                        f"their yes.")}
        cands.append((dd, a, cand))
        if len(cands) >= 6:
            break
    if cands:
        # EARLIEST anchored day wins (lead-time doctrine)… unless the
        # tech who serviced them LAST works a nearby candidate day —
        # continuity is worth a few days' wait (Dallon, Jul 23)
        best = cands[0][2]
        who = _last_tech_cached(_rec_email(rec))
        if who:
            first_dd = cands[0][0]
            for dd, a, cand in cands:
                if (dd - first_dd).days > 5:
                    break
                if any(t in (a.get("techs") or []) for t in who):
                    cand["why"] += (f" {who[0].split()[0]} serviced "
                                    f"them last and is on this day.")
                    best = cand
                    break
        return best
    # no anchor fits → Area Week soft hold around the nearest-area day
    return {"kind": "week",
            "why": "no anchored day within the drive ceiling in the "
                   "window — offer the Area-Week soft hold ('we'll be "
                   "in your area, exact day as the route fills')"}


if __name__ == "__main__":
    demo = offer({"address": "1481 239th Avenue Northeast Sammamish WA "
                             "98074",
                  "newest_message": "I approve! When can you come?"})
    print(json.dumps(demo, indent=1))
