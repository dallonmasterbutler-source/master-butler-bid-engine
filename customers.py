"""
MASTER BUTLER — CUSTOMER PROFILES (Dallon, Jul 9: "it should be just
like doing a quote, matching addresses and names to profiles. if there
are multiple people per address, combine them but make them searchable
so both come up in that profile.")

The identity graph, address-first:
  · a profile is keyed by the property (address slug) when we know it,
    else by the person's email
  · every name/email/phone seen at that property attaches to the SAME
    profile — husband and wife both find it by search
  · messages come from three shelves: the year-long history port
    (hist:<email> blobs), the live message log, and bid records

Pure assembly — no HTTP, no HTML. The dashboard renders it.
"""

import json
import re
from pathlib import Path

BASE = Path(__file__).parent


def slug(address):
    return re.sub(r"[^a-z0-9]+", "-", (address or "").lower()).strip("-")[:60]


def _blob(key, default):
    try:
        import clouddb
        if clouddb.available():
            return clouddb.get_blob(key) or default
    except Exception:
        pass
    f = BASE / "data" / f"{key}.json"
    try:
        return json.loads(f.read_text()) if f.exists() else default
    except Exception:
        return default


_PORT_CACHE = None


def _port():
    """Mac fallback: the raw history_port.json (cloud uses blobs)."""
    global _PORT_CACHE
    if _PORT_CACHE is None:
        f = BASE / "data" / "history_port.json"
        try:
            _PORT_CACHE = json.loads(f.read_text()) if f.exists() else {}
        except Exception:
            _PORT_CACHE = {}
    return _PORT_CACHE


def hist_index():
    """{email: {'name','count','last'}} — light roster info only."""
    try:
        import clouddb
        if clouddb.available():
            return _blob("hist_index", {})
    except Exception:
        pass
    return {e: {"name": v.get("name"), "count": len(v.get("msgs", [])),
                "last": (v["msgs"][-1]["at"] if v.get("msgs") else "")}
            for e, v in _port().items()}


def hist_msgs(email):
    """The ported year of conversation for one email, oldest first."""
    try:
        import clouddb
        if clouddb.available():
            ent = _blob(f"hist:{(email or '').lower()}", None)
            return (ent or {}).get("msgs", [])
    except Exception:
        pass
    return _port().get((email or "").lower(), {}).get("msgs", [])


def _add_person(p, name, email):
    if email:
        e = email.lower()
        if e not in p["emails"]:
            p["emails"].append(e)
    if name:
        n = name.strip()[:60]
        low = n.lower()
        if (n and low not in ("none", "none none")
                and all(low != x.lower() for x in p["names"])):
            p["names"].append(n)


def build_profiles(bids, threads):
    """(profiles, email->key). bids = dashboard.load_bids() rows the
    caller already filtered (no spam/robots/techs/merged); threads =
    msglog.threads() list."""
    profiles, by_email = {}, {}

    def get(key):
        if key not in profiles:
            profiles[key] = {"key": key, "addr": None, "names": [],
                             "emails": [], "phones": [], "stamps": [],
                             "last": "", "jobber_url": None}
        return profiles[key]

    for b in bids:
        m = re.search(r"<([^>]+)>", b.get("from") or "")
        email = (m.group(1).lower() if m else None)
        addr = b.get("address")
        name = (b.get("from") or "").split("<")[0]
        if b.get("lead") or (email and "copycall" in email):
            # a VOICEMAIL wears the CALLER's identity, never copycall's
            # (else every caller merges into one 'person')
            email = None
            cid = b.get("caller_id") or {}
            name = cid.get("name") or ""
            digits = re.sub(r"\D", "", b.get("phone") or "")
            key = ((slug(addr) if addr else None)
                   or (f"tel:{digits}" if digits else None))
        else:
            key = (by_email.get(email)      # someone at a known property
                   or (slug(addr) if addr else None) or email)
        if not key:
            continue
        p = get(key)
        if addr and not p["addr"]:
            p["addr"] = addr
        _add_person(p, name, email)
        if b.get("phone") and b["phone"] not in p["phones"]:
            p["phones"].append(b["phone"])
        if b.get("jobber_client_url"):
            p["jobber_url"] = b["jobber_client_url"]
        p["stamps"].append(b["stamp"])
        if email:
            by_email[email] = key

    for addr_email, name, msgs in threads:
        e = (addr_email or "").lower()
        key = by_email.get(e) or e
        p = get(key)
        _add_person(p, name if name != addr_email else "", e)
        by_email.setdefault(e, key)
        if msgs:
            p["last"] = max(p["last"], msgs[-1]["at"])

    for e, meta in hist_index().items():
        key = by_email.get(e) or e
        p = get(key)
        _add_person(p, meta.get("name"), e)
        by_email.setdefault(e, key)
        p["last"] = max(p["last"], meta.get("last") or "")

    # clients the office created DIRECTLY in Jobber (hourly blob) —
    # they get a file here even before any email/bid exists
    for c in _blob("jobber_new_clients", []):
        e = (c.get("email") or "").lower() or None
        key = ((by_email.get(e) if e else None)
               or (slug(c["address"]) if c.get("address") else None)
               or e or f"jc:{c.get('id')}")
        p = get(key)
        _add_person(p, c.get("name"), e)
        if e:
            by_email.setdefault(e, key)
        if c.get("address") and not p["addr"]:
            p["addr"] = c["address"]
        if c.get("phone") and c["phone"] not in p["phones"]:
            p["phones"].append(c["phone"])
        if c.get("url") and not p["jobber_url"]:
            p["jobber_url"] = c["url"]
        p["last"] = max(p["last"], (c.get("created") or "") + "T00:00:00")

    return profiles, by_email


def matches(p, q):
    """Search hits on ANY handle a person has (Dallon, Jul 9 pm: 'make
    them also searchable by address and phone number'): names, emails,
    the address, and phones — phone matching ignores formatting, so
    425-422-8824 finds 4254228824."""
    q = (q or "").strip().lower()
    if not q:
        return True
    hay = " ".join(p["names"] + p["emails"] + p["phones"]
                   + [p.get("addr") or ""]).lower()
    if all(w in hay for w in q.split()):
        return True
    qd = re.sub(r"\D", "", q)
    return (len(qd) >= 7
            and qd in re.sub(r"\D", "", " ".join(p["phones"])))
