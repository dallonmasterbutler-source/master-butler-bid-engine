"""
MASTER BUTLER — NIGHTLY REPORT SHELF (Dallon, Jul 12: "nightly is the
best option… make it compact and only pertinent info in each card").

Runs once a night (3 AM Pacific gate in the poller). Each builder is
ISOLATED — one failing report can never break the rest — and writes
into ONE compact blob the Scoreboard reads in milliseconds:

  report_shelf = {"at": iso, "cards": [
      {"id", "title", "head",      # the one big number
       "sub",                      # one line under it
       "lines": [...],             # at most three short bullets
       "bars": [(label, value)]}]} # optional mini bar row

Adding report #7 someday = one builder function here. Nothing else.
"""

import collections
import datetime
import re


def _month_key(iso):
    return (iso or "")[:7]


def _build_revenue(clouddb):
    """This month vs the same month last year, by service — from the
    already-mined invoice history (33k invoices)."""
    hist = (clouddb.get_blob("service_history") or {}).get("by_client") or {}
    now = datetime.date.today()
    this_m = now.strftime("%Y-%m")
    last_y = f"{now.year - 1}-{now.strftime('%m')}"
    cur = collections.Counter()
    prev_total = 0.0
    monthly = collections.Counter()          # 12-month shape, all services
    for _client, svcs in hist.items():
        for svc, entries in svcs.items():
            for d, p in entries:
                mk = _month_key(d)
                if mk == this_m:
                    cur[svc] += p
                elif mk == last_y:
                    prev_total += p
                yr_ago = (now.replace(day=1)
                          - datetime.timedelta(days=364)).strftime("%Y-%m")
                if yr_ago <= mk <= this_m:
                    monthly[mk] += p
    total = sum(cur.values())
    # honest comparison: this month SO FAR vs the SAME number of days
    # last year would need day-level data; instead show last year's
    # full month as the target to beat — never a fake % collapse
    top = [f"{svc} ${v:,.0f}" for svc, v in cur.most_common(3)]
    months = sorted(monthly)[-12:]
    return {
        "id": "season", "title": "📆 Revenue by season",
        "head": f"${total:,.0f}",
        "sub": (f"{now:%B} so far · all of {now:%B} {now.year - 1} did "
                f"${prev_total:,.0f}" if prev_total
                else f"{now:%B} so far"),
        "lines": top,
        "bars": [(m[5:], round(monthly[m])) for m in months]}


def _build_speed(clouddb):
    """Request → quote speed, last 30 days."""
    import scoreboard as sb
    quotes = sb.fetch_recent_quotes(150)
    qbyem = {}
    for q in quotes:
        for e in ((q.get("client") or {}).get("emails") or []):
            em = (e.get("address") or "").lower()
            ca = q.get("createdAt")
            if em and ca and (em not in qbyem or ca < qbyem[em]):
                qbyem[em] = ca
    gaps = []
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=30)).strftime("%Y%m%d")
    for stamp, rec in clouddb.all_shadow():
        if stamp[:8] < cutoff or rec.get("merged_into") \
                or rec.get("kind") == "jobber_event":
            continue
        m = re.search(r"<([^>]+)>", rec.get("from") or "")
        em = m.group(1).lower() if m else None
        if not em or em not in qbyem:
            continue
        try:
            r = datetime.datetime.fromisoformat(
                f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
                f"T{stamp[9:11]}:{stamp[11:13]}:00-07:00")
            qt = datetime.datetime.fromisoformat(
                qbyem[em].replace("Z", "+00:00"))
            h = (qt - r).total_seconds() / 3600
            if 0 < h < 24 * 21:
                gaps.append(h)
        except Exception:
            continue
    if not gaps:
        return {"id": "speed", "title": "⚡ Speed to quote",
                "head": "—", "sub": "no matched request→quote pairs "
                "in the last 30 days", "lines": []}
    gaps.sort()
    med = gaps[len(gaps) // 2]
    return {
        "id": "speed", "title": "⚡ Speed to quote",
        "head": (f"{med:.1f}h" if med >= 1 else f"{med * 60:.0f}m"),
        "sub": "median, request → quote sent · last 30 days",
        "lines": [
            f"{sum(1 for g in gaps if g <= 1)} of {len(gaps)} inside "
            f"an hour",
            f"{sum(1 for g in gaps if g > 48)} took over two days"]}


def _build_repeat(clouddb):
    """Share of this month's revenue from returning customers."""
    hist = (clouddb.get_blob("service_history") or {}).get("by_client") or {}
    this_m = datetime.date.today().strftime("%Y-%m")
    rep = new = 0.0
    for _client, svcs in hist.items():
        dates = sorted(d for entries in svcs.values()
                       for d, _p in entries)
        if not dates:
            continue
        cur = sum(p for entries in svcs.values()
                  for d, p in entries if _month_key(d) == this_m)
        if not cur:
            continue
        if _month_key(dates[0]) < this_m:
            rep += cur
        else:
            new += cur
    tot = rep + new
    pct = (rep / tot * 100) if tot else 0
    return {
        "id": "repeat", "title": "🔁 Repeat customers",
        "head": f"{pct:.0f}%",
        "sub": "of this month's revenue is returning customers",
        "lines": [f"${rep:,.0f} returning · ${new:,.0f} first-timers"]}


def _build_whylose(clouddb):
    """Lost-quote picture — grows real as the Declined lane fills."""
    import scoreboard as sb
    quotes = sb.fetch_recent_quotes(150)
    lost = [q for q in quotes
            if (q.get("quoteStatus") or "").lower() == "archived"]
    lost_val = sum(float((q.get("amounts") or {}).get("total") or 0)
                   for q in lost)
    bands = collections.Counter()
    for q in lost:
        t = float((q.get("amounts") or {}).get("total") or 0)
        bands["under $300" if t < 300 else
              "$300–600" if t < 600 else "over $600"] += 1
    ml = clouddb.get_blob("manual_lanes") or {}
    declined = sum(1 for v in ml.values()
                   if v.get("lane") == "declined")
    return {
        "id": "whylose", "title": "📉 Why we lose",
        "head": f"${lost_val:,.0f}",
        "sub": f"{len(lost)} archived quotes in the last 150",
        "lines": ([f"{n} lost {b}" for b, n in bands.most_common(3)]
                  + [f"{declined} marked Declined by the office — the "
                     "real why-report builds as this grows"])}


def _build_shield(clouddb):
    """What the spam filter ate this week."""
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=7)).strftime("%Y%m%d")
    ate = sum(1 for stamp, rec in clouddb.all_shadow()
              if stamp[:8] >= cutoff and rec.get("spam_auto"))
    taught = len(clouddb.get_blob("learned_spam") or [])
    return {
        "id": "shield", "title": "🛡 Spam shield",
        "head": str(ate),
        "sub": "junk emails filed automatically this week",
        "lines": [f"{taught} senders taught by one office click"]}


BUILDERS = (_build_revenue, _build_speed, _build_repeat,
            _build_whylose, _build_shield)


def build(save=True):
    import clouddb
    if not clouddb.available():
        return None
    cards = []
    for fn in BUILDERS:
        try:
            c = fn(clouddb)
            if c:
                cards.append(c)
        except Exception as ex:
            print(f"  (report {fn.__name__} skipped: {ex})")
    shelf = {"at": datetime.datetime.now(datetime.timezone.utc)
             .isoformat(timespec="seconds"), "cards": cards}
    if save and cards:
        clouddb.put_blob("report_shelf", shelf)
    print(f"report shelf: {len(cards)} cards built")
    return shelf


if __name__ == "__main__":
    build()
