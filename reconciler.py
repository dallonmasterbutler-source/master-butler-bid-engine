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


def parse_next_year_price(text):
    """Extract the 'price for 20XX will be $X' promise, skipping junk:
    years masquerading as prices ($2026) and tiny numbers that are really
    percentages or year fragments. Real promises are $40+."""
    for m in NEXT_PRICE_RE.finditer(text):
        val = float(m.group(1))
        if 2000 <= val <= 2099:      # that's a year, not a price
            continue
        if val < 40:                 # % or fragment, not a service price
            continue
        return val
    return None

# Discount taxonomy — every discount line gets sorted into ONE bucket so the
# learning loop only trains on the right kind:
#   honor                 = underquote the office honored (LEARNING GOLD)
#   service_not_performed = part of the job wasn't done (not an underquote)
#   promo                 = seasonal/marketing discount (intentional, not an error)
#   other_discount        = unrecognized — surfaced for a human to look at
NOT_PERFORMED_RE = re.compile(
    r"not\s+(?:performed|completed|done|serviced)|did\s*not\s+(?:do|complete|"
    r"perform|service)|didn'?t\s+(?:do|complete|finish)|unable\s+to|"
    r"skipped|no\s+longer\s+needed|removed\s+from\s+(?:job|invoice)",
    re.IGNORECASE)
PROMO_RE = re.compile(
    r"early\s+install|promo|seasonal|holiday|referral|senior|military|"
    r"new\s+customer|\d{1,2}\s*%|percent\s+off|coupon|special",
    re.IGNORECASE)


def classify_discount(text):
    """Sort one discount line into its taxonomy bucket. Honor wins ties —
    if the office wrote 'honor' anywhere, that's what it is."""
    if HONOR_RE.search(text):
        return "honor"
    if NOT_PERFORMED_RE.search(text):
        return "service_not_performed"
    if PROMO_RE.search(text):
        return "promo"
    return "other_discount"

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
    """Split one invoice into service lines vs discount lines.

    A line counts as a discount if it's negative-priced, is literally named
    'discount', or mentions honoring — then classify_discount() decides
    which bucket it belongs to. FULL text is kept (no truncation) so a
    'price for 20XX will be $X' promise buried late in a long note
    is never lost.
    """
    services, discounts = [], []
    for li in inv["lineItems"]["nodes"]:
        text = f"{li['name']} {li.get('description') or ''}".strip()
        price = li.get("totalPrice") or 0
        is_discount = (price < 0
                       or li["name"].strip().lower() == "discount"
                       or HONOR_RE.search(text))
        if is_discount:
            discounts.append({
                "category": classify_discount(text),
                "text": text,                      # full length, on purpose
                "amount": abs(price),
                "next_year_price": parse_next_year_price(text),
            })
        else:
            services.append({"name": li["name"], "price": price})
    return services, discounts


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
            services, discounts = parse_invoice(inv)
            if discounts:
                honors = [d for d in discounts if d["category"] == "honor"]
                true_total = sum(s["price"] for s in services)
                found.append({
                    "invoice": inv["invoiceNumber"],
                    "date": inv["issuedDate"],
                    "client": inv["client"]["name"],
                    "paid_total": inv["amounts"]["total"],
                    "true_total": true_total,
                    "categories": sorted({d["category"] for d in discounts}),
                    "honored_gap": sum(h["amount"] for h in honors),
                    "next_year_price": next((d["next_year_price"]
                                             for d in discounts
                                             if d["next_year_price"]), None),
                    "discounts": discounts,
                })
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(2)   # let the cost bucket breathe between pages
    return scanned, found


if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    scanned, found = sweep(limit=limit)

    honors = [f for f in found if "honor" in f["categories"]]
    tally = {}
    for f in found:
        for d in f["discounts"]:
            tally[d["category"]] = tally.get(d["category"], 0) + 1

    print(f"\nScanned {scanned} invoices — {len(found)} carried discounts, "
          f"{len(honors)} were honor-pricing corrections")
    print("Discount buckets: " + ", ".join(
        f"{k}={v}" for k, v in sorted(tally.items())))
    print()
    for f in found:
        promise = (f" | promise: ${f['next_year_price']:.0f}"
                   if f["next_year_price"] else "")
        cats = "/".join(f["categories"])
        print(f"  #{f['invoice']} {f['date'][:10]} {f['client'][:26]:<26} "
              f"paid ${f['paid_total']:<8} true ${f['true_total']:<8} "
              f"[{cats}] gap ${f['honored_gap']:.0f}{promise}")
        print(f"      \"{f['discounts'][0]['text'][:110]}\"")
    out = Path("data") / "discount_reconciliation.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(found, indent=1))
    print(f"\nSaved -> {out}")
