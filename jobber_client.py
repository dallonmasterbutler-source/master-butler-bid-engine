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


# ── SHARED TOKEN STORE ───────────────────────────────────────
# Jobber ROTATES refresh tokens on every use, so two machines with
# separate copies eventually kill each other's. One home: the cloud
# blob 'jobber_tokens'. Whoever refreshes, saves there; whoever gets a
# stale-token error, RELOADS from there before giving up.

def _blob_tokens():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob("jobber_tokens") or {}
        import urllib.request as _ur
        from base64 import b64encode
        url, pw = _env("DASHBOARD_URL"), _env("DASHBOARD_PASSWORD")
        if url and pw:
            req = _ur.Request(url.rstrip("/") + "/api/blob/jobber_tokens",
                              headers={"Authorization": "Basic " + b64encode(
                                  f"office:{pw}".encode()).decode()})
            return json.load(_ur.urlopen(req, timeout=20)) or {}
    except Exception:
        pass
    return {}


def _save_tokens(access, refresh):
    from datetime import datetime
    val = {"access": access, "refresh": refresh,
           "at": datetime.now().isoformat(timespec="seconds")}
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob("jobber_tokens", val)
        else:
            from cloudpush import push
            push(blobs={"jobber_tokens": val})
    except Exception:
        pass
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():                      # Mac keeps a local copy too
        lines, out = env_path.read_text().splitlines(), []
        for line in lines:
            if line.startswith("JOBBER_ACCESS_TOKEN="):
                out.append(f"JOBBER_ACCESS_TOKEN={access}")
            elif line.startswith("JOBBER_REFRESH_TOKEN="):
                out.append(f"JOBBER_REFRESH_TOKEN={refresh}")
            else:
                out.append(line)
        env_path.write_text("\n".join(out) + "\n")


_bt = _blob_tokens()
ACCESS_TOKEN = _bt.get("access") or _env("JOBBER_ACCESS_TOKEN")
REFRESH_TOKEN = _bt.get("refresh") or _env("JOBBER_REFRESH_TOKEN")


