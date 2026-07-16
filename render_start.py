"""
MASTER BUTLER — RENDER BOOT SCRIPT (cloud only; never needed locally)

What happens when the Render web service starts:
  1. If DATABASE_URL is set, apply schema.sql to the Postgres database
     (every statement is IF NOT EXISTS — safe to run on every boot).
  2. Start the office dashboard on 0.0.0.0:$PORT.

The dashboard's own guard still applies: it will REFUSE to serve
publicly unless DASHBOARD_PASSWORD is set in the environment.
"""

import os


def apply_schema():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("no DATABASE_URL — skipping schema")
        return
    try:
        import psycopg
    except ImportError:
        print("psycopg not installed — skipping schema")
        return
    sql = open("schema.sql").read()
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print("schema applied (idempotent)")


def start_cloud_ears():
    """THE SWITCH (dark by default): POLL_IN_CLOUD=true + Gmail creds in
    the environment = the inbox watcher runs HERE, and Dallon's Mac is
    retired from listening duty. Requires the always-on (paid) tier —
    the free tier sleeps, and sleeping ears hear nothing."""
    if os.environ.get("POLL_IN_CLOUD", "").lower() != "true":
        print("cloud ears: OFF (POLL_IN_CLOUD not set)")
        return
    if not (os.environ.get("GMAIL_ADDRESS")
            and os.environ.get("GMAIL_APP_PASSWORD")):
        print("cloud ears: OFF (no Gmail credentials in environment)")
        return
    import threading
    import time

    def loop():
        import gmail_poller
        BASE = 120                       # gentler than the old 75s
        delay = BASE
        while True:
            try:
                n = gmail_poller.poll_once()
                gmail_poller._keep_cloud_warm()
                print(f"[cloud ears] poll complete — {n} new")
                delay = BASE             # healthy → back to normal cadence
            except Exception as e:
                # BACK OFF on Gmail's rate limit (Jul 16: polling every
                # 75s into an 'exceeded command/bandwidth limits' block
                # PROLONGS the lockout — the poller was hammering itself
                # stale). Exponential to 15 min; heartbeat still beats so
                # the dashboard knows the loop is alive, just waiting.
                msg = str(e).lower()
                rate = any(w in msg for w in
                           ("exceed", "bandwidth", "limit", "throttl"))
                delay = min(delay * 2, 900) if rate else min(delay + 60, 300)
                try:
                    gmail_poller._keep_cloud_warm()
                except Exception:
                    pass
                print(f"[cloud ears] poll error "
                      f"({'rate-limit backoff' if rate else 'retry'} "
                      f"{delay}s): {e}")
            time.sleep(delay)
    threading.Thread(target=loop, daemon=True).start()
    print("cloud ears: ON — watching the inbox from the cloud (backoff-aware)")


def start_cloud_nightly():
    """THE NIGHTLY, IN THE CLOUD (Dallon, Jul 16): the office-essential
    refreshes fire once a day from here, so they no longer wait on
    Dallon's Mac. A daily-marker blob keeps it to one run/day even
    across restarts. Only runs where cloud ears run (same switch)."""
    if os.environ.get("POLL_IN_CLOUD", "").lower() != "true":
        return
    import threading
    import time

    def loop():
        import cloud_nightly
        while True:
            try:
                now = __import__("datetime").datetime.now()
                # fire in the 21:00–21:59 local hour, once per day
                if now.hour == 21 and not cloud_nightly.already_ran_today():
                    print("[cloud nightly] starting…")
                    cloud_nightly.run()
                    print("[cloud nightly] done")
            except Exception as e:
                print(f"[cloud nightly] error: {e}")
            time.sleep(600)          # check every 10 min
    threading.Thread(target=loop, daemon=True).start()
    print("cloud nightly: ON — office refreshes run from the cloud at 9pm")


if __name__ == "__main__":
    apply_schema()
    os.environ.setdefault("HOST", "0.0.0.0")
    # import AFTER env is set — dashboard reads HOST/PORT at import time
    import dashboard
    from http.server import HTTPServer
    if not dashboard._password():
        raise SystemExit("REFUSING public serve without DASHBOARD_PASSWORD")
    start_cloud_ears()
    start_cloud_nightly()
    print(f"dashboard on {dashboard.HOST}:{dashboard.PORT} (password-protected)")
    HTTPServer((dashboard.HOST, dashboard.PORT),
               dashboard.Handler).serve_forever()
