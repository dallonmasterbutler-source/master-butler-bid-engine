"""
MASTER BUTLER — GMAIL SHADOW POLLER

Watches the office inbox over IMAP and runs every NEW message through the
pipeline in SHADOW MODE:

  * READONLY connection + BODY.PEEK — physically cannot mark anything read,
    move, label, or delete. The office's unread-count workflow is sacred.
  * Nothing is pushed to Jobber. Shadow drafts land in data/shadow_bids/
    for the scoreboard (system's number vs what the office actually quotes).
  * Already-processed messages are remembered in OUR ledger
    (data/processed_ids.txt) — Gmail itself is never used as a checklist.

Run once:      python3 gmail_poller.py
Keep watching: python3 gmail_poller.py --watch   (polls every 2 minutes)
"""

import email
import email.policy
import imaplib
import json
import re as _re
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
LEDGER = BASE / "data" / "processed_ids.txt"
SHADOW_DIR = BASE / "data" / "shadow_bids"


def _creds():
    import os
    creds = {}
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    addr = creds.get("GMAIL_ADDRESS") or os.environ.get("GMAIL_ADDRESS")
    pw = (creds.get("GMAIL_APP_PASSWORD")
          or os.environ.get("GMAIL_APP_PASSWORD", ""))
    return addr, pw.replace(" ", "")


def _processed():
    """The already-seen ledger: local file, PLUS the cloud's own memory
    when the database is reachable (so a cloud watcher never re-processes
    anything the Mac already captured, and vice versa)."""
    seen = set()
    if LEDGER.exists():
        seen |= set(LEDGER.read_text().split())
    try:
        import clouddb
        if clouddb.available():
            seen |= clouddb.seen_message_ids()
    except Exception:
        pass
    return seen


