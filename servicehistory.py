"""
MASTER BUTLER — SERVICE HISTORY SWEEP (LaRee's #1 list item, Jul 8)

"See a history of pricing and dates serviced for a specific service,
instead of just the last one" — without digging through invoices.

Walks every invoice and builds, PER PROPERTY (and per client as a
fallback), a per-service timeline:

  {"by_property": {"<address-slug>": {"gutter": [["2024-11-02", 250], ...],
                                      "window": [...]}},
   "by_client":   {"<name-key>": {...}}}

Service names bridge through the same conservative matcher the learning
store uses ('Gutter Cleaning - Composition' -> 'gutter'). Read-only,
polite paging. Output: data/service_history.json + mirrored to the
cloud so the dashboard shows it on every bid and property page.
"""

import json
import re
import time
from pathlib import Path

import jobber_client as jc
from store import _service_key

QUERY = """
query Recent($first: Int!, $after: String) {
  invoices(first: $first, after: $after, sort: {key: CREATED_AT, direction: DESCENDING}) {
    pageInfo { hasNextPage endCursor }
    nodes { issuedDate
            client { name }
            properties(first: 1) { nodes { address { street city } } }
            lineItems(first: 25) { nodes { name totalPrice } } }
  }
}
"""


def _slug(street, city):
    return re.sub(r"[^a-z0-9]+", "-",
                  f"{street or ''} {city or ''}".lower()).strip("-")[:60]


def _name_key(name):
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def sweep(limit=100000):
    jc.DRY_RUN = False
    by_prop, by_client = {}, {}
    scanned, cursor = 0, None
    while scanned < limit:
        data = None
        for attempt in range(8):
            try:
                data = jc._post(QUERY, {"first": min(10, limit - scanned),
                                        "after": cursor}, "service history")
            except Exception as e:
                print(f"  retry {attempt+1} after {type(e).__name__}")
                time.sleep(10 * (attempt + 1))
                continue
            if data.get("error") and "THROTTLED" in str(
                    data.get("body", "")).upper():
                time.sleep(15 * (attempt + 1))
                continue
            break
        if data is None or data.get("error"):
            print(f"stopping at {scanned}: {str(data)[:120]}")
            break
        block = data["invoices"]
        for inv in block["nodes"]:
            scanned += 1
            date = (inv.get("issuedDate") or "")[:10]
            props = ((inv.get("properties") or {}).get("nodes") or [{}])
            addr = (props[0] if props else {}).get("address") or {}
            slug = _slug(addr.get("street"), addr.get("city"))
            ckey = _name_key((inv.get("client") or {}).get("name"))
            for li in (inv.get("lineItems") or {}).get("nodes", []):
                key = _service_key(li.get("name"))
                price = li.get("totalPrice") or 0
                if not key or price <= 0:
                    continue
                if slug:
                    by_prop.setdefault(slug, {}).setdefault(key, []) \
                        .append([date, price])
                if ckey:
                    by_client.setdefault(ckey, {}).setdefault(key, []) \
                        .append([date, price])
        if scanned % 500 < 10:
            print(f"  ...{scanned} invoices")
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(2)

    out = {"by_property": by_prop, "by_client": by_client}
    path = Path("data") / "service_history.json"
    path.write_text(json.dumps(out, indent=0))
    print(f"scanned {scanned} invoices → {len(by_prop)} properties, "
          f"{len(by_client)} clients → {path}")
    try:
        from cloudpush import push
        push(blobs={"service_history": out})
        print("mirrored to the cloud dashboard")
    except Exception as e:
        print(f"(cloud mirror skipped: {e})")
    return out


if __name__ == "__main__":
    sweep()
