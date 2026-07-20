"""
RHYTHM — THE BRAINS (pure logic, no network, fully testable)

Four jobs:
  1. roll up the activity log into daily / monthly numbers
  2. turn a week of meals into a grocery list split across stores
  3. do the free-window math (subtract busy blocks, keep daytime windows)
  4. suggest outings that fit a free window and haven't been done lately

All functions take plain data in and give plain data out so the tests and
the web layer share the same truth.
"""

from datetime import date, datetime, timedelta

KINDS = ["conversation", "interaction", "contact"]
KIND_LABEL = {
    "conversation": "Conversations",
    "interaction": "Interactions",
    "contact": "New contacts",
}


# ── 1. ACTIVITY ROLLUPS ───────────────────────────────────────────────────
def _month_key(day):
    return day[:7]  # "YYYY-MM"


def rollup(activity, today, goal=0):
    """
    Summarize the activity log.
    `today` is a "YYYY-MM-DD" string. Returns counts for today and the
    current month, per-person and per-kind, plus goal progress.
    """
    month = _month_key(today)
    out = {
        "today": {"a": _zero(), "b": _zero(), "total": _zero()},
        "month": {"a": _zero(), "b": _zero(), "total": _zero()},
        "month_key": month,
        "goal": goal,
    }
    for row in activity:
        kind = row.get("kind")
        if kind not in KINDS:
            continue
        person = "a" if row.get("person") == "a" else "b"
        day = row.get("day", "")
        if day == today:
            out["today"][person][kind] += 1
            out["today"]["total"][kind] += 1
        if _month_key(day) == month:
            out["month"][person][kind] += 1
            out["month"]["total"][kind] += 1
    for scope in ("today", "month"):
        for who in ("a", "b", "total"):
            d = out[scope][who]
            d["all"] = d["conversation"] + d["interaction"] + d["contact"]
    tot = out["month"]["total"]["all"]
    out["goal_pct"] = round(100 * tot / goal) if goal else 0
    out["goal_hit"] = goal and tot >= goal
    return out


def _zero():
    return {"conversation": 0, "interaction": 0, "contact": 0}


def month_history(activity, months=6, today=None):
    """Totals per month for the trailing N months, oldest first."""
    if today is None:
        today = date.today().isoformat()
    cur = datetime.strptime(today[:7] + "-01", "%Y-%m-%d").date()
    keys = []
    y, m = cur.year, cur.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    totals = {k: 0 for k in keys}
    for row in activity:
        if row.get("kind") not in KINDS:
            continue
        mk = _month_key(row.get("day", ""))
        if mk in totals:
            totals[mk] += 1
    return [{"month": k, "count": totals[k]} for k in keys]


