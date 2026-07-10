"""Windows are tax-exempt (Dallon, Jul 10). Lock the quote-tax behavior:
  - mixed quote  → window lines taxable:false, city rate on the rest
  - windows-only → the office's 'Tax Exempt' rate, lines shown at 0%
  - no windows   → unchanged (city rate, everything taxable)
Run: python3 test_tax.py
"""
import jobber_client as jc

_EXEMPT = "Z2lkOi8vSm9iYmVyL1RheFJhdGUvMzIyODc="   # office 'Tax Exempt' id


def _build(services, address="123 Main St, Monroe, WA 98272"):
    cap = {}
    orig_post, orig_match = jc._post, jc.match_tax_rate
    jc._post = lambda q, v, l: cap.setdefault("v", v) or {"quoteCreate": {}}
    jc.match_tax_rate = lambda *a, **k: ({"id": "CITY", "label": "Monroe (9.4%)"}, None)
    try:
        jc.create_draft_quote("C", "P",
                              {"services": services, "notes": [], "confidence": 90},
                              prop_info={}, address=address)
    finally:
        jc._post, jc.match_tax_rate = orig_post, orig_match
    return cap["v"]["attributes"]


def _tax(name, price):
    return {"name": name, "price": price}


def run():
    ok = 0
    fails = []

    def check(cond, msg):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fails.append(msg)

    # mixed: windows excluded, gutters taxed, city rate
    a = _build([_tax("Windows In & Out", 300), _tax("Gutter Cleaning", 180)])
    li = {x["name"]: x for x in a["lineItems"]}
    check(li["Window Cleaning In & Out"]["taxable"] is False,
          "mixed: window line should be taxable:false")
    check(li["Gutter Cleaning - Composition"]["taxable"] is True,
          "mixed: gutter line should be taxable:true")
    check(a.get("taxRateId") == "CITY", "mixed: city rate should attach")

    # standalone in&out → Tax Exempt rate
    a = _build([_tax("Windows In & Out", 300)])
    check(a.get("taxRateId") == _EXEMPT, "in&out only: Tax Exempt rate")
    check(all(x.get("taxable") for x in a["lineItems"]),
          "in&out only: lines taxable:true so 0% code shows")

    # standalone exterior → Tax Exempt rate
    a = _build([_tax("Window Cleaning (Exterior Only)", 250)])
    check(a.get("taxRateId") == _EXEMPT, "exterior only: Tax Exempt rate")

    # no windows → unchanged city rate, all taxable
    a = _build([_tax("Gutter Cleaning", 180), _tax("Moss Treatment", 150)])
    check(a.get("taxRateId") == "CITY", "no windows: city rate")
    check(all(x.get("taxable") in (True, None) for x in a["lineItems"]),
          "no windows: nothing marked non-taxable")

    print(f"RESULT: {ok} passed, {len(fails)} failed")
    for f in fails:
        print("  FAIL:", f)
    return not fails


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
