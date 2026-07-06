"""
MASTER BUTLER — JOBBER ONE-TIME "ALLOW" HANDSHAKE

Run this once:  python3 jobber_auth.py

What it does, in plain English:
  1. Starts a tiny local web server (the "catcher's mitt")
  2. Opens your browser to Jobber's permission screen
  3. You log in as the Master Butler account and click ALLOW
  4. Jobber tosses a one-time code back to the catcher's mitt
  5. This script trades that code for the real access token and
     saves it into .env automatically
  6. Then it runs one harmless live test (asks Jobber "what account
     am I?") to prove the connection works

Requires: in developer.getjobber.com → your app → settings, the
OAuth Callback URL must be exactly:   http://localhost:8085/callback
"""

import json
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ENV = Path(__file__).parent / ".env"
REDIRECT_URI = "http://localhost:8085/callback"
AUTH_URL = "https://api.getjobber.com/api/oauth/authorize"
TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
API_URL = "https://api.getjobber.com/api/graphql"


def env(name):
    for line in ENV.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""


def save_env(updates: dict):
    """Rewrite .env with new values for the given keys (everything else kept)."""
    lines = ENV.read_text().splitlines()
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    ENV.write_text("\n".join(out) + "\n")


captured = {}


class Catcher(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        if "code" in params:
            captured["code"] = params["code"][0]
            body = ("<h2>✅ Got it — you can close this tab.</h2>"
                    "<p>Return to the Terminal window.</p>")
        else:
            body = f"<h2>Hmm, no code received.</h2><p>{qs}</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):  # keep terminal quiet
        pass


def main():
    client_id = env("JOBBER_CLIENT_ID")
    client_secret = env("JOBBER_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("❌ Client ID/Secret missing from .env — add those first.")
        return

    server = HTTPServer(("localhost", 8085), Catcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": "masterbutler",
    })
    print("Opening your browser to Jobber's permission screen...")
    print("Log in as the MASTER BUTLER account and click ALLOW.\n")
    webbrowser.open(url)

    print("Waiting for Jobber to send the code back", end="", flush=True)
    import time
    for _ in range(300):  # wait up to 5 minutes
        if "code" in captured:
            break
        time.sleep(1)
        print(".", end="", flush=True)
    server.shutdown()

    if "code" not in captured:
        print("\n❌ Timed out waiting. Common cause: the app's Callback URL in "
              "developer.getjobber.com isn't exactly " + REDIRECT_URI)
        return

    print("\n✅ Code received. Trading it for the access token...")
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": captured["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        tokens = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        print("❌ Token exchange failed:", e.code, e.read().decode()[:300])
        return

    access = tokens.get("access_token", "")
    refresh = tokens.get("refresh_token", "")
    if not access:
        print("❌ No access token in response:", tokens)
        return

    save_env({"JOBBER_ACCESS_TOKEN": access,
              "JOBBER_REFRESH_TOKEN": refresh})
    print("✅ Tokens saved to .env (never committed to GitHub).")

    # ── One harmless live test: "who am I?" ──
    q = {"query": "query { account { name } }"}
    req = urllib.request.Request(API_URL, data=json.dumps(q).encode(), headers={
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        "X-JOBBER-GRAPHQL-VERSION": "2023-11-15",
    })
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=30))
        name = resp.get("data", {}).get("account", {}).get("name")
        if name:
            print(f"\n🎉 LIVE CONNECTION CONFIRMED — connected to Jobber account: {name}")
        else:
            print("\n⚠ Connected, but unexpected reply:", str(resp)[:300])
    except urllib.error.HTTPError as e:
        print("\n⚠ Token saved but test call failed:", e.code, e.read().decode()[:300])


if __name__ == "__main__":
    main()
