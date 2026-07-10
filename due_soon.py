"""
MASTER BUTLER — DUE FOR THEIR ANNUAL (Jul 10 cycle)

The churn counterpunch: the churn report found the leak is customers
drifting away between seasons, not pricing. This finds loyal ACTIVE
customers whose own yearly rhythm says they're due NOW — so the office
reaches out BEFORE they land on the win-back list.

Cadence is per-customer (median gap between their invoices, 9-15 mo =
annual); the due window is 85%-120% of THEIR rhythm. Output ranked by
lifetime value → data/due_soon.json + cloud blob 'due_soon' (Win-back
page card + morning brief). Refreshed by the night run.
"""

import json
import statistics
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent


def compute():
    hist = json.loads((BASE / "data" / "client_history.json").read_text())
    today = datetime.now()
    due = []
    for cid, c in hist.items():
        inv = sorted(c.get("invoices") or [])
        if len(inv) < 2:
            continue
        dates = [datetime.strptime(d, "%Y-%m-%d") for d, _ in inv]
        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        med = statistics.median(gaps)
        if not 270 <= med <= 460:          # annual-rhythm customers only
            continue
        since = (today - dates[-1]).days
        if not (med * .85) <= since <= (med * 1.2):
            continue
        due.append({"id": cid, "name": c["name"].strip(),
                    "visits": len(inv),
                    "years": len({d.year for d in dates}),
                    "last": inv[-1][0],
                    "last_total": round(inv[-1][1]),
                    "cadence_days": int(med), "days_since": since,
                    "lifetime": round(sum(t for _, t in inv))})
    due.sort(key=lambda r: -r["lifetime"])
    return due


def run():
    due = compute()
    (BASE / "data" / "due_soon.json").write_text(json.dumps(due))
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob("due_soon", due)
        else:
            from cloudpush import push
            push(blobs={"due_soon": due})
    except Exception:
        pass
    print(f"due-for-annual: {len(due)} customers, "
          f"${sum(r['lifetime'] for r in due):,} lifetime")
    return due


if __name__ == "__main__":
    run()
