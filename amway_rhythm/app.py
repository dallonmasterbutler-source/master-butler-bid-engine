"""
RHYTHM — the couple's scheduling + activity web app.

Stdlib HTTPServer, mobile-first HTML, no framework — same spirit as the
office dashboard so it hosts on Render the same way. One shared passcode
gates the whole thing (it's just the two of them). Nothing here touches the
Master Butler business, its customers, or Jobber — it's a separate tool.
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


# ── HTML shell ────────────────────────────────────────────────────────────
CSS = """
:root{--bg:#f6f7fb;--card:#fff;--ink:#1f2430;--soft:#6b7280;--line:#e6e8ef;
--brand:#2f6df6;--brandsoft:#eaf1ff;--good:#16a34a;--warm:#f59e0b;}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,
Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);
-webkit-text-size-adjust:100%}
a{color:var(--brand);text-decoration:none}
.wrap{max-width:680px;margin:0 auto;padding:16px 16px 96px}
h1{font-size:20px;margin:6px 0 2px}h2{font-size:16px;margin:22px 0 10px}
.sub{color:var(--soft);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(20,30,60,.04)}
.row{display:flex;gap:10px;flex-wrap:wrap}
.grow{flex:1 1 0;min-width:0}
.btn{display:inline-block;background:var(--brand);color:#fff;border:0;
border-radius:11px;padding:12px 16px;font-size:15px;font-weight:600;
cursor:pointer;text-align:center}
.btn.wide{width:100%}
.btn.ghost{background:var(--brandsoft);color:var(--brand)}
.btn.soft{background:#eef0f5;color:var(--ink)}
.plus{font-size:22px;line-height:1;background:var(--brandsoft);color:var(--brand);
border:1px solid #d6e2ff;border-radius:14px;padding:16px 8px;font-weight:700;
cursor:pointer;flex:1 1 0;text-align:center}
.plus small{display:block;font-size:12px;font-weight:600;color:var(--soft);
margin-top:6px}
.big{font-size:34px;font-weight:800;line-height:1}
.muted{color:var(--soft);font-size:13px}
.pill{display:inline-block;background:#eef0f5;border-radius:999px;padding:3px 10px;
font-size:12px;color:var(--soft);margin-right:6px}
input,select,textarea{width:100%;padding:11px;border:1px solid var(--line);
border-radius:10px;font-size:15px;background:#fff;font-family:inherit}
label{font-size:13px;color:var(--soft);display:block;margin:10px 0 4px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 4px;
border-bottom:1px solid var(--line);font-size:14px}
.bar{height:10px;background:#eef0f5;border-radius:999px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--brand)}
.nav{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid
var(--line);display:flex;justify-content:space-around;padding:8px 4px env(safe-area-inset-bottom)}
.nav a{flex:1;text-align:center;font-size:11px;color:var(--soft);padding:4px}
.nav a.on{color:var(--brand);font-weight:700}
.nav a b{display:block;font-size:19px;line-height:1.2}
.flash{background:#e7f7ee;color:#15803d;border:1px solid #bfe8d0;border-radius:10px;
padding:10px 12px;font-size:14px;margin:8px 0}
.chip{background:var(--warm);color:#fff;border-radius:999px;padding:2px 8px;
font-size:11px;font-weight:700}
"""

NAV = [
    ("/", "Home", "🏠"),
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

    # today's scheduled outings / trips
    todays = [x for x in s["scheduled"] if x.get("start", "")[:10] == today_str()]
    plan_rows = "".join(
        f'<tr><td>{esc(x["start"][11:16])}</td><td>{esc(x["title"])}</td>'
        f'<td class="muted">{esc(x.get("kind",""))}</td></tr>'
        for x in sorted(todays, key=lambda x: x.get("start", "")))
    plan = (f'<table>{plan_rows}</table>' if plan_rows
            else '<p class="muted">Nothing scheduled yet today — '
                 'plan an outing or a shopping run below.</p>')

    return f"""
<h1>Hi {esc(a)} &amp; {esc(b)} 👋</h1>
<p class="sub">{esc(date.today().strftime("%A, %B %-d"))}</p>

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

<h2>Today's plan</h2>
<div class="card">{plan}</div>

<div class="row">
  <a class="btn grow" href="/meals">Plan meals &amp; shopping</a>
  <a class="btn grow ghost" href="/outings">Suggest an outing</a>
</div>
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
    cfg = s["config"]
    library = s["meal_library"]
    mon = _week_monday()
    plan = s["week_plan"].get(mon, {})
    days = [("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"), ("thu", "Thu"),
            ("fri", "Fri"), ("sat", "Sat"), ("sun", "Sun")]
    opts = lambda sel: "".join(
        f'<option value="{esc(n)}" {"selected" if n == sel else ""}>{esc(n)}</option>'
        for n in [""] + sorted(library.keys()))
    day_rows = "".join(
        f'<label>{esc(lbl)}</label><select name="day_{d}">{opts(plan.get(d,""))}</select>'
        for d, lbl in days)

    # grocery split for whatever is planned
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
    busy_a, busy_b = _week_busy()
    windows = planner.shared_windows(cfg, busy_a, busy_b)
    sugg = planner.suggest_outings(s["places"], windows, today_str(), limit=3)

    cards = ""
    for item in sugg:
        p = item["place"]
        w = item["window"]
        when = (f'{_pretty_window(w)}' if w
                else 'anytime you both have a gap')
        last = (f'last visited {esc(p["last_visited"])}' if p.get("last_visited")
                else 'never been together')
        sched = ""
        if w and (gcal.connected("a") or gcal.connected("b")):
            sched = (f'<form method="post" action="/outings/schedule" style="margin:0">'
                     f'<input type="hidden" name="place_id" value="{esc(p["id"])}">'
                     f'<input type="hidden" name="start" value="{esc(w["start"])}">'
                     f'<input type="hidden" name="end" value="{esc(w["end"])}">'
                     f'<button class="btn ghost">Add to calendar</button></form>')
        cards += f"""<div class="card">
  <div class="row" style="justify-content:space-between;align-items:baseline">
    <b>{esc(p["name"])}</b><span class="pill">{esc(p["type"])}</span></div>
  <p class="muted" style="margin:6px 0">{esc(p.get("notes",""))}</p>
  <p class="muted">📅 {esc(when)} · {last}</p>
  <div class="row">
    <form method="post" action="/outings/visited" style="margin:0">
      <input type="hidden" name="place_id" value="{esc(p["id"])}">
      <button class="btn soft">Mark visited today</button></form>
    {sched}
  </div></div>"""

    all_rows = "".join(
        f'<tr><td>{esc(p["name"])}</td><td class="muted">{esc(p["type"])}</td>'
        f'<td class="muted">{esc(p.get("last_visited","") or "—")}</td></tr>'
        for p in s["places"])

    win_note = ("" if windows else
                '<p class="muted">Tip: connect your calendars so suggestions land '
                'in your real free time. <a href="/calendar">Calendar setup →</a></p>')

    return f"""
<h1>Outings</h1>
<p class="sub">Places to be around people. Freshest picks first.</p>
{win_note}
{cards or '<div class="card muted">Add some places in Settings to get suggestions.</div>'}
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


def calendar_page(s, base_url, flash=""):
    cfg = s["config"]
    if not gcal.available():
        return f"""
<h1>Calendar</h1>
<p class="sub">Connect Google Calendar for you both.</p>
<div class="card">
<p>Google isn't wired up yet. It's a one-time setup Dallon does — see
<b>SETUP_GOOGLE.md</b> in the project. Once the two keys are set, this page
will show a <b>Connect</b> button for each of you.</p>
<p class="muted">Everything else in the app already works without it — the
outing suggestions just won't know your real free time yet.</p>
</div>"""

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

    busy_a, busy_b = _week_busy()
    windows = planner.shared_windows(cfg, busy_a, busy_b)
    win_rows = "".join(
        f'<tr><td>{esc(_pretty_window(w))}</td>'
        f'<td class="muted">{w["minutes"]} min</td></tr>' for w in windows[:12])
    win_html = (f'<table>{win_rows}</table>' if win_rows
                else '<p class="muted">No shared openings found in the next week '
                     '(or calendars not connected yet).</p>')

    return f"""
<h1>Calendar</h1>
<p class="sub">Link both Google calendars to find time you're both free.</p>
{block("a")}{block("b")}
<h2>Your shared openings (next 7 days)</h2>
<div class="card">{win_html}</div>
"""


def settings_page(s, flash=""):
    cfg = s["config"]
    fw = cfg.get("free_windows", {})
    store_rows = "".join(
        f'<tr><td>{esc(st["name"])}</td><td class="muted">{esc(st.get("area",""))}</td>'
        f'<td class="muted">{esc(", ".join(st.get("categories",[])))}</td></tr>'
        for st in s["stores"])
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
<p class="muted"><a href="/calendar">Calendar setup →</a></p>
"""


# ── week/time helpers ─────────────────────────────────────────────────────
def _week_monday(d=None):
    d = d or date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


def _week_busy():
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

    # -- helpers
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

    # -- routing
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path == "/login":
            return self._send(login_html())
        if path.startswith("/oauth/callback"):
            return self._oauth_callback(qs)
        if not self._authed():
            return self._redirect("/login")

        s = store.get()
        if path == "/":
            return self._send(page("Home", home_page(s), "/"))
        if path == "/log":
            return self._send(page("Log", log_page(s), "/log"))
        if path == "/numbers":
            return self._send(page("Numbers", numbers_page(s), "/numbers"))
        if path == "/meals":
            return self._send(page("Meals", meals_page(s), "/meals"))
        if path == "/outings":
            return self._send(page("Outings", outings_page(s), "/outings"))
        if path == "/calendar":
            return self._send(page("Calendar", calendar_page(s, self._base_url()), "/"))
        if path == "/settings":
            return self._send(page("Settings", settings_page(s), "/"))
        if path == "/oauth/start":
            return self._oauth_start(qs)
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

        if path == "/log/quick":
            _add_activity(form.get("person"), form.get("kind"))
            return self._redirect("/log")
        if path == "/log/add":
            _add_activity(form.get("person"), form.get("kind"),
                          form.get("name", ""), form.get("note", ""))
            return self._redirect("/log")
        if path == "/meals/plan":
            _save_week(form)
            return self._redirect("/meals")
        if path == "/outings/add":
            _add_place(form)
            return self._redirect("/outings")
        if path == "/outings/visited":
            _mark_visited(form.get("place_id"))
            return self._redirect("/outings")
        if path == "/outings/schedule":
            _schedule_outing(form)
            return self._redirect("/outings")
        if path == "/settings/save":
            _save_settings(form)
            return self._redirect("/settings")
        if path == "/settings/store":
            _add_store(form)
            return self._redirect("/settings")
        if path == "/oauth/disconnect":
            gcal.disconnect(form.get("person"))
            return self._redirect("/calendar")
        return self._send("bad request", 400, "text/plain")

    # -- oauth
    def _oauth_start(self, qs):
        person = (qs.get("person", ["a"])[0])
        if person not in ("a", "b") or not gcal.available():
            return self._redirect("/calendar")
        redirect = self._base_url() + "/oauth/callback"
        return self._redirect(gcal.auth_url(person, redirect, "rhythm"))

    def _oauth_callback(self, qs):
        if not self._authed():
            return self._redirect("/login")
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
    e = f'<div class="flash" style="background:#fdecec;color:#b91c1c;border-color:#f5c2c2">{esc(err)}</div>' if err else ""
    body = f"""<div class="wrap"><h1 style="margin-top:40px">Rhythm</h1>
<p class="sub">Your and your wife's scheduling + contacts.</p>{e}
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
        row = {"id": store.next_id(s, "act"), "ts": now.isoformat(),
               "day": now.date().isoformat(), "person": person, "kind": kind,
               "name": name.strip(), "note": note.strip()}
        s["activity"].append(row)
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


def _schedule_outing(form):
    pid, start, end = form.get("place_id"), form.get("start"), form.get("end")
    if not (pid and start and end):
        return
    s0 = store.get()
    place = next((p for p in s0["places"] if p["id"] == pid), None)
    if not place:
        return
    title = f"Outing: {place['name']}"
    gid = None
    for pkey in ("a", "b"):
        if gcal.connected(pkey):
            gid = gcal.add_event(pkey, title, start, end,
                                 place.get("notes", "")) or gid

    def _do(s):
        s["scheduled"].append({
            "id": store.next_id(s, "sch"), "kind": "outing", "title": title,
            "start": start, "end": end, "person": "both", "gcal_id": gid})
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
