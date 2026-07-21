"""
MASTER BUTLER — THE SELF-REVIEW (Dallon, Jul 15: 'a shadow program to
periodically review all our work in the dashboard and tell us where we
are failing the most. Time, bids, language, etc.')

Nightly (and on demand), grade OURSELVES across every signal the
system already collects, and rank the failures:

  ⏱ TIME      customer inbound → our first reply, last 30 days
  💰 BIDS      our draft vs the office's real quote (scoreboard) and
               vs Dallon/Tom's rulings (calibration ledger) — which
               SERVICE misses most, and in which direction
  🏠 FACTS     which house fact the office corrects most (fact
               overrides) — where the lookups let us down
  🗣 LANGUAGE  reply drafts vs what actually goes out (draft_sends /
               draft_learnings) — the wording the office keeps fixing
  🧭 SORTING   what the hourly reviewer keeps flagging (stale rows,
               spam leaks, overparse) — where the queue lies

Verdicts are plain English, worst first. History accumulates in the
failure_history blob so trends show. Read-only; nothing moves.
"""

import collections
import json
import re
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).parent

INTERNAL = ("masterbutlerinc.com", "dallon.masterbutler",
            "tomfricke2007", "getjobber.com", "copycall.com")


def _utc(at):
    try:
        t = datetime.fromisoformat(at)
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
    except (ValueError, TypeError):
        return None


def _customer_emails(clouddb):
    """Emails with a real (non-aside) queue record — spam, vendors and
    robot mail never count as 'a customer we ignored'. Returns
    {email: jobber_quote_status_or_None} so every claim in this report
    is CROSS-REFERENCED WITH JOBBER (Dallon, Jul 15): a customer with
    a quote in Jobber was answered — through Jobber — even when no
    Gmail reply exists."""
    ok = {}
    try:
        from dashboard import classify_row
        for _s, rec in sorted(clouddb.all_shadow()):
            m = re.search(r"<([^>]+)>", rec.get("from") or "")
            if m and classify_row(rec)[0] == "main":
                e = m.group(1).lower()
                oq = ((rec.get("open_quote_ctx") or {}).get("status")
                      or "").lower() or None
                ok[e] = oq or ok.get(e)     # newest record wins a status
    except Exception:
        return None
    return ok


def _time_section(now, customers=None, gmail_done=(), spam_senders=()):
    """Inbound → first reply latency, 30 days. Threads the office
    finished in Gmail (trash) or answered via a Jobber quote email
    don't count as ignored — no EMAIL reply is not no reply. SPAM never
    counts as a missed customer either (Jessica #17: Elaine Delaney was
    flagged as 'never replied' — she was a solicitation we correctly
    ignored)."""
    import msglog
    import spam_filter
    lat, unanswered, jobber_answered = [], [], []
    for addr, name, msgs in msglog.threads():
        a = (addr or "").lower()
        if not a or a.startswith("vm:") or any(x in a for x in INTERNAL):
            continue
        if customers is not None and a not in customers:
            continue
        # SPAM is not a missed lead — skip office-taught junk senders and
        # anything the solicitation/robot filter flags (uses the newest
        # inbound's words). We never owed them a reply.
        if any(s and s in a for s in spam_senders):
            continue
        _li = next((m for m in reversed(msgs)
                    if m.get("dir") == "in"), None)
        if _li and spam_filter.looks_spam(
                a, _li.get("subject"), _li.get("body"))[0]:
            continue
        pend = None
        for m in msgs:
            t = _utc(m.get("at"))
            if not t or now - t > timedelta(days=30):
                continue
            if m.get("dir") == "in":
                pend = pend or t              # first unanswered inbound
            elif pend:
                lat.append(((t - pend).total_seconds() / 3600, name))
                pend = None
        if pend and a not in gmail_done:
            # JOBBER CROSS-REFERENCE: a quote on file = they were
            # answered through Jobber, not ignored
            _oq = (customers or {}).get(a) if isinstance(customers,
                                                         dict) else None
            if _oq:
                jobber_answered.append((a, _oq))
            else:
                unanswered.append(((now - pend).total_seconds() / 3600,
                                   name or a))
    hrs = sorted(h for h, _ in lat)
    out = {"n": len(hrs), "unanswered": len(unanswered),
           "answered_via_jobber": len(jobber_answered)}
    if hrs:
        out["median_h"] = round(statistics.median(hrs), 1)
        out["p90_h"] = round(hrs[int(len(hrs) * .9) - 1], 1)
        out["within_4h"] = round(sum(1 for h in hrs if h <= 4)
                                 / len(hrs) * 100)
        out["within_24h"] = round(sum(1 for h in hrs if h <= 24)
                                  / len(hrs) * 100)
        out["slowest"] = [{"who": n, "hours": round(h)} for h, n in
                          sorted(lat, reverse=True)[:5]]
    out["oldest_unanswered"] = [
        {"who": n, "hours": round(h)} for h, n in
        sorted(unanswered, reverse=True)[:5]]
    return out


