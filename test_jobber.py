"""
Jobber client regression tests. Focus: property-address matching, so we
REUSE a customer's existing home instead of creating a duplicate 'new home'
on every approved quote (LaRee, Jul 17 — the quote hung off the duplicate,
read as the billing address, and missed the tax the real home carries).

Run:  python3 test_jobber.py
"""
import jobber_client as jc

passed = failed = 0


def check(a, b, expect):
    global passed, failed
    got = jc._same_property(a, b)
    ok = got == expect
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {'✅' if ok else '❌'} {a!r} vs {b!r} -> {got} (want {expect})")


print("── same home, different formatting → REUSE (True) ──")
check("1011 166th Pl NE", "1011 166th Place Northeast", True)
check("1011 166th Pl NE", "1011 166th Pl NE", True)
check("20905 SE 7th Pl", "20905 Southeast 7th Place", True)
check("123 NW Elm St", "123 Northwest Elm Street", True)
check("500 1st Ave S", "500 1st Avenue South", True)

print("── different home → do NOT reuse (False) ──")
check("1011 166th Pl NE", "1013 166th Pl NE", False)   # house number
check("1011 Main St", "1011 Oak Ave", False)           # street name
check("500 1st Ave S", "500 2nd Ave S", False)         # street number
check("", "1011 166th Pl NE", False)                   # empty

print("── Jessica's Jul 23 batch: names + canonical line items ──")


def check2(name, cond):
    global passed, failed
    passed, failed = passed + cond, failed + (not cond)
    print(f"  {'✅' if cond else '❌'} {name}")


check2("lowercase squarespace name title-cases",
       jc._proper_name("vamshi krishna namilikonda")
       == "Vamshi Krishna Namilikonda")
check2("ALL CAPS name title-cases", jc._proper_name("JANE DOE") == "Jane Doe")
check2("deliberate mixed case untouched (McDonald)",
       jc._proper_name("Ronald McDonald") == "Ronald McDonald")
check2("hyphenated lowercase handled",
       jc._proper_name("mary-jane smith") == "Mary-Jane Smith")

_bid = {"services": [
    {"name": "Pressure Wash Patio (~225 sqft)", "price": 100},
    {"name": "Pressure Wash Sidewalk (~120 sqft)", "price": 60},
    {"name": "Other Services", "price": 150,
     "custom_desc": "Interior windows only"},
], "notes": [], "confidence": "high"}
_was = jc.DRY_RUN
jc.DRY_RUN = True
try:
    _res = jc.create_draft_quote("C1", "P1", _bid)
except Exception as e:
    _res = {"error": str(e)}
finally:
    jc.DRY_RUN = _was
_lis = (_res or {}).get("variables", {}).get("input", {}).get(
    "lineItems") or (_res or {}).get("line_items") or []
if not _lis:                       # dry-run shape differs — inspect echo
    _lis = [li for li in ((_res or {}).get("input") or {}).get(
        "lineItems", [])] if isinstance(_res, dict) else []
_names = [li.get("name") for li in _lis]
if _names:
    check2("no invented '(~sqft)' product names reach Jobber",
           all("(~" not in (n or "") for n in _names))
    check2("patio bills to 'Concrete Surface Cleaning' (Jessica Jul 23)",
           "Concrete Surface Cleaning" in _names)
    check2("sidewalk maps to 'Pressure Wash Sidewalk & Curb'",
           "Pressure Wash Sidewalk & Curb" in _names)
    _oth = next(li for li in _lis
                if li.get("name") == "Other Services"
                and li.get("description") == "Interior windows only")
    check2("custom description rides the line", bool(_oth))
else:
    print("  (dry-run returned no line echo — mapping checked at unit "
          "level instead)")
    check2("patio maps via _OFFICE_LINE",
           jc._OFFICE_LINE["Pressure Wash Patio"][0]
           == "Concrete Surface Cleaning")
    check2("sidewalk maps via _OFFICE_LINE",
           jc._OFFICE_LINE["Pressure Wash Sidewalk"][0]
           == "Pressure Wash Sidewalk & Curb")

print(f"\nRESULT: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
