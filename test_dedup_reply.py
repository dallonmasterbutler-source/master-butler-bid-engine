"""
MASTER BUTLER — regression tests for the Jul 20-21 fixes:

  · the poller's authoritative Message-ID dedup gate (_already_have),
    which stopped 200+ duplicate records from a fail-open 'seen' set;
  · dashboard-reply threading + quoting (mailer.send_reply), so Gmail
    draws the reply arrow and the Sent copy carries the customer's
    original message.

Pure/local — no network, no database. clouddb and the mail send are
stubbed so the logic is exercised in isolation.
"""

import sys
import types

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


# ── 1 · the dedup gate ───────────────────────────────────────────
def test_already_have():
    import gmail_poller
    fake = types.ModuleType("clouddb")
    state = {"has": False}
    fake.available = lambda: True
    fake.has_message_id = lambda mid: state["has"]
    old = sys.modules.get("clouddb")
    sys.modules["clouddb"] = fake
    try:
        # seen-hit → True, DB never consulted
        check("seen-hit returns True",
              gmail_poller._already_have("<a@x>", {"<a@x>"}) is True)
        # seen-miss + DB missing → False (a genuinely new message)
        state["has"] = False
        check("seen-miss + db-miss returns False",
              gmail_poller._already_have("<b@x>", set()) is False)
        # seen-miss + DB HAS it → True (THE fix: stale 'seen' can't
        # cause re-ingestion because the DB is the authority)
        state["has"] = True
        seen = set()
        check("seen-miss + db-hit returns True",
              gmail_poller._already_have("<c@x>", seen) is True)
        # …and it backfills 'seen' so the same id isn't re-queried
        check("db-hit backfills seen", "<c@x>" in seen)
    finally:
        if old is not None:
            sys.modules["clouddb"] = old
        else:
            sys.modules.pop("clouddb", None)


# ── 2 · reply quoting (pure) ─────────────────────────────────────
def test_quote_block():
    import mailer
    check("empty orig → no quote", mailer._quote_block(None) == "")
    check("blank text → no quote", mailer._quote_block({"text": "  "}) == "")
    q = mailer._quote_block({"from": "Jane <j@x.com>", "date": "2026-07-20",
                             "text": "line one\nline two"})
    check("quote has 'wrote:' header",
          "On 2026-07-20, Jane <j@x.com> wrote:" in q)
    check("quote prefixes each line with '>'",
          "> line one" in q and "> line two" in q)


# ── 3 · reply threading headers + quoted tail ────────────────────
def test_reply_threading():
    import mailer
    captured = {}

    def fake_api_send(msg, thread_id=None):
        captured["msg"] = msg
        captured["thread_id"] = thread_id
        return True, "ok (test)"

    # stub the Gmail thread lookup so the test never hits the network
    fake_ga = types.ModuleType("gmail_api")
    fake_ga.thread_for_reply = lambda to, mid=None: "THREAD123" if mid else None
    old_ga = sys.modules.get("gmail_api")
    sys.modules["gmail_api"] = fake_ga
    old_creds, old_send = mailer._creds, mailer._api_send
    mailer._creds = lambda: ("care@masterbutlerinc.com", "pw")
    mailer._api_send = fake_api_send
    try:
        orig = {"from": "Jane <jane@x.com>", "date": "2026-07-20 10:00",
                "text": "Original Squarespace submission\nServices: gutters"}
        ok, why = mailer.send_reply(
            "jane@x.com", "Re: your quote", "Here you go!", "LaRee",
            in_reply_to="abc@mail.gmail.com", orig=orig)   # bare id
        check("send_reply reports ok", ok)
        msg = captured.get("msg")
        check("message captured", msg is not None)
        if msg is not None:
            check("In-Reply-To set + angle-wrapped",
                  msg["In-Reply-To"] == "<abc@mail.gmail.com>")
            check("References set + angle-wrapped",
                  msg["References"] == "<abc@mail.gmail.com>")
            body = msg.get_content()
            check("body carries the customer's original",
                  "Original Squarespace submission" in body)
            check("original is quoted with '>'",
                  "> Original Squarespace submission" in body)
        # THE THREADING FIX: the send must carry the Gmail threadId, not
        # just the header — the header alone orphaned ~40% of replies
        check("threadId looked up + passed to the Gmail send",
              captured.get("thread_id") == "THREAD123")
        # no threading headers or thread lookup when we have no Message-ID
        captured.clear()
        mailer.send_reply("jane@x.com", "Re: hi", "hello", "LaRee")
        m2 = captured.get("msg")
        check("no In-Reply-To without a source id",
              m2 is not None and m2["In-Reply-To"] is None)
        check("no threadId without a source id",
              captured.get("thread_id") is None)
    finally:
        mailer._creds, mailer._api_send = old_creds, old_send
        if old_ga is not None:
            sys.modules["gmail_api"] = old_ga
        else:
            sys.modules.pop("gmail_api", None)


def run():
    test_already_have()
    test_quote_block()
    test_reply_threading()
    print("=" * 50)
    print(f"RESULT: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
