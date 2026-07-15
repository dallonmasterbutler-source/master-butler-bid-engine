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
OFFICE_ONLY = {"moss_removal", "holiday_lights", "permanent_lights",
               "bird_control", "solar_panels",
               "exterior_gutter_cleaning", "pressure_washing"}


def build_property(parsed, facts):
    """Assemble the bid-engine input from parsed email + property facts."""
    services = {}
    office_flags = []
    # THE LEARNING HOOK (LaRee, Jul 10): office corrections to a house's
    # facts (pitch/stories/debris/roof) are remembered per address and
    # merged here — so EVERY intake path (email, manual, voicemail,
    # rebuild) prices from the office's truth, forever.
    try:
        import facts_edit
        office_flags += facts_edit.apply_overrides(
            facts, parsed.get("address"))
    except Exception:
        pass
    # OFFICE PROTOCOL (Dallon's ruling on the LaRee call, Jul 14): roof
    # blow-off is NEVER quoted without a gutter cleaning — the blow-off
    # debris lands in the gutters. Was a warning note; now the gutter
    # line is ADDED to the quote automatically. Exception: gutter guards
    # (the guards SKU includes the blow-off, no open gutters to clean).
    _txt = ((parsed.get("newest_message") or "")
            + " " + (parsed.get("subject") or "")).lower()
    # GUARDS-FROM-HISTORY (Dallon's engine batch, Jul 14 — the Gloria
    # Sloan lesson: her home has guards on file, yet a fresh request
    # drafted a plain gutter cleaning). Homes that ever bought the
    # guards SKU are in the guards_homes registry: their roof blow-off
    # IS the guards SKU, and no gutter line gets auto-added.
    _has_guards = False
    try:
        import re as _re2
        _gslug = _re2.sub(r"[^a-z0-9]+", "-",
                          (parsed.get("address") or "").lower()).strip("-")
        if _gslug:
            _gh = []
            try:
                import clouddb as _gc
                if _gc.available():
                    _gh = _gc.get_blob("guards_homes") or []
            except Exception:
                pass
            if not _gh:
                import json as _gj
                from pathlib import Path as _gp
                _f = _gp(__file__).parent / "data" / "guards_homes.json"
                _gh = _gj.loads(_f.read_text()) if _f.exists() else []
            _has_guards = any(_gslug == g or (len(_gslug) > 12 and
                              (g.startswith(_gslug)
                               or _gslug.startswith(g))) for g in _gh)
    except Exception:
        _has_guards = False
    if _has_guards and "roof_blow_off" in parsed["services"]:
        parsed["services"] = ["roof_blow_off_guards" if s ==
                              "roof_blow_off" else s
                              for s in parsed["services"]
                              if s != "gutter_cleaning"]
        office_flags.append(
            "🛡 THIS HOME HAS GUTTER GUARDS ON FILE — quoted the "
            "guards blow-off SKU (includes the blow-off; no open "
            "gutters to clean). Remove only if the guards are gone.")
    if "roof_blow_off" in parsed["services"] \
            and "gutter_cleaning" not in parsed["services"] \
            and "roof_blow_off_guards" not in parsed["services"] \
            and "gutter guard" not in _txt and "guards" not in _txt:
        parsed["services"] = list(parsed["services"]) + ["gutter_cleaning"]
        office_flags.append(
            "Gutter cleaning ADDED automatically — office rule: no roof "
            "blow-off without gutters (debris lands in them). Remove the "
            "line only if the home has gutter guards.")
    for svc in parsed["services"]:
        if svc in SERVICE_TO_ENGINE:
            key, val = SERVICE_TO_ENGINE[svc]
            if key == "roof_guards":
                services["gutters"] = True   # guards line includes blow-off
            else:
                services[key] = val
        elif svc == "handyman":
            office_flags.append(
                "🔧 HANDYMAN SERVICE requested — NEW service, bid PER JOB "
                "by the office; the engine never prices this (Dallon's "
                "rule, Jul 10 — the Wendy Sklar drywall/painting case).")
        elif svc == "permanent_lights":
            office_flags.append(
                "PERMANENT LIGHT INSTALL requested — price ≈3× a seasonal "
                "holiday-light job (Dallon's rule; seasonal min $385 → "
                "permanent ≈$1,150+; real anchor: quote #36521 at $1,200). "
                "Forward to Tom for the labor quote.")
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
        # they asked for PW (generically or by surface) — aerial may
        # build its priced menu; otherwise it stays quiet (Tillie lesson)
        "wants_pw": ("pressure_washing" in parsed["services"]
                     or any(s.startswith("pw_")
                            for s in parsed["services"])),
        # walkout-rambler facts (Jessica Jensen, Jul 9) — display/notes
        # only for now; pricing waits for calibration evidence
        "basement_sqft": facts.get("basement_sqft"),
        "garage_sqft": facts.get("garage_sqft"),
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

    # KNOWN CUSTOMER, NO ADDRESS: repeat customers rarely repeat their
    # address — Jobber knows it. Read-only lookup by exact email.
    if not parsed.get("address") and parsed.get("sender_email"):
        try:
            from jobber_client import find_client_address
            known = find_client_address(parsed["sender_email"])
            if known:
                parsed["address"] = known
                print(f"    Address from Jobber client record: {known}")
                facts, data_flags2, deduction = lookup(known)
                data_flags = data_flags2 + [
                    "Address pulled from their Jobber client record — "
                    "confirm it's for the SAME property."]
        except Exception:
            pass

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

    # ── HOLIDAY LIGHTS PRE-MEASURE: Tom's escalation arrives with the
    #    roofline + materials math already done (labor stays Tom's) ──
    if prop.get("services", {}).get("holiday_lights") and parsed.get("address"):
        try:
            from lights import estimate_for
            _, lights_note = estimate_for(parsed["address"])
            office_flags.append(lights_note)
        except Exception:
            pass

    # ── PRICE PROMISES: honor what the office put in writing ──
    try:
        from promises import promise_notes
        office_flags += promise_notes(parsed.get("sender_name"))
    except Exception:
        pass    # promises are a bonus, never a blocker

    # ── OFFICE PLAYBOOK (Jul 9 — Martha/Jessica/LaRee's rules):
    #    seasonal windows, service dependencies, referrals, skylights ──
    try:
        import seasons
        s_alert, s_notes = seasons.check(parsed, prop)
        office_flags += s_notes
        if s_alert:
            office_flags.append(s_alert)
            print("OFFICE_ALERT: " + s_alert)   # poller lifts to the badge
    except Exception:
        pass    # playbook notes never block a bid

    # ── AERIAL CROSS-CHECK: the straight-down second opinion ──
    # Adds flags (wrong building, area disagreement) and may RAISE the
    # gutter-debris call from fresh canopy imagery. Never blocks a bid.
    if parsed.get("address"):
        try:
            from aerial import cross_check
            afields, anotes = cross_check(prop, parsed["address"])
            if afields.get("debris"):
                prop["debris"] = afields["debris"]
            for key, area in (afields.get("surfaces") or {}).items():
                prop["surfaces"].setdefault(key, area)   # fill blanks only
            # keep the raw reads for the record — the add-to-quote menu
            # prices later services from these (real areas, real debris)
            if afields.get("aerial_surfaces"):
                prop["aerial_surfaces"] = afields["aerial_surfaces"]
            if afields.get("canopy_level") == "heavy":
                prop["debris_read"] = "heavy"
            office_flags += anotes
            print(f"    Aerial cross-check: {len(anotes)} note(s)")
        except Exception as e:
            office_flags.append(f"Aerial cross-check unavailable ({e}).")
        try:                    # pre-warm the 3D flyover (free tier) so
            from aerial_view import request_render   # it's ready when the
            request_render(parsed["address"])        # office opens the bid
        except Exception:
            pass
        try:
            from aerial import fetch_streetview, street_check
            if fetch_streetview(parsed["address"]) is None:
                office_flags.append("No Street View at this address "
                                    "(rural road) — curb photo unavailable.")
            else:
                sfields, snotes = street_check(prop, parsed["address"])
                for k, v in sfields.items():
                    prop[k] = v
                office_flags += snotes
                if snotes:
                    print(f"    Street-view check: {len(snotes)} note(s)")
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

    # FLOORED-PRICE HONESTY (engine batch, Jul 14): a bid that only
    # reaches $150 because the JOB-MINIMUM adjustment padded it is NOT
    # a confidently-priced job — the floor is masking unknown scope
    # (Buvaneswari: $105 blow-off + $45 'adjustment' shown at 100%).
    # Cap those at 80 with the why. A flat-rate single service (dryer
    # vent alone) has no adjustment line and keeps its confidence.
    if any("minimum adjustment" in (r.get("name") or "").lower()
           for r in results):
        if confidence > 80:
            confidence = 80
            notes.append("Confidence capped at 80 — this price is the "
                         "visit MINIMUM, not a measured job; the "
                         "adjustment line is doing the work. Worth a "
                         "quick scope check before sending.")

    # TECH FIELD-NOTE MINIMUMS: the tech saw the roof; the office didn't.
    try:
        import minimums
        mnotes = minimums.apply(results, email=parsed.get("sender_email"),
                                phone=parsed.get("phone"),
                                address=parsed.get("address"))
        notes += mnotes
        if any("REVIEW" in n for n in mnotes):
            confidence = min(confidence, 45)   # forces office eyes
    except Exception:
        pass

    # NEVER LESS THAN LAST TIME (Martha's Robert Lin catch, Jul 10):
    # a returning customer's line ratchets up to their last invoice.
    try:
        import lastpaid
        lnotes = lastpaid.apply(results,
                                address=parsed.get("address"),
                                client_name=parsed.get("sender_name"))
        notes += lnotes
        if any("REVIEW" in n for n in lnotes):
            confidence = min(confidence, 45)
    except Exception:
        pass

    # USUAL BUNDLE (Tom, Jul 13): 'my roof' from someone who does roof +
    # gutters + moss every year → surface the missing usuals with their
    # last prices so the office quotes the FULL bundle. Note only —
    # never silently adds lines.
    try:
        import lastpaid as _lp
        _ub = _lp.usual_bundle(parsed.get("services"),
                               address=parsed.get("address"),
                               client_name=parsed.get("sender_name"))
        notes += _ub
        if _ub:
            print(f"OFFICE_ALERT: {_ub[0]}")
    except Exception:
        pass

    # TIMED DISCOUNT (Dallon + LaRee, Jul 13): a dated discount from
    # Settings rides every new bid in its window as its OWN labeled
    # line — true prices stay on the lines above, so the learning loop
    # still sees real numbers. After lastpaid so the ratchet compares
    # true prices, never discounted ones.
    try:
        import timed_discounts
        notes += timed_discounts.apply(results)
    except Exception:
        pass

    # EXTERIOR → IN&OUT UPSELL (LaRee, Jul 10: 'when someone asks for
    # exterior windows, quote them ALSO for in and out — capture higher
    # revenue'). A note with the ready number — the office offers it;
    # the total never changes on its own.
    try:
        import bid_engine as _be
        _names = [(s.get("name") or "").lower() for s in results]
        if any("exterior" in n and "window" in n for n in _names) \
                and not any("in & out" in n or "in&out" in n
                            for n in _names) and prop.get("sqft"):
            _est = max(_be.round_to_5(prop["sqft"]
                                      * _be.RATES["windows_in_out"]),
                       _be.WINDOWS_INOUT_MINIMUM)
            notes.append(f"💡 UPSELL: they asked exterior-only — also "
                         f"offer Windows In & Out at ≈${_est:,.0f} "
                         "(LaRee's capture-higher-revenue rule).")
    except Exception:
        pass

    # MOSS PRODUCT RIDES WITH MOSS LABOR (Martha, Jul 10: 'every time
    # someone requests moss treatment, the product has to be added') —
    # the office bills them together on every real invoice.
    if any("moss" in (s.get("name") or "").lower() for s in results) \
            and not any("product" in (s.get("name") or "").lower()
                        for s in results):
        results.append({"name": "Moss Treatment Product", "price": 14.50})
        notes.append("Moss Treatment Product $14.50 added automatically "
                     "(1-3 canisters typical — tech confirms on-site).")

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
                      "stories": prop.get("stories"),
                      # walkout-rambler facts (Jessica Jensen, Jul 9)
                      "basement_sqft": prop.get("basement_sqft"),
                      "garage_sqft": prop.get("garage_sqft"),
                      # persisted reads → add-to-quote menu prices real
                      "aerial_surfaces": prop.get("aerial_surfaces"),
                      "debris_read": prop.get("debris_read")
                      or prop.get("debris"),
                      "buildup_read": prop.get("buildup")},
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
