"""
MASTER BUTLER — MANUAL LEAD ENTRY

The office (or a tech who talked to someone on the street) types in a
name + address + phone + what they want. This runs it through the SAME
pipeline an inbound email hits — property lookup, satellite + street
view, pricing — and drops a finished draft on the dashboard, exactly as
if an email had come in. No email required.

Runs in the cloud (has the API keys) writing straight to the database,
OR on the Mac (writes a local record + mirrors up). Same result either
way: a shadow record the office reviews like any other.
"""

import io
import json
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
SHADOW_DIR = BASE / "data" / "shadow_bids"

# form service keys -> plain phrases the parser understands in the body
SERVICE_PHRASES = {
    "gutters": "gutter cleaning",
    "roof": "roof blow off",
    "moss": "moss treatment",
    "windows": "window cleaning exterior",
    "windows_inout": "window cleaning inside and out",
    "driveway": "pressure wash the driveway",
    "patio": "pressure wash the patio",
    "sidewalk": "pressure wash the walkway",
    "house_wash": "house washing",
    "dryer_vent": "dryer vent cleaning",
    "holiday_lights": "holiday lights",
}


def _synth_eml(name, email, phone, address, services, extra):
    """Build a minimal customer email from the typed lead."""
    wants = ", ".join(SERVICE_PHRASES.get(s, s) for s in services)
    body = (f"Hi, this is {name}. I'd like a quote for {wants}.\n"
            f"The property address is {address}.\n"
            f"My phone is {phone}.\n")
    if extra:
        body += f"\n{extra}\n"
    hdr_from = f"{name} <{email}>" if email else name
    return (f"From: {hdr_from}\r\n"
            f"Subject: Service request (entered by office)\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}").encode()


