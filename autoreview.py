"""
MASTER BUTLER — SCOREBOARD AUTO-REVIEW (Dallon, Jul 8: "i want those to
auto review themselves and learn what was different")

For every shadow draft matched to a real office quote, compare LINE BY
LINE (grouped by service) and write the verdict nobody has to produce
by hand:

  * office ADDED work we never saw requested  -> upsell, not our miss
  * scope changed (ext-only vs in&out)        -> different job
  * same service, different price             -> calibration evidence

Each verdict lands in the review log (visible on the bid + decisions
feed) and each price gap feeds blob 'calibration_ledger'. When a
service shows a CONSISTENT gap (3+ quotes, median beyond ±10%), a
calibration suggestion is raised with the exact Settings key to change
— applying it stays a human click on the Settings page.
"""

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from store import _service_key

BASE = Path(__file__).parent


def _blob(key, default):
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob(key) or default
    except Exception:
        pass
    f = BASE / "data" / f"{key}.json"
    return json.loads(f.read_text()) if f.exists() else default


def _blob_save(key, val):
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob(key, val)
            return
    except Exception:
        pass
    (BASE / "data" / f"{key}.json").write_text(json.dumps(val))
    try:
        from cloudpush import push
        push(blobs={key: val})
    except Exception:
        pass


def _group(lines, name_k="name", price_k="price"):
    out = {}
    for li in lines or []:
        nm = (li.get(name_k) or "").lower()
        k = _service_key(nm)
        if not k:                     # office SKUs the matcher misses
            if "concrete" in nm or "patio" in nm:
                k = "patio"
            elif "curb" in nm or "sidewalk" in nm or "walkway" in nm:
                k = "sidewalk"
            elif "product" in nm and "moss" in nm:
                k = "moss"
            elif "furniture" in nm or "discount" in nm:
                continue              # zero-value housekeeping lines
            else:
                k = (nm or "other")[:20]
        out[k] = out.get(k, 0) + float(li.get(price_k) or 0)
    return {k: v for k, v in out.items() if v > 0}

# our engine's asked-services keys, to tell upsells from misses
_REQ_HINT = {"gutter": "gutter", "roof blow": "roof", "moss": "moss",
             "window": "window", "driveway": "driveway", "patio": "patio",
             "sidewalk": "sidewalk", "house wash": "house"}


def diff_one(row, our_lines, requested):
    ours = _group(our_lines)
    office = _group(row.get("office_lines") or [])
    req_blob = " ".join(requested or []).lower()
    parts, gaps = [], []
    for k in sorted(set(ours) | set(office)):
        o, f = ours.get(k), office.get(k)
        if o and not f:
            parts.append(f"we drafted {k} ${o:.0f}; office left it off")
        elif f and not o:
            hint = _REQ_HINT.get(k, k.split()[0])
            if hint in req_blob:
                parts.append(f"office quoted {k} ${f:.0f} — requested but "
                             f"MISSING from our draft (real miss)")
            else:
                parts.append(f"office ADDED {k} ${f:.0f} (upsell — never "
                             f"requested, not a pricing miss)")
        else:
            pct = 100 * (f - o) / o if o else 0
            if abs(pct) >= 8:
                parts.append(f"{k}: ours ${o:.0f} vs office ${f:.0f} "
                             f"({pct:+.0f}%)")
                gaps.append((k, o, f, pct))
            # near-equal lines are silent — that's the win case
    if not parts:
        parts.append("line-for-line match within 8% — nothing to learn")
    return "; ".join(parts), gaps


def run(verbose=True):
    sb = _blob("scoreboard", None)
    if not sb:
        return 0
    done = _blob("auto_reviews", {})
    ledger = _blob("calibration_ledger", {})

    # our draft lines: local records + cloud slim records
    recs = {}
    for p in sorted((BASE / "data" / "shadow_bids").glob("*.json")):
        recs[p.stem] = json.loads(p.read_text())
    try:
        import urllib.request
        from base64 import b64encode
        from cloudpush import _cfg
        url, pw = _cfg("DASHBOARD_URL"), _cfg("DASHBOARD_PASSWORD")
        if url and pw:
            req = urllib.request.Request(
                url.rstrip("/") + "/api/records",
                headers={"Authorization": "Basic "
                         + b64encode(f"office:{pw}".encode()).decode()})
            for r in json.load(urllib.request.urlopen(req, timeout=60)):
                recs.setdefault(r["stamp"], r)
    except Exception:
        pass

    from dashboard import save_review
    n = 0
    for row in sb.get("rows", []):
        stamp = row.get("stamp")
        if not row.get("office_quote") or stamp in done:
            continue
        rec = recs.get(stamp) or {}
        our_lines = ((rec.get("draft") or {}).get("bid") or {}) \
            .get("services") or []
        if not our_lines:
            continue
        summary, gaps = diff_one(row, our_lines, rec.get("services"))
        gap_pct = row.get("gap_pct")
        verdict = ("match" if not gaps and "upsell" not in summary
                   else "explained")
        save_review({"stamp": stamp, "action": "auto_review",
                     "customer": row.get("customer"),
                     "note": f"[vs office #{row['office_quote']}] {summary}"[:400]})
        done[stamp] = {"at": datetime.now(timezone.utc)
                       .isoformat(timespec="seconds"),
                       "summary": summary[:220], "verdict": verdict,
                       "gap_pct": gap_pct}
        for k, o, f, pct in gaps:
            ledger.setdefault(k, []).append(
                [datetime.now().date().isoformat(), round(o), round(f),
                 round(pct, 1)])
        n += 1
        if verbose:
            print(f"  📖 {stamp} {(row.get('customer') or '')[:28]}: {summary[:120]}")

    # calibration suggestions: consistent gaps become a concrete ask
    for k, samples in ledger.items():
        if len(samples) < 3 or ledger.get(f"_suggested_{k}"):
            continue
        med = statistics.median(s[3] for s in samples)
        if abs(med) < 10:
            continue
        save_review({"stamp": "", "action": "calibration_suggestion",
                     "customer": f"SERVICE: {k}",
                     "note": (f"Across {len(samples)} office quotes, their "
                              f"{k} price runs {med:+.0f}% vs ours. If real, "
                              f"adjust it on the Settings page (rates & "
                              f"multipliers) — one box, takes effect next "
                              f"bid.")})
        ledger[f"_suggested_{k}"] = datetime.now().date().isoformat()

    _blob_save("auto_reviews", done)
    _blob_save("calibration_ledger", ledger)
    if verbose:
        print(f"auto-reviewed {n} matched quote(s)")
    return n


if __name__ == "__main__":
    run()
