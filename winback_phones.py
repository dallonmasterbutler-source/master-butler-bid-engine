"""
MASTER BUTLER — WIN-BACK PHONE ENRICHMENT

The win-back page lists 1,396 lost loyal clients; LaRee shouldn't have
to look up each number. Walk the list (top value first), find each
client in Jobber by exact name, pull their primary phone, and write it
back into the churn_report blob the page reads.

Read-only against Jobber. Polite pacing. Safe to re-run (skips rows
that already have a phone).
"""

import json
import time
from pathlib import Path

import jobber_client as jc

Q = """query Find($term: String!) {
  clients(searchTerm: $term, first: 3) {
    nodes { name phones { number primary } }
  }
}"""


def enrich(limit=500):
    jc.DRY_RUN = False
    path = Path("data/churn_report.json")
    rep = json.loads(path.read_text())
    rows = rep["loyal_then_gone"]
    done = 0
    for r in rows[:limit]:
        if r.get("phone"):
            continue
        try:
            d = jc._post(Q, {"term": r["name"]}, "winback phone")
        except Exception:
            time.sleep(10)
            continue
        if d.get("error"):
            time.sleep(10)
            continue
        for n in (d.get("clients") or {}).get("nodes", []):
            if n["name"].strip().lower() != r["name"].strip().lower():
                continue
            phones = n.get("phones") or []
            pick = next((p for p in phones if p.get("primary")),
                        phones[0] if phones else None)
            if pick:
                r["phone"] = pick["number"]
                done += 1
            break
        if done and done % 50 == 0:
            path.write_text(json.dumps(rep, indent=1))
            print(f"  ...{done} phones")
        time.sleep(1.2)
    path.write_text(json.dumps(rep, indent=1))
    print(f"phones added: {done}")
    try:
        from cloudpush import push
        push(blobs={"churn_report": rep})
        print("win-back page updated in the cloud")
    except Exception as e:
        print(f"(cloud push skipped: {e})")


if __name__ == "__main__":
    enrich()
