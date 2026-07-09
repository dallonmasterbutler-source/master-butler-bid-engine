"""
MASTER BUTLER — COUNTY ASSESSOR RECORDS (public record, stdlib only)

"The sqft is public record, so we need to make a greater effort on
grabbing that info." — Dallon, Jul 8

Two-step lookup, works on Mac and cloud:
  1. lat/lng -> parcel number, via Washington's statewide Current
     Parcels layer (WA Geospatial Portal, free ArcGIS query).
  2. parcel -> finished sqft / stories / roof material, from county
     assessor extracts bundled in data/assessor/*.json.gz
     (Snohomish: Improvement Records; King: EXTR_ResBldg).

Refresh path: rerun the index builds from the county downloads
(annual is plenty — houses don't change size often).
"""

import gzip
import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
IDX_DIR = BASE / "data" / "assessor"

PARCELS_URL = ("https://services.arcgis.com/jsIt88o09Q0r1j8h/arcgis/rest/"
               "services/Current_Parcels/FeatureServer/0/query")

# COUNTY_NM codes in the statewide layer -> our bundled index files
COUNTY_IDX = {"61": "snoco.json.gz",    # Snohomish
              "33": "king.json.gz"}     # King

_cache = {}


def _index(fname):
    if fname not in _cache:
        p = IDX_DIR / fname
        if not p.exists():
            _cache[fname] = {}
        else:
            with gzip.open(p, "rt") as f:
                _cache[fname] = json.load(f)
    return _cache[fname]


def parcel_at(lat, lng):
    """Point-in-polygon on the statewide parcels layer.
    Returns (county_code, parcel_id) or (None, None)."""
    q = urllib.parse.urlencode({
        "geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "outFields": "COUNTY_NM,ORIG_PARCEL_ID",
        "returnGeometry": "false", "f": "json"})
    req = urllib.request.Request(PARCELS_URL + "?" + q,
                                 headers={"User-Agent": "Mozilla/5.0"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:
        return None, None
    feats = d.get("features") or []
    if not feats:
        return None, None
    a = feats[0]["attributes"]
    return a.get("COUNTY_NM"), (a.get("ORIG_PARCEL_ID") or "").strip()


def lookup(lat, lng):
    """lat/lng -> {'sqft','stories','roof_material','parcel','county'}
    from public assessor records, or None."""
    county, pin = parcel_at(lat, lng)
    if not pin:
        return None
    fname = COUNTY_IDX.get(county or "")
    if not fname:
        return None
    hit = _index(fname).get(pin)
    if not hit:
        return None
    sqft, stories, roof = hit[:3]
    out = {"sqft": sqft,
           "stories": stories,
           "roof_material": roof,
           "parcel": pin,
           "county": "Snohomish" if county == "61" else "King"}
    # Jessica Jensen lesson (Jul 9): a walkout basement hides in 'sqft'
    # semantics and a garage adds real roof — carry both when the index
    # has them (entries are [sqft, stories, roof, bsmt_fin, garage_att])
    if len(hit) >= 5:
        if hit[3]:
            out["basement_sqft"] = hit[3]
        if hit[4]:
            out["garage_sqft"] = hit[4]
    return out


if __name__ == "__main__":
    from property_data import geocode, _api_key
    for addr in ("9209 190th Ave SE, Snohomish, WA 98290",
                 "12143 68th Ave SE, Snohomish, WA 98296",
                 "20518 Meadow Lake Rd, Snohomish, WA 98290",
                 "20905 SE 7th Pl, Sammamish, WA 98074"):
        geo = geocode(addr, _api_key())
        rec = lookup(geo["lat"], geo["lng"]) if geo else None
        print(f"{addr[:44]:44} -> {rec}")
