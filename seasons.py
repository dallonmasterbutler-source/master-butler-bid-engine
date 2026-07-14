"""
MASTER BUTLER — THE OFFICE PLAYBOOK, AS CODE
(Jul 9: Martha — "make sure the system knows what we can and can't do
separately, and the time of year"; Jessica/LaRee's list of very
specific situations. Every rule below cites their own documents.)

check(parsed, prop, when=None) -> (office_alert_or_None, [notes])
  · notes  → amber ⚠ stack on the bid page
  · alert  → the ONE badge-worthy fact for the queue row

Rules never block or reprice anything — they put the office's own
playbook in front of whoever reviews the bid.
"""

from datetime import date

# ── geography (doc: "4. Office Procedures and Help") ─────────
REFER_FRICKE = {"lake stevens", "seattle", "everett"}
FRICKE = "David Fricke — westsidewindowsinc.com, (425) 347-3921"
NIELSEN = ("Chris Nielsen — procleangutters@gmail.com, "
           "goteamproclean.com, (425) 238-1523")
# cities named as ours in the training docs (Monroe base + 405 corridor)
IN_AREA = {"monroe", "snohomish", "duvall", "sammamish", "woodinville",
           "bellevue", "kirkland", "redmond", "bothell", "issaquah",
           "carnation", "mill creek", "maltby", "clearview", "sultan",
           "gold bar", "startup", "kenmore", "fall city", "renton",
           "newcastle", "mercer island", "north bend", "snoqualmie"}

_WINTER_PAUSED = ("window", "pressure", "house_wash", "driveway",
                  "patio", "deck", "sidewalk", "solar_panel")
_GUTTER_ROOF = ("gutter", "roof_blow_off", "moss")


def _city(address):
    a = (address or "").lower()
    # ANOTHER STATE entirely (Martha's Heber City UT test sailed
    # through silently) — that's the loudest out-of-area there is
    import re
    m = re.search(r"\b(ut|utah|or|oregon|id|idaho|ca|california|az|"
                  r"arizona|nv|nevada|mt|montana|tx|texas|fl|florida|"
                  r"co|colorado)\.?\s+\d{5}", a)
    if m and not re.search(r"\bwa\b|\bwashington\b", a):
        return "__out_of_state__"
    for c in sorted(REFER_FRICKE | IN_AREA, key=len, reverse=True):
        if c in a:
            return c
    # try "…, City, WA" shape for cities we don't know
    m = re.search(r",\s*([a-z .]+?),?\s*(?:wa|washington)\b", a)
    return m.group(1).strip() if m else None


def _in_pause_window(d):
    """Window cleaning + pressure washing suspend Oct 15 → late Feb
    (doc: '10. Winter Gutters'; Jessica: 'resume late February')."""
    return (d.month, d.day) >= (10, 15) or (d.month, d.day) <= (2, 20)


def _lights_window(d):
    """Holiday lights are up Oct–Feb (Jessica: 'gutters can't be done
    once holiday lights are installed, Oct–February')."""
    return d.month >= 10 or d.month <= 2


