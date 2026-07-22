"""
SAMMAMISH LIGHTS — 3 EFFECTIVE ROUTES (Dallon, Jul 22: "sammamish into
3 effective routes. keep iron mans close, but use our geomapping data",
per the office's 2026 planning sheet: Optimus Prime = Trossachs/East/
Duvall · Flash-Connor = Duthie Hill/244th · Captain America-Nicholas =
Castle Pines/Main/West. Iron Man = Dallon's own north corridor —
Bothell/Monroe/Woodinville/Snohomish — kept tight and untouched for
whoever inherits it).

Method: ACTIVE homes only (an install line since Aug 2025), balanced
k=3 clustering on real lat/lng (equal-ish install counts, geographic
contiguity), clusters then named by which of the sheet's anchor
neighborhoods they contain. Writes the samm_routes blob the
/lightroutes page draws. Pure stdlib; deterministic.
"""

import json
import math
import re
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent

# the sheet's three Sammamish territories, with geographic anchors
ROUTE_DEFS = [
    {"key": "trossachs", "name": "Route 1 — Trossachs · East · Duvall",
     "tech": "Optimus Prime (new tech — Gavin?)", "color": "#8e44ad",
     "anchor": (47.5787, -121.9773)},      # Trossachs Blvd SE
    {"key": "duthie", "name": "Route 2 — Duthie Hill · 244th",
     "tech": "Flash (Connor)", "color": "#c0392b",
     "anchor": (47.6046, -122.0053)},      # Duthie Hill / 244th Ave SE
    {"key": "castle", "name": "Route 3 — Castle Pines · Main · West",
     "tech": "Captain America (Nicholas)", "color": "#1a6b3c",
     "anchor": (47.6360, -122.0570)},      # north/west plateau
]
IRONMAN_CITIES = ("bothell", "monroe", "woodinville", "snohomish",
                  "sultan")


def _nkey(n):
    return re.sub(r"[^a-z ]", "", (n or "").lower()).strip()


def _dist(a, b):
    # local-flat miles: fine at city scale
    dx = (a[1] - b[1]) * 69.0 * math.cos(math.radians(47.6))
    dy = (a[0] - b[0]) * 69.0
    return math.hypot(dx, dy)


def build():
    cal = json.loads((BASE / "data" / "lights_calibration.json")
                     .read_text())
    latest = defaultdict(str)
    for li in cal.get("all_lines") or []:
        c = _nkey(li.get("client"))
        if c:
            latest[c] = max(latest[c], (li.get("date") or "")[:10])
    active = {c for c, d in latest.items() if d >= "2025-08"}

    homes = json.loads((BASE / "data" / "lights_homes.json").read_text())
    samm = [h for h in homes
            if (h.get("city") or "").lower() == "sammamish"
            and _nkey(h.get("client")) in active
            and h.get("lat") and h.get("lng")]
    iron = [h for h in homes
            if (h.get("city") or "").lower() in IRONMAN_CITIES
            and _nkey(h.get("client")) in active
            and h.get("lat") and h.get("lng")]

    # seed on the neighborhood anchors, run k-means to convergence,
    # then rebalance: no route may exceed ceil(n/3)+tol — boundary
    # homes move to their next-nearest under-full route
    cents = [d["anchor"] for d in ROUTE_DEFS]
    assign = [0] * len(samm)
    for _ in range(60):
        moved = False
        for i, h in enumerate(samm):
            j = min(range(3), key=lambda k: _dist(
                (h["lat"], h["lng"]), cents[k]))
            if j != assign[i]:
                assign[i] = j
                moved = True
        for k in range(3):
            pts = [(samm[i]["lat"], samm[i]["lng"])
                   for i in range(len(samm)) if assign[i] == k]
            if pts:
                cents[k] = (sum(p[0] for p in pts) / len(pts),
                            sum(p[1] for p in pts) / len(pts))
        if not moved:
            break
    cap = -(-len(samm) // 3) + 8            # ceil + tolerance
    for _ in range(300):
        sizes = [assign.count(k) for k in range(3)]
        over = [k for k in range(3) if sizes[k] > cap]
        if not over:
            break
        k = over[0]
        # the member farthest from its centroid moves to the nearest
        # under-full route
        members = [i for i in range(len(samm)) if assign[i] == k]
        far = max(members, key=lambda i: _dist(
            (samm[i]["lat"], samm[i]["lng"]), cents[k]))
        others = sorted((kk for kk in range(3)
                         if kk != k and sizes[kk] < cap),
                        key=lambda kk: _dist(
                            (samm[far]["lat"], samm[far]["lng"]),
                            cents[kk]))
        if not others:
            break
        assign[far] = others[0]

    routes = []
    for k, d in enumerate(ROUTE_DEFS):
        hs = [samm[i] for i in range(len(samm)) if assign[i] == k]
        hs.sort(key=lambda h: (h["lat"], h["lng"]))
        spread = max((_dist((a["lat"], a["lng"]), cents[k])
                      for a in hs), default=0)
        routes.append({
            "name": d["name"], "tech": d["tech"], "color": d["color"],
            "count": len(hs), "center": cents[k],
            "max_mi_from_center": round(spread, 1),
            "homes": [{"client": h["client"], "address": h["address"],
                       "lat": h["lat"], "lng": h["lng"],
                       "bulb": h.get("bulb"),
                       "combo": h.get("color_combo")} for h in hs]})

    ic = ((sum(h["lat"] for h in iron) / len(iron)),
          (sum(h["lng"] for h in iron) / len(iron))) if iron else None
    ir_spread = max((_dist((h["lat"], h["lng"]), ic) for h in iron),
                    default=0) if ic else 0
    out = {"routes": routes,
           "sammamish_active": len(samm),
           "ironman": {
               "tech": "Iron Man (Dallon — Nick K to inherit?)",
               "cities": "Bothell · Monroe · Woodinville · Snohomish",
               "count": len(iron), "center": ic,
               "max_mi_from_center": round(ir_spread, 1),
               "points": [[h["lat"], h["lng"]] for h in iron]}}
    return out


if __name__ == "__main__":
    o = build()
    print(f"active Sammamish homes: {o['sammamish_active']}")
    for r in o["routes"]:
        print(f"  {r['name']:44s} {r['count']:3d} homes · "
              f"≤{r['max_mi_from_center']} mi from center · {r['tech']}")
    i = o["ironman"]
    print(f"  Iron Man corridor: {i['count']} homes · "
          f"≤{i['max_mi_from_center']} mi from center (untouched)")
    (BASE / "data" / "samm_routes.json").write_text(json.dumps(o))
    print("→ data/samm_routes.json")