def process_manual(name, address, phone="", email="", services=None,
                   extra="", entered_by="office"):
    """Run a typed lead through the full pipeline. Returns (stamp, record).
    Saves it wherever records live (cloud DB or local + mirror)."""
    services = services or []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    eml_path = SHADOW_DIR / f"{stamp}.eml"
    eml_path.write_bytes(_synth_eml(name, email, phone, address, services, extra))

    from email_parser import parse_eml
    parsed = parse_eml(eml_path)

    record = {
        "message_id": f"manual-{stamp}",
        "received": stamp, "folder": "MANUAL",
        "from": f"{name} <{email}>" if email else f"{name} (manual entry)",
        "subject": "Service request (entered by office)",
        "kind": "new_request",
        "services": parsed.get("services") or services,
        "address": parsed.get("address") or address,
        "phone": parsed.get("phone") or phone,
        "sched_pref": parsed.get("sched_pref"),
        "tech_request": parsed.get("tech_request"),
        "newest_message": parsed.get("newest_message"),
        "office_alert": f"MANUAL ENTRY — added by {entered_by} "
                        "(e.g. a tech's curbside lead). Ran through the full "
                        "pipeline like an inbound email.",
    }

    # LIGHT pipeline: property lookup + pricing (fast, ~3s — no satellite
    # Vision). A curbside quick-quote wants a fast number; the full
    # satellite/street analysis is what inbound emails get on the Mac.
    # (Records with no property data can be re-run deep later.)
    try:
        from pipeline import lookup, build_property, SERVICE_TO_ENGINE
        from bid_engine import calculate_bid
        addr = parsed.get("address") or address
        facts, flags, deduction = lookup(addr) if addr else ({}, [], 0)
        prop, oflags = build_property(parsed, facts or {})
        try:                       # office playbook (seasons/dependencies)
            import seasons
            s_alert, s_notes = seasons.check(parsed, prop)
            oflags += s_notes
            if s_alert:
                oflags.append(s_alert)
                record["office_alert"] += " " + s_alert
        except Exception:
            pass
        out_lines = [f"MANUAL LEAD — {name}", f"Address: {addr}"]
        if not prop.get("sqft"):
            record["office_alert"] += (" No property size found — office "
                                       "verifies before pricing.")
            record["pipeline_output"] = "\n".join(
                out_lines + ["No square footage — office must supply it."])
            record["draft"] = {"customer": {"name": name, "email": email,
                               "phone": phone, "address": addr},
                               "bid": {"services": [], "notes": flags + oflags,
                                       "confidence": 0}, "prop_info": {},
                               "total": 0}
        else:
            results, notes, confidence = calculate_bid(prop)
            confidence = max(0, confidence - deduction)
            try:
                import minimums
                mnotes = minimums.apply(results, email=email, phone=phone,
                                        address=addr)
                notes += mnotes
                if any("REVIEW" in n for n in mnotes):
                    confidence = min(confidence, 45)
            except Exception:
                pass
            # never quote a returning customer less than their last
            # invoice (Martha's Robert Lin catch, Jul 10)
            try:
                import lastpaid
                lnotes = lastpaid.apply(results, address=addr,
                                        client_name=name)
                notes += lnotes
                if any("REVIEW" in n for n in lnotes):
                    confidence = min(confidence, 45)
            except Exception:
                pass
            # exterior → in&out upsell note (LaRee, Jul 10)
            try:
                import bid_engine as _be
                _names = [(s.get("name") or "").lower() for s in results]
                if any("exterior" in n and "window" in n for n in _names) \
                        and not any("in & out" in n for n in _names) \
                        and prop.get("sqft"):
                    _est = max(_be.round_to_5(
                        prop["sqft"] * _be.RATES["windows_in_out"]),
                        _be.WINDOWS_INOUT_MINIMUM)
                    notes.append(f"💡 UPSELL: they asked exterior-only — "
                                 f"also offer Windows In & Out at "
                                 f"≈${_est:,.0f}.")
            except Exception:
                pass
            # moss product rides with moss labor (Martha, Jul 10)
            if any("moss" in (s.get("name") or "").lower()
                   for s in results) \
                    and not any("product" in (s.get("name") or "").lower()
                                for s in results):
                results.append({"name": "Moss Treatment Product",
                                "price": 14.50})
                notes.append("Moss Treatment Product $14.50 added "
                             "automatically (1-3 canisters typical — "
                             "tech confirms on-site).")
            total = sum(s["price"] for s in results)
            record["draft"] = {
                "customer": {"name": name, "email": email, "phone": phone,
                             "address": addr},
                "bid": {"services": results, "notes": notes + flags + oflags,
                        "confidence": confidence},
                "prop_info": {"sqft": prop.get("sqft"),
                              "sqft_source": facts.get("sqft_source"),
                              "pitch": prop.get("pitch"),
                              "roof_material": prop.get("roof_material"),
                              "stories": prop.get("stories"),
                              "basement_sqft": prop.get("basement_sqft"),
                              "garage_sqft": prop.get("garage_sqft")},
                "total": total}
            for s in results:
                out_lines.append(f"  {s['name']:<34} ${s['price']}")
            out_lines.append(f"  TOTAL ${total}  (confidence {confidence}%)")
            out_lines += [f"  ⚠ {n}" for n in (notes + flags + oflags)]
            record["pipeline_output"] = "\n".join(out_lines)
        record["address"] = addr
    except Exception as e:
        import traceback
        record["pipeline_error"] = traceback.format_exc()[-500:]

    # DUPLICATE GUARD: the office may not know a request is already on
    # the queue (customer emailed AND called). Warn, don't block.
    try:
        import re as _re
        from dns_check import canon_addr
        want_addr = canon_addr(record.get("address") or "")
        want_email = (email or "").lower()
        import clouddb
        source = (clouddb.all_shadow() if clouddb.available() else
                  [(pp.stem, __import__("json").loads(pp.read_text()))
                   for pp in sorted(SHADOW_DIR.glob("*.json"))])
        for s, r in source:
            if s == stamp:
                continue
            m = _re.search(r"<([^>]+)>", r.get("from") or "")
            r_email = m.group(1).lower() if m else ""
            same_email = want_email and want_email == r_email
            same_addr = (want_addr and
                         canon_addr(r.get("address") or "") == want_addr)
            if same_email or same_addr:
                record["office_alert"] = (
                    (record.get("office_alert") or "") +
                    f" ⚠ POSSIBLE DUPLICATE of an existing item on the "
                    f"dashboard (same {'email' if same_email else 'address'}"
                    f": {(r.get('from') or '')[:40]}). Check before "
                    "quoting twice.").strip()
                break
    except Exception:
        pass

    # DO-NOT-SERVICE GUARD: manual entries get the same door check.
    try:
        import dns_check
        hit = dns_check.check(email=email, phone=phone,
                              address=record.get("address"), name=name)
    except Exception:
        hit = None
    if hit:
        record["dns_match"] = hit
        record["office_alert"] = (
            f"⛔ DO NOT SERVICE — matches '{hit['name']}' in Jobber "
            f"(matched by {hit['matched_by']}). Do not quote or schedule.")

    # KNOWN IN JOBBER? Same lookups an inbound email gets (Martha's
    # catch, Jul 9: her New-lead test didn't grab her Jobber account) —
    # email → client record; no match → phone → caller-ID; plus the
    # open-quote check so a manual entry can't double-quote either.
    if not hit:
        try:
            import jobber_client as jc
            cs = jc.client_summary(email) if email else None
            if cs is not None and cs.get("known"):
                record["customer_status"] = (
                    "in Jobber — no completed jobs yet"
                    if cs.get("invoices", 0) == 0
                    else f"returning ({cs['invoices']} jobs)")
                if cs.get("url"):
                    record["jobber_client_url"] = cs["url"]
            elif phone:
                cid = jc.caller_id(phone)
                if cid:
                    record["caller_id"] = cid
                    if cid.get("url"):
                        record["jobber_client_url"] = cid["url"]
                    record["customer_status"] = (
                        f"returning ({cid.get('invoices', 0)} jobs)")
                    record["office_alert"] = (
                        (record.get("office_alert") or "") +
                        f" 👤 EXISTING JOBBER CLIENT (matched by phone): "
                        f"{cid['name']}, {cid.get('invoices', 0)} past "
                        f"job(s)"
                        + (f", {cid['address']}" if cid.get("address")
                           else "") + ".").strip()
            if record.get("customer_status") is None and (email or phone):
                record["customer_status"] = "new"
            if email and not record.get("customer_status", "").startswith("new"):
                oq = jc.find_open_quote(email, scan=80)
                if oq:
                    record["open_quote_ctx"] = {
                        "number": oq["quoteNumber"],
                        "status": oq["quoteStatus"],
                        "total": oq["amounts"]["total"],
                        "created": (oq.get("createdAt") or "")[:10],
                        "url": oq.get("jobberWebUri")}
                    record["office_alert"] = (
                        (record.get("office_alert") or "") +
                        f" 📎 EXISTING OPEN QUOTE #{oq['quoteNumber']} "
                        f"(${oq['amounts']['total']}, {oq['quoteStatus']})"
                        " — reply on that quote, don't make a second "
                        "one.").strip()
        except Exception:
            pass          # Jobber enrichment is a bonus, never a blocker

    _save(stamp, record, eml_path)
    _imagery_async(stamp, record, eml_path)
    return stamp, record


