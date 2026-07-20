"""
MASTER BUTLER — SCHEDULING STAGE 1: READ THE CALENDAR, LEARN THE ROUTES
(Dallon, Jul 14: "run this report tonight … include holiday lights …
teach the program the maximum that you can about scheduling.")

READ-ONLY. Pulls every Jobber visit from July 2025 → 60 days ahead,
geocodes through the shared cache, reconstructs every real workday the
company ran, and distills the patterns into the `sched_knowledge` blob
— the seed the Area-Days scheduler (stage 2+) will price dates against.

What it learns:
  · jobs/day by month + weekday (the 2wk/6wk seasonal rhythm, measured)
  · drive gaps between consecutive stops (est. road minutes via
    haversine ×1.35 at 40 km/h — good for LEARNING; live offers will
    use the real Routes API) and how often days break the 15–20 min rule
  · which CITIES pair with which WEEKDAYS (LaRee's area-day habits)
  · per-tech day shapes (Tom's dry-week days included)
  · HOLIDAY LIGHTS as its own season: install vs takedown timeline,
    lights jobs/day, lights areas — the rotation, measured
  · the NEXT-60-DAYS anchor map: which future days already have a
    geographic center of gravity (stage 2 feeds on this directly)
"""

import collections
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent

VQ = """
query Sweep($start: ISO8601DateTime!, $end: ISO8601DateTime!, $after: String) {
  visits(first: 50, after: $after,
         filter: {startAt: {after: $start, before: $end}}) {
    pageInfo { hasNextPage endCursor }
    nodes { title instructions startAt endAt duration isComplete
      assignedUsers(first: 3) { nodes { name { full } } }
      client { name }
      lineItems(first: 6) { nodes { name } }
      property { address { street city postalCode } } }
  }
}"""

LIGHT_RX = re.compile(r"light", re.I)
TAKEDOWN_RX = re.compile(r"take ?down|removal|remove", re.I)
# THE OFFICE'S TITLE CODES (Dallon's teaching, Jul 14):
#   ccc = return customer · LLL = customer's lights live in OUR shop
#   (assigned tech grabs them at the Monday meeting) · c7/c9 = bulb type
CCC_RX = re.compile(r"\bccc\b", re.I)
LLL_RX = re.compile(r"\blll\b", re.I)
BULB_RX = re.compile(r"\bc([79])\b", re.I)


def _save_blob(name, val):
    """Cloud direct when possible; HTTPS courier from the Mac
    (night_run's system python has no psycopg — this is the path
    that kept Jul 14's mine from stranding again)."""
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob(name, val)
            return True
    except Exception:
        pass
    try:
        from cloudpush import push
        push(blobs={name: val})
        return True
    except Exception:
        return False


def _est_minutes(a, b):
    """Haversine km → estimated road minutes (×1.35 road factor,
    40 km/h suburban average)."""
    lat1, lng1, lat2, lng2 = map(math.radians,
                                 (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2)
         * math.sin((lng2 - lng1) / 2) ** 2)
    km = 6371 * 2 * math.asin(math.sqrt(h))
    return km * 1.35 / 40 * 60


def fetch_all(verbose=False):
    import jobber_client as jc
    # back to 2023 (Dallon, Jul 14): one year holds only ONE lights
    # season — route continuity needs several. 2023→now = 3 cycles.
    start = "2023-01-01T08:00:00Z"
    end = (datetime.now(timezone.utc) + timedelta(days=60)) \
        .strftime("%Y-%m-%dT07:00:00Z")
    was, jc.DRY_RUN = jc.DRY_RUN, False
    visits, after, throttles = [], None, 0
    try:
        while True:
            d = jc._post(VQ, {"start": start, "end": end, "after": after},
                         "sched mine (read-only)")
            if d.get("error"):
                if "Throttled" in str(d) and throttles < 60:
                    throttles += 1
                    time.sleep(60)
                    continue
                break
            page = d.get("visits") or {}
            for n in page.get("nodes") or []:
                a = (n.get("property") or {}).get("address") or {}
                addr = ", ".join(x for x in (a.get("street"),
                                             a.get("city"),
                                             a.get("postalCode")) if x)
                if not addr or not n.get("startAt"):
                    continue
                lines = [(li.get("name") or "") for li in
                         ((n.get("lineItems") or {}).get("nodes") or [])]
                visits.append({
                    "client": ((n.get("client") or {}).get("name")
                               or "")[:40],
                    "start": n["startAt"], "end": n.get("endAt"),
                    "dur": n.get("duration"),
                    "title": (n.get("title") or "")[:80],
                    "instr": (n.get("instructions") or "")[:300],
                    "lines": lines[:6],
                    "done": bool(n.get("isComplete")),
                    "techs": [u["name"]["full"] for u in
                              (n.get("assignedUsers") or {})
                              .get("nodes", [])],
                    "address": addr, "city": (a.get("city") or "?")
                    .title()})
            if not page.get("pageInfo", {}).get("hasNextPage"):
                break
            after = page["pageInfo"]["endCursor"]
            time.sleep(3)
    finally:
        jc.DRY_RUN = was
    if verbose:
        print(f"fetched {len(visits)} visits ({throttles} throttle "
              f"waits)")
    return visits


