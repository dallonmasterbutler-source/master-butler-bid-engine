# Master Butler Bid Engine — How To Run It (plain English)

Everything below is SAFE: shadow mode never touches the office inbox's
unread counts, and nothing here can send anything to a customer. All
"sends" are text drafts a human copies out by hand.

Open Terminal, then:

```
cd ~/master-butler-bid-engine
```

## The dashboard (review bids on one screen)
```
python3 dashboard.py
```
Then open **http://localhost:8765** in any browser (works on iPad on the
same network too, using the computer's address instead of localhost).

- Top yellow band = things that need eyes NOW (requests found in spam,
  pipeline errors, bids waiting past 24 hours)
- The list is oldest first; age turns red near the 24-hour promise
- Click a name → everything on one screen: the parsed request, EVERY
  note in one stack, the system's draft, and what similar homes were
  honor-corrected to in the past
- Buttons: **Approve** / **Adjusted** (pick a reason — they're the
  office's own words) / **Escalate** (makes the standardized Dallon/Tom
  form in `data/escalations/`) / **Draft photo-request email** (lands in
  `data/outbox_drafts/` — copy it into Gmail if you like it)

## The inbox watcher (shadow mode)
```
python3 gmail_poller.py --watch
```
Checks the office inbox (AND the spam folder) every 2 minutes, read-only.
New requests get a silent shadow draft in `data/shadow_bids/` which the
dashboard shows. Stop it anytime with Ctrl-C. It cannot mark anything
read — that's enforced by the connection type, not by trust.

## One-off tools
```
python3 pipeline.py <somefile.eml>     # run one email through everything
python3 aerial.py <address>            # straight-down look at a property
python3 reconciler.py 200              # re-check the last 200 invoices
python3 test_pricing.py                # the 84-test pricing safety net
```

## The rules the machine lives by
- Draft only. A human clicks send, in Jobber, every time.
- Flag, don't guess. Unsure = blank line + a why, never a fake number.
- The office's unread email is sacred.
- Every price traces to a real job (test_pricing.py enforces 84 of them).

## Known limits (honest list)
- Aerial imagery: rural spots may use old Solar flights (the note says
  the year) or have none at all (Gold Bar). Enabling "Maps Static API"
  in Google Cloud fixes both — one click, Dallon has the account.
- Holiday lights: labor is still Tom's call; the system only drafts
  materials and forwards.
- Auto-send is hard-locked OFF for the first 6 months. Not a setting.
