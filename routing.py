"""
MASTER BUTLER — LIVE ROUTE BUILDER (Dallon, Jul 9 pm: "build the live
route system... and the takedown schedule the same way")

Reads the LIVING schedule straight from Jobber — visits (jobs) or
tasks (takedowns are tasks) for any date — groups by assigned tech,
geocodes (cached), and orders each tech's day with the Google Routes
API from the Monroe shop and back.

Read-only against Jobber. Writes only our own blobs:
  geocode_cache  {addr-slug: [lat, lng]}     (never pay twice)
  routes:<date>:<kind>  the computed day     (15-min freshness)
"""

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
SHOP = {"lat": 47.8557, "lng": -121.9715, "name": "Shop — Monroe"}
DEFAULT_MIN = 60          # service minutes when Jobber has no duration
DAY_START_H = 8


def _blob(key, default):
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob(key) or default
    except Exception:
        pass
    f = BASE / "data" / f"{key.replace(':', '_')}.json"
    try:
        return json.loads(f.read_text()) if f.exists() else default
    except Exception:
        return default


def _blob_save(key, val):
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob(key, val)
            return
    except Exception:
        pass
    (BASE / "data" / f"{key.replace(':', '_')}.json").write_text(
        json.dumps(val))


def _slug(a):
    return re.sub(r"[^a-z0-9]+", "-", (a or "").lower()).strip("-")[:60]


VISITS_Q = """
query Day($start: ISO8601DateTime!, $end: ISO8601DateTime!, $after: String) {
  visits(first: 60, after: $after,
         filter: {startAt: {after: $start, before: $end}}) {
    pageInfo { hasNextPage endCursor }
    nodes { title startAt endAt
      assignedUsers(first: 3) { nodes { name { full } } }
      client { name }
      property { address { street city postalCode } } }
  }
}
"""

TASKS_Q = """
query Day($start: ISO8601DateTime!, $end: ISO8601DateTime!, $after: String) {
  tasks(first: 60, after: $after,
        filter: {startAt: {after: $start, before: $end}}) {
    pageInfo { hasNextPage endCursor }
    nodes { title startAt isComplete duration
      assignedUsers(first: 3) { nodes { name { full } } }
      client { name }
      property { address { street city postalCode } } }
  }
}
"""


def fetch_day(date_str, kind="visits"):
    """All visits or tasks on one local date -> normalized stop dicts.
    Office reminders (tasks with no property) are dropped — a route is
    made of ADDRESSES."""
    import jobber_client as jc
    was, jc.DRY_RUN = jc.DRY_RUN, False
    try:
        # Pacific day expressed in UTC (PDT = UTC-7; winter drift of an
        # hour only risks day-edge stops, acceptable for day sheets)
        start = f"{date_str}T07:00:00Z"
        end_d = (datetime.fromisoformat(date_str)
                 + timedelta(days=1)).date().isoformat()
        end = f"{end_d}T07:00:00Z"
        q = VISITS_Q if kind == "visits" else TASKS_Q
        out, cursor = [], None
        while True:
            d = jc._post(q, {"start": start, "end": end, "after": cursor},
                         f"route {kind}")
            if d.get("error"):
                return {"error": str(d)[:200], "stops": []}
            page = d[kind]
            for n in page["nodes"]:
                a = (n.get("property") or {}).get("address") or {}
                addr = ", ".join(x for x in (a.get("street"), a.get("city"),
                                             a.get("postalCode")) if x)
                if not addr:
                    continue                      # office reminder, no home
                techs = [u["name"]["full"] for u in
                         (n.get("assignedUsers") or {}).get("nodes", [])]
                st, en = n.get("startAt"), n.get("endAt")
                mins = DEFAULT_MIN
                if kind == "tasks" and n.get("duration"):
                    mins = max(10, int(n["duration"]) // 60)
                elif st and en:
                    try:
                        dt = (datetime.fromisoformat(en.rstrip("Z"))
                              - datetime.fromisoformat(st.rstrip("Z")))
                        m = int(dt.total_seconds() // 60)
                        if 10 <= m <= 600:
                            mins = m
                    except ValueError:
                        pass
                out.append({"title": (n.get("title") or "").strip()
                            or (n.get("client") or {}).get("name") or "?",
                            "client": (n.get("client") or {}).get("name"),
                            "address": addr, "city": a.get("city"),
                            "tech": techs[0] if techs else "Unassigned",
                            "techs": techs, "mins": mins,
                            "done": bool(n.get("isComplete"))
                            if kind == "tasks" else False})
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return {"stops": out}
    finally:
        jc.DRY_RUN = was


def geocode_many(stops):
    """Fill lat/lng on stops via the shared cache; only misses hit
    Google. Returns stops that geocoded."""
    from property_data import geocode, _api_key
    cache = _blob("geocode_cache", {})
    key, dirty, out = _api_key(), False, []
    for s in stops:
        sl = _slug(s["address"])
        if sl in cache:
            s["lat"], s["lng"] = cache[sl]
            out.append(s)
            continue
        try:
            g = geocode(s["address"], key)
            cache[sl] = [round(g["lat"], 5), round(g["lng"], 5)]
            s["lat"], s["lng"] = cache[sl]
            dirty = True
            out.append(s)
        except Exception:
            continue
    if dirty:
        _blob_save("geocode_cache", cache)
    return out


def _routes_api(points, optimize=True):
    """Google Routes API from the shop through points and back.
    Returns (order, legs, decoded_polyline) or None."""
    from property_data import _api_key
    body = {
        "origin": {"location": {"latLng": {
            "latitude": SHOP["lat"], "longitude": SHOP["lng"]}}},
        "destination": {"location": {"latLng": {
            "latitude": SHOP["lat"], "longitude": SHOP["lng"]}}},
        "intermediates": [{"location": {"latLng": {
            "latitude": p["lat"], "longitude": p["lng"]}}}
            for p in points],
        "travelMode": "DRIVE",
        "optimizeWaypointOrder": optimize and len(points) > 1,
    }
    mask = ("routes.optimizedIntermediateWaypointIndex,routes.duration,"
            "routes.distanceMeters,routes.legs.duration,"
            "routes.legs.distanceMeters,routes.polyline.encodedPolyline")
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-Goog-Api-Key": _api_key(),
                 "X-Goog-FieldMask": mask})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=45))["routes"][0]
    except Exception:
        return None
    order = r.get("optimizedIntermediateWaypointIndex",
                  list(range(len(points))))
    legs = [{"secs": int(l["duration"].rstrip("s")),
             "miles": round(l["distanceMeters"] / 1609, 1)}
            for l in r["legs"]]
    return order, legs, _decode(r["polyline"]["encodedPolyline"])


