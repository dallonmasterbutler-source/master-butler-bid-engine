"""
MASTER BUTLER — SHADOW BID FROM A CUSTOMER'S QUOTE (Dallon, Jul 10:
"Kevin's request didn't get bid at all so we cant learn from the
program bid vs the office bid.")

When a returning customer sends a request too sparse to parse ("please
send me a quote"), we STILL want a system number to compare against
whatever the office quotes — otherwise the learning loop is blind. So
we read the services off their last Jobber quote and re-price them
through our own engine. The result is a normal shadow draft: the
scoreboard can then diff program-vs-office like any other bid.

Read-only against Jobber; produces a draft dict, writes nothing here.
"""

import re

# Jobber line-item name (however it's spelled on the quote) → the
# parser service token the pipeline understands. Built by inverting
# jobber_client._OFFICE_LINE plus the pressure-wash surfaces.
_LINE_TO_SERVICE = {
    "gutter cleaning": "gutter_cleaning",
    "roof blow off": "roof_blow_off",
    "roof blow off for installed gutter guards": "roof_blow_off_guards",
    "apply moss treatment": "moss_treatment",
    "moss treatment": "moss_treatment",
    "window cleaning exterior only": "windows_ext",
    "window cleaning in & out": "windows_in_out",
    "window cleaning in and out": "windows_in_out",
    "house washing": "pw_house",
    "house wash": "pw_house",
    "dryer vent cleaning": "dryer_vent",
    "pressure wash driveway": "pw_driveway",
    "pressure wash patio": "pw_patio",
    "pressure wash pathway": "pw_sidewalk",
    "pressure wash sidewalk": "pw_sidewalk",
    "pressure wash walkway": "pw_sidewalk",
}

# lines that are billing companions / add-ons, never a service to re-bid
_SKIP_LINE = ("moss treatment product", "discount", "tax", "travel",
              "fee", "trip charge")


def services_from_lines(lines):
    """Jobber quote line items -> our parser service tokens (deduped)."""
    out = []
    for li in lines or []:
        name = (li.get("name") or "").strip().lower()
        # strip the roof qualifier the office appends ("- Composition")
        base = re.split(r"\s*[-–]\s*(?:composition|other roof|comp\b)",
                        name)[0].strip()
        if any(s in name for s in _SKIP_LINE):
            continue
        svc = _LINE_TO_SERVICE.get(base) or _LINE_TO_SERVICE.get(name)
        if not svc:                          # fuzzy fallback by keyword
            for k, v in _LINE_TO_SERVICE.items():
                if k in name:
                    svc = v
                    break
        if svc and svc not in out:
            out.append(svc)
    return out


def build(address, services, customer, notes_prefix="", source=None):
    """Price a known service list at a known address through our own
    engine → a draft dict (same shape the pipeline produces). Returns
    None if we can't get a size to price from. source overrides the
    provenance note (default: rebuilt-from-their-last-quote)."""
    if not (address and services):
        return None
    from pipeline import lookup, build_property
    from bid_engine import calculate_bid
    facts, flags, _ded = lookup(address)
    parsed = {"services": services, "address": address}
    prop, oflags = build_property(parsed, facts or {})
    if not prop.get("sqft"):
        return None
    results, engine_notes, confidence = calculate_bid(
        dict(prop, request_date=__import__("datetime").date.today()))
    engine_notes = list(engine_notes)
    # returning customer BY DEFINITION — ratchet to their last invoice
    # (Martha's Robert Lin catch) and ride the moss product along
    try:
        import lastpaid
        engine_notes += lastpaid.apply(results, address=address,
                                       client_name=(customer or {}).get("name"))
    except Exception:
        pass
    if any("moss" in (s.get("name") or "").lower() for s in results) \
            and not any("product" in (s.get("name") or "").lower()
                        for s in results):
        results.append({"name": "Moss Treatment Product", "price": 14.50})
    total = sum(s["price"] for s in results)
    # pressure-wash services can't be priced without measured surfaces —
    # say so plainly so the office/scoreboard know the comparison is
    # partial, not that we quoted $0
    priced = {s["name"].lower() for s in results}
    pw_missing = [s for s in services if s.startswith("pw_")
                  and not any("pressure wash" in p or "house wash" in p
                              for p in priced)]
    note = notes_prefix + (source or
           ("Shadow bid rebuilt from the customer's last quote (their "
            "request was too brief to price directly) — so the "
            "scoreboard can compare our number to the office's."))
    extra = ([f"NOTE: {', '.join(pw_missing)} on their last quote — not "
              "in this shadow bid (pressure washing needs surfaces "
              "measured; use 📐 Measure to add it)."] if pw_missing else [])
    return {"customer": customer,
            "bid": {"services": results,
                    "notes": [note] + extra + list(engine_notes)
                    + list(flags) + list(oflags),
                    "confidence": confidence},
            "prop_info": {"sqft": prop.get("sqft"),
                          "sqft_source": prop.get("sqft_source"),
                          "pitch": prop.get("pitch"),
                          "roof_material": prop.get("roof_material"),
                          "stories": prop.get("stories"),
                          "basement_sqft": prop.get("basement_sqft"),
                          "garage_sqft": prop.get("garage_sqft")},
            "total": total}


def from_open_quote(rec):
    """Given a shadow record that already carries open_quote_ctx (from
    the returning-customer check) but has NO priced draft, rebuild a
    shadow bid from that quote's lines. Mutates & returns the record
    (draft attached) or leaves it unchanged. Caller persists."""
    oq = rec.get("open_quote_ctx") or {}
    d = rec.get("draft") or {}
    if d.get("bid", {}).get("services"):     # already has a real bid
        return rec
    # GUARD (Wendy Sklar, Jul 10): this rebuild exists for SPARSE asks
    # ('please send me a quote'). If their message actually DESCRIBES
    # work, it may be a DIFFERENT service — never assume last-quote
    # services onto it. Two tripwires:
    #   1. handyman-class content (drywall/painting/…) = office bids
    #      per job, never automated;
    #   2. a long, substantive message = they said what they want;
    #      rebuilding from an old quote would put words in their mouth.
    msg = rec.get("newest_message") or ""
    try:
        from email_parser import find_services
        asked = find_services(msg)
    except Exception:
        asked = []
    if "handyman" in asked:
        rec["office_alert"] = ((rec.get("office_alert") or "") +
            " 🔧 HANDYMAN work described — office bids this PER JOB; "
            "no automated price (their past quote is context only)."
            ).strip()
        return rec
    body = re.sub(r"\s+", " ", msg).strip()
    if len(body) > 240 and not asked:
        return rec       # substantive message, unrecognized work — flag
                         # path handles it; don't guess from an old quote
    services = services_from_lines(oq.get("lines"))
    if not services:
        return rec
    m = re.search(r"<([^>]+)>", rec.get("from") or "")
    customer = {"name": (rec.get("from") or "").split("<")[0].strip(),
                "email": m.group(1) if m else "",
                "phone": rec.get("phone") or "",
                "address": rec.get("address") or ""}
    draft = build(rec.get("address"), services, customer)
    if draft:
        rec["draft"] = draft
        rec["services"] = sorted(set((rec.get("services") or []) + services))
    return rec
