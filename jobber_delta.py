"""
MASTER BUTLER — 2-MINUTE JOBBER DELTA (Dallon, Jul 12: "hourly seems
almost too long"). One cheap aliased query per ears-cycle pulls the
quotes that most recently CHANGED state — created, sent, approved,
converted, changes-requested — and refreshes the matching records'
open_quote_ctx so the queue tracks reality at ~2 minutes, not ~60.

The heavyweight hourly reconcile stays as the backstop. Read-only.
"""

import re

# one call, five small windows — each sort key surfaces the quotes
# whose that-transition happened most recently
DELTA_Q = """
query Delta {
  created:   quotes(first: 8, sort: {key: CREATED_AT,   direction: DESCENDING}) { nodes { ...Q } }
  sent:      quotes(first: 8, sort: {key: LAST_SENT_AT, direction: DESCENDING}) { nodes { ...Q } }
  approved:  quotes(first: 5, sort: {key: APPROVED_AT,  direction: DESCENDING}) { nodes { ...Q } }
  converted: quotes(first: 5, sort: {key: CONVERTED_AT, direction: DESCENDING}) { nodes { ...Q } }
  changes:   quotes(first: 3, sort: {key: LAST_CHANGES_REQUESTED_AT, direction: DESCENDING}) { nodes { ...Q } }
}
fragment Q on Quote {
  quoteNumber quoteStatus jobberWebUri
  amounts { total }
  client { name emails { address } }
}
"""


def sync(verbose=False):
    """Refresh open_quote_ctx on records whose quote just moved.
    Returns #records updated."""
    import clouddb
    if not clouddb.available():
        return 0
    import jobber_client as jc
    was, jc.DRY_RUN = jc.DRY_RUN, False
    try:
        d = jc._post(DELTA_Q, {}, "jobber delta")
    finally:
        jc.DRY_RUN = was
    if not d or d.get("error") or d.get("dry_run"):
        return 0

    # newest state per quote number (later windows can repeat quotes)
    quotes = {}
    for lane in ("created", "sent", "approved", "converted", "changes"):
        for q in ((d.get(lane) or {}).get("nodes") or []):
            quotes[str(q.get("quoteNumber"))] = q
    by_email = {}
    for qn, q in quotes.items():
        for e in ((q.get("client") or {}).get("emails") or []):
            em = (e.get("address") or "").lower()
            if em:
                by_email.setdefault(em, []).append(q)

    updated = 0
    for stamp, rec in clouddb.all_shadow():
        if rec.get("merged_into") or rec.get("spam_auto") \
                or rec.get("kind") == "jobber_event":
            continue
        m = re.search(r"<([^>]+)>", rec.get("from") or "")
        em = m.group(1).lower() if m else None
        if not em or em not in by_email:
            continue
        ctx = rec.get("open_quote_ctx") or {}
        for q in by_email[em]:
            qn = str(q.get("quoteNumber"))
            # only touch the quote this record already tracks, or give
            # a quote-less record its FRESH quote (it was just created/
            # sent — that's the 'office sent it' lifecycle edge)
            if ctx and str(ctx.get("number")) != qn:
                continue
            new_status = q.get("quoteStatus")
            if ctx.get("status") == new_status and ctx:
                continue
            # MERGE, don't replace (Jul 21 audit): the delta's slim query
            # has no createdAt, and replacing the ctx erased `created` —
            # which the 90-day stale-quote review flag depends on. Keep
            # every field the delta doesn't refresh (created, via, …).
            rec["open_quote_ctx"] = {**ctx,
                "number": q.get("quoteNumber"),
                "status": new_status,
                "total": (q.get("amounts") or {}).get("total"),
                "url": q.get("jobberWebUri"),
                "lines": ctx.get("lines") or []}
            clouddb.ingest_shadow(stamp, rec)
            updated += 1
            break
    if verbose:
        print(f"jobber delta: {len(quotes)} recent quotes, "
              f"{updated} records refreshed")
    return updated