def _remember(msg_id):
    LEDGER.parent.mkdir(exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(msg_id + "\n")


def _already_have(msg_id, seen):
    """True if this exact email is already captured. Checks the fast
    in-memory `seen` set first, then falls back to an AUTHORITATIVE DB
    lookup — because `seen` can be stale/empty (local ledger wiped on a
    cloud restart, or seen_message_ids() silently failed), and when it
    was, the poller re-ingested the whole 2-day window as duplicate
    records (Jul 20: 200+ dupes). The DB check only runs for messages
    that LOOK new, so it's cheap."""
    if msg_id in seen:
        return True
    try:
        import clouddb
        if clouddb.available() and clouddb.has_message_id(msg_id):
            seen.add(msg_id)        # backfill so we don't re-query it
            return True
    except Exception:
        pass
    return False


# Real bid requests land in spam sometimes (seen in the Takeout mining) —
# sweep it too, same readonly guarantee. Spam-found requests get flagged
# so the office knows to fish the original out.
FOLDERS = ["INBOX", "[Gmail]/Spam"]


# Gmail-ids already looked at THIS session — skip re-fetching them every
# poll (in-memory; a restart re-checks the recent window once, cheap).
_API_SEEN = set()
_API_OK = {"ok": False, "at": 0.0, "sendonly": False}
_API_MIRROR_AT = 0.0        # last Gmail archive-mirror run (throttle ~15m)
_MIRROR_SWEEP_AT = 0.0      # last Gmail+Jobber mirror sweep (throttle ~1h)


def _api_configured():
    """Gmail OAuth creds are present at all (regardless of reachability)."""
    try:
        import gmail_api
        return gmail_api.configured()
    except Exception:
        return False


def _use_api():
    """Read via the Gmail API when the token can read. A YES is cached
    for the process (scope doesn't change mid-run), but a NO is retried
    every 10 minutes — the old once-per-process cache meant one token-
    endpoint blip at first poll locked the process onto the IMAP path
    for its whole lifetime (Jul 21 night sweep)."""
    if _API_OK["ok"]:
        return True
    import time as _t
    if _t.time() - _API_OK["at"] < 600:
        return False
    _API_OK["at"] = _t.time()
    try:
        import gmail_api
        scope = gmail_api.read_scope()
    except Exception:
        scope = None
    _API_OK["ok"] = scope is True
    # DEFINITIVELY send-only (Google answered, scope lacks read) is the
    # one case where the IMAP fallback is correct — a transient probe
    # failure (scope None) must not brick polling AND must not bulk-IMAP
    _API_OK["sendonly"] = scope is False
    print("  inbox read path: "
          + ("Gmail API" if _API_OK["ok"] else
             "IMAP (send-only token)" if _API_OK["sendonly"] else
             "unknown (probe failed — retry in 10m)"))
    return _API_OK["ok"]


def reconcile_inbox(verbose=True):
    """BACKSTOP for the 2-day poll window (#49, Jul 21): the normal poll
    only looks at `newer_than:2d`, so any inbox email missed during its
    window — a transient fetch error, an outage longer than the window,
    a message that arrived while the poller was wedged — is never
    revisited and simply vanishes from the dashboard (a Squarespace lead
    went missing exactly this way). This walks the ENTIRE current Gmail
    inbox + spam and ingests anything we have no record for. Read-only
    against Gmail; the _already_have gate makes it idempotent, so
    re-runs are cheap no-ops. Meant for a nightly cadence."""
    import email as _email
    import gmail_api
    if not gmail_api.can_read():
        return 0
    seen = _processed()
    found = 0
    for folder, q in (("INBOX", "in:inbox"), ("[Gmail]/Spam", "in:spam")):
        try:
            ids = gmail_api.list_ids(q, cap=600)      # NO date window
        except Exception as e:
            print(f"  (reconcile {folder} skipped: {e})")
            continue
        for gid in ids:
            try:
                raw = gmail_api.get_raw(gid)
            except Exception:
                continue
            m = _email.message_from_bytes(raw)
            msg_id = (m.get("Message-ID") or f"gmail-{gid}").strip()
            if _already_have(msg_id, seen):
                continue
            found += 1
            shadow_process(raw, msg_id, folder=folder)
            _remember(msg_id)
            seen.add(msg_id)
    if verbose:
        print(f"inbox reconcile: {found} missed message(s) recovered")
    return found


def _poll_via_api(seen):
    """Fetch new mail over the Gmail API — same Message-ID dedup as the
    IMAP path, so nothing is ever re-processed on cutover."""
    import email as _email
    import gmail_api
    new_count = 0
    for folder, q in (("INBOX", "in:inbox"), ("[Gmail]/Spam", "in:spam")):
        try:
            ids = gmail_api.list_ids(f"newer_than:2d {q}", cap=300)
        except Exception as e:
            print(f"  (api list {folder} skipped: {e})")
            continue
        for gid in ids:
            if gid in _API_SEEN:
                continue
            try:
                raw = gmail_api.get_raw(gid)
            except Exception:
                continue
            _API_SEEN.add(gid)
            m = _email.message_from_bytes(raw)
            msg_id = (m.get("Message-ID") or f"gmail-{gid}").strip()
            if _already_have(msg_id, seen):
                continue
            new_count += 1
            shadow_process(raw, msg_id, folder=folder)
            _remember(msg_id)
            seen.add(msg_id)
    try:
        _sweep_sent_api(seen)
    except Exception as e:
        print(f"  (sent sweep skipped: {e})")
    return new_count


def _sent_ledger():
    """Durable already-swept ledger for SENT mail (Jul 21 audit): the file
    ledger is wiped on every cloud restart and sent mail never becomes a
    shadow record, so the DB record-check can't vouch for it either. Each
    deploy re-recorded the same ~40 sent messages into the message log —
    23-26 copies each — and the flood evicted weeks of real history
    through the cap. This blob survives restarts. (set, save_fn)."""
    try:
        import clouddb
        if clouddb.available():
            cur = set(clouddb.get_blob("sent_seen") or [])

            def save(s):
                clouddb.put_blob("sent_seen", sorted(s)[-800:])
            return cur, save
    except Exception:
        pass
    return set(), lambda s: None


def _sweep_sent_api(seen):
    """Office replies sent from Gmail → the message log, via the API."""
    import email as _email
    import gmail_api
    import msglog
    durable, _save_durable = _sent_ledger()
    dirty = False
    for gid in gmail_api.list_ids("newer_than:3d in:sent", cap=40):
        _k = "sent-api-" + gid
        if _k in _API_SEEN:
            continue
        _API_SEEN.add(_k)
        try:
            raw = gmail_api.get_raw(gid)
        except Exception:
            continue
        msg = _email.message_from_bytes(raw, policy=_email.policy.default)
        mid = "sent-" + (msg.get("Message-ID") or gid).strip()
        if mid in seen or mid in durable:
            continue
        seen.add(mid)
        _remember(mid)
        durable.add(mid)
        dirty = True
        to_addr = _email.utils.parseaddr(msg.get("To", ""))[1]
        body = ""
        plain = msg.get_body(preferencelist=("plain",))
        if plain is not None:
            body = plain.get_content()
        else:
            h = msg.get_body(preferencelist=("html",))
            if h is not None:
                body = _re.sub(r"<[^>]+>", " ", h.get_content())
        sent_at = None
        try:
            from email.utils import parsedate_to_datetime
            if msg.get("Date"):
                sent_at = parsedate_to_datetime(msg["Date"]) \
                    .isoformat(timespec="seconds")
        except Exception:
            pass
        msglog.record("out", to_addr, subject=msg.get("Subject", ""),
                      body=body, at=sent_at)
    if dirty:
        _save_durable(durable)


def poll_once():
    """One pass: fetch unseen-by-US messages, shadow-process each."""
    # relay any cloud-queued internal mail while we're here (the cloud
    # host blocks SMTP; the Mac doesn't)
    try:
        import mailer
        n = mailer.drain_outbox()
        if n:
            print(f"  relayed {n} queued internal email(s)")
    except Exception:
        pass
    seen = _processed()
    new_count = 0

    # PREFERRED: the Gmail API (no IMAP rate limits — the Jul 15/16
    # outages). Falls back to IMAP automatically if the token can't read.
    if _use_api():
        new_count = _poll_via_api(seen)
    elif _api_configured() and not _API_OK.get("sendonly"):
        # OAuth creds exist but the API is blipping: SKIP this poll (next
        # one is 2 min away) rather than full-mailbox IMAP scans — the
        # account is chronically OVERQUOTA on IMAP and the bulk fallback
        # is exactly what locks it out longer (Jul 21 night sweep).
        # A DEFINITIVELY send-only token (sendonly=True) still falls
        # through to IMAP — that config reads no other way.
        print("  (Gmail API configured but unreachable — poll skipped; "
              "no bulk-IMAP fallback on the live account)")
    else:
        addr, pw = _creds()
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(addr, pw)
        try:
            for folder in FOLDERS:
                typ, _ = M.select(f'"{folder}"', readonly=True)
                if typ != "OK":
                    print(f"  (cannot open {folder} — skipped)")
                    continue
                typ, data = M.search(None, "ALL")
                ids = data[0].split() if data and data[0] else []
                for num in ids:
                    typ, hdr = M.fetch(
                        num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                    raw_hdr = hdr[0][1].decode(errors="replace")
                    msg_id = raw_hdr.split(":", 1)[-1].strip() \
                        or f"no-id-{num.decode()}"
                    if _already_have(msg_id, seen):
                        continue
                    typ, full = M.fetch(num, "(BODY.PEEK[])")
                    raw = full[0][1]
                    new_count += 1
                    shadow_process(raw, msg_id, folder=folder)
                    _remember(msg_id)
                    seen.add(msg_id)
            try:
                _sweep_sent(M, seen)
            except Exception as e:
                print(f"  (sent sweep skipped: {e})")
        finally:
            try:
                M.logout()
            except Exception:
                pass
    # pull the office's Settings edits down so THIS machine prices and
    # drafts with the same numbers the cloud uses
    try:
        import urllib.request
        from base64 import b64encode
        from cloudpush import _cfg
        url, pw = _cfg("DASHBOARD_URL"), _cfg("DASHBOARD_PASSWORD")
        if url and pw:
            for key in ("pricing_overrides", "canned_replies"):
                req = urllib.request.Request(
                    url.rstrip("/") + f"/api/blob/{key}",
                    headers={"Authorization": "Basic "
                             + b64encode(f"office:{pw}".encode()).decode()})
                val = json.load(urllib.request.urlopen(req, timeout=30))
                if val is not None:
                    (BASE / "data" / f"{key}.json").write_text(
                        json.dumps(val))
    except Exception:
        pass
    _keep_cloud_warm()          # heartbeat on EVERY poll, however invoked

    # 2-MINUTE FRESHNESS (Dallon, Jul 12: "hourly seems almost too
    # long") — both are one cheap call each, every ears-cycle:
    #  · Gmail archive mirror: office archives a thread → row clears
    #  · Jobber delta: a quote was created/sent/approved/converted →
    #    its record's status refreshes
    # The heavy hourly reconcile below stays as the backstop.
    import gmail_mirror
    # The legacy IMAP archive-clear (kill-switched, usually a no-op) runs
    # on its OWN try — when IMAP is OVERQUOTA it must NOT take the API
    # mirror down with it (Jul 20 bug: sync() raised first and skipped the
    # whole block, so archived-in-Gmail rows never filed).
    try:
        gmail_mirror.sync(verbose=False)
    except Exception as _e:
        print(f"  (gmail IMAP mirror skipped: {_e})")
    # THE MIRROR (Jessica, Jul 20): archived/trashed in Gmail = done.
    # The API version works from the cloud, catches ARCHIVES, and doesn't
    # touch the IMAP quota — so it runs even while IMAP is locked out.
    # Throttled to ~15 min (it reads every inbox header). Falls back to
    # the IMAP state_sync (trash-only) only if the API can't read.
    try:
        import time as _t
        global _API_MIRROR_AT
        if _t.time() - _API_MIRROR_AT >= 900:
            did_api = False
            try:
                import gmail_api
                if gmail_api.can_read():
                    gmail_mirror.api_state_sync(verbose=False)
                    did_api = True
            except Exception as _ae:
                print(f"  (gmail API mirror skipped: {_ae})")
            # API down → the IMAP trash-only signal, but ONLY on accounts
            # with no API creds at all or a DEFINITIVELY send-only token:
            # falling back to a full IMAP scan of INBOX+Trash during an
            # API hiccup hits the overquota account at exactly the wrong
            # moment (Jul 21 night sweep)
            if not did_api and (not _api_configured()
                                or _API_OK.get("sendonly")):
                gmail_mirror.state_sync(verbose=False)
            _API_MIRROR_AT = _t.time()
    except Exception as _e:
        print(f"  (gmail state mirror skipped: {_e})")
    try:
        import jobber_delta
        jobber_delta.sync()
    except Exception as _e:
        print(f"  (jobber delta skipped: {_e})")
    # THE MIRROR SWEEP (Dallon, Jul 21 night): hourly, both-systems
    # reconciliation — anything verifiably done in Jobber AND Gmail is
    # filed automatically instead of rotting in the queue.
    try:
        import time as _t2
        global _MIRROR_SWEEP_AT
        if _t2.time() - _MIRROR_SWEEP_AT >= 3600:
            import mirror_sweep
            n = mirror_sweep.sweep(verbose=False)
            if n:
                print(f"  mirror sweep: {len(n)} line(s) filed "
                      "(done in Jobber + Gmail)")
            _MIRROR_SWEEP_AT = _t2.time()
    except Exception as _e:
        print(f"  (mirror sweep skipped: {_e})")

    # QUOTE SYNC (Dallon's rule: dashboard mirrors Jobber, read-only,
    # no quote creation) — refresh which office quotes match our
    # records. Throttled to hourly: the 10-minute ears loop shouldn't
    # hammer Jobber's API all day.
    marker = BASE / "data" / "last_quote_sync.txt"
    try:
        last = datetime.fromisoformat(marker.read_text().strip())
        fresh = (datetime.now() - last).total_seconds() < 3600
    except Exception:
        fresh = False
    if not fresh:
        try:
            # SCOREBOARD + drafts backfill hit JOBBER — isolate them so a
            # Jobber THROTTLE can't starve the local watchdogs below
            # (Jul 16 health-check: scoreboard.run threw on a throttle,
            # jumped past lane/self-review/pulse, and froze all three at
            # 4:23pm — they only read our own DB and never needed Jobber).
            try:
                import scoreboard
                scoreboard.run(limit=40)
                import jobber_delta as _jd
                _jd.backfill_drafts()   # trust fix — Drafts counts real work
            except Exception as _e:
                print(f"  (scoreboard/quote sync skipped — Jobber: {_e})")
            # LOCAL WATCHDOGS — read the cloud DB only, so they run EVERY
            # hour regardless of Jobber's mood.
            try:
                import lane_review
                lane_review.run()            # hourly problem-catcher
            except Exception as _e:
                print(f"  (lane review skipped: {_e})")
            try:
                import failure_review
                failure_review.run()         # 🔍 self-review
            except Exception as _e:
                print(f"  (self review skipped: {_e})")
            try:
                import dashboard as _dash
                _dash.autodrafts_page(None)  # refreshes the pulse blob
            except Exception as _e:
                print(f"  (pulse refresh skipped: {_e})")
            marker.write_text(datetime.now().isoformat(timespec="seconds"))
            try:
                import autoreview
                autoreview.run(verbose=False)   # matches review themselves
            except Exception:
                pass
            # clients the office creates DIRECTLY in Jobber appear on
            # the Customers tab too (Dallon, Jul 9 pm) — hourly pull
            try:
                import jobber_client as _jcc
                recent = _jcc.recent_clients(days=21)
                if recent:
                    import clouddb as _cdb
                    if _cdb.available():
                        _cdb.put_blob("jobber_new_clients", recent)
                    else:
                        from cloudpush import push as _cp
                        _cp(blobs={"jobber_new_clients": recent})
            except Exception:
                pass
            # COMPLETENESS SELF-HEAL (Dallon, Jul 10: 'they don't have
            # to do any research'): recent records missing address/
            # facts/photos/badge/PW-surfaces get them filled hourly
            try:
                import clouddb as _cdb2
                if _cdb2.available():
                    import complete_sweep
                    complete_sweep.run(recent_hours=48)
            except Exception:
                pass
            # NIGHTLY GEAR (Dallon, Jul 12: heavy reports run at 3 AM,
            # never on the office's clock) — history refresh + the
            # report shelf, once per date, gated by a cloud marker
            try:
                from zoneinfo import ZoneInfo as _ZI
                _now_pt = datetime.now(_ZI("America/Los_Angeles"))
                if _now_pt.hour == 3:
                    import clouddb as _cdb3
                    _mark = _cdb3.get_blob("nightly_done") or {}
                    _today = _now_pt.date().isoformat()
                    if _mark.get("date") != _today:
                        _cdb3.put_blob("nightly_done",
                                       {"date": _today})
                        try:
                            import servicehistory
                            servicehistory.refresh(recent=200)
                        except Exception as _e:
                            print(f"  (history refresh: {_e})")
                        try:
                            import reports_nightly
                            reports_nightly.build()
                        except Exception as _e:
                            print(f"  (report shelf: {_e})")
            except Exception:
                pass
            # LEARNING REPORT (Dallon, Jul 12: visible learning on the
            # Scoreboard) — money funnel + what the office taught us
            try:
                import learning_report
                learning_report.build()
            except Exception as _e:
                print(f"  (learning report skipped: {_e})")
            # JOBBER PULSE (Dallon, Jul 10 — read-both no write-back):
            # today's appointments + overdue/active/remaining, hourly,
            # for the Today strip + the Handled-in-Jobber lane
            try:
                import jobber_sync
                jobber_sync.pulse()
            except Exception as _e:
                print(f"  (jobber pulse skipped: {_e})")
            try:
                sb = json.loads((BASE / "data" / "scoreboard.json").read_text())
                import clouddb
                if clouddb.available():
                    clouddb.put_blob("scoreboard", sb)
                else:
                    from cloudpush import push
                    push(blobs={"scoreboard": sb})
            except Exception:
                pass
        except Exception as e:
            print(f"  (quote sync skipped: {e})")
    return new_count


def _sweep_sent(M, seen):
    """Log outbound mail (office replies from Gmail) to the message log.
    Uses the same Message-ID ledger — each sent mail processed once."""
    import email as _email
    typ, _ = M.select('"[Gmail]/Sent Mail"', readonly=True)
    if typ != "OK":
        return
    from datetime import timedelta
    since = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'SINCE {since}')
    ids = data[0].split() if data and data[0] else []
    import msglog
    durable, _save_durable = _sent_ledger()
    dirty = False
    for num in ids[-40:]:
        typ, hdr = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        mid = "sent-" + (hdr[0][1].decode(errors="replace")
                         .split(":", 1)[-1].strip() or num.decode())
        if mid in seen or mid in durable:
            continue
        durable.add(mid)
        dirty = True
        typ, full = M.fetch(num, "(BODY.PEEK[])")
        msg = _email.message_from_bytes(full[0][1],
                                        policy=_email.policy.default)
        to_addr = _email.utils.parseaddr(msg.get("To", ""))[1]
        body = ""
        plain = msg.get_body(preferencelist=("plain",))
        if plain is not None:
            body = plain.get_content()
        else:
            h = msg.get_body(preferencelist=("html",))
            if h is not None:
                body = _re.sub(r"<[^>]+>", " ", h.get_content())
        sent_at = None
        try:
            from email.utils import parsedate_to_datetime
            if msg.get("Date"):
                sent_at = parsedate_to_datetime(msg["Date"]) \
                    .isoformat(timespec="seconds")
        except Exception:
            pass
        msglog.record("out", to_addr, subject=msg.get("Subject", ""),
                      body=body, at=sent_at)
        _remember(mid)
        seen.add(mid)
    if dirty:
        _save_durable(durable)


def _bid_for_quote(quote_number):
    """Which of OUR bids does this Jobber quote belong to? (scoreboard)"""
    if not quote_number:
        return None
    try:
        import clouddb
        if clouddb.available():
            sb = clouddb.get_blob("scoreboard") or {}
        else:
            f = BASE / "data" / "scoreboard.json"
            sb = json.loads(f.read_text()) if f.exists() else {}
        for row in sb.get("rows", []):
            if str(row.get("office_quote")) == str(quote_number):
                return row["stamp"]
    except Exception:
        pass
    return None


def _attach_event(target_stamp, ev, event_stamp):
    """Write the lifecycle event ONTO the customer's own bid record."""
    try:
        import clouddb
        rec = None
        if clouddb.available():
            rec = dict(clouddb.all_shadow()).get(target_stamp)
        if rec is None:
            f = BASE / "data" / "shadow_bids" / f"{target_stamp}.json"
            rec = json.loads(f.read_text()) if f.exists() else None
        if rec is None:
            return
        rec.setdefault("events", []).append(
            {"at": datetime.now().isoformat(timespec="seconds"),
             "event": ev.get("event"), "quote": ev.get("quote_number"),
             "detail": (ev.get("client") or "")[:60]})
        labels = {"quote_approved":
                  f"🎉 QUOTE #{ev.get('quote_number')} APPROVED — convert "
                  "it to a job in Jobber.",
                  "changes_requested":
                  f"✏️ Customer requested CHANGES on quote "
                  f"#{ev.get('quote_number')} — needs office attention.",
                  "request_received":
                  f"📥 Jobber request received (quote "
                  f"#{ev.get('quote_number')})."}
        if ev.get("event") in labels:
            rec["office_alert"] = labels[ev["event"]]
        if clouddb.available():
            clouddb.ingest_shadow(target_stamp, rec)
        else:
            f = BASE / "data" / "shadow_bids" / f"{target_stamp}.json"
            f.write_text(json.dumps(rec, indent=1))
            try:
                from cloudpush import push
                push(records=[(target_stamp, rec)])
            except Exception:
                pass
    except Exception as e:
        print(f"  (event attach failed: {e})")


def _transcribe_voicemail(record, parsed, raw_bytes, stamp, who):
    """Audio attachment -> words on the dashboard (Dallon: 'extract the
    info from the audio'). Returns True when a transcript landed."""
    try:
        import email as _em
        import transcribe
        msg_obj = _em.message_from_bytes(raw_bytes, policy=_em.policy.default)
        fn, audio = transcribe.extract_audio(msg_obj)
        if not audio:
            return False
        vdir = BASE / "data" / "voicemail"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / f"{stamp}-{(fn or 'vm.wav')}").write_bytes(audio)
        text = transcribe.transcribe(audio, fn or "")
        if not text:
            # LOUD, never silent (Terry Brower lesson, Jul 10: a 1:58
            # message read as 'no audio' and only Dallon's own ears
            # caught it). Card says audio EXISTS + flag in the review
            # feed; the hourly complete_sweep retries automatically.
            record["office_alert"] = (
                f"⚠ VOICEMAIL AUDIO from {who} IS ATTACHED "
                f"({(record.get('lead') or {}).get('duration', '?')}) but "
                "transcription failed — it will RETRY automatically within "
                "the hour; if this note persists, LISTEN BY PHONE.")
            try:
                import clouddb as _cdb
                if _cdb.available():
                    _cdb.add_review({
                        "stamp": stamp, "action": "flag_review",
                        "customer": record.get("from"), "by": "auto",
                        "note": f"voicemail transcription failed for {who} "
                                "— audio IS attached; auto-retry queued"})
            except Exception:
                pass
            return False
        record["newest_message"] = f"🎙 VOICEMAIL: “{text}”"
        record["kind"] = "new_request"
        from email_parser import find_services, find_address
        record["services"] = find_services(text) or record.get("services")
        record["address"] = record.get("address") or find_address(text)
        record["office_alert"] = (f"🎙 Voicemail from {who} — transcribed "
                                  "automatically; their words are below.")
        # FULL CARD, not an empty shell (Dallon, Jul 10 — Terry Brower):
        # profile link, returning status, address, priced draft, photos
        try:
            import vm_enrich
            vm_enrich.enrich(record, stamp)
        except Exception as e:
            print(f"  (voicemail enrichment skipped: {e})")
        try:
            import msglog
            msglog.record("in", parsed.get("sender_email") or "",
                          name=who, subject="Voicemail", body=text,
                          stamp=stamp)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"  (voicemail transcription skipped: {e})")
        return False


