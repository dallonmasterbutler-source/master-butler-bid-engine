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
            from heartbeats import beat
            beat("learning", recorded=n, matched=len(matched))
        except Exception as e:
            print(f"   (learning-record update skipped: {e})")
    for row in matched:
        # None-guarded like the digest (a None total here TypeError'd the
        # step and masked the real log line — Jul 21 night sweep)
        print(f"     {(row.get('customer') or '?')[:34]:<34} "
              f"sys ${row.get('system_total') or 0:.0f}"
              f" / office ${row.get('office_total') or 0:.0f} "
              f"({row.get('gap_pct') or 0:+.0f}%)")
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
# ONE-TIME FULL HISTORY REBUILD FIRST (Jul 15: the pre-discount scaler
# was double-inflating invoices carrying a standalone discount line —
# Tracy Van Horn's $371 windows became a $727 floor. Only a fresh
# sweep applies the fix; the nightly merge keeps poisoned entries.)
# Runs BEFORE yoy/win-back so tonight's numbers use clean data.
try:
    from datetime import date as _D
    # BLOB-gated, not file-gated (Jul 16 cloud-ify): a local marker
    # doesn't survive the cron's ephemeral disk → the 2-3h rebuild
    # would re-run EVERY night. The blob is permanent, so it runs once.
    import clouddb as _cdb
    _done = bool((_cdb.get_blob("history_rebuilt_jul15") or {}).get("done")) \
        if _cdb.available() else True
    if not _done and _D.today() >= _D(2026, 7, 16):
        import servicehistory
        print("   FULL service-history rebuild (discount-line fix)…")
        servicehistory.sweep()
        _cdb.put_blob("history_rebuilt_jul15", {"done": True})
except Exception as e:
    print(f"   (history rebuild skipped: {e})")

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

# 4c-bis ── SHADOW OFFERS FOR EVERY APPROVED CUSTOMER (Dallon, Jul 22:
#   "we've locked in multiple jobs since this went live — how can the
#   scorecard show zero?" Because capture only fired when someone
#   VIEWED the grading room. Now every approved-awaiting-schedule
#   customer gets their shadow offer logged nightly, automatically —
#   capture() is first-seen-wins, so views and nights never overwrite.)
try:
    import re as _re4
    import clouddb as _cdb4
    import sched_offers as _so4
    import sched_scorecard as _sc4
    _cap = 0
    if _cdb4.available():
        for _st4, _r4 in _cdb4.all_shadow():
            if _r4.get("merged_into") or _r4.get("spam_auto") \
                    or _r4.get("tech_sender") \
                    or _r4.get("kind") == "jobber_event":
                continue
            _cx4 = _r4.get("open_quote_ctx") or {}
            if (_cx4.get("status") or "").lower() != "approved":
                continue
            _m4 = _re4.search(r"<([^>]+)>", _r4.get("from") or "")
            _em4 = (_m4.group(1).lower() if _m4 else "")
            if "@" not in _em4:
                continue
            _o4 = _so4.offer(_r4)
            if _o4:
                _nm4 = (_r4.get("from") or "").split("<")[0].strip()
                _sc4.capture(_em4, _nm4 or _em4,
                             _r4.get("address"), _o4)
                _cap += 1
    print(f"   shadow offers swept for {_cap} approved customer(s)")
    # grade AGAINST TONIGHT'S bookings — match must run AFTER capture
    # (Jul 23: it ran before, so fresh offers waited a full day)
    _g4 = _sc4.fetch_and_match(verbose=True)
    from heartbeats import beat
    beat("scorecard", captured=_cap, graded_total=_g4)
except Exception as e:
    print(f"   (shadow offer sweep skipped: {e})")

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

# 4f ── SQFT TRUTH SWEEP (Dallon, Jul 15): what sqft has the office
#       put INTO Jobber, and where does it disagree with our sources
try:
    import sqft_mine
    _sqm = sqft_mine.run(verbose=True)
    if _sqm:
        print(f"   sqft compare: {_sqm['with_sqft']} Jobber values, "
              f"{len(_sqm['disagree_15pct'])} disagreements >15%")
