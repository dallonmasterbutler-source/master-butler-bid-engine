"""
MASTER BUTLER — AERIAL CROSS-CHECK (prototype)

The straight-down view answers the two questions ground photos keep
getting wrong:
  * TREES: is canopy actually over the roof? (Connor's question — obvious
    from above, guesswork from the curb)
  * AREAS: driveway/walkway true extent (Boden lesson: ground photos
    undersell length)
  * WRONG BUILDING: is there an outbuilding Solar might have grabbed?
    (Gavin lesson)

Flow: address → geocode (existing helper) → Google Static Maps satellite
tile → Claude Vision with an aerial-specific prompt → strict JSON.

Read-only everywhere. Images cached in data/aerial/.
"""

import json
import re
import subprocess
import urllib.request
from pathlib import Path

import base64

from property_data import geocode, _api_key as _maps_key
from vision import _api_key as _anthropic_key, MODEL


def _prep_jpeg(path):
    """Convert any tile (png/tif) to a resized JPEG and return base64 —
    the API is told image/jpeg, so the bytes must actually BE jpeg."""
    tmp = Path("/tmp/aerial_prep") / (Path(path).stem + ".jpg")
    tmp.parent.mkdir(exist_ok=True)
    subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1400",
                    "-s", "formatOptions", "80", str(path),
                    "--out", str(tmp)], check=True, capture_output=True)
    return base64.standard_b64encode(tmp.read_bytes()).decode()

BASE = Path(__file__).parent
AERIAL_DIR = BASE / "data" / "aerial"

AERIAL_PROMPT = """Analyze this SATELLITE (straight-down) image of a single
residential property. The house of interest is at the CENTER of the image.
Return ONLY a JSON object — first character "{", last "}", strictly valid
JSON, no prose, no markdown fences. Exactly this shape:

{
 "main_roof": {"visible": true/false,
   "footprint_sqft_low": N, "footprint_sqft_high": N,
   "reasoning": "one line: what you scaled against",
   "confidence": "high|medium|low"},
 "other_buildings": [
   {"kind": "garage|shed|barn|outbuilding|neighbor_house",
    "relative_size": "smaller|similar|larger",
    "position": "one line: where relative to the main house"}
 ],
 "roofline": {"front_ft_low": N, "front_ft_high": N,
   "full_perimeter_ft_low": N, "full_perimeter_ft_high": N,
   "detail": "one line: how the eave lines run (for holiday lights)",
   "confidence": "high|medium|low"},
 "canopy_over_roof": {"level": "none|partial|heavy",
   "detail": "one line: which trees, which part of the roof",
   "confidence": "high|medium|low"},
 "mature_trees_within_20ft": {"count_band": "0|1-3|4_plus",
   "types": "conifer|deciduous|mixed|unknown",
   "confidence": "high|medium|low"},
 "surfaces": [
   {"type": "driveway|walkway|patio|deck",
    "sqft_low": N, "sqft_high": N,
    "detail": "one line", "confidence": "high|medium|low"}
 ],
 "not_determinable": ["...", ...]
}

Scale rules:
- At this zoom a typical two-car driveway is ~20 ft wide; a car is ~15 ft
  long; a two-car garage door ~16 ft. Use parked cars and the house itself
  as rulers.
- canopy_over_roof means crowns VISIBLY overlapping the roof outline of the
  CENTER house — trees near the property line that do not overlap the roof
  are "none".
- List every separate structure on the parcel in other_buildings — this is
  used to catch wrong-building measurements.
- Only report what you can see; unknowns go in not_determinable."""


def fetch_tile(address, zoom=20):
    """Geocode the address and save one aerial image. Returns the path.

    Prefers Google Static Maps (freshest imagery). If that API isn't
    enabled on the project (403), falls back to the Solar API's own rgb
    layer — always available wherever Solar works, but the flight can be
    YEARS old (the imageryDate lands in the filename so staleness is
    visible). Enable 'Maps Static API' in Google Cloud for current tiles.
    """
    key = _maps_key()
    geo = geocode(address, key)
    if not geo:
        raise SystemExit(f"could not geocode: {address}")
    lat, lng = geo["lat"], geo["lng"]
    AERIAL_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-")[:60]

    out = AERIAL_DIR / f"{slug}-z{zoom}.png"
    if out.exists():
        return out
    url = ("https://maps.googleapis.com/maps/api/staticmap?"
           f"center={lat},{lng}&zoom={zoom}&size=640x640&scale=2"
           f"&maptype=satellite&key={key}")
    try:
        out.write_bytes(urllib.request.urlopen(url, timeout=30).read())
        return out
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
    # ── fallback: Solar API rgb layer (GeoTIFF → png via sips) ──
    meta = json.load(urllib.request.urlopen(
        "https://solar.googleapis.com/v1/dataLayers:get?"
        f"location.latitude={lat}&location.longitude={lng}"
        f"&radiusMeters=35&view=IMAGERY_AND_ANNUAL_FLUX_LAYERS&key={key}",
        timeout=30))
    d = meta.get("imageryDate", {})
    stamp = f"{d.get('year', 0)}{d.get('month', 0):02}"
    tif = AERIAL_DIR / f"{slug}-solar-{stamp}.tif"
    png = tif.with_suffix(".png")
    if png.exists():
        return png
    tif.write_bytes(urllib.request.urlopen(
        meta["rgbUrl"] + f"&key={key}", timeout=60).read())
    subprocess.run(["sips", "-s", "format", "png", str(tif), "--out",
                    str(png)], check=True, capture_output=True)
    tif.unlink()
    return png


