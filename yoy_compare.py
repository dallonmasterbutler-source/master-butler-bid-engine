"""
MASTER BUTLER — JULY vs LAST JULY, RUNNING TALLY
(Tom's notes via Dallon, Jul 14: "direct comparison to last year …
he would like a running tally.")

Every night this re-mines Jobber invoices for the MATCHED window —
July 1 through *today's date* in both years — so on the 14th it's
1–14 vs 1–14, on the 20th it's 1–20 vs 1–20. Apples to apples, per
service, dollars and counts. Writes the `yoy_july` blob; the Scoreboard
card renders whatever the blob holds and labels the window from it.

READ-ONLY against Jobber. Runs from night_run (the rate-limit bucket
is empty by day — the Jul-14 afternoon attempt starved on Throttled).
"""

import collections
import time
from datetime import date

Q = """
query($after: String) {
  invoices(first: 60, after: $after, sort: {key: CREATED_AT, direction: DESCENDING}) {
    pageInfo { hasNextPage endCursor }
    nodes { createdAt amounts { total }
            lineItems { nodes { name totalPrice } } } } }"""

CANON = ("gutter guard", "gutter", "window", "roof blow", "moss treat",
         "moss remov", "pressure", "dryer vent", "skylight", "light",
         "handyman", "minimum", "product")



def _save_blob(name, val):
    """Cloud direct when possible; HTTPS courier from the Mac."""
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob(name, val)
            return True
    except Exception:
        pass
    try:
        from cloudpush import push
        push(blobs={name: val})
        return True
    except Exception:
        return False

def run(verbose=False, today=None):
    import jobber_client as jc
    d0 = today or date.today()
    if d0.month != 7:                    # July feature; generalize later
        return None
    day = d0.day
    W = {str(d0.year): (f"{d0.year}-07-01", f"{d0.year}-07-{day + 1:02d}"),
         str(d0.year - 1): (f"{d0.year - 1}-07-01",
                            f"{d0.year - 1}-07-{day + 1:02d}")}
    floor = f"{d0.year - 1}-07-01"
    svc = {y: collections.Counter() for y in W}
    rev = {y: collections.Counter() for y in W}
    tot = {y: [0, 0.0] for y in W}
    was, jc.DRY_RUN = jc.DRY_RUN, False
    after, n, throttles = None, 0, 0
    try:
        while True:
            resp = jc._post(Q, {"after": after}, "yoy tally (read-only)")
            if resp.get("error"):
                if "Throttled" in str(resp) and throttles < 40:
                    throttles += 1
                    time.sleep(60)
                    continue
                return None              # give up quietly; retry tomorrow
            conn = resp["invoices"]
            stop = False
            for node in conn["nodes"]:
                c = (node.get("createdAt") or "")[:10]
                if c < floor:
                    stop = True
                    break
                n += 1
                for y, (a, b) in W.items():
                    if a <= c < b:
                        tot[y][0] += 1
                        tot[y][1] += ((node.get("amounts") or {})
                                      .get("total") or 0)
                        for li in ((node.get("lineItems") or {})
                                   .get("nodes") or []):
                            key = (li.get("name") or "?").strip().lower()
                            for canon in CANON:
                                if canon in key:
                                    key = canon
                                    break
                            else:
                                key = key[:30]
                            svc[y][key] += 1
                            rev[y][key] += li.get("totalPrice") or 0
            if stop or not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
            time.sleep(4)
    finally:
        jc.DRY_RUN = was
    blob = {"window_label": f"July 1–{day}",
            "years": sorted(W, reverse=True),
            "totals": {y: {"invoices": tot[y][0],
                           "revenue": round(tot[y][1])} for y in W},
            "services": {y: {k: {"count": svc[y][k],
                                 "revenue": round(rev[y][k])}
                             for k in svc[y]} for y in W},
            "mined_at": d0.isoformat(), "scanned": n}
    _save_blob("yoy_july", blob)
    if verbose:
        for y in sorted(W, reverse=True):
            print(f"July 1–{day} {y}: {tot[y][0]} invoices, "
                  f"${tot[y][1]:,.0f}")
        print(f"(scanned {n} invoices, {throttles} throttle waits)")
    return blob


