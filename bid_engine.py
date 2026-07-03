"""
MASTER BUTLER — BID ENGINE (first working version)

This is the "brain" that turns a property's details into a price.
It follows the exact same math as the HTML calculator.

Read it top to bottom like a recipe. Every section has a plain-English note.
"""


# ─────────────────────────────────────────────────────────────
# STEP 1: THE MULTIPLIER TABLES
# These are the "knobs." Each condition picks a number that we
# multiply the price by. 1.0 means "no change." 1.35 means "35% more."
# These numbers come straight from your HTML calculator.
# ─────────────────────────────────────────────────────────────

STORIES = {
    "1": 1.0,
    "2": 1.0,          # 1 and 2 stories are priced the same
    "3": 1.35,         # 3 stories = always flag for office review
    "3_exp_tech": 1.5, # 3 stories done by an experienced tech
}

PITCH = {
    "mild": 1.0,       # 1-4/12
    "moderate": 1.2,   # 5-8/12
    "steep": 1.35,     # 9-10/12 — check who can service
    "tom_only": 1.5,   # 11-12/12 — Tom only
}

DEBRIS = {
    "minimal": 1.0,
    "moderate": 1.2,
    "heavy": 1.4,
}

GUTTER_TYPE = {
    "standard": 1.0,
    "guards": 1.25,    # includes roof blow-off in one line item
    "specialty": 1.35, # specialty roof
}

ROOF_MATERIAL = {
    "standard": 1.0,
    "metal_mixed": 1.2,
    "metal_full": 1.35,
    "shake": 1.35,
}

ACCESS = {
    "normal": 1.0,
    "tight": 1.1,
}


# ─────────────────────────────────────────────────────────────
# STEP 2: A HELPER THAT CLEANS UP THE FINAL PRICE
# Your calculator shows a range (±10%) and rounds to the nearest $5.
# This little function does that rounding for us.
# ─────────────────────────────────────────────────────────────

def round_to_5(amount):
    """Round a dollar amount to the nearest $5, like the calculator does."""
    return round(amount / 5) * 5


# ─────────────────────────────────────────────────────────────
# STEP 3: THE ACTUAL BID CALCULATION
# We hand this function a "property" (a description of the house)
# and it hands back the prices for each service.
# ─────────────────────────────────────────────────────────────

def calculate_bid(prop):
    sqft = prop["sqft"]

    # Look up each multiplier from the tables above
    stories_mult = STORIES[prop["stories"]]
    pitch_mult = PITCH[prop["pitch"]]
    debris_mult = DEBRIS[prop["debris"]]
    gutter_mult = GUTTER_TYPE[prop["gutter_type"]]
    roof_mult = ROOF_MATERIAL[prop["roof_material"]]
    access_mult = ACCESS[prop["access"]]

    # The "base" stack applies to most services
    base = stories_mult * pitch_mult * debris_mult * access_mult
    # Gutters and roof work also multiply by gutter type and roof material
    gutter_roof = base * gutter_mult * roof_mult

    results = []   # we'll collect each service's price here
    notes = []     # and any warnings the office should see

    # ── GUTTER CLEANING ──
    if prop["services"].get("gutters"):
        # Specialty roof uses a higher base rate ($0.11 vs $0.06)
        rate = 0.11 if gutter_mult >= 1.35 else 0.06
        price = round_to_5(sqft * rate * gutter_roof)
        results.append(("Gutter Cleaning", price))
        if price > 300:
            notes.append("Gutters over $300 — get a second opinion before quoting.")

    # ── ROOF BLOW OFF ──
    if prop["services"].get("roof"):
        rate = 0.03 if gutter_mult >= 1.35 else 0.02
        price = max(50, round_to_5(sqft * rate * gutter_roof))  # $50 minimum floor
        results.append(("Roof Blow Off", price))
        if price > 150:
            notes.append("Roof blow off over $150 — get a second opinion.")

    # ── MOSS TREATMENT ──
    if prop["services"].get("moss"):
        rate = 0.025 if gutter_mult >= 1.35 else 0.015
        price = max(50, round_to_5(sqft * rate * gutter_roof))  # $50 minimum floor
        results.append(("Moss Treatment", price))
        if price > 150:
            notes.append("Moss treatment over $150 — get a second opinion.")

    # ── WINDOWS (EXTERIOR ONLY) ──
    if prop["services"].get("windows"):
        price = round_to_5(sqft * 0.07 * base)
        results.append(("Window Cleaning (Exterior Only)", price))

    # ── PITCH & ROOF SAFETY FLAGS ──
    if pitch_mult >= 1.5:
        notes.append("11-12/12 pitch: TOM ONLY. Do not assign to other techs.")
    elif pitch_mult >= 1.35:
        notes.append("9-10/12 pitch: steep — verify who can service before booking.")
    if stories_mult >= 1.35:
        notes.append("3-story property: flag for office review (tech-doability varies).")
    if roof_mult >= 1.35:
        notes.append("Shake or full metal roof: DRY DAY ONLY. Verify with Tom.")

    return results, notes


# ─────────────────────────────────────────────────────────────
# STEP 4: RUN IT ON AN EXAMPLE HOUSE
# This is the part that actually prints a bid when you run the file.
# Change the numbers here to test different houses.
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Here's our test house. Think of it as filling out the calculator form.
    example_house = {
        "sqft": 2400,
        "stories": "2",
        "pitch": "steep",
        "debris": "moderate",
        "gutter_type": "standard",
        "roof_material": "standard",
        "access": "normal",
        "services": {
            "gutters": True,
            "roof": True,
            "moss": True,
            "windows": True,
        },
    }

    services, notes = calculate_bid(example_house)

    print("=" * 45)
    print("  MASTER BUTLER — BID ESTIMATE")
    print("=" * 45)
    print(f"  Property: {example_house['sqft']} sq ft, "
          f"{example_house['stories']}-story")
    print("-" * 45)

    total = 0
    for name, price in services:
        print(f"  {name:<38} ${price}")
        total += price

    print("-" * 45)
    print(f"  {'TOTAL ESTIMATE':<38} ${total}")
    print("=" * 45)

    if notes:
        print("\n  ⚠ OFFICE NOTES:")
        for note in notes:
            print(f"    • {note}")
