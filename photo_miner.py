"""
MASTER BUTLER — PHOTO MINER

Sweeps the Takeout mailbox for CUSTOMER-ATTACHED PHOTOS (property pics:
roofs, gutters, patios, decks). These become the labeled test set for
Claude Vision — real photos, tied to real senders/addresses/quotes.

Filters to keep it signal, not noise:
  * images only, and bigger than 60 KB (kills logos, icons, signatures)
  * skips our own outbound marketing (blasts carry the same banner image)
  * saves per-email metadata so each photo stays traceable to its sender

Output: data/photos/  (gitignored — customer data never leaves this Mac)
"""

import mailbox
import hashlib
import json
import re
from pathlib import Path

MBOX = Path.home() / "Downloads" / "All mail Including Spam and Trash-002.mbox"
OUT = Path(__file__).parent / "data" / "photos"
OUT.mkdir(parents=True, exist_ok=True)

MIN_BYTES = 60_000          # smaller = logos/signature art, not property pics
OUR_DOMAIN = "masterbutlerinc.com"

seen_hashes = set()         # customers often attach the same photo twice
index = []                  # metadata for every saved photo

count_msgs = 0
count_saved = 0

if not MBOX.exists() or MBOX.stat().st_size < 1_000_000:
    raise SystemExit(f"Mailbox not found (or empty) at:\n  {MBOX}\n"
                     "Re-download the Takeout export first.")
mbox = mailbox.mbox(str(MBOX), create=False)
for msg in mbox:
    count_msgs += 1
    if count_msgs % 10000 == 0:
        print(f"  ...{count_msgs} messages scanned, {count_saved} photos saved")

    sender = str(msg.get("From", ""))
    # skip our own outbound mail — we want CUSTOMER photos
    if OUR_DOMAIN in sender.lower():
        continue

    subject = str(msg.get("Subject", ""))[:120]
    date = str(msg.get("Date", ""))[:32]

    for part in msg.walk():
        ctype = part.get_content_type()
        if not ctype.startswith("image/"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload or len(payload) < MIN_BYTES:
            continue

        digest = hashlib.sha1(payload).hexdigest()[:16]
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)

        ext = {"image/jpeg": "jpg", "image/png": "png",
               "image/heic": "heic", "image/gif": "gif",
               "image/webp": "webp"}.get(ctype, "img")
        fname = f"{digest}.{ext}"
        (OUT / fname).write_bytes(payload)
        count_saved += 1

        index.append({
            "file": fname,
            "bytes": len(payload),
            "from": re.sub(r"\s+", " ", sender)[:100],
            "subject": subject,
            "date": date,
            "original_name": part.get_filename() or "",
        })

(OUT / "photo_index.json").write_text(json.dumps(index, indent=1))
print(f"\nDONE: scanned {count_msgs} messages")
print(f"Saved {count_saved} customer photos -> data/photos/")
print(f"Index with sender/subject/date -> data/photos/photo_index.json")
