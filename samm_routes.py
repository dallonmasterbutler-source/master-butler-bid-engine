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
        if key == "ironman":
            # NINE ROUTES (Dallon, Jul 22: "planning for 9 routes this
            # year") — the north corridor is 2x everyone else's load
            # (574 homes vs ~200 targets), so it splits into two
            # balanced halves; the office picks who takes each.
            c2 = [(47.86, -122.09), (47.83, -121.95)]   # west / east
            a2 = [0] * len(hs)
            for _ in range(60):
                mv = False
                for i, h in enumerate(hs):
                    j = min((0, 1), key=lambda k: _dist(
                        (h["lat"], h["lng"]), c2[k]))
                    if j != a2[i]:
                        a2[i] = j
                        mv = True
                for k in (0, 1):
                    p2 = [(hs[i]["lat"], hs[i]["lng"])
                          for i in range(len(hs)) if a2[i] == k]
                    if p2:
                        c2[k] = (sum(x[0] for x in p2) / len(p2),
                                 sum(x[1] for x in p2) / len(p2))
                if not mv:
                    break
            cap2 = -(-len(hs) // 2) + 12
            for _ in range(400):
                s2 = [a2.count(0), a2.count(1)]
                if max(s2) <= cap2:
                    break
                k = 0 if s2[0] > s2[1] else 1
                mem = [i for i in range(len(hs)) if a2[i] == k]
                far = max(mem, key=lambda i: _dist(
                    (hs[i]["lat"], hs[i]["lng"]), c2[k]))
                a2[far] = 1 - k
            routes.append(_route(
                "Iron Man A — west (Bothell · Woodinville · Kenmore)",
                "Dallon / Nick K? (office picks)", "#b8860b",
                [hs[i] for i in range(len(hs)) if a2[i] == 0]))
            routes.append(_route(
                "Iron Man B — east (Monroe · Snohomish · Sultan)",
                "Yoda-Tom floats here? (office picks)", "#8a5a00",
                [hs[i] for i in range(len(hs)) if a2[i] == 1]))
            continue
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


# ── SEASON MOCK SCHEDULE (Dallon, Jul 22: "use last years routes as
# the standard, using past data as a helper... give me a mock schedule
# for jessica"). Each ACTIVE home lands in the SAME season-week it was
# installed last year (customers expect their date); undated homes join
# their route's median week. Capacity: 8 installs/tech/day, Mon-Sat in
# Oct-Nov (the sheet's Saturday shifts), Mon-Fri otherwise. Overflow
# spills into the next open day and is flagged for Jessica.

from datetime import date as _date, timedelta as _td


def _season_dates(all_lines):
    out = {}
    for li in all_lines:
        if (li.get("kind") or "") == "takedown":
            continue
        c = _nkey(li.get("client"))
        d = (li.get("date") or "")[:10]
        if c and "2025-08" <= d <= "2026-03":
            out[c] = max(out.get(c, ""), d)
    return out


def build_schedule(routes_blob):
    """JESSICA'S DAY MODEL (Jul 22, via Dallon): 10 homes a day planned
    Mon-Thu; FRIDAY STAYS OPEN as backup/standby — no-confirms can't
    overload a day, and a fully-confirmed week uses Friday to absorb
    the extra. A week therefore fits 40 planned (+10 on Friday when
    needed); demand beyond 50 pushes into the next week and is flagged
    RED for a helper."""
    cal = json.loads((BASE / "data" / "lights_calibration.json")
                     .read_text())
    when = _season_dates(cal.get("all_lines") or [])
    sched = {"routes": [], "cap_per_day": 10,
             "model": "Mon-Thu planned, Friday backup (Jessica)"}
    for r in routes_blob["routes"]:
        actives = [h for h in r["homes"] if h.get("active")]
        anchored = []
        undated = []
        for h in actives:
            d = when.get(_nkey(h["client"]))
            if d:
                try:
                    d0 = _date.fromisoformat(d)
                    tgt = d0.replace(year=d0.year + 1)
                except ValueError:
                    tgt = _date.fromisoformat(d) + _td(days=365)
                anchored.append((tgt, h))
            else:
                undated.append(h)
        anchored.sort(key=lambda t: t[0])
        med = (anchored[len(anchored) // 2][0] if anchored
               else _date(2026, 10, 15))
        for h in undated:
            anchored.append((med, h))
        anchored.sort(key=lambda t: t[0])

        # group demand by anchor week, then fill week by week with a
        # carried backlog: 4 planned days x 10, then Friday x 10
        bywk = {}
        for tgt, h in anchored:
            wk = tgt - _td(days=tgt.weekday())
            bywk.setdefault(wk, []).append(h)
        weeks_out = []
        days = {}
        day_detail = {}
        backlog = []
        wk = min(bywk) if bywk else None
        last_wk = max(bywk) if bywk else None
        while wk and (backlog or wk <= last_wk):
            queue = backlog + bywk.get(wk, [])
            backlog = []
            # GEOGRAPHIC DAY PODS (Dallon, Jul 22: "i want to see how
            # you route this") — a day is a tight neighborhood pod, not
            # "whoever was anchored that date": seed each pod on the
            # home farthest from the remaining crowd, then take its 9
            # nearest neighbors. Mon-Thu get the four pods; a 5th pod
            # rides the backup Friday; more than that carries forward.
            pods = []
            rest = queue[:]
            while rest:
                cx = (sum(h["lat"] for h in rest) / len(rest),
                      sum(h["lng"] for h in rest) / len(rest))
                seed = max(rest, key=lambda h: _dist(
                    (h["lat"], h["lng"]), cx))
                rest.remove(seed)
                pod = [seed]
                while len(pod) < 10 and rest:
                    last = pod[-1]
                    nxt = min(rest, key=lambda h: _dist(
                        (last["lat"], last["lng"]),
                        (h["lat"], h["lng"])))
                    rest.remove(nxt)
                    pod.append(nxt)
                pods.append(pod)
            planned = fri = 0
            for pi, pod in enumerate(pods):
                if pi < 4:
                    day = wk + _td(days=pi)
                    planned += len(pod)
                elif pi == 4:
                    day = wk + _td(days=4)
                    fri += len(pod)
                else:
                    backlog.extend(pod)
                    continue
                # chain the pod nearest-neighbor and measure the drive
                chain = [pod[0]]
                pool = pod[1:]
                while pool:
                    last = chain[-1]
                    nxt = min(pool, key=lambda h: _dist(
                        (last["lat"], last["lng"]),
                        (h["lat"], h["lng"])))
                    pool.remove(nxt)
                    chain.append(nxt)
                mi = sum(_dist((chain[i]["lat"], chain[i]["lng"]),
                               (chain[i + 1]["lat"],
                                chain[i + 1]["lng"]))
                         for i in range(len(chain) - 1))
                days.setdefault(day, []).extend(chain)
                day_detail[day] = {"miles": round(mi, 1),
                                   "stops": chain}
            if planned or fri or backlog:
                weeks_out.append([wk.isoformat(), planned, fri,
                                  len(backlog)])
            wk += _td(days=7)
        busiest = max(days, key=lambda d: len(days[d])) if days else None
        sample = None
        if busiest:
            stops = days[busiest][:]
            chain = [stops.pop(0)]
            while stops:
                last = chain[-1]
                nxt = min(stops, key=lambda h: _dist(
                    (last["lat"], last["lng"]), (h["lat"], h["lng"])))
                stops.remove(nxt)
                chain.append(nxt)
            t = 8 * 60
            ss = []
            for h in chain:
                ss.append({"arrive": f"{t // 60}:{t % 60:02d}",
                           "client": h["client"],
                           "address": h["address"]})
                t += 40 + 7          # 10/day pace: 40 min + short hop
            sample = {"date": busiest.isoformat(),
                      "label": busiest.strftime("%a %b %d"),
                      "stops": ss,
                      "done_by": f"{t // 60}:{t % 60:02d}"}
        _daybook = []
        for d in sorted(day_detail):
            dd = day_detail[d]
            t = 8 * 60
            st = []
            for h in dd["stops"]:
                st.append({"arrive": f"{t // 60}:{t % 60:02d}",
                           "client": h["client"],
                           "address": h["address"],
                           "lat": h["lat"], "lng": h["lng"]})
                t += 40 + 7
            _daybook.append({"date": d.isoformat(),
                             "label": d.strftime("%a %b %d"),
                             "friday": d.weekday() == 4,
                             "miles": dd["miles"],
                             "done_by": f"{t // 60}:{t % 60:02d}",
                             "stops": st})
        sched["routes"].append({
            "name": r["name"], "color": r["color"], "tech": r["tech"],
            "active": len(actives),
            "daybook": _daybook,
            "weeks": weeks_out,
            "first_day": min(days).isoformat() if days else None,
            "last_day": max(days).isoformat() if days else None,
            "workdays": len(days),
            "fridays_used": sum(1 for d in days if d.weekday() == 4),
            "sample_day": sample})
    return sched
