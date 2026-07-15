"""
MASTER BUTLER — SQFT TRUTH SWEEP (Dallon, Jul 15: 'jobber also has this
data from the office input. so we should get as much data as we can
outside of jobber and compare it to jobber').

Nightly, throttle-tolerant. Two passes:
  1. Sweep Jobber properties for custom fields — inventory every label
     the office has ever filled in (sqft lives SOMEWHERE; the first
     samples were empty, so find which homes carry it).
  2. Compare every sqft found against our own sources for the same
     address: assessor lookup, office corrections (fact_overrides),
     and what recent bids priced from. Disagreements land in the blob
     for the dashboard to show — each one is a learning.

→ blob sqft_compare + data/sqft_compare.json
"""

import json
import re
import time
from pathlib import Path

BASE = Path(__file__).parent

Q = """query($cursor: String) {
  properties(first: 60, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id
      address { street city }
      customFields { __typename
        ... on CustomFieldText { label valueText }
        ... on CustomFieldNumeric { label valueNumeric }
        ... on CustomFieldArea { label valueArea { length width } } } } } }"""


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


def _slug(address):
    return re.sub(r"[^a-z0-9]+", "-",
                  (address or "").lower()).strip("-")[:60]


def _sqft_from_field(f):
    """A custom field → sqft int if it plausibly is one."""
    label = (f.get("label") or "").lower()
    if not re.search(r"sq\.? ?ft|square|sqft|sf\b|footage", label):
        return None
    v = f.get("valueNumeric")
    if v is None:
        m = re.search(r"[\d,]{3,}", str(f.get("valueText") or ""))
        v = m.group(0).replace(",", "") if m else None
    if v is None and f.get("valueArea"):
        a = f["valueArea"]
        v = (a.get("length") or 0) * (a.get("width") or 0) or None
    try:
        n = int(float(v))
        return n if 200 <= n <= 30000 else None
    except (TypeError, ValueError):
        return None


def run(verbose=False, max_pages=200):
    import jobber_client as jc
    was, jc.DRY_RUN = jc.DRY_RUN, False
    labels, found, cursor, pages, throttles = {}, [], None, 0, 0
    try:
        while pages < max_pages:
            try:
                d = jc._post(Q, {"cursor": cursor}, "sqft sweep")
            except Exception as e:
                if "THROTTLED" in str(e).upper() and throttles < 30:
                    throttles += 1
                    time.sleep(20)
                    continue
                break
            pp = (d.get("properties") or {})
            for n in pp.get("nodes") or []:
                addr = " ".join(filter(None, [
                    (n.get("address") or {}).get("street"),
                    (n.get("address") or {}).get("city")]))
                for f in n.get("customFields") or []:
                    lb = f.get("label") or "?"
                    labels[lb] = labels.get(lb, 0) + 1
                    sq = _sqft_from_field(f)
                    if sq:
                        found.append({"address": addr, "sqft": sq,
                                      "label": lb})
            pages += 1
            pi = pp.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                break
            cursor = pi.get("endCursor")
            time.sleep(1.5)
    finally:
        jc.DRY_RUN = was

    # pass 2 — compare Jobber's sqft to ours for the same address
    ours = {}
    try:
        import facts_edit
        ov_all, _ = facts_edit._blob()
    except Exception:
        ov_all = {}
    diffs = []
    for row in found:
        sl = _slug(row["address"])
        office = (ov_all.get(sl) or {}).get("sqft")
        assessor = None
        try:
            from pipeline import lookup
            facts, _fl, _dd = lookup(row["address"])
            assessor = (facts or {}).get("sqft")
        except Exception:
            pass
        row["ours_assessor"] = assessor
        row["ours_office"] = office
        best = office or assessor
        if best and abs(best - row["sqft"]) / best > 0.15:
            diffs.append(dict(row, gap_pct=round(
                (row["sqft"] - best) / best * 100)))

    out = {"pages": pages, "throttled": throttles,
           "labels": labels, "with_sqft": len(found),
           "disagree_15pct": diffs[:200], "sample": found[:200]}
    (BASE / "data" / "sqft_compare.json").write_text(
        json.dumps(out, indent=1))
    _save_blob("sqft_compare", out)
    if verbose:
        print(f"sqft sweep: {pages} pages, labels={labels}, "
              f"{len(found)} sqft values, {len(diffs)} disagree >15%")
    return out


if __name__ == "__main__":
    run(verbose=True, max_pages=3)
