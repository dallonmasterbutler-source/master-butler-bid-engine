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

print(f"\nRESULT: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