BACKFILL_Q = """
query($term: String!) {
  clients(searchTerm: $term, first: 3) {
    nodes { emails { address }
      quotes { nodes { quoteNumber quoteStatus createdAt jobberWebUri
                       amounts { total } } } } } }"""


def backfill_drafts(verbose=False):
    """THE TRUST FIX (Dallon, Jul 14: '30 inbox and 20 drafts… but
    really they are working in the background with gmail and the old
    system — no trust').

    The 2-minute delta only sees the last handful of quote transitions;
    a quote the office created in Jobber BEFORE our record existed (or
    outside those windows) never gets linked, so the row sits in Drafts
    forever — the Jul-14 audit found 6 of 20 'drafts' already quoted,
    approved, or even converted in Jobber (Tammy Jett's job was DONE).

    Hourly, for every un-reviewed record that still looks like a ready
    draft with NO tracked quote: search Jobber by the customer's email;
    if their newest quote is recent (created within 45 days) and past
    the draft stage, write it into open_quote_ctx — the existing lane
    rules then move the row to Waiting/Won/Handled on the next render.
    READ-ONLY against Jobber; a new customer message still resurfaces
    any row (standing rule)."""
    import clouddb
    if not clouddb.available():
        return 0
    import jobber_client as jc
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=45)).isoformat()[:10]
    was, jc.DRY_RUN = jc.DRY_RUN, False
    updated = 0
    try:
        for stamp, rec in clouddb.all_shadow():
            if rec.get("merged_into") or rec.get("spam_auto") \
                    or rec.get("tech_sender") or rec.get("reviewed") \
                    or rec.get("kind") in ("jobber_event", "phone_lead"):
                continue
            if not ((rec.get("draft") or {}).get("total")):
                continue                 # not a priced draft
            # a MISSING link, or a STALE one — lhasija's record tracked
            # her 2025 quote while the office sent #36642 the same
            # morning. A ctx created 45+ days ago is history, not this
            # request; look again.
            _ctx = rec.get("open_quote_ctx") or {}
            if _ctx and (_ctx.get("created") or "9999") >= cutoff:
                continue                 # fresh link — leave it alone
            m = re.search(r"<([^>]+)>", rec.get("from") or "")
            em = m.group(1).lower() if m else None
            if not em:
                continue
            d = jc._post(BACKFILL_Q, {"term": em},
                         "drafts backfill (read-only)")
            if not d or d.get("error") or d.get("dry_run"):
                continue
            newest = None
            for node in (d.get("clients") or {}).get("nodes") or []:
                addrs = [(e.get("address") or "").lower()
                         for e in node.get("emails") or []]
                if em not in addrs:
                    continue             # exact email match only
                for q in (node.get("quotes") or {}).get("nodes") or []:
                    if not newest or (q.get("createdAt") or "") > \
                            (newest.get("createdAt") or ""):
                        newest = q
                break
            if not newest:
                continue
            status = (newest.get("quoteStatus") or "").lower()
            if status == "draft" or (newest.get("createdAt")
                                     or "")[:10] < cutoff:
                continue                 # old history ≠ this request
            rec["open_quote_ctx"] = {
                "number": newest.get("quoteNumber"),
                "status": newest.get("quoteStatus"),
                "total": (newest.get("amounts") or {}).get("total"),
                "url": newest.get("jobberWebUri"),
                "created": (newest.get("createdAt") or "")[:10],
                "lines": [],
                "via": "backfill (office quoted directly in Jobber)"}
            clouddb.ingest_shadow(stamp, rec)
            updated += 1
            if verbose:
                print(f"  linked {em} → #{newest.get('quoteNumber')} "
                      f"{status}")
    finally:
        jc.DRY_RUN = was
    if verbose:
        print(f"drafts backfill: {updated} rows linked to Jobber truth")
    return updated


if __name__ == "__main__":
    sync(verbose=True)
    backfill_drafts(verbose=True)