def _bids_section(clouddb):
    """Where our prices miss the office's."""
    sb = clouddb.get_blob("scoreboard") or {}
    bysvc = collections.defaultdict(list)
    worst, overparsed = [], 0
    for r in sb.get("rows") or []:
        gp, ot = r.get("gap_pct"), r.get("office_total")
        if gp is None or not ot:
            continue
        svcs = r.get("services") or ["?"]
        # OVERPARSE rows are PARSER failures, not pricing failures —
        # Irene Hwang's 10-service $1,408 draft (office: $246) was
        # getting pinned on whatever service happened to be listed
        # first, inventing a 'moss removal prices 160% high' verdict
        # (Dallon caught it, Jul 15). Track them as their own bucket.
        if len(svcs) >= 7:
            overparsed += 1
            continue
        for s in svcs:                 # every service on the bid owns
            bysvc[s].append(gp)        # a share of the miss, not just
        if abs(gp) > 10:               # the first-listed one
            worst.append({"who": (r.get("customer") or "?")
                          .split("<")[0].strip()[:30],
                          "gap_pct": gp, "ours": r.get("system_total"),
                          "office": ot})
    svc_rank = []
    for s, gaps in bysvc.items():
        if len(gaps) < 2:
            continue
        svc_rank.append({
            "service": s, "n": len(gaps),
            "avg_abs_gap": round(sum(abs(g) for g in gaps) / len(gaps), 1),
            "bias": round(sum(gaps) / len(gaps), 1)})
    # RANK BY BIAS, not scatter (Dallon, Jul 20). avg_abs_gap punished
    # correctly-flat services like dryer vent for the natural spread around
    # a set price — and each service inherits the WHOLE bid's gap, so a
    # cheap consistent line took the blame for big bids' misses. Bias (the
    # systematic over/under direction) is what says "we're actually
    # mispricing this"; that's what should top the list.
    svc_rank.sort(key=lambda x: -abs(x["bias"]))
    cl = clouddb.get_blob("calibration_ledger") or {}
    rulings = {s: len(v) for s, v in cl.items() if v}
    matched = [r for r in (sb.get("rows") or []) if r.get("office_total")
               and r.get("gap_pct") is not None
               and len(r.get("services") or []) < 7]
    within10 = (round(sum(1 for r in matched if abs(r["gap_pct"]) <= 10)
                      / len(matched) * 100) if matched else None)
    return {"within_10pct": within10, "n_matched": len(matched),
            "overparsed_excluded": overparsed,
            "by_service": svc_rank[:8], "worst": sorted(
                worst, key=lambda x: -abs(x["gap_pct"]))[:8],
            "rulings_by_service": rulings}


def _facts_section(clouddb):
    fo = clouddb.get_blob("fact_overrides") or {}
    fields = collections.Counter(
        k for v in fo.values() for k in v if not k.startswith("_"))
    return {"homes_corrected": len(fo),
            "by_field": dict(fields.most_common())}


def _language_section(clouddb):
    sends = clouddb.get_blob("draft_sends") or []
    dl = clouddb.get_blob("draft_learnings") or {}
    bykind = collections.defaultdict(list)
    for s in sends:
        bykind[s.get("kind") or "?"].append(s.get("acc") or 0)
    kinds = [{"kind": k, "n": len(v),
              "avg_acc": round(sum(v) / len(v))} for k, v in
             bykind.items()]
    kinds.sort(key=lambda x: x["avg_acc"])
    fixes = []
    for kind, store in dl.items():
        for sent, cnt in sorted((store.get("added") or {}).items(),
                                key=lambda kv: -kv[1])[:2]:
            if cnt >= 2:
                fixes.append({"kind": kind, "count": cnt,
                              "office_says": sent[:120]})
    return {"live_sends": len(sends), "by_kind": kinds,
            "top_office_fixes": sorted(fixes,
                                       key=lambda x: -x["count"])[:6]}