def _decode(enc):
    pts, idx, lat, lng = [], 0, 0, 0
    while idx < len(enc):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(enc[idx]) - 63
                idx += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += d
            else:
                lng += d
        pts.append([round(lat / 1e5, 5), round(lng / 1e5, 5)])
    return pts


def _nearest_neighbor(points):
    """Fallback order for days beyond the API's 25-stop optimizer."""
    left = list(range(len(points)))
    cur, order = (SHOP["lat"], SHOP["lng"]), []
    while left:
        i = min(left, key=lambda j: (points[j]["lat"] - cur[0]) ** 2
                + (points[j]["lng"] - cur[1]) ** 2)
        order.append(i)
        cur = (points[i]["lat"], points[i]["lng"])
        left.remove(i)
    return order


def build_day(date_str, kind="visits", max_age_min=15):
    """The whole day, every tech, ordered + timed. Cached briefly so
    office clicks stay instant; force=recompute by passing max_age 0."""
    key = f"routes:{date_str}:{kind}"
    cached = _blob(key, None)
    if cached and max_age_min:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                cached["computed_at"])).total_seconds() / 60
            if age < max_age_min:
                return cached
        except (KeyError, ValueError):
            pass
    raw = fetch_day(date_str, kind)
    if raw.get("error"):
        return {"error": raw["error"], "techs": {}, "date": date_str,
                "kind": kind, "computed_at":
                datetime.now(timezone.utc).isoformat(timespec="seconds")}
    stops = geocode_many(raw["stops"])
    by_tech = {}
    for s in stops:
        by_tech.setdefault(s["tech"], []).append(s)
    techs = {}
    for tech, pts in sorted(by_tech.items()):
        if len(pts) > 23:                     # API optimizer cap is 25
            order = _nearest_neighbor(pts)
            pts = [pts[i] for i in order]
            rt = _routes_api(pts[:23], optimize=False)
        else:
            rt = _routes_api(pts)
        if rt:
            order, legs, poly = rt
            pts = [pts[i] for i in order] if len(order) == len(pts) else pts
        else:
            legs, poly = [{"secs": 0, "miles": 0}] * (len(pts) + 1), []
        t = DAY_START_H * 60
        day = []
        for i, p in enumerate(pts):
            t += (legs[i]["secs"] // 60) if i < len(legs) else 0
            day.append({**{k: p[k] for k in
                           ("title", "client", "address", "city", "lat",
                            "lng", "mins", "done")},
                        "n": i + 1, "arrive": f"{t//60}:{t%60:02d}",
                        "drive_min": (legs[i]["secs"] // 60)
                        if i < len(legs) else 0})
            t += p["mins"]
        if legs and len(legs) > len(pts):
            t += legs[-1]["secs"] // 60
        techs[tech] = {"stops": day, "poly": poly[::3],
                       "back_at": f"{t//60}:{t%60:02d}",
                       "drive_min": sum(l["secs"] for l in legs) // 60,
                       "drive_mi": round(sum(l["miles"] for l in legs), 1)}
    out = {"date": date_str, "kind": kind, "techs": techs,
           "skipped_no_address": len(raw["stops"]) - len(stops)
           + sum(1 for s in raw["stops"] if not s.get("address")),
           "computed_at":
           datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _blob_save(key, out)
    return out