def _shadow_sources_for_dedupe():
    """(stamp, record) pairs from wherever records live — small helper
    shared by the voicemail fold check."""
    try:
        import clouddb
        if clouddb.available():
            return clouddb.all_shadow()
    except Exception:
        pass
    try:
        return [(p.stem, json.loads(p.read_text()))
                for p in sorted(SHADOW_DIR.glob("*.json"))]
    except Exception:
        return []


def shadow_process(raw_bytes, msg_id, folder="INBOX"):
    """Run one raw email through the pipeline; save the shadow draft."""
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    while (SHADOW_DIR / f"{stamp}.json").exists():
        # two emails in the same second must not share a stamp — the
        # second would silently overwrite the first (found in testing)
        from datetime import timedelta
        stamp = (datetime.strptime(stamp, "%Y%m%d-%H%M%S")
                 + timedelta(seconds=1)).strftime("%Y%m%d-%H%M%S")

    # save the raw email so the pipeline (and any re-run) can use it
    eml_path = SHADOW_DIR / f"{stamp}.eml"
    eml_path.write_bytes(raw_bytes)

    from email_parser import parse_eml
    parsed = parse_eml(eml_path)
    disp = (parsed.get("sender_name")
            or (parsed.get("sender_email") or "?").split("@")[0]
            .replace(".", " ").replace("_", " ").title())
    record = {"message_id": msg_id, "received": stamp, "folder": folder,
              "from": f"{disp} <{parsed['sender_email']}>",
              "subject": parsed["subject"], "kind": parsed["kind"],
              "services": parsed["services"], "address": parsed["address"],
              "phone": parsed.get("phone"),
              "sched_pref": parsed.get("sched_pref"),
              "tech_request": parsed.get("tech_request"),
              "newest_message": parsed.get("newest_message")}
    # bulk-mail marker for the spam filter (real customers never have it)
    try:
        import email as _email
        if _email.message_from_bytes(raw_bytes).get("List-Unsubscribe"):
            record["list_unsub"] = True
    except Exception:
        pass

    # TECH GATE (Jessica's call, Jul 9): mail FROM one of our techs is
    # field traffic — a callback, a job question. Tagged and VISIBLE on
    # the inbox, but never priced, never a lead, never spam-filtered.
    try:
        from techs import tech_for
        _tech = tech_for(parsed.get("sender_email"))
    except Exception:
        _tech = None
    if _tech:
        record["tech_sender"] = _tech
        record["kind"] = "tech_note"
        print(f"  👷 tech mail — {_tech}: {record['subject'][:48]} "
              "(tagged, no bid)")
        (SHADOW_DIR / f"{stamp}.json").write_text(json.dumps(record,
                                                             indent=1))
        try:
            import clouddb
            if clouddb.available():
                clouddb.ingest_shadow(stamp, record)
            else:
                from cloudpush import push_or_queue
                push_or_queue(stamp, record)
        except Exception:
            pass
        return

    # SPAM GATE (Dallon Jul 8: "teach our program... avoid putting it in
    # the dashboard"): solicitations get SAVED — visible in the queue's
    # spam drawer, never vanished — but skip pricing, Jobber lookups,
    # satellite imagery, and the conversation log entirely.
    try:
        import spam_filter
        _is_spam, _why = spam_filter.looks_spam(
            record["from"], record["subject"],
            record.get("newest_message") or "",
            has_address=bool(record.get("address")),
            list_unsub=bool(record.get("list_unsub")),
            kind=record["kind"])
    except Exception:
        _is_spam, _why = False, ""
    if _is_spam:
        record["spam_auto"] = _why
        print(f"  🚫 spam — filed without pricing: "
              f"{record['subject'][:48]} ({_why[:60]})")
        (SHADOW_DIR / f"{stamp}.json").write_text(json.dumps(record,
                                                             indent=1))
        try:
            import clouddb
            if clouddb.available():
                clouddb.ingest_shadow(stamp, record)
            else:
                from cloudpush import push_or_queue
                push_or_queue(stamp, record)
        except Exception:
            pass
        return
    # DO-NOT-SERVICE GUARD (Dallon): match by email, phone, ADDRESS
    # (new-email evaders), or name — flag loudly before anyone quotes.
    if parsed["kind"] in ("new_request", "scheduling"):
        try:
            import dns_check
            hit = dns_check.check(email=parsed.get("sender_email"),
                                  phone=parsed.get("phone"),
                                  address=parsed.get("address"),
                                  name=parsed.get("sender_name"))
        except Exception:
            hit = None
        if hit:
            record["dns_match"] = hit
            record["office_alert"] = (
                f"⛔ DO NOT SERVICE — matches '{hit['name']}' in Jobber "
                f"(matched by {hit['matched_by']}; marker: {hit['why'][:80]}). "
                "Do not quote or schedule; tell Dallon/Tom if unsure.")

    # OPEN-QUOTE CHECK (Dallon Jul 8, the Shadi/Nithya lesson): a known
    # customer writing in usually continues an EXISTING quote thread —
    # say so before anyone drafts a duplicate.
    if parsed["kind"] in ("new_request", "scheduling", "other") \
            and parsed.get("sender_email") \
            and not record.get("dns_match"):
        try:
            import jobber_client as jc
            oq = jc.find_open_quote(parsed["sender_email"], scan=80)
        except Exception:
            oq = None
        if oq:
            lines = "; ".join(
                f"{li['name'][:32]} ${li['totalPrice']:.0f}"
                for li in (oq.get("lineItems") or {}).get("nodes", [])
                if (li.get("totalPrice") or 0) > 0)[:150]
            record["open_quote_ctx"] = {
                "number": oq["quoteNumber"], "status": oq["quoteStatus"],
                "total": oq["amounts"]["total"],
                "created": (oq.get("createdAt") or "")[:10],
                "url": oq.get("jobberWebUri"),
                # ALL the info comes in (Dallon Jul 9, the Mia lesson)
                "lines": [{"name": li["name"],
                           "price": li.get("totalPrice")}
                          for li in (oq.get("lineItems") or {})
                          .get("nodes", [])][:8]}
            if oq["quoteStatus"] == "approved":
                record["office_alert"] = (
                    f"📎 CUSTOMER ALREADY APPROVED quote "
                    f"#{oq['quoteNumber']} (${oq['amounts']['total']}, "
                    f"{(oq.get('createdAt') or '')[:10]}: {lines}). This "
                    "email is likely about scheduling that work — not a "
                    "new request.")
            elif oq["quoteStatus"] in ("archived", "converted"):
                # the Kevin Pham case: a recent quote was archived
                # (postponed) or done — the ask is a REVIVAL, and the
                # story is in their conversation, not this email
                record["office_alert"] = (
                    f"📎 THEY HAVE RECENT HISTORY: quote "
                    f"#{oq['quoteNumber']} (${oq['amounts']['total']}, "
                    f"{oq['quoteStatus']} "
                    f"{(oq.get('createdAt') or '')[:10]}: {lines}). "
                    "READ THEIR FILE on the Customers tab before "
                    "quoting fresh — an archived quote usually means a "
                    "postponement (surgery, travel, weather).")
            else:
                record["office_alert"] = (
                    f"📎 EXISTING OPEN QUOTE #{oq['quoteNumber']} "
                    f"(${oq['amounts']['total']}, {oq['quoteStatus']} "
                    f"since {(oq.get('createdAt') or '')[:10]}: {lines}). "
                    "Likely a follow-up — reply on that quote, don't send "
                    "a second one.")
            # THE AMENDMENT ENGINE (Dallon's spec, built Jul-14 night):
            # add/remove/instead/only language + their quote's own lines
            # → propose the revision FROM their lines, never reprice
            # from scratch (the Gloria lesson). Proposal rides the
            # record; the office reviews like any draft.
            try:
                import amendments
                import lastpaid as _lp
                _prop = amendments.propose(
                    {"open_quote_ctx": record["open_quote_ctx"],
                     "newest_message": parsed.get("newest_message")
                     or parsed.get("body") or ""},
                    last_paid=_lp.last_paid(parsed.get("address"),
                                            parsed.get("sender_name")))
                if _prop:
                    record["amendment_proposal"] = _prop
                    record.setdefault("draft", {}).setdefault(
                        "notes", []).append(_prop["note"])
                    print("     → amendment proposal built from quote "
                          f"#{_prop['quote']} (${_prop['total']})")
            except Exception as _ae:
                print(f"     → amendment engine skipped: {_ae}")

    # NEW-or-RETURNING (techs' ask): first job = say so, exactly once.
    # Kevin Pham lesson (Jul 10): a returning customer writing "please
    # send me a quote" parses as kind OTHER — they must STILL get
    # recognized, so the check runs for every real-customer kind.
    if parsed["kind"] in ("new_request", "scheduling", "other") \
            and parsed.get("sender_email"):
        try:
            import jobber_client as jc
            cs = jc.client_summary(parsed["sender_email"])
        except Exception:
            cs = None
        if cs is not None:
            if not cs["known"]:
                record["customer_status"] = "new"
            elif cs["invoices"] == 0:
                record["customer_status"] = "in Jobber — no completed jobs yet"
            else:
                record["customer_status"] = f"returning ({cs['invoices']} jobs)"
            if cs.get("url"):
                record["jobber_client_url"] = cs["url"]
            # PHOTO PORT (Jessica, Jul 9): on-site pictures already on
            # the client's Jobber profile come over during intake —
            # they show in the bid's gallery labeled 'Jobber'.
            if cs.get("id") and cs.get("known"):
                try:
                    _port_jobber_photos(cs["id"], record, stamp)
                except Exception:
                    pass
            # SPARSE ASK FROM A KNOWN CUSTOMER (Kevin Pham, Jul 10):
            # "Please send me a quote" with no services/address isn't
            # missing info — it means WE already have the info.
            if (cs.get("known") and cs.get("invoices", 0) > 0
                    and not record.get("services")
                    and not record.get("address")
                    and not record.get("office_alert")):
                record["office_alert"] = (
                    f"🗂 RETURNING CUSTOMER ({cs['invoices']} past jobs) "
                    "left the request nearly blank — they expect us to "
                    "know them. Read their file on the Customers tab and "
                    "their last quote before replying; don't ask them to "
                    "re-explain.")
    if parsed.get("jobber_event"):
        ev = parsed["jobber_event"]
        record["jobber_event"] = ev
        # ONE CUSTOMER = ONE ROW (Dallon Jul 8): a Jobber event about a
        # quote we track ATTACHES to that customer's bid instead of
        # spawning a second queue item.
        target = _bid_for_quote(ev.get("quote_number"))
        if target:
            _attach_event(target, ev, stamp)
            record["merged_into"] = target
        else:
            # NOT one of our tracked quotes (office-direct) — the event
            # must still wear its customer's name/address/link (Dallon,
            # Jul 10: anonymous 'quote approved' rows, 3rd recurrence)
            try:
                import jobber_sync
                jobber_sync.dress_event(record)
                record.pop("_merge_target", None)
            except Exception:
                pass
        if ev["event"] == "quote_approved":
            record["office_alert"] = (
                f"🎉 QUOTE #{ev.get('quote_number')} APPROVED"
                + (f" by {ev['client']}" if ev.get("client") else "")
                + (f" (${ev['amount']})" if ev.get("amount") else "")
                + " — convert it to a job in Jobber.")
        elif ev["event"] == "changes_requested":
            record["office_alert"] = (
                f"Quote #{ev.get('quote_number')}: customer requested "
                "changes — needs office attention.")

    if parsed.get("lead"):
        lead = parsed["lead"]
        record["lead"] = lead
        # SAME CALL, SECOND NOTIFICATION (deploy overlaps double-process;
        # CopyCall sometimes re-sends): fold instead of a second entry
        try:
            for _ps, _pr in _shadow_sources_for_dedupe():
                _pl = _pr.get("lead") or {}
                if _pl.get("caller") == lead.get("caller") \
                        and _pl.get("when") == lead.get("when") \
                        and _ps != stamp:
                    record["merged_into"] = _ps
                    break
        except Exception:
            pass
        style = lead.get("style")
        who = parsed.get("phone") or "unknown caller"
        # SOFTWARE CALLER-ID: Jobber usually knows this number already.
        try:
            import jobber_client as jc
            cid = jc.caller_id(parsed.get("phone"))
        except Exception:
            cid = None
        if cid:
            record["caller_id"] = cid
            # the entry becomes a PERSON (Jessica): name + address on it
            if not record.get("address") and cid.get("address"):
                record["address"] = cid["address"]
            who = (f"{cid['name']} ({who}) — EXISTING CLIENT, "
                   f"{cid['invoices']} past job(s)"
                   + (f", {cid['address']}" if cid.get("address") else ""))
        elif parsed.get("phone"):
            who = f"{who} — not in Jobber, likely NEW lead"
        if style in ("notification", "audio") and \
                _transcribe_voicemail(record, parsed, raw_bytes, stamp, who):
            style = "transcript"
        if style == "notification":
            record["office_alert"] = (
                f"VOICEMAIL from {who}"
                + (f" ({lead['duration']}" if lead.get("duration") else " (")
                + (f", mailbox {lead['mailbox']})" if lead.get("mailbox") else ")")
                + " — retrieve + transcribe the message into this record; "
                "reply by EMAIL per office policy.")
        elif style == "audio":
            record["office_alert"] = (
                f"VOICEMAIL AUDIO attached from {who} — transcription "
                "pending; reply by EMAIL per office policy.")
        elif style == "transcript" and parsed["kind"] == "phone_lead":
            record["office_alert"] = (
                f"VOICEMAIL TRANSCRIPT from {who} — no services detected; "
                "read + reply by EMAIL per office policy.")

    if "Spam" in folder and parsed["kind"] == "new_request":
        record["office_alert"] = ("FOUND IN SPAM — real request; office "
                                  "should rescue it from the spam folder")

    # LIVE MESSAGES: every human inbound lands in the conversation log,
    # timestamped by the email's OWN Date header (not when we polled it).
    try:
        import msglog
        if parsed.get("sender_email") and parsed["kind"] != "jobber_event":
            sent_at = None
            try:
                import email as _em
                from email.utils import parsedate_to_datetime
                hdr = _em.message_from_bytes(raw_bytes).get("Date")
                if hdr:
                    sent_at = parsedate_to_datetime(hdr) \
                        .isoformat(timespec="seconds")
            except Exception:
                pass
            msglog.record("in", parsed["sender_email"],
                          name=parsed.get("sender_name") or "",
                          subject=parsed.get("subject") or record["subject"],
                          body=parsed.get("newest_message") or "",
                          stamp=stamp, at=sent_at)
    except Exception:
        pass

    # DUPLICATE LINKING: same person/thread/address within 30 days gets
    # LINKED, never dropped — the office decides "same job" vs "new job".
    try:
        from dedup import check_duplicate
        priors = []
        try:                    # records live in the DB on Render — the
            import clouddb as _cdb   # file glob found NOTHING there and
            if _cdb.available():     # the dup guard never fired (Mia bug)
                _source = _cdb.all_shadow()
            else:
                _source = [(pj.stem, json.loads(pj.read_text()))
                           for pj in sorted(SHADOW_DIR.glob("*.json"))]
        except Exception:
            _source = []
        for _st, pr in _source:
            m = _re.search(r"<([^>]+)>", pr.get("from", ""))
            try:
                _rcv = datetime.strptime(_st, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            priors.append({
                "stamp": _st,
                "sender_email": m.group(1) if m else "",
                "phone": pr.get("phone"),
                "address": pr.get("address"),
                "thread_id": None,
                "received": _rcv,
            })
        m = _re.search(r"<([^>]+)>", record["from"])
        # IDENTICAL RESEND, NO ADDRESS (Kevin Pham sent 'Please send me
        # a quote' twice, 20 min apart — identity dedup needs an
        # address, so neither folded): same sender + same words within
        # 48h is one email, period.
        _mytext = (record.get("newest_message") or "").strip()[:120]
        if m and _mytext:
            for _ps, _pr in _shadow_sources_for_dedupe():
                if _ps == stamp or _pr.get("merged_into"):
                    continue
                _pm = _re.search(r"<([^>]+)>", _pr.get("from") or "")
                if not _pm or _pm.group(1).lower() != m.group(1).lower():
                    continue
                if (_pr.get("newest_message") or "").strip()[:120] \
                        != _mytext:
                    continue
                try:
                    _age_h = abs((datetime.strptime(stamp, "%Y%m%d-%H%M%S")
                                  - datetime.strptime(_ps,
                                                      "%Y%m%d-%H%M%S"))
                                 .total_seconds()) / 3600
                except ValueError:
                    continue
                if _age_h <= 48:
                    record["merged_into"] = _ps
                    print(f"     → identical resend folded into {_ps}")
                    break
        verdict = check_duplicate(
            {"sender_email": m.group(1) if m else "",
             "phone": record.get("phone"),
             "address": record.get("address"),
             "received": datetime.now()}, priors)
        if verdict["verdict"] == "suspected_duplicate":
            # AUTO-SETTLE the obvious ones (the Fenich lesson): a
            # services-free reply/confirmation in the same thread folds
            # into the earlier bid by itself; the office is only asked
            # when there could be NEW work.
            prior_rec = {}
            try:
                pj = SHADOW_DIR / f"{verdict['match']['stamp']}.json"
                if pj.exists():
                    prior_rec = json.loads(pj.read_text())
                else:
                    import clouddb as _cdb
                    if _cdb.available():
                        prior_rec = dict(_cdb.all_shadow()).get(
                            verdict["match"]["stamp"]) or {}
            except Exception:
                prior_rec = {}
            from dedup import looks_same_job
            # send-failure notices all share Jobber's sender and carry
            # no services — the folder was merging DIFFERENT customers'
            # bounces into one row (Avani's + Greg's vanished into
            # Sherrie's, Jul 15). Each victim keeps their own record.
            _sf = (record.get("jobber_event") or {}).get("event") \
                == "send_failed"
            if not _sf and looks_same_job(record.get("subject"),
                              prior_rec.get("subject"),
                              record.get("newest_message"),
                              bool(parsed.get("services"))):
                record["merged_into"] = verdict["match"]["stamp"]
                record["office_alert"] = None
                print(f"     → auto-folded into {verdict['match']['stamp']}"
                      " (reply/confirmation, no new services)")
                try:
                    from store import save_review as _sr
                except Exception:
                    _sr = None
                try:
                    import clouddb as _cdb
                    entry = {"stamp": stamp, "action": "duplicate_same",
                             "customer": record.get("from"), "by": "auto",
                             "note": f"auto-folded into "
                                     f"{verdict['match']['stamp']} — reply/"
                                     "confirmation, no new services",
                             "at": datetime.now().isoformat(
                                 timespec="seconds")}
                    if _cdb.available():
                        _cdb.add_review(entry)
                except Exception:
                    pass
            else:
                record["duplicate_of"] = verdict["match"]["stamp"]
                record["office_alert"] = (record.get("office_alert", "") +
                    f" POSSIBLE DUPLICATE of {verdict['match']['stamp']} "
                    f"({verdict['reason']}) — same job or new job?").strip()
        elif verdict["verdict"] == "multi_property":
            # realty / property manager: same client, another house —
            # NEW job, own property record, notes stay per-property
            record["same_client_as"] = verdict["match"]["stamp"]
            record["office_alert"] = (record.get("office_alert", "") +
                " MULTI-PROPERTY CLIENT (same contact as "
                f"{verdict['match']['stamp']}, different address) — "
                "separate quote; keep notes on THIS property.").strip()
    except Exception:
        pass    # linking is a bonus, never a blocker

    print(f"  📧 {parsed['subject'][:60]}")
    print(f"     kind={parsed['kind']}  services={parsed['services']}")

    if parsed["kind"] == "new_request" and parsed["services"]:
        # full pipeline run (property lookup + vision on any photos + engine)
        try:
            import io
            from contextlib import redirect_stdout
            from pipeline import process
            buf = io.StringIO()
            with redirect_stdout(buf):
                draft = process(eml_path)
            record["pipeline_output"] = buf.getvalue()
            if draft:                       # structured copy for the dashboard
                record["draft"] = draft
                # a known customer often skips their address — the
                # pipeline found it in Jobber; keep it ON the record
                # (Becky lesson: record stayed address-less)
                if not record.get("address"):
                    record["address"] = (draft.get("customer") or {}) \
                        .get("address")
            # playbook rule that deserves the queue badge (seasons.py)
            if not record.get("office_alert"):
                m_a = _re.search(r"OFFICE_ALERT: (.+)",
                                 record["pipeline_output"])
                if m_a:
                    record["office_alert"] = m_a.group(1).strip()
            print("     → shadow draft generated")
        except Exception as e:
            record["pipeline_error"] = str(e)
            print(f"     → pipeline error: {e}")
    else:
        print("     → no bid needed (question/scheduling/other)")

    # LEARN FROM RETURNING CUSTOMERS (Dallon, Jul 10 — the Kevin Pham
    # gap): a sparse ask from someone with a past quote never got a
    # system bid, so we couldn't compare our number to the office's.
    # Rebuild a shadow bid from that quote's services → the scoreboard
    # can learn like any other bid.
    if record.get("open_quote_ctx") and record.get("address") \
            and not (((record.get("draft") or {}).get("bid") or {})
                     .get("services")):
        try:
            import shadow_from_quote
            shadow_from_quote.from_open_quote(record)
            if ((record.get("draft") or {}).get("bid") or {}).get("services"):
                print("     → shadow bid rebuilt from their last quote "
                      "(for scoreboard learning)")
        except Exception as e:
            print(f"     → shadow-from-quote skipped: {e}")

    out = SHADOW_DIR / f"{stamp}.json"
    out.write_text(json.dumps(record, indent=1))

    # mirror to the cloud: direct database writes when we ARE the cloud
    # (poller running on Render), HTTPS courier when we're the Mac
    try:
        import clouddb
        if clouddb.available():
            clouddb.ingest_shadow(stamp, record)
            clouddb.put_photo(stamp, "eml", 0, raw_bytes)   # raw archive
            try:
                from pipeline import extract_photos
                from imgprep import prep_jpeg_bytes
                for i, p in enumerate(extract_photos(eml_path)):
                    clouddb.put_photo(stamp, "customer", i,
                                      prep_jpeg_bytes(p, 900, 70))
            except Exception:
                pass
            print("     → cloud: written directly")
        else:
            from cloudpush import push_or_queue, flush_pending
            flush_pending()
            ok = push_or_queue(stamp, record)
            print("     → cloud: " + ("synced" if ok else "queued (offline)"))
    except Exception:
        pass                    # cloud mirroring never blocks shadow mode


def _port_jobber_photos(client_id, record, stamp, cap=6):
    """Pull the client's own on-site pictures from their Jobber profile
    into the bid's gallery (Jessica, Jul 9). Saved under kind 'jobber';
    the gallery labels them automatically. Cloud-direct or courier."""
    import re as _re
    import urllib.request as _ur
    import jobber_client as jc
    photos = jc.client_photos(client_id, limit=cap)
    if not photos:
        return
    ref = (_re.sub(r"[^a-z0-9]+", "-",
                   (record.get("address") or "").lower()).strip("-")[:60]
           or stamp)
    saved = 0
    caps = {}
    for i, (_fn, url, _cap) in enumerate(photos):
        if _cap:
            caps[str(i)] = _cap[:300]     # the tech's words (LaRee)
        try:
            data = _ur.urlopen(url, timeout=25).read()
            if len(data) > 4_000_000:
                continue
            from imgprep import prep_jpeg_from_bytes
            data = prep_jpeg_from_bytes(data, 1000, 72)
        except Exception:
            continue
        try:
            import clouddb
            if clouddb.available():
                clouddb.put_photo(ref, "jobber", i, data)
            else:
                from cloudpush import push
                import base64 as _b64
                push(photos=[{"ref": ref, "kind": "jobber", "idx": i,
                              "b64": _b64.b64encode(data).decode()}])
            saved += 1
        except Exception:
            continue
    if caps:
        try:
            import clouddb
            if clouddb.available():
                allc = clouddb.get_blob("photo_captions") or {}
                if allc.get(ref) != caps:
                    allc[ref] = caps
                    clouddb.put_blob("photo_captions", allc)
        except Exception:
            pass
    if saved:
        print(f"     → ported {saved} on-site photo(s) from Jobber")


def _keep_cloud_warm():
    """Each poll: leave a heartbeat (dashboard shows 'ears last heard
    Xm' + alarm if silent) and, from the Mac, ping /health so the free
    tier stays awake."""
    from datetime import timezone
    beat = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        import clouddb
        if clouddb.available():                 # we ARE the cloud
            beat["host"] = "render-cloud"
            clouddb.put_blob("poller_heartbeat", beat)
            return
        import urllib.request
        from cloudpush import _cfg, push
        url = _cfg("DASHBOARD_URL")
        if url:
            urllib.request.urlopen(url.rstrip("/") + "/health", timeout=20)
            beat["host"] = "dallon-mac"
            push(blobs={"poller_heartbeat": beat})
    except Exception:
        pass


if __name__ == "__main__":
    watch = "--watch" in sys.argv
    while True:
        n = poll_once()
        _keep_cloud_warm()
        print(f"[{datetime.now():%H:%M}] poll complete — {n} new message(s)")
        if not watch:
            break
        time.sleep(120)
