"""
MASTER BUTLER — OUTBOUND MAIL (internal only)

One job: when LaRee clicks 🚩 on a bid, email the review to Tom and
Dallon. Sends from the same customercare@ account the poller reads,
via Gmail SMTP. INTERNAL RECIPIENTS ONLY — this module never emails
customers, and refuses any address not on the allow-list.

Follows the KEY-READING RULE: .env file first, then os.environ
(the cloud has no .env).
"""

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).parent

TOM = "tomfricke2007@gmail.com"
DALLON = "dallon.masterbutler@gmail.com"
ALLOWED = {TOM, DALLON}


def _creds():
    creds = {}
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    addr = creds.get("GMAIL_ADDRESS") or os.environ.get("GMAIL_ADDRESS")
    pw = (creds.get("GMAIL_APP_PASSWORD")
          or os.environ.get("GMAIL_APP_PASSWORD", ""))
    return addr, pw.replace(" ", "")


def send_internal(subject, body, to=(TOM, DALLON)):
    to = [t for t in to if t in ALLOWED]
    if not to:
        return False, "no allowed recipients"
    addr, pw = _creds()
    if not (addr and pw):
        return False, "no mail credentials"
    msg = EmailMessage()
    msg["From"] = f"Master Butler Bidding <{addr}>"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(addr, pw)
            s.send_message(msg)
        return True, "sent"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def send_review_flag(bid, note="", link=""):
    """The 🚩 button: LaRee (or anyone) flags a bid for Tom & Dallon."""
    cust = bid.get("customer") or bid.get("from") or "unknown"
    total = bid.get("total")
    lines = [f"LaRee flagged a bid for review.",
             "",
             f"Customer: {cust}",
             f"System total: ${total}" if total else "",
             f"Note: {note}" if note else "",
             f"Dashboard: {link}" if link else "",
             "",
             "(sent automatically by the Master Butler bidding system)"]
    return send_internal(f"🚩 Review requested: {cust}",
                         "\n".join(x for x in lines if x != ""))


if __name__ == "__main__":
    addr, pw = _creds()
    print(f"account: {addr}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(addr, pw)
    print("SMTP login OK (no mail sent)")
