"""
Rhythm test gate — pure logic, no network. Run before every commit:

    python3 test_rhythm.py

Covers the storage round-trip, activity rollups, the grocery split, the
free-window math, and outing suggestions.
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

import planner  # noqa: E402
import store  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  XX  FAIL: {name}")


# ── store ─────────────────────────────────────────────────────────────────
def test_store():
    store._reset_for_tests()
    s = store.get()
    check("store: seeds two people", s["config"]["person_a"] and s["config"]["person_b"])
    check("store: seeds meals", len(s["meal_library"]) >= 3)
    check("store: seeds stores", len(s["stores"]) >= 2)
    check("store: seeds products", len(s["products"]) >= 5)
    check("store: training_log starts empty", s["training_log"] == [])

    store.update(lambda st: st["activity"].append({"x": 1}))
    check("store: write persists", len(store.get()["activity"]) == 1)

    got_id = store.update(lambda st: store.next_id(st, "t"))
    check("store: next_id unique-ish", got_id.startswith("t"))
    store._reset_for_tests()


# ── rollups ───────────────────────────────────────────────────────────────
def test_rollup():
    acts = [
        {"day": "2026-07-20", "person": "a", "kind": "conversation"},
        {"day": "2026-07-20", "person": "a", "kind": "contact"},
        {"day": "2026-07-20", "person": "b", "kind": "interaction"},
        {"day": "2026-07-05", "person": "b", "kind": "contact"},   # same month, other day
        {"day": "2026-06-30", "person": "a", "kind": "contact"},   # prior month
        {"day": "2026-07-20", "person": "a", "kind": "junk"},      # ignored
    ]
    r = planner.rollup(acts, "2026-07-20", goal=10)
    check("rollup: today total = 3", r["today"]["total"]["all"] == 3)
    check("rollup: today A = 2", r["today"]["a"]["all"] == 2)
    check("rollup: month total = 4", r["month"]["total"]["all"] == 4)
    check("rollup: ignores prior month", r["month"]["total"]["all"] == 4)
    check("rollup: goal pct", r["goal_pct"] == 40)
    check("rollup: goal not hit", not r["goal_hit"])

    r2 = planner.rollup(acts, "2026-07-20", goal=3)
    check("rollup: goal hit at 3", r2["goal_hit"])


def test_training():
    products = [{"id": "p0", "name": "A"}, {"id": "p1", "name": "B"},
                {"id": "p2", "name": "C"}]
    # deterministic rotation: consecutive days advance by one, and wrap
    d0 = planner.product_of_day(products, "2026-07-20")
    d1 = planner.product_of_day(products, "2026-07-21")
    d3 = planner.product_of_day(products, "2026-07-23")
    check("training: rotates each day", d0["id"] != d1["id"])
    check("training: wraps after len", d3["id"] == d0["id"])
    check("training: same product same day", d0["id"]
          == planner.product_of_day(products, "2026-07-20")["id"])
    check("training: empty library -> None", planner.product_of_day([]) is None)

    log = [{"day": "2026-07-20", "product_id": "p0"}]
    check("training: trained_today true", planner.trained_today(log, "2026-07-20", "p0"))
    check("training: trained_today false", not planner.trained_today(log, "2026-07-20", "p1"))

    # streak: three days in a row ending today
    log2 = [{"day": "2026-07-18"}, {"day": "2026-07-19"}, {"day": "2026-07-20"}]
    check("training: streak counts run", planner.training_streak(log2, "2026-07-20") == 3)
    # today not done yet, but yesterday+before were -> streak still holds
    check("training: today-not-done keeps streak",
          planner.training_streak(log2, "2026-07-21") == 3)
    # a gap breaks it
    log3 = [{"day": "2026-07-15"}, {"day": "2026-07-20"}]
    check("training: gap breaks streak", planner.training_streak(log3, "2026-07-20") == 1)
    check("training: no log -> 0", planner.training_streak([], "2026-07-20") == 0)


def test_history():
    acts = [{"day": "2026-07-01", "person": "a", "kind": "contact"},
            {"day": "2026-07-15", "person": "b", "kind": "contact"},
            {"day": "2026-05-10", "person": "a", "kind": "contact"}]
    h = planner.month_history(acts, months=6, today="2026-07-20")
    check("history: 6 buckets", len(h) == 6)
    check("history: july=2", h[-1]["month"] == "2026-07" and h[-1]["count"] == 2)
    check("history: may=1", any(x["month"] == "2026-05" and x["count"] == 1 for x in h))


# ── grocery split ─────────────────────────────────────────────────────────
def test_grocery():
    library = {
        "Meal1": [{"item": "beef", "category": "meat", "qty": "1 lb"},
                  {"item": "lettuce", "category": "produce", "qty": "1"}],
        "Meal2": [{"item": "beef", "category": "meat", "qty": "2 lb"},
                  {"item": "soap", "category": "household", "qty": "1"}],
    }
    items = planner.grocery_list(["Meal1", "Meal2"], library)
    beef = next(i for i in items if i["item"] == "beef")
    check("grocery: combines duplicate item", len(beef["qtys"]) == 2)

    stores = [{"id": "a", "name": "Butcher", "area": "Monroe", "categories": ["meat"]},
              {"id": "b", "name": "Fred Meyer", "area": "Monroe", "categories": ["produce"]}]
    trips = planner.split_by_store(items, stores)
    names = {t["store"]["name"]: [i["item"] for i in t["items"]] for t in trips}
    check("grocery: meat -> butcher", "beef" in names.get("Butcher", []))
    check("grocery: produce -> fred meyer", "lettuce" in names.get("Fred Meyer", []))
    check("grocery: leftover -> Anywhere", "soap" in names.get("Anywhere", []))


# ── free windows ──────────────────────────────────────────────────────────
def test_windows():
    day = datetime(2026, 7, 20, 0, 0)  # a Monday
    ws = datetime(2026, 7, 20, 17, 0)
    we = datetime(2026, 7, 20, 21, 0)
    busy = [(datetime(2026, 7, 20, 18, 0), datetime(2026, 7, 20, 19, 0))]
    free = planner.subtract_busy(ws, we, busy)
    check("windows: busy splits window in two", len(free) == 2)
    check("windows: first free 17-18", free[0] == (ws, datetime(2026, 7, 20, 18, 0)))

    a_free = [(datetime(2026, 7, 20, 17, 0), datetime(2026, 7, 20, 20, 0))]
    b_free = [(datetime(2026, 7, 20, 18, 0), datetime(2026, 7, 20, 21, 0))]
    both = planner.intersect(a_free, b_free, min_minutes=45)
    check("windows: intersect 18-20", both == [(datetime(2026, 7, 20, 18, 0),
                                                datetime(2026, 7, 20, 20, 0))])

    tiny = planner.intersect(
        [(datetime(2026, 7, 20, 17, 0), datetime(2026, 7, 20, 17, 30))],
        [(datetime(2026, 7, 20, 17, 0), datetime(2026, 7, 20, 17, 30))],
        min_minutes=45)
    check("windows: drops too-short overlap", tiny == [])

    cfg = {"free_windows": {"weekday": ["17:00", "21:00"],
                            "weekend": ["09:00", "21:00"]}}
    sw = planner.shared_windows(cfg, [], [], start_day=day.date(), days=1)
    check("windows: whole window free when no busy", len(sw) == 1 and sw[0]["minutes"] == 240)


# ── outings ───────────────────────────────────────────────────────────────
def test_outings():
    places = [
        {"id": "1", "name": "Park", "type": "park", "last_visited": ""},          # never
        {"id": "2", "name": "Store", "type": "store", "last_visited": "2026-07-19"},  # recent
        {"id": "3", "name": "Event", "type": "event", "last_visited": "2026-01-01"},   # stale
    ]
    windows = [{"day": "2026-07-20", "start": "2026-07-20T17:00:00",
                "end": "2026-07-20T19:00:00", "minutes": 120}]
    sugg = planner.suggest_outings(places, windows, today="2026-07-20", limit=3)
    check("outings: never-visited ranked first", sugg[0]["place"]["name"] == "Park")
    check("outings: recent ranked last", sugg[-1]["place"]["name"] == "Store")
    check("outings: top gets the window", sugg[0]["window"] is not None)


def main():
    print("RHYTHM TEST GATE")
    for fn in (test_store, test_rollup, test_training, test_history,
               test_grocery, test_windows, test_outings):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{'='*40}\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
