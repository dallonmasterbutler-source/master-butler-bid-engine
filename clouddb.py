"""
MASTER BUTLER — CLOUD DATA LAYER (runs on Render; optional everywhere else)

The single shared shelf: shadow records, office decisions, and the
display blobs (scoreboard, honor history) live in Postgres so the
dashboard shows the same truth from any machine.

available() is the seam: on Render (DATABASE_URL + psycopg installed)
everything reads/writes the database; on Dallon's Mac it quietly says
"not here" and the dashboard falls back to the local files — local dev
and the offline test suite keep working untouched.
"""

import json
import os
from pathlib import Path


def _database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return None


def available():
    if not _database_url():
        return False
    try:
        import psycopg  # noqa: F401 — cloud-only dependency
        return True
    except ImportError:
        return False


def _conn():
    import psycopg
    return psycopg.connect(_database_url())


# ── shadow records ───────────────────────────────────────────

def ingest_shadow(stamp, record):
    """Insert or update one shadow record. Idempotent by stamp."""
    with _conn() as con:
        con.execute(
            "INSERT INTO shadow_records (stamp, record) VALUES (%s, %s) "
            "ON CONFLICT (stamp) DO UPDATE SET record = EXCLUDED.record",
            (stamp, json.dumps(record)))
        con.commit()


def all_shadow():
    """Every shadow record, oldest first: [(stamp, record_dict), ...]."""
    with _conn() as con:
        rows = con.execute(
            "SELECT stamp, record FROM shadow_records ORDER BY stamp").fetchall()
    return [(s, r) for s, r in rows]


# ── review log ───────────────────────────────────────────────

def add_review(entry):
    with _conn() as con:
        con.execute("INSERT INTO review_log (entry) VALUES (%s)",
                    (json.dumps(entry),))
        con.commit()


def all_reviews():
    with _conn() as con:
        rows = con.execute("SELECT entry FROM review_log ORDER BY id").fetchall()
    return [r[0] for r in rows]


# ── kv blobs (scoreboard, honor history, brief) ──────────────

def put_blob(key, value):
    with _conn() as con:
        con.execute(
            "INSERT INTO kv_blobs (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = now()", (key, json.dumps(value)))
        con.commit()


def get_blob(key):
    with _conn() as con:
        row = con.execute("SELECT value FROM kv_blobs WHERE key = %s",
                          (key,)).fetchone()
    return row[0] if row else None
