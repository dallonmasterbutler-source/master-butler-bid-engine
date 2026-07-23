"""
MASTER BUTLER — regression tests for Martha's appointment-confirmation
quick response (Jul 23):

  · the template is always in the shared set and sits at slot #3 until
    its own usage earns it a place (LaRee's most-used ordering intact);
  · (date)/(services) substitution helper exists in the dropdown JS;
  · office_confirmed() reads the date out of the sent confirmation and
    grades the captured shadow offer immediately — first grade wins,
    unfilled '(date)' blanks never grade, years are inferred forward.

Pure/local — MB_SANDBOX, no network, no database.
"""

import os
import sys
from datetime import date

os.environ["MB_SANDBOX"] = "1"

import sched_scorecard as sc

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
MSG = ("Great! We have your appointment confirmed on Tuesday, August 4th "
       "for gutter cleaning, roof blow off, and moss treatment. Thank "
       "you for booking with us. We look forward to servicing your home!")

check("month-name date parses ('Tuesday, August 4th')",
      sc._confirm_date(MSG, TODAY) == date(2026, 8, 4))
check("slash date parses ('confirmed on 8/4')",
      sc._confirm_date("appointment confirmed on 8/4 for gutters",
                       TODAY) == date(2026, 8, 4))
check("slash date with year ('8/4/26')",
      sc._confirm_date("confirmed for 8/4/26.", TODAY) == date(2026, 8, 4))
check("past month rolls to next year ('January 5' said in July)",
      sc._confirm_date("confirmed on January 5", TODAY) == date(2027, 1, 5))
check("no date named → None",
      sc._confirm_date("Your appointment is confirmed, see you soon!",
                       TODAY) is None)

# ── office_confirmed against a stubbed ledger ──
_STORE = {"j@x.com": {"name": "J", "address": "1 Main St", "kind": "date",
                      "offered_date": "2026-08-04", "mins": 9, "why": "",
                      "first_seen": "2026-07-20", "actual_date": None}}
sc._load = lambda: _STORE
sc._save = lambda d: _STORE.update(d)

check("unfilled '(date)' blank never grades",
      sc.office_confirmed("j@x.com", "confirmed on (date) for gutters",
                          TODAY) is False)
check("sent confirmation grades the captured offer",
      sc.office_confirmed("j@x.com", MSG, TODAY) is True
      and _STORE["j@x.com"]["actual_date"] == "2026-08-04"
      and _STORE["j@x.com"]["actual_src"] == "office confirmation message")
check("first grade wins (second send is a no-op)",
      sc.office_confirmed("j@x.com",
                          "appointment confirmed on August 20", TODAY)
      is False and _STORE["j@x.com"]["actual_date"] == "2026-08-04")
check("unknown customer is a no-op",
      sc.office_confirmed("nobody@x.com", MSG, TODAY) is False)

# ── THE MOVE LEDGER (Dallon: 'if the office ends up moving a job off
# the day we will measure it with our own system') ──
_ML = {"m@x.com": {"name": "M", "address": "9 Oak St, Monroe, WA 98272",
                   "kind": "date", "offered_date": "2026-08-04",
                   "offered_truck": "Adam Mcbride",
                   "first_seen": "2026-07-20", "actual_date": None}}
sc._load = lambda: _ML
sc._save = lambda d: _ML.update(d)


def vis(day, techs=("Adam Mcbride",)):
    return [{"address": "9 Oak St, Monroe, WA 98272",
             "start": f"{day}T16:00:00Z", "techs": list(techs)}]


sc.match(vis("2026-08-04"), today=date(2026, 8, 1))
check("first sighting grades date + truck",
      _ML["m@x.com"]["actual_date"] == "2026-08-04"
      and _ML["m@x.com"]["actual_techs"] == ["Adam Mcbride"])
sc.match(vis("2026-08-04"), today=date(2026, 8, 2))
check("visit still on its day = no phantom move",
      not _ML["m@x.com"].get("moves"))
sc.match(vis("2026-08-06", ("Shane Strand",)), today=date(2026, 8, 2))
mv = _ML["m@x.com"]["moves"]
check("moved at scheduling time = before_day move",
      len(mv) == 1 and mv[0]["phase"] == "before_day"
      and mv[0]["from"] == "2026-08-04" and mv[0]["to"] == "2026-08-06"
      and _ML["m@x.com"]["actual_date"] == "2026-08-06")
sc.match(vis("2026-08-10"), today=date(2026, 8, 6))
check("moved on the day itself = day_of move (tech couldn't finish)",
      len(mv) == 2 and mv[1]["phase"] == "day_of")
rep = sc.report()
check("report counts the moves by phase",
      rep["moves_total"] == 2 and rep["moves_before_day"] == 1
      and rep["moves_day_of"] == 1)

# ── the template in the dropdown payload ──
import json

import dashboard

_fake = {"canned_replies": {"Quote Approval": "a", "On our way": "b",
                            "Running late": "c", "Thanks": "d"},
         "canned_replies_personal": {},
         "qr_usage": {"Quote Approval": 40, "On our way": 30,
                      "Running late": 20, "Thanks": 10}}
dashboard._blob_rw = lambda k, dflt=None: _fake.get(k, dflt)
payload = json.loads(dashboard._canned_payload().replace("<\\/", "</"))
keys = list(payload["shared"])
check("appointment confirmation is seeded into the shared set",
      "Appointment confirmation" in payload["shared"]
      and "(date)" in payload["shared"]["Appointment confirmation"]
      and "(services)" in payload["shared"]["Appointment confirmation"])
check("it sits at slot #3 while unused (Martha's ask)",
      keys.index("Appointment confirmation") == 2)
check("most-used ordering intact around it",
      keys[0] == "Quote Approval" and keys[1] == "On our way")
_fake["qr_usage"]["Appointment confirmation"] = 25
payload2 = json.loads(dashboard._canned_payload().replace("<\\/", "</"))
check("once used it earns its place by LaRee's rule",
      list(payload2["shared"]).index("Appointment confirmation") == 2
      and list(payload2["shared"])[3] == "Running late")
check("qrSub helper ships in the dropdown JS",
      "function qrSub" in dashboard._CANNED_MERGE_JS)

print(f"RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
