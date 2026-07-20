"""
RHYTHM — the couple's scheduling + activity + daily-training web app.

Stdlib HTTPServer, mobile-first HTML, no framework — same spirit as the
office dashboard so it hosts on Render the same way. One shared passcode
gates the whole thing (it's just the two of them). Nothing here touches the
Master Butler business, its customers, or Jobber — it's a separate tool.

Calendars live in Google: Rhythm doesn't store your calendar, it hands an
outing to the Google app with a one-tap "Add to Google Calendar" link.
"""

import hashlib
import hmac
import html
import os
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gcal
import planner
import store

PASSCODE = os.environ.get("RHYTHM_PASSCODE", "rhythm")
SECRET = os.environ.get("RHYTHM_SECRET", "dev-secret-change-me").encode()


# ── tiny helpers ──────────────────────────────────────────────────────────
def esc(s):
    return html.escape(str(s if s is not None else ""))


def today_str():
    return date.today().isoformat()


def _sign(value):
    mac = hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{value}.{mac}"


def _valid_cookie(raw):
    if not raw or "." not in raw:
        return False
    value, mac = raw.rsplit(".", 1)
    good = hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(mac, good) and value == "ok"


def person_name(cfg, key):
    return cfg["person_a"] if key == "a" else cfg["person_b"]


def gcal_link(title, start_iso, end_iso, details=""):
    """A deep link that opens Google Calendar with a new event pre-filled."""
    def fmt(iso):
        return datetime.fromisoformat(iso).strftime("%Y%m%dT%H%M%S")
    q = urllib.parse.urlencode({
        "action": "TEMPLATE", "text": title,
        "dates": f"{fmt(start_iso)}/{fmt(end_iso)}", "details": details})
    return "https://calendar.google.com/calendar/render?" + q


# ── HTML shell ────────────────────────────────────────────────────────────
CSS = """
:root{--bg:#f6f7fb;--card:#fff;--ink:#1f2430;--soft:#6b7280;--line:#e6e8ef;
--brand:#2f6df6;--brandsoft:#eaf1ff;--good:#16a34a;--warm:#f59e0b;--gold:#f4b400;}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,
Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);
-webkit-text-size-adjust:100%}
a{color:var(--brand);text-decoration:none}
.wrap{max-width:680px;margin:0 auto;padding:16px 16px 96px}
h1{font-size:20px;margin:6px 0 2px}h2{font-size:16px;margin:22px 0 10px}
.sub{color:var(--soft);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(20,30,60,.04)}
.card.train{border-color:#d6e2ff;background:linear-gradient(180deg,#f7faff,#fff)}
.row{display:flex;gap:10px;flex-wrap:wrap}
.grow{flex:1 1 0;min-width:0}
.btn{display:inline-block;background:var(--brand);color:#fff;border:0;
border-radius:11px;padding:12px 16px;font-size:15px;font-weight:600;
cursor:pointer;text-align:center}
.btn.wide{width:100%}
.btn.ghost{background:var(--brandsoft);color:var(--brand)}
.btn.soft{background:#eef0f5;color:var(--ink)}
.btn.done{background:#e7f7ee;color:#15803d}
.plus{font-size:22px;line-height:1;background:var(--brandsoft);color:var(--brand);
border:1px solid #d6e2ff;border-radius:14px;padding:16px 8px;font-weight:700;
cursor:pointer;flex:1 1 0;text-align:center}
.plus small{display:block;font-size:12px;font-weight:600;color:var(--soft);
margin-top:6px}
.big{font-size:34px;font-weight:800;line-height:1}
.muted{color:var(--soft);font-size:13px}
.pill{display:inline-block;background:#eef0f5;border-radius:999px;padding:3px 10px;
font-size:12px;color:var(--soft);margin-right:6px}
.pill.line{background:var(--brandsoft);color:var(--brand);font-weight:600}
.starter{background:#fff7e6;border:1px solid #ffe6ad;border-radius:11px;
padding:10px 12px;font-size:14px;margin:12px 0}
.facts{margin:10px 0 0;padding-left:18px}.facts li{margin:6px 0;font-size:14px}
input,select,textarea{width:100%;padding:11px;border:1px solid var(--line);
border-radius:10px;font-size:15px;background:#fff;font-family:inherit}
textarea{min-height:88px}
label{font-size:13px;color:var(--soft);display:block;margin:10px 0 4px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 4px;
border-bottom:1px solid var(--line);font-size:14px}
.bar{height:10px;background:#eef0f5;border-radius:999px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--brand)}
.nav{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid
var(--line);display:flex;justify-content:space-around;padding:8px 2px env(safe-area-inset-bottom)}
.nav a{flex:1;text-align:center;font-size:10px;color:var(--soft);padding:4px 2px}
.nav a.on{color:var(--brand);font-weight:700}
.nav a b{display:block;font-size:18px;line-height:1.2}
.flash{background:#e7f7ee;color:#15803d;border:1px solid #bfe8d0;border-radius:10px;
padding:10px 12px;font-size:14px;margin:8px 0}
.chip{background:var(--warm);color:#fff;border-radius:999px;padding:2px 8px;
font-size:11px;font-weight:700}
.streak{background:#fff4d6;color:#8a6d00;border-radius:999px;padding:3px 10px;
font-size:12px;font-weight:700}
"""

