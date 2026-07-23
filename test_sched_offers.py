"""
MASTER BUTLER — regression tests for the offer engine's truck rules
(Dallon, Jul 23: 'you are also keeping in mind who serviced them last,
the dollar amount on the truck already and the time those services
take?'):

  · $800+ of windows alone = a full truck day → never rides an
    anchored day (standby, office schedules by hand);
  · dollars-on-the-truck: a job's crew-hours must fit what the day's
    trucks have left ($100/crew-hr windows, $140/crew-hr the rest);
  · windows-mix: window hours never stack past one truck's day;
  · continuity: the tech who serviced them last pulls the offer to a
    nearby day he works;
  · anchors without hours (pre-Jul-23 blob) still offer on the old
    jobs-count backstop.

Pure/local — MB_SANDBOX, stubbed geocode/drive/knowledge, no network.
"""

import os
import sys
from datetime import date, timedelta

os.environ["MB_SANDBOX"] = "1"

import routing
import sched_offers as so

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


TODAY = date(2026, 7, 23)
so._geocode = lambda a: (47.62, -122.02)
routing.drive_min = lambda a, b: 10.0
so._last_tech_cached = lambda e: None

D1 = (TODAY + timedelta(days=4)).isoformat()
D2 = (TODAY + timedelta(days=6)).isoformat()


def knowledge(anchors):
    so._knowledge = lambda: {"future_anchors": anchors}


def rec(svcs, email="c@x.com"):
    return {"address": "1 Main St, Sammamish WA", "from": f"C <{email}>",
            "newest_message": "I approve! When can you come?",
            "draft": {"bid": {"services": svcs}}}


BASE_DAY = {"centroid": [47.63, -122.03], "cities": {"Sammamish": 3}}

# ── $800 windows = a full day on its own ──
knowledge({D1: dict(BASE_DAY, jobs=3, hours=2.0, windows_hours=0.0,
                    techs=["Connor L", "Shane P"])})
o = so.offer(rec([{"name": "Window Cleaning", "price": 800}]), TODAY)
check("$800 of windows never rides an anchored day (standby)",
      o["kind"] == "standby" and "full day" in o["why"])

# ── $900 gutters is doable on a 2-truck day ──
o = so.offer(rec([{"name": "Gutter Cleaning", "price": 900}]), TODAY)
check("$900 of gutters fits a 2-truck day (firm date)",
      o["kind"] == "date" and o["date"] == D1)

# ── dollars-on-the-truck: same gutters skip a loaded 1-truck day ──
knowledge({D1: dict(BASE_DAY, jobs=3, hours=4.0, windows_hours=0.0,
                    techs=["Connor L"]),
           D2: dict(BASE_DAY, jobs=2, hours=2.0, windows_hours=0.0,
                    techs=["Shane P", "Austin R"])})
o = so.offer(rec([{"name": "Gutter Cleaning", "price": 900}]), TODAY)
check("loaded 1-truck day skipped, next day offered",
      o["kind"] == "date" and o["date"] == D2)

# ── windows-mix: window hours never stack past one truck's day ──
knowledge({D1: dict(BASE_DAY, jobs=3, hours=6.0, windows_hours=5.5,
                    techs=["Connor L", "Shane P"]),
           D2: dict(BASE_DAY, jobs=2, hours=2.0, windows_hours=0.0,
                    techs=["Shane P"])})
o = so.offer(rec([{"name": "Window Cleaning in/out", "price": 400}]),
             TODAY)
check("$400 windows won't stack onto a 5.5h-windows day",
      o["kind"] == "date" and o["date"] == D2)

# ── continuity: last tech pulls the offer to his day ──
knowledge({D1: dict(BASE_DAY, jobs=2, hours=2.0, windows_hours=0.0,
                    techs=["Shane P"]),
           D2: dict(BASE_DAY, jobs=2, hours=2.0, windows_hours=0.0,
                    techs=["Adam Mcbride"])})
