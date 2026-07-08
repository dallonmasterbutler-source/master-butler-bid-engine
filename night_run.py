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
print("\n[1/6] Reconciler — recent invoices…")
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
print("\n[2/6] Scoreboard — shadow drafts vs office quotes…")
try:
    import scoreboard
    r = scoreboard.run(limit=60)
    matched = [x for x in r["rows"] if x.get("office_quote")]
    print(f"   {r['shadow_drafts']} shadow drafts · {len(matched)} matched")
    if matched:
        try:
            import store
            n = store.record_office_quotes(r)
            print(f"   {n} final price(s) recorded into the learning DB")
        except Exception as e:
            print(f"   (learning-record update skipped: {e})")
    for row in matched:
        print(f"     {row['customer'][:34]:<34} sys ${row['system_total']:.0f}"
              f" / office ${row['office_total']:.0f} ({row['gap_pct']:+.0f}%)")
except Exception as e:
    print(f"   skipped ({e})")

# 3 ── holds resurfacing
print("\n[3/6] Holds resurfacing in the next 2 days…")
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

# 4 ── database sync (idempotent) — cloud decisions come DOWN first,
#      so the office's reason taps land in the local learning store
print("\n[4/6] Database sync…")
try:
    import json
    from pathlib import Path
    from cloudpush import pull_reviews
    reviews = pull_reviews()
    if reviews:
        Path("data/review_log.json").write_text(json.dumps(reviews, indent=1))
        print(f"   {len(reviews)} office decision(s) pulled from the cloud")
except Exception as e:
    print(f"   (review pull skipped: {e})")
try:
    import store
    r, l = store.sync()
    counts = store.report()
    print(f"   +{r} request(s), +{l} bid line(s) — DB now: "
          f"{counts['requests']} requests / {counts['bids']} bids / "
          f"{counts['audit_log']} audit entries")
except Exception as e:
    print(f"   skipped ({e})")

# 5 ── backup + morning brief
print("\n[5/6] Backup + morning brief…")
try:
    import zipfile
    from pathlib import Path
    data = Path("data")
    bdir = data / "backups"
    bdir.mkdir(exist_ok=True)
    zpath = bdir / f"backup-{datetime.now():%Y%m%d}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in data.rglob("*"):
            if (f.is_file() and "backups" not in f.parts
                    and "aerial" not in f.parts     # re-fetchable
                    and "photos" not in f.parts):   # re-minable (3 GB!)
                z.write(f, f.relative_to(data))
    print(f"   backup -> {zpath} ({zpath.stat().st_size // 1024} KB)")
    # keep the last 14 nightly backups
    olds = sorted(bdir.glob("backup-*.zip"))[:-14]
    for o in olds:
        o.unlink()
except Exception as e:
    print(f"   backup skipped ({e})")
# CLOUD-MEMORY BACKUP: pull the database's whole brain down nightly.
try:
    import gzip
    import urllib.request
    from base64 import b64encode
    from cloudpush import _cfg
    url, pw = _cfg("DASHBOARD_URL"), _cfg("DASHBOARD_PASSWORD")
    if url and pw:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/backup",
            headers={"Authorization": "Basic "
                     + b64encode(f"office:{pw}".encode()).decode()})
        raw = urllib.request.urlopen(req, timeout=300).read()
        bdir = Path("data/backups"); bdir.mkdir(parents=True, exist_ok=True)
        out = bdir / f"cloud-{datetime.now():%Y%m%d}.json.gz"
        out.write_bytes(gzip.compress(raw))
        print(f"   cloud backup -> {out} ({out.stat().st_size//1024} KB)")
        for o in sorted(bdir.glob("cloud-*.json.gz"))[:-14]:
            o.unlink()
except Exception as e:
    print(f"   cloud backup skipped ({e})")

try:
    import mailer
    n = mailer.drain_outbox()
    if n:
        print(f"   relayed {n} queued internal email(s)")
except Exception as e:
    print(f"   outbox skipped ({e})")
brief_text = None
try:
    import digest
    path, brief_text = digest.write()
    print(f"   morning brief -> {path}")
except Exception as e:
    print(f"   brief skipped ({e})")

# 6 ── mirror display data to the cloud dashboard
print("\n[6/6] Cloud mirror…")
try:
    import json
    from pathlib import Path
    from cloudpush import push, flush_pending
    n = flush_pending()
    blobs = {}
    for key, fname in (("scoreboard", "scoreboard.json"),
                       ("discount_reconciliation",
                        "discount_reconciliation.json")):
        f = Path("data") / fname
        if f.exists():
            blobs[key] = json.loads(f.read_text())
    if brief_text:
        blobs["brief"] = brief_text
    senders = Path("data") / "internal_senders.txt"
    if senders.exists():
        blobs["internal_senders"] = [
            l.strip() for l in senders.read_text().splitlines()
            if l.strip() and not l.startswith("#")]
    n += push(blobs=blobs)
    print(f"   {n} item(s) mirrored to the cloud")
except Exception as e:
    print(f"   cloud mirror skipped ({e})")

print("\nDone. Dashboard reads all of this automatically.")