def _sorting_section(clouddb):
    lr = clouddb.get_blob("lane_review") or {}
    checks = collections.Counter(
        f.get("check") for f in lr.get("findings") or [])
    return {"open_flags": dict(checks.most_common()),
            "as_of": lr.get("at")}


def _verdicts(R):
    """Plain-English 'where we're failing most', worst first."""
    v = []
    t = R["time"]
    if t.get("n") and t.get("within_24h", 100) < 80:
        v.append(f"⏱ Only {t['within_24h']}% of customers hear back "
                 f"within a day (median {t.get('median_h')}h) — speed "
                 "is the biggest lever on close rate.")
    if t.get("unanswered"):
        v.append(f"⏱ {t['unanswered']} customers from the last 30 days "
                 "have NO reply by email AND no quote in Jobber — "
                 f"truly ignored (another {t.get('answered_via_jobber', 0)} "
                 "were answered through Jobber only). The hourly 🕰 "
                 "check names them.")
    b = R["bids"]
    if b.get("within_10pct") is not None and b["within_10pct"] < 60:
        v.append(f"💰 Only {b['within_10pct']}% of our drafts land "
                 "within ±10% of the office's real quote "
                 f"({b['n_matched']} matched).")
    if b.get("by_service"):
        s0 = b["by_service"][0]
        d = "HIGH" if s0["bias"] > 0 else "LOW"
        v.append(f"💰 Worst-priced service: {s0['service']} — off by "
                 f"{s0['avg_abs_gap']}% on average across {s0['n']} "
                 f"quotes, usually too {d}.")
    f = R["facts"]
    if f.get("by_field"):
        k0, n0 = next(iter(f["by_field"].items()))
        v.append(f"🏠 Most-corrected house fact: {k0} ({n0} of "
                 f"{f['homes_corrected']} corrected homes) — the "
                 "lookups need help there.")
    lg = R["language"]
    if lg.get("by_kind"):
        k0 = lg["by_kind"][0]
        if k0["avg_acc"] < 70:
            v.append(f"🗣 Weakest reply type: {k0['kind']} — the office "
                     f"rewrites it to {k0['avg_acc']}% similarity "
                     f"({k0['n']} sends). Its wording needs adopting.")
    elif not lg.get("live_sends"):
        v.append("🗣 No live pre-filled sends graded yet — the office "
                 "hasn't used the box; that's the missing feedback "
                 "loop, not a wording problem.")
    s = R["sorting"]
    if s.get("open_flags"):
        top = next(iter(s["open_flags"].items()))
        v.append(f"🧭 Most common queue flag right now: {top[0]} "
                 f"(×{top[1]}) — see the hourly review card.")
    return v


def run(verbose=False):
    import clouddb
    if not clouddb.available():
        return {}
    now = datetime.now(timezone.utc)
    R = {"at": now.isoformat(timespec="seconds"),
         "time": _time_section(
             now, _customer_emails(clouddb),
             {e for e, s in (clouddb.get_blob("gmail_state")
                             or {}).items()
              if (s or {}).get("state") == "done"},
             spam_senders=tuple(clouddb.get_blob("learned_spam") or [])),
         "bids": _bids_section(clouddb),
         "facts": _facts_section(clouddb),
         "language": _language_section(clouddb),
         "sorting": _sorting_section(clouddb)}
    R["verdicts"] = _verdicts(R)
    clouddb.put_blob("failure_review", R)
    hist = clouddb.get_blob("failure_history") or []
    hist.append({"at": R["at"],
                 "median_reply_h": R["time"].get("median_h"),
                 "within_24h": R["time"].get("within_24h"),
                 "bid_within_10pct": R["bids"].get("within_10pct"),
                 "unanswered": R["time"].get("unanswered")})
    clouddb.put_blob("failure_history", hist[-90:])
    if verbose:
        for x in R["verdicts"]:
            print("  ", x)
    return R


if __name__ == "__main__":
    out = run(verbose=True)
    print(json.dumps({k: out[k] for k in ("time", "bids")}, indent=1)[:1500])
