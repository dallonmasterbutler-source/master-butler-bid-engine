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

import email
import email.policy
from pathlib import Path

from email_parser import parse_eml
from bid_engine import calculate_bid


def extract_photos(eml_path):
    """Pull attached photos (>50KB — real pics, not logos) to a temp folder."""
    msg = email.message_from_bytes(Path(eml_path).read_bytes(),
                                   policy=email.policy.default)
    outdir = Path("/tmp/pipeline_photos") / Path(eml_path).stem
    outdir.mkdir(parents=True, exist_ok=True)
    photos = []
    for i, part in enumerate(msg.walk()):
        if part.get_content_type().startswith("image/"):
            data = part.get_payload(decode=True)
            if data and len(data) > 50_000:
                p = outdir / f"photo_{len(photos)+1}.jpg"
                p.write_bytes(data)
                photos.append(p)
    return photos


# ─────────────────────────────────────────────────────────────
# PROPERTY DATA (STUB)
# In production this step calls Google Geocoding + Solar APIs.
# Today it uses a small lookup of addresses we researched by hand,
# so the demo runs on real customer emails with real house sizes.
# Anything not in the table comes back "unknown" — and the pipeline
# must handle that gracefully (lower confidence, flag for office).
# ─────────────────────────────────────────────────────────────

KNOWN_PROPERTIES = {
    # address fragment (lowercased) -> facts we verified from records.
    # (In production this table becomes a county-records/listing lookup.)
    "24323 se 42nd": {"sqft": 2540, "stories": "2"},   # Jing Xu, Issaquah
    "325 7th ave":   {"sqft": 1910, "stories": "1"},   # Dawn Goehner, Kirkland
    "2005 265th":    {"sqft": 3730, "stories": "2"},   # Shibu, Sammamish
    "22225 ne 31st": {"sqft": 2820, "stories": "2"},   # Sammamish
}


def records_for(address):
    """What property records tell us (stub table for now)."""
    if address:
        addr = address.lower()
        for fragment, facts in KNOWN_PROPERTIES.items():
            if fragment in addr:
                return facts
    return {}


