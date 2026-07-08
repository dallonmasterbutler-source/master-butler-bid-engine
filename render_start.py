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


if __name__ == "__main__":
    apply_schema()
    os.environ.setdefault("HOST", "0.0.0.0")
    # import AFTER env is set — dashboard reads HOST/PORT at import time
    import dashboard
    from http.server import HTTPServer
    if not dashboard._password():
        raise SystemExit("REFUSING public serve without DASHBOARD_PASSWORD")
    print(f"dashboard on {dashboard.HOST}:{dashboard.PORT} (password-protected)")
    HTTPServer((dashboard.HOST, dashboard.PORT),
               dashboard.Handler).serve_forever()
