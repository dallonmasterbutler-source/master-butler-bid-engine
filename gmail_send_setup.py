"""
MASTER BUTLER — GMAIL API SEND: ONE-TIME SETUP (Dallon's go, Jul 14)

Why: Render blocks SMTP, so dashboard replies currently relay through
the Mac (2–10 min lag). The Gmail API is plain HTTPS — the cloud can
send INSTANTLY as customercare@ once this one-time consent exists.

Dallon's five clicks (console.cloud.google.com, project 815253616550 —
the same one with Maps/Speech):
  1. APIs & Services → Library → search "Gmail API" → Enable
  2. APIs & Services → OAuth consent screen → Internal (Workspace) →
     app name "Master Butler Dashboard" → Save
  3. APIs & Services → Credentials → + Create credentials →
     OAuth client ID → Application type: Desktop app → Create
  4. Copy the Client ID and Client secret into .env as
     GMAIL_OAUTH_CLIENT_ID=…  and  GMAIL_OAUTH_CLIENT_SECRET=…
  5. Run:  python3 gmail_send_setup.py
     → it prints a URL; open it SIGNED IN AS customercare@, click
     Allow, paste the code back here. Done — the refresh token lands
     in the shared cloud store and the cloud can send.

Scope is gmail.send ONLY — this token can send as the account but can
never read, delete, or manage mail.
"""

import json
import urllib.parse
import urllib.request

SCOPE = "https://www.googleapis.com/auth/gmail.send"
REDIRECT = "urn:ietf:wg:oauth:2.0:oob"


def _env(k):
    from pathlib import Path
    for line in (Path(__file__).parent / ".env").read_text().splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return None


def main():
    cid = _env("GMAIL_OAUTH_CLIENT_ID")
    sec = _env("GMAIL_OAUTH_CLIENT_SECRET")
    if not (cid and sec):
        print("Add GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET "
              "to .env first (steps 1–4 in this file's docstring).")
        return
    url = ("https://accounts.google.com/o/oauth2/v2/auth?" +
           urllib.parse.urlencode({
               "client_id": cid, "redirect_uri": REDIRECT,
               "response_type": "code", "scope": SCOPE,
               "access_type": "offline", "prompt": "consent"}))
    print("\n1. Open this URL signed in as customercare@"
          "masterbutlerinc.com:\n\n" + url + "\n")
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
               "refresh_token": rt, "scope": SCOPE}
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob("gmail_oauth", payload)
        else:                    # Mac has no direct DB — push over HTTPS
            from cloudpush import push
            push(blobs={"gmail_oauth": payload})
    except Exception as e:
        print("Could not reach the cloud store:", e)
        print("Token kept locally in data/gmail_oauth.json — rerun "
              "later or tell Claude.")
        from pathlib import Path
        (Path(__file__).parent / "data" / "gmail_oauth.json").write_text(
            json.dumps(payload))
        return
    print("\n✅ Saved to the shared store — the cloud can now send as "
          "customercare@ (send-only scope). Next: Dallon flips "
          "REPLIES_ENABLED and the pre-filled reply boxes go live.")


if __name__ == "__main__":
    main()
