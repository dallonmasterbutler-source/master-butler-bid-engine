"""
MASTER BUTLER — PRICING SAFETY NET

Every test here pins a price to a REAL job or real Jobber tier.
If a future change accidentally shifts pricing, these tests fail loudly
instead of letting wrong bids go out silently.

Run with:  python3 test_pricing.py
"""

from bid_engine import calculate_bid, pw_concrete_price, round_to_5


def house(**overrides):
    """A standard test property; override any field per test."""
    base = {
        "sqft": 2400, "stories": "2", "pitch": "steep", "debris": "moderate",
        "gutter_type": "standard", "roof_material": "standard", "access": "normal",
        "window_style": "standard", "window_condition": "normal",
        "window_access": "standard", "french_pane": "none",
        "services": {}, "surfaces": {},
    }
    base.update(overrides)
    return base


def line(results, name_part):
    """Find one service line by (partial) name."""
    for s in results:
        if name_part.lower() in s["name"].lower():
            return s
    return None


passed = failed = 0

def check(label, actual, expected, tolerance=0):
    global passed, failed
    ok = abs(actual - expected) <= tolerance
    status = "✅" if ok else "❌"
    print(f"{status} {label}: got {actual}, expected {expected}"
          + (f" (±{tolerance})" if tolerance else ""))
    if ok: passed += 1
    else: failed += 1


print("── ANCHOR 1: The original comp test house ──")
# Calculator truth was $545 (gutter line $235). Office practice floors
# gutter cleaning at $250 → $560 under the CURRENT provisional minimum.
# If Tom approves dropping the min, this reverts to 545.
r, notes1, _ = calculate_bid(house(services={"gutters": True, "roof": True,
                                             "moss": True, "windows": True}))
check("Comp house total (w/ $250 gutter min)", sum(s["price"] for s in r), 560)
check("Gutter-min note explains the bump",
      1 if any("service minimum" in n for n in notes1) else 0, 1)
from bid_engine import GUTTER_CLEANING_MINIMUM
check("Gutter min is the office's current $250 (pending Tom)",
      GUTTER_CLEANING_MINIMUM, 250)

print("\n── ANCHOR 2: Real shake job (Sammamish, charged $350) ──")
r, _, _ = calculate_bid(house(sqft=2200, stories="1", pitch="moderate",
                              roof_material="shake",
                              services={"gutters": True}))
check("Shake gutter clean", line(r, "gutter")["price"], 345, tolerance=10)

print("\n── ANCHOR 3: Real patio invoice (Connie, 250 sqft = $100) ──")
check("250 sqft patio", round_to_5(pw_concrete_price(250)), 100)

print("\n── ANCHOR 4: Jobber concrete tiers ──")
check("100 sqft (tier $60)", round_to_5(pw_concrete_price(100)), 60, tolerance=5)
check("400 sqft (tier $120)", round_to_5(pw_concrete_price(400)), 120, tolerance=5)
check("900 sqft (tier $190)", round_to_5(pw_concrete_price(900)), 190, tolerance=10)

print("\n── RULE: gutter guards = ONE combined line, no separate blow-off ──")
r, notes, _ = calculate_bid(house(gutter_type="guards",
                                  services={"gutters": True, "roof": True}))
check("Only one line item", len(r), 1)
check("Combined line exists", 1 if line(r, "guards") else 0, 1)

print("\n── RULE: dryer vent $100 as add-on, $150 alone ──")
r, _, _ = calculate_bid(house(services={"gutters": True, "dryer_vent": True}))
check("Add-on price", line(r, "dryer")["price"], 100)
r, _, _ = calculate_bid(house(services={"dryer_vent": True}))
check("Alone price", line(r, "dryer")["price"], 150)

print("\n── RULE: PW with no measurement → flagged, never guessed ──")
r, notes, conf = calculate_bid(house(services={"patio": True}))  # no surfaces given
check("No priced patio line", 1 if line(r, "patio") is None else 0, 1)
check("Flag note present", 1 if any("NO measurement" in n for n in notes) else 0, 1)
check("Confidence dropped", 1 if conf < 80 else 0, 1)

print("\n── RULE: heavy buildup = 1.5x (Boden tech-grade calibration) ──")
check("250 sqft heavy patio", round_to_5(pw_concrete_price(250, "heavy")), 150)

