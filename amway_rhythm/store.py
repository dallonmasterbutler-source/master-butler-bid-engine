"""
RHYTHM — DATA LAYER (the shelf for the couple's scheduling app)

Same seam idea as the bid engine's clouddb: on Render (DATABASE_URL +
psycopg) everything reads/writes one JSON blob in Postgres so both phones
see the same truth; on a plain machine it quietly falls back to a local
JSON file so dev + the offline tests keep working.

Everything is one document ("state") — this app is small and the two of
them are the only writers, so a single read-modify-write blob is simpler
and safer than a pile of tables. A process lock serializes writers.
"""

import copy
import json
import os
import threading
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "rhythm.json"

_LOCK = threading.RLock()
_CACHE = None


def _default_state():
    """A fresh, usable state — names are placeholders until Settings is used."""
    return {
        "config": {
            "person_a": "You",
            "person_b": "Your wife",
            "monthly_goal": 200,          # combined contacts/conversations target
            # the daily windows we're willing to be "out and about" in, local
            # time, used to find shared free slots. 24h "HH:MM" strings.
            "free_windows": {
                "weekday": ["17:00", "21:00"],
                "weekend": ["09:00", "21:00"],
            },
        },
        # activity log — the heart. one row per logged conversation/contact.
        "activity": [],                    # {id, ts, day, person, kind, name, note}
        # daily product training
        "products": _seed_products(),      # [ {id, name, line, category, blurb, facts, starter, link} ]
        "training_log": [],                # [ {day, product_id, person} ]
        # meal planning
        "meal_library": _seed_meals(),     # {name: [ {item, category, qty} ]}
        "week_plan": {},                   # {"YYYY-MM-DD"(mon): {"mon":[names],...}}
        "stores": _seed_stores(),          # [ {id, name, area, categories:[...]} ]
        # outings
        "places": _seed_places(),          # [ {id, name, type, area, notes, last_visited} ]
        # google calendar oauth tokens, per person key ("a"/"b")
        "gcal": {},                        # {"a": {tokens...}, "b": {...}}
        # anything we've pushed onto calendars, so we don't double-add
        "scheduled": [],                   # {id, kind, title, start, end, person, gcal_id}
        "_seq": 0,
    }


def _seed_products():
    """
    The daily-training library. Single source of truth is products.json in
    this folder (shared with the shareable team_edition.html). Falls back to a
    tiny built-in set if the file is somehow missing so the app still runs.
    Each product: id, name, line, category, blurb, facts[], starter, link.
    Keep to Amway's official, factual claims — no health/medical or income promises.
    """
    path = Path(__file__).parent / "products.json"
    try:
        rows = json.loads(path.read_text())
        out = []
        for r in rows:
            r = dict(r)
            r["category"] = r.pop("cat", r.get("category", ""))
            out.append(r)
        if out:
            return out
    except Exception:
        pass
    return [{
        "id": "pr_nutrilite", "name": "Nutrilite (the brand)", "line": "Nutrilite",
        "category": "Health & wellness",
        "blurb": "Amway's vitamin, mineral and dietary-supplement brand.",
        "facts": ["The world's #1 selling brand of vitamins and dietary supplements."],
        "starter": "Do you take a daily vitamin?",
        "link": "https://www.google.com/search?q=amway+nutrilite"}]


def _seed_meals():
    return {
        "Sheet-pan chicken & veggies": [
            {"item": "chicken thighs", "category": "meat", "qty": "2 lb"},
            {"item": "broccoli", "category": "produce", "qty": "2 heads"},
            {"item": "potatoes", "category": "produce", "qty": "3 lb"},
            {"item": "olive oil", "category": "pantry", "qty": "1"},
        ],
        "Taco night": [
            {"item": "ground beef", "category": "meat", "qty": "1.5 lb"},
            {"item": "tortillas", "category": "pantry", "qty": "2 packs"},
            {"item": "lettuce", "category": "produce", "qty": "1"},
            {"item": "cheese", "category": "dairy", "qty": "1 bag"},
            {"item": "salsa", "category": "pantry", "qty": "1 jar"},
        ],
        "Spaghetti": [
            {"item": "pasta", "category": "pantry", "qty": "2 boxes"},
            {"item": "marinara", "category": "pantry", "qty": "2 jars"},
            {"item": "ground beef", "category": "meat", "qty": "1 lb"},
            {"item": "garlic bread", "category": "bakery", "qty": "1"},
        ],
        "Breakfast-for-dinner": [
            {"item": "eggs", "category": "dairy", "qty": "1 dozen"},
            {"item": "bacon", "category": "meat", "qty": "1 lb"},
            {"item": "pancake mix", "category": "pantry", "qty": "1"},
            {"item": "syrup", "category": "pantry", "qty": "1"},
        ],
        "Big salad + rotisserie chicken": [
            {"item": "rotisserie chicken", "category": "deli", "qty": "1"},
            {"item": "salad greens", "category": "produce", "qty": "2 bags"},
            {"item": "tomatoes", "category": "produce", "qty": "1 lb"},
            {"item": "dressing", "category": "pantry", "qty": "1"},
        ],
    }


