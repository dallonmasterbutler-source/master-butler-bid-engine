# Rhythm — a scheduling + contacts app for Dallon & his wife

A small, private web app for the two of you to run your Amway life together:

1. **Meals & shopping** — plan the week's dinners and it splits the grocery
   list across your stores, so ordinary errands spread you out and get you
   in front of more people.
2. **Outings** — a rotating list of parks, stores, kids events and community
   spots. It suggests the freshest ones and (once calendars are linked) drops
   them into time you're *both* free.
3. **Numbers** — tap **+1** every time either of you has a conversation, an
   interaction, or picks up a new contact. It rolls up daily and monthly with
   a goal and a 6-month trend. This is the engine of the business.
4. **Daily training** — one Amway product a day, cycling through your library:
   a short fact card plus a ready-to-use "talk-starter" question, with a
   "trained today" streak. Manage the products in Settings, or have Claude
   research more cards for the lines you sell.
5. **Shared calendar** — link both Google calendars once (OAuth) so Rhythm
   reads when you're *really* both free; adding an event opens the Google app
   with it pre-filled, so your calendar stays the source of truth.

It's completely separate from the Master Butler bidding system — its own
folder, its own data, nothing to do with customers or Jobber.

## Two editions

- **Couple edition** (this Python app) — the full thing for you and your wife:
  meals/shopping, outings, calendar OAuth, the works. Runs on a server.
- **Team edition** (`team_edition.html`) — a single self-contained web page with
  just **daily training + tracking**, made to *share*. No accounts, no server,
  no you-as-admin: each person opens the link on their phone and their data
  lives only in their own browser. Send it to a team member, they “Add to Home
  Screen,” done. Open the file in any browser to try it — nothing to install.
  It never scrapes amway.com; the product cards are baked into the page.
  - **Today** rotates one product a day (talk-starter + learned-today streak).
  - **Study** tab lets anyone browse the whole library on their own — search
    and category filters — *without* touching the daily card or the streak.
  - **Progress** tab: day/month totals, goal ring, per-kind, 6-month history.

### The product library
Both editions read the same **`products.json`** — one source of truth, 55 cards
covering Nutrilite, XS/BodyKey fitness, Artistry/beauty, and Amway Home/durables.
Granularity is **one card per distinct product or family**, never per
shade/flavor/scent (all lipsticks are one “Artistry Makeup” card; XS gets
separate cards for its genuinely different types). Facts are Amway’s own,
factual, non-medical. To add or edit cards, change `products.json`; the Python
app picks it up automatically. To refresh the shareable page’s inlined copy,
re-run the build step that injects `products.json` into `team_edition.html`.

## Run it locally (your Mac)
```
cd amway_rhythm
RHYTHM_PASSCODE=picksomething python3 app.py
```
Open <http://localhost:8100>, enter the passcode. Data saves to
`amway_rhythm/data/rhythm.json`.

## Run the tests (before any change)
```
python3 test_rhythm.py
```

## Hosting it (so it's on both your phones)
It runs on Render exactly like the office dashboard:
- **Start command:** `python3 app.py`
- **Env vars:**
  - `RHYTHM_PASSCODE` — the shared password you both type in.
  - `RHYTHM_SECRET` — any long random string (signs the login cookie).
  - `RHYTHM_BASE_URL` — your Render URL, e.g. `https://rhythm.onrender.com`
    (needed so Google can redirect back after login).
  - `DATABASE_URL` — *optional.* If set (and `psycopg` installed), state lives
    in Postgres so both phones share one source of truth. Without it, it uses
    the local JSON file — perfect for trying it on your Mac.
  - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — for calendar linking. See
    **SETUP_GOOGLE.md**. Everything else works without them.

Add it to a phone home screen: open the URL in Safari → Share → *Add to Home
Screen*. It behaves like an app.

## What's where
- `app.py` — the web app (pages, buttons, routing). Stdlib only.
- `planner.py` — the brains: rollups, grocery split, free-window math,
  outing suggestions. All pure + tested.
- `store.py` — where data lives (local JSON file, or Postgres in the cloud).
- `gcal.py` — Google Calendar linking (read your free/busy, write events).
- `test_rhythm.py` — the test gate.
- `SETUP_GOOGLE.md` — the one-time Google Calendar setup.

## The rules this app keeps
- Private to the two of you (one shared passcode).
- It only ever writes to **your own** calendars — never anyone else's, never
  anything customer-facing.
- Start simple. Meal planning stays light on purpose — no calorie math, just
  "what's for dinner and who buys what where."
- Product training sticks to Amway's official, factual claims — no health/
  medical cures and no income promises. The app carries a reminder of that.
- It doesn't scrape amway.com. The product library is curated (seeded here and
  editable in Settings), so it never breaks when their website changes.