print("\n── RULE: roof-data sanity checks (no network needed) ──")
from property_data import validate_roof, pitch_band

ok, flags, ded = validate_roof(438)            # the real Woodinville shed hit
check("Tiny roof rejected", 0 if ok else 1, 1)
ok, flags, ded = validate_roof(1235, 4212)     # the real Ying Wan mismatch
check("Bad ratio rejected", 0 if ok else 1, 1)
ok, flags, ded = validate_roof(2716, 2200)     # the real Alison (good data)
check("Good roof accepted", 1 if ok else 0, 1)
check("Good roof no deduction", ded, 0)
ok, flags, ded = validate_roof(4146)           # the real Gavin outbuilding grab
check("Big roof w/o records distrusted", ded, 15)
check("Outbuilding flag present", 1 if any("outbuilding" in f for f in flags) else 0, 1)
ok, flags, ded = validate_roof(3508)           # Shane's real legit roof
check("Shane-size roof still trusted", ded, 0)
ok, flags, ded = validate_roof(None)
check("Missing solar deducts 25", ded, 25)

print("\n── RULE: measured pitch maps to the right knob ──")
check("5/12 rounds DOWN to mild (grace zone)", pitch_band(5)[0] == "mild", True)
check("5/12 carries verify flag", 1 if pitch_band(5)[1] else 0, 1)
check("6/12 is moderate", pitch_band(6)[0] == "moderate", True)
check("10.5/12 does NOT round down (safety)", pitch_band(10.5)[0] == "tom_only", True)
check("9/12 is steep", pitch_band(9)[0] == "steep", True)
check("11/12 is TOM ONLY", pitch_band(11.5)[0] == "tom_only", True)

print("\n── RULE: 3-story stays flagged no matter what (SAFETY) ──")
r, notes, conf = calculate_bid(house(stories="3", services={"gutters": True}))
check("3-story office flag present",
      1 if any("3-story" in n for n in notes) else 0, 1)

print("\n── RULE: $150 job minimum (Dallon's floor) ──")
r, notes, _ = calculate_bid(house(sqft=800, stories="1", pitch="mild",
                                  debris="minimal", services={"moss": True}))
check("Small job bumped to $150", sum(s["price"] for s in r), 150)
check("Minimum note present",
      1 if any("minimum" in n.lower() for n in notes) else 0, 1)

print("\n── ANCHOR 6: Dallon's own home (guards blow-off, low pitch = $250, ~1h) ──")
r, notes, _ = calculate_bid(house(sqft=3368, stories="2", pitch="mild",
                                  debris="moderate", gutter_type="guards",
                                  services={"gutters": True}))
g = line(r, "guards")
check("Guards blow-off near \$250", g["price"], 250, tolerance=15)
check("Hours near 1h (blower job)", 1 if 0.5 <= g["hours"] <= 1.3 else 0, 1)

print("\n── RULE: lights + gutter guards = DECLINE ──")
r, notes, _ = calculate_bid(house(gutter_type="guards",
                                  services={"holiday_lights": True}))
check("Decline note present",
      1 if any("DECLINE LIGHTS" in n for n in notes) else 0, 1)

print("\n── RULE: seasonal scheduling notes ──")
import datetime
r, notes, _ = calculate_bid(house(request_date=datetime.date(2026, 10, 20),
                                  services={"gutters": True, "windows": True}))
check("Light-season gutter push note",
      1 if any("LIGHT SEASON" in n for n in notes) else 0, 1)
check("Winter window suspension note",
      1 if any("WINTER SUSPENSION" in n for n in notes) else 0, 1)
r, notes, _ = calculate_bid(house(request_date=datetime.date(2026, 6, 1),
                                  services={"gutters": True, "windows": True}))
check("No seasonal notes in June",
      1 if not any("SEASON" in n or "SUSPENSION" in n for n in notes) else 0, 1)

print("\n── ANCHOR 5: Real Boden job (aggregate concrete, heavy moss, ~600sqft) ──")
# Dallon charged $215; on-site tech's FINAL grade was $230 (not ~$300).
# heavy=1.5 prices this at $229 — the tech grade is the anchor.
r, notes, _ = calculate_bid(house(buildup="heavy",
                                  services={"patio": True, "sidewalk": True},
                                  surfaces={"patio": 200, "sidewalk": 400}))
