"""
MASTER BUTLER — PER-CLIENT PRICE MINIMUMS FROM TECH FIELD NOTES

Techs leave live notes in Jobber ("Raise gutter price to at least $400
or do not service") — the office never sees the roof, the TECH does.
The dns_sweep collects those into client_minimums; this module applies
them to a draft automatically.

Dallon's rule (Jul 8): apply the raise silently, but if it raises a
line by MORE THAN 50%, flag the bid for review instead of trusting it
blindly.
"""

import json
from pathlib import Path

from dns_check import canon_addr

BASE = Path(__file__).parent

REVIEW_RAISE = 1.5          # raise > 50% => needs human eyes


def _list():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob("client_minimums") or []
    except Exception:
        pass
    p = BASE / "data" / "client_minimums.json"
    return json.loads(p.read_text()) if p.exists() else []


def find_client(email=None, phone=None, address=None):
    email = (email or "").lower().strip()
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    addr_c = canon_addr(address) if address else ""
    for e in _list():
        if email and email in e.get("emails", []):
            return e
        if digits and len(digits) == 10 and digits in e.get("phones", []):
            return e
        if addr_c and any(canon_addr(a) == addr_c
                          for a in e.get("addresses", [])):
            return e
    return None


def apply(services, email=None, phone=None, address=None):
    """Mutates the priced service lines per the client's tech notes.
    Returns a list of note strings to surface on the bid (empty = no
    matching client or nothing raised)."""
    client = find_client(email, phone, address)
    if not client:
        return []
    notes = []
    for rule in client.get("minimums", []):
        svc, floor = rule["service"], rule["min"]
        for line in services:
            lname = line.get("name", "").lower()
            if svc != "any" and svc not in lname:
                continue
            if line["price"] >= floor:
                notes.append(f"Tech-note minimum ${floor} for "
                             f"{svc if svc != 'any' else 'this job'} "
                             "already met.")
                continue
            old = line["price"]
            line["price"] = floor
            pct = 100 * (floor - old) / old if old else 100
            msg = (f"💲 TECH FIELD NOTE applied: {line['name']} raised "
                   f"${old} → ${floor} (+{pct:.0f}%) — "
                   f"“{rule['note'][:90]}”")
            if floor > old * REVIEW_RAISE:
                msg += " 🚩 RAISE OVER 50% — REVIEW BEFORE SENDING"
            notes.append(msg)
            break                          # one line per rule
    return notes