def analyze(visits, verbose=False):
    from routing import geocode_many
    geocode_many(visits)                 # fills lat/lng via shared cache

    def _local_day(iso):                 # UTC → Pacific-ish day
        t = datetime.fromisoformat(iso.rstrip("Z")) - timedelta(hours=7)
        return t.date()

    days = collections.defaultdict(list)
    for v in visits:
        v["is_light"] = bool(LIGHT_RX.search(
            v["title"] + " " + " ".join(v["lines"])))
        v["is_takedown"] = v["is_light"] and bool(TAKEDOWN_RX.search(
            v["title"] + " " + " ".join(v["lines"])))
        days[_local_day(v["start"])].append(v)

    today = datetime.now(timezone.utc).date()
    K = {"jobs_per_day_by_month": {}, "weekday_city": {},
         "drive_gaps": {}, "techs": {}, "lights": {},
         "future_anchors": {}, "totals": {}}
    permonth = collections.defaultdict(list)
    wd_city = collections.defaultdict(collections.Counter)
    gaps, over20, pairs = [], 0, 0
    tech_days = collections.defaultdict(list)
    li_month = collections.defaultdict(lambda: [0, 0])   # install, takedown
    light_day_sizes, light_cities = [], collections.Counter()
    codes = collections.Counter()
    lll_future = []                      # Monday-meeting shop-grab list
    route_cont = collections.defaultdict(set)   # lights client → techs
                                                # across seasons

    for d, vs in sorted(days.items()):
        past = d <= today
        if past:
            permonth[d.strftime("%Y-%m")].append(len(vs))
            for v in vs:
                wd_city[d.strftime("%a")][v["city"]] += 1
            # consecutive-stop gaps per tech-team, ordered by start time
            byteam = collections.defaultdict(list)
            for v in vs:
                byteam[tuple(sorted(v["techs"])) or ("?",)].append(v)
            for team, tvs in byteam.items():
                tvs.sort(key=lambda x: x["start"])
                tech_days[team[0] if team else "?"].append(len(tvs))
                for i in range(1, len(tvs)):
                    a, b = tvs[i - 1], tvs[i]
                    if "lat" in a and "lat" in b:
                        m = _est_minutes((a["lat"], a["lng"]),
                                         (b["lat"], b["lng"]))
                        if m < 90:       # ignore cross-day artifacts
                            gaps.append(m)
                            pairs += 1
                            if m > 20:
                                over20 += 1
            lts = [v for v in vs if v["is_light"]]
            if lts:
                light_day_sizes.append(len(lts))
                for v in lts:
                    light_cities[v["city"]] += 1
                    li_month[d.strftime("%Y-%m")][
                        1 if v["is_takedown"] else 0] += 1
                    t = v["title"]
                    if CCC_RX.search(t):
                        codes["ccc (return customer)"] += 1
                    if LLL_RX.search(t):
                        codes["LLL (lights in our shop)"] += 1
                    m = BULB_RX.search(t)
                    if m:
                        codes[f"c{m.group(1)} bulbs"] += 1
                    # route continuity: who has installed for this
                    # client across seasons (goal: SAME tech = faster)
                    cl = (v.get("client") or v["title"].split("-")[0]
                          ).strip().lower()[:40]
                    for tech in v["techs"]:
                        route_cont[cl].add(tech)
        else:
            # FUTURE LLL visits = the Monday-meeting shop-grab list
            for v in vs:
                if v["is_light"] and LLL_RX.search(v["title"]):
                    lll_future.append({
                        "date": d.isoformat(),
                        "client": v.get("client") or v["title"][:40],
                        "techs": v["techs"], "city": v["city"]})
            # FUTURE: the anchor map stage 2 feeds on
            cities = collections.Counter(v["city"] for v in vs)
            pts = [(v["lat"], v["lng"]) for v in vs if "lat" in v]
            centroid = ([round(sum(p[0] for p in pts) / len(pts), 4),
                         round(sum(p[1] for p in pts) / len(pts), 4)]
                        if pts else None)
            K["future_anchors"][d.isoformat()] = {
                "jobs": len(vs), "cities": dict(cities.most_common(3)),
                "centroid": centroid,
                "techs": sorted({t for v in vs for t in v["techs"]})[:4]}

    for mo, counts in sorted(permonth.items()):
        K["jobs_per_day_by_month"][mo] = {
            "workdays": len(counts), "total_jobs": sum(counts),
            "avg_per_day": round(sum(counts) / len(counts), 1),
            "max_day": max(counts)}
    K["weekday_city"] = {wd: dict(c.most_common(5))
                         for wd, c in wd_city.items()}
    if gaps:
        gaps.sort()
        K["drive_gaps"] = {
            "pairs_measured": pairs,
            "median_min": round(gaps[len(gaps) // 2], 1),
            "p90_min": round(gaps[int(len(gaps) * .9)], 1),
            "share_over_20min": round(over20 / pairs, 3),
            "method": "haversine ×1.35 @40km/h (estimate)"}
    K["techs"] = {t: {"days": len(c),
                      "avg_jobs_per_day": round(sum(c) / len(c), 1)}
                  for t, c in sorted(tech_days.items(),
                                     key=lambda kv: -len(kv[1]))[:8]}
    _multi = [c for c, ts in route_cont.items() if len(ts) > 1]
    K["lights"] = {
        "by_month_install_takedown": {m: v for m, v in
                                      sorted(li_month.items())},
        "avg_lights_jobs_per_lights_day": (round(
            sum(light_day_sizes) / len(light_day_sizes), 1)
            if light_day_sizes else 0),
        "top_areas": dict(light_cities.most_common(8)),
        "title_codes_seen": dict(codes),
        # route continuity doctrine (Dallon): same tech, same route,
        # year over year — flag clients whose installer CHANGED
        "route_continuity": {
            "lights_clients_tracked": len(route_cont),
            "kept_same_tech": sum(1 for ts in route_cont.values()
                                  if len(ts) == 1),
            "tech_changed": len(_multi),
            "changed_examples": _multi[:10]},
        # whoever has LLL that week grabs the lights at Monday meeting
        "lll_shop_grab_upcoming": lll_future[:20]}
    K["totals"] = {"visits": len(visits), "workdays": len(permonth) and
                   sum(len(v) for v in [permonth]) and
                   sum(x["workdays"] for x in
                       K["jobs_per_day_by_month"].values()),
                   "mined_at": today.isoformat()}
    return K


def run(verbose=False):
    import clouddb
    visits = fetch_all(verbose=verbose)
    if not visits:
        return None
    K = analyze(visits, verbose=verbose)
    _save_blob("sched_knowledge", K)
    (BASE / "data" / "sched_mine.json").write_text(json.dumps(K, indent=1))
    # SHADOW SCORECARD: match the dates the shadow OFFERED against the days
    # the office actually booked — proof-before-power (never schedules).
    try:
        import sched_scorecard
        sched_scorecard.match(visits)
    except Exception:
        pass
    # lights homes roster → the footage measurer (Dallon, Jul 14:
    # "measure linear feet on the front … do like 100 homes")
    seen, homes = set(), {}
    _ftrx = re.compile(r"(\d{2,4})\s*(?:ft|feet|lf|linear)", re.I)
    _colrx = re.compile(r"warm white|cool white|red[/& ]?(?:green|white)?"
                        r"|green[/& ]?blue|multi ?color|blue|candy cane",
                        re.I)
    for v in visits:
        if v.get("is_light") and v.get("address") and "lat" in v:
            k2 = v["address"].lower()
            blob = " ".join([v["title"], v.get("instr") or ""]
                            + v.get("lines", []))
            h = homes.get(k2) or {"client": v.get("client"),
                                  "address": v["address"],
                                  "city": v["city"],
                                  "lat": v["lat"], "lng": v["lng"]}
            # the office writes color combo + footage in TITLES and
            # NOTES (Dallon, Jul 14) — capture the per-home lights spec
            m = _ftrx.search(blob)
            if m and not h.get("noted_ft"):
                h["noted_ft"] = int(m.group(1))
            c = _colrx.search(blob)
            if c and not h.get("color_combo"):
                h["color_combo"] = c.group(0).lower()
            bm = re.search(r"\bc([79])\b", blob, re.I)
            if bm:
                h["bulb"] = f"c{bm.group(1)}"
            homes[k2] = h
    homes = list(homes.values())
    (BASE / "data" / "lights_homes.json").write_text(
        json.dumps(homes, indent=1))
    if verbose:
        print(json.dumps(K["drive_gaps"], indent=1))
        print("months:", len(K["jobs_per_day_by_month"]),
              "· future anchor days:", len(K["future_anchors"]))
    return K


if __name__ == "__main__":
    run(verbose=True)
