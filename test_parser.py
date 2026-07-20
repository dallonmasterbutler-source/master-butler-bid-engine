"""
Parser regression tests — locks the service-detection fixes so they can't
silently break. Focus: in/out vs exterior windows (Martha, Jul 15: "the
system keeps defaulting to exterior windows when people request in/out").
In & out is priced ~2x exterior, so a miss here is real money.

Run:  python3 test_parser.py
"""
import email_parser
# part of the office-fix smoke test

passed = failed = 0


def check(text, must_have=(), must_not=()):
    global passed, failed
    got = email_parser.find_services(text)
    ok = all(s in got for s in must_have) and all(s not in got
                                                  for s in must_not)
    tag = "✅" if ok else "❌"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {tag} {text[:52]!r:56} -> {got}")


print("── in & out windows: explicit phrasings ──")
check("windows in & out", must_have=["windows_in_out"],
      must_not=["windows_exterior"])
check("windows in and out please", must_have=["windows_in_out"])
check("interior/exterior window cleaning", must_have=["windows_in_out"],
      must_not=["windows_exterior"])
check("int/ext windows", must_have=["windows_in_out"])
check("window cleaning in & out", must_have=["windows_in_out"])

print("── in & out BY MEANING (Martha's real-world phrasings) ──")
check("indoor/outdoor window cleaning", must_have=["windows_in_out"],
      must_not=["windows_exterior"])
check("window cleaning in my house inside and out",
      must_have=["windows_in_out"], must_not=["windows_exterior"])
check("windows inside & out", must_have=["windows_in_out"])
check("clean the windows, both sides", must_have=["windows_in_out"])

print("── exterior-only stays exterior (don't over-upgrade) ──")
check("exterior window cleaning only", must_have=["windows_exterior"],
      must_not=["windows_in_out"])
check("just the outside of the windows", must_not=["windows_in_out"])

print("── in & out supersedes exterior when both trip ──")
# 'interior/exterior' trips both keys; customer wants both sides
check("interior and exterior windows", must_have=["windows_in_out"],
      must_not=["windows_exterior"])

print("── unrelated 'in'/'out' words don't create in & out ──")
# 'out' in 'out front' near 'window' must NOT force in&out on its own
check("clean the exterior windows out front",
      must_not=["windows_in_out"])

print(f"\nRESULT: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
