"""
MASTER BUTLER — CRITICAL-DETAILS ACCEPTANCE TRIALS (Dallon, Jul 10:
'run through some trials real fast to make sure it's cleaned up and we
don't miss any of the critical details that we built earlier').

Exercises every office-critical behavior end-to-end against a LOCAL
instance (file mode — production cloud untouched; local push OFF).
This checklist is the GATE for the design rollout: it must be 100%
green before AND after any reskin.

Run:  python3 critical_trials.py          (spins its own server :8771)
"""

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
PORT = 8771
URL = f"http://127.0.0.1:{PORT}"
RESULTS = []


def check(name, cond, note=""):
    RESULTS.append((name, bool(cond), note))
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {note}" if note and not cond else ""))


def _pw():
    for ln in (BASE / ".env").read_text().splitlines():
        if ln.startswith("DASHBOARD_PASSWORD="):
            return ln.split("=", 1)[1].strip()
    return ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


def main():
    # ── backups of everything the trials may touch ──
    data = BASE / "data"
    saved = {}
    for f in ("msg_read.json", "learned_spam.json", "review_log.json",
              "must_know.json", "holds.json"):
        p = data / f
        saved[f] = p.read_bytes() if p.exists() else None

    srv = subprocess.Popen(
        [sys.executable, "dashboard.py"],
        # MB_SANDBOX: file-mode server whose button clicks must never
        # reach production — cloudpush refuses when it's set (Jul 10 pm)
        env={"HOST": "127.0.0.1", "PORT": str(PORT), "PATH": "/usr/bin:/bin",
             "HOME": str(Path.home()), "MB_SANDBOX": "1"},
        cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)

    try:
        pw = _pw()
        # ── auth ──
        op = urllib.request.build_opener(NoRedirect)
        body = urllib.parse.urlencode({"password": pw, "next": "/"}).encode()
        try:
            r = op.open(urllib.request.Request(URL + "/login", data=body))
        except urllib.error.HTTPError as e:
            r = e
        tok = ""
        for h, v in r.headers.items():
            if h.lower() == "set-cookie" and "mb_auth=" in v:
                tok = v.split("mb_auth=")[1].split(";")[0]
        check("Sign-in issues session cookie", bool(tok))
        H = {"Cookie": "mb_auth=" + tok}

        def get(p):
            return urllib.request.urlopen(
                urllib.request.Request(URL + p, headers=H), timeout=30
            ).read().decode()

        def post(p, fields):
            data_ = urllib.parse.urlencode(fields).encode()
            try:
                r = urllib.request.build_opener(NoRedirect).open(
                    urllib.request.Request(URL + p, data=data_, headers=H))
                return r.status
            except urllib.error.HTTPError as e:
                return e.code

        login_html = urllib.request.urlopen(URL + "/login").read().decode()
        check("Login page has show-password toggle",
              "Show password" in login_html or "👁" in login_html)

        # ── THE INBOX (the office's home = THE MIRROR since Jul 22) ──
        h = get("/")
        check("Inbox renders", "Bid" in h and "irow" in h)
        check("Search box on the mirror (Jessica)", "fsearch" in h)
        # the classic lane view survives at /?classic=1 (safety hatch)
        hc = get("/?classic=1")
        check("Bulk mark-seen: checkboxes on rows", "class='rowsel'" in hc)
        check("Bulk mark-seen: action bar + already-quoted selector",
              "bulkbar" in hc and "already-quoted" in hc.lower()
              or "bulkQuoted" in hc)
        check("Classic search box (Jessica)", "isearch" in hc)
        # FIVE BUBBLES (Dallon's approved collapse, built Jul 14 night):
        # fix-its ride Inbox, nudges ride Waiting, in-Jobber rides the
        # Handled fold — so the chips are exactly these five.
        check("Lanes (5 bubbles: inbox/drafts/won/waiting/techs)",
              "lanechip" in hc and "lane-inbox" in hc
              and "lane-drafts" in hc and "lane-won" in hc
              and "lane-waiting" in hc
              and "data-l='nudge'" not in hc
              and "data-l='fixits'" not in hc)
        check("Scroll keeper (LaRee's jump-to-top fix)",
              "KEEP MY PLACE" in h or "__saveScroll" in h)
        check("Pulse auto-refresh wiring", "/api/pulse" in h)
        check("New lead button", "New lead" in h)

        # a real email customer card
        keys = re.findall(r"href='/\?c=([^']+)'", h)
        email_key = next((k for k in keys if "@" in urllib.parse.unquote(k)), None)
        vm_key = next((k for k in keys
                       if urllib.parse.unquote(k).startswith("vm:")), None)
        if email_key:
            c = get("/?c=" + email_key)
            check("Customer card renders (pinned + folds)",
                  "Conversation" in c)
            check("Full-message expandable bubbles",
                  "msgbody" in c or "No messages logged" in c)
            check("Spam button on email cards", "🚫 Spam" in c
                  or "mark_spam" in c)
            check("Done/step-away buttons",
                  "mark_done" in c or "step_away" in c or "Done — seen" in c)
        if vm_key:
            v = get("/?c=" + vm_key)
            check("Voicemail card: transcript visible",
                  "🎙" in v or "VOICEMAIL" in v)
            check("Voicemail card: NO spam button (protected)",
                  "🚫 Spam" not in v)

        # ── CUSTOMERS TAB ──
        cu = get("/customers")
        check("Customers tab renders", "Search any name" in cu
              or "Directory" in cu or "irow" in cu)
        check("Customers scroll keeper (LaRee)",
              "scroll:/customers:list" in cu)

        # ── SCOREBOARD ──
        sb = get("/scoreboard")
        check("Scoreboard renders", "Scoreboard" in sb or "compared" in sb)
        check("Scoreboard auto-refresh (LaRee)", "sb_scroll" in sb)
        # her name as SALESPERSON ("by Jessica Jensen") is correct and
        # wanted; her house as a CUSTOMER row is what must stay gone
        _txt = re.sub(r"<[^>]+>", " ", sb)
        check("Jessica Jensen excluded as a CUSTOMER (manager)",
              not re.search(r"(?<!by\s)Jessica Jensen", _txt))

        # ── OTHER PAGES ──
        for path, marker, name in [
            ("/winback", "contacted", "Win-back page"),
            ("/settings", "Quick", "Settings page"),
            ("/history", "", "History page"),
            ("/guide", "?", "Guide page"),
            ("/drafts", "", "Drafts page"),
            ("/brief", "", "Morning brief"),
            ("/autodrafts", "grading room", "Auto-drafts + sched scorecard"),
            ("/newbid", "Estimated total", "NEW-DESIGN preview /newbid"),
            ("/lightroutes", "Sammamish", "Lights routes page"),
            ("/lightsched", "mock schedule", "Lights schedule page"),
            ("/", "Tech Questions", "MIRROR is the default view"),
            ("/?classic=1", "laneSwap", "classic lanes at /?classic=1"),
        ]:
            try:
                pg = get(path)
                check(name, (marker in pg) if marker else len(pg) > 500)
            except Exception as e:
                check(name, False, str(e)[:60])

        # F&F ruling text present in Settings
        st = get("/settings")
        check("F&F discount = Sept/Feb/March when slow",
              "March" in st and ("slow" in st or "50%" in st))

        # ── API ──
        pulse = get("/api/pulse")
        check("/api/pulse returns token JSON", "{" in pulse)
        # HTTP BASIC must keep working — the cron's cloudpush courier
        # and every scripted /api call ride on it. A NameError inside
        # the auth catch-all broke it for 14h unnoticed (Jul 22).
        from base64 import b64encode as _b64e
        try:
            _bh = {"Authorization": "Basic "
                   + _b64e(f"office:{pw}".encode()).decode()}
            _bc = urllib.request.urlopen(
                urllib.request.Request(URL + "/api/pulse", headers=_bh),
                timeout=30).status
        except urllib.error.HTTPError as _be:
            _bc = _be.code
        check("HTTP Basic auth accepted on /api (courier path)",
              _bc == 200)

        # ── POSTs (local files only; production untouched) ──
        st_ = post("/mark_seen_bulk", [("keys", "trial@example.com"),
                                       ("keys", "trial2@example.com")])
        marks = json.loads((data / "msg_read.json").read_text()) \
            if (data / "msg_read.json").exists() else {}
        check("POST bulk mark-seen writes marks",
              st_ == 303 and "trial@example.com" in marks)

        st_ = post("/mark_done", {"addr": "trial@example.com", "back": "/"})
        check("POST mark-done (✓ seen it)", st_ == 303)
        st_ = post("/step_away", {"addr": "trial@example.com", "stamp": ""})
        check("POST step-away (hand back)", st_ == 303)

        # spam learner guards
        post("/mark_spam", {"stamp": "", "sender":
                            "☎ Voicemail <messages@copycall.com>"})
        spam = json.loads((data / "learned_spam.json").read_text()) \
            if (data / "learned_spam.json").exists() else []
        check("Spam guard: copycall REFUSED",
              not any("copycall" in s for s in spam))
        post("/mark_spam", {"stamp": "", "sender":
                            "Bad Actor <badactor9@gmail.com>"})
        spam = json.loads((data / "learned_spam.json").read_text()) \
            if (data / "learned_spam.json").exists() else []
        check("Spam guard: gmail learns ADDRESS not domain",
              "badactor9@gmail.com" in spam and "gmail.com" not in spam)

        # decision write (reject on a scratch stamp — no Jobber effects)
        st_ = post("/review", {"stamp": "19990101-000000",
                               "action": "reject",
                               "customer": "Trial <trial@example.com>",
                               "back": "/"})
        rl = json.loads((data / "review_log.json").read_text()) \
            if (data / "review_log.json").exists() else []
        check("POST decision writes review log", st_ == 303 and any(
            r_.get("stamp") == "19990101-000000" for r_ in rl))

        st_ = post("/must_know", {"stamp": "", "address":
                                  "1 Trial St, Monroe, WA 98272",
                                  "text": "TRIAL gate code 0000",
                                  "back": "/"})
        check("POST Must-Know saves", st_ == 303)

    finally:
        srv.terminate()
        # ── restore every touched file ──
        for f, blob in saved.items():
            p = data / f
            if blob is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(blob)

    # ── unit suites ──
    print("\n  — unit suites —")
    for t in ("test_pricing.py", "test_tax.py", "test_lastpaid.py",
              "test_seasons.py", "test_spam_filter.py", "test_parser.py",
              "test_jobber.py", "test_dedup_reply.py",
              "test_mirror_sweep.py", "test_msglog.py",
              "test_sched_confirm.py", "test_sched_offers.py"):
        if not (BASE / t).exists():
            continue
        r = subprocess.run([sys.executable, t], cwd=BASE,
                           capture_output=True, text=True, timeout=300)
        last = (r.stdout.strip().splitlines() or ["?"])[-1]
        check(f"{t}: {last}", r.returncode == 0 and "0 failed" in last
              or "failed" not in last.lower() and r.returncode == 0)

    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\nTRIALS: {ok}/{len(RESULTS)} passed")
    for n, c, note in RESULTS:
        if not c:
            print(f"  FAILED: {n} {note}")
    return ok == len(RESULTS)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
