"""
MASTER BUTLER — SAME-PERSON DETECTOR (Dallon, Jul 13: Natallie Buxton
emailed from two addresses — natallie.buxton@comcast.net AND
@icloud.com — and showed as two customers. Teach the program to catch
these and combine them).

Builds a `customer_aliases` blob {alias_email: canonical_email}. The
dashboard resolves every email through it before grouping, so one
person = one card no matter how many addresses they use.

CONSERVATIVE by design — the whole point is to NEVER merge two
different people. Two emails unite ONLY on a strong signal:
  · same 10-digit phone, OR
  · same property address AND same last name (one household), OR
  · same email name-part AND same full name (natallie.buxton@X /
    natallie.buxton@Y, both 'Natallie Buxton').
Generic name-parts (info/office/…) are never used for the third rule.
"""

import collections
import re

_GENERIC_LOCAL = {"info", "office", "contact", "admin", "sales",
                  "hello", "team", "support", "service", "billing",
                  "mail", "email", "quote", "quotes", "customercare",
                  "noreply", "no-reply"}


def _digits(p):
    return "".join(c for c in (p or "") if c.isdigit())[-10:]


def _canon_addr(a):
    return re.sub(r"[^a-z0-9]", "", (a or "").lower())[:40]


def _name(frm):
    return re.sub(r"\s+", " ", (frm or "").split("<")[0].strip()).lower()


def _email(frm):
    m = re.search(r"<([^>]+)>", frm or "")
    return (m.group(1).lower() if m else "").strip()


def _local(e):
    return e.split("@")[0] if "@" in e else e


def build_aliases(save=True, verbose=False):
    import clouddb
    if not clouddb.available():
        return {}
    prof = {}
    for _s, r in clouddb.all_shadow():
        if r.get("merged_into") or r.get("spam_auto") \
                or r.get("tech_sender"):
            continue
        e = _email(r.get("from"))
        if not e:
            continue
        p = prof.setdefault(e, {"names": set(), "phones": set(),
                                "addrs": set(), "n": 0})
        p["n"] += 1
        nm = _name(r.get("from"))
        if nm and "@" not in nm and nm not in ("none", "none none"):
            p["names"].add(nm)
        ph = _digits(r.get("phone"))
        if len(ph) == 10:
            p["phones"].add(ph)
        ad = _canon_addr(r.get("address"))
        if ad:
            p["addrs"].add(ad)

    emails = list(prof)
    parent = {e: e for e in emails}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    by_phone = collections.defaultdict(list)
    by_addrname = collections.defaultdict(list)
    by_localname = collections.defaultdict(list)
    for e, p in prof.items():
        for ph in p["phones"]:
            by_phone[ph].append(e)
        lastnames = {n.split()[-1] for n in p["names"] if len(n.split()) >= 2}
        for ad in p["addrs"]:
            for ln in lastnames:
                by_addrname[(ad, ln)].append(e)
        loc = _local(e)
        if len(loc) > 4 and loc not in _GENERIC_LOCAL:
            for n in p["names"]:
                if len(n.split()) >= 2:          # full name only
                    by_localname[(loc, n)].append(e)

    # SAFE SIGNALS ONLY (Jul 13 dry-run: shared placeholder phones and
    # common addresses chained UNRELATED people together — Jim
    # Cavanaugh into 'Sundar Priya', Dallon into John Bowen). So:
    #  · email-name + full name (natallie.buxton@X / @Y) — the specific,
    #    self-contained signal that solves the real case and can't chain.
    #  · phone/address ONLY when EXACTLY two emails share it AND they
    #    also share a last name — a shared number on 3+ emails is an
    #    office/placeholder line, never an identity.
    def _tight(index):
        return {k: v for k, v in index.items() if len(set(v)) == 2}

    def _lastname(e):
        lns = {n.split()[-1] for n in prof[e]["names"]
               if len(n.split()) >= 2}
        return lns

    for grp in by_localname.values():
        for e in grp[1:]:
            union(grp[0], e)
    for index in (_tight(by_phone), _tight(by_addrname)):
        for grp in index.values():
            a, b = list(set(grp))[:2] if len(set(grp)) == 2 else (None, None)
            if a and b and (_lastname(a) & _lastname(b)):  # same surname
                union(a, b)

    comps = collections.defaultdict(list)
    for e in emails:
        comps[find(e)].append(e)
    aliases = {}
    for members in comps.values():
        if len(members) < 2:
            continue
        canonical = max(members, key=lambda e: (
            len(prof[e]["addrs"]) + len(prof[e]["phones"]), prof[e]["n"]))
        for e in members:
            if e != canonical:
                aliases[e] = canonical

    if verbose:
        by_canon = collections.defaultdict(list)
        for a, c in aliases.items():
            by_canon[c].append(a)
        for c, al in by_canon.items():
            names = sorted(prof[c]["names"])[:1]
            print(f"  {names[0] if names else c}: {c}  ← {', '.join(al)}")
        print(f"{len(aliases)} aliases across {len(by_canon)} people")
    if save:
        clouddb.put_blob("customer_aliases", aliases)
    return aliases


if __name__ == "__main__":
    build_aliases(save=False, verbose=True)
