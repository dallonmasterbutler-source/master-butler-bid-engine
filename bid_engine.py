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
    "guards_blow_off":  0.06,   # dedicated Jobber SKU: blow-off over installed
                                # guards — a fast blower job, NOT gutter
                                # cleaning x1.25 (calibrated on Dallon's own
                                # home: $250, 45-60 min)
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
# STEP 1c: PRESSURE WASHING (sqft-based — calibrated July 2026)
# Model fits ALL our real anchors: Connie's 250 sqft patio = $100,
# and the Jobber tiers 100→$60, 400→$120, 900→$190.
# "Declining block": the first 250 sqft costs more per-foot than the rest,
# because setup/haul time is baked into every job no matter how small.
# ─────────────────────────────────────────────────────────────

PW_CONCRETE = {
    "first_block_sqft": 250,    # the "setup-heavy" portion
    "first_block_rate": 0.40,   # $/sqft for the first 250 sqft
    "remainder_rate":   0.15,   # $/sqft beyond that
    "minimum":          60,     # never quote below this
}

PW_HOUSE_WASH_RATE = 0.20       # $/sqft × stories (from calculator)

# Surface buildup — the "heavy debris" exception Dallon called out.
# heavy recalibrated 1.4→1.5 on Boden's final tech grade ($230 for 600 sqft
# heavy moss on aggregate concrete — 1.4 was ~7% light).
PW_BUILDUP = {
    "clean":    1.0,
    "moderate": 1.2,
    "heavy":    1.5,   # also triggers a confirm-with-photo flag
}

# Surface MATERIAL — pavers/cobblestone are wand work over joints, no
# surface cleaner: slower everywhere (Shadi patio + Boden calibration,
# July 2026 — Dallon's hour-check: ~1h of paver patio ≈ $140).
# RULE: material and buildup do NOT stack — growth in paver joints is
# just what pavers look like; the material factor already covers the
# slow work. We charge whichever factor is LARGER, never both.
PW_MATERIAL = {
    "concrete": 1.0,
    "asphalt":  1.0,    # priced same as concrete (opt-out policy elsewhere)
    "pavers":   1.5,    # includes cobblestone / brick
}

# Target hourly rates (price ÷ rate = estimated job hours, feeds pathing)
TARGET_HOURLY = {
    "gutters": 125, "gutters_specialty": 150, "roof": 150,
    "moss": 150, "windows": 100, "pressure": 115,
    "guards_blow_off": 300,   # blower work: Pricing doc says 200-400/hr
}

# Flat-fee services
DRYER_VENT_ADDON = 100   # when booked with another service
DRYER_VENT_ALONE = 150   # when booked by itself
WET_DAY_GUTTER_MULT = 1.3  # gutters cost more on wet days (wet debris)

# No visit goes out below this, period (Dallon's floor — drive time,
# setup, and insurance make anything smaller a money-loser).
JOB_MINIMUM = 150
GUTTER_CLEANING_MINIMUM = 175   # Tom ruled Jul 8 (was $250) — lower easy
                                # gutter jobs to WIN more of them
# Windows floors (Tom Jul 8 + Dallon refinement): every window job is
# AT LEAST an hour of work. Solo window visit = $200 floor. Windows in
# a MULTI-SERVICE visit = $175 floor (ladders already out, but the hour
# is still real). History says windows are our #1 quiet underbid.
WINDOWS_MINIMUM = 200            # windows-only visit
WINDOWS_MINIMUM_BUNDLED = 175    # windows within a multi-service visit

# ── TOM'S DRY-SEASON ROOF-LANE RULES (Jul 8 call) ──
# The roof lane = gutters + roof blow-off + moss treatment.
# · Gutters-only OR gutters+blow-off over this = flag for Tom review
#   (on a dry day he may do it cheaper — e.g. Bellevue $690 → $500 dry).
ROOF_LANE_REVIEW_OVER = 500
# · In dry season Tom won't roll out for a roof-lane visit under this
#   (gutters+blow-off+moss). Flagged, not auto-bumped — office bundles
#   or holds for a fuller day.
DRY_SEASON_ROOF_FLOOR = 400
# · TWO-PRICE (Tom, Jul 8): a high roof-lane price the customer balks at
#   also gets a DRY-DAY price offered in exchange for flexible timing.
#   Tom's logic: "a 2-hour job is worth $500; 3-4 of those a day is well
#   worth it" (~$250/hr). That back-solves to ~27% off the standard
#   roof lane — which lands his Bellevue example ($690 std → ~$500 dry).
#   The STANDARD price stays the true price (learning ground truth); the
#   dry-day price is a scheduling concession, never a rate cut, floored
#   so it's always worth the roll-out.
DRY_DAY_DISCOUNT = 0.27