total = sum(s["price"] for s in r)
check("Combined-visit pricing near tech's $230", total, 230, tolerance=10)
check("Setup-once note present",
      1 if any("setup priced once" in n for n in notes) else 0, 1)

print("\n── RULE: >\$1,100 exceeds a tech-day → split flag ──")
r, notes, _ = calculate_bid(house(sqft=6000, services={"gutters": True, "windows_inout": True}))
big_total = sum(s["price"] for s in r)
check("Big job flagged for split", 1 if (big_total > 1100) == any("exceeds one tech-day" in n for n in notes) else 0, 1)
r, notes, _ = calculate_bid(house(services={"gutters": True}))
check("Normal job NOT flagged", 0 if any("exceeds one tech-day" in n for n in notes) else 1, 1)

print("\n── RULE: asphalt policy (Dallon's home lesson) ──")
from vision import vision_to_prop_fields
all_asphalt = {"surfaces": [{"type": "driveway", "material": "asphalt",
                             "sqft_low": 500, "sqft_high": 700}]}
f, n = vision_to_prop_fields(all_asphalt)
check("All-asphalt driveway NOT auto-priced", 0 if f.get("surfaces", {}).get("driveway") else 1, 1)
check("All-asphalt office flag", 1 if any("ENTIRELY ASPHALT" in x for x in n) else 0, 1)
mixed = {"surfaces": [{"type": "driveway", "material": "concrete", "sqft_low": 400, "sqft_high": 500},
                      {"type": "driveway", "material": "asphalt", "sqft_low": 200, "sqft_high": 300}]}
f, n = vision_to_prop_fields(mixed)
check("Mixed driveway fully priced (both areas)", f["surfaces"]["driveway"], 700)
check("Customer disclosure present", 1 if any(x.startswith("CUSTOMER:") for x in n) else 0, 1)

print("\n── RULE: Jobber safety dropdowns auto-fill (exact office strings) ──")
from jobber_client import safety_options, MUST_UPDATE
g, r = safety_options("mild", "standard", "2")
check("Easy home = employee", 1 if g == "Employee can service gutters" and r == "Employee can service roof " else 0, 1)
g, r = safety_options("tom_only", "standard", "2")
check("11-12/12 = Tom (exact double-space string)", 1 if g == "Only Tom can service  gutters " else 0, 1)
g, r = safety_options("steep", "shake", "2")
check("Steep+shake = Must be updated (human call)", 1 if g == MUST_UPDATE and r == MUST_UPDATE else 0, 1)
g, r = safety_options("mild", "shake", "2")
check("Mild shake = Exp Tech Dry Day", 1 if g == "Experienced Technician, Dry Day" else 0, 1)
g, r = safety_options("moderate", "standard", "3")
check("3-story = Must be updated (office assigns)", 1 if g == MUST_UPDATE else 0, 1)

print("\n── ANCHOR 7: pavers/cobblestone factor (Shadi patio + Boden recheck) ──")
# Shadi Mosleh (Bothell, July 2026): small paver patio, Dallon's hour-check
# said ~$140 for ~1h of wand work. ~235 sqft pavers, no buildup stacking.
r, notes, _ = calculate_bid(house(pitch="mild",
                                  services={"patio": True},
                                  surfaces={"patio": 235},
                                  surface_materials={"patio": "pavers"}))
check("Shadi paver patio ≈ $140", sum(s["price"] for s in r), 140, tolerance=10)
check("Pavers note present",
      1 if any("Pavers" in n for n in notes) else 0, 1)
# No-stack rule: heavy buildup on a paver surface charges max(1.5, 1.5),
# never 1.5 × 1.5 — same price as heavy alone, one honest factor.
r, notes, _ = calculate_bid(house(pitch="mild", buildup="heavy",
                                  services={"patio": True, "sidewalk": True},
                                  surfaces={"patio": 200, "sidewalk": 400},
                                  surface_materials={"patio": "pavers",
                                                     "sidewalk": "pavers"}))
