"""
MASTER BUTLER — MORNING BRIEF

One page, plain English: what came in, what's waiting, how the shadow
system scored, what needs Dallon. build_data() makes the STRUCTURE;
build() renders it as text (nightly email + data/briefs/ file) and the
dashboard's 📋 Brief tab renders the same structure as readable cards
(Dallon, Jul 9 pm: "it looks like a block of text").

Run:  python3 digest.py        (night_run also generates it)
"""

import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
BRIEFS = BASE / "data" / "briefs"


def build_data():
    """The brief as structure: {'date', 'pin': [bullets],
    'sections': [{'icon','title','sub','items':[str]}]}."""
    import dashboard as db

    data = {"date": f"{datetime.now():%A, %B %d, %Y}",
            "pin": [], "sections": []}

    pin = BASE / "data" / "brief_pin.txt"
    txt = ""
    if pin.exists():
        txt = pin.read_text()
    else:
        try:                       # the cloud has no files — blob copy
            import clouddb
            if clouddb.available():
                txt = clouddb.get_blob("brief_pin") or ""
        except Exception:
            pass
    if txt.strip():
        # the pin file is hand-written with '· ' bullets over wrapped
        # lines — reflow into one string per bullet
        cur = []
        for ln in txt.splitlines():
            s = ln.strip()
            if not s or s.startswith("📌"):
                continue
            if s.startswith("·"):
                if cur:
                    data["pin"].append(" ".join(cur))
                cur = [s.lstrip("· ")]
            else:
                cur.append(s)
        if cur:
            data["pin"].append(" ".join(cur))

    def sec(icon, title, sub="", items=None):
        if items is not None and not items:
            return
        data["sections"].append({"icon": icon, "title": title,
                                 "sub": sub, "items": items or []})

    bids = db.load_bids()
    live_holds, resurfaced = db.active_holds()
    queue = [b for b in bids if not b["reviewed"]
             and b["stamp"] not in live_holds
             and db.classify_row(b)[0] == "main"]  # office work only —
    # robots/internal/spam sit in the drawer, not the morning number
    new_requests = [b for b in queue if b.get("kind") == "new_request"]
    oldest = max((b["age_hours"] for b in queue), default=0)
    sec("📥", f"Queue: {len(queue)} waiting",
        f"{len(new_requests)} real requests · oldest {oldest:.0f}h"
        + ("  ⚠ past 24h SLA" if oldest >= 24 else ""),
        [f"{(b['from'] or '')[:44]} — {b.get('kind')}"
         + (" ⚠ alert" if b.get("office_alert") else "")
         + (" [dup?]" if b.get("duplicate_of") else "")
         + f" ({b['age_hours']:.0f}h)" for b in queue[:8]])

    if resurfaced:
        sec("⏰", "Back from hold — answer these first", "",
            [f"{h.get('customer', s)} — {h.get('hold_reason')}"
             for s, h in resurfaced.items()])

    try:
        sb = (db.clouddb.get_blob("scoreboard")
              if db.clouddb.available() else None)
        if not sb:
            p = BASE / "data" / "scoreboard.json"
            sb = json.loads(p.read_text()) if p.exists() else None
    except Exception:
        sb = None
    if sb:
        matched = [r for r in sb["rows"] if r.get("office_quote")]
        if matched:
            close = sum(1 for r in matched
                        if r.get("gap_pct") is not None
                        and abs(r["gap_pct"]) <= 10)
            sec("📊", f"Scoreboard: {len(matched)} compared",
                f"{close} within 10% of the office",
                [f"{(r.get('customer') or '?')[:34]}: system "
                 f"${r['system_total']:,.0f} vs office "
                 f"${r['office_total']:,.0f} ({r['gap_pct']:+.0f}%)"
                 for r in matched[:6]])
        else:
            waiting = sum(1 for r in sb["rows"] if not r.get("office_quote"))
            sec("📊", "Scoreboard",
                f"{waiting} shadow draft(s) waiting for office quotes")

    # quotes out but quiet — the follow-up money (pre-lights ops, Jul 10)
    if sb:
        from datetime import datetime as _dt
        nudge = []
        for r in sb["rows"]:
            if (r.get("office_status") or "").lower() != "awaiting_response":
                continue
            try:
                age = (datetime.now() - _dt.strptime(
                    r["stamp"][:8], "%Y%m%d")).days
            except (KeyError, ValueError):
                continue
            if age >= 5:
                nudge.append((age, r))
        if nudge:
            nudge.sort(reverse=True, key=lambda x: x[0])
            sec("📤", f"Quotes gone quiet ({len(nudge)}) — worth a nudge",
                "sent, no customer response, 5+ days",
                [f"{(r.get('customer') or '?')[:32]} — "
                 f"${r['office_total']:,.0f} · quote "
                 f"#{r['office_quote']} · {age}d since request"
                 for age, r in nudge[:8]])

    glance = []
    try:
        sbs = db.scoreboard_status()
        wins = [s for s, st in sbs.items()
                if (st or "").lower() in ("approved", "converted")]
        if wins:
            glance.append(f"🏆 {len(wins)} quote(s) WON — see the scoreboard")
    except Exception:
        pass
    try:
        wb = db._winback_done()
        reb = sum(1 for v in wb.values() if v.get("outcome") == "rebooked")
        if wb:
            glance.append(f"📞 Win-back: {len(wb)} contacted, "
                          f"{reb} rebooked")
    except Exception:
        pass
    try:
        import msglog
        marks = db._msg_read()
        unread = sum(1 for a, n, ms in msglog.threads()
                     if ms[-1]["at"] > marks.get(a, ""))
        if unread:
            glance.append(f"💬 {unread} conversation(s) unread")
    except Exception:
        pass
    if glance:
        sec("👀", "At a glance", "", glance)

    try:                       # tomorrow's day sheets, precomputed
        from datetime import timedelta
        tmr = (datetime.now() + timedelta(days=1)).date().isoformat()
        import routing
        rd = routing.build_day(tmr, "visits", max_age_min=12 * 60)
        if rd.get("techs"):
            sec("🚐", f"Tomorrow's routes ({tmr})",
                "open the Routes tab for maps + printed order",
                [f"{tech}: {len(t['stops'])} stop(s), "
                 f"{t['drive_min']} min / {t['drive_mi']} mi driving, "
                 f"back {t['back_at']}"
                 for tech, t in rd["techs"].items()])
    except Exception:
        pass

    try:
        ar = db._blob_rw("auto_reviews", {})
        recent_ar = [v for v in ar.values() if v.get("summary")][-3:]
        if recent_ar:
            sec("📖", "What the system learned (auto-reviews)", "",
                [v["summary"][:110] for v in recent_ar])
    except Exception:
        pass

    reviews = db.load_reviews()
    today = datetime.now().date().isoformat()
    recent = [r for r in reviews if (r.get("at") or "").startswith(today)]
    if recent:
        taught = [r for r in recent if r.get("reason") or r.get("note")]
        sec("✅", f"Decisions logged today: {len(recent)}",
            f"{len(taught)} came with a teaching reason" if taught else "")

    try:
        flagged = db.flagged_for_review()
        if flagged:
            sec("🚩", f"With Tom & Dallon ({len(flagged)} waiting)", "",
                [f.get("customer", f.get("stamp")) for f in flagged[:6]])
    except Exception:
        pass

    try:
        ideas = [x for x in db.load_ideas() if x.get("status") == "open"]
        if ideas:
            sec("💡", f"Ideas from the office ({len(ideas)} open)",
                "Claude pre-plans these overnight",
                [f"{x['who']} ({x.get('at', '')[:10]}): {x['text'][:100]}"
                 for x in ideas[:8]])
    except Exception:
        pass

    sec("📌", "Standing flags for Dallon", "", [
        "Jobber: archive TEST quotes #36600/01/02 + tech tests "
        "#36577/78, #36582-87, #36593; then client "
        "'TEST - Carl Fullrun (TEST)'",
        "QUOTING IS LIVE for everyone (drafts only — a human sends "
        "from Jobber)",
        "Martha: roof-blow-off-solo policy · Techs: grades #36582-87",
        "Messages send stays OFF (REPLIES_ENABLED) until you rule",
        "Lights labor evidence: data/lights_calibration.json — set the "
        "anchor with Tom"])
    return data


def build():
    """Text render — the nightly email + data/briefs/ file."""
    d = build_data()
    lines = [f"MASTER BUTLER — MORNING BRIEF · {d['date']}", "=" * 56, ""]
    if d["pin"]:
        lines.append("📌 FOR THE OFFICE THIS MORNING:")
        for b in d["pin"]:
            lines.append(f"  · {b}")
        lines += ["", "=" * 56, ""]
    for s in d["sections"]:
        head = f"{s['icon']} {s['title'].upper()}"
        if s["sub"]:
            head += f" — {s['sub']}"
        lines.append(head)
        for it in s["items"]:
            lines.append(f"  · {it}")
        lines.append("")
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
