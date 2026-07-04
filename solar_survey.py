"""
MASTER BUTLER — SOLAR SURVEY: roof area vs house sqft

Question we're settling with data: the pricing formulas were built on
HOUSE square footage (living space). The Solar API hands us ROOF area.
Are they interchangeable? If not, what's the relationship?

Method: run 15 REAL properties (our price-anchor houses + real customer
addresses from mined form submissions) through geocoding + Solar, then
compare — and for houses with real invoice prices, test which basis
makes the pricing most consistent.
"""

import urllib.request
import urllib.parse
import json
import math
import time
from pathlib import Path

KEY = [l.split("=", 1)[1].strip()
       for l in open(Path(__file__).parent / ".env")
       if l.startswith("GOOGLE_MAPS_API_KEY=")][0]

# label, address, house_sqft (records, None=unknown), stories, real_price_note
PROPERTIES = [
    ("Alison Grande (shake $350 gutters)", "20613 NE 34th Pl, Sammamish, WA 98074", 2200, 1, "gutters $350 shake"),
    ("Prolink (shake $200 gutters)",       "13810 SE 52nd Pl, Bellevue, WA 98006", 2120, 2, "gutters $200 shake (underpriced)"),
    ("Jing Xu",                            "24323 SE 42nd Pl, Issaquah, WA 98029", 2540, 2, None),
    ("Dawn Goehner",                       "325 7th Ave W, Kirkland, WA 98033", 1910, 1, None),
    ("Shibu (patio $100)",                 "2005 265th Ave SE, Sammamish, WA 98075", 3730, 2, "patio $100"),
    ("22225 NE 31st (sidewalk quote)",     "22225 NE 31st St, Sammamish, WA 98074", 2820, 2, None),
    ("Ying Wan (driveway $417)",           "3731 130th Ave NE, Bellevue, WA 98005", 4212, 2, "driveway $417 deck $300 patio $175"),
    ("Jerry Chichester (in/out calc $461)","6421 NE 187th St, Kenmore, WA 98028", None, 1, "in/out sqft calc said $461 => ~3546 house sqft"),
    ("Form: Monroe",                       "19688 143rd Pl SE, Monroe, WA 98272", None, None, None),
    ("Form: Bellevue",                     "1824 151st Ave SE, Bellevue, WA 98007", None, None, None),
    ("Form: Sammamish 1",                  "24530 SE 1st St, Sammamish, WA 98074", None, None, None),
    ("Form: Woodinville",                  "18309 NE 201st Dr, Woodinville, WA 98077", None, None, None),
    ("Form: Issaquah 1",                   "2092 30th Ln NE, Issaquah, WA 98029", None, None, None),
    ("Form: Sammamish 2",                  "843 245th Place NE, Sammamish, WA 98074", None, None, None),
    ("Form: Redmond",                      "2831 266th Ave NE, Redmond, WA 98053", None, None, None),
]


def geocode(address):
    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(
        {"address": address, "key": KEY})
    r = json.load(urllib.request.urlopen(url, timeout=15))
    if r["status"] == "OK":
        loc = r["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    return None, None


def solar(lat, lng):
    url = ("https://solar.googleapis.com/v1/buildingInsights:findClosest?"
           + urllib.parse.urlencode({"location.latitude": lat,
                                     "location.longitude": lng,
                                     "requiredQuality": "MEDIUM",
                                     "key": KEY}))
    try:
        r = json.load(urllib.request.urlopen(url, timeout=20))
    except urllib.error.HTTPError:
        return None
    sp = r.get("solarPotential", {})
    m2 = sp.get("wholeRoofStats", {}).get("areaMeters2")
    segs = sp.get("roofSegmentStats", [])
    pitch = None
    if segs:
        main = max(segs, key=lambda s: s.get("stats", {}).get("areaMeters2", 0))
        deg = main.get("pitchDegrees")
        if deg is not None:
            pitch = 12 * math.tan(math.radians(deg))
    # ground footprint of the building (roof projected to flat ground)
    ground_m2 = sp.get("buildingStats", {}).get("areaMeters2")
    return {
        "roof_sqft": m2 * 10.7639 if m2 else None,
        "ground_sqft": ground_m2 * 10.7639 if ground_m2 else None,
        "pitch_12": pitch,
        "segments": len(segs),
        "imagery_year": r.get("imageryDate", {}).get("year"),
    }


def main():
    rows = []
    for label, address, house_sqft, stories, note in PROPERTIES:
        lat, lng = geocode(address)
        info = solar(lat, lng) if lat else None
        rows.append((label, address, house_sqft, stories, note, info))
        got = f"{info['roof_sqft']:,.0f} sqft roof" if info and info["roof_sqft"] else "NO SOLAR DATA"
        print(f"  {label[:38]:<40} {got}")
        time.sleep(0.3)

    print("\n" + "=" * 78)
    print(f"{'PROPERTY':<38}{'HOUSE':>7}{'ROOF':>8}{'RATIO':>7}{'PITCH':>7}{'YR':>6}")
    print("-" * 78)
    for label, address, house_sqft, stories, note, info in rows:
        if not info or not info["roof_sqft"]:
            print(f"{label[:37]:<38}{'—':>7}{'no data':>8}")
            continue
        roof = info["roof_sqft"]
        ratio = f"{roof/house_sqft:.2f}" if house_sqft else "—"
        pitch = f"{info['pitch_12']:.0f}/12" if info["pitch_12"] else "—"
        print(f"{label[:37]:<38}"
              f"{house_sqft or '—':>7}"
              f"{roof:>8,.0f}"
              f"{ratio:>7}"
              f"{pitch:>7}"
              f"{info['imagery_year'] or '—':>6}")

    # ── The pricing question ──
    print("\n" + "=" * 78)
    print("PRICING TEST — real shake gutter jobs, rate under each basis:")
    for label, address, house_sqft, stories, note, info in rows:
        if note and "gutters" in note and info and info["roof_sqft"]:
            price = 350 if "350" in note else 200
            print(f"\n  {label}")
            print(f"    real price ${price}  |  house {house_sqft} sqft  |  "
                  f"roof {info['roof_sqft']:,.0f} sqft  |  {stories}-story")
            print(f"    implied rate per HOUSE sqft: ${price/house_sqft:.3f}")
            print(f"    implied rate per ROOF  sqft: ${price/info['roof_sqft']:.3f}")


if __name__ == "__main__":
    main()
