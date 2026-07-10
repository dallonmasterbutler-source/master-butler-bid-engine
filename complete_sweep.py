"""
MASTER BUTLER — COMPLETENESS FIXER (Dallon, Jul 10: "EVERYTHING that
was done on gmail, zillow, tax docs from wa state, pw bids sent to me
etc, should all be done here so they dont have to do any research.
The only way this gets used is if they trust the system.")

For every live customer record, fill what the office used to research
by hand:
  · missing address        -> their Jobber file's property address
  · missing house facts    -> county assessor (sqft/stories/roof/bsmt)
  · missing photos         -> aerial tile + street view -> cloud gallery
  · missing customer badge -> Jobber client summary
  · PW without surfaces    -> one aerial Vision survey (~2¢)

Run with DATABASE_URL set (cloud-direct). Idempotent; only fills gaps.
"""

import json
import re
import sys

import clouddb
import jobber_client as jc
from techs import tech_for


def _slug(a):
    return re.sub(r"[^a-z0-9]+", "-", (a or "").lower()).strip("-")[:60]


def _email(r):
    m = re.search(r"<([^>]+)>", r.get("from") or "")
    return m.group(1).lower() if m else None


def _skip(r, e):
    return (r.get("merged_into") or r.get("spam_auto")
            or r.get("tech_sender")
            or (e and (tech_for(e) or any(x in e for x in
                ("copycall", "getjobber", "noreply", "no-reply",
                 "masterbutlerinc", "accounts.google"))))
            or (not e and not r.get("phone")))


def run(recent_hours=None):
    """recent_hours: only records newer than N hours (the cloud's
    hourly self-heal); None = the whole backlog."""
    if not clouddb.available():
        sys.exit("DATABASE_URL not set")
    from datetime import datetime, timedelta
    floor = ((datetime.now() - timedelta(hours=recent_hours))
             .strftime("%Y%m%d-%H%M%S") if recent_hours else "")
    from property_data import geocode, _api_key
    import assessor
    key = _api_key()
    photo_refs = {p[0] for p in clouddb._exec(
        "SELECT DISTINCT ref, kind, idx FROM photos WHERE kind != 'eml'",
        (), fetch="all")}
    stats = {"addr": 0, "facts": 0, "photos": 0, "status": 0,
             "surfaces": 0}
    for stamp, rec in clouddb.all_shadow():
        if floor and stamp < floor:
            continue
        e = _email(rec)
        if _skip(rec, e):
            continue
        changed = False

        # 1) address from their Jobber file
        if not rec.get("address") and e:
            try:
                a = jc.find_client_address(e)
            except Exception:
                a = None
            if a:
                rec["address"] = a
                stats["addr"] += 1
                changed = True
        addr = rec.get("address")

        # 2) house facts from the county assessor
        pi = (rec.get("draft") or {}).get("prop_info") or {}
        if addr and not pi.get("sqft"):
            try:
                g = geocode(addr, key)
                facts = assessor.lookup(g["lat"], g["lng"]) if g else None
            except Exception:
                facts = None
            if facts and facts.get("sqft"):
                pi = rec.setdefault("draft", {}).setdefault("prop_info", {})
                pi["sqft"] = facts["sqft"]
                pi["sqft_source"] = (f"{facts['county']} County assessor "
                                     f"record")
                if facts.get("stories") and not pi.get("stories"):
                    s = facts["stories"]
                    pi["stories"] = (str(int(s)) if s == int(s) else str(s))
                if facts.get("roof_material") and not pi.get("roof_material"):
                    pi["roof_material"] = facts["roof_material"]
                for k_ in ("basement_sqft", "garage_sqft"):
                    if facts.get(k_):
                        pi[k_] = facts[k_]
                stats["facts"] += 1
                changed = True

        # 3) photos: aerial tile + street view into the cloud gallery
        if addr and _slug(addr) not in photo_refs \
                and stamp not in photo_refs:
            got = 0
            try:
                import aerial
                from imgprep import prep_jpeg_bytes
                for kind, fetch in (("aerial", aerial.fetch_tile),
                                    ("street", aerial.fetch_streetview)):
                    try:
                        p = fetch(addr)
                        if p:
                            clouddb.put_photo(_slug(addr), kind, 0,
                                              prep_jpeg_bytes(p, 1000, 72))
                            got += 1
                    except Exception:
                        continue
            except Exception:
                pass
            if got:
                photo_refs.add(_slug(addr))
                stats["photos"] += 1
                changed = True

        # 4) returning badge + Jobber link
        if e and not rec.get("customer_status"):
            try:
                cs = jc.client_summary(e)
            except Exception:
                cs = None
            if cs is not None:
                rec["customer_status"] = (
                    "new" if not cs["known"] else
                    f"returning ({cs['invoices']} jobs)" if cs["invoices"]
                    else "in Jobber — no completed jobs yet")
                if cs.get("url") and not rec.get("jobber_client_url"):
                    rec["jobber_client_url"] = cs["url"]
                stats["status"] += 1
                changed = True

        # 5) PW asks get their surfaces measured from the sky
        svcs = rec.get("services") or []
        pi = (rec.get("draft") or {}).get("prop_info") or {}
        if addr and not pi.get("aerial_surfaces") and any(
                s.startswith("pw") or s == "pressure_washing"
                for s in svcs):
            try:
                from aerial import cross_check
                afields, _ = cross_check(
                    {"surfaces": {}, "services": {"gutters": True}}, addr)
                got = afields.get("aerial_surfaces") or {}
                if got:
                    pi = rec.setdefault("draft", {}).setdefault(
                        "prop_info", {})
                    pi["aerial_surfaces"] = got
                    if afields.get("debris"):
                        pi["debris_read"] = afields["debris"]
                    stats["surfaces"] += 1
                    changed = True
            except Exception:
                pass

        if changed:
            clouddb.ingest_shadow(stamp, rec)
            print(f"  ✓ {stamp} {(rec.get('from') or '')[:40]}", flush=True)
    print("COMPLETE SWEEP DONE:", json.dumps(stats), flush=True)


if __name__ == "__main__":
    run()
