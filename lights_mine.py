"""
MASTER BUTLER — HOLIDAY LIGHTS LABOR CALIBRATION (the overnight task,
Dallon Jul 9: lights pricing isn't nailed — roofline + materials are
automatic, LABOR is Tom's number with no data behind it.)

Two passes:
  1. Sweep Jobber invoice history for holiday-light line items —
     price, date, client, property address.
  2. For a sample of those homes, measure the front roofline from the
     aerial (same pre-measure the intake uses) → implied $/ft.

Output: data/lights_calibration.json + a plain-English report.
NO pricing changes — labor stays Tom's call; this hands Dallon the
evidence to set an anchor.

Run:  python3 lights_mine.py            (sweep 1500 invoices, sample 20)
"""

import json
import re
from pathlib import Path

import jobber_client as jc

BASE = Path(__file__).parent
OUT = BASE / "data" / "lights_calibration.json"

LIGHTS_RE = re.compile(r"light", re.I)
NOT_LIGHTS_RE = re.compile(r"skylight|light\s*fixture|lightweight", re.I)

INVOICES_Q = """
query Lights($first: Int!, $after: String) {
  invoices(first: $first, after: $after,
           sort: {key: CREATED_AT, direction: DESCENDING}) {
    pageInfo { hasNextPage endCursor }
    nodes {
      invoiceNumber issuedDate
      client { name }
      properties(first: 1) { nodes { address { street city postalCode } } }
      lineItems(first: 25) { nodes { name description totalPrice } }
    }
  }
}
"""


def sweep(max_invoices=1500):
    jc.DRY_RUN = False
    hits, scanned, cursor = [], 0, None
    while scanned < max_invoices:
        d = jc._post(INVOICES_Q, {"first": 100, "after": cursor},
                     "lights sweep")
        if d.get("error"):
            print("API error:", str(d)[:200])
            break
        page = d["invoices"]
        for inv in page["nodes"]:
            scanned += 1
            for li in inv["lineItems"]["nodes"]:
                text = f"{li['name']} {li.get('description') or ''}"
                if not LIGHTS_RE.search(li["name"]) \
                        or NOT_LIGHTS_RE.search(text):
                    continue
                price = li.get("totalPrice") or 0
                if price <= 0:
                    continue
                props = ((inv.get("properties") or {})
                         .get("nodes") or [])
                a = (props[0].get("address") or {}) if props else {}
                addr = ", ".join(x for x in (a.get("street"),
                                             a.get("city"),
                                             a.get("postalCode")) if x)
                kind = ("takedown" if re.search(r"take\s*down|removal",
                                                text, re.I)
                        else "install")
                hits.append({"invoice": inv["invoiceNumber"],
                             "date": inv.get("issuedDate"),
                             "client": (inv.get("client") or {}).get("name"),
                             "address": addr,
                             "line": li["name"][:80],
                             "kind": kind,
                             "price": price})
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    print(f"scanned {scanned:,} invoices — {len(hits)} light line(s)",
          flush=True)
    return hits


def measure_sample(hits, n=20):
    """Aerial roofline for a sample of installed homes → implied $/ft."""
    from lights import estimate_for
    done, out = set(), []
    installs = [h for h in hits if h["kind"] == "install"
                and h["address"] and h["price"] >= 200]
    installs.sort(key=lambda h: h["date"] or "", reverse=True)
    for h in installs:
        if h["address"] in done or len(out) >= n:
            continue
        done.add(h["address"])
        try:
            est, note = estimate_for(h["address"])
        except Exception as e:
            est, note = None, str(e)[:80]
        if not est:
            print(f"  ✗ {h['address'][:44]} — {note[:60]}", flush=True)
            continue
        lo, hi = est["front_ft"]
        mid = (lo + hi) / 2
        out.append({**h, "front_ft": mid,
                    "dollars_per_ft": round(h["price"] / mid, 2)})
        print(f"  ✓ {h['address'][:40]:40} ${h['price']:>7,.0f} "
              f"/ ~{mid:.0f} ft = ${h['price']/mid:,.2f}/ft", flush=True)
    return out


def run():
    hits = sweep()
    sample = measure_sample(hits)
    per_ft = sorted(s["dollars_per_ft"] for s in sample)
    med = per_ft[len(per_ft)//2] if per_ft else None
    report = {"lines_found": len(hits),
              "installs": sum(1 for h in hits if h["kind"] == "install"),
              "takedowns": sum(1 for h in hits if h["kind"] == "takedown"),
              "sampled": len(sample),
              "dollars_per_ft_all": per_ft,
              "dollars_per_ft_median": med,
              "sample": sample,
              "all_lines": hits}
    OUT.write_text(json.dumps(report, indent=1))
    print("\nLIGHTS CALIBRATION" + "=" * 40)
    print(f"  light lines in history: {len(hits)} "
          f"({report['installs']} installs / {report['takedowns']} "
          "takedowns)")
    if med:
        print(f"  measured sample: {len(sample)} homes — "
              f"median ${med:,.2f}/front-ft (installed price incl labor "
              f"+ materials; materials are $1.45/ft)")
        print(f"  → implied LABOR ≈ ${med - 1.45:,.2f}/ft at the median")
    print(f"  full data -> {OUT}")


if __name__ == "__main__":
    run()
