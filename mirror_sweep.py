"""
MASTER BUTLER — THE MIRROR SWEEP (Dallon, Jul 21 night: "we need to
build a system where we continually check both gmail and jobber,
systematically taking away things that were done in jobber AND gmail —
those 45 old ones should have been taken care of long ago")

A standing reconciler. For every customer line the dashboard would show,
it asks BOTH systems for a verdict and files the line only when both
say "handled":

  JOBBER done =
    · quote converted or archived, or
    · an APPROVED quote whose client has a Jobber JOB created at/after
      the quote (the office scheduled it — the quote just never flipped
      to converted), or
    · the customer has no Jobber side at all (vacuously done — Gmail
      alone decides those)
  GMAIL done =
    · their thread is archived/trashed (sender-keyed mirror state), or
    · every record Message-ID of theirs has left the Gmail inbox and
      the records predate the snapshot (per-message mirror), or
    · they never came through Gmail at all (vacuously done — Jobber
      alone decides those)

Both done → the sticky `cleared` blob (exactly what the office's ✓ Done
writes) + a review-log entry naming the evidence. Fully reversible: any
new customer action (email, voicemail, Jobber approve/change-request)
resurfaces the line through the standing rules.

FAIL-SAFE BY DESIGN: any uncertainty — Jobber throttled, mirror stale,
live open quote (draft/awaiting/changes), anything unquantifiable —
leaves the line exactly where it is. Hiding a real customer is the one
unforgivable failure; this only files what BOTH systems prove is done.

Runs: hourly from the poll loop, nightly from cloud_nightly, and on
demand via POST /mirror_sweep (the office/Dallon can trigger a pass).
"""

import json
import re
import time
from datetime import datetime, timezone

# statuses that mean money is still in flight — NEVER auto-filed
LIVE_QUOTE = ("draft", "awaiting_response", "changes_requested")


def _email_of(rec):
    m = re.search(r"<([^>]+)>", rec.get("from") or "")
    e = (m.group(1) if m else "").strip().lower()
    return e if "@" in e else ""      # phone-number froms are not emails


def _jobber_verdict(email, newest_ctx, sleep_s=2.0):
    """(done?, evidence, certain?) from Jobber's point of view."""
    status = ((newest_ctx or {}).get("status") or "").lower()
    number = (newest_ctx or {}).get("number")
    if not newest_ctx:
        return True, "no Jobber side", True          # vacuous
    if status in ("converted", "archived"):
        return True, f"quote #{number} {status}", True
    if status in LIVE_QUOTE:
        return False, f"quote #{number} still {status}", True
    if status == "approved":
        import jobber_client as jc
        time.sleep(sleep_s)                 # polite: one query per client
        jobs = jc.client_jobs(email)
        if jobs is None:
            return False, "Jobber couldn't answer (left alone)", False
        created = ((newest_ctx or {}).get("created") or "")[:10]
        after = [j for j in jobs
                 if (j.get("createdAt") or "")[:10] >= created]
        if after:
            j = after[0]
            return True, (f"approved #{number} → job "
                          f"#{j.get('jobNumber')} scheduled "
                          f"{(j.get('createdAt') or '')[:10]}"), True
        return False, f"approved #{number}, no job booked yet", True
    return False, f"quote #{number} status '{status}' (left alone)", False


def _gmail_verdict(email, recs, gmail_state, mids_blob):
    """(done?, evidence) from Gmail's point of view."""
    inbox_recs = [r for r in recs
                  if r.get("folder") == "INBOX"
                  and "@" in (r.get("message_id") or "")]
    if not inbox_recs:
        return True, "never came through Gmail"      # vacuous
    st = (gmail_state.get(email) or {}).get("state")
    if st == "done":
        return True, "thread archived in Gmail"
    if st in ("unread", "working"):
        return False, "still sitting in the Gmail inbox"
    # per-message: every mid gone AND every record predates the snapshot
    if isinstance(mids_blob, dict) and mids_blob.get("at"):
        mids = set(mids_blob.get("mids") or [])
        at = mids_blob["at"]
        import dashboard as _db
        if all(_db._stamp_utc(r.get("received") or "") and
               _db._stamp_utc(r.get("received") or "") < at and
               r["message_id"].strip() not in mids for r in inbox_recs):
            return True, "all their messages archived in Gmail"
    return False, "Gmail can't vouch (left alone)"


def sweep(verbose=True, limit=80):
    """One reconciliation pass. Returns the list of filed lines."""
    import clouddb
    if not clouddb.available():
        return []
    import dashboard as _db

    gmail_state = clouddb.get_blob("gmail_state") or {}
    mids_blob = clouddb.get_blob("gmail_inbox_mids") or {}
    cleared = clouddb.get_blob("cleared") or {}
    marks = clouddb.get_blob("msg_read") or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # group records per customer email (same keying as the roster)
    by_email = {}
    for stamp, r in clouddb.all_shadow():
        if r.get("merged_into") or r.get("spam_auto") \
                or r.get("tech_sender") or r.get("kind") == "jobber_event":
            continue
        e = _email_of(r)
        if not e or "masterbutlerinc" in e:
            continue
        by_email.setdefault(_db._canon_email(e), []).append((stamp, r))

    filed = []
    for email, pairs in list(by_email.items())[:1000]:
        if len(filed) >= limit:
            break
        pairs.sort()
        stamps = [s for s, _ in pairs]
        recs = [r for _, r in pairs]
        # newest customer event vs an existing sticky clear — if the
        # office already ✓-Done'd them and nothing new came in, or the
        # line wouldn't show anyway, skip (nothing to take away)
        newest_evt = _db._stamp_utc(stamps[-1])
        if cleared.get(email) and newest_evt \
                and newest_evt <= cleared[email]:
            continue
        newest_ctx = next((r.get("open_quote_ctx")
                           for _, r in reversed(pairs)
                           if r.get("open_quote_ctx")), None)
        j_done, j_why, j_sure = _jobber_verdict(email, newest_ctx)
        if not j_done:
            continue
        g_done, g_why = _gmail_verdict(email, recs, gmail_state, mids_blob)
        if not g_done:
            continue
        # POSITIVE EVIDENCE REQUIRED (first-run lesson): two vacuous
        # verdicts ("no Jobber side" + "never came through Gmail") is
        # zero proof — e.g. a real request sitting in the spam folder
        # would match. At least one system must POSITIVELY vouch.
        if j_why == "no Jobber side" and g_why == "never came through Gmail":
            continue
        # both systems vouch → file it (the exact writes ✓ Done makes:
        # the sticky cleared blob AND the office-wide read-mark — the
        # first run wrote only cleared, and Won-lane rows kept showing)
        cleared[email] = now
        marks[email] = now
        filed.append({"email": email, "jobber": j_why, "gmail": g_why})
        try:
            clouddb.add_review({
                "stamp": stamps[-1], "action": "auto_filed",
                "customer": email,
                "note": f"mirror sweep — Jobber: {j_why} · Gmail: {g_why}",
                "at": now})
        except Exception:
            pass
        if verbose:
            print(f"  filed {email} | Jobber: {j_why} | Gmail: {g_why}")
    if filed:
        clouddb.put_blob("cleared", cleared)
        clouddb.put_blob("msg_read", marks)
    if verbose:
        print(f"mirror sweep: {len(filed)} line(s) filed "
              f"({len(by_email)} customers checked)")
    return filed


if __name__ == "__main__":
    for f in sweep(verbose=True):
        pass
