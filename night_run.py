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

# 4b ── July running tally (Tom's ask, Jul 14): re-mine the matched
#       window nightly — July 1 → today, both years — so the Scoreboard
#       card is a live year-over-year race, not a snapshot.
try:
    import yoy_compare
    _yy = yoy_compare.run_local(verbose=True)
    if _yy:
        print(f"   yoy tally refreshed through {_yy['window_label']}")
except Exception as e:
    print(f"   (yoy tally skipped: {e})")

# 4c ── SCHEDULING STAGE 1 (Dallon, Jul 14: "run this report tonight")
#       — read a year of Jobber visits + 60 days ahead, learn the
#       routes, write sched_knowledge. Read-only; throttle-tolerant.
try:
    import sched_mine
    _sk = sched_mine.run(verbose=True)
    if _sk:
        print(f"   sched knowledge: {_sk['totals'].get('visits')} visits "
              f"mined, {len(_sk.get('future_anchors') or {})} future "
              f"anchor days mapped")
except Exception as e:
    print(f"   (sched mine skipped: {e})")

# 4d ── LIGHTS DEEP MINE (Dallon, Jul 14): seasonal/early-bird pricing
#       tiers from invoices + front-footage v1 on ~100 lights homes
try:
    import lights_deep_mine
    _lp = lights_deep_mine.run(verbose=True)
    if _lp:
        print(f"   lights pricing: per-ft median "
              f"{_lp['prices'].get('per_ft_median')} · "
              f"{_lp['front_footage_v1']['n']} fronts measured")
except Exception as e:
    print(f"   (lights deep mine skipped: {e})")

# 4e ── PW WIN-BACK LIST (Dallon, Jul 15): rebuilt nightly from the
#       fresh invoice archive, pushed as the pw_winback blob so the
#       /pwwinback page renders on Render (no data/ there).
try:
    import winback
    _wbk = winback.save()
    if _wbk:
        print(f"   pw win-back: {len(_wbk.get('rows') or [])} lapsed "
              f"PW customers, ${_wbk.get('value', 0):,.0f} at last prices")
except Exception as e:
    print(f"   (pw win-back skipped: {e})")

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
    from pathlib import Path
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
    try:
        import mailer
        ok, why = mailer.send_internal(
            f"☀️ Master Butler morning brief — {datetime.now():%b %d}",
            brief_text, to=[mailer.DALLON, mailer.TOM])
        print(f"   brief emailed to Dallon + Tom: {why}")
    except Exception as e:
        print(f"   (brief email skipped: {e})")
except Exception as e:
    print(f"   brief skipped ({e})")

# 5b ── QA self-check: every live bid page must render
try:
    import json
    import urllib.request
    from base64 import b64encode
    from pathlib import Path
    from cloudpush import _cfg
    url, pw = _cfg("DASHBOARD_URL"), _cfg("DASHBOARD_PASSWORD")
    if url and pw:
        hdr = {"Authorization": "Basic "
               + b64encode(f"office:{pw}".encode()).decode()}
        recs = json.load(urllib.request.urlopen(
            urllib.request.Request(url.rstrip("/") + "/api/records",
                                   headers=hdr), timeout=120))
        bad = []
        for r in recs:
            try:
                h = urllib.request.urlopen(urllib.request.Request(
                    f"{url.rstrip('/')}/bid/{r['stamp']}", headers=hdr),
                    timeout=45).read().decode()
                if "Small hiccup" in h or len(h) < 2000:
                    bad.append(r["stamp"])
            except Exception:
                bad.append(r["stamp"])
        msg = (f"   QA: {len(recs)} bid pages checked, "
               f"{len(bad)} broken" + (f" -> {bad[:5]}" if bad else " ✅"))
        print(msg)
        if bad:
            Path("data/brief_pin.txt").write_text(
                f"🔴 QA ALERT: {len(bad)} bid page(s) failing to render: "
                f"{', '.join(bad[:6])} — tell Claude.\n")
except Exception as e:
    print(f"   QA check skipped ({e})")

# 5b ── refresh the due-for-annual list (books itself out of date as
# customers schedule) + rerun the DNS sweep periodically
try:
    import due_soon
    due_soon.run()
except Exception as e:
    print(f"   due-soon refresh skipped ({e})")
try:
    import servicehistory
    servicehistory.refresh(recent=120)     # keep 'Past here' current
except Exception as e:
    print(f"   service-history refresh skipped ({e})")
try:
    from datetime import date as _date
    if _date.today().weekday() == 6:       # Sundays: refresh DNS list
        import dns_sweep
        dns_sweep.sweep()
        print("   DNS sweep refreshed (weekly)")
except Exception as e:
    print(f"   DNS sweep skipped ({e})")

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
    pin = Path("data") / "brief_pin.txt"
    if pin.exists():                # the 📋 Brief tab builds live in the
        blobs["brief_pin"] = pin.read_text()   # cloud — it needs the pin
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
