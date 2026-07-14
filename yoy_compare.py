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


def run(verbose=False, today=None):
    import clouddb
    import jobber_client as jc
    if not clouddb.available():
        return None
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
    clouddb.put_blob("yoy_july", blob)
    if verbose:
        for y in sorted(W, reverse=True):
            print(f"July 1–{day} {y}: {tot[y][0]} invoices, "
                  f"${tot[y][1]:,.0f}")
        print(f"(scanned {n} invoices, {throttles} throttle waits)")
    return blob


if __name__ == "__main__":
    run(verbose=True)
