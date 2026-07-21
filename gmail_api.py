"""
MASTER BUTLER — GMAIL API READ (Jul 16)

Reads the inbox over the Gmail HTTP API instead of IMAP. IMAP's
per-account command/bandwidth limits kept taking the poller down
(Jul 15 + 16); the API has vastly higher quotas and no connection
limits. Read-only: list message ids, fetch raw RFC822, never modifies
the mailbox.

Uses the same OAuth creds as sending (mailer._oauth_env), now that the
token carries gmail.readonly + gmail.send.
"""

import base64
import json
import urllib.parse
import urllib.request

_TOK = {"access": None, "exp": 0}


def _access_token():
    """A cached access token minted from the shared OAuth refresh token."""
    import time
    if _TOK["access"] and time.time() < _TOK["exp"] - 60:
        return _TOK["access"]
    import mailer
    o = mailer._oauth_env()
    if not o:
        raise RuntimeError("no Gmail OAuth creds")
    body = urllib.parse.urlencode({
        "client_id": o["GMAIL_OAUTH_CLIENT_ID"],
        "client_secret": o["GMAIL_OAUTH_CLIENT_SECRET"],
        "refresh_token": o["GMAIL_OAUTH_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}).encode()
    tok = json.load(urllib.request.urlopen(
        "https://oauth2.googleapis.com/token", body, timeout=20))
    _TOK["access"] = tok["access_token"]
    _TOK["exp"] = time.time() + int(tok.get("expires_in", 3600))
    return _TOK["access"]


def can_read():
    """True only if the granted scope includes read access — otherwise
    the caller falls back to IMAP (send-only token can't read)."""
    try:
        at = _access_token()
        info = json.load(urllib.request.urlopen(
            "https://oauth2.googleapis.com/tokeninfo?access_token=" + at,
            timeout=15))
        return "gmail.readonly" in (info.get("scope") or "") \
            or "mail.google.com" in (info.get("scope") or "")
    except Exception:
        return False


def _get(url):
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + _access_token()})
    return json.load(urllib.request.urlopen(req, timeout=30))


def list_ids(query, cap=500):
    """Message ids matching a Gmail search query (e.g. 'newer_than:2d
    in:inbox'), newest first, paginated up to `cap`."""
    ids, page = [], None
    while len(ids) < cap:
        u = ("https://gmail.googleapis.com/gmail/v1/users/me/messages?"
             + urllib.parse.urlencode(
                 {"q": query, "maxResults": 100,
                  **({"pageToken": page} if page else {})}))
        d = _get(u)
        ids += [m["id"] for m in d.get("messages", [])]
        page = d.get("nextPageToken")
        if not page:
            break
    return ids[:cap]


def get_raw(msg_id):
    """The full raw RFC822 bytes of one message (same bytes IMAP's
    BODY.PEEK[] returned — parses identically downstream)."""
    d = _get("https://gmail.googleapis.com/gmail/v1/users/me/messages/"
             + msg_id + "?format=raw")
    return base64.urlsafe_b64decode(d["raw"])


def get_meta(msg_id):
    """Cheap header-only fetch (From + Message-ID + labelIds), no body.
    Used by the archive mirror to see who is still in the inbox and
    whether their mail is unread — a few units per call vs a full
    download (Jul 20: mirror moved off IMAP onto the API)."""
    d = _get("https://gmail.googleapis.com/gmail/v1/users/me/messages/"
             + msg_id + "?format=metadata&metadataHeaders=From"
             "&metadataHeaders=Message-ID")
    hdrs = {h["name"].lower(): h["value"]
            for h in (d.get("payload", {}) or {}).get("headers", [])}
    return {"from": hdrs.get("from", ""),
            "message_id": (hdrs.get("message-id", "") or "").strip(),
            "labels": d.get("labelIds", []) or []}


def inbox_index(cap=600):
    """Everyone currently sitting in the Gmail INBOX, by sender address:
        { sender_email: {"unread": bool} }  plus  set(message_ids)
    'In the inbox' is the office's open-work set; anything NOT here has
    been archived or trashed = handled (Jessica's mirror, Jul 20). None
    on any read failure so the caller changes nothing."""
    import email.utils
    try:
        ids = list_ids("in:inbox", cap=cap)
    except Exception:
        return None, None
    senders, msgids = {}, set()
    for mid in ids:
        try:
            m = get_meta(mid)
        except Exception:
            continue
        _, em = email.utils.parseaddr(m["from"])
        unread = "UNREAD" in m["labels"]
        if em:
            em = em.lower()
            senders[em] = senders.get(em, False) or unread
        if m["message_id"]:
            msgids.add(m["message_id"])
    return senders, msgids
