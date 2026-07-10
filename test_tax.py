"""Windows are tax-exempt (Dallon's ruling, Jul 10 pm): the ADDRESS is
always charged its city rate; window LINES are individually non-taxable.
  - mixed quote  → window lines taxable:false, city rate taxes the rest
  - windows-only → SAME: city rate attached ($0 tax falls out naturally,
    and anything the office adds later is taxed) — never a whole-quote
    'Tax Exempt' rate (that erased tax on added services)
  - no windows   → unchanged (city rate, everything taxable)
Run: python3 test_tax.py
"""
import jobber_client as jc


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

    # standalone in&out → CITY rate stays on the address, window line
    # non-taxable ($0 tax falls out; add-ons later get taxed correctly)
    a = _build([_tax("Windows In & Out", 300)])
    check(a.get("taxRateId") == "CITY", "in&out only: city rate on address")
    check(all(x.get("taxable") is False for x in a["lineItems"]),
          "in&out only: window line non-taxable")

    # standalone exterior → same
    a = _build([_tax("Window Cleaning (Exterior Only)", 250)])
    check(a.get("taxRateId") == "CITY", "exterior only: city rate on address")

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
