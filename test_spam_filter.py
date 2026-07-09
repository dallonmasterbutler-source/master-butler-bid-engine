"""
Locks the spam filter to the 67 hand-labeled real emails from the
Jul 8, 2026 study of the whole account (data/spam_fixtures.json).

THE CONTRACT:
 · zero false positives — no 'customer' or 'not_spam' email may EVER
   be called spam (hiding a customer is the unforgivable failure)
 · every 'spam' email is caught (all 14 were verified by hand; two of
   them Tom/Dallon called junk themselves)
"""

import json
from pathlib import Path

import spam_filter

FIX = Path(__file__).parent / "data" / "spam_fixtures.json"


def run():
    fixtures = json.loads(FIX.read_text())
    passed = failed = 0
    for f in fixtures:
        is_spam, why = spam_filter.looks_spam(
            f["from"], f["subject"], f["body"],
            list_unsub=f.get("list_unsub", False))
        want = f["label"] == "spam"
        if is_spam == want:
            passed += 1
        else:
            failed += 1
            kind = ("FALSE POSITIVE (called a real email spam!)"
                    if is_spam else "missed spam")
            print(f"FAIL #{f['idx']:>3} {kind}")
            print(f"     from: {f['from'][:60]}")
            print(f"     subj: {f['subject'][:70]}")
            if why:
                print(f"     why:  {why[:100]}")
    print("=" * 50)
    print(f"RESULT: {passed} passed, {failed} failed "
          f"({len(fixtures)} labeled emails)")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
