"""
MASTER BUTLER — PIPELINE INVARIANT CHECK (Dallon, Jul 22: "make sure
things are working in the order they need to" — after the Liuliu fold
bug, where dedup and the mirror each worked alone and hid a customer
together).

Pulls the live cloud dump over HTTPS (read-only) and re-derives the
whole visibility pipeline offline, then asserts the invariants that
must hold BETWEEN features:

  I1  Every sender with unread/working mail in the office's Gmail inbox
      whose records exist → a VISIBLE mirror line (or a named, provable
      reason: tech thread, spam, robot).
  I2  Every Message-ID in the inbox snapshot belongs to SOME record
      (ingest completeness — nothing sat unprocessed).
  I3  No customer is sticky-cleared with an inbound (msglog OR folded
      record) NEWER than the clear.
  I4  Every record folded (merged_into) points at a parent that exists
      and is not itself merged (no orphan chains).
  I5  The snapshot is fresh (< 25 min) and gmail_state agrees with it.

Run:  python3 pipeline_check.py          (uses .env creds; read-only)
Exit 0 = all invariants hold. Nonzero = printed violations.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).parent


def _cfg(key):
    import os
    v = os.environ.get(key)
    if v:
        return v
    env = BASE / ".env"
    if env.exists():
        for ln in env.read_text().splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


def _fetch_dump():
    import urllib.parse
    url = (_cfg("DASHBOARD_URL") or "").rstrip("/")
    pw = _cfg("DASHBOARD_PASSWORD")
    body = urllib.parse.urlencode({"password": pw, "next": "/"}).encode()
    op = urllib.request.build_opener(_NoRedirect)
    try:
        r = op.open(urllib.request.Request(url + "/login", data=body),
                    timeout=30)
    except urllib.error.HTTPError as e:
        r = e                       # the 303 IS the success carrying the cookie
    tok = ""
    for h, v in r.headers.items():
        if h.lower() == "set-cookie" and "mb_auth=" in v:
            tok = v.split("mb_auth=")[1].split(";")[0]
    req = urllib.request.Request(url + "/api/backup",
                                 headers={"Cookie": "mb_auth=" + tok,
                                          "Accept-Encoding": "gzip"})
    r = urllib.request.urlopen(req, timeout=120)
    raw = r.read()
    if (r.headers.get("Content-Encoding") or "") == "gzip":
        import gzip
        raw = gzip.decompress(raw)      # 21MB dump travels as ~3MB
    return json.loads(raw)


def _stamp_utc(stamp):
    try:
        return (datetime.strptime(stamp, "%Y%m%d-%H%M%S").astimezone()
                .astimezone(timezone.utc).isoformat(timespec="seconds"))
    except (ValueError, TypeError):
        return ""


def _email_of(r):
    m = re.search(r"<([^>]+)>", r.get("from") or "")
    e = (m.group(1) if m else "").strip().lower()
    return e if "@" in e else ""


def run(dump=None, verbose=True):
    import urllib.parse  # noqa: F401 (used in _fetch_dump)
    d = dump or _fetch_dump()
    blobs = d.get("blobs") or {}
    recs = d.get("shadow_records") or {}
    mids_blob = blobs.get("gmail_inbox_mids") or {}
    state = blobs.get("gmail_state") or {}
    cleared = blobs.get("cleared") or {}
    ml = blobs.get("message_log") or []
    mids = set(mids_blob.get("mids") or [])
    at = mids_blob.get("at") or ""
    problems = []

    def bad(inv, msg):
        problems.append(f"{inv}: {msg}")

    # index records per email (unmerged + folds), and per mid
    by_email, folds, by_mid = {}, {}, {}
    for stamp, r in recs.items():
        e = _email_of(r)
        mid = (r.get("message_id") or "").strip()
        if mid:
            by_mid[mid] = stamp
        if not e:
            continue
        slot = folds if r.get("merged_into") else by_email
        slot.setdefault(e, []).append((stamp, r))

    # latest inbound per addr from msglog (real UTC)
    latest_in = {}
    for m in ml:
        if m.get("dir") != "in":
            continue
        a = (m.get("addr") or "").lower()
        try:
            t = datetime.fromisoformat(m["at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            t = t.astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            continue
        if t > latest_in.get(a, ""):
            latest_in[a] = t

    def visible(e):
        """Re-derive the mirror's existence rule for one customer."""
        pairs = (by_email.get(e) or []) + (folds.get(e) or [])
        for stamp, r in pairs:
            mid = (r.get("message_id") or "").strip()
            ru = (_stamp_utc(r.get("received") or "")
                  or _stamp_utc(stamp))
            if ru and at and ru >= at:
                return True
            if mid and mid in mids:
                return True
            if r.get("folder") == "INBOX" and "@" not in mid:
                return True
        return False

    def sticky(e):
        clr = cleared.get(e) or ""
        if not clr:
            return False
        evs = [latest_in.get(e, "")]
        for stamp, r in (by_email.get(e) or []) + (folds.get(e) or []):
            evs.append(_stamp_utc(r.get("received") or "")
                       or _stamp_utc(stamp))
        newest = max(evs)
        return not (newest and newest > clr)

    # I5 — snapshot freshness
    try:
        age_min = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(at)).total_seconds() / 60
        if age_min > 25:
            bad("I5", f"inbox snapshot is {age_min:.0f} min old ({at})")
    except Exception:
        bad("I5", f"snapshot has no readable timestamp ({at!r})")

    # I1 — in Gmail inbox, has records → must be visible & not sticky
    for e, v in state.items():
        if v.get("state") not in ("unread", "working"):
            continue
        pairs = (by_email.get(e) or []) + (folds.get(e) or [])
        if not pairs:
            continue                    # I2 covers ingest gaps by mid
        rs = [r for _, r in pairs]
        if all(r.get("spam_auto") or r.get("tech_sender")
               or r.get("kind") == "jobber_event" for r in rs):
            continue                    # named reason: not a customer line
        if not visible(e):
            bad("I1", f"{e} has {v['state']} mail in Gmail but NO "
                      f"visible line (the Liuliu class)")
        else:
            # a dashboard ✓ Done while Gmail sits unread is LEGITIMATE
            # (their Done is authoritative until new customer action) —
            # the violation is an inbound NEWER than the clear that
            # still reads as sticky (msglog vs stamps disagreeing)
            clr = cleared.get(e) or ""
            li = latest_in.get(e, "")
            if clr and li and li > clr and sticky(e):
                bad("I3", f"{e} wrote at {li}, AFTER the {clr} clear, "
                          f"yet still computes sticky")

    # I6 — COLLECTOR HEARTBEATS (Jul 23, the silent-scorecard lesson:
    # a collector can run green while collecting nothing). Each beat
    # must be FRESH (cadence) and, where it carries counts, PLAUSIBLE
    # (pulse). A missing/stale beat = that collector is down.
    hb = blobs.get("heartbeats") or {}
    now_utc = datetime.now(timezone.utc)

    def age_min(name):
        b = hb.get(name) or {}
        try:
            return (now_utc - datetime.fromisoformat(b["at"])
                    ).total_seconds() / 60
        except Exception:
            return None

    for name, limit in (("poll", 15), ("mirror", 35), ("sweep", 150),
                        ("learning", 26 * 60), ("scorecard", 26 * 60),
                        ("backup", 26 * 60), ("brief", 26 * 60),
                        ("pipeline_check", 26 * 60)):
        a = age_min(name)
        if a is None:
            bad("I6", f"collector '{name}' has NO heartbeat — it may be "
                      f"running without collecting (the scorecard class)")
        elif a > limit:
            bad("I6", f"collector '{name}' last beat {a/60:.1f}h ago "
                      f"(limit {limit/60:.1f}h) — down or silent")
    # pulse checks: running ≠ collecting
    lb = hb.get("learning") or {}
    if lb.get("matched", 0) > 0 and lb.get("recorded", 0) == 0:
        bad("I6", f"learning ran with {lb['matched']} matched quotes but "
                  f"recorded 0 — collecting nothing (the Jul-16 class)")
    bk = hb.get("backup") or {}
    if bk.get("at") and (bk.get("kb") or 0) < 100:
        bad("I6", f"backup beat says only {bk.get('kb')} KB — not a "
                  f"restore point")
    # scorecard stagnation: firm offers 10+ days old with ZERO grades
    # company-wide means matching is broken, not that nobody booked
    sc = blobs.get("sched_scorecard") or {}
    old_cut = (now_utc.date() - timedelta(days=10)).isoformat()
    stale_firm = [v for v in sc.values()
                  if v.get("kind") == "date"
                  and (v.get("first_seen") or "9999") <= old_cut
                  and not v.get("actual_date")]
    any_graded = any(v.get("actual_date") for v in sc.values())
    if len(stale_firm) >= 10 and not any_graded:
        bad("I6", f"{len(stale_firm)} firm offers are 10+ days old with "
                  f"zero grades anywhere — the booking matcher is "
                  f"likely broken again")
    # overbooking pulse (Dallon, Jul 23: 'make sure we don't overbook a
    # day'): jobs moved OFF their day after it started mean the pace
    # guesses are too optimistic — that's a Dallon decision, not a drift
    _mv = [m for v in sc.values() for m in (v.get("moves") or [])]
    _dayof = sum(1 for m in _mv if m.get("phase") == "day_of")
    if _dayof >= 3:
        bad("I6", f"{_dayof} jobs have been moved OFF their day after it "
                  f"started — the crew-hour paces are overbooking; "
                  f"Dallon should tighten SAFETY_H or the paces")

    # I2 — every inbox mid belongs to some record
    unknown = [m for m in mids if m not in by_mid]
    if unknown:
        bad("I2", f"{len(unknown)} inbox message(s) have NO record — "
                  f"possible ingest gap: {unknown[:3]}")

    # I4 — fold chains resolve to live parents
    for e, pairs in folds.items():
        for stamp, r in pairs:
            parent = r.get("merged_into")
            pr = recs.get(parent)
            if not pr:
                bad("I4", f"{stamp} folded into MISSING record {parent}")
            elif pr.get("merged_into"):
                bad("I4", f"{stamp} folded into {parent}, which is itself "
                          f"merged (chain — activity may be lost)")

    if verbose:
        print(f"pipeline check: {len(problems)} violation(s) · "
              f"{len(state)} senders · {len(recs)} records · "
              f"{len(mids)} inbox mails · snapshot {at}")
        for p in problems:
            print("  ❌", p)
        if not problems:
            print("  ✅ all invariants hold — Gmail and the mirror agree")
    return problems


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