check("Pavers + heavy never stack (600 sqft = ~$230, not ~$345)",
      sum(s["price"] for s in r), 230, tolerance=10)
check("No double-charge: heavy note suppressed when pavers factor covers it",
      0 if any("HEAVY buildup priced in" in n for n in notes) else 1, 1)

print("\n── RULE: trees → debris (Connor's question — proximity only) ──")
# canopy ON the roof = heavy debris charge
crowded = {"trees": {"visible": True, "canopy_over_roof": "heavy",
                     "mature_trees_within_20ft": "4_plus",
                     "detail": "conifers overhanging roofline",
                     "confidence": "high"}}
f, n = vision_to_prop_fields(crowded)
check("Canopy over roof = heavy debris", 1 if f.get("debris") == "heavy" else 0, 1)
check("Office confirm note attached",
      1 if any("confirm from photo" in x for x in n) else 0, 1)
# Dallon's own home: 4+ conifers NEAR but none over roof = NO auto-charge
dallons = {"trees": {"visible": True, "canopy_over_roof": "none",
                     "mature_trees_within_20ft": "4_plus",
                     "detail": "conifers ring the yard",
                     "confidence": "high"}}
f, n = vision_to_prop_fields(dallons)
check("Trees near but not over roof = NO auto-heavy (Dallon's home truth)",
      0 if f.get("debris") else 1, 1)
check("Office-may-bump note instead",
      1 if any("heavy droppers" in x for x in n) else 0, 1)
# "some tree coverage, not that close" = normal, NO upcharge
scenic = {"trees": {"visible": True, "canopy_over_roof": "none",
                    "mature_trees_within_20ft": "0",
                    "detail": "tree line at back fence",
                    "confidence": "high"}}
f, n = vision_to_prop_fields(scenic)
check("Distant tree line = NO debris change", 0 if f.get("debris") else 1, 1)
# low confidence = touch nothing
unsure = {"trees": {"visible": True, "canopy_over_roof": "heavy",
                    "mature_trees_within_20ft": "unknown",
                    "confidence": "low"}}
f, n = vision_to_prop_fields(unsure)
check("Low-confidence tree read changes nothing", 0 if f.get("debris") else 1, 1)

print("\n── RULE: tax auto-attach (flag-don't-guess) ──")
from jobber_client import match_tax_rate
# offline fixture mirroring the office's real Jobber rate names
TAX_FIXTURE = [{"id": i, "name": n, "label": n, "tax": 0, "default": False}
               for i, n in enumerate([
                   "Monroe 3112", "Everett 3105", "Gold Bar 3106",
                   "Bellevue 1704", "Bellevue Non-RTA 4004",
                   "Bothell King Co 1706", "Bothell Sno Co 3120",
                   "Snohomish 4231", "Snohomish City 3115",
                   "Snohomish Co. 3100", "Mountlake Terrce 3113"])]
rate, note = match_tax_rate("Monroe", "98272", rates=TAX_FIXTURE)
check("Monroe = one rate, auto-attached",
      1 if rate and rate["name"] == "Monroe 3112" else 0, 1)
rate, note = match_tax_rate("Bothell", "98012", rates=TAX_FIXTURE)
check("Bothell 98012 resolved by ZIP to Sno Co",
      1 if rate and rate["name"] == "Bothell Sno Co 3120" else 0, 1)
rate, note = match_tax_rate("Snohomish", "98290", rates=TAX_FIXTURE)
check("Snohomish ambiguous = NOT set, office picks",
      1 if rate is None and note and "multiple rates" in note else 0, 1)
rate, note = match_tax_rate("Bellevue", "98004", rates=TAX_FIXTURE)
check("Bellevue RTA/non-RTA = NOT set, office picks",
      1 if rate is None and note and "multiple rates" in note else 0, 1)
rate, note = match_tax_rate("Mountlake Terrace", None, rates=TAX_FIXTURE)
check("Office's 'Terrce' spelling still matches",
      1 if rate and rate["name"] == "Mountlake Terrce 3113" else 0, 1)
rate, note = match_tax_rate("Portland", None, rates=TAX_FIXTURE)
check("Unknown city = NOT set + flag", 1 if rate is None and note else 0, 1)