def _seed_stores():
    # Deliberately a few different spots around Monroe/Snohomish County so a
    # week's shopping naturally spreads you out. Edit freely in Settings.
    return [
        {"id": "s1", "name": "Fred Meyer (Monroe)", "area": "Monroe",
         "categories": ["produce", "pantry", "dairy", "bakery", "deli"]},
        {"id": "s2", "name": "Costco (Woodinville)", "area": "Woodinville",
         "categories": ["meat", "bulk", "produce"]},
        {"id": "s3", "name": "Safeway (Snohomish)", "area": "Snohomish",
         "categories": ["pantry", "dairy", "deli", "bakery"]},
    ]


def _seed_places():
    return [
        {"id": "p1", "name": "Lake Tye Park", "type": "park", "area": "Monroe",
         "notes": "Big playground, walking loop — lots of families.", "last_visited": ""},
        {"id": "p2", "name": "Farmers Market", "type": "event", "area": "Monroe",
         "notes": "Saturdays, summer. Great for conversations.", "last_visited": ""},
        {"id": "p3", "name": "Library story time", "type": "kids", "area": "Monroe",
         "notes": "Weekday mornings — other parents.", "last_visited": ""},
        {"id": "p4", "name": "Costco", "type": "store", "area": "Woodinville",
         "notes": "Samples + friendly crowd.", "last_visited": ""},
        {"id": "p5", "name": "Al Borlin Park", "type": "park", "area": "Monroe",
         "notes": "Riverside trails.", "last_visited": ""},
    ]


def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    # cloud shelf first
    if _cloud_available():
        _CACHE = _cloud_load()
        return _CACHE
    if DATA_FILE.exists():
        try:
            _CACHE = json.loads(DATA_FILE.read_text())
        except Exception:
            _CACHE = _default_state()
    else:
        _CACHE = _default_state()
    _merge_defaults(_CACHE)
    return _CACHE


def _merge_defaults(state):
    """Make sure older saved blobs gain any newly-added keys."""
    base = _default_state()
    for k, v in base.items():
        if k not in state:
            state[k] = v
    for k, v in base["config"].items():
        state["config"].setdefault(k, v)


def get():
    """Return a deep copy of the whole state (read-only use)."""
    with _LOCK:
        return copy.deepcopy(_load())


def update(fn):
    """
    Atomically read-modify-write. `fn(state)` mutates the dict in place;
    may return a value which update() passes back to the caller.
    """
    global _CACHE
    with _LOCK:
        state = _load()
        result = fn(state)
        _persist(state)
        _CACHE = state
        return result


def next_id(state, prefix="x"):
    state["_seq"] = state.get("_seq", 0) + 1
    return f"{prefix}{state['_seq']}"


def _persist(state):
    if _cloud_available():
        _cloud_save(state)
        return
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(DATA_FILE)


# ── cloud seam (Postgres single-row JSON) ────────────────────────────────
def _database_url():
    url = os.environ.get("DATABASE_URL")
    return url or None


def _cloud_available():
    if not _database_url():
        return False
    try:
        import psycopg  # noqa: F401
        return True
    except ImportError:
        return False


def _cloud_exec(sql, params=(), fetch=None):
    import psycopg
    with psycopg.connect(_database_url(), autocommit=True) as conn:
        cur = conn.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        return None


def _cloud_load():
    _cloud_exec("CREATE TABLE IF NOT EXISTS rhythm_state "
                "(id INT PRIMARY KEY, blob JSONB)")
    row = _cloud_exec("SELECT blob FROM rhythm_state WHERE id=1", fetch="one")
    if row and row[0]:
        state = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        _merge_defaults(state)
        return state
    state = _default_state()
    _cloud_save(state)
    return state


def _cloud_save(state):
    _cloud_exec(
        "INSERT INTO rhythm_state (id, blob) VALUES (1, %s) "
        "ON CONFLICT (id) DO UPDATE SET blob = EXCLUDED.blob",
        (json.dumps(state),),
    )


def _reset_for_tests():
    """Test helper: wipe cache + local file so each test starts clean."""
    global _CACHE
    with _LOCK:
        _CACHE = None
        if DATA_FILE.exists():
            DATA_FILE.unlink()
