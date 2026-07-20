"""
RHYTHM — GOOGLE CALENDAR (OAuth 2.0, read free/busy, write events)

Pure standard library (urllib) so there's nothing extra to install and it
deploys on Render like the rest of the shop's code. Two people each connect
their own Google account once; we keep a refresh token per person and mint
short-lived access tokens as needed.

available()  -> are Google app credentials configured at all?
connected(p) -> has person "a"/"b" linked their calendar?

Nothing here talks to a customer or the business — it's the couple's own
calendars only. If credentials aren't set, every call degrades quietly and
the web app shows a "Connect" prompt instead of erroring.
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

import store

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = "https://www.googleapis.com/auth/calendar.events " \
         "https://www.googleapis.com/auth/calendar.readonly"


def _client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def _client_secret():
    return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def available():
    """True once Dallon has set the Google app credentials (see SETUP_GOOGLE.md)."""
    return bool(_client_id() and _client_secret())


def connected(person):
    tokens = store.get()["gcal"].get(person) or {}
    return bool(tokens.get("refresh_token"))


def account_email(person):
    return (store.get()["gcal"].get(person) or {}).get("email", "")


# ── OAuth handshake ───────────────────────────────────────────────────────
def auth_url(person, redirect_uri, state):
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",             # force a refresh token every time
        "state": f"{person}:{state}",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(person, code, redirect_uri):
    """Trade the auth code for tokens and remember them for this person."""
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    tok = _post(TOKEN_URL, data)
    email = _whoami(tok.get("access_token", ""))

    def _save(s):
        cur = s["gcal"].get(person, {})
        cur.update({
            "refresh_token": tok.get("refresh_token", cur.get("refresh_token")),
            "access_token": tok.get("access_token"),
            "expires_at": _expiry(tok.get("expires_in", 3600)),
            "email": email or cur.get("email", ""),
        })
        s["gcal"][person] = cur
    store.update(_save)
    return True


def disconnect(person):
    store.update(lambda s: s["gcal"].pop(person, None))


def _access_token(person):
    tokens = store.get()["gcal"].get(person) or {}
    if not tokens.get("refresh_token"):
        return None
    if tokens.get("access_token") and tokens.get("expires_at", 0) > _now() + 60:
        return tokens["access_token"]
    # refresh
    data = urllib.parse.urlencode({
        "refresh_token": tokens["refresh_token"],
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "grant_type": "refresh_token",
    }).encode()
    tok = _post(TOKEN_URL, data)
    access = tok.get("access_token")
    if not access:
        return None

    def _save(s):
        cur = s["gcal"].get(person, {})
        cur["access_token"] = access
        cur["expires_at"] = _expiry(tok.get("expires_in", 3600))
        s["gcal"][person] = cur
    store.update(_save)
    return access


# ── Calendar reads / writes ───────────────────────────────────────────────
def busy(person, start_iso, end_iso):
    """Return [(start_iso, end_iso), ...] busy blocks, or [] if not connected."""
    access = _access_token(person)
    if not access:
        return []
    body = json.dumps({
        "timeMin": _rfc3339(start_iso),
        "timeMax": _rfc3339(end_iso),
        "items": [{"id": "primary"}],
    }).encode()
    try:
        resp = _post(f"{API}/freeBusy", body, bearer=access, json_body=True)
    except Exception:
        return []
    cals = resp.get("calendars", {})
    prim = next(iter(cals.values()), {}) if cals else {}
    out = []
    for b in prim.get("busy", []):
        out.append((b["start"], b["end"]))
    return out


def add_event(person, title, start_iso, end_iso, description=""):
    """
    Create an event on this person's primary calendar. Returns the event id,
    or None if not connected. This only ever writes to the couple's OWN
    calendar — never anything customer- or business-facing.
    """
    access = _access_token(person)
    if not access:
        return None
    body = json.dumps({
        "summary": title,
        "description": description,
        "start": {"dateTime": _rfc3339(start_iso)},
        "end": {"dateTime": _rfc3339(end_iso)},
    }).encode()
    try:
        resp = _post(f"{API}/calendars/primary/events", body,
                     bearer=access, json_body=True)
    except Exception:
        return None
    return resp.get("id")


# ── low-level helpers ─────────────────────────────────────────────────────
def _whoami(access):
    if not access:
        return ""
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("email", "")
    except Exception:
        return ""


def _post(url, data, bearer=None, json_body=False):
    headers = {}
    if json_body:
        headers["Content-Type"] = "application/json"
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _now():
    return int(datetime.now().timestamp())


def _expiry(seconds):
    return _now() + int(seconds)


def _rfc3339(iso):
    """Accept a bare 'YYYY-MM-DDTHH:MM:SS' and hand Google an RFC3339 string."""
    if iso.endswith("Z") or "+" in iso[10:]:
        return iso
    # treat as local; Google is fine with a floating local time + no tz here
    return iso
