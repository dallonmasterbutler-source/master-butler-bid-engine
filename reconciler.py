"""
MASTER BUTLER — RECONCILER (the system's memory of what really happened)

Walks final Jobber INVOICES and extracts the truth the learning loop needs:

  * the PRE-DISCOUNT line prices  = what the job was really worth
    (office convention: underquoted jobs get the line RAISED to true price
     plus an "honor 20XX pricing" discount line — so the line item, not
     the total, is ground truth)
  * honor-pricing discount lines  = hand-labeled underquotes, with the gap
  * "price for 20XX will be $X"   = a stored PROMISE to pre-load into that
    customer's next rebooking quote

Read-only. Never modifies Jobber.
"""

import re
import json
import time
from pathlib import Path

import jobber_client as jc


def _query_patiently(query, variables, label, tries=5):
    """Jobber throttles by query cost. Sip politely: back off and retry."""
    for i in range(tries):
        data = jc._post(query, variables, label)
        body = str(data.get("body", ""))
        if data.get("error") and "THROTTLED" in body.upper():
            wait = 15 * (i + 1)
            print(f"    (throttled — waiting {wait}s)")
            time.sleep(wait)
            continue
        return data
    return data

HONOR_RE = re.compile(r"honou?r", re.IGNORECASE)
NEXT_PRICE_RE = re.compile(
    r"(?:20\d\d|next\s+year).{0,40}?\$?\s*(\d{2,5})", re.IGNORECASE)

INVOICES_QUERY = """
query Recent($first: Int!, $after: String) {
  invoices(first: $first, after: $after, sort: {key: CREATED_AT, direction: DESCENDING}) {
    pageInfo { hasNextPage endCursor }
    nodes {
      invoiceNumber issuedDate invoiceStatus
      client { name id }
      amounts { total discountAmount }
      lineItems(first: 25) { nodes { name description unitPrice quantity totalPrice } }
    }
  }
}
"""


def parse_invoice(inv):
    """Split one invoice into service lines vs honor-discount lines."""
    services, honors = [], []
    for li in inv["lineItems"]["nodes"]:
        text = f"{li['name']} {li.get('description') or ''}"
        price = li.get("totalPrice") or 0
        if HONOR_RE.search(text) or (li["name"].strip().lower() == "discount"
                                     and price < 0):
            m = NEXT_PRICE_RE.search(text)
            honors.append({
                "text": text.strip()[:160],
                "amount": abs(price),
                "next_year_price": float(m.group(1)) if m else None,
            })
        else:
            services.append({"name": li["name"], "price": price})
    return services, honors


def sweep(limit=200):
    """Pull recent invoices and report every honor-pricing correction found."""
    jc.DRY_RUN = False
    found, scanned, cursor = [], 0, None
    while scanned < limit:
        page = _query_patiently(INVOICES_QUERY,
                        {"first": min(10, limit - scanned), "after": cursor},
                        "recent invoices")
        if page.get("error"):
            raise SystemExit(f"query failed: {page}")
        block = page["invoices"]
        for inv in block["nodes"]:
            scanned += 1
            services, honors = parse_invoice(inv)
            if honors:
                true_total = sum(s["price"] for s in services)
                found.append({
                    "invoice": inv["invoiceNumber"],
                    "date": inv["issuedDate"],
                    "client": inv["client"]["name"],
                    "paid_total": inv["amounts"]["total"],
                    "true_total": true_total,
                    "honored_gap": sum(h["amount"] for h in honors),
                    "next_year_price": next((h["next_year_price"]
                                             for h in honors
                                             if h["next_year_price"]), None),
                    "honor_text": honors[0]["text"],
                })
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(2)   # let the cost bucket breathe between pages
    return scanned, found


if __name__ == "__main__":
    scanned, found = sweep(limit=200)
    print(f"Scanned {scanned} recent invoices — "
          f"{len(found)} honor-pricing corrections found\n")
    for f in found:
        promise = (f" | 20XX promise: ${f['next_year_price']:.0f}"
                   if f["next_year_price"] else "")
        print(f"  #{f['invoice']} {f['date'][:10]} {f['client'][:28]:<28} "
              f"paid ${f['paid_total']:<8} true ${f['true_total']:<8} "
              f"gap ${f['honored_gap']:.0f}{promise}")
        print(f"      \"{f['honor_text'][:110]}\"")
    out = Path("data") / "honor_corrections.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(found, indent=1))
    print(f"\nSaved -> {out}")