NAV = [
    ("/", "Home", "🏠"),
    ("/training", "Train", "🎓"),
    ("/log", "Log", "➕"),
    ("/meals", "Meals", "🛒"),
    ("/outings", "Outings", "📍"),
    ("/numbers", "Numbers", "📈"),
]


def page(title, body, active="/", flash=""):
    nav = "".join(
        f'<a class="{"on" if p == active else ""}" href="{p}"><b>{i}</b>{esc(t)}</a>'
        for p, t, i in NAV)
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{esc(title)} · Rhythm</title><style>{CSS}</style></head><body>
<div class="wrap">{flash_html}{body}</div>
<div class="nav">{nav}</div></body></html>"""


# ── page builders ─────────────────────────────────────────────────────────
def home_page(s):
    cfg = s["config"]
    r = planner.rollup(s["activity"], today_str(), cfg.get("monthly_goal", 0))
    a, b = person_name(cfg, "a"), person_name(cfg, "b")
    today = r["today"]["total"]
    goal_hit = '<span class="chip">Goal hit! 🎉</span>' if r["goal_hit"] else ""

    prod = planner.product_of_day(s["products"], today_str())
    if prod:
        done = planner.trained_today(s["training_log"], today_str(), prod["id"])
        badge = '✅ done' if done else 'not yet'
        train_card = f"""<div class="card train">
  <div class="muted">🎓 Today's product training</div>
  <div class="row" style="justify-content:space-between;align-items:baseline;margin-top:4px">
    <b style="font-size:17px">{esc(prod['name'])}</b><span class="pill line">{esc(prod['line'])}</span></div>
  <p class="muted" style="margin:6px 0">{esc(prod['blurb'])}</p>
  <a class="btn wide ghost" href="/training">Open today's training · {badge}</a></div>"""
    else:
        train_card = ""

    return f"""
<h1>Hi {esc(a)} &amp; {esc(b)} 👋</h1>
<p class="sub">{esc(date.today().strftime("%A, %B %-d"))}</p>

{train_card}

<div class="card">
  <div class="row" style="align-items:baseline;justify-content:space-between">
    <div><div class="big">{today['all']}</div>
      <div class="muted">contacts logged today {goal_hit}</div></div>
    <a class="btn ghost" href="/log">Log activity</a>
  </div>
  <div class="row" style="margin-top:12px">
    <div class="grow"><b>{r['month']['total']['all']}</b>
      <span class="muted">this month</span></div>
    <div class="grow"><b>{r['goal_pct']}%</b>
      <span class="muted">of {r['goal']} goal</span></div>
  </div>
  <div class="bar" style="margin-top:8px"><span style="width:{min(100,r['goal_pct'])}%"></span></div>
</div>

<div class="row">
  <a class="btn grow" href="/meals">Plan meals &amp; shopping</a>
  <a class="btn grow ghost" href="/outings">Suggest an outing</a>
</div>
<p class="muted" style="margin-top:14px"><a href="/settings">Settings</a> ·
<a href="/calendar">Calendar</a></p>
"""


