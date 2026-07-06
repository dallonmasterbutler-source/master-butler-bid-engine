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
Exact field names are verified by introspecting the live schema the
first time we connect with real credentials — anything uncertain is
marked  # VERIFY  below.
"""

import json
import os
import urllib.request
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
JOBBER_GRAPHQL_VERSION = "2023-11-15"          # VERIFY current version at connect time
CLIENT_ID = _env("JOBBER_CLIENT_ID")
CLIENT_SECRET = _env("JOBBER_CLIENT_SECRET")
ACCESS_TOKEN = _env("JOBBER_ACCESS_TOKEN")     # filled after the one-time "Allow" handshake

# Default to the safe settings. Flip these only once live testing is verified.
DRY_RUN = _env("JOBBER_DRY_RUN", "true").lower() != "false"
TEST_MODE = _env("JOBBER_TEST_MODE", "true").lower() != "false"


# ─────────────────────────────────────────────────────────────
# THE GRAPHQL CALLS WE NEED (all reads/writes go through here)
# ─────────────────────────────────────────────────────────────

def _post(query, variables, label):
    """Send one GraphQL request — or, in dry-run, just show it."""
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
        return {"error": e.code, "body": e.read().decode()[:500]}
    if resp.get("errors"):
        return {"error": "graphql", "body": resp["errors"]}
    return resp.get("data", {})


# ── Find a client by email (so we don't create duplicates) ──
FIND_CLIENT = """
query FindClient($email: String!) {
  clients(filter: { email: $email }, first: 1) {   # VERIFY filter arg name
    nodes { id firstName lastName emails { address } }
  }
}
"""

def find_client(email):
    data = _post(FIND_CLIENT, {"email": email}, "find client by email")
    if data.get("dry_run"):
        return None
    nodes = data.get("clients", {}).get("nodes", [])
    return nodes[0]["id"] if nodes else None


# ── Create a client ──
CREATE_CLIENT = """
mutation CreateClient($input: ClientCreateInput!) {   # VERIFY input type
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
CREATE_PROPERTY = """
mutation CreateProperty($input: PropertyCreateInput!) {   # VERIFY
  propertyCreate(input: $input) {
    property { id }
    userErrors { message }
  }
}
"""

def create_property(client_id, address):
    variables = {"input": {"clientId": client_id, "address": {"street": address}}}
    data = _post(CREATE_PROPERTY, variables, "create property")
    if data.get("dry_run"):
        return "DRY_RUN_PROPERTY_ID"
    return data.get("propertyCreate", {}).get("property", {}).get("id")


# ── Create a DRAFT quote from our bid line items ──
CREATE_QUOTE = """
mutation CreateQuote($input: QuoteCreateInput!) {   # VERIFY
  quoteCreate(input: $input) {
    quote { id quoteNumber }
    userErrors { message }
  }
}
"""

def create_draft_quote(client_id, property_id, bid):
    """bid = the dict from calculate_bid: has line items + office notes.

    We attach our office notes and confidence as a message on the quote so
    the reviewer sees exactly why the engine priced it this way.
    """
    line_items = [{
        "name": s["name"],
        "quantity": 1,
        "unitPrice": float(s["price"]),
    } for s in bid["services"]]

    note_lines = [f"Auto-generated draft — confidence {bid['confidence']}%."]
    note_lines += [f"⚠ {n}" for n in bid["notes"]]

    variables = {"input": {
        "clientId": client_id,
        "propertyId": property_id,
        "lineItems": line_items,
        "message": "\n".join(note_lines),   # VERIFY field name for internal note
        # NOTE: no "send" / "deliver" field is set anywhere. Draft only.
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
