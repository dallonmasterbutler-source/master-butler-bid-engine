"""
MASTER BUTLER — THE AMENDMENT ENGINE (v1, Jul-14 night batch)

Dallon's spec: "if someone asks for another quote… start from THEIR
lines, apply the delta." 29 scope-change messages a month (the Jul-14
mining), and every one used to be re-priced from scratch — which is how
Gloria's 'just my one skylight' became a fresh windows quote.

propose(rec) — when a record has an open quote WITH lines and the
newest message carries add/remove/instead/only language, build the
proposed revision:

  · START from the customer's own quote lines (their prices, their
    scope — the office already blessed those numbers)
  · REMOVE lines they're dropping ("remove the gutter cleaning",
    "we already had that done", "don't need X")
  · KEEP-ONLY when they say "just/only the X" (Gloria's shape)
  · ADD lines they're requesting, priced from their own last-paid
    history when we have it, else marked for the office
  · never invents scope; never sends anything — the proposal lands as
    the draft + a loud note, the office reviews like any draft

Pure functions + real-case tests in __main__.
"""

import re

# line-name ↔ spoken-service matching
_SPOKEN = {
    "gutter": ("gutter",),
    "roof blow": ("roof blow", "blow off", "roof clean"),
    "moss treat": ("moss treatment", "treat the moss", "moss treat"),
    "moss remov": ("moss removal", "remove the moss", "moss remov"),
    "window": ("window",),
    "skylight": ("skylight",),
    "dryer": ("dryer vent", "dryer"),
    "driveway": ("driveway", "cement court", "concrete"),
    "patio": ("patio",),
    "sidewalk": ("sidewalk", "walkway"),
    "pressure": ("pressure wash", "power wash", "pw "),
    "house wash": ("house wash", "siding"),
    "light": ("light",),
}


def _cats_in(text):
    t = (text or "").lower()
    return {c for c, words in _SPOKEN.items()
            if any(w in t for w in words)}


def _line_cat(name):
    n = (name or "").lower()
    for c, words in _SPOKEN.items():
        if any(w in n for w in words) or c in n:
            return c
    return None


REMOVE_RX = re.compile(
    r"(?:remove|take (?:off|out)|drop|skip|without|don'?t need|do not "
    r"need|no longer need|already (?:had|did|done)|cancel)(?P<tail>"
    r"[^.!?\n]{0,80})", re.I)
ONLY_RX = re.compile(
    r"(?:just|only)(?: want| need| looking for| interested in| do)?"
    r"(?P<tail>[^.!?\n]{0,80})", re.I)
# NB: bare "include(s)" is DESCRIPTIVE ("the quote includes gutter
# cleaning") — only imperative shapes count as an add-request
ADD_RX = re.compile(
    r"(?:\badd\b|also (?:do|want|need|include)|please include|"
    r"can you (?:also )?(?:do|include)|plus|as well as)"
    r"(?P<tail>[^.!?\n]{0,80})", re.I)
INSTEAD_RX = re.compile(
    r"(?P<new>[^.!?\n]{3,60})\s+instead of\s+(?P<old>[^.!?\n]{3,60})",
    re.I)


def parse_deltas(message):
    """message → {'remove': set, 'add': set, 'only': set} of categories."""
    m = (message or "")
    # only the customer's newest words, not quoted tails
    m = re.split(r"On (Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|"
                 r"Jun|Jul|Aug|Sep|Oct|Nov|Dec)", m)[0]
    out = {"remove": set(), "add": set(), "only": set()}
    for mt in INSTEAD_RX.finditer(m):
        out["add"] |= _cats_in(mt.group("new"))
        out["remove"] |= _cats_in(mt.group("old"))
    for mt in REMOVE_RX.finditer(m):
        out["remove"] |= _cats_in(mt.group("tail"))
    # passive / pronoun shapes: "the quote includes gutter cleaning …
    # can THIS be removed" — the service sits BEFORE the verb, often a
    # sentence earlier. A sentence with a remove-verb but no service
    # word borrows the categories of the sentence before it.
    sents = re.split(r"(?<=[.!?])\s+", m)
    for i, s in enumerate(sents):
        if re.search(r"\bremoved?\b|\btaken? (off|out)\b", s, re.I):
            cats = _cats_in(s) or (_cats_in(sents[i - 1]) if i else set())
            out["remove"] |= cats
    for mt in ADD_RX.finditer(m):
        out["add"] |= _cats_in(mt.group("tail"))
    for mt in ONLY_RX.finditer(m):
        out["only"] |= _cats_in(mt.group("tail"))
    out["remove"] -= out["add"]          # "instead" handled above
    return out


