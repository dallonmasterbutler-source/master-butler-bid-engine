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
    # ── ROBOT SENDERS (Jul 21): automated notices that were cluttering
    #    the queue must be filed spam; the real form pipe must survive ──
    robot_cases = [
        # (from, subject, body, want_spam)
        ("no-reply@squarespace.com", "Update to Your Squarespace Website",
         "Your site had activity.", True),
        ("no-reply@notifications.nicejob.co",
         "You've received a new review on Google", "5 stars!", True),
        ("sales21@maishimfg.com", "Following Up - Wire Mesh Supply",
         "We manufacture wire mesh at wholesale factory price.", True),
        ("vzwmail@ecrmemail.verizonwireless.com",
         "Confirm your company's primary contact", "Verizon account.", True),
        # PROTECT the real pipes — these must NEVER be spam:
        ("dirkskristin@yahoo.com", "Form Submission - Get a Quote",
         "Name:\nEmail: dirkskristin@yahoo.com\nAddress: 1114 Gaines Ave SE\n"
         "Services: moss treatment, driveway", False),
        ("form-submission@squarespace.com", "Form Submission - Get a Quote",
         "Name:\nEmail Address:\nServices: gutters", False),
    ]
    for frm, subj, body, want in robot_cases:
        is_spam, why = spam_filter.looks_spam(frm, subj, body)
        if is_spam == want:
            passed += 1
        else:
            failed += 1
            kind = ("FALSE POSITIVE (called a real email spam!)"
                    if is_spam else "missed robot/spam")
            print(f"FAIL robot-case {kind}\n     from: {frm}")
    print("=" * 50)
    print(f"RESULT: {passed} passed, {failed} failed "
          f"({len(fixtures)} labeled + {len(robot_cases)} robot cases)")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
