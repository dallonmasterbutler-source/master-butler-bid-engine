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
            rec["open_quote_ctx"] = {
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


if __name__ == "__main__":
    sync(verbose=True)