if __name__ == "__main__":
    run(verbose=True)


def run_local(verbose=False):
    """THE DAY-MATCHED RACE, THROTTLE-FREE (Tom via Dallon, Jul 15:
    'exact date match… running numbers for the month… with the split').
    Computes July 1→today for BOTH years from the local invoice-line
    archive (service_history.json, refreshed nightly) — no Jobber API,
    so it can never starve. Same method both years = a fair race."""
    import json
    import collections
    from pathlib import Path
    from datetime import date
    d0 = date.today()
    if d0.month != 7:
        return None
    day = d0.day
    # file on the Mac, blob on Render (Jul 16 cloud-ify) — one loader
    from servicehistory import load_history
    byp = (load_history() or {}).get("by_property") or {}
    if not byp:
        return None
    LABEL = {"gutter": "Gutters", "roof blow": "Roof blow-off",
             "moss": "Moss", "window_exterior": "Windows (ext)",
             "window_inout": "Windows (in&out)", "window": "Windows",
             "dryer": "Dryer vents", "patio": "Pressure wash",
             "sidewalk": "Pressure wash", "driveway": "Pressure wash",
             "house wash": "House wash", "light": "Holiday lights"}
    yrs = (str(d0.year), str(d0.year - 1))
    svc = {y: collections.Counter() for y in yrs}
    rev = {y: collections.Counter() for y in yrs}
    tot = {y: [0, 0.0] for y in yrs}
    seen = {y: set() for y in yrs}
    for prop, buckets in byp.items():
        for key, entries in buckets.items():
            lbl = next((v for k, v in LABEL.items() if k in key),
                       key.replace("_", " ").title())
            for e in entries:
                if not e or not e[0]:
                    continue
                dte, price = e[0], (e[1] if len(e) > 1 else 0) or 0
                for y in yrs:
                    if f"{y}-07-01" <= dte <= f"{y}-07-{day:02d}":
                        svc[y][lbl] += 1
                        rev[y][lbl] += price
                        tot[y][1] += price
                        seen[y].add((prop, dte))
    for y in yrs:
        tot[y][0] = len(seen[y])
    # MONTH-TO-MONTH HISTORY (Tom via Dallon, Jul 15): every month's
    # line revenue for 3 trailing years, so the card can race any month
    # against the same month last year.
    monthly = collections.defaultdict(lambda: [0, 0.0])
    seen_m = collections.defaultdict(set)
    floor_m = f"{d0.year - 2}-01"
    for prop, buckets in byp.items():
        for key, entries in buckets.items():
            for e in entries:
                if not e or not e[0]:
                    continue
                mo2 = e[0][:7]
                if mo2 >= floor_m:
                    monthly[mo2][1] += (e[1] if len(e) > 1 else 0) or 0
                    seen_m[mo2].add((prop, e[0]))
    for m2 in monthly:
        monthly[m2][0] = len(seen_m[m2])
    blob = {"window_label": f"July 1–{day} (day-matched)",
            "monthly": {m2: {"jobs": v[0], "revenue": round(v[1])}
                        for m2, v in sorted(monthly.items())},
            "years": list(yrs),
            "totals": {y: {"invoices": tot[y][0],
                           "revenue": round(tot[y][1])} for y in yrs},
            "services": {y: {k: {"count": svc[y][k],
                                 "revenue": round(rev[y][k])}
                             for k in svc[y]} for y in yrs},
            "mined_at": d0.isoformat(),
            "note": "line-item revenue from the invoice archive, same "
                    "method both years"}
    _save_blob("yoy_july", blob)
    if verbose:
        for y in yrs:
            print(f"  July 1-{day} {y}: {tot[y][0]} jobs, "
                  f"${tot[y][1]:,.0f}")
    return blob