def training_page(s):
    cfg = s["config"]
    prod = planner.product_of_day(s["products"], today_str())
    streak = planner.training_streak(s["training_log"], today_str())
    if not prod:
        return ('<h1>Daily training</h1><div class="card muted">No products yet — '
                'add some in <a href="/settings">Settings</a>.</div>')
    done = planner.trained_today(s["training_log"], today_str(), prod["id"])
    facts = "".join(f"<li>{esc(f)}</li>" for f in prod.get("facts", []))
    link = (f'<a class="btn soft" href="{esc(prod["link"])}" target="_blank" '
            f'rel="noreferrer">Learn more ↗</a>' if prod.get("link") else "")
    starter = (f'<div class="starter">💬 <b>Talk-starter:</b> '
               f'{esc(prod["starter"])}</div>' if prod.get("starter") else "")
    if done:
        action = '<button class="btn wide done" disabled>✓ Trained today</button>'
    else:
        action = ('<form method="post" action="/training/done">'
                  f'<input type="hidden" name="product_id" value="{esc(prod["id"])}">'
                  '<button class="btn wide">Mark trained today</button></form>')
    streak_html = (f'<span class="streak">🔥 {streak}-day streak</span>'
                   if streak else '')

    # tiny preview of what's next
    idx = (planner._day_index(today_str()) + 1) % len(s["products"])
    nxt = s["products"][idx]["name"]

    return f"""
<h1>Daily training {streak_html}</h1>
<p class="sub">One product a day. Learn it, then use the talk-starter today.</p>
<div class="card train">
  <div class="row" style="justify-content:space-between;align-items:baseline">
    <b style="font-size:19px">{esc(prod['name'])}</b>
    <span class="pill line">{esc(prod['line'])}</span></div>
  <div class="muted">{esc(prod.get('category',''))}</div>
  <p style="margin:10px 0 0">{esc(prod['blurb'])}</p>
  <ul class="facts">{facts}</ul>
  {starter}
  <div class="row" style="margin-top:6px">{link}</div>
  <div style="margin-top:12px">{action}</div>
</div>
<p class="muted">Tomorrow: {esc(nxt)} · <a href="/settings">Manage products</a></p>
<p class="muted">Keep to Amway's official claims — no health cures or income promises.</p>
"""


def log_page(s, flash=""):
    cfg = s["config"]
    a, b = person_name(cfg, "a"), person_name(cfg, "b")
    quick = ""
    for pkey, pname in (("a", a), ("b", b)):
        btns = "".join(
            f'<form method="post" action="/log/quick" class="grow" style="margin:0">'
            f'<input type="hidden" name="person" value="{pkey}">'
            f'<input type="hidden" name="kind" value="{k}">'
            f'<button class="plus" type="submit">+1<small>{esc(planner.KIND_LABEL[k])}</small></button>'
            f'</form>'
            for k in planner.KINDS)
        quick += f'<h2>{esc(pname)}</h2><div class="row">{btns}</div>'

    recent = sorted(s["activity"], key=lambda x: x.get("ts", ""), reverse=True)[:12]
    rows = "".join(
        f'<tr><td>{esc(x.get("day",""))}</td>'
        f'<td>{esc(person_name(cfg, x.get("person")))}</td>'
        f'<td>{esc(planner.KIND_LABEL.get(x.get("kind"), x.get("kind")))}</td>'
        f'<td>{esc(x.get("name",""))}</td></tr>'
        for x in recent)
    recent_html = (f'<table><tr><th>Day</th><th>Who</th><th>What</th><th>Name</th></tr>'
                   f'{rows}</table>' if rows
                   else '<p class="muted">No activity logged yet.</p>')

    return f"""
<h1>Log activity</h1>
<p class="sub">Tap +1 the moment it happens. Add a name if you want to remember.</p>
{quick}
<h2>Add with a name / note</h2>
<div class="card">
<form method="post" action="/log/add">
  <div class="row">
    <div class="grow"><label>Who</label>
      <select name="person"><option value="a">{esc(a)}</option>
      <option value="b">{esc(b)}</option></select></div>
    <div class="grow"><label>What</label>
      <select name="kind">
      <option value="conversation">Conversation</option>
      <option value="interaction">Interaction</option>
      <option value="contact">New contact</option></select></div>
  </div>
  <label>Name (optional)</label><input name="name" placeholder="Who did you talk with?">
  <label>Note (optional)</label><input name="note" placeholder="Follow up next week…">
  <button class="btn wide" style="margin-top:12px">Save</button>
</form></div>
<h2>Recent</h2>
<div class="card">{recent_html}</div>
"""


