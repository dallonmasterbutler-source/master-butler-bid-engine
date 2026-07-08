"""
MASTER BUTLER — COPYCALL VOICEMAIL VOLUME REPORT (Dallon, Jul 8)

Before paying for an AI receptionist, measure the problem: stream the
8.9 GB Takeout archive and pull every CopyCall voicemail notification.
Each one carries the caller's number, message length, and timestamp:

  "...left a 1:00 long message ... from 14254660025, on Friday..."

Produces data/copycall_calls.json (one row per voicemail) plus a
printed month-by-month volume report with hang-up rates and repeat
callers. The caller-number list doubles as the input for the software
caller-ID build (match against Jobber clients).
"""

import json
import re
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path

from mbox_miner import iter_messages, MBOX

PAT = re.compile(rb"left a (\d+):(\d+) long message.*?from (\d{7,15}),",
                 re.S)


def scan():
    calls, scanned = [], 0
    for raw in iter_messages(MBOX):
        scanned += 1
        if scanned % 10000 == 0:
            print(f"  ...{scanned} emails, {len(calls)} voicemails")
        if b"copycall" not in raw[:2000].lower():
            continue
        m = PAT.search(raw[:4000])
        if not m:
            continue
        secs = int(m.group(1)) * 60 + int(m.group(2))
        number = m.group(3).decode()
        date = ""
        dm = re.search(rb"\nDate: ([^\r\n]+)", raw[:2000])
        if dm:
            try:
                date = parsedate_to_datetime(
                    dm.group(1).decode()).date().isoformat()
            except Exception:
                pass
        calls.append({"date": date, "from": number, "secs": secs})
    Path("data/copycall_calls.json").write_text(json.dumps(calls))
    print(f"scanned {scanned} emails -> {len(calls)} voicemails")
    return calls


def report(calls):
    months = Counter(c["date"][:7] for c in calls if c["date"])
    hangups = [c for c in calls if c["secs"] <= 5]
    real = [c for c in calls if c["secs"] > 5]
    nums = Counter(c["from"] for c in real)
    repeats = sum(1 for n, k in nums.items() if k > 1)
    print()
    print("COPYCALL VOICEMAIL REPORT")
    print("=" * 46)
    print(f"total voicemail notifications: {len(calls)}")
    print(f"hang-ups / dead air (<=5s):    {len(hangups)} "
          f"({len(hangups)/max(len(calls),1):.0%})")
    print(f"real messages (>5s):           {len(real)}")
    print(f"unique callers (real):         {len(nums)}")
    print(f"callers who left 2+ messages:  {repeats}")
    if real:
        avg = sum(c['secs'] for c in real) / len(real)
        print(f"avg real message length:       {avg:.0f}s")
    print()
    print("BY MONTH (last 24):")
    for m in sorted(months)[-24:]:
        print(f"  {m}: {months[m]:>4} {'#' * min(months[m], 60)}")


if __name__ == "__main__":
    report(scan())