def _imagery_async(stamp, record, eml_path):
    """Manual bids deserve the same pictures email bids get. The office
    got their ~3s quote already; fetch the aerial tile + street view in
    a short background thread and re-save — the photos appear on the
    bid page on its next auto-refresh. (Bounded: each fetch has its own
    urllib timeout inside aerial.py.)"""
    import threading

    def work():
        try:
            import aerial
            addr = record.get("address")
            if not addr:
                return
            try:
                aerial.fetch_tile(addr)
            except Exception:
                pass
            try:
                aerial.fetch_streetview(addr)
            except Exception:
                pass
            _save(stamp, record, eml_path)   # upserts record + photos
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()


def _save(stamp, record, eml_path):
    """Persist the record: cloud DB directly when we're the cloud,
    otherwise local file + courier mirror (same as the poller)."""
    try:
        import clouddb
        if clouddb.available():
            clouddb.ingest_shadow(stamp, record)
            try:
                from cloudpush import gather_photos
                import base64
                for ph in gather_photos(stamp, record):
                    clouddb.put_photo(ph["ref"], ph["kind"], ph["idx"],
                                      base64.b64decode(ph["b64"]))
            except Exception:
                pass
            return
    except Exception:
        pass
    (SHADOW_DIR / f"{stamp}.json").write_text(json.dumps(record, indent=1))
    try:
        from cloudpush import push_or_queue
        push_or_queue(stamp, record)
    except Exception:
        pass


if __name__ == "__main__":
    s, r = process_manual("Curbside Carl", "9209 190th Ave SE, Snohomish, WA 98290",
                          phone="425-555-0199", services=["gutters", "windows"],
                          entered_by="demo")
    print(f"created {s}: {r.get('draft', {}).get('total')} "
          f"(kind {r['kind']}, {len(r.get('services', []))} services)")
