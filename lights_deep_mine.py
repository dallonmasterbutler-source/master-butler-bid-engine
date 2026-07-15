"""
MASTER BUTLER — LIGHTS DEEP MINE: SEASONAL PRICING + FRONT FOOTAGE
(Dallon, Jul 14: "some discounts apply to diwali/early bird pricing,
october and november pricing … critical for the system to bid right.
… see how many homes you can measure linear feet on the front and lock
down our per-ft pricing, including material cost — 'material priced on
site' sometimes bites us. Do like 100 homes.")

Two read-only jobs, run overnight after the route mine:

1. PRICE MINE — every invoice since 2023 with a lights line:
   · per-MONTH pricing (Oct early-bird vs Nov vs Dec vs takedown)
   · every DISCOUNT line riding a lights invoice (early bird/October/
     Diwali tiers, named)
   · every MATERIAL line (the 'priced on site' wildcard, measured)
   · $/ft wherever the line text carries footage ("120 ft")

2. FRONT FOOTAGE v1 — for up to 100 lights homes (roster from
   sched_mine): Google Solar buildingInsights roof segments. The
   street side = the compass direction from the roof center toward the
   geocoded street point; front footage ≈ the horizontal run of
   street-facing eaves + gable peaks. v1 is an ESTIMATE — raw segments
   are saved so the math can be recalibrated against homes Dallon can
   eyeball. Additional sides / add-ons are explicitly NOT captured.

Writes blob `lights_pricing` + data/lights_footage.json.
"""

import collections
import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent

IQ = """
query($after: String) {
  invoices(first: 60, after: $after, sort: {key: CREATED_AT, direction: DESCENDING}) {
    pageInfo { hasNextPage endCursor }
    nodes { createdAt amounts { total }
            client { name }
            lineItems { nodes { name totalPrice quantity } } } } }"""

FT_RX = re.compile(r"(\d{2,4})\s*(?:ft|feet|lf|linear)", re.I)
LIGHT_RX = re.compile(r"light", re.I)
MATERIAL_RX = re.compile(r"material|bulb|clip|timer|wire|extension", re.I)
DISCOUNT_RX = re.compile(r"discount|early|october|promo|diwali", re.I)
TAKEDOWN_RX = re.compile(r"take ?down|removal", re.I)


def _save_blob(name, val):
    """Cloud direct when possible; HTTPS courier from the Mac
    (night_run's system python has no psycopg)."""
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


