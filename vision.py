"""
MASTER BUTLER — VISION MODULE (the system's eyes)

Takes customer/property photos and returns STRUCTURED facts the bid
engine can use directly: surfaces + areas, buildup severity, roof
material, stories, hazards, items to move — each with its own
confidence, so the engine knows what to trust.

Lessons already baked into the instructions (from the real Boden job):
  * moss in the joints of a SHADED path = call it HEAVY, don't hedge
  * a path that continues beyond the photo frame = widen the high end
    of the area range (segment photos systematically under-measure)
  * unpriceable != invisible: say what you can't determine

Cost: roughly 1-6 cents per bid depending on photo count.
"""

import json
import re
import base64
import subprocess
import urllib.request
from pathlib import Path

MODEL = "claude-sonnet-5"          # accuracy matters more than pennies here
MAX_PHOTOS = 10                     # sanity cap per analysis
MAX_EDGE_PX = 1400                  # resize target (API limit + cost control)


def _api_key():
    for line in (Path(__file__).parent / ".env").read_text().splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("No ANTHROPIC_API_KEY in .env")


def _prep_image(path):
    """Resize/compress with macOS sips into a temp copy; return base64."""
    tmp = Path("/tmp/vision_prep") / Path(path).name
    tmp.parent.mkdir(exist_ok=True)
    subprocess.run(["sips", "-Z", str(MAX_EDGE_PX), "-s", "formatOptions", "78",
                    str(path), "--out", str(tmp)],
                   capture_output=True)
    src = tmp if tmp.exists() else Path(path)
    return base64.standard_b64encode(src.read_bytes()).decode()


PROMPT = """You are the property assessor for Master Butler, a home services company
(gutter cleaning, roof blow-off, moss treatment, window cleaning, pressure washing).
Analyze these customer/property photos and return ONLY a JSON object — no prose,
no markdown fences. The very first character of your reply must be "{" and the
last must be "}". The JSON must be strictly valid: double-quoted keys, no
trailing commas, no comments. Exactly this shape:

{
 "surfaces": [
   {"type": "driveway|patio|sidewalk|deck|entry",
    "material": "concrete|aggregate|pavers|stone|wood|asphalt",
    "sqft_low": N, "sqft_high": N,
    "reasoning": "one line: what reference objects you measured against",
    "continues_beyond_frame": true/false,
    "confidence": "high|medium|low"}
 ],
 "buildup": {"level": "clean|moderate|heavy", "detail": "one line", "confidence": "..."},
 "roof": {"visible": true/false, "material": "composition|shake|metal|tile|unknown",
          "moss": "none|light|moderate|heavy|unknown", "confidence": "..."},
 "stories": {"value": "1|2|3|unknown", "confidence": "..."},
 "windows": {"visible_count": N, "french_panes": true/false/null,
             "skylights": true/false/null, "confidence": "..."},
 "move_items": {"needed": true/false, "items": ["grill", "planters", ...]},
 "hazards": ["power lines near roof", "AC unit beside path", ...],
 "not_determinable": ["roof pitch", "back of house", ...],
 "services_suggested": ["pw_sidewalk", "moss_treatment", ...]
}

Measurement rules (from real calibration jobs):
- Use reference objects: exterior door ~3 ft wide, garage bay ~8-9 ft,
  siding boards ~6-8 in, concrete control joints often 4-5 ft apart.
- If a walkway/path visibly continues beyond any photo's frame, set
  continues_beyond_frame true AND stretch sqft_high generously — segmented
  photos of long paths under-measure badly.
- HARD RULE, no judgment call: IF you can see moss or algae in the joints
  or edges of any walkway/patio AND that surface is shaded (trees, hedges,
  north side), THEN buildup level MUST be "heavy". Describing joint moss
  and then writing "moderate" is the #1 known error — real jobs priced
  "moderate" on joint-moss photos ran 40% over. When in doubt between
  moderate and heavy, choose heavy.
- If one continuous area contains MULTIPLE materials (e.g. a concrete
  apron meeting an asphalt drive), report EACH material as its own
  surfaces entry with its own sqft — never blend them into one.
- Only report what you can see. Missing things go in not_determinable."""


