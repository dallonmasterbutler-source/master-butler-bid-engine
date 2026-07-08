"""
MASTER BUTLER — CHURN DATA SWEEP (Dallon's request, Jul 8)

Walks every invoice and builds a per-CLIENT service history:
  client -> [(date, total), ...]

That's the raw material for the churn report:
  * customers we served for years who then stopped
  * one-and-done customers who never came back
  * and for each: what their LAST invoice looked like (did the price
    jump? was there a discount?) — to infer whether PRICE drove them off.

Read-only, polite paging (same manners as the reconciler).
Output: data/client_history.json
"""

import json
import time
from pathlib import Path

import jobber_client as jc

QUERY = """
query Recent($first: Int!, $after: String) {
  invoices(first: $first, after: $after, sort: {key: CREATED_AT, direction: DESCENDING}) {
    pageInfo { hasNextPage endCursor }
    nodes { issuedDate
            amounts { total }
            client { id name } }
  }
}
"""


def sweep(limit=100000):
    jc.DRY_RUN = False
    clients, scanned, cursor = {}, 0, None
    while scanned < limit:
        data = None
        for attempt in range(8):
            try:
                data = jc._post(QUERY, {"first": min(25, limit - scanned),
                                        "after": cursor}, "client history")
            except Exception as e:           # network hiccup / timeout
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
            c = inv.get("client") or {}
            cid = c.get("id")
            if not cid:
                continue
            rec = clients.setdefault(cid, {"name": c.get("name"),
                                           "invoices": []})
            rec["invoices"].append(
                [inv.get("issuedDate", "")[:10],
                 float(inv.get("amounts", {}).get("total") or 0)])
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(2)
    out = Path("data") / "client_history.json"
    out.write_text(json.dumps(clients, indent=0))
    print(f"scanned {scanned} invoices across {len(clients)} clients "
          f"-> {out}")
    return scanned, clients


if __name__ == "__main__":
    sweep()