print("\n── RULE: aerial cross-check (flag-don't-guess, offline) ──")
from aerial import cross_check, _imagery_year
from pathlib import Path as _P
check("Solar tile year parsed",
      _imagery_year(_P("x-solar-201307.png")) or 0, 2013)
check("Static tile = current (no year)",
      0 if _imagery_year(_P("x-z20.png")) is None else 1, 0)
GAVIN_READING = {
    "main_roof": {"visible": True, "footprint_sqft_low": 2400,
                  "footprint_sqft_high": 2900, "confidence": "medium"},
    "other_buildings": [{"kind": "outbuilding", "relative_size": "smaller",
                         "position": "north of house"}],
    "canopy_over_roof": {"level": "partial", "detail": "one maple corner",
                         "confidence": "medium"},
    "mature_trees_within_20ft": {"count_band": "1-3", "types": "deciduous",
                                 "confidence": "medium"},
    "surfaces": [{"type": "driveway", "sqft_low": 700, "sqft_high": 900,
                  "confidence": "medium"}],
}
prop = {"surfaces": {"driveway": 400}, "services": {"driveway": True}}
f, n = cross_check(prop, "test", _reading=GAVIN_READING,
                   _tile="t-solar-202407.png")
check("Outbuilding = wrong-building flag",
      1 if any("other structures" in x for x in n) else 0, 1)
check("Area disagreement flagged (800 aerial vs 400 ground)",
      1 if any("office verify" in x for x in n) else 0, 1)
check("Partial canopy = note only, no auto-heavy",
      0 if f.get("debris") else 1, 1)
f, n = cross_check(prop, "test", _reading=GAVIN_READING,
                   _tile="t-solar-201307.png")
check("Stale imagery = tree reads skipped",
      1 if any("too old to trust" in x for x in n) else 0, 1)
heavy_reading = dict(GAVIN_READING,
                     canopy_over_roof={"level": "heavy", "detail": "cedars",
                                       "confidence": "high"})
f, n = cross_check(prop, "test", _reading=heavy_reading,
                   _tile="t-z20.png")
check("Fresh heavy canopy = debris raised",
      1 if f.get("debris") == "heavy" else 0, 1)

print("\n── RULE: street-view second opinion (conservative merge, offline) ──")
from aerial import street_check
CURB = {"stories": {"value": "2", "confidence": "high"},
        "pitch_looks": {"value": "moderate", "confidence": "medium"},
        "roof_material": {"value": "composition", "confidence": "medium"},
        "french_panes": True}
# Dallon's home truth: 2 stories / mild — 1 pitch band apart = stay QUIET
f, n = street_check({"stories": "2", "pitch": "mild",
                     "roof_material": "standard"}, "", _reading=CURB)
check("1-band pitch gap = no false alarm",
      0 if any("OVERCALL" in x or "UNDERCALL" in x for x in n) else 1, 1)
check("Matching stories = no flag",
      0 if any("OFFICE VERIFY" in x for x in n) else 1, 1)
check("Gridded windows = note only, NEVER auto-premium (Dallon's home)",
      0 if f.get("french_pane") else 1, 1)
# 2-band gap = the overcall flag fires (data says steep, curb sees mild)
MILD_CURB = dict(CURB, pitch_looks={"value": "mild", "confidence": "high"})
f, n = street_check({"stories": "2", "pitch": "steep",
                     "roof_material": "standard"}, "", _reading=MILD_CURB)
check("Steep-vs-mild-looking = OVERCALL flag",
      1 if any("OVERCALL" in x for x in n) else 0, 1)
# stories disagreement = flag, never silent fix
f, n = street_check({"stories": "3", "pitch": "moderate",
                     "roof_material": "standard"}, "", _reading=CURB)
check("Stories disagreement = flagged, not changed",
      1 if any("OFFICE VERIFY" in x for x in n) and not f.get("stories") else 0, 1)
# shake seen on a 'standard' record = adopt the CAUTIOUS direction
shake = dict(CURB, roof_material={"value": "shake", "confidence": "high"})
f, n = street_check({"stories": "2", "pitch": "mild",
                     "roof_material": "standard"}, "", _reading=shake)
check("Shake spotted = specialty pricing adopted",
      1 if f.get("roof_material") == "shake" else 0, 1)

