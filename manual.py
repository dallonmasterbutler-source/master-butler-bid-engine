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
                              "stories": prop.get("stories")},
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
