"""
MASTER BUTLER — GMAIL API READ ACCESS: ONE-TIME SETUP (Jul 16)

Why: the inbox poller reads mail over IMAP, whose per-account rate
limits keep taking the poller down (Jul 15 + Jul 16 outages). The
Gmail API has vastly higher quotas and no connection limits — but our
current token is send-ONLY, so it can't read. This grants READ access
on top of send, so the poller can move to the sturdy API.

Same 2-minute flow you did for sending:
  Run:  python3 gmail_read_setup.py
  → it prints a URL. Open it SIGNED IN AS customercare@masterbutlerinc.com,
    click Allow, paste the code back here. Done.

Scopes: gmail.readonly + gmail.send — reads mail and keeps sending;
it can never delete or alter anything in the mailbox.
"""

import json
import urllib.parse
import urllib.request
from pathlib import Path

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/gmail.send")
REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
BASE = Path(__file__).parent


def _env(k):
    for line in (BASE / ".env").read_text().splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return None


def main():
    cid = _env("GMAIL_OAUTH_CLIENT_ID")
    sec = _env("GMAIL_OAUTH_CLIENT_SECRET")
    if not (cid and sec):
        print("GMAIL_OAUTH_CLIENT_ID / _SECRET missing from .env.")
        return
    url = ("https://accounts.google.com/o/oauth2/v2/auth?" +
           urllib.parse.urlencode({
               "client_id": cid, "redirect_uri": REDIRECT,
               "response_type": "code", "scope": SCOPES,
               "access_type": "offline", "prompt": "consent"}))
    print("\n1. Open this URL signed in as customercare@masterbutlerinc.com"
          ":\n\n" + url + "\n")
    code = input("2. Paste the code Google shows you: ").strip()
    body = urllib.parse.urlencode({
        "code": code, "client_id": cid, "client_secret": sec,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code"
    }).encode()
    resp = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}),
        timeout=30))
    rt = resp.get("refresh_token")
    if not rt:
        print("No refresh token in response:", str(resp)[:300])
        return
    payload = {"client_id": cid, "client_secret": sec,
               "refresh_token": rt, "scope": SCOPES}
    saved = False
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob("gmail_oauth", payload)
            saved = True
    except Exception:
        pass
    if not saved:
        try:
            from cloudpush import push
            push(blobs={"gmail_oauth": payload})
            saved = True
        except Exception as e:
            print("Could not reach the cloud store:", e)
    (BASE / "data" / "gmail_oauth.json").write_text(json.dumps(payload))
    # also drop the fresh refresh token into .env for local scripts
    try:
        envp = BASE / ".env"
        lines = envp.read_text().splitlines()
        lines = [ln for ln in lines
                 if not ln.startswith("GMAIL_OAUTH_REFRESH_TOKEN=")]
        lines.append(f"GMAIL_OAUTH_REFRESH_TOKEN={rt}")
        envp.write_text("\n".join(lines) + "\n")
    except Exception:
        pass
    print("\n✅ Read + send access granted and saved to the cloud store."
          "\n   Tell Claude — the poller can now move to the Gmail API.")


if __name__ == "__main__":
    main()