STREET_PROMPT = """Analyze this STREET-LEVEL photo of a residential property
(the house nearest the camera / center of frame). Return ONLY a JSON
object — first character "{", last "}", strictly valid, no prose:

{
 "stories": {"value": "1|1.5|2|3|unknown", "confidence": "high|medium|low"},
 "pitch_looks": {"value": "low|mild|moderate|steep|unknown",
   "detail": "one line: what the rooflines show",
   "confidence": "high|medium|low"},
 "roof_material": {"value": "composition|shake|metal|tile|unknown",
   "confidence": "high|medium|low"},
 "french_panes": true/false/null,
 "garage_bays": N,
 "visible_window_count": N,
 "hazards": ["power lines at roofline", ...],
 "detail": "one line describing the home"
}

Rules:
- Judge pitch by the gable/roofline angles you can SEE: low ≈ under 4/12,
  mild ≈ 4-6/12, moderate ≈ 7-8/12, steep ≈ 9/12 and up. If trees or the
  angle hide the roof, say "unknown" — never guess.
- french_panes = windows divided into small panes (grids). null if unclear.
- Only report what is clearly visible."""


def analyze_aerial(tile_path, extra_context="", prompt=None):
    """One image → strict JSON (same hardened loop as vision.py).
    Default prompt is the straight-down AERIAL one; pass STREET_PROMPT
    for curb photos."""
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": _prep_jpeg(tile_path)}}]
    text = prompt or AERIAL_PROMPT
    if extra_context:
        text += f"\n\nContext: {extra_context}"
    content.append({"type": "text", "text": text})

    cost, last_err = 0.0, None
    for attempt in (1, 2):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": MODEL, "max_tokens": 3000,
                             "messages": [{"role": "user", "content": content}]}).encode(),
            headers={"x-api-key": _anthropic_key(),
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=180))
        if resp.get("error"):
            raise RuntimeError(f"API error: {resp['error']}")
        raw = next((b["text"] for b in resp["content"]
                    if b.get("type") == "text"), None)
        u = resp.get("usage", {})
        cost += (u.get("input_tokens", 0) * 3 + u.get("output_tokens", 0) * 15) / 1e6
        if raw is None:
            last_err = RuntimeError("no text block")
            continue
        raw = raw.strip()
        if "{" in raw:
            raw = raw[raw.find("{"):raw.rfind("}") + 1]
        raw = re.sub(r",\s*([}\]])", r"\1", raw)
        raw = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
        try:
            return json.loads(raw), cost
        except json.JSONDecodeError as e:
            last_err = e
    raise RuntimeError(f"aerial returned unparseable JSON twice: {last_err}")


def survey(address, extra_context=""):
    """address → (aerial_reading, tile_path, cost)."""
    tile = fetch_tile(address)
    reading, cost = analyze_aerial(tile, extra_context)
    return reading, tile, cost


def fetch_streetview(address):
    """Curb-side photo of the property (the tech-pulling-up view) —
    LaRee's all-sides wish. Returns the path, or None where Google's
    car never drove (rural roads). Cached like the tiles."""
    key = _maps_key()
    slug = re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-")[:60]
    AERIAL_DIR.mkdir(parents=True, exist_ok=True)
    out = AERIAL_DIR / f"{slug}-street.jpg"
    if out.exists():
        return out
    loc = urllib.parse.quote(address)
    # metadata first — free, and tells us if imagery exists at all
    meta = json.load(urllib.request.urlopen(
        "https://maps.googleapis.com/maps/api/streetview/metadata?"
        f"location={loc}&key={key}", timeout=30))
    if meta.get("status") != "OK":
        return None
    try:
        out.write_bytes(urllib.request.urlopen(
            "https://maps.googleapis.com/maps/api/streetview?size=640x640"
            f"&location={loc}&fov=80&key={key}", timeout=30).read())
    except urllib.error.HTTPError:
        return None       # imagery exists but fetch refused — try next run
    return out


