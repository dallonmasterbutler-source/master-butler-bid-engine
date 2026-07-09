"""
MASTER BUTLER — MORNING BRIEF

One page, plain English: what came in, what's waiting, how the shadow
system scored, what needs Dallon. Written to data/briefs/ and printed.

Run:  python3 digest.py        (night_run also generates it)
"""

import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
BRIEFS = BASE / "data" / "briefs"


def build():
    import dashboard as db

    lines = [f"MASTER BUTLER — MORNING BRIEF · {datetime.now():%A, %B %d, %Y}",
             "=" * 56, ""]

    pin = BASE / "data" / "brief_pin.txt"
    if pin.exists() and pin.read_text().strip():
        lines.append(pin.read_text().rstrip())
        lines.append("")
        lines.append("=" * 56)
        lines.append("")

    bids = db.load_bids()
    live_holds, resurfaced = db.active_holds()
    queue = [b for b in bids if not b["reviewed"]
             and b["stamp"] not in live_holds]
    new_requests = [b for b in queue if b.get("kind") == "new_request"]
    oldest = max((b["age_hours"] for b in queue), default=0)

    lines.append(f"QUEUE: {len(queue)} item(s) waiting "
                 f"({len(new_requests)} real requests) — oldest has waited "
                 f"{oldest:.0f}h" + ("  ⚠ past 24h SLA" if oldest >= 24 else ""))
    for b in queue[:8]:
        mark = " [SPAM-FOUND]" if b.get("office_alert") else ""
        dup = " [DUP?]" if b.get("duplicate_of") else ""
        lines.append(f"  · {b['from'][:44]} — {b.get('kind')}"
                     f"{mark}{dup} ({b['age_hours']:.0f}h)")

    if resurfaced:
        lines.append("")
        lines.append("BACK FROM HOLD (answer these first):")
        for s, h in resurfaced.items():
            lines.append(f"  ⏰ {h.get('customer', s)} — {h.get('hold_reason')}")

    sb_path = BASE / "data" / "scoreboard.json"
    if sb_path.exists():
        sb = json.loads(sb_path.read_text())
        matched = [r for r in sb["rows"] if r.get("office_quote")]
        lines.append("")
        if matched:
            close = sum(1 for r in matched
                        if r.get("gap_pct") is not None
                        and abs(r["gap_pct"]) <= 10)
            lines.append(f"SCOREBOARD: {len(matched)} compared — "
                         f"{close} within 10% of the office.")
            for r in matched[:6]:
                lines.append(f"  · {(r.get('customer') or '?')[:34]}: "
                             f"sys ${r['system_total']:.0f} vs office "
                             f"${r['office_total']:.0f} ({r['gap_pct']:+.0f}%)")
        else:
            waiting = sum(1 for r in sb["rows"] if not r.get("office_quote"))
            lines.append(f"SCOREBOARD: {waiting} shadow draft(s) still "
                         "waiting for office quotes.")

    # WINS + campaign + conversation glance (all from shared blobs)
    try:
        sbs = db.scoreboard_status()
        wins = [s for s, st in sbs.items()
                if (st or "").lower() in ("approved", "converted")]
        if wins:
            lines.append("")
            lines.append(f"🏆 WON: {len(wins)} quote(s) approved by "
                         "customers — see the scoreboard.")
    except Exception:
        pass
    try:
        wb = db._winback_done()
        reb = sum(1 for v in wb.values() if v.get("outcome") == "rebooked")
        if wb:
            lines.append(f"📞 Win-back: {len(wb)} contacted, "
                         f"{reb} REBOOKED so far.")
    except Exception:
        pass
    try:
        import msglog
        marks = db._msg_read()
        unread = sum(1 for a, n, ms in msglog.threads()
                     if ms[-1]["at"] > marks.get(a, ""))
        if unread:
            lines.append(f"💬 {unread} conversation(s) waiting on the "
                         "Messages tab.")
    except Exception:
        pass
    try:
        ar = db._blob_rw("auto_reviews", {})
        recent_ar = [v for v in ar.values() if v.get("summary")][-3:]
        if recent_ar:
            lines.append("")
            lines.append("📖 WHAT THE SYSTEM LEARNED (auto-reviews):")
            for v in recent_ar:
                lines.append(f"  · {v['summary'][:90]}")
    except Exception:
        pass

    reviews = db.load_reviews()
    today = datetime.now().date().isoformat()
    recent = [r for r in reviews if (r.get("at") or "").startswith(today)]
    if recent:
        lines.append("")
        lines.append(f"DECISIONS LOGGED TODAY: {len(recent)}")
        taught = [r for r in recent if r.get("reason") or r.get("note")]
        if taught:
            lines.append(f"  ({len(taught)} came with a teaching reason/note)")

    try:
        flagged = db.flagged_for_review()
        if flagged:
            lines.append("")
            lines.append(f"🚩 SENT TO TOM & DALLON ({len(flagged)} waiting):")
            for f in flagged[:6]:
                lines.append(f"  · {f.get('customer', f.get('stamp'))}")
    except Exception:
        pass

    try:
        ideas = [x for x in db.load_ideas() if x.get("status") == "open"]
        if ideas:
            lines.append("")
            lines.append(f"💡 IDEAS FROM THE OFFICE ({len(ideas)} open):")
            for x in ideas[:6]:
                lines.append(f"  · {x['who']}: {x['text'][:90]}")
    except Exception:
        pass

    lines += ["", "STANDING FLAGS FOR DALLON:",
              "  · Jobber portal: add Users-read scope (salesperson labels)",
              "  · Jobber: archive test quotes #36577/78, #36582-87, #36593",
              "  · CopyCall ticket (FOR-JESSICA on Desktop) = free voicemail audio",
              "  · Martha: roof-blow-off-solo policy · Techs: grades #36582-87",
              "  · Messages send stays OFF (REPLIES_ENABLED) until you rule",
              "  · PUSH_ON_APPROVE stays OFF until you flip it", ""]
    return "\n".join(lines)


def write():
    BRIEFS.mkdir(parents=True, exist_ok=True)
    text = build()
    path = BRIEFS / f"brief-{datetime.now():%Y%m%d}.txt"
    path.write_text(text)
    return path, text


if __name__ == "__main__":
    path, text = write()
    print(text)
    print(f"(saved -> {path})")
