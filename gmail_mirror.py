"""
MASTER BUTLER — GMAIL ARCHIVE MIRROR (Dallon, Jul 12: "the dashboard
is still full of 70 items but the gmail only has a few — the office is
all caught up. Reconcile this so Monday morning it's ready to work
side by side with them.")

The office's habit IS the truth: when they archive a thread in Gmail,
it's handled. This mirror makes the dashboard honor that:

  · a queue record whose sender has NO messages left in the Gmail
    INBOX was archived by the office → mark it seen (off the queue)
  · records that never came through Gmail (voicemails, Jobber-side
    leads) are NEVER touched — the office never saw those in Gmail,
    so Gmail can't vouch for them
  · the standing resurface rule still applies: any new message brings
    the customer straight back to New in bold

READ-ONLY against Gmail (readonly IMAP select). Runs hourly from the
poller; every clear is written to the review log.
"""

import email.utils
import imaplib
import re


def _inbox_state():
    """What's currently sitting in INBOX: (senders, message_ids).

    We need BOTH because a website lead does NOT arrive from the
    customer — a Squarespace form is From 'form-submission@squarespace
    .info', so the customer's own address is never an inbox *sender*
    even while their request sits unhandled in the inbox (Dallon, Jul
    13: Amanda Gentry vanished from the queue for exactly this). The
    Message-ID is the record's real fingerprint and matches whether the
    mail came direct or through Squarespace — so it, not the sender, is
    what tells us the office archived a thread."""
    from gmail_poller import _creds
    import email as _email
    addr, pw = _creds()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(addr, pw)
    senders, msgids = set(), set()
    try:
        typ, _ = M.select('"INBOX"', readonly=True)
        if typ != "OK":
            return None, None
        typ, data = M.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        for i in ids:
            typ, hdr = M.fetch(
                i, "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])")
            if typ != "OK" or not hdr or not hdr[0]:
                continue
            raw = hdr[0][1].decode("utf-8", "replace")
            msg = _email.message_from_string(raw)
            _, em = email.utils.parseaddr(msg.get("From") or "")
            if em:
                senders.add(em.lower())
            mid = (msg.get("Message-ID") or "").strip()
            if mid:
                msgids.add(mid)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return senders, msgids


def state_sync(verbose=True):
    """LaRee's phone-call lesson (Jul 14) as a NON-DESTRUCTIVE mirror.

    Her words: in Gmail, trash = DONE; greyed out (read, still in the
    inbox) = being worked on. The Jul-13 kill switch stays — this never
    clears a row. It only WRITES what Gmail says into a `gmail_state`
    blob; the dashboard shows it as a chip and a fold, and a human (or
    a new message) decides everything else.

        gmail_state = { email: {"state": "unread"|"working"|"done",
                                "at": iso} }

      · unread  — their message sits UNREAD in the Gmail inbox
      · working — in the inbox but READ (greyed out on their screen)
      · done    — the office moved the thread to TRASH
    Absent = Gmail can't vouch (archived, or never came through Gmail)
    → dashboard shows the row exactly as before.
    """
    import clouddb
    if not clouddb.available():
        return 0
    from gmail_poller import _creds
    import email as _email
    from datetime import datetime, timezone

    addr, pw = _creds()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(addr, pw)

    def _scan(box):
        out = {}                        # msgid -> (sender, unread)
        typ, _ = M.select('"%s"' % box, readonly=True)
        if typ != "OK":
            return None
        typ, data = M.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        for i in ids:
            typ, resp = M.fetch(
                i, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])")
            if typ != "OK" or not resp or not resp[0]:
                continue
            flg = resp[0][0]
            flg = flg.decode("utf-8", "replace") if isinstance(
                flg, bytes) else str(flg)
            raw = resp[0][1].decode("utf-8", "replace")
            msg = _email.message_from_string(raw)
            _, em = email.utils.parseaddr(msg.get("From") or "")
            mid = (msg.get("Message-ID") or "").strip()
            if mid:
                out[mid] = (em.lower(), "\\Seen" not in flg)
        return out

    try:
        inbox = _scan("INBOX")
        trash = _scan("[Gmail]/Trash")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    if inbox is None:                   # couldn't read — change nothing
        return 0
    trash = trash or {}

    inbox_senders = {}                  # sender -> any-unread?
    for mid, (s, unread) in inbox.items():
        if s:
            inbox_senders[s] = inbox_senders.get(s, False) or unread
    trash_senders = {s for s, _ in trash.values() if s}

    # map each queue customer to a state via Message-ID first (forms
    # arrive From Squarespace — the Amanda Gentry lesson), sender second
    mids_by_email = {}
    emails = set()
    for _s, _r in clouddb.all_shadow():
        if _r.get("merged_into") or _r.get("spam_auto") \
                or _r.get("tech_sender") or _r.get("kind") == "jobber_event":
            continue
        m = re.search(r"<([^>]+)>", _r.get("from") or "")
        e = m.group(1).lower() if m else None
        if not e:
            continue
        emails.add(e)
        mid = (_r.get("message_id") or "").strip()
        if mid:
            mids_by_email.setdefault(e, set()).add(mid)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = {}
    for e in emails:
        mids = mids_by_email.get(e, set())
        hits = [inbox[m] for m in mids if m in inbox]
        if hits or e in inbox_senders:
            unread = any(u for _, u in hits) or inbox_senders.get(e, False)
            state[e] = {"state": "unread" if unread else "working",
                        "at": now}
        elif any(m in trash for m in mids) or e in trash_senders:
            state[e] = {"state": "done", "at": now}
    clouddb.put_blob("gmail_state", state)
    if verbose:
        cts = {}
        for v in state.values():
            cts[v["state"]] = cts.get(v["state"], 0) + 1
        print(f"gmail state mirror: {cts} across {len(emails)} customers "
              "(display-only — nothing cleared)")
    return len(state)