def numbers_page(s):
    cfg = s["config"]
    r = planner.rollup(s["activity"], today_str(), cfg.get("monthly_goal", 0))
    a, b = person_name(cfg, "a"), person_name(cfg, "b")

    def kind_table(scope):
        d = r[scope]
        head = f'<tr><th></th><th>{esc(a)}</th><th>{esc(b)}</th><th>Total</th></tr>'
        body = ""
        for k in planner.KINDS:
            body += (f'<tr><td>{esc(planner.KIND_LABEL[k])}</td>'
                     f'<td>{d["a"][k]}</td><td>{d["b"][k]}</td>'
                     f'<td><b>{d["total"][k]}</b></td></tr>')
        body += (f'<tr><td><b>All</b></td><td>{d["a"]["all"]}</td>'
                 f'<td>{d["b"]["all"]}</td><td><b>{d["total"]["all"]}</b></td></tr>')
        return f'<table>{head}{body}</table>'

    hist = planner.month_history(s["activity"], months=6, today=today_str())
    mx = max([h["count"] for h in hist] + [1])
    bars = "".join(
        f'<div class="row" style="align-items:center;margin:6px 0">'
        f'<div style="width:70px" class="muted">{esc(h["month"])}</div>'
        f'<div class="grow bar"><span style="width:{round(100*h["count"]/mx)}%"></span></div>'
        f'<div style="width:44px;text-align:right"><b>{h["count"]}</b></div></div>'
        for h in hist)

    return f"""
<h1>Numbers</h1>
<p class="sub">Conversations, interactions and new contacts — this is the engine.</p>
<div class="card"><h2 style="margin-top:0">This month ({esc(r['month_key'])})</h2>
{kind_table('month')}
<div class="bar" style="margin-top:12px"><span style="width:{min(100,r['goal_pct'])}%"></span></div>
<p class="muted">{r['month']['total']['all']} of {r['goal']} goal · {r['goal_pct']}%</p>
</div>
<div class="card"><h2 style="margin-top:0">Today</h2>{kind_table('today')}</div>
<div class="card"><h2 style="margin-top:0">Last 6 months</h2>{bars}</div>
"""


def meals_page(s, flash=""):
    library = s["meal_library"]
    mon = _week_monday()
    plan = s["week_plan"].get(mon, {})
    days = [("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"), ("thu", "Thu"),
            ("fri", "Fri"), ("sat", "Sat"), ("sun", "Sun")]

    def opts(sel):
        return "".join(
            f'<option value="{esc(n)}" {"selected" if n == sel else ""}>{esc(n)}</option>'
            for n in [""] + sorted(library.keys()))
    day_rows = "".join(
        f'<label>{esc(lbl)}</label><select name="day_{d}">{opts(plan.get(d,""))}</select>'
        for d, lbl in days)

    chosen = [plan[d] for d, _ in days if plan.get(d)]
    items = planner.grocery_list(chosen, library)
    trips = planner.split_by_store(items, s["stores"])
    trip_html = ""
    for t in trips:
        li = "".join(
            f'<tr><td>{esc(it["item"])}</td><td class="muted">'
            f'{esc(", ".join(q for q in it["qtys"] if q))}</td></tr>'
            for it in t["items"])
        area = f' · {esc(t["store"].get("area",""))}' if t["store"].get("area") else ""
        trip_html += (f'<div class="card"><h2 style="margin-top:0">🛒 '
                      f'{esc(t["store"]["name"])}{area}</h2><table>{li}</table></div>')
    if not trip_html:
        trip_html = ('<div class="card muted">Pick a few meals above and your '
                     'shopping list will split across your stores.</div>')

    return f"""
<h1>Meals &amp; shopping</h1>
<p class="sub">Plan the week, then split the list across stores — more stops
means you're out and about more.</p>
<div class="card"><form method="post" action="/meals/plan">
<h2 style="margin-top:0">Week of {esc(mon)}</h2>
{day_rows}
<button class="btn wide" style="margin-top:14px">Save week &amp; rebuild list</button>
</form></div>
<h2>Shopping split</h2>
{trip_html}
<p class="muted">Manage your stores &amp; meals in <a href="/settings">Settings</a>.</p>
"""