def check(parsed, prop=None, when=None):
    d = when or date.today()
    services = parsed.get("services") or []
    text = ((parsed.get("newest_message") or "") + " "
            + (parsed.get("subject") or "")).lower()
    svc_blob = " ".join(services)
    prop = prop or {}
    alert, notes = None, []

    def has(*words, where=None):
        blob = where if where is not None else (svc_blob + " " + text)
        return any(w in blob for w in words)

    # 1 · ROOF BLOW-OFF NEEDS GUTTERS (Jessica: "we tell people they
    #     have to book a gutter cleaning with roof blow off, unless
    #     they have gutter guards")
    if "roof_blow_off" in services and "gutter_cleaning" not in services \
            and "guards" not in svc_blob and "gutter guard" not in text:
        alert = alert or ("Roof blow-off is only offered WITH a gutter "
                          "cleaning unless the home has gutter guards "
                          "(office rule) — confirm before quoting.")

    # 1b · DRYER VENT UP HIGH (LaRee, Jul 13: "even at 100% confidence,
    #      some dryer vents are on the 3rd story — we need to KNOW").
    #      A simple 1-2 story vent is anyone's job; 3+ stories (or the
    #      customer saying it's up high) needs eyes before booking.
    if "dryer_vent" in services:
        try:
            _st = float(prop.get("stories")) if prop.get("stories") \
                else None
        except (TypeError, ValueError):
            _st = None
        if (_st and _st >= 3) or has("third story", "3rd story",
                                     "third floor", "3rd floor",
                                     "high vent", "up high"):
            alert = alert or (
                "DRYER VENT MAY BE UP HIGH (3-story home or the customer "
                "says so) — check photos & Must-Know before booking; NOT "
                "an anyone-can-do-it job.")

    # 1c · REALTY / PROPERTY-MANAGEMENT LANGUAGE (Dallon, Jul 14 —
    #      the Eli DeBerry lesson: ~25 realtors in our last year).
    #      A realtor's houses must never share pricing anchors, and
    #      their timing runs on LISTING deadlines, not seasons.
    _realty_hits = [w for w in (
        "listing", "tenant", "rental property", "property manager",
        "property management", "my client", "closing date", "escrow",
        "staging", "vacant", "on the market", "open house",
        "photographed for sale", "my seller", "my buyer",
        "move-out", "move out clean") if w in text]
    _realty_dom = any(d in text[:0] or d in (parsed.get("sender_email")
                      or parsed.get("from") or "").lower() for d in (
        "windermere", "johnlscott", "compass.com", "remax", "kw.com",
        "realty", "realtor", "cbbain", "sotheby", "century21",
        "exprealty", "homesmart"))
    if _realty_hits or _realty_dom:
        alert = alert or (
            "🏘 REALTY/PM SIGNALS (" +
            (", ".join(_realty_hits[:3]) if _realty_hits
             else "realty email domain") +
            ") — likely multiple properties: confirm WHICH house, price "
            "per property (never reuse another house's numbers), and ask "
            "for their deadline — realtors run on listing dates, not "
            "seasons.")

    # 2 · MOSS REMOVAL IS AUGUST-ONLY (docs 9.2 + quick responses)
    if has("moss_removal", "moss removal"):
        if d.month == 8:
            notes.append("Moss removal: August season is NOW — removal "
                         "includes a gutter cleaning; presidential/"
                         "air-compressor or Tom-only roofs go on Tom's "
                         "4th week of August.")
        elif d.month in (5, 6, 7):
            notes.append("Moss removal happens in AUGUST. Treatment "
                         "(~$150) needs 4–6 weeks to work first — add "
                         "the moss-treatment option to the quote now.")
        else:
            notes.append("Moss removal is August-only — offer moss "
                         "TREATMENT now (removal next August). Use the "
                         "'Moss Removal Request' quick response.")

    # 3 · WINDOWS + PRESSURE WASHING PAUSE OCT 15 → LATE FEB
    if _in_pause_window(d) and has(*_WINTER_PAUSED, where=svc_blob):
        notes.append("Windows/pressure washing are SUSPENDED for the "
                     "season (Oct 15 → late Feb, office rule) — quote "
                     "for the spring list, say so in the reply.")

    # 4 · HOLIDAY-LIGHTS DISCLAIMER ON WINTER GUTTER/ROOF QUOTES
    if _lights_window(d) and has(*_GUTTER_ROOF, where=svc_blob):
        notes.append("Oct–Feb: confirm no holiday lights are up — "
                     "gutters can't be cleaned with lights installed. "
                     "Add the disclaimer to the quote description.")

    # 5 · SKYLIGHTS ON HIGH-RISK ROOFS (doc 9.1)
    if "skylight" in text and has("window", where=svc_blob):
        msg = ("Skylights mentioned: if this roof is Tom-only/high-risk "
               "our techs can't reach them — skylights NOT included; "
               "remove skylight verbiage and note it on the quote "
               "(office rule 9.1).")
        if (prop.get("roof_material") or "") == "metal":
            alert = alert or msg
        else:
            notes.append(msg)

    # 6 · PATIO/DECK PRESSURE WASHING → MOVE-FURNITURE LINE (doc 9.1)
    if has("patio", "deck") and has("pressure", "wash"):
        notes.append("Add “move furniture” to all patio/deck pressure-"
                     "washing quotes (office rule).")

    # 7 · NEW-CONSTRUCTION WINDOWS: DO NOT BOOK (doc 9.1)
    if has("new construction", "new build", "post construction",
           "construction debris", where=text) \
            and has("window", where=svc_blob + " " + text):
        alert = ("NEW-CONSTRUCTION windows — we do NOT book these. "
                 "Ask for pictures and send to TOM (not Spencer) to "
                 "confirm we won't service.")

    # 8 · FRENCH PANES rate rule (doc 9.5)
    if has("french pane", "french-pane", "divided lite", where=text):
        notes.append("French panes: all = 2× window rate, partial = "
                     "+50%, under half = +25% (water-fed-pole exterior "
                     "only = no increase). Send home + pricing to "
                     "Dallon to double-check.")

    # 8b · TOM'S ONE-OFF PRICING ANCHORS (learned from his office
    #      threads, Jul 9 — Dallon: "needs to be learned from")
    if has("solar panel", where=text) and has("pressure", "wash", "clean",
                                              where=text):
        notes.append("Solar panels: $10 per panel per side to pressure "
                     "wash (Tom's ruling, Jul 2026; was $6 — raised). "
                     "$27/side + materials if painting.")
    if has("paint", "painting", where=text) and has("exterior", "house",
                                                    "siding", where=text):
        notes.append("Exterior painting: Tom's anchor ≈$3,200 labor + "
                     "paint (includes pressure-wash prep) — he usually "
                     "wants to see the house in person first. Custom "
                     "quote, forward to Tom.")
    if has("stain", "staining", where=text) and "fence" in text:
        notes.append("Fence staining: $27 per side per panel + materials "
                     "(Tom, Jul 2026). Pressure wash prep: $6 per panel "
                     "per side.")

    # 9 · RUST STAINS: we don't (doc 9.5)
    if "rust" in text and has("pressure", "wash", "driveway",
                              "concrete"):
        notes.append("Rust stains: we do NOT remove them (acid is too "
                     "hazardous for our trucks) — customer can apply "
                     "a Home Depot product at their own risk.")

    # 10 · OUT-OF-AREA REFERRALS (doc: Office Procedures)
    city = _city(parsed.get("address"))
    if city == "__out_of_state__":
        alert = ("Address appears to be OUTSIDE WASHINGTON — we don't "
                 "service there. Double-check the address (typo?) before "
                 "declining.")
    elif city in REFER_FRICKE:
        alert = (f"OUT OF AREA ({city.title()}) — office refers these "
                 f"to {FRICKE}. Don't quote.")
    elif city is None and parsed.get("address"):
        pass                          # no city found — stay quiet
    elif city not in IN_AREA and city is not None:
        alert = alert or (f"'{city.title()}' may be OUTSIDE our service "
                          f"area — if we don't serve it, refer to "
                          f"{NIELSEN}.")

    return alert, notes
