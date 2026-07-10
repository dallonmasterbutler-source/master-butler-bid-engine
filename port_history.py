"""
MASTER BUTLER — ONE-TIME HISTORY PORT (Dallon, Jul 9: Customers tab —
"port over all info for the last year... conversations time stamped so
the office can look back... like reading a text message")

Mines the Gmail Takeout mbox for the last 12 months of real customer
conversation and writes data/history_port.json:
    {email: {"name": best display name,
             "msgs": [{"at", "dir", "subject", "body"}, ...oldest-first]}}

Excluded on purpose: robots/noise senders, spam (learned lexicon),
internal mail (Dallon/Tom/company), tech field mail (tagged live
instead), Jobber transactional events. Bodies are cleaned + truncated;
caps keep the whole port a few MB.
"""

import json
import mailbox
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).parent
MBOX = Path("/Users/dallonanderson/Downloads/"
            "All mail Including Spam and Trash-002.mbox")
OUT = BASE / "data" / "history_port.json"

OFFICE = ("customercare@masterbutlerinc.com",
          "dallon.masterbutler@gmail.com")
CUTOFF = datetime.now(timezone.utc) - timedelta(days=365)

SKIP_PARTS = ("no-reply", "noreply", "donotreply", "notifications@",
              "marketing@", "newsletter", "@stripe.com", "receipts@",
              "facebookmail", "@facebook.com", "@instagram.com",
              "getjobber.com", "copycall", "squarespace.com",
              "accounts.google.com", "invoice+statements",
              "masterbutlerinc.com", "dallon.masterbutler",
              "tomfricke2007", "frickefamily07", "mailer-daemon",
              "postmaster@", "@google.com", "@apple.com", "@intuit.com")

MAX_BODY = 1200
MAX_MSGS = 200          # per counterpart


def _body_text(msg):
    """First text/plain part, decoded; falls back to stripped html."""
    def dec(part):
        try:
            return part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", "replace")
        except Exception:
            return ""
    if msg.is_multipart():
        html = ""
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                t = dec(part)
                if t.strip():
                    return t
            elif ct == "text/html" and not html:
                html = dec(part)
        return re.sub(r"<[^>]+>", " ", html)
    t = dec(msg)
    if (msg.get_content_type() or "") == "text/html":
        t = re.sub(r"<[^>]+>", " ", t)
    return t


def run():
    import msglog
    try:
        import spam_filter
    except Exception:
        spam_filter = None
    from techs import TECH_ROSTER
    skip = tuple(SKIP_PARTS) + tuple(e.lower() for e in TECH_ROSTER)

    hist = {}
    n_seen = n_kept = 0
    for msg in mailbox.mbox(str(MBOX)):
        n_seen += 1
        if n_seen % 20000 == 0:
            print(f"  …{n_seen:,} scanned, {n_kept:,} kept", flush=True)
        try:
            dt = parsedate_to_datetime(msg.get("Date") or "")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt < CUTOFF:
            continue
        frm = getaddresses([msg.get("From") or ""])
        tos = getaddresses([(msg.get("To") or "")
                            + "," + (msg.get("Cc") or "")])
        f_email = (frm[0][1] if frm else "").lower()
        outbound = any(o in f_email for o in OFFICE)
        if outbound:
            others = [(n, e.lower()) for n, e in tos
                      if e and not any(o in e.lower() for o in OFFICE)]
        else:
            others = [(frm[0][0], f_email)] if f_email else []
        if not others:
            continue
        name, email_addr = others[0]
        if any(s in email_addr for s in skip):
            continue
        subject = str(msg.get("Subject") or "")[:120]
        body = msglog.clean_body(_body_text(msg))[:MAX_BODY].strip()
        if not body and not subject:
            continue
        if spam_filter and not outbound:
            try:
                if spam_filter.looks_spam(f"{name} <{email_addr}>",
                                          subject, body)[0]:
                    continue
            except Exception:
                pass
        ent = hist.setdefault(email_addr, {"name": "", "msgs": []})
        if name and not outbound and len(name) > len(ent["name"]):
            ent["name"] = name[:60]
        ent["msgs"].append({
            "at": dt.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "dir": "out" if outbound else "in",
            "subject": subject,
            "body": body})
        n_kept += 1

    for e, ent in hist.items():
        ent["msgs"].sort(key=lambda m: m["at"])
        # collapse exact duplicates (Takeout keeps All Mail + folders)
        seen, ded = set(), []
        for m in ent["msgs"]:
            k = (m["at"], m["dir"], m["body"][:80])
            if k in seen:
                continue
            seen.add(k)
            ded.append(m)
        ent["msgs"] = ded[-MAX_MSGS:]

    OUT.write_text(json.dumps(hist))
    total = sum(len(v["msgs"]) for v in hist.values())
    print(f"DONE: {len(hist):,} correspondents, {total:,} messages "
          f"-> {OUT} ({OUT.stat().st_size/1e6:.1f} MB)", flush=True)


if __name__ == "__main__":
    run()
