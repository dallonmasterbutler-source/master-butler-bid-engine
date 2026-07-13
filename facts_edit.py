"""
MASTER BUTLER — EDITABLE 'HOW IT WAS PRICED' THAT LEARNS (LaRee ×2,
Jul 10: 'if the house isn't steep pitch, the office should be able to
click moderate pitch and the system needs to learn from that' + the
Kimberly experienced-tech note: 'if the program says steep and LaRee
changed to moderate, Jobber should auto-adjust to her notes').

Three pieces:

  overrides_for(address)      → the office's corrections for this house
  apply_overrides(facts, a)   → merge them into lookup facts (wired at
                                intake, so EVERY future bid on this
                                address is born with the office truth —
                                that's the learning)
  reprice(rec, edits, by)     → apply an edit to a live record: save the
                                override, re-run the engine, swap the
                                draft (old total noted), log it.

The Jobber side follows for free: prop_info carries the corrected
pitch, so _is_tom_only / '- Other Roof' lines auto-adjust at push.
Corrections are FACTS about the house, not prices — pricing anchors
stay locked.
"""

import datetime
import json
import re
from pathlib import Path

BASE = Path(__file__).parent

# the editable facts and their allowed values (anything else = refused)
EDITABLE = {
    "pitch": ("mild", "moderate", "steep", "tom_only"),
    "stories": ("1", "2", "3", "3_exp_tech"),
    "debris": ("light", "moderate", "heavy"),
    "roof_material": ("standard", "shake", "tile", "metal"),
}


def _slug(address):
    return re.sub(r"[^a-z0-9]+", "-", (address or "").lower()).strip("-")[:60]


def _blob():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob("fact_overrides") or {}, "cloud"
    except Exception:
        pass
    p = BASE / "data" / "fact_overrides.json"
    return (json.loads(p.read_text()) if p.exists() else {}), "file"


def _save(d):
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob("fact_overrides", d)
            return
    except Exception:
        pass
    (BASE / "data" / "fact_overrides.json").write_text(json.dumps(d))


def overrides_for(address):
    if not address:
        return {}
    d, _ = _blob()
    return d.get(_slug(address)) or {}


def apply_overrides(facts, address):
    """Merge the office's corrections into lookup facts. Returns a list
    of note strings (empty = no corrections on file)."""
    ov = overrides_for(address)
    notes = []
    for k, v in ov.items():
        if k in EDITABLE and v:
            if str(facts.get(k)) != str(v):
                notes.append(f"🏠 office correction on file: {k} = {v} "
                             f"(was {facts.get(k)}, set by "
                             f"{ov.get('_by', 'office')})")
            facts[k] = v
    return notes


