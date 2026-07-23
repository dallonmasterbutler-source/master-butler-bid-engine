"""
MASTER BUTLER — regression tests for msglog.clean_body (Jul 23):

  · Squarespace's footer repeats the form fields AFTER the customer's
    message — the summary must show their words ONCE, not the message
    followed by a second copy of the skeleton;
  · a message at the very end of the mail (no footer) still captures;
  · 'Email Address:' is not the house address;
  · reply tails ('On … wrote:') still cut; plain messages untouched.

Pure/local — no network, no database.
"""

import sys

import msglog

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


FORM_DUP = """Name:
Jane Doe
Email Address:
jane@x.com
Address:
123 Main St, Monroe WA
SERVICES REQUESTED:
Gutter cleaning
MESSAGE:
Hi, we need our gutters done before the rain. Back roof is steep.
Name:
Jane Doe
Email Address:
jane@x.com
SERVICES REQUESTED:
Gutter cleaning
Sent via form submission"""

out = msglog.clean_body(FORM_DUP)
check("footer repeat is trimmed off the message",
      out.count("gutters done before the rain") == 1
      and "Sent via" not in out and out.count("Jane Doe") == 0)
check("real street address wins over 'Email Address:'",
      "123 Main St, Monroe WA" in out and "jane@x.com" not in out)
check("services line survives", "Gutter cleaning" in out)

FORM_END = ("Name:\nJoe\nEmail Address:\njoe@x.com\nMESSAGE:\n"
            "Just the message, nothing after it, call me back today.")
check("message at end-of-mail (no footer) still captured",
      "call me back today" in msglog.clean_body(FORM_END))

FORM_MID = ("Name:\nJoe\nEmail:\njoe@x.com\nMESSAGE:\n"
            "My address is on file with you already, come whenever "
            "works this week thanks.")
check("the word 'address' inside their sentence doesn't cut it",
      "come whenever works this week" in msglog.clean_body(FORM_MID))

check("reply tail still cut",
      msglog.clean_body("See you Tuesday!\n\nOn Jul 9, 2026, at 12:17 PM, "
                        "Master Butler <c@m.com> wrote:\n> old")
      == "See you Tuesday!")
check("plain message untouched",
      msglog.clean_body("Can you come July 15, 2026 at 10am?")
      == "Can you come July 15, 2026 at 10am?")

print(f"RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
