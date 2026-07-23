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
    # MB_SANDBOX blocks the HTTP courier (cloudpush) but direct DB writes
    # sailed straight past it — a trials run on any machine whose .env
    # carried DATABASE_URL could overwrite live office blobs (Jul 21
    # night sweep). Sandbox = file mode, full stop.
    if os.environ.get("MB_SANDBOX"):
        return False
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
_TTL = 45      # was 10 (Jul 21 perf hunt): every office click 20s apart
               # paid ~20 cold SELECTs + a 1,400-record re-parse → 2s+
               # pages. Writes update/invalidate their own keys in-process
               # (put_blob, ingest_shadow, add_review), and the poller
               # shares this process — so office actions and fresh mail
               # still show INSTANTLY; only cross-process writes (the
               # nightly cron) can lag by up to 45s, which is nothing.
_CACHE = {}                       # key -> (fetched_at, value)
# Write-generation counter (Jul 21 night sweep): three threads share this
# cache (HTTP server, poller, background sweep). Without it, a read that
# was IN FLIGHT when a write invalidated its key would store its pre-write
# snapshot with a fresh timestamp — hiding the office's own action for up
# to 45s, exactly what the TTL comment above promises can't happen.
_GEN = [0]


def _bump():
    _GEN[0] += 1


def _cached(key, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    gen = _GEN[0]
    v = fn()
    if _GEN[0] == gen:            # no write raced this fetch — safe to keep
        if len(_CACHE) > 400:     # bounded (Jul 23 OOM): drop the oldest
            for _k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
                _CACHE.pop(_k, None)
        _CACHE[key] = (time.time(), v)
    return v


# ── shadow records ───────────────────────────────────────────

def ingest_shadow(stamp, record):
    """Insert or update one shadow record. Idempotent by stamp."""
    _exec("INSERT INTO shadow_records (stamp, record) VALUES (%s, %s) "
          "ON CONFLICT (stamp) DO UPDATE SET record = EXCLUDED.record",
          (stamp, json.dumps(record)))
    _CACHE.pop("shadow", None)
    _bump()


def get_shadow(stamp):
    """ONE record, fresh from the DB (no cache) — for read-modify-write
    callers that must not base their write on a minutes-old snapshot
    (Jul 21 night sweep: the delta backfill clobbered office edits made
    mid-pass with loop-start copies)."""
    row = _exec("SELECT record FROM shadow_records WHERE stamp = %s",
                (stamp,), fetch="one")
    return dict(row[0]) if row else None


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


def has_message_id(msg_id):
    """Authoritative 'is this exact email already a record?' check — one
    indexed lookup, straight to the DB, no cache. The poller's in-memory
    `seen` set can go stale/empty (local ledger wiped on a cloud restart,
    or a transient DB error swallowed), and when it did the whole 2-day
    window got re-ingested as fresh duplicate records (Jul 20: 200+ dupes).
    Gating each ingest on this closes that hole regardless of `seen`."""
    if not msg_id:
        return False
    row = _exec("SELECT 1 FROM shadow_records "
                "WHERE record->>'message_id' = %s LIMIT 1",
                (msg_id,), fetch="one")
    return row is not None


# ── review log ───────────────────────────────────────────────

def add_review(entry):
    _exec("INSERT INTO review_log (entry) VALUES (%s)",
          (json.dumps(entry),))
    _CACHE.pop("reviews", None)
    _bump()


def all_reviews():
    rows = _cached("reviews", lambda: _exec(
        "SELECT entry FROM review_log ORDER BY id", fetch="all"))
    return [dict(r[0]) for r in rows]


# ── photos (customer pics + aerial/street tiles) ─────────────

def put_photo(ref, kind, idx, jpeg_bytes):
    _exec("INSERT INTO photos (ref, kind, idx, jpeg) VALUES (%s,%s,%s,%s) "
          "ON CONFLICT (ref, kind, idx) DO UPDATE SET jpeg = EXCLUDED.jpeg",
          (ref, kind, idx, jpeg_bytes))
    for _k in [k for k in list(_CACHE) if k.startswith("pix:")]:
        _CACHE.pop(_k, None)
    _bump()


def photos_index(refs):
    """[(ref, kind, idx), ...] for any of the given refs — cheap listing
    (no image bytes) so pages render fast. CACHED (Jul 23 perf shave):
    the detail card asks 2-3 times per view for the same refs — each a
    full DB round trip; the 45s TTL + put_photo invalidation make repeat
    views near-free, and a fresh photo still shows within one poll."""
    if not refs:
        return []
    key = "pix:" + "|".join(sorted(set(refs)))
    if sum(1 for k in _CACHE if k.startswith("pix:")) > 300:
        for _k in [k for k in list(_CACHE) if k.startswith("pix:")]:
            _CACHE.pop(_k, None)      # bounded — the box has 512MB
    return _cached(key, lambda: _exec(
        "SELECT ref, kind, idx FROM photos WHERE ref = ANY(%s) "
        "ORDER BY kind, idx", (list(refs),), fetch="all"))


def get_photo(ref, kind, idx):
    row = _exec("SELECT jpeg FROM photos WHERE ref=%s AND kind=%s AND idx=%s",
                (ref, kind, idx), fetch="one")
    return bytes(row[0]) if row else None


# ── whole files (the learning store's SQLite file) ───────────
# The pricing-learning DB was LOCAL SQLite only: on the cloud cron the
# disk evaporates at container exit, and the Mac's nightly runner has
# been dead since Jul 16 — "N final prices recorded" was recorded into
# nothing (Jul 21 night sweep; Dallon's Jul 22 ruling: move it here).

_FILES_DDL_DONE = [False]


def _files_table():
    if not _FILES_DDL_DONE[0]:
        _exec("CREATE TABLE IF NOT EXISTS files ("
              "name TEXT PRIMARY KEY, data BYTEA NOT NULL, "
              "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        _FILES_DDL_DONE[0] = True


def put_file(name, blob):
    _files_table()
    _exec("INSERT INTO files (name, data) VALUES (%s, %s) "
          "ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data, "
          "updated_at = now()", (name, blob))


def get_file(name):
    _files_table()
    row = _exec("SELECT data FROM files WHERE name = %s", (name,),
                fetch="one")
    return bytes(row[0]) if row else None


def file_stamp(name):
    """The cloud copy's updated_at (iso string), or None — cheap
    newer-than check so callers skip re-downloading an unchanged file."""
    _files_table()
    row = _exec("SELECT updated_at FROM files WHERE name = %s", (name,),
                fetch="one")
    return row[0].isoformat() if row else None


# ── kv blobs (scoreboard, honor history, brief) ──────────────

def put_blob(key, value):
    _exec("INSERT INTO kv_blobs (key, value) VALUES (%s, %s) "
          "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
          "updated_at = now()", (key, json.dumps(value)))
    _bump()
    _CACHE[f"blob:{key}"] = (time.time(), copy.deepcopy(value))


def merge_blob(key, updates):
    """Atomically fold {k: v} pairs into a JSON-object blob IN THE
    DATABASE (jsonb ||) — no read-modify-write, so concurrent writers
    (dashboard clicks, hourly sweep thread, the nightly's separate
    process) can never overwrite each other's additions (Jul 21 night
    sweep: the cleared/msg_read lost-update races)."""
    if not updates:
        return
    _exec("INSERT INTO kv_blobs (key, value) VALUES (%s, %s) "
          "ON CONFLICT (key) DO UPDATE SET "
          "value = kv_blobs.value || EXCLUDED.value, updated_at = now()",
          (key, json.dumps(updates)))
    _CACHE.pop(f"blob:{key}", None)
    _bump()


# MEMORY DIET (Jul 23: the 512MB box OOM'd twice — resting RSS crept to
# ~325MB because every blob ever read stays in _CACHE forever; the
# ~2,800 per-customer hist:* blobs and a handful of multi-MB reports
# were the bulk). Big/one-per-customer blobs read fresh, uncached.
_NO_CACHE_PREFIX = ("hist:", "routes:")
_NO_CACHE = {"discount_reconciliation", "service_history", "hist_index",
             "churn_report", "samm_routes", "samm_sched"}


def get_blob(key):
    def _fetch():
        row = _exec("SELECT value FROM kv_blobs WHERE key = %s", (key,),
                    fetch="one")
        return row[0] if row else None
    if key in _NO_CACHE or key.startswith(_NO_CACHE_PREFIX):
        return _fetch()
    return copy.deepcopy(_cached(f"blob:{key}", _fetch))
