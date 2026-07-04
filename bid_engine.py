"""
MASTER BUTLER — BID ENGINE (first working version)

This is the "brain" that turns a property's details into a price.
It follows the exact same math as the HTML calculator.

Read it top to bottom like a recipe. Every section has a plain-English note.
"""

import math


# ═════════════════════════════════════════════════════════════
# ★★★  PRICING CONFIG — THE ONE PLACE TO EDIT ALL PRICING  ★★★
#
# Everything in STEP 1 (rates, multipliers, floors, thresholds) can be
# changed right here WITHOUT touching the math below. Change a number,
# save, and the whole system uses the new value everywhere.
#
# This is the foundation for the future "Pricing" screen where the office
# will edit these in boxes instead of in code. Same numbers, prettier door.
# ═════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────
# STEP 1a: THE MULTIPLIER TABLES ("knobs")
# Each condition picks a number that we multiply the price by.
# 1.0 means "no change." 1.35 means "35% more."
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
}
# NOTE: roof type (shake/metal/etc.) is NOT set here anymore. It lives ONLY in
# ROOF_MATERIAL below, so a specialty roof is counted exactly once.

ROOF_MATERIAL = {
    "standard": 1.0,
    "metal_mixed": 1.2,
    "metal_full": 1.35,   # TODO: calibrate against a real metal-roof job
    "shake": 1.8,         # calibrated to real cedar-shake gutter jobs (Jul 2026)
}

ACCESS = {
    "normal": 1.0,
    "tight": 1.1,
}

# ── WINDOW-ONLY KNOBS ──
# Windows have their OWN multipliers. They do NOT use roof pitch or debris,
# because how steep the roof is doesn't change how hard windows are to clean.

WINDOW_STYLE = {
    "standard": 1.0,
    "large": 1.2,      # large / picture windows
    "mixed": 1.25,     # mixed / custom
}

WINDOW_CONDITION = {
    "normal": 1.0,
    "1_2_years": 1.15,   # not cleaned in 1-2 years
    "3_plus_years": 1.35, # 3+ years — also gets an office note
}

WINDOW_ACCESS = {
    "standard": 1.0,
    "some_hard_reach": 1.15,
    "skylights_interior": 1.3,
}

FRENCH_PANE = {
    "none": 1.0,
    "some": 1.35,      # ~25-50% french panes
    "majority": 1.6,   # ~50-75%+ french panes
}


# ─────────────────────────────────────────────────────────────
# STEP 1b: THE BASE RATES (the starting price per sqft, before knobs)
# One base rate per service. Roof difficulty (shake/metal) is handled by the
# ROOF_MATERIAL multiplier above — NOT by a separate rate — so it's only ever
# counted once. Change any number here and every bid uses it automatically.
# ─────────────────────────────────────────────────────────────

RATES = {
    "gutter_cleaning":  0.06,
    "roof_blow_off":    0.02,
    "moss_treatment":   0.015,
    "windows_exterior": 0.07,
    "windows_in_out":   0.13,
}

# Minimum price floors — a service never quotes below this.
PRICE_FLOORS = {
    "roof_blow_off": 50,
    "moss_treatment": 50,
}

# "Get a second opinion" thresholds — bids above these get an office note.
SECOND_OPINION_LIMITS = {
    "gutter_cleaning": 300,
    "roof_blow_off": 150,
    "moss_treatment": 150,
}

# Rounding behavior (matches the calculator). Change these to change how
# every price is rounded and how wide the ±range is.
PRICE_RANGE = 0.10   # ±10% low/high band
ROUND_STEP = 5       # round every price to the nearest $5


# ─────────────────────────────────────────────────────────────
# STEP 2: A HELPER THAT CLEANS UP THE FINAL PRICE
# Your calculator shows a range (±10%) and rounds to the nearest $5.
# This little function does that rounding for us.
# ─────────────────────────────────────────────────────────────

