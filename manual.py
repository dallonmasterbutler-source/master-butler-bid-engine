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

    # full pipeline (property lookup + aerial + street + pricing)
    try:
        from pipeline import process
        buf = io.StringIO()
        with redirect_stdout(buf):
            draft = process(eml_path)
        record["pipeline_output"] = buf.getvalue()
        if draft:
            record["draft"] = draft
            if draft.get("customer", {}).get("address"):
                record["address"] = draft["customer"]["address"]
    except Exception as e:
        record["pipeline_error"] = str(e)

    _save(stamp, record, eml_path)
    return stamp, record


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