# Trees change; imagery older than this can't be trusted for canopy calls.
MAX_TREE_IMAGERY_AGE_YEARS = 5


def _imagery_year(tile_path):
    """Solar-fallback tiles carry their flight date in the filename;
    Static Maps tiles are treated as current."""
    m = re.search(r"-solar-(\d{4})\d{2}", Path(tile_path).name)
    return int(m.group(1)) if m else None      # None = current imagery


def cross_check(prop, address, today_year=2026, _reading=None, _tile=None):
    """Aerial second opinion on a drafted property. Returns (fields, notes).

    FLAG-DON'T-GUESS: this only ever adds notes and raises the debris
    call (canopy over roof, fresh imagery only). It never silently
    changes areas — big disagreements become office flags.
    (_reading/_tile: test injection — skips the network.)
    """
    fields, notes = {}, []
    if _reading is not None:
        reading, tile = _reading, _tile or "test-tile.png"
    else:
        reading, tile, cost = survey(address)
    year = _imagery_year(tile)
    vintage = f"{year} imagery" if year else "current imagery"
    stale = year is not None and (today_year - year) > MAX_TREE_IMAGERY_AGE_YEARS

    # 1) WRONG-BUILDING GUARD (the Gavin lesson): outbuildings on the
    #    parcel mean Solar's roof number may belong to the wrong structure.
    others = [b for b in reading.get("other_buildings", [])
              if b.get("kind") != "neighbor_house"]
    if others:
        kinds = ", ".join(b.get("kind", "structure") for b in others)
        notes.append(f"Aerial ({vintage}): parcel has other structures "
                     f"({kinds}) — verify roof/sqft is the HOUSE, not one "
                     "of these.")

    # 2) AREA SECOND OPINION (the Boden lesson): compare aerial surface
    #    reads to ground-photo reads; big gaps get flagged, not auto-fixed.
    ground = prop.get("surfaces", {}) or {}
    for s in reading.get("surfaces", []):
        key = {"driveway": "driveway", "walkway": "sidewalk",
               "patio": "patio"}.get(s.get("type"))
        if not key:
            continue
        conf = s.get("confidence", "low")
        mid = (s.get("sqft_low", 0) + s.get("sqft_high", 0)) / 2
        if key in ground and ground[key]:
            if conf == "low":
                continue            # never second-guess photos on a low read
            ratio = mid / ground[key]
            if ratio > 1.4 or ratio < 0.6:
                notes.append(f"Aerial ({vintage}): {key} looks ~{int(mid)} "
                             f"sqft from above vs {ground[key]} from photos "
                             "— office verify before sending.")
        elif prop.get("services", {}).get(key):
            # the service was requested and NOBODY had a measurement —
            # an aerial measurement beats a $0 draft (fills blanks only,
            # never overrides ground photos; office verifies). Low-
            # confidence reads still fill, but say so LOUDLY.
            fields.setdefault("surfaces", {})[key] = int(mid)
            loud = (" ⚠ LOW-CONFIDENCE read — verify against a photo "
                    "before sending" if conf == "low" else
                    " — priced on that; office verify before sending")
            notes.append(f"Aerial ({vintage}): {key} measured ~{int(mid)} "
                         f"sqft FROM ABOVE (range {s.get('sqft_low')}-"
                         f"{s.get('sqft_high')}, {conf} confidence){loud}.")

    # 2b) GENERIC "pressure washing" with NO surface named: don't guess
    #     which surfaces the customer meant — hand the office a PRICED
    #     MENU of what's visible from above instead.
    pw_keys = ("driveway", "patio", "sidewalk")
    if not any(prop.get("services", {}).get(k) for k in pw_keys):
        menu = []
        for s in reading.get("surfaces", []):
            key = {"driveway": "driveway", "walkway": "sidewalk",
                   "patio": "patio"}.get(s.get("type"))
            if not key:
                continue
            mid = int((s.get("sqft_low", 0) + s.get("sqft_high", 0)) / 2)
            if mid <= 0:
                continue
            from bid_engine import pw_concrete_price, round_to_5
            menu.append(f"{key} ~{mid} sqft ≈ "
                        f"${round_to_5(pw_concrete_price(mid))}")
        if menu:
            notes.append(f"Aerial PW menu ({vintage}) — customer didn't "
                         "specify surfaces; pick with them: "
                         + "; ".join(menu)
                         + ". (Individual prices; combining in one visit "
                         "shares the setup and comes out lower.)")

    # 3) TREES (Connor's question) — only from reasonably fresh imagery.
    if stale:
        notes.append(f"Aerial imagery is from {year} — tree/canopy reads "
                     "skipped (too old to trust).")
    else:
        canopy = reading.get("canopy_over_roof", {})
        near = reading.get("mature_trees_within_20ft", {})
        if canopy.get("confidence") in ("high", "medium"):
            if canopy.get("level") == "heavy":
                fields["debris"] = "heavy"
                notes.append(f"Aerial ({vintage}): heavy canopy over roof — "
                             f"heavy-debris charge ({canopy.get('detail', '')}). "
                             "Office: confirm.")
            elif (canopy.get("level") == "partial"
                  or near.get("count_band") in ("1-3", "4_plus")):
                notes.append(f"Aerial ({vintage}): trees near house, canopy "
                             f"{canopy.get('level', '?')} — normal debris "
                             "assumed; office may bump for heavy droppers.")
    return fields, notes