def outings_page(s, flash=""):
    cfg = s["config"]
    # If calendars are connected, suggest slots that are actually free for
    # BOTH of you; otherwise fall back to your configured out-windows. Either
    # way, adding hands off to the Google app.
    busy_a, busy_b = _week_busy()
    windows = planner.shared_windows(cfg, busy_a, busy_b)
    sugg = planner.suggest_outings(s["places"], windows, today_str(), limit=3)

    cards = ""
    for item in sugg:
        p = item["place"]
        w = item["window"]
        when = _pretty_window(w) if w else 'anytime you both have a gap'
        last = (f'last visited {esc(p["last_visited"])}' if p.get("last_visited")
                else 'never been together')
        add = ""
        if w:
            url = gcal_link(f"Outing: {p['name']}", w["start"], w["end"],
                            p.get("notes", ""))
            add = (f'<a class="btn ghost" target="_blank" rel="noreferrer" '
                   f'href="{esc(url)}">Add to Google Calendar</a>')
        cards += f"""<div class="card">
  <div class="row" style="justify-content:space-between;align-items:baseline">
    <b>{esc(p["name"])}</b><span class="pill">{esc(p["type"])}</span></div>
  <p class="muted" style="margin:6px 0">{esc(p.get("notes",""))}</p>
  <p class="muted">📅 {esc(when)} · {last}</p>
  <div class="row">
    <form method="post" action="/outings/visited" style="margin:0">
      <input type="hidden" name="place_id" value="{esc(p["id"])}">
      <button class="btn soft">Mark visited today</button></form>
    {add}
  </div></div>"""

    all_rows = "".join(
        f'<tr><td>{esc(p["name"])}</td><td class="muted">{esc(p["type"])}</td>'
        f'<td class="muted">{esc(p.get("last_visited","") or "—")}</td></tr>'
        for p in s["places"])

    return f"""
<h1>Outings</h1>
<p class="sub">Places to be around people. Freshest picks first — tap to drop
one on your Google Calendar.</p>
{cards or '<div class="card muted">Add some places below to get suggestions.</div>'}
<h2>Add a place</h2>
<div class="card"><form method="post" action="/outings/add">
  <label>Name</label><input name="name" placeholder="Lake Tye Park" required>
  <div class="row"><div class="grow"><label>Type</label>
    <select name="type"><option>park</option><option>store</option>
    <option>event</option><option>kids</option><option>community</option></select></div>
    <div class="grow"><label>Area</label><input name="area" placeholder="Monroe"></div></div>
  <label>Notes</label><input name="notes" placeholder="Good for meeting families">
  <button class="btn wide" style="margin-top:12px">Add place</button>
</form></div>
<h2>All places</h2>
<div class="card"><table><tr><th>Place</th><th>Type</th><th>Last visit</th></tr>
{all_rows}</table></div>
"""


def calendar_page(s):
    cfg = s["config"]
    busy_a, busy_b = _week_busy()
    windows = planner.shared_windows(cfg, busy_a, busy_b)
    win_rows = ""
    for w in windows[:12]:
        url = gcal_link("Time out together", w["start"], w["end"],
                        "Be out and about — Amway relationships")
        win_rows += (f'<tr><td>{esc(_pretty_window(w))}</td>'
                     f'<td class="muted">{w["minutes"]} min</td>'
                     f'<td><a target="_blank" rel="noreferrer" href="{esc(url)}">Add ↗</a></td></tr>')
    win_html = (f'<table><tr><th>When</th><th></th><th></th></tr>{win_rows}</table>'
                if win_rows else '<p class="muted">No shared openings found — '
                                 'set your out-windows in Settings.</p>')

    # Connect blocks — only when Google credentials are configured
    connect_html = ""
    if gcal.available():
        def block(pkey):
            name = person_name(cfg, pkey)
            if gcal.connected(pkey):
                email = gcal.account_email(pkey) or "connected"
                return (f'<div class="card"><b>{esc(name)}</b> '
                        f'<span class="muted">· {esc(email)} ✓</span>'
                        f'<form method="post" action="/oauth/disconnect" style="margin-top:8px">'
                        f'<input type="hidden" name="person" value="{pkey}">'
                        f'<button class="btn soft">Disconnect</button></form></div>')
            return (f'<div class="card"><b>{esc(name)}</b>'
                    f'<a class="btn wide ghost" style="margin-top:10px" '
                    f'href="/oauth/start?person={pkey}">Connect {esc(name)}\'s Google</a></div>')
        connect_html = ("<h2>Connect your calendars</h2>"
                        "<p class=\"sub\">Link once so the openings below reflect when "
                        "you're really both free.</p>" + block("a") + block("b"))
    else:
        connect_html = (
            '<div class="card"><b>Optional:</b> connect Google to make the openings '
            'below reflect your real free/busy. It\'s a one-time setup — see '
            '<b>SETUP_GOOGLE.md</b>. Everything works without it.</div>')

    return f"""
<h1>Calendar</h1>
<p class="sub">Your calendar lives in Google. Rhythm reads when you're free and
hands new events to the Google app.</p>
{connect_html}
<div class="card">
<a class="btn wide" target="_blank" rel="noreferrer"
   href="https://calendar.google.com">Open Google Calendar ↗</a>
<p class="muted" style="margin-top:12px">Anywhere you see “Add to Google
Calendar,” tapping it opens the Google app with the event pre-filled — pick
the time and save.</p>
</div>
<h2>Your shared openings (next 7 days)</h2>
<div class="card">{win_html}</div>
<p class="muted">Adjust your out-windows in <a href="/settings">Settings</a>.</p>
"""