print("\n── RULE: reconciler discount taxonomy ──")
from reconciler import classify_discount
check("'honor 2026 pricing' = honor",
      1 if classify_discount("Discount honor 2026 pricing, 2027 will be $325") == "honor" else 0, 1)
check("'not performed' = service_not_performed",
      1 if classify_discount("Moss treatment not performed - roof too wet") == "service_not_performed" else 0, 1)
check("'15%' promo = promo",
      1 if classify_discount("February or March Discount 15% two services") == "promo" else 0, 1)
check("Unrecognized = other_discount",
      1 if classify_discount("Discount to offset tax not being added") == "other_discount" else 0, 1)
check("honor wins even if % present",
      1 if classify_discount("honor 2026 pricing 10% adjustment") == "honor" else 0, 1)
from reconciler import parse_next_year_price
check("Promise parsed ('next year will be 350')",
      parse_next_year_price("honor 2026 gutter rate next year will be 350") or 0, 350)
check("Year is NOT a price ('free in 2025, charge full next year')",
      parse_next_year_price("Pavers done for free in 2025. Charge full next year.") or 0, 0)
check("Tiny numbers rejected (15% is not $15)",
      parse_next_year_price("honor 2026 pricing, 15 percent adjustment") or 0, 0)

print("\n── RULE: duplicates are PROPERTY-aware (the realty lesson) ──")
from datetime import datetime as _dt
from dedup import check_duplicate, normalize_phone
realty_1 = {"sender_email": "office@realty.com", "phone": "(425) 555-0100",
            "address": "100 Main St, Monroe, WA 98272",
            "thread_id": None, "received": _dt(2026, 7, 1)}
realty_2 = {"sender_email": "office@realty.com", "phone": "425-555-0100",
            "address": "200 Pine Ave, Everett, WA 98208",
            "thread_id": None, "received": _dt(2026, 7, 3)}
v = check_duplicate(realty_2, [realty_1])
check("Realty: same client + DIFFERENT house = multi_property, NOT duplicate",
      1 if v["verdict"] == "multi_property" else 0, 1)
same_again = {"sender_email": "office@realty.com", "phone": None,
              "address": "100 main street monroe wa 98272",
              "thread_id": None, "received": _dt(2026, 7, 2)}
v = check_duplicate(same_again, [realty_1])
check("Same client + SAME house = duplicate",
      1 if v["verdict"] == "suspected_duplicate" else 0, 1)
phone_only = {"sender_email": "personal@gmail.com", "phone": "+1 425.555.0100",
              "address": "100 Main St, Monroe, WA 98272",
              "thread_id": None, "received": _dt(2026, 7, 2)}
v = check_duplicate(phone_only, [realty_1])
check("Different email, SAME phone + house = duplicate (phone catches it)",
      1 if v["verdict"] == "suspected_duplicate" else 0, 1)
check("Phone normalizing (+1 425.555.0100 = (425) 555-0100)",
      1 if normalize_phone("+1 425.555.0100") == normalize_phone("(425) 555-0100") else 0, 1)
spouse = {"sender_email": "spouse@other.com", "phone": "360-555-9999",
          "address": "100 main st monroe wa 98272",
          "thread_id": None, "received": _dt(2026, 7, 2)}
v = check_duplicate(spouse, [realty_1])
check("Different contact, same house = still linked (spouse case)",
      1 if v["verdict"] == "suspected_duplicate" else 0, 1)

print("\n── RULE: queue hygiene (Dallon/Tom/robots -> drawer, never dropped) ──")
from dashboard import classify_row
lane, why = classify_row({"from": "Dallon Anderson <dallon.masterbutler@gmail.com>",
                          "kind": "question"})
check("Dallon's questions leave the queue", 1 if lane == "aside" else 0, 1)
lane, why = classify_row({"from": "inc Master Butler <customercare@masterbutlerinc.com>",
                          "kind": "new_request"})
check("Company-address mail leaves the queue", 1 if lane == "aside" else 0, 1)
lane, why = classify_row({"from": "The Jobber Team <marketing@getjobber.com>",
                          "kind": "scheduling"})
