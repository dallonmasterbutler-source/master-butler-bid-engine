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
import threading
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
        if not created:
            # delta-attached ctx carries no quote date; '>= ""' would let
            # ANY historical job count as proof of scheduling (Jul 21
            # night sweep) — without the date we can't tell, so leave it
            return False, (f"approved #{number}, quote date unknown "
                           "(left alone)"), False
        after = [j for j in jobs
                 if (j.get("createdAt") or "")[:10] >= created]
        if after:
            j = after[0]
            return True, (f"approved #{number} → job "
                          f"#{j.get('jobNumber')} scheduled "
                          f"{(j.get('createdAt') or '')[:10]}"), True
        # (the 7-day aged-approval heuristic was removed the same night it
        # was added — the mirror is now anchored to the office's ACTUAL
        # Gmail inbox, so their archive behavior decides; no guessing)
        return False, f"approved #{number}, no job booked yet", True
    return False, f"quote #{number} status '{status}' (left alone)", False


def _gmail_verdict(raws, recs, gmail_state, mids_blob):
    """(done?, evidence) from Gmail's point of view. `raws` = every RAW
    sender address in this customer's group — gmail_state is keyed by
    raw address while the sweep groups by _canon_email, and consulting
    only the canonical one read the PRIMARY address's 'done' for an
    aliased customer whose NEW request came from the secondary (Jul 21
    night sweep)."""
    import dashboard as _db
    if isinstance(raws, str):
        raws = [raws]
    raws = list(raws)
    inbox_recs = [r for r in recs if r.get("folder") == "INBOX"]
    if not inbox_recs:
        # a real request the poller rescued from Spam never sat in the
        # office's inbox — that is NOT 'handled', it's 'unseen' (Jul 21
        # night sweep: a positive Jobber verdict on an OLD quote was
        # enough to file a spam-trapped NEW request)
        if any("spam" in (r.get("folder") or "").lower() for r in recs):
            return False, "request sitting in Spam (left alone)"
        return True, "never came through Gmail"      # vacuous
    # a record whose Message-ID isn't a real header (gmail-<gid>/no-id
    # fallbacks) can never be matched against the inbox snapshot — its
    # absence is unprovable, so Gmail can't vouch for this customer
    if any("@" not in (r.get("message_id") or "") for r in inbox_recs):
        return False, "Gmail can't vouch (unmatchable Message-ID)"
    newest_seen = max((_db._stamp_utc(r.get("received") or r.get("stamp")
                                      or "") or "" for r in inbox_recs))
    states = [gmail_state.get(a) or {} for a in raws]
    if any(s.get("state") in ("unread", "working") for s in states):
        return False, "still sitting in the Gmail inbox"
    if states and all(s.get("state") == "done" for s in states):
        # FRESHNESS (Jul 21 night sweep): 'done' observed BEFORE their
        # newest email proves nothing about that email — a returning
        # customer in the 15-min mirror gap was filed permanently here
        newest_at = max((s.get("at") or "") for s in states)
        if newest_at and newest_seen and newest_at >= newest_seen:
            return True, "thread archived in Gmail"
        # stale 'done' → fall through to the per-message check
    # per-message: every mid gone AND every record predates the snapshot
    if isinstance(mids_blob, dict) and mids_blob.get("at"):
        mids = set(mids_blob.get("mids") or [])
        at = mids_blob["at"]
        if all(_db._stamp_utc(r.get("received") or "") and
               _db._stamp_utc(r.get("received") or "") < at and
               r["message_id"].strip() not in mids for r in inbox_recs):
            return True, "all their messages archived in Gmail"
    return False, "Gmail can't vouch (left alone)"


_SWEEPING = threading.Lock()


def sweep(verbose=True, limit=80):
    """One reconciliation pass. Returns the list of filed lines."""
    import clouddb
    if not clouddb.available():
        return []
    if not _SWEEPING.acquire(blocking=False):
        # hourly poller pass + office /mirror_sweep clicks + the nightly
        # can overlap — stacked passes hammer Jobber (2s-per-client each)
        # and double-file; one at a time is plenty (Jul 21 night sweep)
        if verbose:
            print("mirror sweep: another pass is already running — skipped")
        return []
    try:
        return _sweep_inner(verbose, limit)
    finally:
        _SWEEPING.release()


def _sweep_inner(verbose, limit):
    import clouddb
    import dashboard as _db

    gmail_state = clouddb.get_blob("gmail_state") or {}
    mids_blob = clouddb.get_blob("gmail_inbox_mids") or {}
    cleared = clouddb.get_blob("cleared") or {}
    marks = clouddb.get_blob("msg_read") or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # group records per customer email (same keying as the roster);
    # remember every RAW address too — gmail_state is raw-keyed
    by_email, raws_of = {}, {}
    for stamp, r in clouddb.all_shadow():
        if r.get("merged_into") or r.get("spam_auto") \
                or r.get("tech_sender") or r.get("kind") == "jobber_event":
            continue
        e = _email_of(r)
        if not e or "masterbutlerinc" in e:
            continue
        canon = _db._canon_email(e)
        by_email.setdefault(canon, []).append((stamp, r))
        raws_of.setdefault(canon, set()).add(e)

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
        g_done, g_why = _gmail_verdict(raws_of.get(email) or [email],
                                       recs, gmail_state, mids_blob)
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
        # ATOMIC MERGE (Jul 21 night sweep, round 2): jsonb || in the DB
        # adds ONLY our filed customers — concurrent office clicks and
        # the nightly's separate process can never be overwritten by a
        # stale wholesale write.
        ups = {f["email"]: now for f in filed}
        clouddb.merge_blob("cleared", ups)
        clouddb.merge_blob("msg_read", ups)
    if verbose:
        print(f"mirror sweep: {len(filed)} line(s) filed "
              f"({len(by_email)} customers checked)")
    return filed


if __name__ == "__main__":
    for f in sweep(verbose=True):
        pass