def settings_page(s, flash=""):
    cfg = s["config"]
    fw = cfg.get("free_windows", {})
    store_rows = "".join(
        f'<tr><td>{esc(st["name"])}</td><td class="muted">{esc(st.get("area",""))}</td>'
        f'<td class="muted">{esc(", ".join(st.get("categories",[])))}</td></tr>'
        for st in s["stores"])
    prod_rows = "".join(
        f'<tr><td>{esc(p["name"])}</td><td class="muted">{esc(p.get("line",""))}</td>'
        f'<td><form method="post" action="/settings/product/delete" style="margin:0">'
        f'<input type="hidden" name="id" value="{esc(p["id"])}">'
        f'<button class="btn soft" style="padding:6px 10px">Remove</button></form></td></tr>'
        for p in s["products"])
    return f"""
<h1>Settings</h1>
<div class="card"><form method="post" action="/settings/save">
  <div class="row"><div class="grow"><label>Your name</label>
    <input name="person_a" value="{esc(cfg['person_a'])}"></div>
    <div class="grow"><label>Wife's name</label>
    <input name="person_b" value="{esc(cfg['person_b'])}"></div></div>
  <label>Monthly contact goal (combined)</label>
  <input name="monthly_goal" type="number" value="{esc(cfg['monthly_goal'])}">
  <div class="row"><div class="grow"><label>Weekday out-window</label>
    <input name="wd" value="{esc('-'.join(fw.get('weekday',['17:00','21:00'])))}"></div>
    <div class="grow"><label>Weekend out-window</label>
    <input name="we" value="{esc('-'.join(fw.get('weekend',['09:00','21:00'])))}"></div></div>
  <button class="btn wide" style="margin-top:14px">Save settings</button>
</form></div>

<h2>Daily training products</h2>
<div class="card"><table><tr><th>Product</th><th>Line</th><th></th></tr>
{prod_rows}</table></div>
<div class="card"><form method="post" action="/settings/product">
  <div class="row"><div class="grow"><label>Product name</label>
    <input name="name" placeholder="Nutrilite Double X" required></div>
    <div class="grow"><label>Line</label><input name="line" placeholder="Nutrilite"></div></div>
  <label>Category</label><input name="category" placeholder="Health & wellness">
  <label>One-line description</label><input name="blurb" placeholder="The flagship multivitamin.">
  <label>Facts (one per line)</label>
  <textarea name="facts" placeholder="22 vitamins and minerals&#10;NSF certified"></textarea>
  <label>Talk-starter (a question to open a conversation)</label>
  <input name="starter" placeholder="Do you take a daily vitamin?">
  <label>Learn-more link (optional)</label><input name="link" placeholder="https://www.amway.com/...">
  <button class="btn wide" style="margin-top:12px">Add product</button>
</form></div>

<h2>Add a store</h2>
<div class="card"><form method="post" action="/settings/store">
  <label>Name</label><input name="name" placeholder="Fred Meyer (Monroe)" required>
  <div class="row"><div class="grow"><label>Area</label>
    <input name="area" placeholder="Monroe"></div>
    <div class="grow"><label>Carries (comma separated)</label>
    <input name="categories" placeholder="produce, pantry, dairy"></div></div>
  <button class="btn wide" style="margin-top:12px">Add store</button>
</form></div>
<h2>Your stores</h2>
<div class="card"><table><tr><th>Store</th><th>Area</th><th>Carries</th></tr>
{store_rows}</table></div>
<p class="muted"><a href="/calendar">Calendar →</a></p>
"""