def propose(rec, last_paid=None):
    """Record → {'lines': […], 'total': $, 'note': str} or None.
    last_paid: optional {category-ish name: price} for pricing adds."""
    oq = rec.get("open_quote_ctx") or {}
    lines = [l for l in (oq.get("lines") or [])
             if l.get("name") and l.get("price") is not None]
    if not lines or not oq.get("number"):
        return None
    msg = rec.get("newest_message") or ""
    d = parse_deltas(msg)
    if not (d["remove"] or d["add"] or d["only"]):
        return None

    kept, dropped = [], []
    for l in lines:
        cat = _line_cat(l["name"])
        low = (l.get("name") or "").lower()
        companion = any(w in low for w in ("product", "adjustment",
                                           "tax", "fee"))
        if d["only"]:
            (kept if (cat in d["only"] or companion and kept)
             else dropped).append(l)
        elif cat and cat in d["remove"]:
            dropped.append(l)
        else:
            kept.append(l)
    added = []
    have = {_line_cat(l["name"]) for l in kept}
    for cat in d["add"] - have:
        price = None
        for k, v in (last_paid or {}).items():
            if cat in k or k in cat:
                price = v[0] if isinstance(v, (list, tuple)) else v
                break
        added.append({"name": f"{cat.title()} (amendment — office "
                              f"prices)" if price is None else
                              f"{cat.title()} (from their history)",
                      "price": price or 0})
    if not (dropped or added):
        return None
    new_lines = kept + added
    total = round(sum(l.get("price") or 0 for l in new_lines), 2)
    bits = []
    if dropped:
        bits.append("removed " + ", ".join(l["name"] for l in dropped))
    if added:
        bits.append("added " + ", ".join(l["name"] for l in added))
    note = (f"🔁 AMENDMENT of quote #{oq['number']}: started from THEIR "
            f"quote's own lines ({len(lines)} → {len(new_lines)}); "
            + "; ".join(bits) +
            f". New total ${total:,.2f}. Office reviews before anything "
            f"goes out — an added line at $0 means PRICE IT.")
    return {"lines": new_lines, "total": total, "note": note,
            "quote": oq.get("number")}


if __name__ == "__main__":
    ok = 0
    # Gloria: keep-only skylight from a converted quote's lines
    g = propose({"open_quote_ctx": {"number": "34108", "lines": [
        {"name": "Roof Blow Off for Installed Gutter Guards",
         "price": 250.0},
        {"name": "Skylight Cleaning", "price": 10.0}]},
        "newest_message": "I just need my one skylight cleaned this "
                          "time, please."})
    ok += bool(g and len(g["lines"]) == 1
               and g["lines"][0]["name"].startswith("Skylight"))
    print("Gloria keep-only:", g and [l["name"] for l in g["lines"]],
          f"${g and g['total']}")
    # Daniela: remove gutter, keep moss
    d2 = propose({"open_quote_ctx": {"number": "36000", "lines": [
        {"name": "Gutter Cleaning", "price": 278.0},
        {"name": "Moss Treatment", "price": 95.0},
        {"name": "Moss Treatment Product", "price": 14.5}]},
        "newest_message": "The quote includes gutter cleaning ($278) — "
                          "we actually had this service done recently. "
                          "Can this be removed from the quote?"})
    ok += bool(d2 and len(d2["lines"]) == 2 and d2["total"] == 109.5)
    print("Daniela remove-gutter:", d2 and [l["name"] for l in
                                            d2["lines"]],
          f"${d2 and d2['total']}")
    # Jerry: add dryer vent + driveway instead of wash
    j = propose({"open_quote_ctx": {"number": "36449", "lines": [
        {"name": "House Wash", "price": 400.0}]},
        "newest_message": "Please add to quote: Clean dryer vent. "
                          "Power wash cement court instead of wash."},
        last_paid={"dryer": (150, "2025-09-01")})
    ok += bool(j and any("Dryer" in l["name"] for l in j["lines"]))
    print("Jerry add+instead:", j and [(l["name"], l["price"])
                                       for l in j["lines"]])
    # non-amendment stays quiet
    q = propose({"open_quote_ctx": {"number": "1", "lines": [
        {"name": "Gutter Cleaning", "price": 200}]},
        "newest_message": "Thanks, July 20th works for us!"})
    ok += q is None
    print("quiet on non-amendment:", q is None)
    print(f"{ok}/4")