# ── SCHEDULING BY DOLLARS (Dallon's rule, July 2026) ──
# The schedule blocks time by job dollars (price is a time proxy: ~$400 job
# ≈ 4-hour block). A tech-day is booked to $850–$1,100 of work; anything
# over $1,100 is too much for one tech to finish in a day.
DAY_CAPACITY = {"day_low": 850, "day_high": 1100}

# ─────────────────────────────────────────────────────────────
# STEP 1d: SEASONAL RULES (from office training docs, July 2026)
# Light season runs mid-Sept through December and OWNS the schedule.
# ─────────────────────────────────────────────────────────────

# DATES ARE DEFAULTS, NOT LAWS — the office adjusts these year to year
# (weather shifts, light volume varies). Change them here; nothing else
# in the code needs touching.
SEASONS = {
    # (start_month, start_day), (end_month, end_day)
    "light_season":   ((9, 15), (12, 31)),   # lights take priority, always
    "winter_freeze":  ((10, 15), (2, 28)),   # washers WINTERIZED (physical,
                                             # not policy) + windows paused;
                                             # both reopen together ~late Feb
}


def seasonal_notes(when, services):
    """Office scheduling rules by date. Returns notes; never blocks a bid —
    the office decides, but the system must SAY it so weird bids don't slip."""
    notes = []
    m, d = when.month, when.day

    def in_window(start, end):
        (sm, sd), (em, ed) = start, end
        if (sm, sd) <= (em, ed):                       # same-year window
            return (sm, sd) <= (m, d) <= (em, ed)
        return (m, d) >= (sm, sd) or (m, d) <= (em, ed)  # wraps New Year

    light_season = in_window(*SEASONS["light_season"])
    winter = in_window(*SEASONS["winter_freeze"])

    if light_season:
        if services.get("holiday_lights"):
            notes.append("LIGHT SEASON: holiday lights take scheduling "
                         "priority — book first.")
        if services.get("gutters") or services.get("roof") or services.get("moss"):
            notes.append("LIGHT SEASON: add to the STANDBY GUTTERS backlog — "
                         "these jobs fill low light weeks or December. "
                         "No gutter cleaning on homes with lights installed.")
    if winter and any(services.get(s) for s in
                      ("windows", "windows_inout", "patio", "driveway",
                       "sidewalk", "deck", "house_wash")):
        notes.append("WINTER SUSPENSION (Oct 15–late Feb): pressure washing "
                     "and window cleaning are paused for freezing temps — "
                     "quote now, offer scheduling from end of February.")
    return notes


# ─────────────────────────────────────────────────────────────
# STEP 2: A HELPER THAT CLEANS UP THE FINAL PRICE
# Your calculator shows a range (±10%) and rounds to the nearest $5.
# This little function does that rounding for us.
# ─────────────────────────────────────────────────────────────