PITCH_ORDER = ["low", "mild", "moderate", "steep", "tom_only"]
# (street photos can't SAY "tom_only" — but data claiming tom_only while
# the roofline looks mild/moderate is exactly the overcall to catch)


def street_check(prop, address, _reading=None):
    """Curb-photo second opinion on stories / pitch / roof material.

    CONSERVATIVE MERGE (these inputs move price AND safety):
      * stories: adopt only if the property had none/default; a
        DISAGREEMENT with existing data is flagged, never auto-fixed.
      * pitch: never changed automatically in either direction — a
        big disagreement becomes an office flag (pitch overcall is the
        #1 known input error; the flag is the fix).
      * roof material: adopted only when it makes the bid MORE cautious
        (standard → shake/metal), because that's the expensive-to-miss
        direction. Never downgrades.
      * french panes: note for the window pricer.
    Returns (fields, notes). (_reading = test injection.)
    """
    fields, notes = {}, []
    if _reading is None:
        curb = fetch_streetview(address)
        if curb is None:
            return fields, notes
        _reading, _ = analyze_aerial(curb, prompt=STREET_PROMPT)
    r = _reading

    st = r.get("stories", {})
    if st.get("confidence") == "high" and st.get("value") not in (None, "unknown"):
        seen = st["value"].replace("1.5", "2")     # 1.5-story prices as 2
        if not prop.get("stories") or prop.get("stories_defaulted"):
            fields["stories"] = seen
            notes.append(f"Street view: {st['value']}-story home "
                         "(filled a blank, not overriding records).")
        elif prop["stories"] != seen:
            notes.append(f"Street view sees {st['value']} stories but "
                         f"records say {prop['stories']} — OFFICE VERIFY "
                         "(stories change price and tech assignment).")

    p = r.get("pitch_looks", {})
    have = prop.get("pitch")
    if (p.get("confidence") in ("high", "medium")
            and p.get("value") in PITCH_ORDER and have in PITCH_ORDER):
        gap = PITCH_ORDER.index(have) - PITCH_ORDER.index(p["value"])
        if gap >= 2:
            notes.append(f"Street view: roofline looks {p['value']} but "
                         f"data says {have} — possible pitch OVERCALL, "
                         "office verify before pricing the premium.")
        elif gap <= -2:
            notes.append(f"Street view: roofline looks {p['value']} but "
                         f"data says {have} — possible UNDERCALL, verify "
                         "for tech safety.")

    rm = r.get("roof_material", {})
    if (rm.get("confidence") == "high"
            and rm.get("value") in ("shake", "metal", "tile")
            and prop.get("roof_material") in (None, "standard")):
        mapped = {"shake": "shake", "metal": "metal_full",
                  "tile": "standard"}[rm["value"]]
        if mapped != "standard":
            fields["roof_material"] = mapped
            notes.append(f"Street view: {rm['value']} roof — specialty "
                         "pricing applied (verify on site).")

    # NOTE ONLY — decorative grids look identical to true divided panes
    # from the curb (Dallon's own home: gridded windows, standard window
    # price was correct). The premium is an office call, never automatic.
    if r.get("french_panes") is True:
        notes.append("Street view: windows appear gridded — IF true divided "
                     "french panes (not decorative grids), apply the pane "
                     "premium. Office judges from photos.")
    return fields, notes


if __name__ == "__main__":
    import sys
    addr = " ".join(sys.argv[1:])
    if not addr:
        raise SystemExit("usage: python3 aerial.py <address>")
    reading, tile, cost = survey(addr)
    print(f"tile: {tile}   (~${cost:.3f})")
    print(json.dumps(reading, indent=1))
