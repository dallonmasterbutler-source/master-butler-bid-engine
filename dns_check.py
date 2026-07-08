"""
MASTER BUTLER — DO-NOT-SERVICE CHECK (intake guard)

Every new request is checked against the DNS list from dns_sweep.
Matching is deliberately WIDER than email — these folks have tried new
email addresses before (Dallon) — so we also match:

  * phone digits (last 10)
  * property address, canonicalized ('SE 7th Pl' == 'Southeast 7th Place')
  * exact full name (flagged as 'verify' — names can collide)

Returns {'name','why','matched_by'} or None.
"""

import json
import re
from pathlib import Path

BASE = Path(__file__).parent

_ABBR = {"se": "southeast", "sw": "southwest", "ne": "northeast",
         "nw": "northwest", "n": "north", "s": "south", "e": "east",
         "w": "west", "pl": "place", "st": "street", "ave": "avenue",
         "av": "avenue", "rd": "road", "dr": "drive", "ln": "lane",
         "ct": "court", "cir": "circle", "blvd": "boulevard",
         "hwy": "highway", "pkwy": "parkway", "ter": "terrace"}


def canon_addr(s):
    toks = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
    out = []
    for t in toks:
        if t in ("wa", "washington", "usa") or (t.isdigit() and len(t) == 5
                                                and out):
            continue
        out.append(_ABBR.get(t, t))
    return "-".join(out)[:80]


def _list():
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob("dns_list") or []
    except Exception:
        pass
    p = BASE / "data" / "dns_list.json"
    return json.loads(p.read_text()) if p.exists() else []


def _norm_name(s):
    s = re.sub(r"[^a-z ]", "", (s or "").lower()).strip()
    return re.sub(r"\s+", " ", s)


def check(email=None, phone=None, address=None, name=None):
    entries = _list()
    if not entries:
        return None
    email = (email or "").lower().strip()
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    addr_c = canon_addr(address) if address else ""
    name_n = _norm_name(name)
    for e in entries:
        if email and email in e.get("emails", []):
            return {"name": e["name"], "why": e["why"], "matched_by": "email"}
        if digits and len(digits) == 10 and digits in e.get("phones", []):
            return {"name": e["name"], "why": e["why"], "matched_by": "phone"}
        if addr_c and any(canon_addr(a) == addr_c
                          for a in e.get("addresses", [])):
            return {"name": e["name"], "why": e["why"],
                    "matched_by": "property address (may be a NEW email — "
                                  "same house)"}
    for e in entries:                       # weakest signal last
        en = _norm_name(e["name"].lstrip("*x "))
        if name_n and len(name_n) > 8 and en == name_n:
            return {"name": e["name"], "why": e["why"],
                    "matched_by": "name (VERIFY — names can collide)"}
    return None
