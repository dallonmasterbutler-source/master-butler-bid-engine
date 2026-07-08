# Master Butler — Automated Bidding System

Reads customer emails → looks up the property (records, satellite,
street view) → analyzes photos → drafts a priced bid → the office
reviews it on one screen → approved bids become **draft** quotes in
Jobber → a reconciler reads final invoices and teaches the system what
really happened.

Built by Dallon Anderson (Master Butler Inc., Monroe WA) with Claude.
Python standard library only — nothing to install.

## The four promises (enforced in code, not by trust)
1. **Nothing sends to a customer, ever.** All "sends" are draft text
   files a human copies out. Jobber quotes are created as drafts only.
2. **The office inbox is sacred.** IMAP readonly + PEEK — the system
   physically cannot mark mail read.
3. **Flag, don't guess.** Unsure means a blank line and a why, never a
   fake number.
4. **Every price traces to a real job.** `test_pricing.py` pins 97
   anchors from real invoices and tech grades; drift fails loudly.

## The pieces
| File | What it does |
|---|---|
| `gmail_poller.py` | watches inbox + spam (readonly), shadow-drafts new requests, links duplicates |
| `email_parser.py` | customer language → structured request |
| `property_data.py` | geocode + Solar roof data + sanity checks |
| `aerial.py` | satellite + street-view imagery → Vision second opinions |
| `vision.py` | customer photos → measurements, buildup, hazards |
| `bid_engine.py` | THE PRICES — one editable config block |
| `pipeline.py` | email in → draft bid out (wires everything above) |
| `jobber_client.py` | draft quotes, custom fields, tax auto-attach |
| `dashboard.py` | office review screen (see docs/OFFICE_PILOT.md) |
| `scoreboard.py` | system vs office — the trust report card |
| `reconciler.py` | reads final invoices; honor-pricing = ground truth |
| `promises.py` | "price for 20XX will be $X" — kept automatically |
| `lights.py` | holiday lights materials pre-measure (labor = Tom) |
| `store.py` | SQLite twin of schema.sql until Render exists |
| `clouddb.py` | Postgres layer (cloud); falls back to files on the Mac |
| `cloudpush.py` | Mac→cloud courier (records, photos, blobs; offline queue) |
| `imgprep.py` | image resize — sips on Mac, Pillow in cloud |
| `render_start.py` | cloud boot: schema, dashboard, optional cloud ears |
| `night_run.py` | evening pass: reconcile, score, resurface, sync, back up, brief |
| `test_pricing.py` | the safety net — run before trusting anything |

## Daily use
See **docs/OFFICE_PILOT.md**. Short version:
`python3 dashboard.py` → http://localhost:8765, and
`python3 gmail_poller.py --watch` to keep shadow mode live.

## Deployment
**LIVE:** https://masterbutler-dashboard.onrender.com (password-protected;
Postgres-backed; auto-deploys from this repo's main branch). The inbox
watcher still runs on Dallon's Mac; its cloud twin is built and dark
(`POLL_IN_CLOUD` — see render_start.py).

Secrets live in `.env` (never committed). Customer data lives in
`data/` (never committed; backed up nightly by night_run).
