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
    A starter training library — one card served per day, cycling through.
    Facts are kept factual and high-level (sourced from Amway's own material).
    Grow/trim this to the products you actually sell in Settings, or ask
    Claude to research more cards for your lines.

    IMPORTANT: stick to Amway's official product claims when you talk to
    people — no health/medical cures and no income promises.
    """
    return [
        {"id": "pr_nutrilite", "name": "Nutrilite (the brand)", "line": "Nutrilite",
         "category": "Health & wellness",
         "blurb": "Amway's vitamin, mineral and dietary-supplement brand.",
         "facts": [
             "The world's No. 1 selling brand of vitamins and dietary supplements (Euromonitor).",
             "Grows many of its own plants on 6,000+ acres of certified-organic farmland in the US, Mexico and Brazil.",
             "Uses natural methods on the farms — ladybugs instead of pesticides, compost instead of synthetic fertilizer.",
             "Seed-to-supplement control means they trace ingredients from the farm to the finished product."],
         "starter": "Do you take a daily vitamin? Most people have no idea where the ingredients actually come from.",
         "link": "https://www.google.com/search?q=amway+nutrilite"},
        {"id": "pr_doublex", "name": "Nutrilite Double X", "line": "Nutrilite",
         "category": "Health & wellness",
         "blurb": "The flagship multivitamin — vitamins, minerals and plant nutrients.",
         "facts": [
             "22 vitamins and minerals plus 22 plant concentrates in one supplement.",
             "Includes the PhytoProtect blend (rosemary, turmeric, fava d'anta) for antioxidant support.",
             "Supports heart, brain, bone, eye, immune and skin health with 40+ nutrients.",
             "NSF certified — independently tested for what's on the label."],
         "starter": "If you could cover your whole day's nutrition gaps with one thing, would you want to see it?",
         "link": "https://www.amway.com/en_US/Nutrilite%E2%84%A2-Double-X%E2%84%A2-Multivitamin---31-Day-Supply-Refill-p-A0244"},
        {"id": "pr_protein", "name": "Nutrilite Organics Plant Protein", "line": "Nutrilite",
         "category": "Health & wellness",
         "blurb": "Organic plant protein from brown rice, peas and chia.",
         "facts": [
             "21 g of organic plant protein per serving with all 9 essential amino acids.",
             "No added sugar, and no artificial flavors, colors, preservatives or sweeteners.",
             "Sourced in part from Nutrilite's own organic farms and partner farms.",
             "Comes unflavored, chocolate and vanilla so it mixes into anything."],
         "starter": "What do you usually have for breakfast? A lot of folks are surprised how little protein they get.",
         "link": "https://www.google.com/search?q=amway+nutrilite+organics+plant+protein"},
        {"id": "pr_artistry", "name": "Artistry Skincare", "line": "Artistry",
         "category": "Beauty",
         "blurb": "Amway's premium skincare and color-cosmetics brand.",
         "facts": [
             "Among the world's top-5 largest-selling premium skincare brands (Euromonitor).",
             "Launched in 1968 and holds 250+ patents and patents pending.",
             "Backed by a network of 900+ scientists across research and quality assurance.",
             "Sold entirely person-to-person — not in any retail store."],
         "starter": "What's your skincare routine like right now — happy with it, or always tweaking?",
         "link": "https://www.google.com/search?q=amway+artistry+skincare"},
        {"id": "pr_espring", "name": "eSpring Water Purifier", "line": "Amway Home",
         "category": "Home / water",
         "blurb": "Under-counter water purifier with UV-C LED + carbon filter.",
         "facts": [
             "First and only UV-C LED purifier certified by NSF to all four standards (42, 53, 55, 401).",
             "Reduces 170+ contaminants including microplastics, PFOA/PFOS and pharmaceuticals.",
             "Destroys up to 99.9999% of bacteria and 99.99% of viruses.",
             "UV-C LEDs are built to last up to 10 years — no annual lamp to replace."],
         "starter": "Ever wonder what's actually in your tap water? The list surprised me.",
         "link": "https://www.amway.com/en_US/espring"},
        {"id": "pr_xs", "name": "XS Energy Drink", "line": "XS",
         "category": "Health / energy",
         "blurb": "Sugar-free energy drink with B vitamins and lots of flavors.",
         "facts": [
             "Sugar-free and only about 15 calories per can (0-1 g carbs).",
             "A megadose of B vitamins and ~114 mg caffeine — like a 10 oz coffee.",
             "Colors come from natural ingredients, not artificial dyes.",
             "Certified kosher, with 20+ flavors including caffeine-free options."],
         "starter": "Do you drink energy drinks? Have you ever looked at the sugar in the popular ones?",
         "link": "https://www.google.com/search?q=amway+xs+energy+drink"},
        {"id": "pr_sa8", "name": "Amway Home SA8 Laundry", "line": "Amway Home",
         "category": "Home / cleaning",
         "blurb": "Concentrated laundry detergent from the Legacy of Clean line.",
         "facts": [
             "Concentrated triple-action formula — you use less per load.",
             "Biodegradable with no phosphates or chlorine.",
             "Recognized by the EPA Safer Choice program for safer ingredients.",
             "Rinses clean in any water temperature and is safe for HE washers."],
         "starter": "How much do you spend on laundry stuff a month? Concentrated changes the math.",
         "link": "https://www.amway.com/en_US/Amway-Home%E2%84%A2-SA8%E2%84%A2-Liquid-Laundry-Detergent-p-110478"},
        {"id": "pr_dishdrops", "name": "Amway Home Dish Drops", "line": "Amway Home",
         "category": "Home / cleaning",
         "blurb": "Highly concentrated dishwashing liquid.",
         "facts": [
             "Very concentrated — a small amount makes a lot of suds, so a bottle lasts.",
             "Biodegradable surfactants and no phosphates.",
             "Cuts grease in hot or cold water.",
             "Part of the Legacy of Clean / Amway Home cleaning family."],
         "starter": "Do you go through dish soap fast? The concentrated kind honestly lasts us months.",
         "link": "https://www.google.com/search?q=amway+home+dish+drops"},
        {"id": "pr_glister", "name": "Glister Multi-Action Toothpaste", "line": "Glister",
         "category": "Personal care",
         "blurb": "Fluoride toothpaste with plant-based ingredients.",
         "facts": [
             "Contains fluoride to help prevent cavities, consistent with FDA rules.",
             "Uses a silica polishing agent and Nutrilite-certified peppermint.",
             "Removes plaque and helps minimize buildup with regular brushing.",
             "A little goes a long way — a small strip is enough."],
         "starter": "Random one — do you ever think about what's in your toothpaste?",
         "link": "https://www.google.com/search?q=amway+glister+toothpaste"},
        {"id": "pr_gh", "name": "G&H Body Care", "line": "G&H",
         "category": "Personal care",
         "blurb": "Body wash, lotion and hand care with naturally derived ingredients.",
         "facts": [
             "Built on naturally derived ingredients like orange-blossom honey and shea butter.",
             "Uses Nutrilite white chia seed oil to support skin health.",
             "Includes a patented anti-irritant complex — gentle for the whole family.",
             "A full line: wash, scrub, lotion and hand care that work together."],
         "starter": "What body wash do you use? We switched to one that doesn't dry us out in winter.",
         "link": "https://www.google.com/search?q=amway+g%26h+body+care"},
        {"id": "pr_atmosphere", "name": "Atmosphere Sky Air Purifier", "line": "Atmosphere",
         "category": "Home / air",
         "blurb": "Air treatment system with multi-stage HEPA filtration.",
         "facts": [
             "Three-stage filtration: pre-filter, particulate/HEPA filter and odor/carbon filter.",
             "Designed to capture very small airborne particles including many allergens.",
             "Automatically senses air quality and adjusts fan speed.",
             "Sized for a large room — good for bedrooms and living spaces."],
         "starter": "Anyone in your house deal with allergies? Indoor air is usually worse than people think.",
         "link": "https://www.google.com/search?q=amway+atmosphere+sky+air+purifier"},
        {"id": "pr_satinique", "name": "Satinique Hair Care", "line": "Satinique",
         "category": "Personal care",
         "blurb": "Shampoo, conditioner and treatments for healthy-looking hair.",
         "facts": [
             "A full system — shampoo, conditioner and targeted treatments made to work together.",
             "Formulated to cleanse gently while adding shine and manageability.",
             "Options for different hair needs like volume, smoothing and scalp care.",
             "Part of Amway's beauty portfolio alongside Artistry."],
         "starter": "Are you a one-and-done shampoo person or do you have a whole routine?",
         "link": "https://www.google.com/search?q=amway+satinique+hair+care"},
    ]


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
