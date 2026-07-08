"""
MASTER BUTLER — NIGHT RUN (one command = the whole housekeeping pass)

Run it any evening (or let a scheduler run it later):

    python3 night_run.py

What it does, in order — all read-only against Jobber:
  1. Reconciler: re-checks the last 200 invoices for new honor
     corrections and price promises
  2. Scoreboard: matches shadow drafts to the office's newest quotes
  3. Holds: lists anything resurfacing tomorrow so mornings start informed

Prints a short report; details land in data/ for the dashboard.
"""

from datetime import datetime, timedelta

print("═" * 56)
print(f"MASTER BUTLER NIGHT RUN — {datetime.now():%B %d, %Y %I:%M %p}")
print("═" * 56)

# 1 ── reconciler (recent slice, polite)
print("\n[1/4] Reconciler — recent invoices…")
try:
    import reconciler
    scanned, found = reconciler.sweep(limit=200)
    honors = [f for f in found if "honor" in f["categories"]]
    promises = [f for f in found if f.get("next_year_price")]
    print(f"   {scanned} scanned · {len(honors)} honor corrections · "
          f"{len(promises)} price promises")
    import json
    from pathlib import Path
    out = Path("data") / "discount_reconciliation_recent.json"
    out.write_text(json.dumps(found, indent=1))
except Exception as e:
    print(f"   skipped ({e})")

# 2 ── scoreboard
print("\n[2/4] Scoreboard — shadow drafts vs office quotes…")
try:
    import scoreboard
    r = scoreboard.run(limit=60)
    matched = [x for x in r["rows"] if x.get("office_quote")]
    print(f"   {r['shadow_drafts']} shadow drafts · {len(matched)} matched")
    for row in matched:
        print(f"     {row['customer'][:34]:<34} sys ${row['system_total']:.0f}"
              f" / office ${row['office_total']:.0f} ({row['gap_pct']:+.0f}%)")
except Exception as e:
    print(f"   skipped ({e})")

# 3 ── holds resurfacing
print("\n[3/4] Holds resurfacing in the next 2 days…")
try:
    import dashboard
    live, resurfaced = dashboard.active_holds()
    soon = datetime.now().date() + timedelta(days=2)
    coming = {s: h for s, h in live.items()
              if (h.get("hold_until") or "9999") <= soon.isoformat()}
    for s, h in {**resurfaced, **coming}.items():
        print(f"   ⏰ {h.get('customer', s)} — {h.get('hold_reason')} "
              f"(due {h.get('hold_until')})")
    if not resurfaced and not coming:
        print("   none")
except Exception as e:
    print(f"   skipped ({e})")

# 4 ── database sync (idempotent)
print("\n[4/4] Database sync…")
try:
    import store
    r, l = store.sync()
    counts = store.report()
    print(f"   +{r} request(s), +{l} bid line(s) — DB now: "
          f"{counts['requests']} requests / {counts['bids']} bids / "
          f"{counts['audit_log']} audit entries")
except Exception as e:
    print(f"   skipped ({e})")

print("\nDone. Dashboard reads all of this automatically.")
