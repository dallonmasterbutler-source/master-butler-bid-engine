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
    # NEUTRAL TONE (Dallon, Jul 21): the brief informs, it never tells the
    # office what to do or implies they're behind — just the facts.
    sec("📥", f"Inbox: {len(queue)} open",
        f"{len(new_requests)} new request(s) · oldest {oldest:.0f}h",
        [f"{(b['from'] or '')[:44]} — {b.get('kind')}"
         + (" · change requested" if b.get("office_alert") else "")
         + (" · possible duplicate" if b.get("duplicate_of") else "")
         + f" ({b['age_hours']:.0f}h)" for b in queue[:8]])

    if resurfaced:
        sec("⏰", f"Back from hold ({len(resurfaced)})", "",
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
            sec("📤", f"Open quotes, no reply yet ({len(nudge)})",
                "sent 5+ days ago",
                [f"{(r.get('customer') or '?')[:32]} — "
                 f"${r['office_total']:,.0f} · quote "
                 f"#{r['office_quote']} · {age}d since request"
                 for age, r in nudge[:8]])

    try:                     # the churn counterpunch (Jul 10 cycle)
        due = (db._blob_rw("due_soon", []) or [])
        if due:
            sec("📅", f"Due for their annual: {len(due)} customers",
                f"${sum(r['lifetime'] for r in due):,} lifetime value · "
                "in their usual service window",
                [f"{r['name'][:28]} — last {r['last']} "
                 f"(${r['last_total']:,}), ${r['lifetime']:,} lifetime"
                 for r in due[:5]])
    except Exception:
        pass

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

    # 🎓 WHAT THE SYSTEM LEARNED FROM THE OFFICE (Dallon, Jul 21: 'a small
    # section showing what the brain learned from their hard work — it
    # shows how valuable they are'). Real numbers only, never flattery:
    # the system is only as good as the office's corrections, and this
    # makes that visible.
    try:
        from datetime import timedelta as _td7
        wk = (datetime.now() - _td7(days=7)).date().isoformat()
        learned = []
        revs = db.load_reviews()
        taught = [r for r in revs if (r.get("at") or "")[:10] >= wk
                  and (r.get("reason") or r.get("note"))
                  and (r.get("action") or "") in
                  ("adjusted", "price_edit", "combined", "duplicate_same",
                   "duplicate_new", "lane_move", "customer_flag",
                   "escalated", "must_know")]
        if taught:
            learned.append(
                f"You corrected or filed {len(taught)} thing(s) this week — "
                "the system studies every one and adjusts.")
        n_spam = len(db._learned_spam() or [])
        if n_spam:
            learned.append(
                f"{n_spam} junk sender(s) you've flagged now get filtered "
                "automatically, so they stay out of your inbox for good.")
        if sb:
            _m = [r for r in sb["rows"] if r.get("gap_pct") is not None]
            if len(_m) >= 5:
                _c = round(sum(1 for r in _m if abs(r["gap_pct"]) <= 10)
                           / len(_m) * 100)
                learned.append(
                    f"The system's quotes now land within 10% of yours on "
                    f"{_c}% of bids — because it's calibrated to how YOU "
                    "price, not a generic formula.")
        if learned:
            sec("🎓", "What the system learned from you",
                "it's smart because you taught it", learned)
    except Exception:
        pass

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
        lines.append("📌 NOTES:")
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


# sections that are Dallon-only plumbing — the office shouldn't have to
# read past them. Everything else is office-facing.
_DALLON_ONLY = {"Standing flags for Dallon"}


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def build_html(data=None):
    """The brief as a scannable HTML email (Dallon, Jul 21: 'a huge block
    of text that was hard for the office to understand'). Reuses the same
    build_data() structure the dashboard's Brief tab renders — clean
    cards, one glanceable number per section, office info first and
    Dallon's plumbing fenced off at the bottom. Email-safe: inline styles
    only, table-centered, no fl* layout."""
    d = data or build_data()
    G, GOLD, INK, MUT, LINE, BG = ("#1f6b47", "#b8860b", "#16211b",
                                   "#5c6b62", "#e2e7e1", "#f4f6f3")

    def card(s, dallon=False):
        acc = GOLD if dallon else G
        items = ""
        for it in s["items"]:
            items += (
                f"<tr><td style='padding:5px 0;border-top:1px solid {LINE};"
                f"font-size:14px;line-height:1.45;color:{INK}'>"
                f"{_esc(it)}</td></tr>")
        sub = (f"<div style='font-size:13px;color:{MUT};margin-top:2px'>"
               f"{_esc(s['sub'])}</div>" if s.get("sub") else "")
        body = (f"<table width='100%' cellpadding='0' cellspacing='0' "
                f"style='margin-top:8px'>{items}</table>" if s["items"]
                else "")
        return (
            f"<tr><td style='padding:16px 20px'>"
            f"<table width='100%' cellpadding='0' cellspacing='0'>"
            f"<tr><td style='font-size:20px;width:30px;vertical-align:top'>"
            f"{s['icon']}</td>"
            f"<td><div style='font-size:16px;font-weight:700;color:{acc}'>"
            f"{_esc(s['title'])}</div>{sub}{body}</td></tr></table>"
            f"</td></tr>")

    office = [s for s in d["sections"] if s["title"] not in _DALLON_ONLY]
    dallon = [s for s in d["sections"] if s["title"] in _DALLON_ONLY]

    pin = ""
    if d["pin"]:
        rows = "".join(
            f"<div style='font-size:14.5px;line-height:1.5;color:{INK};"
            f"margin:3px 0'>• {_esc(b)}</div>" for b in d["pin"])
        pin = (f"<tr><td style='padding:14px 20px'>"
               f"<table width='100%' cellpadding='0' cellspacing='0' "
               f"style='background:#f7efd8;border:1px solid #e8d9a8;"
               f"border-radius:10px'><tr><td style='padding:12px 15px'>"
               f"<div style='font-size:12px;font-weight:800;color:{GOLD};"
               f"text-transform:uppercase;letter-spacing:.5px;"
               f"margin-bottom:6px'>📌 Notes</div>"
               f"{rows}</td></tr></table></td></tr>")

    divider = ("" if not dallon else
               f"<tr><td style='padding:18px 20px 4px'>"
               f"<div style='border-top:1px solid {LINE};padding-top:10px;"
               f"font-size:11px;font-weight:800;color:{MUT};"
               f"text-transform:uppercase;letter-spacing:1px'>"
               f"— just for Dallon —</div></td></tr>")

    return (
        f"<!doctype html><html><body style='margin:0;background:{BG};'>"
        f"<table width='100%' cellpadding='0' cellspacing='0' "
        f"style='background:{BG};padding:24px 12px'>"
        f"<tr><td align='center'>"
        f"<table width='600' cellpadding='0' cellspacing='0' "
        f"style='max-width:600px;background:#fff;border-radius:14px;"
        f"overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"
        f"Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        f"box-shadow:0 1px 3px rgba(16,33,27,.08)'>"
        # header
        f"<tr><td style='background:{G};padding:18px 20px'>"
        f"<div style='font-size:12px;color:#cfe6d9;font-weight:700;"
        f"text-transform:uppercase;letter-spacing:1px'>Master Butler</div>"
        f"<div style='font-size:22px;color:#fff;font-weight:800'>"
        f"☀️ Morning Brief</div>"
        f"<div style='font-size:13px;color:#cfe6d9'>{_esc(d['date'])}</div>"
        f"</td></tr>"
        + pin
        + "".join(card(s) for s in office)
        + divider
        + "".join(card(s, dallon=True) for s in dallon)
        + f"<tr><td style='padding:14px 20px;background:{BG};"
        f"font-size:11px;color:{MUT};text-align:center'>"
        f"Master Butler bidding system · generated overnight</td></tr>"
        f"</table></td></tr></table></body></html>")


def write():
    BRIEFS.mkdir(parents=True, exist_ok=True)
    data = build_data()
    text = build()
    html = build_html(data)
    path = BRIEFS / f"brief-{datetime.now():%Y%m%d}.txt"
    path.write_text(text)
    return path, text, html


if __name__ == "__main__":
    path, text, html = write()
    print(text)
    print(f"(saved -> {path})")
