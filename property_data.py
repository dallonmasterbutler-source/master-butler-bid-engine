"""
MASTER BUTLER — PROPERTY DATA (live Google APIs + sanity checks)

Turns an address into the facts the bid engine needs, and — just as
important — GRADES its own data. The 15-property survey taught us that
the Solar API is right ~80% of the time and wrong in detectable ways
(grabs a shed, grabs the neighbor, misses under tree cover). So every
lookup returns both the facts AND a list of data_flags describing what
looks off. Flags lower the confidence score and show up in office notes.

STORIES RULE (permanent): stories is a SAFETY input, not just pricing.
It always survives into the property record, and 3-story homes always
carry the office-review flag no matter how good the other data looks.
"""

import urllib.request
import urllib.parse
import json
import math
from pathlib import Path


def _api_key():
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("GOOGLE_MAPS_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


# ─────────────────────────────────────────────────────────────
# SANITY CHECKS (pure logic — no network, fully testable)
# ─────────────────────────────────────────────────────────────

# A detached-home roof smaller than this is probably a shed/garage/wrong hit
SUSPICIOUS_ROOF_SQFT = 800

# Roof-to-house ratios outside this band mean "probably the wrong building"
# (survey range on verified homes was 0.89–1.39)
RATIO_LOW, RATIO_HIGH = 0.6, 1.8


def validate_roof(roof_sqft, records_sqft=None):
    """Grade Solar's roof measurement. Returns (usable, flags, deduction)."""
    flags = []
    deduction = 0

    if roof_sqft is None:
        return False, ["No Solar roof data for this address."], 25

    if roof_sqft < SUSPICIOUS_ROOF_SQFT:
        flags.append(f"Roof measured only {roof_sqft:,.0f} sqft — likely the "
                     "wrong building (shed/garage). Do not trust; verify.")
        return False, flags, 25

    if records_sqft:
        ratio = roof_sqft / records_sqft
        if not (RATIO_LOW <= ratio <= RATIO_HIGH):
            flags.append(f"Roof {roof_sqft:,.0f} sqft vs records "
                         f"{records_sqft:,.0f} sqft (ratio {ratio:.2f}) — "
                         "mismatch, Solar may have measured the wrong building.")
            return False, flags, 20

    return True, flags, 0


def pitch_band(pitch_over_12):
    """Convert a measured x/12 pitch to our calculator knob.

    CONSERVATIVE NEAR BOUNDARIES (calibrated July 2026): satellite pitch
    reads ramblers about one band HIGH — two ground-truth checks (Dallon's
    and Shane's homes, both actually mild) came back "moderate". Within
    the grace zone above a band edge we price the CHEAPER band and flag
    for verification, instead of silently upcharging 20%.
    """
    GRACE = 1.0   # x/12 units above a band edge that still price down
    if pitch_over_12 is None:
        return "moderate", ["Pitch unknown — assumed moderate; verify."]
    if pitch_over_12 <= 4 + GRACE:
        flags = ([] if pitch_over_12 <= 4 else
                 [f"Pitch measured {pitch_over_12:.0f}/12 — near mild/moderate "
                  "boundary, priced as MILD; verify on-site."])
        return "mild", flags
    if pitch_over_12 <= 8:
        # NO grace at the moderate/steep edge: "steep" decides WHO can be
        # on the roof (safety), not just price. Never rounded down.
        return "moderate", []
    if pitch_over_12 <= 10:
        return "steep", []
    # NO grace above steep: 11-12/12 is a SAFETY call, never rounded down.
    return "tom_only", []


# ─────────────────────────────────────────────────────────────
# LIVE API CALLS
# ─────────────────────────────────────────────────────────────

def geocode(address, key):
    url = ("https://maps.googleapis.com/maps/api/geocode/json?"
           + urllib.parse.urlencode({"address": address, "key": key}))
    r = json.load(urllib.request.urlopen(url, timeout=15))
    if r["status"] == "OK":
        res = r["results"][0]
        loc = res["geometry"]["location"]
        return {"lat": loc["lat"], "lng": loc["lng"],
                "formatted": res["formatted_address"]}
    return None


def solar_roof(lat, lng, key):
    url = ("https://solar.googleapis.com/v1/buildingInsights:findClosest?"
           + urllib.parse.urlencode({"location.latitude": lat,
                                     "location.longitude": lng,
                                     "requiredQuality": "MEDIUM",
                                     "key": key}))
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
    return {"roof_sqft": m2 * 10.7639 if m2 else None,
            "pitch_over_12": pitch,
            "segments": len(segs)}


# ─────────────────────────────────────────────────────────────
# THE ONE CALL THE PIPELINE MAKES
# ─────────────────────────────────────────────────────────────

def lookup_property(address, records=None):
    """Address in → facts + data_flags + confidence deduction out.

    `records` optionally carries what we know from property records
    (e.g. {"sqft": 2540, "stories": "2"}). Records sqft is what the
    CURRENT pricing formulas use; roof data is captured alongside it
    for the future roof-basis recalibration.
    """
    records = records or {}
    flags = []
    deduction = 0
    key = _api_key()

    out = {
        "address": address,
        "sqft": records.get("sqft"),          # pricing basis (records)
        "stories": records.get("stories"),     # SAFETY input — never dropped
        "roof_sqft": None, "pitch": "moderate",
        "lat": None, "lng": None,
    }

    if not key:
        flags.append("No API key — property lookup skipped entirely.")
        return out, flags, 40

    geo = geocode(address, key) if address else None
    if not geo:
        flags.append("Address could not be geocoded — verify with customer.")
        return out, flags, 30
    out["lat"], out["lng"] = geo["lat"], geo["lng"]
    if geo["formatted"].split(",")[0].lower() not in (address or "").lower():
        flags.append(f"Address normalized to: {geo['formatted']}")

    roof = solar_roof(geo["lat"], geo["lng"], key)
    roof_sqft = roof["roof_sqft"] if roof else None
    usable, roof_flags, roof_ded = validate_roof(roof_sqft, records.get("sqft"))
    flags += roof_flags
    deduction += roof_ded
    if usable:
        out["roof_sqft"] = roof_sqft
        band, pitch_flags = pitch_band(roof["pitch_over_12"])
        out["pitch"] = band
        flags += pitch_flags
        if band == "tom_only":
            flags.append("Pitch measured 11+/12 — TOM ONLY job.")
    # If we have no records sqft, roof area is our only size signal —
    # usable for a rough draft but the office must confirm.
    if not out["sqft"]:
        if usable:
            out["sqft"] = round(roof_sqft)   # imperfect stand-in, flagged
            flags.append("No records sqft — using ROOF area as stand-in. "
                         "Office should verify size before sending.")
            deduction += 15
        else:
            flags.append("No usable size data at all — office must supply sqft.")

    # STORIES — the safety knob. Unknown stories = assume 2 but say so.
    if not out["stories"]:
        out["stories"] = "2"
        flags.append("Stories unknown — assumed 2. Verify: 3-story homes "
                     "have safety/tech-assignment rules.")
        deduction += 10

    return out, flags, min(deduction, 60)
