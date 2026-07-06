"""
MASTER BUTLER — JOBBER CLIENT

Turns an APPROVED bid into a DRAFT quote inside Jobber.

Hard safety rules baked in (not optional):
  * Everything created here is a DRAFT. This code has no path that
    sends a quote to a customer — that's a human click inside Jobber.
  * DRY_RUN mode (default until real credentials exist) prints exactly
    what WOULD be sent to Jobber, so we can see the payloads with zero
    risk before touching the live account.
  * When TEST_MODE is on, customer names are prefixed "TEST - " so any
    practice records are obvious and easy to delete.

Jobber uses a GraphQL API. Auth is OAuth (Client ID + Secret → token).
All field names were verified against the LIVE schema on July 5, 2026
(quoteCreate attributes, property nesting, searchTerm client lookup).
Access tokens auto-refresh — no human re-authorization needed.
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path


# ─────────────────────────────────────────────────────────────
# CONFIG — read from .env (never hard-coded, never committed)
# ─────────────────────────────────────────────────────────────

def _env(name, default=None):
    path = Path(__file__).parent / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, default)


JOBBER_API_URL = "https://api.getjobber.com/api/graphql"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
JOBBER_GRAPHQL_VERSION = "2023-11-15"          # verified against live schema Jul 2026
CLIENT_ID = _env("JOBBER_CLIENT_ID")
CLIENT_SECRET = _env("JOBBER_CLIENT_SECRET")
ACCESS_TOKEN = _env("JOBBER_ACCESS_TOKEN")     # short-lived (~60 min)
REFRESH_TOKEN = _env("JOBBER_REFRESH_TOKEN")   # long-lived; used to mint new access tokens


def refresh_access_token():
    """Jobber access tokens die after ~an hour. This trades our long-lived
    refresh token for a fresh access token and saves it back to .env,
    so the system never needs a human to re-do the Allow handshake."""
    global ACCESS_TOKEN, REFRESH_TOKEN
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(JOBBER_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    tokens = json.load(urllib.request.urlopen(req, timeout=30))
    ACCESS_TOKEN = tokens["access_token"]
    # Jobber rotates refresh tokens: save BOTH back to .env
    new_refresh = tokens.get("refresh_token", REFRESH_TOKEN)
    REFRESH_TOKEN = new_refresh
    env_path = Path(__file__).parent / ".env"
    lines = env_path.read_text().splitlines()
    out = []
    for line in lines:
        if line.startswith("JOBBER_ACCESS_TOKEN="):
            out.append(f"JOBBER_ACCESS_TOKEN={ACCESS_TOKEN}")
        elif line.startswith("JOBBER_REFRESH_TOKEN="):
            out.append(f"JOBBER_REFRESH_TOKEN={new_refresh}")
        else:
            out.append(line)
    env_path.write_text("\n".join(out) + "\n")
    return ACCESS_TOKEN

# Default to the safe settings. Flip these only once live testing is verified.
DRY_RUN = _env("JOBBER_DRY_RUN", "true").lower() != "false"
TEST_MODE = _env("JOBBER_TEST_MODE", "true").lower() != "false"


# ─────────────────────────────────────────────────────────────
# THE GRAPHQL CALLS WE NEED (all reads/writes go through here)
# ─────────────────────────────────────────────────────────────

def _post(query, variables, label, _retried=False):
    """Send one GraphQL request — or, in dry-run, just show it.
    If the access token has expired (401), auto-refresh once and retry."""
    payload = {"query": query, "variables": variables}
    if DRY_RUN or not ACCESS_TOKEN:
        print(f"\n[DRY RUN] Would send to Jobber → {label}")
        print(json.dumps(variables, indent=2))
        return {"dry_run": True, "label": label, "variables": variables}

    req = urllib.request.Request(
        JOBBER_API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "X-JOBBER-GRAPHQL-VERSION": JOBBER_GRAPHQL_VERSION,
        },
    )
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        if e.code == 401 and not _retried and REFRESH_TOKEN:
            refresh_access_token()          # token died — mint a new one
            return _post(query, variables, label, _retried=True)
        return {"error": e.code, "body": e.read().decode()[:500]}
    if resp.get("errors"):
        return {"error": "graphql", "body": resp["errors"]}
    return resp.get("data", {})


# ── Find a client by email (so we don't create duplicates) ──
# Live schema note: there is NO email filter — we use searchTerm, then
# confirm the exact email match ourselves before trusting it.
FIND_CLIENT = """
query FindClient($term: String!) {
  clients(searchTerm: $term, first: 5) {
    nodes { id name emails { address } }
  }
}
"""

def find_client(email):
    data = _post(FIND_CLIENT, {"term": email}, "find client by email")
    if data.get("dry_run") or data.get("error"):
        return None
    for node in data.get("clients", {}).get("nodes", []):
        addrs = [e["address"].lower() for e in node.get("emails", [])]
        if email.lower() in addrs:          # exact match only
            return node["id"]
    return None


# ── Create a client ──  (input fields verified against live schema)
CREATE_CLIENT = """
mutation CreateClient($input: ClientCreateInput!) {
  clientCreate(input: $input) {
    client { id }
    userErrors { message }
  }
}
"""

def create_client(name, email, phone=None):
    first, _, last = (name or "").partition(" ")
    if TEST_MODE:
        first = "TEST - " + first          # obvious, easy-to-delete practice record
    variables = {"input": {
        "firstName": first or "Customer",
        "lastName": last or "",
        "emails": [{"address": email, "primary": True}] if email else [],
        "phones": [{"number": phone, "primary": True}] if phone else [],
    }}
    data = _post(CREATE_CLIENT, variables, "create client")
    if data.get("dry_run"):
        return "DRY_RUN_CLIENT_ID"
    return data.get("clientCreate", {}).get("client", {}).get("id")


def find_or_create_client(name, email, phone=None):
    existing = find_client(email) if email else None
    return existing or create_client(name, email, phone)


# ── Create a property under a client ──
# Live schema: propertyCreate(clientId:, input: { properties: [...] })
# and each property carries a structured address (street1/city/province/…).
CREATE_PROPERTY = """
mutation CreateProperty($clientId: EncodedId!, $input: PropertyCreateInput!) {
  propertyCreate(clientId: $clientId, input: $input) {
    properties { id }
    userErrors { message }
  }
}
"""

def split_address(address):
    """'20613 NE 34th Pl, Sammamish, WA 98074' -> structured pieces."""
    parts = [p.strip() for p in (address or "").split(",")]
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 1 else ""
    state_zip = parts[2].split() if len(parts) > 2 else []
    province = state_zip[0] if state_zip else "WA"
    postal = state_zip[1] if len(state_zip) > 1 else ""
    return {"street1": street, "city": city, "province": province,
            "postalCode": postal, "country": "US"}

def create_property(client_id, address):
    variables = {"clientId": client_id,
                 "input": {"properties": [{"address": split_address(address)}]}}
    data = _post(CREATE_PROPERTY, variables, "create property")
    if data.get("dry_run"):
        return "DRY_RUN_PROPERTY_ID"
    props = data.get("propertyCreate", {}).get("properties") or []
    return props[0]["id"] if props else None


# ── Create a DRAFT quote from our bid line items ──
# Live schema: quoteCreate takes `attributes` (QuoteCreateAttributes).
# We deliberately DO NOT set `transitionQuoteTo` — leaving it unset is
# what keeps the quote a DRAFT that only a human can send.
CREATE_QUOTE = """
mutation CreateQuote($attributes: QuoteCreateAttributes!) {
  quoteCreate(attributes: $attributes) {
    quote { id quoteNumber }
    userErrors { message }
  }
}
"""

def create_draft_quote(client_id, property_id, bid):
    """bid = the dict from calculate_bid: has line items + office notes.

    We attach our office notes and confidence as the quote message so
    the reviewer sees exactly why the engine priced it this way.
    """
    line_items = [{
        "name": s["name"],
        "quantity": 1,
        "unitPrice": float(s["price"]),
        "saveToProductsAndServices": False,   # don't pollute their catalog
    } for s in bid["services"]]

    note_lines = [f"Auto-generated draft — confidence {bid['confidence']}%."]
    note_lines += [f"⚠ {n}" for n in bid["notes"]]

    variables = {"attributes": {
        "clientId": client_id,
        "propertyId": property_id,
        "title": "TEST - Bid Engine draft" if TEST_MODE else "Service Quote",
        "lineItems": line_items,
        "message": "\n".join(note_lines),
        # NOTE: no transitionQuoteTo, no send/deliver anywhere. Draft only.
    }}
    return _post(CREATE_QUOTE, variables, "create DRAFT quote")


# ─────────────────────────────────────────────────────────────
# THE ONE FUNCTION THE PIPELINE CALLS
# ─────────────────────────────────────────────────────────────

def push_approved_bid(customer, bid):
    """customer = {name, email, phone, address}; bid = calculate_bid output.
    Creates client (if new) + property + DRAFT quote. Never sends."""
    client_id = find_or_create_client(
        customer.get("name"), customer.get("email"), customer.get("phone"))
    property_id = create_property(client_id, customer.get("address", ""))
    return create_draft_quote(client_id, property_id, bid)


# ─────────────────────────────────────────────────────────────
# DEMO (safe): shows the full dry-run for a sample approved bid
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("JOBBER CLIENT — DRY RUN")
    print(f"  Live credentials present: {bool(ACCESS_TOKEN)}")
    print(f"  DRY_RUN: {DRY_RUN}   TEST_MODE: {TEST_MODE}")
    print("=" * 60)

    sample_customer = {
        "name": "Jane Homeowner",
        "email": "jane@example.com",
        "phone": "425-555-0100",
        "address": "20613 NE 34th Pl, Sammamish, WA 98074",
    }
    sample_bid = {
        "services": [
            {"name": "Gutter Cleaning", "price": 345},
            {"name": "Moss Treatment", "price": 60},
        ],
        "notes": ["Shake roof: dry day only — verify with Tom.",
                  "Moss product billed separately."],
        "confidence": 85,
    }

    result = push_approved_bid(sample_customer, sample_bid)
    print("\n" + "=" * 60)
    print("Nothing was sent. This is exactly what WOULD go to Jobber")
    print("once real credentials are added and DRY_RUN is turned off.")
