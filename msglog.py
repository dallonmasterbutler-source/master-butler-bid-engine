"""
MASTER BUTLER — LIVE MESSAGE LOG (Dallon, Jul 8: "make this a LIVE
system... the office sees in and out messages cleaned up on our site,
responds from there; Gmail becomes only-when-needed")

One shared ledger of customer conversation, both directions:

  {"at","dir"("in"/"out"),"addr","name","subject","body","by","stamp"}

Writers: the poller (inbound + a Sent-folder sweep for anything the
office still sends from Gmail) and the dashboard reply box.
Reader: the dashboard Messages page, grouped into threads by address.
Storage: cloud blob 'message_log' (capped) with a local-file fallback.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
LOCAL = BASE / "data" / "message_log.json"
CAP = 600

# senders that are machines, not customers — never worth a thread
ROBOT_HINTS = ("noreply", "no-reply", "notifications@", "mailer-daemon",
               "@txn.getjobber", "@copycall", "@squarespace",
               "@google.com", "@stripe", "@intuit")


def _load():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob("message_log") or [], "db"
    except Exception:
        pass
    if LOCAL.exists():
        return json.loads(LOCAL.read_text()), "file"
    return [], "file"


def _save(log, channel):
    log = log[-CAP:]
    if channel == "db":
        import clouddb
        clouddb.put_blob("message_log", log)
    else:
        LOCAL.write_text(json.dumps(log))
        try:
            from cloudpush import push
            push(blobs={"message_log": log})
        except Exception:
            pass


def clean_body(text, limit=1200):
    """Strip quoted history, signatures, and form skeletons down to the
    words a human actually typed."""
    if not text:
        return ""
    # Website form submissions: summarize the skeleton, keep their words
    if re.search(r"Name:\s*\n", text) and re.search(r"Email( Address)?:",
                                                    text):
        parts = ["📋 Website quote request"]
        svc = re.search(r"(?:IN-HOUSE )?SERVICES[^\n]*:\s*\n?\s*([^\n]{3,90})",
                        text, re.I)
        adr = re.search(r"\bAddress[^\n:]{0,20}:\s*\n?\s*([^\n]{6,80})", text)
        msg = re.search(r"(?:MESSAGE|Comments?)\s*:?\s*\n\s*(.{10,400})",
                        text, re.S | re.I)
        if svc:
            parts.append("Services: " + svc.group(1).strip())
        if adr:
            parts.append("Address: " + adr.group(1).strip())
        if msg:
            words = re.sub(r"\s+", " ", msg.group(1)).strip()
            parts.append("“" + words[:300] + "”")
        return "\n".join(parts)
    # reply headers WRAP across lines in real mail ("On Thu, Jul 9 …
    # <address\n…> wrote:") — cut at the header's START, dot spanning
    # newlines (Dallon's 'say quotes carry reply tails', fixed Jul 10)
    m = re.search(r"(?:^|\n)\s*On .{5,220}?wrote:", text, re.S)
    if m:
        text = text[:m.start()]
    # flattened mail loses its newlines — cut on the date-anchored
    # header mid-line ("… On Jul 9, 2026, at 12:17 PM, Master Butler…")
    m = re.search(r"\bOn (?:[A-Z][a-z]{2,8},? )?[A-Z][a-z]{2,8} "
                  r"\d{1,2}, \d{4},? at ", text)
    if m:
        text = text[:m.start()]
    text = re.sub(r"\s*Sent from my [A-Za-z ]{2,24}", " ", text)
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(">") or re.match(r"^On .{5,80} wrote:$", s):
            break
        if re.match(r"^-+\s*(Original|Forwarded) message", s, re.I):
            break
        if re.match(r"^Sent from (my|Yahoo|Outlook|Gmail)", s, re.I):
            continue                          # phone signatures
        out.append(ln)
    body = "\n".join(out)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:limit]


def record(direction, addr, name="", subject="", body="", by="",
           stamp="", at=None):
    addr = (addr or "").strip().lower()
    if not addr or any(h in addr for h in ROBOT_HINTS):
        return False
    log, channel = _load()
    key = (direction, addr, (subject or "")[:60], (body or "")[:80])
    for m in log[-80:]:                       # cheap de-dupe window
        if (m["dir"], m["addr"], (m.get("subject") or "")[:60],
                (m.get("body") or "")[:80]) == key:
            return False
    log.append({"at": at or datetime.now(timezone.utc)
                .isoformat(timespec="seconds"),
                "dir": direction, "addr": addr, "name": (name or "")[:60],
                "subject": (subject or "")[:120],
                "body": clean_body(body), "by": by, "stamp": stamp})
    _save(log, channel)
    return True


def threads():
    """Newest-first list of (addr, display_name, messages_newest_last)."""
    log, _ = _load()
    by_addr = {}
    for m in log:
        by_addr.setdefault(m["addr"], []).append(m)
    out = []
    for addr, msgs in by_addr.items():
        msgs.sort(key=lambda m: m["at"])
        name = next((m["name"] for m in reversed(msgs)
                     if m.get("name") and m["name"] != "None"), addr)
        out.append((addr, name, msgs))
    out.sort(key=lambda t: t[2][-1]["at"], reverse=True)
    return out
