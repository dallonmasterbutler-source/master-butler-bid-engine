"""
MASTER BUTLER — 3D FLYOVER (Google Aerial View API)
(Dallon, Jul 9: "google isn't enough sometimes to get us pictures of
the home" + LaRee's questionnaire wish: "see all sides of a home for
window cleaning and pressure washing bids — the most underbid quotes.")

Google renders a photorealistic orbit VIDEO of a US address — every
side of the house, no scraping, no Zillow/Redfin terms problems.
Free tier: 5,000 lookups/month (we need ~100).

ONE-TIME (Dallon, same two clicks as Speech-to-Text):
  1. Enable: https://console.developers.google.com/apis/api/aerialview.googleapis.com/overview?project=815253616550
  2. Credentials → the Maps key → API restrictions → add "Aerial View API"

lookup(address) -> (state, payload)
  state ∈ ACTIVE (payload = mp4/landing uris) · PROCESSING · NOT_FOUND
          (render auto-requested) · DISABLED (needs the clicks) · ERROR
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
_API = "https://aerialview.googleapis.com/v1/videos"


def _key():
    if (BASE / ".env").exists():
        for line in (BASE / ".env").read_text().splitlines():
            if line.startswith("GOOGLE_MAPS_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("GOOGLE_MAPS_API_KEY", "")


def _get(url):
    return json.load(urllib.request.urlopen(url, timeout=30))


def request_render(address):
    """Ask Google to render a flyover for this address (async, takes
    minutes). Safe to call repeatedly — duplicates just no-op."""
    key = _key()
    if not key or not address:
        return False
    try:
        req = urllib.request.Request(
            _API + ":renderVideo?key=" + key,
            data=json.dumps({"address": address}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception:
        return False


def lookup(address):
    key = _key()
    if not key or not address:
        return "ERROR", None
    url = (_API + ":lookupVideo?key=" + key
           + "&address=" + urllib.parse.quote(address))
    try:
        r = _get(url)
        state = r.get("state")
        if state == "ACTIVE":
            return "ACTIVE", r.get("uris") or {}
        if state == "PROCESSING":
            return "PROCESSING", None
        return "ERROR", None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        if e.code == 404:                 # no video yet — ask for one
            request_render(address)
            return "NOT_FOUND", None
        if e.code == 403:
            return "DISABLED", body[:200]
        return "ERROR", body[:200]
    except Exception:
        return "ERROR", None


def listing_links(address):
    """One-click Zillow/Redfin lookups for the office — links only,
    their photos stay on their site (no scraping, no terms issues)."""
    if not address:
        return []
    q = urllib.parse.quote(address)
    return [
        ("Zillow", f"https://www.zillow.com/homes/{q}_rb/"),
        ("Redfin", "https://www.google.com/search?q="
                   + urllib.parse.quote(f"site:redfin.com {address}")),
    ]