def _mint(refresh_token):
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(JOBBER_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def refresh_access_token():
    """Fresh access token, rotation-safe: reload the shared store first
    (the other machine may have already rotated), then mint, then save
    the new pair back to the store."""
    global ACCESS_TOKEN, REFRESH_TOKEN
    stored = _blob_tokens()
    if stored.get("access") and stored["access"] != ACCESS_TOKEN:
        ACCESS_TOKEN = stored["access"]        # someone beat us to it
        REFRESH_TOKEN = stored.get("refresh") or REFRESH_TOKEN
        return ACCESS_TOKEN
    try:
        tokens = _mint(REFRESH_TOKEN)
    except urllib.error.HTTPError:
        stored = _blob_tokens()                # our refresh was stale —
        if stored.get("refresh"):              # take the store's and retry
            tokens = _mint(stored["refresh"])
        else:
            raise
    ACCESS_TOKEN = tokens["access_token"]
    REFRESH_TOKEN = tokens.get("refresh_token", REFRESH_TOKEN)
    _save_tokens(ACCESS_TOKEN, REFRESH_TOKEN)
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
    global ACCESS_TOKEN, REFRESH_TOKEN
    payload = {"query": query, "variables": variables}
    if not ACCESS_TOKEN:                 # self-heal: the store may have
        bt = _blob_tokens()              # tokens we missed at import
        if bt.get("access"):
            ACCESS_TOKEN = bt["access"]
            REFRESH_TOKEN = bt.get("refresh") or REFRESH_TOKEN
            print("  (jobber tokens loaded from shared store)")
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


# ── Look up a known client's property address (read-only) ──
# Repeat customers rarely repeat their address — but Jobber knows it.
FIND_CLIENT_PROPERTY = """
query FindProp($term: String!) {
  clients(searchTerm: $term, first: 5) {
    nodes { id emails { address }
            properties { address { street city province postalCode } } }
  }
}
"""


def find_client_address(email_addr):
    """Exact-email client match -> their property address string, or None.
    READ-ONLY: runs live even in dry-run mode (dry-run guards writes)."""
    global DRY_RUN
    if not email_addr:
        return None
    was = DRY_RUN
    DRY_RUN = False
    try:
        data = _post(FIND_CLIENT_PROPERTY, {"term": email_addr},
                     "find client property")
    finally:
        DRY_RUN = was
    if data.get("dry_run") or data.get("error"):
        return None
    for node in data.get("clients", {}).get("nodes", []):
        addrs = [e["address"].lower() for e in node.get("emails", [])]
        if email_addr.lower() not in addrs:
            continue
        for p in node.get("properties", []):
            a = p.get("address") or {}
            if a.get("street"):
                return (f"{a['street']}, {a.get('city', '')}, "
                        f"{a.get('province', '')} {a.get('postalCode', '')}"
                        ).strip(", ")
    return None


# ── Software caller-ID (read-only) ──
# A voicemail arrives with just a number; Jobber usually knows the face.
FIND_CLIENT_BY_PHONE = """
query FindByPhone($term: String!) {
  clients(searchTerm: $term, first: 3) {
    nodes { name
            invoices(first: 1) { totalCount }
            quotes(first: 1) { totalCount }
            properties { address { street city } } }
  }
}
"""


def caller_id(phone):
    """Digits in -> {'name','invoices','quotes','address'} or None.
    READ-ONLY: runs live even in dry-run mode (dry-run guards writes)."""
    global DRY_RUN
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    if len(digits) != 10:
        return None
    pretty = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    was = DRY_RUN
    DRY_RUN = False
    try:
        for term in (pretty, digits):
            data = _post(FIND_CLIENT_BY_PHONE, {"term": term}, "caller id")
            if data.get("dry_run") or data.get("error"):
                return None
            nodes = data.get("clients", {}).get("nodes", [])
            if nodes:
                n = nodes[0]
                addr = ""
                for p in n.get("properties", []):
                    a = p.get("address") or {}
                    if a.get("street"):
                        addr = f"{a['street']}, {a.get('city', '')}".strip(", ")
                        break
                return {"name": n["name"],
                        "invoices": n["invoices"]["totalCount"],
                        "quotes": n["quotes"]["totalCount"],
                        "address": addr}
    finally:
        DRY_RUN = was
    return None


# ── New-or-returning check (read-only) ──
# Techs asked for a one-time "new customer" note on first jobs. This is
# the fact that powers it: invoice count 0 = their first job, exactly once.
CLIENT_SUMMARY = """
query Summary($term: String!) {
  clients(searchTerm: $term, first: 5) {
    nodes { emails { address }
            invoices(first: 1) { totalCount } }
  }
}
"""


def client_summary(email_addr):
    """Exact-email match -> {'known': bool, 'invoices': N} or None on
    lookup failure. READ-ONLY: runs live even in dry-run mode."""
    global DRY_RUN
    if not email_addr:
        return None
    was = DRY_RUN
    DRY_RUN = False
    try:
        data = _post(CLIENT_SUMMARY, {"term": email_addr}, "client summary")
    finally:
        DRY_RUN = was
    if data.get("dry_run") or data.get("error"):
        return None
    for node in data.get("clients", {}).get("nodes", []):
        addrs = [e["address"].lower() for e in node.get("emails", [])]
        if email_addr.lower() in addrs:
            return {"known": True,
                    "invoices": node["invoices"]["totalCount"]}
    return {"known": False, "invoices": 0}


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




# ─────────────────────────────────────────────────────────────
# CUSTOM FIELDS — the office's own data model, auto-filled
# IDs are the QUOTE-level configurations from the live account.
# Dropdown values must match the office's option strings EXACTLY —
# including their trailing/double spaces. Do not "clean" them.
# ─────────────────────────────────────────────────────────────

CF_QUOTE = {
    "must_know":     "Z2lkOi8vSm9iYmVyL0N1c3RvbUZpZWxkQ29uZmlndXJhdGlvblRleHQvMjc5MjQ0",
    "sqft":          "Z2lkOi8vSm9iYmVyL0N1c3RvbUZpZWxkQ29uZmlndXJhdGlvblRleHQvMjkzMzAz",
    "gutter_safety": "Z2lkOi8vSm9iYmVyL0N1c3RvbUZpZWxkQ29uZmlndXJhdGlvbkRyb3Bkb3duLzI3OTIyMg==",
    "roof_safety":   "Z2lkOi8vSm9iYmVyL0N1c3RvbUZpZWxkQ29uZmlndXJhdGlvbkRyb3Bkb3duLzI3OTIyNg==",
}

MUST_UPDATE = "Must be updated "     # the office's own "unknown" convention


def safety_options(pitch, roof_material, stories):
    """Map measured pitch/roof/stories onto the office's safety dropdowns.

    CONSERVATIVE BY DESIGN: anything ambiguous gets "Must be updated " —
    the office's own convention for "a human decides." The system only
    answers when the answer is obvious.
    Returns (gutter_safety_value, roof_safety_value).
    """
    specialty = roof_material in ("shake", "metal_full", "metal_mixed")

    if stories == "3" or stories == "3_exp_tech":
        return MUST_UPDATE, MUST_UPDATE          # tech assignment = office call
    if pitch == "tom_only":
        if specialty:
            return "Tom, dry day, safety ", "Tom, dry day, safety "
        return "Only Tom can service  gutters ", "Tom can service roof"
    if pitch == "steep":
        if specialty:
            return MUST_UPDATE, MUST_UPDATE      # steep + specialty = human call
        return "Experienced Technician", "Experienced Technician"
    if pitch in ("mild", "moderate"):
        if specialty:
            return "Experienced Technician, Dry Day", "Experienced Technician, Dry Day"
        return "Employee can service gutters", "Employee can service roof "
    return MUST_UPDATE, MUST_UPDATE


def build_custom_fields(prop_info):
    """prop_info: {sqft, sqft_source, pitch, roof_material, stories, must_know}."""
    if not prop_info:
        return []
    fields = []
    if prop_info.get("sqft"):
        note = f"{prop_info['sqft']}"
        if prop_info.get("sqft_source"):
            note += f" ({prop_info['sqft_source']})"
        fields.append({"customFieldConfigurationId": CF_QUOTE["sqft"],
                       "valueText": note})
    g, r = safety_options(prop_info.get("pitch"), prop_info.get("roof_material"),
                          prop_info.get("stories"))
    fields.append({"customFieldConfigurationId": CF_QUOTE["gutter_safety"],
                   "valueDropdown": g})
    fields.append({"customFieldConfigurationId": CF_QUOTE["roof_safety"],
                   "valueDropdown": r})
    if prop_info.get("must_know"):
        fields.append({"customFieldConfigurationId": CF_QUOTE["must_know"],
                       "valueText": prop_info["must_know"][:500]})
    return fields


# ─────────────────────────────────────────────────────────────
# TAX RATES — auto-attach the office's own city rate to every quote
# (2 of 3 office questionnaires flagged missing tax as a real recurring
#  miss). The office maintains ~51 rates in Jobber named by city, e.g.
#  "Monroe 3112 (9.4%)". We match the quote address's CITY against that
#  list. FLAG-DON'T-GUESS: only attach when the match is unambiguous;
#  cities with several valid rates (RTA/non-RTA, city vs county) get an
#  internal note listing the candidates for the office to pick.
# ─────────────────────────────────────────────────────────────

TAX_RATES_QUERY = """
query { taxRates(first: 100) { nodes { id name label tax default } } }
"""
TAX_CACHE = Path(__file__).parent / "data" / "tax_rates.json"

# Known Jobber-side spellings that differ from the mailing address.
TAX_CITY_ALIASES = {
    "mountlake terrace": "mountlake terrce",   # office's spelling in Jobber
}

# ZIP splits we can resolve safely (city spans two counties).
ZIP_TAX_HINTS = {
    "98011": "Bothell King Co",
    "98012": "Bothell Sno Co",
    "98021": "Bothell Sno Co",
}


def fetch_tax_rates(refresh=False):
    """Return the account's tax-rate list, cached locally so quote
    creation doesn't spend an API call every time."""
    if not refresh and TAX_CACHE.exists():
        return json.loads(TAX_CACHE.read_text())
    data = _post(TAX_RATES_QUERY, {}, "list tax rates")
    if data.get("dry_run") or data.get("error"):
        return json.loads(TAX_CACHE.read_text()) if TAX_CACHE.exists() else []
    rates = data.get("taxRates", {}).get("nodes", [])
    TAX_CACHE.parent.mkdir(exist_ok=True)
    TAX_CACHE.write_text(json.dumps(rates, indent=1))
    return rates


def wa_dor_location_code(street, city, zip_code):
    """Ask Washington State's own tax API for the address's location
    code + rate. Free, official, auto-updates when the state changes
    rates (Tom's question). Returns (code, rate) or (None, None)."""
    import urllib.parse
    try:
        q = urllib.parse.urlencode({"output": "text", "addr": street,
                                    "city": city or "", "zip": zip_code or ""})
        resp = urllib.request.urlopen(
            f"https://webgis.dor.wa.gov/webapi/AddressRates.aspx?{q}",
            timeout=15).read().decode()
        parts = dict(p.split("=", 1) for p in resp.split() if "=" in p)
        code = parts.get("LocationCode")
        rate = float(parts.get("Rate", 0) or 0)
        # ResultCode 0-5 = address/zip-level match; 9 = not found
        if code and code != "-1" and parts.get("ResultCode") not in ("9",):
            return code, rate
    except Exception:
        pass
    return None, None


def match_tax_rate(city, postal=None, rates=None, street=None):
    """Match a city (+ ZIP for county splits) to ONE office tax rate.

    Returns (rate_dict, note_string). Exactly one of the two is None:
      * unambiguous match  -> (rate, None)
      * zero or several    -> (None, "⚠ TAX not set — ...") for the office
    """
    if rates is None:
        rates = fetch_tax_rates()

    # BEST PATH: Washington's own API → location code → the office's
    # Jobber rate carrying that code in its name ("Monroe 3112",
    # "Snohomish 4231" — the office named them with the state's codes).
    # Exact per-ADDRESS answer; kills the Snohomish/Bellevue ambiguity.
    if street:
        code, wa_rate = wa_dor_location_code(street, city, postal)
        if code:
            hits = [r for r in rates if code in r["name"]]
            if len(hits) == 1:
                return hits[0], None
            if not hits and wa_rate:
                return None, (f"⚠ TAX: WA state says code {code} "
                              f"({wa_rate*100:.1f}%) but no Jobber rate "
                              "carries that code — office: add it once, "
                              "then this fills automatically.")

    city_key = (city or "").strip().lower()
    if not city_key:
        return None, "⚠ TAX not set — no city on the address."
    city_key = TAX_CITY_ALIASES.get(city_key, city_key)

    hint = ZIP_TAX_HINTS.get((postal or "").strip()[:5])
    if hint:
        for r in rates:
            if r["name"].lower().startswith(hint.lower()):
                return r, None

    matches = [r for r in rates if r["name"].lower().startswith(city_key)]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, (f"⚠ TAX not set — no rate found for '{city}'. "
                      "Office: pick the correct rate.")
    labels = "; ".join(m["label"] for m in matches)
    return None, (f"⚠ TAX not set — '{city}' has multiple rates, "
                  f"office picks one: {labels}")


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

def create_draft_quote(client_id, property_id, bid, prop_info=None,
                       address=None):
    """bid = the dict from calculate_bid: has line items + office notes.
    address = the service address string; used to auto-attach the
    office's city tax rate (unambiguous matches only).

    We attach our office notes and confidence as the quote message so
    the reviewer sees exactly why the engine priced it this way.
    """
    line_items = [{
        "name": s["name"],
        "quantity": 1,
        "unitPrice": float(s["price"]),
        "saveToProductsAndServices": False,   # don't pollute their catalog
    } for s in bid["services"]]

    # INTERNAL notes (confidence, hazards, move-items) go on the quote as a
    # pinned internal note — the office/tech see them, the CUSTOMER NEVER DOES.
    # The client-facing `message` stays clean and professional.
    customer_lines = [n[len("CUSTOMER:"):].strip()
                      for n in bid["notes"] if n.startswith("CUSTOMER:")]
    note_lines = [f"Auto-generated draft — confidence {bid['confidence']}%."]
    note_lines += [f"⚠ {n}" for n in bid["notes"]
                   if not n.startswith("CUSTOMER:")]

    tax_rate_id = None
    if address:
        addr = split_address(address)
        rate, tax_note = match_tax_rate(addr["city"], addr["postalCode"],
                                        street=addr["street1"])
        if rate:
            tax_rate_id = rate["id"]
            note_lines.append(f"Tax auto-attached: {rate['label']}")
        else:
            note_lines.append(tax_note)

    variables = {"attributes": {
        "clientId": client_id,
        "propertyId": property_id,
        "title": "TEST - Bid Engine draft" if TEST_MODE else "Service Quote",
        "lineItems": line_items,
        "message": "Thank you for requesting a quote from Master Butler! "
                   "Please review the services below and let us know of any "
                   "questions."
                   + ("".join("\n\n" + c for c in customer_lines)),
                   # customer-facing: generic text + explicit CUSTOMER: notes only
        "notes": [{"message": "\n".join(note_lines), "pinned": True}],
        "customFields": build_custom_fields(prop_info),
        # NOTE: no transitionQuoteTo, no send/deliver anywhere. Draft only.
    }}
    if tax_rate_id:
        variables["attributes"]["taxRateId"] = tax_rate_id
    return _post(CREATE_QUOTE, variables, "create DRAFT quote")


# ── COMBINE: add services to a customer's existing OPEN quote ──
# (Tom's request: open quotes wanting more services get combined,
#  not duplicated.)
OPEN_QUOTES = """
query Recent($first: Int!) {
  quotes(first: $first, sort: {key: CREATED_AT, direction: DESCENDING}) {
    nodes { id quoteNumber quoteStatus jobberWebUri
            amounts { total }
            client { emails { address } } }
  }
}
"""

OPEN_STATUSES = ("draft", "awaiting_response", "changes_requested")


def find_open_quote(email_addr, scan=40):
    """Newest OPEN (unconverted) quote for this exact client email."""
    if not email_addr:
        return None
    global DRY_RUN
    was, DRY_RUN = DRY_RUN, False          # read-only; dry-run guards writes
    try:
        data = _post(OPEN_QUOTES, {"first": scan}, "find open quote")
    finally:
        DRY_RUN = was
    if data.get("error"):
        return None
    for q in data.get("quotes", {}).get("nodes", []):
        if q.get("quoteStatus") not in OPEN_STATUSES:
            continue
        addrs = [e["address"].lower() for e in (q.get("client") or {})
                 .get("emails", [])]
        if email_addr.lower() in addrs:
            return q
    return None


ADD_LINES = """
mutation($quoteId: EncodedId!, $items: [QuoteCreateLineItemAttributes!]!) {
  quoteCreateLineItems(quoteId: $quoteId, lineItems: $items) {
    quote { quoteNumber amounts { total } }
    userErrors { message } } }
"""


def add_lines_to_quote(quote_node_id, services):
    """Append this bid's line items to an existing quote (stays a draft
    of whatever status it had; nothing sends)."""
    items = [{"name": s["name"], "quantity": 1,
              "unitPrice": float(s["price"]),
              "saveToProductsAndServices": False} for s in services]
    return _post(ADD_LINES, {"quoteId": quote_node_id, "items": items},
                 "combine into open quote")


# ─────────────────────────────────────────────────────────────
# THE ONE FUNCTION THE PIPELINE CALLS
# ─────────────────────────────────────────────────────────────

def push_approved_bid(customer, bid, prop_info=None):
    """customer = {name, email, phone, address}; bid = calculate_bid output;
    prop_info = measured facts for custom fields (sqft, pitch, roof, stories).
    Creates client (if new) + property + DRAFT quote. Never sends."""
    client_id = find_or_create_client(
        customer.get("name"), customer.get("email"), customer.get("phone"))
    property_id = create_property(client_id, customer.get("address", ""))
    return create_draft_quote(client_id, property_id, bid, prop_info,
                              address=customer.get("address"))


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