def api_state_sync(verbose=True):
    """THE MIRROR, DONE RIGHT (Jessica, Jul 20: 'mirror our box… we delete
    things and they show back up… done needs to stay done').

    Two fixes over state_sync():
      1. Runs over the Gmail API, not IMAP — so it works from the CLOUD
         and doesn't depend on Dallon's Mac or burn the IMAP quota (the
         reason the old mirror kept going stale).
      2. GONE-FROM-INBOX = DONE. The office's real flow is inbox → reply →
         ARCHIVE (not trash). state_sync only caught trashed threads, so
         archived-and-handled mail sat in the dashboard Inbox for days
         (Dallon, Jul 20: 'I still see things done 13 days ago'). Here,
         anything no longer in the Gmail inbox is 'done'.

    Still NON-DESTRUCTIVE and display-only: it only writes the
    `gmail_state` blob. The dashboard files a 'done' row to the drawer
    (recoverable), and ANY new customer message resurfaces it. An open
    Jobber quote (awaiting_response) still outranks this in the dashboard
    ladder, so money-in-flight never gets filed.
    """
    import clouddb
    if not clouddb.available():
        return 0
    import gmail_api
    from datetime import datetime, timezone
    if not gmail_api.can_read():
        return 0
    inbox_senders, inbox_msgids = gmail_api.inbox_index()
    if inbox_senders is None:               # couldn't read — change nothing
        return 0

    # map each queue customer to a state — by Message-ID first (forms
    # arrive From Squarespace; the Amanda Gentry lesson), sender second
    mids_by_email, emails = {}, set()
    for _s, _r in clouddb.all_shadow():
        if _r.get("merged_into") or _r.get("spam_auto") \
                or _r.get("tech_sender") or _r.get("kind") == "jobber_event":
            continue
        m = re.search(r"<([^>]+)>", _r.get("from") or "")
        e = m.group(1).lower() if m else None
        if not e:
            continue
        emails.add(e)
        mid = (_r.get("message_id") or "").strip()
        if mid:
            mids_by_email.setdefault(e, set()).add(mid)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = {}
    for e in emails:
        mids = mids_by_email.get(e, set())
        in_inbox_by_mid = any(m in inbox_msgids for m in mids)
        if in_inbox_by_mid or e in inbox_senders:
            unread = e in inbox_senders and inbox_senders.get(e, False)
            state[e] = {"state": "unread" if unread else "working", "at": now}
        else:
            # not in the inbox at all → the office archived or trashed it
            state[e] = {"state": "done", "at": now}
    clouddb.put_blob("gmail_state", state)
    # PER-MESSAGE MIRROR (Dallon, Jul 21): the sender-keyed state above
    # can't speak for rows whose sender ISN'T the customer — voicemails
    # (from the copycall phone system) and website forms. So also publish
    # the raw set of Message-IDs currently sitting in the Gmail inbox. A
    # record whose own message_id has LEFT this set was archived by the
    # office in Gmail = done, no matter who it came from. Only written when
    # the inbox read succeeded (we returned early otherwise), so it's never
    # a stale/empty set that would mass-hide rows.
    clouddb.put_blob("gmail_inbox_mids", sorted(inbox_msgids))
    if verbose:
        cts = {}
        for v in state.values():
            cts[v["state"]] = cts.get(v["state"], 0) + 1
        print(f"gmail API mirror: {cts} across {len(emails)} customers "
              "(display-only; archived/trashed = done)")
    return len(state)


# KILL SWITCH (Dallon, Jul 13 — URGENT): auto-clearing rows from the
# queue because a Gmail thread was archived kept making live rows vanish
# under the office's hands (Squarespace-form leads; and draft rows for
# Robert Lin / Alok Tyagi disappearing mid-work on the 2-min refresh).
# Guessing 'handled' from Gmail archive state fights the lanes and the
# office's own workflow, so it stays OFF. The office clears items by
# acting IN the dashboard (mark done / reply / decision) — reliable
# signals. Flip back on ONLY after this is proven safe end-to-end.
CLEAR_ENABLED = False


