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


def _inbox_senders():
    """Set of lowercase from-addresses currently sitting in INBOX."""
    from gmail_poller import _creds
    addr, pw = _creds()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(addr, pw)
    senders = set()
    try:
        typ, _ = M.select('"INBOX"', readonly=True)
        if typ != "OK":
            return None
        typ, data = M.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        for i in ids:
            typ, hdr = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if typ != "OK" or not hdr or not hdr[0]:
                continue
            raw = hdr[0][1].decode("utf-8", "replace")
            _, em = email.utils.parseaddr(raw.replace("From:", "").strip())
            if em:
                senders.add(em.lower())
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return senders


def sync(verbose=True):
    """Mirror Gmail's archive state onto the queue. Returns #cleared."""
    import clouddb
    if not clouddb.available():
        return 0
    senders = _inbox_senders()
    if senders is None:                 # couldn't read — change nothing
        return 0

    # which addresses actually corresponded with us THROUGH GMAIL —
    # only those threads are Gmail's to vouch for
    import msglog
    gmail_addrs = set()
    for addr, _name, ms in msglog.threads():
        if any(m.get("dir") == "in" for m in ms):
            gmail_addrs.add((addr or "").lower())

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