# ── 1b. DAILY PRODUCT TRAINING ────────────────────────────────────────────
def _day_index(day):
    """Whole days since a fixed epoch — a stable, ever-increasing counter."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return (d - date(2000, 1, 1)).days


def product_of_day(products, day=None):
    """
    Pick one product for the given day, rotating through the whole library so
    every product comes up before any repeats. Same product for both spouses.
    """
    if not products:
        return None
    if day is None:
        day = date.today().isoformat()
    return products[_day_index(day) % len(products)]


def trained_today(training_log, day, product_id):
    return any(r.get("day") == day and r.get("product_id") == product_id
               for r in training_log)


def training_streak(training_log, today=None):
    """
    Count consecutive days (ending today or yesterday) with at least one
    training logged. Today not-yet-done doesn't break a streak until tomorrow.
    """
    if today is None:
        today = date.today().isoformat()
    days = {r.get("day") for r in training_log if r.get("day")}
    cur = datetime.strptime(today, "%Y-%m-%d").date()
    # if today isn't done yet, start counting from yesterday
    if cur.isoformat() not in days:
        cur = cur - timedelta(days=1)
    streak = 0
    while cur.isoformat() in days:
        streak += 1
        cur = cur - timedelta(days=1)
    return streak


# ── 2. GROCERY SPLIT ──────────────────────────────────────────────────────
def grocery_list(meal_names, meal_library):
    """Collect + combine ingredients for the chosen meals."""
    items = {}
    for name in meal_names:
        for ing in meal_library.get(name, []):
            key = ing["item"].lower()
            if key in items:
                items[key]["qtys"].append(ing.get("qty", ""))
            else:
                items[key] = {
                    "item": ing["item"],
                    "category": ing.get("category", "pantry"),
                    "qtys": [ing.get("qty", "")],
                }
    return list(items.values())


def split_by_store(items, stores):
    """
    Assign each ingredient to a store that carries its category. If no store
    lists that category, it lands in a leftover 'Anywhere' bucket. Deliberately
    spreads the trip out — more stops = more time out and about.
    """
    buckets = {s["id"]: {"store": s, "items": []} for s in stores}
    leftover = []
    for it in items:
        placed = False
        for s in stores:
            if it["category"] in s.get("categories", []):
                buckets[s["id"]]["items"].append(it)
                placed = True
                break
        if not placed:
            leftover.append(it)
    trips = [b for b in buckets.values() if b["items"]]
    if leftover:
        trips.append({"store": {"id": "any", "name": "Anywhere", "area": ""},
                      "items": leftover})
    return trips


# ── 3. FREE-WINDOW MATH ───────────────────────────────────────────────────
def _parse(dt):
    return datetime.fromisoformat(dt) if isinstance(dt, str) else dt


def subtract_busy(win_start, win_end, busy):
    """
    Given one window [start,end) and a list of busy (start,end) intervals,
    return the free sub-intervals. Everything is datetime.
    """
    frees = [(win_start, win_end)]
    for b_start, b_end in sorted(busy, key=lambda x: x[0]):
        b_start, b_end = _parse(b_start), _parse(b_end)
        new = []
        for f_start, f_end in frees:
            if b_end <= f_start or b_start >= f_end:
                new.append((f_start, f_end))       # no overlap
                continue
            if b_start > f_start:
                new.append((f_start, min(b_start, f_end)))
            if b_end < f_end:
                new.append((max(b_end, f_start), f_end))
        frees = new
    return frees


def intersect(a_free, b_free, min_minutes=45):
    """Windows where BOTH are free, at least min_minutes long."""
    out = []
    for a_s, a_e in a_free:
        for b_s, b_e in b_free:
            s, e = max(a_s, b_s), min(a_e, b_e)
            if (e - s) >= timedelta(minutes=min_minutes):
                out.append((s, e))
    out.sort()
    return out


def shared_windows(config, busy_a, busy_b, start_day=None, days=7):
    """
    For the next `days`, build each day's "willing to be out" window from
    config.free_windows, subtract each person's busy blocks, and return the
    slots where they're both free.
    """
    if start_day is None:
        start_day = date.today()
    fw = config.get("free_windows", {})
    results = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        is_weekend = d.weekday() >= 5
        span = fw.get("weekend" if is_weekend else "weekday", ["17:00", "21:00"])
        w_start = datetime.combine(d, _time(span[0]))
        w_end = datetime.combine(d, _time(span[1]))
        a_free = subtract_busy(w_start, w_end, busy_a)
        b_free = subtract_busy(w_start, w_end, busy_b)
        both = intersect(a_free, b_free)
        for s, e in both:
            results.append({"day": d.isoformat(), "start": s.isoformat(),
                            "end": e.isoformat(),
                            "minutes": int((e - s).total_seconds() // 60)})
    return results


def _time(hhmm):
    h, m = hhmm.split(":")
    return datetime.min.time().replace(hour=int(h), minute=int(m))


# ── 4. OUTING SUGGESTIONS ─────────────────────────────────────────────────
def suggest_outings(places, windows, today=None, limit=3):
    """
    Rank places by how long since we last visited (never-visited first),
    and pair the top ones with upcoming free windows.
    """
    if today is None:
        today = date.today().isoformat()

    def staleness(p):
        lv = p.get("last_visited", "")
        if not lv:
            return 10_000            # never visited → most stale
        try:
            return (datetime.strptime(today, "%Y-%m-%d")
                    - datetime.strptime(lv, "%Y-%m-%d")).days
        except ValueError:
            return 10_000

    ranked = sorted(places, key=staleness, reverse=True)
    suggestions = []
    for i, place in enumerate(ranked[:limit]):
        win = windows[i] if i < len(windows) else None
        suggestions.append({"place": place, "window": win,
                            "stale_days": staleness(place)})
    return suggestions