def mine_prices(verbose=False):
    import jobber_client as jc
    was, jc.DRY_RUN = jc.DRY_RUN, False
    bymonth = collections.defaultdict(list)     # month# → install $ lines
    materials, discounts, perft = [], [], []
    after, throttles = None, 0
    try:
        while True:
            d = jc._post(IQ, {"after": after}, "lights price mine "
                         "(read-only)")
            if d.get("error"):
                if "Throttled" in str(d) and throttles < 60:
                    throttles += 1
                    time.sleep(60)
                    continue
                break
            conn = d["invoices"]
            stop = False
            for node in conn["nodes"]:
                c = (node.get("createdAt") or "")[:10]
                if c < "2023-01-01":
                    stop = True
                    break
                lines = ((node.get("lineItems") or {}).get("nodes")
                         or [])
                if not any(LIGHT_RX.search(li.get("name") or "")
                           for li in lines):
                    continue
                mo = int(c[5:7])
                for li in lines:
                    nm = (li.get("name") or "").strip()
                    val = li.get("totalPrice") or 0
                    if LIGHT_RX.search(nm) and not TAKEDOWN_RX.search(nm) \
                            and val > 50:
                        bymonth[mo].append(val)
                        m = FT_RX.search(nm)
                        if m and val > 100:
                            ft = int(m.group(1))
                            if 20 <= ft <= 600:
                                perft.append({"ft": ft, "usd": val,
                                              "per_ft": round(val / ft, 2),
                                              "month": mo, "when": c})
                    if MATERIAL_RX.search(nm) and 0 < val < 900:
                        materials.append({"name": nm[:60], "usd": val,
                                          "qty": li.get("quantity"),
                                          "when": c})
                    if DISCOUNT_RX.search(nm) and val < 0:
                        discounts.append({"name": nm[:60], "usd": val,
                                          "month": mo, "when": c})
            if stop or not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
            time.sleep(4)
    finally:
        jc.DRY_RUN = was

    def med(xs):
        xs = sorted(xs)
        return round(xs[len(xs) // 2], 2) if xs else None

    out = {"install_line_by_month": {
               str(m): {"n": len(v), "median": med(v),
                        "mean": round(sum(v) / len(v), 2)}
               for m, v in sorted(bymonth.items())},
           "per_ft_samples": sorted(perft, key=lambda x: x["when"],
                                    reverse=True)[:120],
           "per_ft_median": med([p["per_ft"] for p in perft]),
           "per_ft_by_month": {
               str(m): med([p["per_ft"] for p in perft
                            if p["month"] == m])
               for m in sorted({p["month"] for p in perft})},
           "material_lines": {"n": len(materials),
                              "median_usd": med([m["usd"]
                                                 for m in materials]),
                              "recent": materials[:40]},
           "discount_lines": discounts[:60]}
    if verbose:
        print(f"lights price mine: months={list(out['install_line_by_month'])} "
              f"per-ft n={len(perft)} median={out['per_ft_median']} "
              f"materials n={len(materials)} discounts n={len(discounts)}")
    return out


def _bearing(a, b):
    """Compass bearing a→b in degrees."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = (math.cos(la1) * math.sin(la2)
         - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def measure_fronts(limit=100, verbose=False):
    from property_data import _api_key
    key = _api_key()
    try:
        homes = json.loads((BASE / "data" / "lights_homes.json")
                           .read_text())
    except Exception:
        return None
    out = []
    for h in homes[:limit]:
        url = ("https://solar.googleapis.com/v1/buildingInsights:"
               "findClosest?" + urllib.parse.urlencode(
                   {"location.latitude": h["lat"],
                    "location.longitude": h["lng"],
                    "requiredQuality": "LOW", "key": key}))
        try:
            r = json.load(urllib.request.urlopen(url, timeout=20))
        except Exception:
            continue
        segs = (r.get("solarPotential") or {}).get("roofSegmentStats") \
            or []
        ctr = r.get("center") or {}
        if not segs or ctr.get("latitude") is None:
            continue
        # street direction: roof center → the parcel's street-side
        # geocode point (they differ; the offset points at the road)
        street_az = _bearing((ctr["latitude"], ctr["longitude"]),
                             (h["lat"], h["lng"]))
        front_ft, peaks = 0.0, 0
        raw = []
        for s in segs:
            az = s.get("azimuthDegrees")
            st = s.get("stats") or {}
            a_m2 = st.get("areaMeters2") or 0
            pitch = s.get("pitchDegrees") or 20
            bb = s.get("boundingBox") or {}
            sw, ne = bb.get("sw") or {}, bb.get("ne") or {}
            raw.append({"az": az, "m2": round(a_m2, 1), "pitch": pitch})
            if az is None or a_m2 < 5:
                continue
            diff = abs((az - street_az + 180) % 360 - 180)
            if diff <= 60:               # roughly street-facing plane
                # horizontal eave run ≈ plane area ÷ slope depth;
                # slope depth from bbox extent projected on azimuth
                if sw and ne:
                    dlat = (ne["latitude"] - sw["latitude"]) * 111320
                    dlng = ((ne["longitude"] - sw["longitude"])
                            * 111320 * math.cos(math.radians(
                                ctr["latitude"])))
                    # width perpendicular to azimuth (the eave line)
                    azr = math.radians(az)
                    width_m = abs(dlat * math.sin(azr)) \
                        + abs(dlng * math.cos(azr))
                    front_ft += width_m * 3.281
            elif 60 < diff <= 120:       # gable side → a front peak run
                peaks += 1
        # SIDE WRAP (Dallon, Jul 14): installs turn the corner and run
        # 5-10 ft down EACH side of the home — add the midpoint (+15 ft
        # total) to every front estimate; the raw eave number stays
        # separate so calibration can tune the wrap per home style.
        SIDE_WRAP_FT = 15
        est = round(front_ft + peaks * 12 + SIDE_WRAP_FT)
        out.append({"client": h["client"], "address": h["address"],
                    "city": h["city"], "front_eave_ft_est": round(
                        front_ft), "peaks_est": peaks,
                    "side_wrap_ft": 15,
                    "front_total_ft_est": est,
                    "segments": raw[:10]})
        time.sleep(0.2)
    (BASE / "data" / "lights_footage.json").write_text(
        json.dumps(out, indent=1))
    if verbose and out:
        fts = sorted(x["front_total_ft_est"] for x in out)
        print(f"footage v1: {len(out)} homes measured · median front "
              f"{fts[len(fts)//2]} ft · range {fts[0]}–{fts[-1]}")
    return out


def run(verbose=False):
    import clouddb
    prices = mine_prices(verbose=verbose)
    fronts = measure_fronts(verbose=verbose) or []
    blob = {"prices": prices,
            "front_footage_v1": {
                "n": len(fronts),
                "method": "Solar API street-facing eaves + 12ft/peak "
                          "(v1 ESTIMATE — calibrate vs known homes "
                          "before quoting off it)",
                "homes": fronts[:100]},
            "mined_at": date.today().isoformat()}
    _save_blob("lights_pricing", blob)
    return blob


if __name__ == "__main__":
    run(verbose=True)
