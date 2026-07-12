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
    nodes { issuedDate invoiceNumber
            client { name }
            properties(first: 1) { nodes { address { street city } } }
            lineItems(first: 25) { nodes { name totalPrice } } }
  }
}
"""


def _discount_factors():
    """{invoice_number: scale} from the reconciler's discount mining —
    the office writes discounts in the notes, and the reconciler already
    read them all (Dallon, Jul 12: floors must be the PRE-discount
    price). paid $850 with a $150 promo => every line scales ×1.176.
    service_not_performed gaps are NOT discounts (work didn't happen) —
    those invoices keep their real line prices."""
    try:
        import clouddb
        if clouddb.available():
            d = clouddb.get_blob("discount_factors")
            if d:
                return d
    except Exception:
        pass
    factors = {}
    for f in ("discount_reconciliation.json",
              "discount_reconciliation_recent.json"):
        p = Path("data") / f
        if not p.exists():
            continue
        for e in json.loads(p.read_text()):
            paid = e.get("paid_total") or 0
            disc = sum((d.get("amount") or 0)
                       for d in (e.get("discounts") or [])
                       if d.get("category") != "service_not_performed")
            if paid > 0 and disc > 0:
                factors[str(e.get("invoice"))] = round(
                    min((paid + disc) / paid, 2.0), 4)
    return factors


def _slug(street, city):
    return re.sub(r"[^a-z0-9]+", "-",
                  f"{street or ''} {city or ''}".lower()).strip("-")[:60]


def _name_key(name):
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


# a DISCOUNTED line must never become a price floor (Dallon, Jul 12:
# 'when price matching past invoices make sure we aren't taking into
# account the discounts they had'). Standalone discount lines already
# key to None; this catches hybrids like 'Window Cleaning Discount
# Package' that would otherwise anchor the customer at the deal price.
_DISC_RX = re.compile(r"discount|coupon|%\s*off|special|promo|deal\b",
                      re.I)


def _looks_discounted(line_name):
    return bool(_DISC_RX.search(line_name or ""))


def sweep(limit=100000):
    jc.DRY_RUN = False
    by_prop, by_client = {}, {}
    factors = _discount_factors()
    undiscounted = 0
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
            fac = factors.get(str(inv.get("invoiceNumber")), 1.0)
            if fac > 1.0:
                undiscounted += 1
            for li in (inv.get("lineItems") or {}).get("nodes", []):
                key = _service_key(li.get("name"))
                price = li.get("totalPrice") or 0
                if not key or price <= 0 \
                        or _looks_discounted(li.get("name")):
                    continue
                price = round(price * fac, 2)   # PRE-discount floor
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
          f"{len(by_client)} clients → {path} "
          f"({undiscounted} discounted invoices scaled to pre-discount)")
    try:
        from cloudpush import push
        push(blobs={"service_history": out})
        print("mirrored to the cloud dashboard")
    except Exception as e:
        print(f"(cloud mirror skipped: {e})")
    return out


def refresh(recent=120):
    """Nightly incremental (Jul 10 cycle): sweep only the newest
    invoices and MERGE into the existing history so the 'Past here'
    column and LaRee's per-service card stay current without the
    2-3 hour full sweep."""
    path = Path("data") / "service_history.json"
    if not path.exists():
        return sweep(limit=recent)
    old = json.loads(path.read_text())
    sweep(limit=recent)                # writes a recent-only file
    new = json.loads(path.read_text())
    for side in ("by_property", "by_client"):
        merged = old.get(side, {})
        for k, svcs in new.get(side, {}).items():
            slot = merged.setdefault(k, {})
            for svc, visits in svcs.items():
                have = {tuple(v) for v in slot.get(svc, [])}
                slot.setdefault(svc, []).extend(
                    [d, p] for d, p in visits if (d, p) not in have)
        old[side] = merged
    path.write_text(json.dumps(old, indent=0))
    try:
        from cloudpush import push
        push(blobs={"service_history": old})
    except Exception:
        pass
    print(f"service history merged (+recent {recent} invoices)")
    return old


if __name__ == "__main__":
    sweep()
