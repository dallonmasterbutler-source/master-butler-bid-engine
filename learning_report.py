"""
MASTER BUTLER — THE LEARNING REPORT (Dallon, Jul 12: "a spot where we
can see the visible learning reports… probably the scoreboard, since
we're trying to learn from the quotes we win/lose — and the money
we're losing").

Built hourly by the poller into the `learning_report` blob; the
Scoreboard renders it. Three chapters:

  money    — win rate, dollars sitting in unanswered quotes (and how
             much has gone stale), dollars past due
  learning — everything the office has taught the system: house-fact
             corrections, spam senders, price floors raised, declines
  sorting  — the lane ladder's report card: how often the office
             overrides it, and any repeated pattern that deserves a
             rule change (proposed to Dallon, never auto-applied)
"""

import collections
import datetime


def build(save=True):
    import clouddb
    if not clouddb.available():
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    rep = {"at": now.isoformat(timespec="seconds")}

    # ── money: the quote funnel ──
    try:
        import scoreboard as sb
        quotes = sb.fetch_recent_quotes(150)
        stat = collections.Counter()
        await_n = await_val = stale_n = stale_val = won_val = 0
        for q in quotes:
            s = (q.get("quoteStatus") or "").lower()
            stat[s] += 1
            tot = float((q.get("amounts") or {}).get("total") or 0)
            if s == "awaiting_response":
                await_n += 1
                await_val += tot
                try:
                    t = datetime.datetime.fromisoformat(
                        q["createdAt"].replace("Z", "+00:00"))
                    if (now - t).days >= 7:
                        stale_n += 1
                        stale_val += tot
                except Exception:
                    pass
            elif s in ("approved", "converted"):
                won_val += tot
        won_n = stat["approved"] + stat["converted"]
        decided = won_n + stat["archived"]
        rep["money"] = {
            "quotes": len(quotes), "won_n": won_n,
            "win_rate": round(won_n / max(1, decided + await_n) * 100),
            "won_val": round(won_val),
            "awaiting_n": await_n, "awaiting_val": round(await_val),
            "stale_n": stale_n, "stale_val": round(stale_val)}
    except Exception:
        pass

    # past-due invoices — one cheap query
    try:
        import jobber_client as jc
        was, jc.DRY_RUN = jc.DRY_RUN, False
        try:
            d = jc._post("""query { past: invoices(filter: {status:
                past_due}, first: 30) { totalCount
                nodes { amounts { invoiceBalance } } } }""",
                {}, "past due")
        finally:
            jc.DRY_RUN = was
        pd = d.get("past") or {}
        rep["pastdue"] = {
            "n": pd.get("totalCount") or 0,
            "val": round(sum(float((x.get("amounts") or {})
                                   .get("invoiceBalance") or 0)
                             for x in pd.get("nodes", [])))}
    except Exception:
        pass

    # ── learning: what the office has taught the system ──
    try:
        facts = clouddb.get_blob("fact_overrides") or {}
        spam = clouddb.get_blob("learned_spam") or []
        reviews = clouddb.all_reviews()
        floors = sum(1 for r in reviews
                     if r.get("action") == "price_floor")
        rep["learning"] = {
            "fact_corrections": len([k for k in facts
                                     if not str(k).startswith("_")]),
            "spam_senders": len(spam),
            "floors_raised": floors}
    except Exception:
        pass

    # ── sorting: the lane ladder's report card ──
    try:
        diary = clouddb.get_blob("lane_corrections") or []
        cutoff = (now - datetime.timedelta(days=14)) \
            .isoformat(timespec="seconds")
        recent = [d for d in diary if (d.get("at") or "") >= cutoff]
        by_to = collections.Counter(d.get("to") for d in recent
                                    if d.get("to") != "auto")
        proposals = [
            {"pattern": to, "n": n,
             "text": (f"The office filed {n} cards under '{to}' by "
                      "hand in 2 weeks — the ladder may need a rule "
                      "here. Tell Claude to look at the diary.")}
            for to, n in by_to.items() if n >= 5]
        rep["sorting"] = {
            "moves_14d": len(recent),
            "by_lane": dict(by_to),
            "proposals": proposals,
            "last": [{"at": d.get("at", "")[:16], "by": d.get("by"),
                      "key": d.get("key", "")[:34], "to": d.get("to")}
                     for d in recent[-5:]]}
    except Exception:
        pass

    # ── history: one snapshot per day → sparklines and trend arrows
    # on the Scoreboard grow by themselves ──
    try:
        old = clouddb.get_blob("learning_report") or {}
        hist = old.get("history") or []
        today = now.date().isoformat()
        mo = rep.get("money") or {}
        snap = {"d": today, "win": mo.get("win_rate"),
                "awaiting": mo.get("awaiting_val"),
                "stale": mo.get("stale_val"),
                "pastdue": (rep.get("pastdue") or {}).get("val")}
        if hist and hist[-1].get("d") == today:
            hist[-1] = snap
        else:
            hist.append(snap)
        rep["history"] = hist[-30:]
    except Exception:
        pass

    if save:
        try:
            clouddb.put_blob("learning_report", rep)
        except Exception:
            pass
    return rep


if __name__ == "__main__":
    import json
    print(json.dumps(build(), indent=1))
