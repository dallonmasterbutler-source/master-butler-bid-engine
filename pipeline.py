"""
MASTER BUTLER — END-TO-END PIPELINE (first working version)

This wires the pieces together for the first time:

    customer email (.eml)
        → email_parser   (who? where? what service?)
        → property data  (sqft etc. — Google APIs later; stub today)
        → bid_engine     (the priced draft)
        → a DRAFT BID printed for office review

Nothing is sent anywhere. This is the "assembly line" the real system
will run; today it runs on saved emails so we can watch it work.
"""

from pathlib import Path

from email_parser import parse_eml
from bid_engine import calculate_bid


# ─────────────────────────────────────────────────────────────
# PROPERTY DATA (STUB)
# In production this step calls Google Geocoding + Solar APIs.
# Today it uses a small lookup of addresses we researched by hand,
# so the demo runs on real customer emails with real house sizes.
# Anything not in the table comes back "unknown" — and the pipeline
# must handle that gracefully (lower confidence, flag for office).
# ─────────────────────────────────────────────────────────────

KNOWN_PROPERTIES = {
    # address fragment (lowercased) -> facts we verified from records
    "24323 se 42nd": {"sqft": 2540, "stories": "2"},   # Jing Xu, Sammamish
    "325 7th ave":   {"sqft": 1910, "stories": "1"},   # Dawn Goehner, Kirkland
    "2005 265th":    {"sqft": 3730, "stories": "2"},   # Shibu, Sammamish
    "22225 ne 31st": {"sqft": 2820, "stories": "2"},   # Sammamish
}


def lookup_property(address):
    """Pretend to be the Google APIs. Returns facts or 'unknown'."""
    if address:
        addr = address.lower()
        for fragment, facts in KNOWN_PROPERTIES.items():
            if fragment in addr:
                return {**facts, "source": "records (stub)"}
    return {"sqft": None, "stories": "2", "source": "NOT FOUND — needs lookup"}


# ─────────────────────────────────────────────────────────────
# PARSED SERVICES → BID ENGINE SWITCHES
# The parser speaks in service names; the engine wants toggles.
# ─────────────────────────────────────────────────────────────

SERVICE_TO_ENGINE = {
    "gutter_cleaning":       ("gutters", True),
    "roof_blow_off":         ("roof", True),
    "roof_blow_off_guards":  ("roof_guards", True),   # handled via gutter_type
    "moss_treatment":        ("moss", True),
    "windows_exterior":      ("windows", True),
    "windows_in_out":        ("windows_inout", True),
    "windows_unspecified":   ("windows", True),        # office confirms in/out
    "house_wash":            ("house_wash", True),
    "pw_driveway":           ("driveway", True),
    "pw_patio":              ("patio", True),
    "pw_sidewalk":           ("sidewalk", True),
    "pw_deck":               ("deck", True),
    "dryer_vent":            ("dryer_vent", True),
}

# Services the engine can't price yet — always office review
OFFICE_ONLY = {"moss_removal", "holiday_lights", "bird_control",
               "exterior_gutter_cleaning", "pressure_washing"}


def build_property(parsed, facts):
    """Assemble the bid-engine input from parsed email + property facts."""
    services = {}
    office_flags = []
    for svc in parsed["services"]:
        if svc in SERVICE_TO_ENGINE:
            key, val = SERVICE_TO_ENGINE[svc]
            if key == "roof_guards":
                services["gutters"] = True   # guards line includes blow-off
            else:
                services[key] = val
        elif svc in OFFICE_ONLY:
            office_flags.append(f"'{svc}' requested — office quotes this one.")
    if "windows_unspecified" in parsed["services"]:
        office_flags.append("Windows: in/out not specified — confirm with customer.")

    prop = {
        "sqft": facts["sqft"],
        "stories": facts["stories"],
        # Unknown conditions default to the SAFE middle; Vision fills these later
        "pitch": "moderate", "debris": "moderate",
        "gutter_type": "guards" if "roof_blow_off_guards" in parsed["services"] else "standard",
        "roof_material": "standard", "access": "normal",
        "window_style": "standard", "window_condition": "normal",
        "window_access": "standard", "french_pane": "none",
        "services": services, "surfaces": {},
    }
    return prop, office_flags


# ─────────────────────────────────────────────────────────────
# RUN ONE EMAIL THROUGH THE WHOLE LINE
# ─────────────────────────────────────────────────────────────

def process(eml_path):
    parsed = parse_eml(eml_path)

    print("=" * 64)
    print(f"📧  {Path(eml_path).name}")
    print(f"    From: {parsed['sender_name']} <{parsed['sender_email']}>")
    print(f"    Kind: {parsed['kind']}")

    if parsed["kind"] != "new_request":
        print(f"    → routed to OFFICE ({parsed['kind']}) — no bid generated.")
        return

    print(f"    Services asked: {', '.join(parsed['services'])}")
    print(f"    Address: {parsed['address'] or '— none given —'}")

    facts = lookup_property(parsed["address"])
    print(f"    Property: {facts['sqft'] or '???'} sqft "
          f"[{facts['source']}]")

    prop, office_flags = build_property(parsed, facts)

    if not prop["sqft"]:
        print("    → DRAFT HELD: no square footage — office must supply it.")
        for f in office_flags:
            print(f"      ⚠ {f}")
        return

    results, notes, confidence = calculate_bid(prop)

    print(f"\n    DRAFT BID  (confidence {confidence}%)")
    total = 0
    for s in results:
        print(f"      {s['name']:<42} ${s['price']}")
        total += s["price"]
    print(f"      {'TOTAL':<42} ${total}")
    for n in notes + office_flags:
        print(f"      ⚠ {n}")
    print("    → status: PENDING OFFICE REVIEW (never auto-sends)")


if __name__ == "__main__":
    base = Path(__file__).parent
    demos = [
        "roof cleaning for my sammamish house.eml",   # clean request w/ address
        "*Today!-4.eml",                              # Dawn, windows in/out
        "*Today!-7.eml",                              # Jeff, no address
        "Pending Appt 6_29-2*PM.eml",                 # scheduling reply
    ]
    for pattern in demos:
        for p in sorted((base / "test_emails").glob(pattern)):
            process(p)
            print()
            break