except Exception as e:
    print(f"   (sqft sweep skipped: {e})")

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
# On the Mac the .gz lands in data/backups (14 kept). On the CRON the
# disk is ephemeral — writing there was a restore point that vanished
# with the container — so the .gz is EMAILED to Dallon instead: Gmail is
# off-site storage that's always awake (Jul 21 audit: the Mac's pull had
# been dead since Jul 16 — five days with no restore point).
try:
    import gzip
    import os as _os
    import urllib.request
    from base64 import b64encode
    from pathlib import Path
    from cloudpush import _cfg
    url, pw = _cfg("DASHBOARD_URL"), _cfg("DASHBOARD_PASSWORD")
    if url and pw:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/backup",
            headers={"Authorization": "Basic "
                     + b64encode(f"office:{pw}".encode()).decode(),
                     "Accept-Encoding": "gzip"})
        _resp = urllib.request.urlopen(req, timeout=300)
        raw = _resp.read()
        if (_resp.headers.get("Content-Encoding") or "") == "gzip":
            raw = gzip.decompress(raw)   # travels ~3MB, stored re-gzipped
        # a dump this small is no restore point (an unreachable DB
        # returns '{}' — gzipping and mailing that as 'the full backup'
        # is a quiet lie; Jul 21 night sweep)
        if len(raw) < 10_000:
            raise RuntimeError(f"dump suspiciously small ({len(raw)} B) "
                               "— NOT a restore point, not emailed")
        gz = gzip.compress(raw)
        if _os.environ.get("RENDER"):        # cloud → email it off-site
            import mailer
            ok, why = mailer.send_internal(
                f"💾 Master Butler nightly backup — {datetime.now():%b %d}",
                "Attached: tonight's full cloud-memory restore point "
                "(records, decisions, learning — photos regenerate). "
                "If the database ever dies, this file brings it back.\n\n"
                "Nothing to do — just let it sit in Gmail.",
                to=["dallon.masterbutler@gmail.com"],
                attachment=(f"masterbutler-backup-{datetime.now():%Y%m%d}"
                            ".json.gz", gz))
            if ok:
                print(f"   cloud backup emailed to Dallon "
                      f"({len(gz)//1024} KB): {why}")
                from heartbeats import beat
                beat("backup", kb=len(gz)//1024)
            else:
                print(f"   ⚠️ CLOUD BACKUP EMAIL FAILED — no restore "
                      f"point tonight ({len(gz)//1024} KB ready): {why}")
        else:                                # Mac → keep the local shelf
            bdir = Path("data/backups")
            bdir.mkdir(parents=True, exist_ok=True)
            out = bdir / f"cloud-{datetime.now():%Y%m%d}.json.gz"
            out.write_bytes(gz)
            print(f"   cloud backup -> {out} ({len(gz)//1024} KB)")
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
    path, brief_text, brief_html = digest.write()
    print(f"   morning brief -> {path}")
    try:
        import mailer
        ok, why = mailer.send_internal(
            f"☀️ Master Butler morning brief — {datetime.now():%b %d}",
            brief_text, to=[mailer.DALLON, mailer.TOM], html=brief_html)
        print(f"   brief emailed to Dallon + Tom: {why}")
        if ok:
            from heartbeats import beat
            beat("brief")
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
                # ONLY a genuine error page counts (Dallon, Jul 20). The old
                # "len < 2000" guess false-flagged sparse-but-healthy pages —
                # that noise plus a never-clearing alert nagged for days after
                # the real breaks were long fixed.
                if "Small hiccup" in h:
                    bad.append(r["stamp"])
            except Exception:
                bad.append(r["stamp"])           # HTTP fail / timeout = real
        msg = (f"   QA: {len(recs)} bid pages checked, "
               f"{len(bad)} broken" + (f" -> {bad[:5]}" if bad else " ✅"))
        print(msg)
        # Write the result to the SHARED store (the brief reads the blob, not
        # this ephemeral file) and CLEAR it when everything's healthy, so a
        # fixed page stops nagging.
        alert = (f"🔴 QA ALERT: {len(bad)} bid page(s) failing to render: "
                 f"{', '.join(bad[:6])} — tell Claude.\n") if bad else ""
        try:
            import clouddb
            clouddb.put_blob("brief_pin", alert)
        except Exception:
            pass
        Path("data/brief_pin.txt").write_text(alert)
except Exception as e:
    print(f"   QA check skipped ({e})")

# 5a-bis ── PIPELINE INVARIANTS (Jul 22, the Liuliu/Asish class:
# features correct alone, wrong in sequence). Gmail and the mirror
# must AGREE every night; any violation emails Dallon immediately —
# never again a hidden customer nobody knew about.
try:
    import pipeline_check
    _pv = pipeline_check.run(verbose=True)
    if _pv:
        import mailer
        mailer.send_internal(
            f"🔴 Master Butler pipeline check — {len(_pv)} violation(s)",
            "The nightly Gmail↔dashboard invariant check found:\n\n"
            + "\n".join("· " + p for p in _pv[:40])
            + "\n\nThese are customers/records the two systems disagree "
              "about — tell Claude.",
            to=["dallon.masterbutler@gmail.com"])
        print(f"   ⚠️ {len(_pv)} pipeline violation(s) — emailed Dallon")
    else:
        print("   pipeline invariants: all hold ✅")
    from heartbeats import beat
    beat("pipeline_check", violations=len(_pv))
except Exception as e:
    print(f"   (pipeline check skipped: {e})")

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