def pw_concrete_price(sqft, buildup="clean", material="concrete"):
    """Price a flat surface (patio/driveway/sidewalk) from its area.

    First 250 sqft at the higher rate (setup baked in), the rest cheaper,
    never below the minimum. Buildup OR material multiplies at the end —
    whichever is larger, never both (pavers already price in the slow work).
    Fits: Connie 250→$100, Jobber tiers 100→$60, 400→$120, 900→$190.
    """
    first = min(sqft, PW_CONCRETE["first_block_sqft"]) * PW_CONCRETE["first_block_rate"]
    rest = max(0, sqft - PW_CONCRETE["first_block_sqft"]) * PW_CONCRETE["remainder_rate"]
    raw = max(PW_CONCRETE["minimum"], first + rest)
    factor = max(PW_BUILDUP[buildup], PW_MATERIAL.get(material, 1.0))
    return raw * factor


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

    # Dry season (Tom's roof weather): June-July-August. Roof-lane rules
    # below only bite in dry season. Uses the request date if given.
    import datetime as _dt
    _when = prop.get("request_date") or _dt.date.today()
    is_dry_season = _when.month in (6, 7, 8)

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

    def add(name, price, hourly_key):
        """Record one service line with its ±range and estimated hours."""
        results.append({
            "name": name,
            "price": price,
            "low": round_to_5(price * (1 - PRICE_RANGE)),
            "high": round_to_5(price * (1 + PRICE_RANGE)),
            "hours": round(price / TARGET_HOURLY[hourly_key], 1),
        })

    # Gutter guards include the roof blow-off in ONE combined line item.
    has_guards = prop["gutter_type"] == "guards"

    # ── GUTTER CLEANING (optionally with wet-day pricing) ──
    if prop["services"].get("gutters"):
        if has_guards:
            # Guards = blow-off over the guards: its own flat SKU rate on the
            # base stack only. It is NOT gutter cleaning with a premium.
            price = round_to_5(sqft * RATES["guards_blow_off"] * base * roof_mult)
            add("Roof Blow Off for Gutter Guards", price, "guards_blow_off")
            notes.append("Gutter guards: this is the blow-off-over-guards "
                         "service — under-guard cleaning is a separate custom "
                         "quote if requested.")
        else:
            price = round_to_5(sqft * RATES["gutter_cleaning"] * gutter_roof)
            # Current office practice: gutter cleaning never quotes below
            # $250 (Jessica/Dallon, Jul 2026). PROVISIONAL — Tom may approve
            # lowering it (strategy: win the easy jobs). One-number change.
            if price < GUTTER_CLEANING_MINIMUM:
                price = GUTTER_CLEANING_MINIMUM
                notes.append(f"Gutter cleaning raised to the "
                             f"${GUTTER_CLEANING_MINIMUM} service minimum "
                             "(current office practice, pending Tom).")
            hourly = "gutters_specialty" if roof_mult >= 1.35 else "gutters"
            add("Gutter Cleaning", price, hourly)
        if prop.get("wet_day"):
            wet = round_to_5(price * WET_DAY_GUTTER_MULT)
            notes.append(f"Wet day option: gutters ${wet} if worked wet "
                         f"(dry day ${price}). Present both to customer.")
        if price > SECOND_OPINION_LIMITS["gutter_cleaning"]:
            notes.append("Gutters over $300 — get a second opinion before quoting.")

    # ── ROOF BLOW OFF (skipped automatically when guards include it) ──
    if prop["services"].get("roof") and not has_guards:
        price = max(PRICE_FLOORS["roof_blow_off"],
                    round_to_5(sqft * RATES["roof_blow_off"] * gutter_roof))
        add("Roof Blow Off", price, "roof")
        if price > SECOND_OPINION_LIMITS["roof_blow_off"]:
            notes.append("Roof blow off over $150 — get a second opinion.")

    # ── MOSS TREATMENT ──
    if prop["services"].get("moss"):
        price = max(PRICE_FLOORS["moss_treatment"],
                    round_to_5(sqft * RATES["moss_treatment"] * gutter_roof))
        add("Moss Treatment", price, "moss")
        notes.append("Moss treatment product billed separately: $14.50/canister, "
                     "1-3 typical, tech determines on-site.")
        if price > SECOND_OPINION_LIMITS["moss_treatment"]:
            notes.append("Moss treatment over $150 — get a second opinion.")

    # Window floor depends on the VISIT: solo window job = $200; windows
    # riding a multi-service visit = $175 (ladders already out).
    _non_window = ("gutters", "roof", "moss", "driveway", "patio",
                   "sidewalk", "house_wash", "dryer_vent", "deck")
    _bundled = any(prop["services"].get(k) for k in _non_window)
    _win_floor = WINDOWS_MINIMUM_BUNDLED if _bundled else WINDOWS_MINIMUM

    # ── WINDOWS (EXTERIOR ONLY) ──
    # Uses window_mult (not base), then french pane on top.
    if prop["services"].get("windows"):
        price = round_to_5(sqft * RATES["windows_exterior"] * window_mult * french_mult)
        if price < _win_floor:
            price = _win_floor
            notes.append(f"Windows raised to the ${_win_floor} "
                         f"{'bundled ' if _bundled else ''}minimum — every "
                         "window job is at least an hour (Tom/Dallon, Jul 2026).")
        add("Window Cleaning (Exterior Only)", price, "windows")

    # ── WINDOWS (IN & OUT) ──
    # Same window scaling, but a higher rate because the tech cleans inside too.
    if prop["services"].get("windows_inout"):
        price = round_to_5(sqft * RATES["windows_in_out"] * window_mult * french_mult)
        if price < _win_floor:
            price = _win_floor
            notes.append(f"Windows raised to the ${_win_floor} "
                         f"{'bundled ' if _bundled else ''}minimum — every "
                         "window job is at least an hour (Tom/Dallon, Jul 2026).")
        add("Windows In & Out", price, "windows")

    # ── PRESSURE WASHING (sqft-based; measured area required) ──
    # prop["surfaces"] holds measured areas, e.g. {"patio": 250, "driveway": 600}
    # If a surface was requested but has no measurement, we don't guess —
    # we flag it for a human quote.
    # The setup-heavy "first block" premium applies ONCE PER VISIT, not per
    # surface — the truck sets up one time. (Learned from the real Boden job:
    # entry + walkways priced separately would double-charge the setup.)
    # We price the COMBINED area, then split the total across surfaces
    # proportionally so each line item still shows its own price.
    buildup = prop.get("buildup", "clean")
    surface_names = {"patio": "Pressure Wash Patio",
                     "driveway": "Pressure Wash Driveway",
                     "sidewalk": "Pressure Wash Sidewalk"}
    measured = {k: prop.get("surfaces", {}).get(k)
                for k in surface_names
                if prop.get("services", {}).get(k)}
    areas = {k: a for k, a in measured.items() if a}
    if areas:
        # combined BASE price on total area (setup priced once), split
        # proportionally, THEN each surface applies its own factor —
        # max(buildup, material), never stacked.
        materials = prop.get("surface_materials", {})
        base_combined = pw_concrete_price(sum(areas.values()))
        total_area = sum(areas.values())
        pavers_used = heavy_applied = False
        for key, area in areas.items():
            mat = materials.get(key, "concrete")
            mat_mult = PW_MATERIAL.get(mat, 1.0)
            factor = max(PW_BUILDUP[buildup], mat_mult)
            if mat == "pavers" and mat_mult >= PW_BUILDUP[buildup]:
                pavers_used = True
            if buildup == "heavy" and PW_BUILDUP[buildup] > mat_mult:
                heavy_applied = True
            share = round_to_5(base_combined * area / total_area * factor)
            label = f"{surface_names[key]} (~{area} sqft"
            label += ", pavers)" if mat == "pavers" else ")"
            add(label, share, "pressure")
        if pavers_used:
            notes.append("Pavers/cobblestone: slower wand work priced in "
                         "(1.5x); joint growth NOT double-charged as buildup.")
        if heavy_applied:
            notes.append("Pressure washing: HEAVY buildup priced in (1.5x) — "
                         "confirm with photo before appointment.")
        if len(areas) > 1:
            notes.append("Multiple PW surfaces: setup priced once for the "
                         "visit, split across line items.")
    for key, a in measured.items():
        if not a:
            notes.append(f"{surface_names[key]}: requested but NO measurement "
                         "available — needs office/expert quote (get photo "
                         "or dimensions).")

    # ── HOUSE WASH ──
    if prop["services"].get("house_wash"):
        price = round_to_5(sqft * PW_HOUSE_WASH_RATE * stories_mult * access_mult)
        add("House Washing", price, "pressure")
        notes.append("House wash: homeowner must be present for this service.")

    # ── DECK — always a custom quote (send photos), per Jobber policy ──
    if prop["services"].get("deck"):
        notes.append("Deck pressure wash: ALWAYS custom quote — request photos.")

    # ── DRYER VENT (flat fee; cheaper as an add-on) ──
    if prop["services"].get("dryer_vent"):
        others = len(results) > 0
        price = DRYER_VENT_ADDON if others else DRYER_VENT_ALONE
        results.append({"name": "Dryer Vent Cleaning", "price": price,
                        "low": price, "high": price, "hours": 1.0})

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

    # ── HOLIDAY LIGHTS (Tom quotes labor; engine enforces the hard rules) ──
    if prop["services"].get("holiday_lights"):
        if has_guards:
            notes.append("DECLINE LIGHTS: home has gutter guards — we cannot "
                         "install holiday lights on gutter guards (clip "
                         "corrosion issue). Send the standard decline verbiage.")
        else:
            notes.append("HOLIDAY LIGHTS: forward to Tom for custom labor "
                         "quote ($385 minimum; ~175 linear ft material on an "
                         "average home; C7 warm white unless requested).")

    # ── JOB MINIMUM (no visit below $150) ──
    running_total = sum(s["price"] for s in results)
    if 0 < running_total < JOB_MINIMUM:
        bump = JOB_MINIMUM - running_total
        results.append({"name": "Service Minimum Adjustment", "price": bump,
                        "low": bump, "high": bump, "hours": 0})
        notes.append(f"Job under ${JOB_MINIMUM} minimum — added "
                     f"${bump} adjustment to reach the visit minimum.")

    # ── TOM'S ROOF-LANE RULES (Jul 8 call) ──
    # Roof lane = gutters + roof blow-off + moss. Two guardrails:
    roof_lane = {"Gutter Cleaning", "Roof Blow Off Cleaning", "Roof Blow Off",
                 "Roof Blow Off for Gutter Guards", "Moss Treatment"}
    lane_lines = [s for s in results if s["name"] in roof_lane]
    lane_total = sum(s["price"] for s in lane_lines)
    lane_services = {s["name"] for s in lane_lines}
    only_gutter_lane = lane_services and lane_services <= {
        "Gutter Cleaning", "Roof Blow Off", "Roof Blow Off Cleaning"}
    if only_gutter_lane and lane_total > ROOF_LANE_REVIEW_OVER:
        notes.append(f"REVIEW (Tom): gutters/blow-off total ${lane_total:.0f} "
                     f"is over ${ROOF_LANE_REVIEW_OVER} — Tom may do it for "
                     "less on a dry day. Confirm before sending.")

    # TWO-PRICE: offer a dry-day price on a meaningful roof lane. The
    # standard price is the true price; the dry-day price is the
    # flexible-scheduling concession (~27% off, floored at the roll-out
    # minimum, never above standard). Surfaced as a note the office can
    # offer the customer — "$X standard, or $Y if you'll take a dry day."
    if lane_total > DRY_SEASON_ROOF_FLOOR:
        dry = max(DRY_SEASON_ROOF_FLOOR,
                  round_to_5(lane_total * (1 - DRY_DAY_DISCOUNT)))
        if dry < lane_total:
            notes.append(
                f"CUSTOMER: We can do the roof/gutter work for ${dry:.0f} if "
                "you're flexible on timing — we schedule it for a dry day "
                "when it's fastest for our crew. Otherwise it's "
                f"${lane_total:.0f} on your preferred date.")
            notes.append(
                f"DRY-DAY OPTION: roof lane ${dry:.0f} (vs ${lane_total:.0f} "
                "standard). Standard is the TRUE price for records; the "
                "dry-day price is a flexible-timing concession, not a rate "
                "cut — becomes a weather-pending hold if the customer takes it.")
    # The $400 dry-season floor is about TOM'S roll-outs — he does
    # high-risk roofs only now (tom_only pitch). Scoped there so it
    # doesn't nag every small summer gutter job a regular tech handles.
    # (INTERPRETATION — confirm with Dallon: Tom-tier only, or all jobs?)
    if (is_dry_season and prop.get("pitch") == "tom_only"
            and lane_lines and 0 < lane_total < DRY_SEASON_ROOF_FLOOR):
        notes.append(f"DRY SEASON: Tom-tier roof-lane visit is only "
                     f"${lane_total:.0f} — Tom won't roll out for under "
                     f"${DRY_SEASON_ROOF_FLOOR} in dry season. Bundle more, "
                     "or hold for a fuller day.")

    # ── DAY-CAPACITY CHECK (schedule is booked by dollars) ──
    booked_total = sum(s["price"] for s in results)
    if booked_total > DAY_CAPACITY["day_high"]:
        notes.append(f"SCHEDULING: ${booked_total} exceeds one tech-day "
                     f"(${DAY_CAPACITY['day_high']} max) — split across days "
                     "or assign a helper.")

    # ── SEASONAL SCHEDULING RULES ──
    import datetime
    when = prop.get("request_date") or datetime.date.today()
    notes.extend(seasonal_notes(when, prop["services"]))

    # ── CONFIDENCE SCORE (data-quality based — works from day one) ──
    # Starts at 100 and loses points for anything missing or risky.
    # Later, Vision/Solar will fill these fields; today they're inputs.
    confidence = 100
    if not prop.get("sqft"):
        confidence -= 30
    if prop.get("pitch") == "unknown":
        confidence -= 15
    if any("NO measurement" in n for n in notes):
        confidence -= 25
    if pitch_mult >= 1.35 or stories_mult >= 1.35:
        confidence -= 10   # steep/tall = more ways to be wrong
    if buildup == "heavy":
        confidence -= 10
    confidence = max(0, confidence)

    return results, notes, confidence


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

    services, notes, confidence = calculate_bid(example_house)

    print("=" * 62)
    print("  MASTER BUTLER — BID ESTIMATE")
    print("=" * 62)
    print(f"  Property: {example_house['sqft']} sq ft, "
          f"{example_house['stories']}-story   |   Confidence: {confidence}%")
    print("-" * 62)

    total = 0
    total_hours = 0
    for s in services:
        rng = f"(${s['low']}-${s['high']})"
        print(f"  {s['name']:<38} ${s['price']:<5} {rng:<12} {s['hours']}h")
        total += s["price"]
        total_hours += s["hours"]

    print("-" * 62)
    print(f"  {'TOTAL ESTIMATE':<38} ${total}   ~{round(total_hours,1)} hrs on site")
    print("=" * 62)

    if notes:
        print("\n  ⚠ OFFICE NOTES:")
        for note in notes:
            print(f"    • {note}")
