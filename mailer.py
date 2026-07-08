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


def _oauth_env():
    creds = {}
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    out = {}
    for k in ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET",
              "GMAIL_OAUTH_REFRESH_TOKEN"):
        out[k] = creds.get(k) or os.environ.get(k)
    return out if all(out.values()) else None


def _api_send(msg):
    """Gmail API send (HTTPS 443 — works from the cloud, where SMTP
    ports are blocked). Send-only OAuth scope; reading stays on IMAP."""
    import base64
    import json
    import urllib.parse
    import urllib.request
    o = _oauth_env()
    if not o:
        return False, "no oauth credentials"
    try:
        body = urllib.parse.urlencode({
            "client_id": o["GMAIL_OAUTH_CLIENT_ID"],
            "client_secret": o["GMAIL_OAUTH_CLIENT_SECRET"],
            "refresh_token": o["GMAIL_OAUTH_REFRESH_TOKEN"],
            "grant_type": "refresh_token"}).encode()
        tok = json.load(urllib.request.urlopen(
            "https://oauth2.googleapis.com/token", body, timeout=20))
        access = tok["access_token"]
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=json.dumps({"raw": raw}).encode(),
            headers={"Authorization": f"Bearer {access}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        return True, "sent (gmail api)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _smtp_send(msg, addr, pw):
    """Try SSL:465 then STARTTLS:587 (some hosts block one, not both)."""
    last = None
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(addr, pw)
            s.send_message(msg)
        return True, "sent (465)"
    except Exception as e:
        last = e
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(addr, pw)
            s.send_message(msg)
        return True, "sent (587)"
    except Exception as e:
        last = e
    return False, f"{type(last).__name__}: {last}"


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
    ok, why = _api_send(msg)          # works everywhere, cloud included
    if not ok:
        ok, why = _smtp_send(msg, addr, pw)
    if ok:
        return ok, why
    # SMTP blocked here (Render free tier blocks mail ports): queue the
    # message in the cloud DB; the Mac relays it next time it checks in.
    queued = _queue_outbox(subject, body, to)
    return False, (f"queued for Mac relay ({why})" if queued
                   else f"send failed, queue failed ({why})")


def _queue_outbox(subject, body, to):
    try:
        import clouddb
        if not clouddb.available():
            return False
        from datetime import datetime
        box = clouddb.get_blob("mail_outbox") or []
        box.append({"at": datetime.now().isoformat(timespec="seconds"),
                    "subject": subject, "body": body, "to": list(to)})
        clouddb.put_blob("mail_outbox", box[-50:])   # bounded
        return True
    except Exception:
        return False


def _outbox_read():
    """Direct DB when possible (cloud); otherwise over HTTPS through the
    dashboard — the Mac is stdlib-only, no Postgres driver."""
    import clouddb
    if clouddb.available():
        return clouddb.get_blob("mail_outbox") or [], "db"
    import json
    import urllib.request
    from base64 import b64encode
    from cloudpush import _cfg
    url, pw = _cfg("DASHBOARD_URL"), _cfg("DASHBOARD_PASSWORD")
    if not (url and pw):
        return [], None
    req = urllib.request.Request(
        url.rstrip("/") + "/api/blob/mail_outbox",
        headers={"Authorization": "Basic "
                 + b64encode(f"office:{pw}".encode()).decode()})
    return json.load(urllib.request.urlopen(req, timeout=60)), "http"


def _outbox_write(remaining, channel):
    import clouddb
    if channel == "db" and clouddb.available():
        clouddb.put_blob("mail_outbox", remaining)
    elif channel == "http":
        from cloudpush import push
        push(blobs={"mail_outbox": remaining})


def drain_outbox():
    """Run wherever SMTP works (the Mac): send every queued message.
    Called by night_run and the poller; safe to run any time."""
    try:
        box, channel = _outbox_read()
        if not box or channel is None:
            return 0
        addr, pw = _creds()
        remaining, sent = [], 0
        for m in box:
            to = [t for t in m.get("to", []) if t in ALLOWED]
            if not to:
                continue
            msg = EmailMessage()
            msg["From"] = f"Master Butler Bidding <{addr}>"
            msg["To"] = ", ".join(to)
            msg["Subject"] = m["subject"]
            msg.set_content(m["body"] + f"\n\n(queued {m['at']}, relayed later)")
            ok, _ = _api_send(msg)
            if not ok:
                ok, _ = _smtp_send(msg, addr, pw)
            if ok:
                sent += 1
            else:
                remaining.append(m)
        if sent or remaining != box:
            _outbox_write(remaining, channel)
        return sent
    except Exception:
        return 0


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
