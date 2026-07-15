"""One-off: re-process a single stored .eml through the CURRENT parser +
pipeline and write the refreshed record back to the cloud, preserving the
context already on the record (dedup links, open-quote, office alerts).

Used to refresh Rozi Mesaros (Jul 10): her Squarespace form gave a clear
address + service the old parser missed, so nothing was ported and the
draft held. Run:  DATABASE_URL=... python3 refresh_record.py <stamp>
"""
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import clouddb
from email_parser import parse_eml
from pipeline import process

SHADOW_DIR = Path("data/shadow_bids")


def refresh(stamp):
    if not clouddb.available():
        sys.exit("DATABASE_URL not set — refresh writes to the cloud")
    eml = SHADOW_DIR / f"{stamp}.eml"
    if not eml.exists():
        sys.exit(f"no .eml for {stamp}")

    # start from the record the cloud already has, so we keep everything
    # the pipeline doesn't recompute (dedup links, alerts, open quote)
    cloud = dict(clouddb.all_shadow())
    rec = dict(cloud.get(stamp) or {})
    if not rec:                       # fall back to the local mirror
        local = SHADOW_DIR / f"{stamp}.json"
        rec = json.loads(local.read_text()) if local.exists() else {}
    parsed = parse_eml(eml)

    # the fields the fixed parser now recovers
    if parsed.get("address"):
        rec["address"] = parsed["address"]
    if parsed.get("services"):
        rec["services"] = parsed["services"]
    if parsed.get("phone") and not rec.get("phone"):
        rec["phone"] = parsed["phone"]
    if parsed.get("newest_message"):
        # the fixed parser stores the WHOLE (tidied) request, not a
        # 300-char preview cut mid-form (Martha, Jul 15)
        rec["newest_message"] = parsed["newest_message"]
    rec["kind"] = parsed["kind"]

    # full pipeline run → priced draft + human-readable trace
    buf = io.StringIO()
    with redirect_stdout(buf):
        draft = process(eml)
    rec["pipeline_output"] = buf.getvalue()
    if draft:
        rec["draft"] = draft
        if not rec.get("address"):
            rec["address"] = (draft.get("customer") or {}).get("address")

    clouddb.ingest_shadow(stamp, rec)
    # local mirror too
    (SHADOW_DIR / f"{stamp}.json").write_text(json.dumps(rec, indent=1))

    b = (draft or {}).get("bid") or {}
    print(f"REFRESHED {stamp}")
    print(f"  address : {rec.get('address')}")
    print(f"  services: {rec.get('services')}")
    print(f"  draft   : ${(draft or {}).get('total')} "
          f"@ {b.get('confidence')}% "
          f"({len(b.get('services') or [])} line items)")


if __name__ == "__main__":
    refresh(sys.argv[1] if len(sys.argv) > 1 else "20260710-093516")
