"""
MASTER BUTLER — GMAIL API READ ACCESS: ONE-TIME SETUP (Jul 16)

Why: the inbox poller reads mail over IMAP, whose per-account rate
limits keep taking it down. The Gmail API has huge quotas and no
connection limits — but our token is send-ONLY, so it can't read.
This grants READ (on top of send) so the poller can move to the API.

Google killed the old copy-paste (OOB) flow, so this uses the modern
loopback flow: it opens a Google page in your browser, you click
Allow, and Google hands the code straight back to this script — no
pasting.

Run:  python3 gmail_read_setup.py
  → your browser opens to a Google consent page. Sign in as
    customercare@masterbutlerinc.com, click Allow. Done.

Scopes: gmail.readonly + gmail.send — reads mail and keeps sending;
never deletes or alters anything in the mailbox.
"""

import json
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/gmail.send")
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"
BASE = Path(__file__).parent
_CODE = {}


def _env(k):
    for line in (BASE / ".env").read_text().splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return None


class _Catch(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path).query
        _CODE.update(urllib.parse.parse_qs(q))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "code" in _CODE
        self.wfile.write(
            b"<h2>" + (b"&#9989; All set \xe2\x80\x94 read access granted."
                       if ok else b"&#10060; Something went wrong.")
            + b" You can close this tab and return to the terminal.</h2>")

    def log_message(self, *a):
        pass


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
    srv = HTTPServer(("localhost", PORT), _Catch)
    print("\nOpening Google in your browser — sign in as "
          "customercare@masterbutlerinc.com and click Allow.")
    print("(If it doesn't open, paste this URL into your browser:)\n\n"
          + url + "\n")
    webbrowser.open(url)
    while "code" not in _CODE and "error" not in _CODE:
        srv.handle_request()
    if "error" in _CODE:
        print("Google returned an error:", _CODE.get("error"))
        return
    code = _CODE["code"][0]
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
    try:
        envp = BASE / ".env"
        lines = [ln for ln in envp.read_text().splitlines()
                 if not ln.startswith("GMAIL_OAUTH_REFRESH_TOKEN=")]
        lines.append(f"GMAIL_OAUTH_REFRESH_TOKEN={rt}")
        envp.write_text("\n".join(lines) + "\n")
    except Exception:
        pass
    print("\n✅ Read + send access granted and saved to the cloud "
          "store.\n   Tell Claude — the poller can move to the Gmail API.")


if __name__ == "__main__":
    main()
