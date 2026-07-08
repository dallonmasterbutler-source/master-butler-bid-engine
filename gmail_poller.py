"""
MASTER BUTLER — GMAIL SHADOW POLLER

Watches the office inbox over IMAP and runs every NEW message through the
pipeline in SHADOW MODE:

  * READONLY connection + BODY.PEEK — physically cannot mark anything read,
    move, label, or delete. The office's unread-count workflow is sacred.
  * Nothing is pushed to Jobber. Shadow drafts land in data/shadow_bids/
    for the scoreboard (system's number vs what the office actually quotes).
  * Already-processed messages are remembered in OUR ledger
    (data/processed_ids.txt) — Gmail itself is never used as a checklist.

Run once:      python3 gmail_poller.py
Keep watching: python3 gmail_poller.py --watch   (polls every 2 minutes)
"""

import email
import email.policy
import imaplib
import json
import re as _re
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
LEDGER = BASE / "data" / "processed_ids.txt"
SHADOW_DIR = BASE / "data" / "shadow_bids"


def _creds():
    creds = {}
    for line in (BASE / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    return (creds["GMAIL_ADDRESS"],
            creds["GMAIL_APP_PASSWORD"].replace(" ", ""))


def _processed():
    if LEDGER.exists():
        return set(LEDGER.read_text().split())
    return set()


def _remember(msg_id):
    LEDGER.parent.mkdir(exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(msg_id + "\n")


# Real bid requests land in spam sometimes (seen in the Takeout mining) —
# sweep it too, same readonly guarantee. Spam-found requests get flagged
# so the office knows to fish the original out.
FOLDERS = ["INBOX", "[Gmail]/Spam"]


def poll_once():
    """One pass: fetch unseen-by-US messages, shadow-process each."""
    addr, pw = _creds()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(addr, pw)
    seen = _processed()
    new_count = 0

    for folder in FOLDERS:
        typ, _ = M.select(f'"{folder}"', readonly=True)  # the safety guarantee
        if typ != "OK":
            print(f"  (cannot open {folder} — skipped)")
            continue
        typ, data = M.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []

        for num in ids:
            # stable Message-ID header (num changes; Message-ID doesn't)
            typ, hdr = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            raw_hdr = hdr[0][1].decode(errors="replace")
            msg_id = raw_hdr.split(":", 1)[-1].strip() or f"no-id-{num.decode()}"
            if msg_id in seen:
                continue

            typ, full = M.fetch(num, "(BODY.PEEK[])")   # untouched
            raw = full[0][1]
            new_count += 1
            shadow_process(raw, msg_id, folder=folder)
            _remember(msg_id)
            seen.add(msg_id)

    M.logout()
    return new_count


def shadow_process(raw_bytes, msg_id, folder="INBOX"):
    """Run one raw email through the pipeline; save the shadow draft."""
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # save the raw email so the pipeline (and any re-run) can use it
    eml_path = SHADOW_DIR / f"{stamp}.eml"
    eml_path.write_bytes(raw_bytes)

    from email_parser import parse_eml
    parsed = parse_eml(eml_path)
    record = {"message_id": msg_id, "received": stamp, "folder": folder,
              "from": f"{parsed['sender_name']} <{parsed['sender_email']}>",
              "subject": parsed["subject"], "kind": parsed["kind"],
              "services": parsed["services"], "address": parsed["address"],
              "phone": parsed.get("phone")}
    if "Spam" in folder and parsed["kind"] == "new_request":
        record["office_alert"] = ("FOUND IN SPAM — real request; office "
                                  "should rescue it from the spam folder")

    # DUPLICATE LINKING: same person/thread/address within 30 days gets
    # LINKED, never dropped — the office decides "same job" vs "new job".
    try:
        from dedup import check_duplicate
        priors = []
        for pj in sorted(SHADOW_DIR.glob("*.json")):
            pr = json.loads(pj.read_text())
            m = _re.search(r"<([^>]+)>", pr.get("from", ""))
            priors.append({
                "stamp": pj.stem,
                "sender_email": m.group(1) if m else "",
                "phone": pr.get("phone"),
                "address": pr.get("address"),
                "thread_id": None,
                "received": datetime.strptime(pj.stem, "%Y%m%d-%H%M%S"),
            })
        m = _re.search(r"<([^>]+)>", record["from"])
        verdict = check_duplicate(
            {"sender_email": m.group(1) if m else "",
             "phone": record.get("phone"),
             "address": record.get("address"),
             "received": datetime.now()}, priors)
        if verdict["verdict"] == "suspected_duplicate":
            record["duplicate_of"] = verdict["match"]["stamp"]
            record["office_alert"] = (record.get("office_alert", "") +
                f" POSSIBLE DUPLICATE of {verdict['match']['stamp']} "
                f"({verdict['reason']}) — same job or new job?").strip()
        elif verdict["verdict"] == "multi_property":
            # realty / property manager: same client, another house —
            # NEW job, own property record, notes stay per-property
            record["same_client_as"] = verdict["match"]["stamp"]
            record["office_alert"] = (record.get("office_alert", "") +
                " MULTI-PROPERTY CLIENT (same contact as "
                f"{verdict['match']['stamp']}, different address) — "
                "separate quote; keep notes on THIS property.").strip()
    except Exception:
        pass    # linking is a bonus, never a blocker

    print(f"  📧 {parsed['subject'][:60]}")
    print(f"     kind={parsed['kind']}  services={parsed['services']}")

    if parsed["kind"] == "new_request" and parsed["services"]:
        # full pipeline run (property lookup + vision on any photos + engine)
        try:
            import io
            from contextlib import redirect_stdout
            from pipeline import process
            buf = io.StringIO()
            with redirect_stdout(buf):
                draft = process(eml_path)
            record["pipeline_output"] = buf.getvalue()
            if draft:                       # structured copy for the dashboard
                record["draft"] = draft
            print("     → shadow draft generated")
        except Exception as e:
            record["pipeline_error"] = str(e)
            print(f"     → pipeline error: {e}")
    else:
        print("     → no bid needed (question/scheduling/other)")

    out = SHADOW_DIR / f"{stamp}.json"
    out.write_text(json.dumps(record, indent=1))

    # mirror to the cloud dashboard (queues locally if offline)
    try:
        from cloudpush import push_or_queue, flush_pending
        flush_pending()
        ok = push_or_queue(stamp, record)
        print("     → cloud: " + ("synced" if ok else "queued (offline)"))
    except Exception:
        pass                    # cloud mirroring never blocks shadow mode


if __name__ == "__main__":
    watch = "--watch" in sys.argv
    while True:
        n = poll_once()
        print(f"[{datetime.now():%H:%M}] poll complete — {n} new message(s)")
        if not watch:
            break
        time.sleep(120)