def analyze_photos(photo_paths, extra_context=""):
    """Run one Vision analysis over up to MAX_PHOTOS images.
    Returns (parsed_dict, cost_estimate_usd)."""
    photos = list(photo_paths)[:MAX_PHOTOS]
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": _prep_image(p)}}
               for p in photos]
    text = PROMPT
    if extra_context:
        clean = re.sub(r"[^\x20-\x7E\n]", " ", extra_context)  # strip mail junk chars
        text += f"\n\nContext from the customer's request: {clean}"
    content.append({"type": "text", "text": text})

    cost = 0.0
    last_err = None
    for attempt in (1, 2):                      # one retry on bad JSON
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": MODEL, "max_tokens": 3000,   # thinking + JSON both fit
                             "messages": [{"role": "user", "content": content}]}).encode(),
            headers={"x-api-key": _api_key(), "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=180))
        if resp.get("error"):
            raise RuntimeError(f"API error: {resp['error']}")

        # take the text block regardless of position (never assume [0]);
        # Sonnet may emit a thinking block first — and if output ran long,
        # the text block can be missing entirely: retry in that case.
        raw = next((b["text"] for b in resp["content"]
                    if b.get("type") == "text"), None)
        if raw is None:
            last_err = RuntimeError("no text block (thinking consumed budget)")
            continue
        raw = raw.strip()
        # keep only the outermost JSON object (tolerates fences/prose)
        if "{" in raw:
            raw = raw[raw.find("{"):raw.rfind("}") + 1]
        # repair the two most common model slips: trailing commas, // comments
        raw = re.sub(r",\s*([}\]])", r"\1", raw)
        raw = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)

        u = resp.get("usage", {})
        cost += (u.get("input_tokens", 0) * 3 + u.get("output_tokens", 0) * 15) / 1e6
        try:
            return json.loads(raw), cost
        except json.JSONDecodeError as e:
            last_err = e                        # retry once, then give up loudly
    raise RuntimeError(f"Vision returned unparseable JSON twice: {last_err}")


def vision_to_prop_fields(v):
    """Translate a Vision result into bid-engine property fields + notes.
    Only fills what Vision saw confidently; everything else stays for
    other data sources (Solar, records) or the office."""
    fields, notes = {}, []

    # surfaces → measured areas (use midpoint; widen note if frame-limited)
    surfaces = {}
    driveway_materials = [s.get("material") for s in v.get("surfaces", [])
                          if s.get("type") == "driveway"]
    all_asphalt_driveway = (driveway_materials
                            and all(m == "asphalt" for m in driveway_materials))
    for s in v.get("surfaces", []):
        key = {"driveway": "driveway", "patio": "patio", "entry": "patio",
               "sidewalk": "sidewalk", "deck": "deck"}.get(s["type"])
        if not key or key == "deck":        # decks stay custom-quote
            continue
        # ASPHALT POLICY (Dallon, July 2026):
        #  * entirely-asphalt driveway → office/custom flag, don't auto-price
        #  * mixed concrete+asphalt → price it ALL, but disclose to customer
        #    (asphalt is softer; some customers choose to skip it)
        if s.get("type") == "driveway" and all_asphalt_driveway:
            notes.append("Driveway is ENTIRELY ASPHALT — office/custom quote "
                         "per policy (softer surface; some customers decline).")
            continue
        if s.get("material") == "asphalt":
            notes.append("CUSTOMER: Part of your driveway is asphalt, which is "
                         "a softer surface than concrete. We're happy to "
                         "include or skip that section — just let us know.")
            notes.append("Driveway includes an asphalt section — priced in "
                         "total; customer may opt out of that portion.")
        mid = int((s["sqft_low"] + s["sqft_high"]) / 2)
        surfaces[key] = surfaces.get(key, 0) + mid
        # PAVERS/COBBLESTONE (Shadi + Boden calibration, July 2026):
        # wand work over joints, no surface cleaner — engine prices the
        # material factor INSTEAD of buildup, never both.
        if s.get("material") in ("pavers", "stone"):
            fields.setdefault("surface_materials", {})[key] = "pavers"
        if s.get("continues_beyond_frame"):
            notes.append(f"{s['type'].title()} continues beyond photo frame — "
                         f"area may exceed {s['sqft_high']} sqft; consider "
                         "aerial cross-check before finalizing.")
    if surfaces:
        fields["surfaces"] = surfaces

    b = v.get("buildup", {})
    if b.get("level"):
        fields["buildup"] = b["level"]

    r = v.get("roof", {})
    if r.get("visible") and r.get("material") not in (None, "unknown"):
        fields["roof_material"] = {"composition": "standard", "shake": "shake",
                                   "metal": "metal_full", "tile": "shake"}.get(
                                       r["material"], "standard")
    s = v.get("stories", {})
    if s.get("value") in ("1", "2", "3") and s.get("confidence") == "high":
        fields["stories"] = s["value"]

    if v.get("move_items", {}).get("needed"):
        items = ", ".join(v["move_items"].get("items", [])) or "items"
        notes.append(f"Add Move Furniture line — visible on site: {items}.")
    for h in v.get("hazards", []):
        notes.append(f"HAZARD (from photos): {h}")
    for nd in v.get("not_determinable", []):
        notes.append(f"Not visible in photos: {nd}")

    return fields, notes


if __name__ == "__main__":
    import sys
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/boden")
    photos = sorted(folder.glob("*.jpg")) + sorted(folder.glob("*.jpeg"))
    print(f"Analyzing {len(photos)} photos from {folder} ...")
    result, cost = analyze_photos(photos)
    print(json.dumps(result, indent=1))
    fields, notes = vision_to_prop_fields(result)
    print("\n→ ENGINE FIELDS:", json.dumps(fields))
    print("→ NOTES:")
    for n in notes:
        print("   •", n)
    print(f"\n[cost ≈ ${cost:.3f}]")
