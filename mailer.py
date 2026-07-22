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
import re
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
    if all(out.values()):
        return out
    # the CLOUD has no .env oauth lines — the shared store carries the
    # same send-only credentials (saved Jul 14, verified by test send)
    try:
        import clouddb
        if clouddb.available():
            b = clouddb.get_blob("gmail_oauth") or {}
            if b.get("refresh_token"):
                return {"GMAIL_OAUTH_CLIENT_ID": b["client_id"],
                        "GMAIL_OAUTH_CLIENT_SECRET": b["client_secret"],
                        "GMAIL_OAUTH_REFRESH_TOKEN": b["refresh_token"]}
    except Exception:
        pass
    return None


def _api_send(msg, thread_id=None):
    """Gmail API send (HTTPS 443 — works from the cloud, where SMTP
    ports are blocked). Send-only OAuth scope; reading stays on IMAP.
    thread_id nests the message INTO the customer's existing Gmail thread
    (the header alone doesn't reliably thread — Jessica's Jul-20 report)."""
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
        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=json.dumps(payload).encode(),
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


def send_internal(subject, body, to=(TOM, DALLON), html=None,
                  attachment=None):
    """attachment = (filename, bytes) — used by the nightly backup so the
    restore point lands OFF-SITE in Gmail even when no Mac is awake to
    pull it (Jul 21 audit: the Mac's pull had been dead since Jul 16)."""
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
    msg.set_content(body)                 # plain-text fallback
    if html:                              # richer, scannable version
        msg.add_alternative(html, subtype="html")
    if attachment:
        fname, blob = attachment
        msg.add_attachment(blob, maintype="application",
                           subtype="gzip", filename=fname)
    ok, why = _api_send(msg)          # works everywhere, cloud included
    if not ok:
        ok, why = _smtp_send(msg, addr, pw)
    if ok:
        return ok, why
    # SMTP blocked here (Render free tier blocks mail ports): queue the
    # message in the cloud DB; the Mac relays it next time it checks in.
    # NEVER queue a message that carries an attachment — the outbox only
    # stores subject/body, so the relay would deliver a 'backup attached'
    # email with NO backup while the log claimed success (Jul 21 night
    # sweep). A loud failure the caller must handle beats a quiet lie.
    if attachment:
        return False, f"SEND FAILED — attachment NOT queued ({why})"
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


DEFAULT_SIGNATURE = ("— {name}\n"
                     "Master Butler, inc\n"
                     "customercare@masterbutlerinc.com")


def _signature(by):
    """The block appended to every office reply. Precedence (Dallon,
    Jul 13): the sender's OWN saved signature (with their title) →
    the shared office signature → the built-in default. {name} always
    fills with whoever's signed in, so a blank never ships."""
    tmpl = DEFAULT_SIGNATURE
    try:
        import clouddb
        if clouddb.available():
            personal = clouddb.get_blob("email_signatures_personal") or {}
            mine = (personal.get(by) or "").strip() if by else ""
            if mine:
                tmpl = mine
            else:
                shared = (clouddb.get_blob("email_signature") or "").strip()
                if shared:
                    tmpl = shared
    except Exception:
        pass
    return tmpl.replace("{name}", by or "").strip()


def _quote_block(orig):
    """Gmail-style quoted original so the SENT copy carries the customer's
    words (Jessica, Jul 20: 'replying to a Squarespace… it doesn't show
    the previous submission — we're losing that in actual gmail'). orig =
    {"from","date","text"}. Renders the familiar 'On <date>, <who> wrote:'
    header + '> ' quoted lines."""
    if not orig or not (orig.get("text") or "").strip():
        return ""
    who = (orig.get("from") or "").strip()
    date = (orig.get("date") or "").strip()
    head = "On " + ", ".join(x for x in (date, who) if x) + " wrote:"
    quoted = "\n".join("> " + ln for ln
                       in (orig["text"].strip().splitlines() or [""]))
    return "\n\n" + head + "\n" + quoted


def send_reply(to_addr, subject, body, by, in_reply_to="", orig=None):
    """OFFICE-DRIVEN customer reply from the dashboard Messages page.
    This is a compose tool for a HUMAN — it requires a named office
    user, exactly one recipient, and never runs from automation.
    Sent via Gmail API (falls back to SMTP on the Mac).

    in_reply_to = the customer's last-inbound Message-ID: sets the
    In-Reply-To/References headers so Gmail THREADS the reply (shows the
    ↩ reply arrow) instead of orphaning it. orig = their original message
    for the quoted tail. Both fixes Jessica's Jul-20 report."""
    to_addr = (to_addr or "").strip()
    if not by:
        return False, "pick your name in the top bar first"
    if not re.match(r"^[\w.+-]+@[\w.-]+\.\w+$", to_addr):
        return False, "invalid recipient address"
    addr, pw = _creds()
    if not (addr and pw):
        return False, "no mail credentials"
    msg = EmailMessage()
    msg["From"] = f"Master Butler <{addr}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    # THREAD IT (Jessica, Jul 20): a bare Message-ID in In-Reply-To +
    # References is what Gmail matches on to draw the reply arrow and
    # nest the message under the customer's thread.
    thread_id = None
    mid = (in_reply_to or "").strip()
    if mid:
        if not mid.startswith("<"):
            mid = "<" + mid.strip("<>") + ">"
        msg["In-Reply-To"] = mid
        msg["References"] = mid
    # RELIABLE THREADING: look up the customer's Gmail thread so the send
    # lands INSIDE it — the header alone orphaned ~40% of replies (Jessica,
    # Jul 20: no reply arrow, customer's message not shown). Falls back to
    # the customer's address when the referenced Message-ID isn't in the
    # mailbox (Squarespace forms arrive via SparkPost).
    try:
        import gmail_api
        thread_id = gmail_api.thread_for_reply(to_addr, mid)
    except Exception:
        thread_id = None
    msg.set_content(body + "\n\n" + _signature(by) + _quote_block(orig))
    ok, why = _api_send(msg, thread_id=thread_id)
    if not ok:
        ok, why = _smtp_send(msg, addr, pw)   # SMTP: header-only threading
    return ok, why


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