so._last_tech_cached = lambda e: ["Adam Mcbride"]
o = so.offer(rec([{"name": "Gutter Cleaning", "price": 300}]), TODAY)
check("last tech (2 days later) wins the offer",
      o["kind"] == "date" and o["date"] == D2
      and "Adam serviced them last" in o["why"])
so._last_tech_cached = lambda e: None

# ── PER-TRUCK (Dallon: 'automate the trucks scheduling eventually') ──
# a day whose SUM has room but where no single truck fits is FULL
knowledge({D1: dict(BASE_DAY, jobs=4, hours=12.0, windows_hours=0.0,
                    techs=["Connor L", "Shane P"],
                    by_truck={"Connor L": {"jobs": 2, "hours": 6.0,
                                           "win_h": 0.0, "dollars": 840},
                              "Shane P": {"jobs": 2, "hours": 6.0,
                                          "win_h": 0.0, "dollars": 840}}),
           D2: dict(BASE_DAY, jobs=1, hours=1.0, windows_hours=0.0,
                    techs=["Austin R"],
                    by_truck={"Austin R": {"jobs": 1, "hours": 1.0,
                                           "win_h": 0.0, "dollars": 140}})})
o = so.offer(rec([{"name": "Gutter Cleaning", "price": 900}]), TODAY)
check("day-sum has room but no single truck fits -> next day",
      o["kind"] == "date" and o["date"] == D2)
check("the offer names its truck pick",
      o.get("truck") == "Austin R" and "Truck: Austin" in o["why"])

# lightest truck wins when several fit
knowledge({D1: dict(BASE_DAY, jobs=3, hours=7.0, windows_hours=0.0,
                    techs=["Connor L", "Shane P"],
                    by_truck={"Connor L": {"jobs": 2, "hours": 5.0,
                                           "win_h": 0.0, "dollars": 700},
                              "Shane P": {"jobs": 1, "hours": 1.0,
                                          "win_h": 0.0, "dollars": 140}})})
o = so.offer(rec([{"name": "Gutter Cleaning", "price": 300}]), TODAY)
check("lightest truck gets the job", o.get("truck") == "Shane P")

# continuity: last tech's TRUCK preferred when he has room
so._last_tech_cached = lambda e: ["Connor L"]
o = so.offer(rec([{"name": "Gutter Cleaning", "price": 300}]), TODAY)
check("last tech's truck preferred when he has room",
      o.get("truck") == "Connor L" and "his truck" in o["why"])
# ...but a full last-tech truck never overflows
o = so.offer(rec([{"name": "Gutter Cleaning", "price": 900}]), TODAY)
check("full last-tech truck never overflows (job rides the other)",
      o.get("truck") == "Shane P")
so._last_tech_cached = lambda e: None

# per-truck windows rule: window hours can't stack on a windows truck
knowledge({D1: dict(BASE_DAY, jobs=2, hours=6.0, windows_hours=5.5,
                    techs=["Connor L", "Shane P"],
                    by_truck={"Connor L": {"jobs": 1, "hours": 5.5,
                                           "win_h": 5.5, "dollars": 550},
                              "Shane P": {"jobs": 1, "hours": 0.5,
                                          "win_h": 0.0, "dollars": 70}})})
o = so.offer(rec([{"name": "Window Cleaning", "price": 400}]), TODAY)
check("windows job lands on the non-windows truck",
      o["kind"] == "date" and o.get("truck") == "Shane P")

# ── pre-Jul-23 anchors (no hours) still offer on the count backstop ──
knowledge({D1: dict(BASE_DAY, jobs=3, techs=["Connor L"])})
o = so.offer(rec([{"name": "Gutter Cleaning", "price": 900}]), TODAY)
check("anchor without hours falls back to the jobs-count check",
      o["kind"] == "date" and o["date"] == D1)

print(f"RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
