"""
MASTER BUTLER — DUPLICATE DETECTION

Stops the system from creating two bids for one job. Real causes we've
seen in the actual mailbox:
  - a customer submits the website form 4 times in a row (impatient clicks)
  - a customer replies inside an existing email thread
  - the same person emails again a week later about the same request

Rule of thumb: NEVER silently drop anything. A suspected duplicate is
LINKED to the earlier request and shown to the office with one button:
"same job" or "new job". Five seconds of human judgment beats a lost lead.
"""

import re
from datetime import datetime, timedelta

# How long an open request "owns" new messages from the same person/address
WINDOW_DAYS = 30


def normalize_email(addr: str) -> str:
    return (addr or "").strip().lower()


def normalize_address(addr: str) -> str:
    """Boil an address down so trivial format differences still match.
    '325 7th Ave W, Kirkland, WA. 98033' == '325 7th ave w kirkland wa 98033'
    """
    a = (addr or "").lower()
    a = re.sub(r"[^\w\s]", " ", a)          # drop punctuation
    a = re.sub(r"\b(street|st|avenue|ave|place|pl|road|rd|drive|dr|"
               r"court|ct|lane|ln|boulevard|blvd|way)\b", "", a)
    a = re.sub(r"\s+", " ", a).strip()
    return a


def normalize_phone(phone: str) -> str:
    """Digits only, last 10 (handles +1, dashes, dots, spaces)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def looks_same_job(subject, prior_subject, message, has_services):
    """AUTO-VERDICT (learned from the Fenich case, Jul 9 — his 'Yes,
    the 14th will work' sat on the queue asking 'same job or new
    job?'). Returns True only when it's SAFE to say same job:
      · the email adds NO new services, AND
      · it's a reply in the same thread, OR reads like a short
        confirmation/acknowledgment.
    Anything with new services or real length still asks the office —
    folding a genuinely new request is the unforgivable failure."""
    if has_services:
        return False
    msg = (message or "").strip().lower()
    subj = (subject or "").strip().lower()
    psubj = (prior_subject or "").strip().lower()
    core = re.sub(r"^(re:|fwd?:)\s*", "", psubj).strip()
    same_thread = subj.startswith(("re:", "fwd")) and core and core in subj
    confirmish = (len(msg) <= 220 and re.search(
        r"\b(yes|works for (me|us)|will work|that works|sounds good|"
        r"perfect|confirm(ed)?|ok(ay)?|see you (then|on)|thank(s| you))\b",
        msg))
    return bool(same_thread or confirmish)


def check_duplicate(incoming: dict, open_requests: list) -> dict:
    """Compare one incoming request against open ones — PROPERTY-AWARE.

    Identity = email/phone. A JOB = identity + ADDRESS. The realty lesson
    (Dallon, Jul 2026): one client, many houses — the same sender at a
    DIFFERENT address is a NEW JOB from a multi-property client, never a
    duplicate. (Jobber can't split notes per location; our records can,
    so notes/history stay per-property.)

    incoming / open request fields used:
      sender_email, phone, address, thread_id, received (datetime)

    Returns {"verdict": "new" | "suspected_duplicate" | "multi_property",
             "match": <request or None>, "reason": str}
    """
    email_n = normalize_email(incoming.get("sender_email"))
    phone_n = normalize_phone(incoming.get("phone"))
    addr_n = normalize_address(incoming.get("address"))
    thread = incoming.get("thread_id")
    when = incoming.get("received") or datetime.now()

    multi = None    # remember a same-client-different-house hit
    for prior in open_requests:
        age = when - (prior.get("received") or when)
        if age > timedelta(days=WINDOW_DAYS):
            continue

        # Strongest signal: same email thread = same conversation, always link
        if thread and prior.get("thread_id") and thread == prior["thread_id"]:
            return {"verdict": "suspected_duplicate", "match": prior,
                    "reason": "reply in the same email thread"}

        prior_addr = normalize_address(prior.get("address"))
        same_person = (
            (email_n and normalize_email(prior.get("sender_email")) == email_n)
            or (phone_n and normalize_phone(prior.get("phone")) == phone_n))

        if same_person:
            if addr_n and prior_addr and addr_n != prior_addr:
                # realty / property manager: same client, different house
                multi = multi or {
                    "verdict": "multi_property", "match": prior,
                    "reason": "same client, DIFFERENT address — separate "
                              "job; notes stay per-property"}
                continue
            return {"verdict": "suspected_duplicate", "match": prior,
                    "reason": f"same contact within {WINDOW_DAYS} days"
                              + (" at the same address" if addr_n else "")}

        # Same property, different contact (spouses submit for one house)
        if addr_n and prior_addr == addr_n:
            return {"verdict": "suspected_duplicate", "match": prior,
                    "reason": f"same property address within {WINDOW_DAYS} days"}

    if multi:
        return multi
    return {"verdict": "new", "match": None, "reason": "no open match"}


# ─────────────────────────────────────────────────────────────
# PROVE IT on the real repeat-submitter found in the mailbox mining
# (same person submitted the water-leak form 4x) + a same-house spouse case.
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import datetime as dt
    import csv
    from pathlib import Path

    passed = failed = 0
    def check(label, got, want):
        global passed, failed
        ok = got == want
        print(("✅" if ok else "❌"), f"{label}: {got}")
        passed, failed = passed + (ok), failed + (not ok)

    # Real duplicates from the mined form data, if available
    data = Path(__file__).parent / "data" / "form_submissions.csv"
    if data.exists():
        rows = list(csv.DictReader(open(data)))
        leak = [r for r in rows if "water leak" in r["notes"].lower()]
        print(f"Real repeat-submitter found in mined data: {len(leak)} submissions")
        open_reqs = []
        dupes = 0
        for i, r in enumerate(leak):
            item = {"sender_email": r["email"], "address": r["address"],
                    "thread_id": None, "received": dt(2026, 6, 1, 12, i)}
            verdict = check_duplicate(item, open_reqs)
            if verdict["verdict"] == "suspected_duplicate":
                dupes += 1
            else:
                open_reqs.append(item)
            print(f"   submission {i+1}: {verdict['verdict']} ({verdict['reason']})")
        check("Repeat submissions caught", dupes, len(leak) - 1)

    # Synthetic sanity checks
    base = {"sender_email": "a@x.com", "address": "325 7th Ave W, Kirkland, WA 98033",
            "thread_id": "t1", "received": dt(2026, 7, 1)}
    spouse = {"sender_email": "b@y.com",   # different person...
              "address": "325 7th ave w kirkland wa. 98033",  # ...same house
              "thread_id": None, "received": dt(2026, 7, 3)}
    later = {"sender_email": "a@x.com", "address": None,
             "thread_id": None, "received": dt(2026, 9, 15)}  # 2.5 months later

    check("Spouse same-house is linked",
          check_duplicate(spouse, [base])["verdict"], "suspected_duplicate")
    check("Same person after window is NEW",
          check_duplicate(later, [base])["verdict"], "new")

    print(f"\nRESULT: {passed} passed, {failed} failed")
    exit(1 if failed else 0)