def set_override(address, edits, by="office"):
    """Persist corrections for this house. Returns the cleaned edits."""
    clean = {k: v for k, v in edits.items()
             if k in EDITABLE and v in EDITABLE[k]}
    if not (address and clean):
        return {}
    d, _ = _blob()
    entry = d.get(_slug(address)) or {}
    entry.update(clean)
    entry["_by"] = by
    entry["_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    d[_slug(address)] = entry
    _save(d)
    return clean


def reprice(rec, edits, by="office"):
    """Apply fact edits to a live record: save the override, re-run the
    engine at this address with the corrected facts, swap the draft.
    Mutates & returns (rec, summary_note) — caller persists.
    Never touches Jobber; the next approve-push carries the truth."""
    address = rec.get("address") or \
        ((rec.get("draft") or {}).get("customer") or {}).get("address")
    clean = set_override(address, edits, by)
    if not clean:
        return rec, "no valid edits"

    old_total = (rec.get("draft") or {}).get("total")
    services = rec.get("services") or []
    if not (address and services):
        return rec, ("correction saved for this house — no draft to "
                     "reprice (record has no services/address)")

    from pipeline import lookup, build_property
    from bid_engine import calculate_bid
    facts, flags, _ded = lookup(address)
    facts = facts or {}
    apply_overrides(facts, address)          # merge the fresh correction
    parsed = {"services": services, "address": address}
    prop, oflags = build_property(parsed, facts)
    if not prop.get("sqft"):
        return rec, "correction saved — can't reprice without sqft"
    results, notes, confidence = calculate_bid(
        dict(prop, request_date=datetime.date.today()))
    notes = list(notes)
    try:
        import lastpaid
        nm = (rec.get("caller_id") or {}).get("name") \
            or (rec.get("from") or "").split("<")[0].strip()
        notes += lastpaid.apply(results, address=address, client_name=nm)
    except Exception:
        pass
    if any("moss" in (s.get("name") or "").lower() for s in results) \
            and not any("product" in (s.get("name") or "").lower()
                        for s in results):
        results.append({"name": "Moss Treatment Product", "price": 14.50})
    total = sum(s.get("price") or 0 for s in results)

    edit_txt = ", ".join(f"{k} → {v}" for k, v in clean.items())
    summary = (f"🏠 {by} corrected the house facts ({edit_txt}) — "
               f"repriced ${old_total or 0:,.0f} → ${total:,.0f}. "
               "The system remembers this house; every future bid here "
               "starts from the office's correction.")
    d = rec.setdefault("draft", {})
    cust = d.get("customer") or {
        "name": (rec.get("from") or "").split("<")[0].strip(),
        "email": (re.search(r"<([^>]+)>", rec.get("from") or "") or
                  [None, ""])[1] if "<" in (rec.get("from") or "") else "",
        "phone": rec.get("phone") or "", "address": address}
    d["customer"] = cust
    d["bid"] = {"services": results,
                "notes": [summary] + notes + list(flags) + list(oflags),
                "confidence": confidence}
    d["prop_info"] = {k: prop.get(k) for k in
                      ("sqft", "sqft_source", "pitch", "roof_material",
                       "stories", "basement_sqft", "garage_sqft")}
    d["total"] = total
    return rec, summary


# ── UI: the editor (drops into the Site Specifications rail) ──
def editor_html(rec, stamp, back="/"):
    """Compact correction form. Self-contained inline styles on the
    design tokens; POSTs to /edit_facts."""
    import html as _h
    pi = ((rec.get("draft") or {}).get("prop_info") or {})
    ov = overrides_for(rec.get("address"))

    # BIG controls (Dallon, Jul 10 pm: 'make these bigger/taller so
    # they are easier to see and click' — LaRee's standing UI rule)
    def sel(name, label, current):
        opts = "<option value=''>—</option>" + "".join(
            f"<option value='{v}'{' selected' if str(current) == v else ''}>"
            f"{v.replace('_', ' ')}</option>" for v in EDITABLE[name])
        return (f"<label style='display:block;font-size:12px;"
                f"font-weight:800;letter-spacing:1px;text-transform:"
                f"uppercase;color:#a3adab;margin-top:14px'>{label}"
                f"<select name='{name}' style='width:100%;margin-top:6px;"
                f"background:rgba(0,0,0,.35);border:1px solid "
                f"rgba(201,162,39,.3);border-radius:11px;color:#e2e8f0;"
                f"padding:0 44px 0 14px;font:inherit;font-size:17px;"
                f"font-weight:700;cursor:pointer;height:54px;"
                # Safari draws native selects and IGNORES padding/height
                # (Dallon: 'still small on my end') — appearance:none
                # makes every browser honor the size; arrow drawn back on
                f"-webkit-appearance:none;appearance:none;"
                f"background-image:url(\"data:image/svg+xml,%3Csvg "
                f"xmlns='http://www.w3.org/2000/svg' width='16' "
                f"height='16' viewBox='0 0 24 24' fill='none' "
                f"stroke='%23c9a227' stroke-width='2.5' "
                f"stroke-linecap='round'%3E%3Cpath d='M6 9l6 6 6-6'/"
                f"%3E%3C/svg%3E\");background-repeat:no-repeat;"
                f"background-position:right 14px center'>"
                f"{opts}</select></label>")

    fixed = ("<div style='font-size:12px;color:#e8c56a;margin-top:10px'>"
             "🏠 corrections on file: "
             + ", ".join(f"{k}={v}" for k, v in ov.items()
                         if not k.startswith("_")) + "</div>") if ov else ""
    # COLLAPSED by default (Dallon, Jul 13: the open editor made the
    # right rail run past the buttons). Auto-OPEN when this house
    # already has a correction on file, so a prior edit is never
    # hidden. id='fixfacts' lets the auto-refresh stand down while it's
    # open — an in-progress edit can never be wiped by a reload.
    open_attr = " open" if ov else ""
    # its own COLOR so it stands out from the specs (Dallon, Jul 13:
    # 'make the fix-the-facts bubble a different color, easier to see')
    # — a blue-slate pill, distinct from the emerald room + gold rail.
    return (
        f"<details id='fixfacts' class='fixfacts'{open_attr} "
        f"style='margin-top:14px'>"
        f"<summary style='cursor:pointer;list-style:none;font-size:12px;"
        f"font-weight:800;letter-spacing:1.2px;text-transform:uppercase;"
        f"color:#bcd6f0;display:flex;align-items:center;gap:7px;"
        f"background:#1f3350;border:1px solid #3a5a86;border-radius:11px;"
        f"padding:12px 14px'>"
        f"<span style='font-size:14px'>✎</span> Fix the facts — reprices "
        f"&amp; remembered</summary>"
        f"<form method='POST' action='/edit_facts' style='margin-top:6px'>"
        f"<input type='hidden' name='stamp' value='{_h.escape(stamp)}'>"
        f"<input type='hidden' name='back' value='{_h.escape(back)}'>"
        + sel("pitch", "Pitch", pi.get("pitch"))
        + sel("stories", "Stories", pi.get("stories"))
        + sel("debris", "Debris", pi.get("debris_read") or pi.get("debris"))
        + sel("roof_material", "Roof", pi.get("roof_material"))
        + fixed +
        "<button style='margin-top:14px;width:100%;border-radius:999px;"
        "padding:15px;font-weight:800;font-size:15px;background:#c9a227;"
        "border:1px solid #c9a227;color:#0b3d2e;cursor:pointer'>"
        "💾 Save — reprice &amp; remember the house</button>"
        "<div style='font-size:11.5px;color:#a3adab;margin-top:7px'>Every "
        "future bid at this address starts from your correction. Prices "
        "stay locked — this edits FACTS.</div></form>")