# ── week/time helpers ─────────────────────────────────────────────────────
def _week_monday(d=None):
    d = d or date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


def _week_busy():
    """Busy blocks for both spouses over the next 7 days (empty if not linked)."""
    start = datetime.combine(date.today(), datetime.min.time()).isoformat()
    end = (datetime.combine(date.today(), datetime.min.time())
           + timedelta(days=7)).isoformat()
    return gcal.busy("a", start, end), gcal.busy("b", start, end)


def _pretty_window(w):
    try:
        s = datetime.fromisoformat(w["start"])
        e = datetime.fromisoformat(w["end"])
        return s.strftime("%a %-m/%-d %-I:%M") + e.strftime("–%-I:%M %p")
    except Exception:
        return w.get("day", "")


# ── HTTP handler ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        jar = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                jar[k] = v
        return jar

    def _authed(self):
        return _valid_cookie(self._cookies().get("rhythm"))

    def _base_url(self):
        env = os.environ.get("RHYTHM_BASE_URL")
        if env:
            return env.rstrip("/")
        host = self.headers.get("Host", "localhost")
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return f"{proto}://{host}"

    def _send(self, body, code=200, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to, headers=None):
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _form(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path == "/login":
            return self._send(login_html())
        if not self._authed():
            return self._redirect("/login")
        if path == "/oauth/start":
            return self._oauth_start(qs)
        if path == "/oauth/callback":
            return self._oauth_callback(qs)

        s = store.get()
        pages = {
            "/": ("Home", home_page, "/"),
            "/training": ("Train", training_page, "/training"),
            "/log": ("Log", log_page, "/log"),
            "/numbers": ("Numbers", numbers_page, "/numbers"),
            "/meals": ("Meals", meals_page, "/meals"),
            "/outings": ("Outings", outings_page, "/outings"),
            "/calendar": ("Calendar", calendar_page, "/"),
            "/settings": ("Settings", settings_page, "/"),
        }
        if path in pages:
            title, fn, active = pages[path]
            return self._send(page(title, fn(s), active))
        if path == "/logout":
            return self._redirect("/login", {"Set-Cookie": "rhythm=; Max-Age=0; Path=/"})
        return self._send(page("Not found", "<h1>Not found</h1>"), 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            form = self._form()
            if form.get("passcode", "") == PASSCODE:
                cookie = f"rhythm={_sign('ok')}; Path=/; HttpOnly; SameSite=Lax; Max-Age=7776000"
                return self._redirect("/", {"Set-Cookie": cookie})
            return self._send(login_html("That passcode didn't match."))

        if not self._authed():
            return self._redirect("/login")
        form = self._form()

        routes = {
            "/log/quick": (lambda: _add_activity(form.get("person"), form.get("kind")), "/log"),
            "/log/add": (lambda: _add_activity(form.get("person"), form.get("kind"),
                                               form.get("name", ""), form.get("note", "")), "/log"),
            "/training/done": (lambda: _mark_trained(form.get("product_id")), "/training"),
            "/meals/plan": (lambda: _save_week(form), "/meals"),
            "/outings/add": (lambda: _add_place(form), "/outings"),
            "/outings/visited": (lambda: _mark_visited(form.get("place_id")), "/outings"),
            "/settings/save": (lambda: _save_settings(form), "/settings"),
            "/settings/store": (lambda: _add_store(form), "/settings"),
            "/settings/product": (lambda: _add_product(form), "/settings"),
            "/settings/product/delete": (lambda: _del_product(form.get("id")), "/settings"),
            "/oauth/disconnect": (lambda: gcal.disconnect(form.get("person")), "/calendar"),
        }
        if path in routes:
            action, dest = routes[path]
            action()
            return self._redirect(dest)
        return self._send("bad request", 400, "text/plain")

    # -- oauth
    def _oauth_start(self, qs):
        person = qs.get("person", ["a"])[0]
        if person not in ("a", "b") or not gcal.available():
            return self._redirect("/calendar")
        redirect = self._base_url() + "/oauth/callback"
        return self._redirect(gcal.auth_url(person, redirect, "rhythm"))

    def _oauth_callback(self, qs):
        code = qs.get("code", [""])[0]
        state = qs.get("state", [""])[0]
        person = state.split(":", 1)[0] if ":" in state else "a"
        if code and person in ("a", "b"):
            try:
                gcal.exchange_code(person, code, self._base_url() + "/oauth/callback")
            except Exception:
                pass
        return self._redirect("/calendar")


def login_html(err=""):
    e = (f'<div class="flash" style="background:#fdecec;color:#b91c1c;'
         f'border-color:#f5c2c2">{esc(err)}</div>' if err else "")
    body = f"""<div class="wrap"><h1 style="margin-top:40px">Rhythm</h1>
<p class="sub">Your and your wife's scheduling, training + contacts.</p>{e}
<div class="card"><form method="post" action="/login">
<label>Passcode</label><input name="passcode" type="password" autofocus>
<button class="btn wide" style="margin-top:12px">Enter</button></form></div></div>"""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rhythm</title><style>{CSS}</style></head><body>{body}</body></html>"""


# ── state mutations ───────────────────────────────────────────────────────
def _add_activity(person, kind, name="", note=""):
    if person not in ("a", "b") or kind not in planner.KINDS:
        return
    now = datetime.now()

    def _do(s):
        s["activity"].append({
            "id": store.next_id(s, "act"), "ts": now.isoformat(),
            "day": now.date().isoformat(), "person": person, "kind": kind,
            "name": name.strip(), "note": note.strip()})
    store.update(_do)


def _mark_trained(product_id):
    if not product_id:
        return
    day = today_str()

    def _do(s):
        if not planner.trained_today(s["training_log"], day, product_id):
            s["training_log"].append({"day": day, "product_id": product_id, "person": ""})
    store.update(_do)


def _save_week(form):
    mon = _week_monday()
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    def _do(s):
        plan = {}
        for d in days:
            val = form.get(f"day_{d}", "").strip()
            if val:
                plan[d] = val
        s["week_plan"][mon] = plan
    store.update(_do)


def _add_place(form):
    name = form.get("name", "").strip()
    if not name:
        return

    def _do(s):
        s["places"].append({
            "id": store.next_id(s, "plc"), "name": name,
            "type": form.get("type", "park"), "area": form.get("area", "").strip(),
            "notes": form.get("notes", "").strip(), "last_visited": ""})
    store.update(_do)


def _mark_visited(place_id):
    def _do(s):
        for p in s["places"]:
            if p["id"] == place_id:
                p["last_visited"] = today_str()
    store.update(_do)


def _add_product(form):
    name = form.get("name", "").strip()
    if not name:
        return
    facts = [ln.strip() for ln in form.get("facts", "").splitlines() if ln.strip()]

    def _do(s):
        s["products"].append({
            "id": store.next_id(s, "pr"), "name": name,
            "line": form.get("line", "").strip(),
            "category": form.get("category", "").strip(),
            "blurb": form.get("blurb", "").strip(), "facts": facts,
            "starter": form.get("starter", "").strip(),
            "link": form.get("link", "").strip()})
    store.update(_do)


def _del_product(pid):
    def _do(s):
        s["products"] = [p for p in s["products"] if p["id"] != pid]
    store.update(_do)


def _save_settings(form):
    def _do(s):
        c = s["config"]
        c["person_a"] = form.get("person_a", c["person_a"]).strip() or c["person_a"]
        c["person_b"] = form.get("person_b", c["person_b"]).strip() or c["person_b"]
        try:
            c["monthly_goal"] = max(0, int(form.get("monthly_goal", c["monthly_goal"])))
        except ValueError:
            pass
        c["free_windows"] = {
            "weekday": _parse_window(form.get("wd"), c["free_windows"]["weekday"]),
            "weekend": _parse_window(form.get("we"), c["free_windows"]["weekend"]),
        }
    store.update(_do)


def _parse_window(text, fallback):
    try:
        a, b = (text or "").split("-")
        datetime.strptime(a.strip(), "%H:%M")
        datetime.strptime(b.strip(), "%H:%M")
        return [a.strip(), b.strip()]
    except Exception:
        return fallback


def _add_store(form):
    name = form.get("name", "").strip()
    if not name:
        return
    cats = [c.strip().lower() for c in form.get("categories", "").split(",") if c.strip()]

    def _do(s):
        s["stores"].append({
            "id": store.next_id(s, "st"), "name": name,
            "area": form.get("area", "").strip(), "categories": cats})
    store.update(_do)


def run(port=None):
    port = int(port or os.environ.get("PORT", 8100))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Rhythm running on http://localhost:{port}  (passcode: set RHYTHM_PASSCODE)")
    httpd.serve_forever()


if __name__ == "__main__":
    run()