def sync(verbose=True):
    """Mirror Gmail's archive state onto the queue. Returns #cleared."""
    import clouddb
    if not CLEAR_ENABLED:
        if verbose:
            print("gmail mirror: auto-clear DISABLED (kill switch) — "
                  "no rows cleared")
        return 0
    if not clouddb.available():
        return 0
    senders, inbox_msgids = _inbox_state()
    if senders is None:                 # couldn't read — change nothing
        return 0

    # which addresses actually corresponded with us THROUGH GMAIL —
    # only those threads are Gmail's to vouch for. Also track, per
    # address, whether their LATEST message is still inbound (they wrote
    # last and we haven't replied SINCE). 'Ever replied' is not enough:
    # a returning customer we answered months ago for an old job would
    # look handled while their new request sits unanswered (exactly
    # Amanda Gentry — old gutter job replied to, new handyman ask not).
    import msglog
    from datetime import datetime as _d, timezone as _z

    def _utc(at):
        try:
            t = _d.fromisoformat(at)
            return t.replace(tzinfo=_z.utc) if t.tzinfo is None else t
        except Exception:
            return None

    gmail_addrs = set()
    awaiting_us = set()          # latest message is inbound = we owe a reply
    for addr, _name, ms in msglog.threads():
        a = (addr or "").lower()
        ins = [t for t in (_utc(m.get("at")) for m in ms
                           if m.get("dir") == "in") if t]
        outs = [t for t in (_utc(m.get("at")) for m in ms
                            if m.get("dir") == "out") if t]
        if ins:
            gmail_addrs.add(a)
            if not outs or max(ins) > max(outs):
                awaiting_us.add(a)

    # per customer: every Message-ID we hold, and whether they have an
    # UNANSWERED website form. A form arrives From Squarespace, so the
    # office 'archiving' it in Gmail is ambiguous — it can just be inbox
    # tidying, NOT a reply to the customer. So a form lead we never
    # replied to must stay visible until a human actually deals with it
    # (Dallon, Jul 13: Amanda Gentry — completed gutter job, wrote back
    # wanting handyman/plumbing; we may decline the work but still owe
    # her a reply). Settled/converted quotes count as handled.
    msgids_by_email = {}
    open_form_email = set()
    for _s, _r in clouddb.all_shadow():
        _m = re.search(r"<([^>]+)>", _r.get("from") or "")
        _e = _m.group(1).lower() if _m else None
        if not _e:
            continue
        _mid = (_r.get("message_id") or "").strip()
        if _mid:
            msgids_by_email.setdefault(_e, set()).add(_mid)
        _is_form = "squarespace" in _mid.lower() or (
            _r.get("newest_message") or "").lstrip().lower().startswith(
            "form submission")
        _oq = (_r.get("open_quote_ctx") or {}).get("status", "").lower()
        _settled = _oq in ("approved", "won", "converted")
        if _is_form and not _settled and _e in awaiting_us:
            open_form_email.add(_e)

    marks = clouddb.get_blob("msg_read") or {}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cleared = []
    for stamp, rec in clouddb.all_shadow():
        if rec.get("merged_into") or rec.get("spam_auto") \
                or rec.get("tech_sender") or rec.get("lead"):
            continue
        if rec.get("kind") == "jobber_event":
            continue
        m = re.search(r"<([^>]+)>", rec.get("from") or "")
        e = m.group(1).lower() if m else None
        if not e or e in marks or e not in gmail_addrs:
            continue
        if e in senders:
            continue                    # still in their inbox → still open
        # their real message (form submissions included) still in the
        # inbox = office hasn't archived it = still open.
        if msgids_by_email.get(e, set()) & inbox_msgids:
            continue
        # an unanswered website form is NOT vouched-for by Gmail archive
        # state (its sender is Squarespace, not the customer) — keep it
        # visible until a person actually replies or acts on it.
        if e in open_form_email:
            continue
        marks[e] = now
        cleared.append(e)

    if cleared:
        clouddb.put_blob("msg_read", marks)
        for e in cleared[:200]:
            try:
                clouddb.add_review({
                    "action": "mark_done", "by": "auto (Gmail mirror)",
                    "at": now, "customer": e,
                    "note": "cleared — the office archived this thread "
                            "in Gmail (their inbox is the truth)"})
            except Exception:
                pass
    if verbose:
        print(f"gmail mirror: {len(senders)} senders still in INBOX, "
              f"{len(cleared)} dashboard rows cleared to match")
    return len(cleared)


if __name__ == "__main__":
    sync()
