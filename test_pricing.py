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

print("\n── RULE: heavy buildup = 1.4x ──")
check("250 sqft heavy patio", round_to_5(pw_concrete_price(250, "heavy")), 140)

print("\n── RULE: roof-data sanity checks (no network needed) ──")
from property_data import validate_roof, pitch_band

ok, flags, ded = validate_roof(438)            # the real Woodinville shed hit
check("Tiny roof rejected", 0 if ok else 1, 1)
ok, flags, ded = validate_roof(1235, 4212)     # the real Ying Wan mismatch
check("Bad ratio rejected", 0 if ok else 1, 1)
ok, flags, ded = validate_roof(2716, 2200)     # the real Alison (good data)
check("Good roof accepted", 1 if ok else 0, 1)
check("Good roof no deduction", ded, 0)
ok, flags, ded = validate_roof(None)
check("Missing solar deducts 25", ded, 25)

print("\n── RULE: measured pitch maps to the right knob ──")
check("5/12 is moderate", pitch_band(5)[0] == "moderate", True)
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

print("\n" + "=" * 50)
print(f"RESULT: {passed} passed, {failed} failed")
exit(1 if failed else 0)