def lookup(address):
    """LIVE lookup: geocode + Solar roof measurement + sanity grading."""
    from property_data import lookup_property
    facts, flags, deduction = lookup_property(address, records_for(address))
    return facts, flags, deduction


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

    if not facts.get("stories"):
        office_flags.append("Stories unknown — assumed 2 (verify; matters "
                            "for safety flags and window/house-wash pricing).")
    prop = {
        "sqft": facts["sqft"],
        "stories": facts.get("stories") or "2",
        # Pitch now comes MEASURED from Solar; debris still needs Vision/photos
        "pitch": facts.get("pitch", "moderate"), "debris": "moderate",
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

    facts, data_flags, deduction = lookup(parsed["address"])
    roof = f", roof {facts['roof_sqft']:,.0f} sqft" if facts.get("roof_sqft") else ""
    print(f"    Property: {facts['sqft'] or '???'} sqft, "
          f"{facts['stories']}-story, pitch {facts['pitch']}{roof}")

    prop, office_flags = build_property(parsed, facts)
    office_flags = data_flags + office_flags

    # ── VISION: if the customer sent photos, look at them ──
    # Merge policy: records/Solar own sqft & stories; Vision owns what
    # only photos can show (buildup, surfaces, move-items, hazards).
    photos = extract_photos(eml_path)
    if photos:
        print(f"    Photos attached: {len(photos)} — running Vision...")
        try:
            from vision import analyze_photos, vision_to_prop_fields
            v, cost = analyze_photos(photos,
                                     extra_context=parsed["newest_message"][:300])
            vfields, vnotes = vision_to_prop_fields(v)
            prop["buildup"] = vfields.get("buildup", prop.get("buildup", "clean"))
            if vfields.get("surface_materials"):        # pavers pricing
                prop["surface_materials"] = vfields["surface_materials"]
            if vfields.get("debris"):                   # trees → gutter debris
                prop["debris"] = vfields["debris"]
            if vfields.get("surfaces"):
                prop["surfaces"].update(vfields["surfaces"])
                # photos measured a surface → make sure it gets priced
                for k in vfields["surfaces"]:
                    prop["services"].setdefault(k, True)
            if vfields.get("roof_material") and prop["roof_material"] == "standard":
                prop["roof_material"] = vfields["roof_material"]
            office_flags += vnotes
            upsells = [s for s in v.get("services_suggested", [])
                       if s in SERVICE_TO_ENGINE
                       and SERVICE_TO_ENGINE[s][0] not in prop["services"]]
            if upsells:
                office_flags.append("Photos suggest possible add-ons: "
                                    + ", ".join(upsells))
            print(f"    Vision: buildup={prop.get('buildup')}, "
                  f"surfaces={vfields.get('surfaces', {})} (${cost:.2f})")
        except Exception as e:
            office_flags.append(f"Vision failed ({e}) — photos need manual look.")

    # ── PRICE PROMISES: honor what the office put in writing ──
    try:
        from promises import promise_notes
        office_flags += promise_notes(parsed.get("sender_name"))
    except Exception:
        pass    # promises are a bonus, never a blocker

    # ── AERIAL CROSS-CHECK: the straight-down second opinion ──
    # Adds flags (wrong building, area disagreement) and may RAISE the
    # gutter-debris call from fresh canopy imagery. Never blocks a bid.
    if parsed.get("address"):
        try:
            from aerial import cross_check
            afields, anotes = cross_check(prop, parsed["address"])
            if afields.get("debris"):
                prop["debris"] = afields["debris"]
            office_flags += anotes
            print(f"    Aerial cross-check: {len(anotes)} note(s)")
        except Exception as e:
            office_flags.append(f"Aerial cross-check unavailable ({e}).")
        try:
            from aerial import fetch_streetview
            if fetch_streetview(parsed["address"]) is None:
                office_flags.append("No Street View at this address "
                                    "(rural road) — curb photo unavailable.")
        except Exception:
            pass                    # curb photo is a bonus, never a blocker

    # House sqft is only required by services that PRICE on it.
    # A pressure-washing-only job prices on photo-measured surface areas.
    NEEDS_HOUSE_SQFT = {"gutters", "roof", "moss", "windows",
                        "windows_inout", "house_wash"}
    needs_sqft = any(prop["services"].get(s) for s in NEEDS_HOUSE_SQFT)
    if needs_sqft and not prop["sqft"]:
        print("    → DRAFT HELD: no square footage — office must supply it.")
        for f in office_flags:
            print(f"      ⚠ {f}")
        return
    if not prop["sqft"]:
        prop["sqft"] = 0   # PW-only: engine ignores it, confidence notes it

    results, notes, confidence = calculate_bid(prop)
    confidence = max(0, confidence - deduction)   # data quality lowers trust

    print(f"\n    DRAFT BID  (confidence {confidence}%)")
    total = 0
    for s in results:
        print(f"      {s['name']:<42} ${s['price']}")
        total += s["price"]
    print(f"      {'TOTAL':<42} ${total}")
    for n in notes + office_flags:
        print(f"      ⚠ {n}")
    print("    → status: PENDING OFFICE REVIEW (never auto-sends)")

    # structured result, so callers (dashboard approve) don't re-parse text
    return {
        "customer": {"name": parsed.get("sender_name"),
                     "email": parsed.get("sender_email"),
                     "phone": parsed.get("phone"),
                     "address": parsed.get("address")},
        "bid": {"services": results, "notes": notes + office_flags,
                "confidence": confidence},
        "prop_info": {"sqft": prop.get("sqft"),
                      "sqft_source": prop.get("sqft_source"),
                      "pitch": prop.get("pitch"),
                      "roof_material": prop.get("roof_material"),
                      "stories": prop.get("stories")},
        "total": total,
    }


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