check("Robot mail leaves the queue", 1 if lane == "aside" else 0, 1)
lane, why = classify_row({"from": "Shadi Mosleh <shadimoslehy@gmail.com>",
                          "kind": "new_request"})
check("Customer request stays in the queue", 1 if lane == "main" else 0, 1)
lane, why = classify_row({"from": "Jane Doe <jane@x.com>", "kind": "question"})
check("Customer QUESTION stays (office answers those)",
      1 if lane == "main" else 0, 1)
lane, why = classify_row({"from": "Spammer <win@lottery.biz>", "kind": "other",
                          "folder": "[Gmail]/Spam"})
check("Spam junk goes to the drawer", 1 if lane == "aside" else 0, 1)
lane, why = classify_row({"from": "Kimila B <kimila@yahoo.com>",
                          "kind": "other", "folder": "INBOX"})
check("Outside human with kind 'other' STAYS in queue (never hide a customer)",
      1 if lane == "main" else 0, 1)

print("\n── RULE: phone leads (CopyCall voicemails = call back, not email) ──")
from email_parser import parse_phone_lead
REAL_COPYCALL = ("We just wanted to let you know you were just left a 1:00 "
                 "long message (number 201) in mailbox 4252221063 from "
                 "12069738356, on Tuesday, July 07, 2026 at 05:19:45 PM, so "
                 "you might want to check it when you get a chance.  Thanks!")
lead = parse_phone_lead(REAL_COPYCALL)
check("Caller number extracted", 1 if lead and lead["caller"] == "(206) 973-8356" else 0, 1)
check("Duration extracted", 1 if lead and lead["duration"] == "1:00" else 0, 1)
check("No number = no lead (never a fake)",
      1 if parse_phone_lead("check your voicemail sometime") is None else 0, 1)
lane, _ = classify_row({"from": "☎ Voicemail from (206) 973-8356 "
                        "<messages@copycall.com>", "kind": "phone_lead"})
check("Phone lead rides the MAIN queue", 1 if lane == "main" else 0, 1)

print("\n── RULE: office↔system service-name bridge (offline) ──")
from store import _service_key
check("'Gutter Cleaning - Composition' bridges to gutter",
      1 if _service_key("Gutter Cleaning - Composition") == "gutter" else 0, 1)
check("'Roof Blow Off - Composition' bridges to roof blow",
      1 if _service_key("Roof Blow Off - Composition") == "roof blow" else 0, 1)
check("'Moss Treatment Product' bridges to moss",
      1 if _service_key("Moss Treatment Product") == "moss" else 0, 1)
check("Unknown line = no bridge (never mismatched)",
      1 if _service_key("Mystery Fee") is None else 0, 1)

print("\n── RULE: lights materials math (labor stays Tom's) ──")
from lights import materials_estimate, C7_PER_FT, LABOR_MINIMUM
est = materials_estimate(50, 60, 180, 220)
check("Front 50ft materials = $72.50", est["front_materials"][0], 72.5)
check("Perimeter 220ft materials = $319", est["perimeter_materials"][1], 319)
check("C7 rate locked at $1.45/ft", C7_PER_FT, 1.45)
check("Labor floor stays Tom's $385", LABOR_MINIMUM, 385)
check("Labor is never a number here",
      1 if "Tom" in est["labor"] else 0, 1)

print("\n── RULE: price promises kept (fuzzy name match, offline) ──")
from promises import promises_for
FAKE_RECON = [{"invoice": "1", "date": "2026-05-07", "client": "Carol & Michael  Ross",
               "next_year_price": 350.0,
               "discounts": [{"text": "honor 2026 gutter rate next year will be 350",
                              "next_year_price": 350.0}]},
              {"invoice": "2", "date": "2026-01-01", "client": "Someone Else",
               "next_year_price": None, "discounts": []}]
check("'Carol Ross' finds 'Carol & Michael Ross' promise",
      promises_for("Carol Ross", _records=FAKE_RECON)[0]["promised_price"], 350)
check("Stranger finds nothing",
      len(promises_for("Random Stranger", _records=FAKE_RECON)), 0)
check("Empty name finds nothing", len(promises_for("", _records=FAKE_RECON)), 0)

print("\n" + "=" * 50)
print(f"RESULT: {passed} passed, {failed} failed")
exit(1 if failed else 0)
