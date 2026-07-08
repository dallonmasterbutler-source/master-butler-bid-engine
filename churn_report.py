"""
MASTER BUTLER — CHURN REPORT (Dallon's request, Jul 8)

"Find the jobs that we did for a few years and then they never used us
again. Or those that we did once and never came back... see if we can
infer if it was the price that made them leave."

Reads data/client_history.json (from churn_sweep.py) and builds:

  * LOYAL-THEN-GONE: clients with 2+ years of history whose last
    invoice is old enough that they've clearly skipped a season.
  * ONE-AND-DONE: single invoice, never returned.
  * PRICE INFERENCE: for each lost client, did their LAST invoice jump
    vs. their own typical spend? Compared against the same jump rate
    among clients who STAYED, so the number means something.

Output: printed report + data/churn_report.json (+ cloud blob).
"""

import json
import statistics
from datetime import date
from pathlib import Path

TODAY = date(2026, 7, 8)
# Annual/seasonal service business: someone who hasn't booked in 20
# months has skipped at least one full season on purpose.
CHURN_MONTHS = 20
PRICE_JUMP = 0.25          # last invoice ≥25% over their own median


def months_ago(iso):
    y, m = int(iso[:4]), int(iso[5:7])
    return (TODAY.year - y) * 12 + (TODAY.month - m)


def analyze():
    data = json.loads(Path("data/client_history.json").read_text())
    loyal_gone, one_done, active = [], [], []
    for cid, rec in data.items():
        invs = sorted(i for i in rec["invoices"] if i[0] and i[1] > 0)
        if not invs:
            continue
        first, last = invs[0], invs[-1]
        yrs = {d[:4] for d, _ in invs}
        gone = months_ago(last[0]) >= CHURN_MONTHS
        prior = [t for _, t in invs[:-1]]
        med = statistics.median(prior) if prior else None
        jump = (med and med > 0 and last[1] >= med * (1 + PRICE_JUMP))
        row = {"name": rec["name"], "n": len(invs),
               "years": len(yrs), "first": first[0], "last": last[0],
               "last_total": round(last[1]),
               "typical": round(med) if med else None,
               "lifetime": round(sum(t for _, t in invs)),
               "price_jump": bool(jump)}
        if gone and len(yrs) >= 2 and len(invs) >= 3:
            loyal_gone.append(row)
        elif gone and len(invs) == 1:
            one_done.append(row)
        elif not gone:
            active.append(row)

    # Baseline: among ACTIVE repeat clients, how often does the most
    # recent invoice show the same ≥25% jump? If lost clients jump far
    # more often, price is implicated.
    act_rep = [r for r in active if r["typical"]]
    base = sum(r["price_jump"] for r in act_rep) / max(len(act_rep), 1)
    lost_jump = sum(r["price_jump"] for r in loyal_gone) / max(len(loyal_gone), 1)

    loyal_gone.sort(key=lambda r: -r["lifetime"])
    lost_value = sum(r["lifetime"] for r in loyal_gone)

    out = {"as_of": TODAY.isoformat(), "churn_months": CHURN_MONTHS,
           "loyal_then_gone": loyal_gone, "one_and_done_count": len(one_done),
           "one_and_done_old": len([r for r in one_done]),
           "active_clients": len(active),
           "jump_rate_lost": round(lost_jump, 3),
           "jump_rate_active": round(base, 3),
           "lost_lifetime_value": round(lost_value)}
    Path("data/churn_report.json").write_text(json.dumps(out, indent=1))

    print(f"CHURN REPORT — as of {TODAY} (gone = {CHURN_MONTHS}+ months quiet)")
    print("=" * 60)
    print(f"Active clients (booked recently): {len(active)}")
    print(f"LOYAL-THEN-GONE (2+ yrs, 3+ jobs, then silence): "
          f"{len(loyal_gone)}")
    print(f"  their combined lifetime value: ${lost_value:,.0f}")
    print(f"ONE-AND-DONE (single job, never returned): {len(one_done)}")
    print()
    print(f"PRICE SIGNAL — final invoice jumped ≥{PRICE_JUMP:.0%} over "
          f"their own typical spend:")
    print(f"  lost loyal clients: {lost_jump:.0%}")
    print(f"  still-active clients (baseline): {base:.0%}")
    print()
    print("TOP 25 LOST LOYAL CLIENTS (by lifetime value):")
    for r in loyal_gone[:25]:
        tag = "  <- PRICE JUMP" if r["price_jump"] else ""
        print(f"  {r['name'][:32]:32} {r['n']:>3} jobs "
              f"{r['first'][:4]}-{r['last'][:4]}  last ${r['last_total']:>5}"
              f" (typ ${r['typical']})  ltv ${r['lifetime']:,}{tag}")
    try:
        from cloudpush import push
        push(blobs={"churn_report": out})
        print("\n(mirrored to cloud)")
    except Exception as e:
        print(f"\n(cloud mirror skipped: {e})")
    return out


if __name__ == "__main__":
    analyze()
