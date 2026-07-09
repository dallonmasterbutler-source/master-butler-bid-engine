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

import copy
import json
import os
import threading
import time
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


# ── ONE connection, reused (Jul 9, "make it faster for Martha"):
#    connect-per-query meant a fresh TLS handshake for every read —
#    a queue render fired 15+ of them. The lock matters because the
#    cloud poller thread shares this process with the page server.
_LOCK = threading.Lock()
_CONN = None


def _exec(sql, params=(), fetch=None):
    global _CONN
    import psycopg
    with _LOCK:
        for attempt in (1, 2):
            try:
                if _CONN is None or _CONN.closed:
                    _CONN = psycopg.connect(_database_url(),
                                            autocommit=True)
                cur = _CONN.execute(sql, params)
                if fetch == "all":
                    return cur.fetchall()
                if fetch == "one":
                    return cur.fetchone()
                return None
            except psycopg.Error:
                try:
                    _CONN.close()
                except Exception:
                    pass
                _CONN = None
                if attempt == 2:
                    raise


# ── 10s read cache: a page render used to re-download the whole
#    shadow table 3-4 times. Writes update/invalidate immediately, so
#    the office always sees its OWN action on the very next page.
_TTL = 10
_CACHE = {}                       # key -> (fetched_at, value)


def _cached(key, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    v = fn()
    _CACHE[key] = (time.time(), v)
    return v


# ── shadow records ───────────────────────────────────────────

def ingest_shadow(stamp, record):
    """Insert or update one shadow record. Idempotent by stamp."""
    _exec("INSERT INTO shadow_records (stamp, record) VALUES (%s, %s) "
          "ON CONFLICT (stamp) DO UPDATE SET record = EXCLUDED.record",
          (stamp, json.dumps(record)))
    _CACHE.pop("shadow", None)


def all_shadow():
    """Every shadow record, oldest first: [(stamp, record_dict), ...].
    Returns per-record dict copies — callers decorate them freely."""
    rows = _cached("shadow", lambda: _exec(
        "SELECT stamp, record FROM shadow_records ORDER BY stamp",
        fetch="all"))
    return [(s, dict(r)) for s, r in rows]


def seen_message_ids():
    """Every Message-ID already shadow-processed — the CLOUD ledger.
    Derived from the records themselves, so the Mac's history counts
    automatically (it mirrored everything up)."""
    rows = _exec("SELECT record->>'message_id' FROM shadow_records",
                 fetch="all")
    return {r[0] for r in rows if r[0]}


# ── review log ───────────────────────────────────────────────

def add_review(entry):
    _exec("INSERT INTO review_log (entry) VALUES (%s)",
          (json.dumps(entry),))
    _CACHE.pop("reviews", None)


def all_reviews():
    rows = _cached("reviews", lambda: _exec(
        "SELECT entry FROM review_log ORDER BY id", fetch="all"))
    return [dict(r[0]) for r in rows]


# ── photos (customer pics + aerial/street tiles) ─────────────

def put_photo(ref, kind, idx, jpeg_bytes):
    _exec("INSERT INTO photos (ref, kind, idx, jpeg) VALUES (%s,%s,%s,%s) "
          "ON CONFLICT (ref, kind, idx) DO UPDATE SET jpeg = EXCLUDED.jpeg",
          (ref, kind, idx, jpeg_bytes))


def photos_index(refs):
    """[(ref, kind, idx), ...] for any of the given refs — cheap listing
    (no image bytes) so pages render fast."""
    if not refs:
        return []
    return _exec("SELECT ref, kind, idx FROM photos WHERE ref = ANY(%s) "
                 "ORDER BY kind, idx", (list(refs),), fetch="all")


def get_photo(ref, kind, idx):
    row = _exec("SELECT jpeg FROM photos WHERE ref=%s AND kind=%s AND idx=%s",
                (ref, kind, idx), fetch="one")
    return bytes(row[0]) if row else None


# ── kv blobs (scoreboard, honor history, brief) ──────────────

def put_blob(key, value):
    _exec("INSERT INTO kv_blobs (key, value) VALUES (%s, %s) "
          "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
          "updated_at = now()", (key, json.dumps(value)))
    _CACHE[f"blob:{key}"] = (time.time(), copy.deepcopy(value))


def get_blob(key):
    def _fetch():
        row = _exec("SELECT value FROM kv_blobs WHERE key = %s", (key,),
                    fetch="one")
        return row[0] if row else None
    return copy.deepcopy(_cached(f"blob:{key}", _fetch))
