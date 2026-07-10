"""
MASTER BUTLER — NEVER QUOTE A RETURNING CUSTOMER LESS THAN THEY PAID

Martha's catch (Jul 10): Robert Lin's new bid came out $225 while his
Aug-2025 invoice for the same services was $255 — the engine's minimums
sat BELOW what he already pays happily. Rule: work off BOTH — take the
engine's number and the customer's last invoice per service, and use
whichever is higher. Prices only ever ratchet UP for a returning
customer, never down.

Same shape as minimums.py (tech field notes): applied in the pipeline
right after pricing, silently for small raises, 🚩 review-flagged when
a raise exceeds 50% (Dallon's rule for automatic raises).
"""

import json
import re
from pathlib import Path

from store import _service_key

BASE = Path(__file__).parent

REVIEW_RAISE = 1.5            # raise > 50% => needs human eyes

# lines that are billing companions, not services to floor
_SKIP = ("product", "discount", "tax", "fee", "trip")


def _slug(address):
    return re.sub(r"[^a-z0-9]+", "-", (address or "").lower()).strip("-")


def _history():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob("service_history") or {}
    except Exception:
        pass
    p = BASE / "data" / "service_history.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _client_key(name):
    return re.sub(r"\s+", " ", (name or "").lower()).strip()


def last_paid(address=None, client_name=None, hist=None):
    """{service_key: (price, date)} — what they paid on their most recent
    visit, per service. Property match first (the house is the job);
    client-name match as fallback. Within the latest date, the LARGEST
    line wins (a moss visit bills labor $55 + product $13 into the same
    bucket — the floor is the labor line)."""
    hist = hist if hist is not None else _history()
    buckets = None
    s = _slug(address)
    if s:
        for k, v in (hist.get("by_property") or {}).items():
            if k == s or (len(s) > 12 and (k.startswith(s) or s.startswith(k))):
                buckets = v
                break
    if buckets is None and client_name:
        buckets = (hist.get("by_client") or {}).get(_client_key(client_name))
    if not buckets:
        return {}
    out = {}
    for svc, entries in buckets.items():
        dated = sorted((e for e in entries if e and e[0]), reverse=True)
        if not dated:
            continue
        latest_day = dated[0][0]
        price = max(p for d, p in dated if d == latest_day)
        if price and price > 0:
            out[svc] = (float(price), latest_day)
    return out


def apply(services, address=None, client_name=None):
    """Mutates priced lines: any line UNDER the customer's last-paid
    price for the same service is raised to it. Returns note strings
    (empty = new customer or nothing raised)."""
    paid = last_paid(address, client_name)
    if not paid:
        return []
    notes = []
    for line in services:
        lname = (line.get("name") or "").lower()
        if any(w in lname for w in _SKIP):
            continue
        key = _service_key(line.get("name"))
        if not key or key not in paid:
            continue
        floor, when = paid[key]
        price = line.get("price") or 0
        if price >= floor:
            continue
        line["price"] = floor
        pct = 100 * (floor - price) / price if price else 100
        msg = (f"💲 RETURNING CUSTOMER: {line['name']} raised "
               f"${price:g} → ${floor:g} — their last invoice "
               f"({when}) charged ${floor:g}; never quote a returning "
               f"customer less than they paid (Martha's rule).")
        if price and floor > price * REVIEW_RAISE:
            msg += " 🚩 RAISE OVER 50% — REVIEW BEFORE SENDING"
        notes.append(msg)
    return notes
