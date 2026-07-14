"""
MASTER BUTLER — TIMED DISCOUNTS (Dallon + LaRee, Jul 13: "15% for the
2nd week of August… flexible for slow seasons, low incoming bids").

A discount runs only between its start and end dates. Every NEW bid
priced inside the window gets the discount as ITS OWN LABELED LINE —
the true price stays visible above it, so the learning loop still
sees real numbers. Outside the window: full price, automatically.

Rules (Dallon approved the mockup Jul 13):
  · only ONE timed discount applies per bid — the LARGEST wins,
    they never stack;
  · the discount base is the service lines (product/companion lines
    like the $14.50 moss canister are excluded);
  · office edits require a name tag and land in the settings_change
    log like every other pricing knob.

Storage: blob `timed_discounts` = [{name, pct, start, end, by, at}].
"""

import datetime
import json
from pathlib import Path

BASE = Path(__file__).parent

# billing companions never discounted
_SKIP = ("product", "discount", "tax", "fee", "trip")


def _load():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob("timed_discounts") or []
    except Exception:
        pass
    p = BASE / "data" / "timed_discounts.json"
    try:
        return json.loads(p.read_text()) if p.exists() else []
    except Exception:
        return []


def active(when=None, discounts=None):
    """The single discount in force on `when` — largest pct wins."""
    d = when or datetime.date.today()
    if isinstance(d, datetime.datetime):
        d = d.date()
    best = None
    for t in (discounts if discounts is not None else _load()):
        try:
            s = datetime.date.fromisoformat(t.get("start") or "")
            e = datetime.date.fromisoformat(t.get("end") or "")
            pct = float(t.get("pct") or 0)
        except (ValueError, TypeError):
            continue
        if s <= d <= e and pct > 0 and \
                (best is None or pct > float(best["pct"])):
            best = t
    return best


def apply(results, when=None):
    """Append the active discount line to a fresh bid. Mutates results,
    returns note strings (empty when no discount is running)."""
    t = active(when)
    if not t or not results:
        return []
    if any(s.get("timed_discount") for s in results):
        return []                       # already carries one
    base = sum((s.get("price") or 0) for s in results
               if (s.get("price") or 0) > 0
               and not any(w in (s.get("name") or "").lower()
                           for w in _SKIP))
    if base <= 0:
        return []
    pct = float(t["pct"])
    amt = round(base * pct / 100, 2)
    results.append({"name": f"{t['name']} ({pct:g}%)", "price": -amt,
                    "timed_discount": True})
    return [f"⏳ Timed discount applied: {t['name']} — {pct:g}% off "
            f"(runs {t.get('start')} to {t.get('end')}; set by "
            f"{t.get('by') or 'office'}). True prices stay on the "
            "lines above; the discount is its own line."]
