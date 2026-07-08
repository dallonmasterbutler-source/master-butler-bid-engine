"""
MASTER BUTLER — PRICE PROMISES

When the office honors old pricing, the discount line often contains a
written commitment: "price for 2027 will be $X". The reconciler sweep
extracts those; THIS module makes sure the promise is kept — when that
customer comes back, the drafted quote says so before the office ever
compares numbers.

Data source: data/discount_reconciliation.json (reconciler sweep output).
"""

import json
import re
from pathlib import Path

BASE = Path(__file__).parent
RECON = BASE / "data" / "discount_reconciliation.json"


def _norm(name):
    """Loose name key: lowercase letters only, order kept."""
    return re.sub(r"[^a-z ]", "", (name or "").lower()).split()


def promises_for(customer_name, _records=None):
    """Any recorded next-year price promises for this customer.
    Match = every word of one name appears in the other (handles
    'Carol & Michael Ross' vs 'Carol Ross'). _records = test injection."""
    if _records is None:
        if not RECON.exists():
            return []
        _records = json.loads(RECON.read_text())
    if not customer_name:
        return []
    want = _norm(customer_name)
    if not want:
        return []
    hits = []
    for f in _records:
        if not f.get("next_year_price"):
            continue
        have = _norm(f.get("client"))
        if all(w in have for w in want) or all(w in want for w in have):
            texts = [d["text"] for d in f["discounts"] if d.get("next_year_price")]
            hits.append({
                "invoice": f["invoice"],
                "date": f["date"][:10],
                "promised_price": f["next_year_price"],
                "text": texts[0] if texts else "",
            })
    return hits


def promise_notes(customer_name):
    """Office notes for any promises owed to this customer."""
    notes = []
    for p in promises_for(customer_name):
        notes.append(
            f"PRICE PROMISE on file: ${p['promised_price']:.0f} was promised "
            f"on invoice #{p['invoice']} ({p['date']}) — "
            f"\"{p['text'][:90]}\". Honor it or beat it.")
    return notes
