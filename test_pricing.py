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


print("── ANCHOR 1: The original comp test house = $545 ──")
r, _, _ = calculate_bid(house(services={"gutters": True, "roof": True,
                                        "moss": True, "windows": True}))
check("Comp house total", sum(s["price"] for s in r), 545)

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

print("\n" + "=" * 50)
print(f"RESULT: {passed} passed, {failed} failed")
exit(1 if failed else 0)
