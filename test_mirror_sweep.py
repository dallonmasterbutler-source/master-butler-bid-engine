"""
MIRROR-SWEEP VERDICT TESTS (Jul 21 night sweep) — the auto-file logic is
the one place a bug can HIDE A REAL CUSTOMER, so its evidence rules get
locked here as pure-function cases:
  · a stale sender-keyed "done" (observed before their newest email)
    must NOT file
  · an approved quote with no quote date must NOT count any old job as
    proof of scheduling
  · a request sitting in Spam is UNSEEN, not handled
  · records with fallback Message-IDs (gmail-<gid>) are unprovable
Run: python3 test_mirror_sweep.py
"""

import sys

passed = failed = 0


def check(label, cond):
    global passed, failed
    ok = bool(cond)
    passed += ok
    failed += (not ok)
    print(f"  {'✅' if ok else '❌'} {label}")


import mirror_sweep as ms

# a record "received" stamp of 10:00 today, local
REC = {"folder": "INBOX", "message_id": "<abc@mail.example>",
       "received": "20260721-100000"}

import dashboard as _db
REC_UTC = _db._stamp_utc(REC["received"])

print("── Gmail verdict: freshness of the sender-keyed 'done' ──")
done_stale = {"x@y.com": {"state": "done", "at": "2000-01-01T00:00:00+00:00"}}
done_fresh = {"x@y.com": {"state": "done", "at": "2099-01-01T00:00:00+00:00"}}
g, why = ms._gmail_verdict("x@y.com", [REC], done_stale, {})
check("stale 'done' (before their newest email) does NOT file", not g)
g, why = ms._gmail_verdict("x@y.com", [REC], done_fresh, {})
check("fresh 'done' (after their newest email) DOES file", g)

print("── Gmail verdict: legacy 'done' with no timestamp ──")
g, _ = ms._gmail_verdict("x@y.com", [REC],
                         {"x@y.com": {"state": "done"}}, {})
check("'done' with no 'at' cannot vouch alone", not g)

print("── Gmail verdict: Spam is unseen, not handled ──")
g, why = ms._gmail_verdict("x@y.com",
                           [{"folder": "[Gmail]/Spam",
                             "message_id": "<s@m>",
                             "received": "20260721-100000"}], {}, {})
check("spam-folder request stays (not vacuously done)",
      not g and "Spam" in why)

print("── Gmail verdict: fallback Message-IDs are unprovable ──")
g, why = ms._gmail_verdict(
    "x@y.com", [{"folder": "INBOX", "message_id": "gmail-18c2",
                 "received": "20000101-100000"}],
    {}, {"at": "2099-01-01T00:00:00+00:00", "mids": []})
check("gmail-<gid> record never files via the mids snapshot", not g)

print("── Gmail verdict: the mids path still files real archives ──")
g, why = ms._gmail_verdict(
    "x@y.com", [REC], {},
    {"at": "2099-01-01T00:00:00+00:00", "mids": ["<other@mid>"]})
check("old record, real mid, gone from a newer snapshot → filed",
      g and "archived" in why)
g, _ = ms._gmail_verdict(
    "x@y.com", [REC], {},
    {"at": "2099-01-01T00:00:00+00:00", "mids": [REC["message_id"]]})
check("...but NOT while the mid still sits in the inbox", not g)

print("── Jobber verdict: approved quote with no quote date ──")
import jobber_client as jc
_orig = jc.client_jobs
jc.client_jobs = lambda email: [{"jobNumber": 7,
                                 "createdAt": "2024-01-01T00:00:00Z"}]
try:
    d, why, sure = ms._jobber_verdict(
        "x@y.com", {"status": "approved", "number": 12}, sleep_s=0)
    check("no quote date → an old job is NOT proof (left alone)",
          not d and "unknown" in why)
    d, why, sure = ms._jobber_verdict(
        "x@y.com", {"status": "approved", "number": 12,
                    "created": "2023-12-01"}, sleep_s=0)
    check("dated quote + later job → filed as scheduled", d)
finally:
    jc.client_jobs = _orig

print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