def round_to_5(amount):
    """Match the calculator EXACTLY.

    The calculator doesn't just round the raw price. It builds a small range —
    10% below and 10% above — rounds each end to the nearest $5, then shows the
    MIDDLE of that range (also rounded to $5) as the price.

    We copy that here so our numbers can never drift from the calculator.
    (We round halves UP, the way the calculator's JavaScript does.)
    """
    def r5(x):
        # nearest ROUND_STEP dollars, halves round up
        return math.floor(x / ROUND_STEP + 0.5) * ROUND_STEP
    low = r5(amount * (1 - PRICE_RANGE))
    high = r5(amount * (1 + PRICE_RANGE))
    return r5((low + high) / 2)


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

    # The "base" stack applies to gutters/roof/moss work
    base = stories_mult * pitch_mult * debris_mult * access_mult
    # Gutters and roof work also multiply by gutter type and roof material
    gutter_roof = base * gutter_mult * roof_mult

    # Windows get their OWN stack — stories & access, plus the window knobs.
    # Notice: NO pitch, NO debris here. That's the fix.
    window_style_mult = WINDOW_STYLE[prop["window_style"]]
    window_condition_mult = WINDOW_CONDITION[prop["window_condition"]]
    window_access_mult = WINDOW_ACCESS[prop["window_access"]]
    french_mult = FRENCH_PANE[prop["french_pane"]]
    window_mult = (stories_mult * window_style_mult
                   * window_condition_mult * window_access_mult * access_mult)

    results = []   # we'll collect each service's price here
    notes = []     # and any warnings the office should see

    # ── GUTTER CLEANING ──
    if prop["services"].get("gutters"):
        price = round_to_5(sqft * RATES["gutter_cleaning"] * gutter_roof)
        results.append(("Gutter Cleaning", price))
        if price > SECOND_OPINION_LIMITS["gutter_cleaning"]:
            notes.append("Gutters over $300 — get a second opinion before quoting.")

    # ── ROOF BLOW OFF ──
    if prop["services"].get("roof"):
        price = max(PRICE_FLOORS["roof_blow_off"],
                    round_to_5(sqft * RATES["roof_blow_off"] * gutter_roof))
        results.append(("Roof Blow Off", price))
        if price > SECOND_OPINION_LIMITS["roof_blow_off"]:
            notes.append("Roof blow off over $150 — get a second opinion.")

    # ── MOSS TREATMENT ──
    if prop["services"].get("moss"):
        price = max(PRICE_FLOORS["moss_treatment"],
                    round_to_5(sqft * RATES["moss_treatment"] * gutter_roof))
        results.append(("Moss Treatment", price))
        if price > SECOND_OPINION_LIMITS["moss_treatment"]:
            notes.append("Moss treatment over $150 — get a second opinion.")

    # ── WINDOWS (EXTERIOR ONLY) ──
    # Uses window_mult (not base), then french pane on top.
    if prop["services"].get("windows"):
        price = round_to_5(sqft * RATES["windows_exterior"] * window_mult * french_mult)
        results.append(("Window Cleaning (Exterior Only)", price))

    # ── WINDOWS (IN & OUT) ──
    # Same window scaling, but a higher rate because the tech cleans inside too.
    if prop["services"].get("windows_inout"):
        price = round_to_5(sqft * RATES["windows_in_out"] * window_mult * french_mult)
        results.append(("Windows In & Out", price))

    # Safety check: the calculator lets you pick ONE window service, not both.
    # If both got turned on by mistake, let the office know.
    if prop["services"].get("windows") and prop["services"].get("windows_inout"):
        notes.append("Both window services selected — usually pick just one "
                     "(Exterior Only OR In & Out). Double-check with the office.")

    # ── PITCH & ROOF SAFETY FLAGS ──
    if pitch_mult >= 1.5:
        notes.append("11-12/12 pitch: TOM ONLY. Do not assign to other techs.")
    elif pitch_mult >= 1.35:
        notes.append("9-10/12 pitch: steep — verify who can service before booking.")
    if stories_mult >= 1.35:
        notes.append("3-story property: flag for office review (tech-doability varies).")
    if roof_mult >= 1.35:
        notes.append("Shake or full metal roof: DRY DAY ONLY. Verify with Tom.")
    if window_condition_mult >= 1.35 and (prop["services"].get("windows")
                                          or prop["services"].get("windows_inout")):
        notes.append("Windows not cleaned in 3+ years — consult customer and "
                     "document condition with photos before starting.")

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
        "window_style": "standard",
        "window_condition": "normal",
        "window_access": "standard",
        "french_pane": "none",
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
