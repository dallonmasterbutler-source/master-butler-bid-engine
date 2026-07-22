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


def _territory_of(city, lat, lng):
    """City -> superhero territory per the office's 2026 sheet.
    Sammamish is handled separately (3-way split). South-of-I-90
    Bellevue-side homes ride Spiderman's S-I90 block."""
    c = (city or "").lower()
    if c in ("bothell", "monroe", "sultan", "woodinville", "snohomish",
             "gold bar", "mill creek", "kenmore", "lynnwood", "edmonds",
             "everett", "lake stevens", "lake forest park"):
        return "ironman"
    if c in ("redmond",):
        return "superman"
    if c in ("kirkland",):
        return "batman"
    if c in ("bellevue", "medina", "clyde hill", "yarrow point",
             "mercer island", "newcastle", "renton"):
        return "spiderman"
    if c in ("issaquah", "snoqualmie", "north bend", "fall city",
             "carnation"):
        return "wolverine"
    if c in ("duvall",):
        return "optimus"          # rides with Trossachs/East per sheet
    return "wolverine" if (lng or 0) > -122.0 else "spiderman"


TERRITORIES = {
    "ironman":  {"name": "Iron Man — North corridor",
                 "tech": "Dallon (Nick K to inherit?)",
                 "cities": "Bothell · Monroe · Woodinville · Snohomish "
                           "· north satellites", "color": "#b8860b"},
    "superman": {"name": "Superman — Redmond",
                 "tech": "Shane", "cities": "Redmond",
                 "color": "#1d4ed8"},
    "batman":   {"name": "Batman — Kirkland (+ Monroe Mainvue)",
                 "tech": "Adam", "cities": "Kirkland",
                 "color": "#0f172a"},
    "spiderman": {"name": "Spiderman — Bellevue · S-I90",
                  "tech": "Austin",
                  "cities": "Bellevue · Medina · Mercer Is. · "
                            "Newcastle · Renton", "color": "#dc2626"},
    "wolverine": {"name": "Wolverine — Issaquah · Snoqualmie valley",
                  "tech": "Mark",
                  "cities": "Issaquah · Snoqualmie · North Bend · "
                            "Fall City · Carnation", "color": "#6d28d9"},
}


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
    homes = [h for h in homes if h.get("lat") and h.get("lng")]
    # geocode sanity fence: our whole service region fits in this box —
    # anything outside is a bad geocode (one Spiderman home graded the
    # route 108 mi wide). Sidelined for the office, never routed wrong.
    bad = [h for h in homes
           if not (47.2 < h["lat"] < 48.2 and -122.6 < h["lng"] < -121.4)]
    homes = [h for h in homes if h not in bad]
    for h in homes:
        h["active"] = _nkey(h.get("client")) in active

    samm = [h for h in homes
            if (h.get("city") or "").lower() == "sammamish"]

    # ── Sammamish 3-way split (ALL homes now, not just active):
    # k-means seeded on the sheet's neighborhood anchors + rebalance
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
    cap = -(-len(samm) // 3) + 10
    for _ in range(400):
        sizes = [assign.count(k) for k in range(3)]
        over = [k for k in range(3) if sizes[k] > cap]
        if not over:
            break
        k = over[0]
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

    def _route(name, tech, color, hs, center=None):
        hs = sorted(hs, key=lambda h: (h["lat"], h["lng"]))
        if hs and not center:
            center = (sum(h["lat"] for h in hs) / len(hs),
                      sum(h["lng"] for h in hs) / len(hs))
        spread = max((_dist((h["lat"], h["lng"]), center)
                      for h in hs), default=0) if center else 0
        return {"name": name, "tech": tech, "color": color,
                "count": len(hs),
                "active": sum(1 for h in hs if h.get("active")),
                "center": center,
                "max_mi_from_center": round(spread, 1),
                "homes": [{"client": h["client"],
                           "address": h["address"],
                           "lat": h["lat"], "lng": h["lng"],
                           "bulb": h.get("bulb"),
                           "active": h.get("active", False)}
                          for h in hs]}

    routes = []
    for k, d in enumerate(ROUTE_DEFS):
        hs = [samm[i] for i in range(len(samm)) if assign[i] == k]
        if d["key"] == "trossachs":     # Duvall rides with Optimus
            hs = hs + [h for h in homes
                       if (h.get("city") or "").lower() == "duvall"]
        routes.append(_route(d["name"], d["tech"], d["color"], hs,
                             cents[k]))

    for key, t in TERRITORIES.items():
        hs = [h for h in homes
              if (h.get("city") or "").lower() not in
              ("sammamish", "duvall")
              and _territory_of(h.get("city"), h["lat"],
                                h["lng"]) == key]
        routes.append(_route(t["name"], t["tech"], t["color"], hs))

    return {"routes": routes,
            "bad_geocodes": [{"client": h["client"],
                              "address": h["address"]} for h in bad],
            "total_homes": len(homes),
            "total_active": sum(1 for h in homes if h["active"]),
            "sammamish_active": sum(1 for h in samm if h["active"])}


if __name__ == "__main__":
    o = build()
    print(f"all homes: {o['total_homes']} "
          f"({o['total_active']} active this season)")
    for r in o["routes"]:
        print(f"  {r['name']:46s} {r['count']:4d} homes "
              f"({r['active']:3d} active) · "
              f"<= {r['max_mi_from_center']} mi · {r['tech']}")
    (BASE / "data" / "samm_routes.json").write_text(json.dumps(o))
    print("-> data/samm_routes.json")
