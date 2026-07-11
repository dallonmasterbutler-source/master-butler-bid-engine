"""
MASTER BUTLER — COMPLETENESS FIXER (Dallon, Jul 10: "EVERYTHING that
was done on gmail, zillow, tax docs from wa state, pw bids sent to me
etc, should all be done here so they dont have to do any research.
The only way this gets used is if they trust the system.")

For every live customer record, fill what the office used to research
by hand:
  · missing address        -> a merged sibling's, then their Jobber file
  · missing display name   -> their Jobber client name (Jim Cavanaugh,
                              not jrcavinc@aol.com)
  · missing house facts    -> county assessor (sqft/stories/roof/bsmt)
  · missing photos         -> aerial tile + street view -> cloud gallery
  · missing customer badge -> Jobber client summary
  · PW without surfaces    -> one aerial Vision survey (~2¢)

Run with DATABASE_URL set (cloud-direct). Idempotent; only fills gaps.
One bad record can NEVER kill the sweep (per-record isolation, Jul 10
pm — records were staying empty shells when an earlier record threw).
A sweep_heartbeat blob records every run so silence is visible.
"""

import json
import re
import sys

import clouddb
import jobber_client as jc
from techs import tech_for


def _slug(a):
    return re.sub(r"[^a-z0-9]+", "-", (a or "").lower()).strip("-")[:60]


def _email(r):
    m = re.search(r"<([^>]+)>", r.get("from") or "")
    return m.group(1).lower() if m else None


def _skip(r, e):
    # a VOICEMAIL record wears copycall's sender but the CALLER is the
    # customer — never skip those (Terry Brower stayed an empty shell
    # because 'copycall' in the from-address skipped him, Jul 10).
    # Same for JOBBER EVENTS: they wear noreply@getjobber until step 8
    # dresses them as the customer (Zina Lee stayed 'You received a
    # new request from…', Jul 10 pm)
    if (r.get("lead") or r.get("kind") == "jobber_event") \
            and not (r.get("merged_into") or r.get("spam_auto")):
        return False
    return (r.get("merged_into") or r.get("spam_auto")
            or r.get("tech_sender")
            or (e and (tech_for(e) or any(x in e for x in
                ("copycall", "getjobber", "noreply", "no-reply",
                 "masterbutlerinc", "accounts.google"))))
            or (not e and not r.get("phone")))


def run(recent_hours=None):
    """recent_hours: only records newer than N hours (the cloud's
    hourly self-heal); None = the whole backlog."""
    if not clouddb.available():
        sys.exit("DATABASE_URL not set")
    from datetime import datetime, timedelta
    floor = ((datetime.now() - timedelta(hours=recent_hours))
             .strftime("%Y%m%d-%H%M%S") if recent_hours else "")
    from property_data import geocode, _api_key
    import assessor
    key = _api_key()
    photo_refs = {p[0] for p in clouddb._exec(
        "SELECT DISTINCT ref, kind, idx FROM photos WHERE kind != 'eml'",
        (), fetch="all")}
    stats = {"addr": 0, "facts": 0, "photos": 0, "status": 0,
             "surfaces": 0}
    rows = list(clouddb.all_shadow())
    global _ROWS_CACHE
    _ROWS_CACHE = rows

    # addresses known ANYWHERE in each merge family (Jan Hudson,
    # Jul 10 pm: her 'quote approved' event carried 920 Harrison Ave
    # but was merged INTO her thread record, which stayed address-less)
    kin_addr = {}
    for s, r in rows:
        t = r.get("merged_into")
        if t and r.get("address") and t not in kin_addr:
            kin_addr[t] = r["address"]

    # best display name per email, from the customer's OWN emails —
    # beats a broken Jobber name ('(null) Saveliev' stayed on the
    # queue while his replies arrived signed Vadim Tank Saveliev)
    best_name = {}
    for s, r in rows:
        f = r.get("from") or ""
        e2 = _email(r)
        disp = f.split("<")[0].strip()
        if (e2 and disp and "@" not in disp and "null" not in disp.lower()
                and "jobber" != disp.lower()
                and len(disp) > len(best_name.get(e2, ""))):
            best_name[e2] = disp

    for stamp, rec in rows:
        if floor and stamp < floor:
            continue
        e = _email(rec)
        if _skip(rec, e):
            continue
        try:
            changed = _fill(stamp, rec, e, kin_addr, best_name,
                            photo_refs, stats, geocode, assessor, key)
        except Exception as ex:
            # isolate: the NEXT record still gets its sweep
            print(f"  ✗ {stamp} sweep error: {ex}", flush=True)
            stats.setdefault("errors", 0)
            stats["errors"] += 1
            continue
        if changed:
            clouddb.ingest_shadow(stamp, rec)
            print(f"  ✓ {stamp} {(rec.get('from') or '')[:40]}", flush=True)

    # heartbeat: proof of life, visible from any machine
    try:
        clouddb.put_blob("sweep_heartbeat", {
            "at": datetime.now().isoformat(timespec="seconds"),
            "recent_hours": recent_hours, "stats": stats})
    except Exception:
        pass
    print("COMPLETE SWEEP DONE:", json.dumps(stats), flush=True)


def _fill(stamp, rec, e, kin_addr, best_name, photo_refs, stats,
          geocode, assessor, key):
    changed = False

    # 0a) a broken/missing display name heals from the customer's own
    # best-known name ('(null) Saveliev' → 'Vadim Tank Saveliev')
    disp0 = (rec.get("from") or "").split("<")[0].strip()
    broken = (not disp0 or "@" in disp0 or "null" in disp0.lower())
    if e and broken and best_name.get(e) \
            and best_name[e] != disp0:
        rec["from"] = f"{best_name[e]} <{e}>"
        stats.setdefault("named", 0)
        stats["named"] += 1
        changed = True

    # 0) address from a merged sibling (ground truth from the family)
    if not rec.get("address") and kin_addr.get(stamp):
        rec["address"] = kin_addr[stamp]
        stats["addr"] += 1
        changed = True

    # 1) address from their Jobber file
    if not rec.get("address") and e:
        try:
            a = jc.find_client_address(e)
        except Exception:
            a = None
        if a:
            rec["address"] = a
            stats["addr"] += 1
            changed = True
    addr = rec.get("address")

    # 2) house facts from the county assessor
    pi = (rec.get("draft") or {}).get("prop_info") or {}
    if addr and not pi.get("sqft"):
        try:
            g = geocode(addr, key)
            facts = assessor.lookup(g["lat"], g["lng"]) if g else None
        except Exception:
            facts = None
        if facts and facts.get("sqft"):
            # draft can be EXPLICITLY None (6 records were; the crash
            # silently killed every sweep mid-run, Jul 10 pm)
            if rec.get("draft") is None:
                rec["draft"] = {}
            pi = rec["draft"].setdefault("prop_info", {})
            pi["sqft"] = facts["sqft"]
            pi["sqft_source"] = (f"{facts['county']} County assessor "
                                 f"record")
            if facts.get("stories") and not pi.get("stories"):
                s = facts["stories"]
                pi["stories"] = (str(int(s)) if s == int(s) else str(s))
            if facts.get("roof_material") and not pi.get("roof_material"):
                pi["roof_material"] = facts["roof_material"]
            for k_ in ("basement_sqft", "garage_sqft"):
                if facts.get(k_):
                    pi[k_] = facts[k_]
            stats["facts"] += 1
            changed = True

    # 3) photos: aerial tile + street view into the cloud gallery
    if addr and _slug(addr) not in photo_refs \
            and stamp not in photo_refs:
        got = 0
        try:
            import aerial
            from imgprep import prep_jpeg_bytes
            for kind, fetch in (("aerial", aerial.fetch_tile),
                                ("street", aerial.fetch_streetview)):
                try:
                    p = fetch(addr)
                    if p:
                        clouddb.put_photo(_slug(addr), kind, 0,
                                          prep_jpeg_bytes(p, 1000, 72))
                        got += 1
                except Exception:
                    continue
        except Exception:
            pass
        if got:
            photo_refs.add(_slug(addr))
            stats["photos"] += 1
            changed = True

    # 4) returning badge + Jobber link + their REAL NAME (Dallon,
    #    Jul 10 pm: 'jrcavinc@aol.com … has 15 jobs' but no name —
    #    Jobber has him as Jim Cavanaugh; wear it)
    disp = (rec.get("from") or "").split("<")[0].strip()
    needs_name = bool(e) and (not disp or disp.lower() == e)
    if e and (not rec.get("customer_status") or needs_name):
        try:
            cs = jc.client_summary(e)
        except Exception:
            cs = None
        if cs is not None and not rec.get("customer_status"):
            rec["customer_status"] = (
                "new" if not cs["known"] else
                f"returning ({cs['invoices']} jobs)" if cs["invoices"]
                else "in Jobber — no completed jobs yet")
            if cs.get("url") and not rec.get("jobber_client_url"):
                rec["jobber_client_url"] = cs["url"]
            stats["status"] += 1
            changed = True
        if cs and cs.get("name") and needs_name:
            # Jobber names can carry '(null)' where a first name
            # should be — scrub before wearing
            import re as _r2
            nm2 = _r2.sub(r"\(?\bnull\b\)?", "",
                          cs["name"], flags=_r2.I).strip()
            if nm2 and nm2.lower() != e:
                rec["from"] = f"{nm2} <{e}>"
                stats.setdefault("named", 0)
                stats["named"] += 1
                changed = True

    # 5) PW asks get their surfaces measured from the sky
    svcs = rec.get("services") or []
    pi = (rec.get("draft") or {}).get("prop_info") or {}
    if addr and not pi.get("aerial_surfaces") and any(
            s.startswith("pw") or s == "pressure_washing"
            for s in svcs):
        try:
            from aerial import cross_check
            afields, _ = cross_check(
                {"surfaces": {}, "services": {"gutters": True}}, addr)
            got = afields.get("aerial_surfaces") or {}
            if got:
                if rec.get("draft") is None:
                    rec["draft"] = {}
                pi = rec["draft"].setdefault("prop_info", {})
                pi["aerial_surfaces"] = got
                if afields.get("debris"):
                    pi["debris_read"] = afields["debris"]
                stats["surfaces"] += 1
                changed = True
        except Exception:
            pass

    # 6) VOICEMAIL SELF-HEAL (Dallon, Jul 10: Terry Brower's 1:58
    #    message showed 'no audio' while the WAV sat in the email —
    #    'we can't allow that to keep happening, be proactive').
    #    Any voicemail record without a transcript gets its raw email
    #    pulled from the archive and transcription RETRIED here,
    #    hourly, so a one-time failure can never become a permanent
    #    'nothing to hear'.
    if (rec.get("lead") and "🎙" not in (rec.get("newest_message") or "")
            and rec.get("kind") in ("phone_lead", "new_request")
            and (rec.get("lead") or {}).get("duration")
            not in ("0:00", "0:01", "0:02", None)):
        try:
            import email as _em
            import transcribe as _tr
            raw = clouddb.get_photo(stamp, "eml", 0)
            if raw:
                msg_o = _em.message_from_bytes(
                    bytes(raw), policy=_em.policy.default)
                fn, audio = _tr.extract_audio(msg_o)
                text = _tr.transcribe(audio, fn or "") if audio else ""
                if text:
                    who = ((rec.get("caller_id") or {}).get("name")
                           or (rec.get("lead") or {}).get("caller")
                           or "caller")
                    rec["newest_message"] = f"🎙 VOICEMAIL: “{text}”"
                    rec["kind"] = "new_request"
                    from email_parser import find_services, find_address
                    rec["services"] = (find_services(text)
                                       or rec.get("services"))
                    rec["address"] = rec.get("address") \
                        or find_address(text)
                    rec["office_alert"] = (
                        f"🎙 Voicemail from {who} — transcribed on "
                        "retry; their words are below.")
                    stats.setdefault("vm_retried", 0)
                    stats["vm_retried"] += 1
                    changed = True
                elif audio and "transcription" not in \
                        (rec.get("office_alert") or "").lower():
                    # STILL failing → say so LOUDLY on the card and in
                    # the review feed (never a silent 'no audio' again)
                    rec["office_alert"] = ((rec.get("office_alert") or "")
                        + " ⚠ AUDIO IS ATTACHED but transcription keeps "
                        "failing — LISTEN BY PHONE (dial the mailbox); "
                        "Dallon has been flagged.").strip()
                    try:
                        clouddb.add_review({
                            "stamp": stamp, "action": "flag_review",
                            "customer": rec.get("from"), "by": "auto",
                            "note": "voicemail transcription failing "
                                    "repeatedly — audio IS attached"})
                    except Exception:
                        pass
                    stats.setdefault("vm_failing", 0)
                    stats["vm_failing"] += 1
                    changed = True
        except Exception:
            pass

    # 7) VOICEMAIL FULL CARD (Terry Brower, Jul 10: 'an empty shell
    #    with a name and the voicemail — it needs everything else'):
    #    any transcribed voicemail missing its profile link, status,
    #    priced draft, or photos gets them filled here, hourly.
    if rec.get("lead") and "🎙" in (rec.get("newest_message") or ""):
        try:
            import vm_enrich
            if vm_enrich.enrich(rec, stamp):
                stats.setdefault("vm_enriched", 0)
                stats["vm_enriched"] += 1
                changed = True
        except Exception:
            pass

    # 7.5) HONEST GAPS (Dallon, Jul 10 pm queue audit): a voicemail
    #    from a number Jobber doesn't know, with no address in the
    #    message, can never auto-fill — SAY so instead of looking
    #    broken, once, so the office asks on the call-back.
    if (rec.get("lead") and "🎙" in (rec.get("newest_message") or "")
            and not rec.get("address")
            and "ask for the address" not in
            (rec.get("office_alert") or "")):
        try:
            known = jc.caller_id(rec.get("phone")
                                 or (rec.get("lead") or {}).get("phone"))
        except Exception:
            known = None
        if not known:
            rec["office_alert"] = ((rec.get("office_alert") or "")
                + " 🆕 New caller — Jobber doesn't know this number and "
                "they didn't say an address; ask for the address when "
                "calling back.").strip()
            stats.setdefault("honest_gaps", 0)
            stats["honest_gaps"] += 1
            changed = True

    # 7.6) VOICEMAILS FOLD INTO THEIR PERSON (Dallon's queue audit,
    #    Jul 10 pm: Jan Hudson + Suzanne Vaughan each showed twice —
    #    caller-ID knew their email the whole time; the voicemail row
    #    and the email row never linked)
    if (rec.get("lead") and not rec.get("merged_into")):
        ci_em = ((rec.get("caller_id") or {}).get("email") or "").lower()
        if ci_em:
            for stamp2, r2 in _ROWS_CACHE:
                if r2 is rec or r2.get("merged_into") \
                        or r2.get("kind") == "jobber_event" \
                        or r2.get("lead"):
                    continue
                if ci_em in (r2.get("from") or "").lower():
                    rec["merged_into"] = stamp2
                    r2["office_alert"] = ((r2.get("office_alert") or "")
                        + " ☎ their voicemail is folded into this "
                        "thread (auto-linked by caller-ID).").strip()
                    clouddb.ingest_shadow(stamp2, r2)
                    stats.setdefault("vm_linked", 0)
                    stats["vm_linked"] += 1
                    changed = True
                    break

    # 7.7) SAME CALLER = ONE ROW (Suzanne Vaughan showed twice): a
    #    newer voicemail record folds into the caller's oldest live one
    if (rec.get("lead") and not rec.get("merged_into")
            and rec.get("phone")):
        ph = "".join(ch for ch in rec["phone"] if ch.isdigit())[-10:]
        if len(ph) == 10:
            for stamp2, r2 in _ROWS_CACHE:
                if r2 is rec or r2.get("merged_into") \
                        or not r2.get("lead") or stamp2 >= stamp:
                    continue
                ph2 = "".join(ch for ch in (r2.get("phone") or "")
                              if ch.isdigit())[-10:]
                if ph2 == ph:
                    rec["merged_into"] = stamp2
                    r2["office_alert"] = ((r2.get("office_alert") or "")
                        + " ☎ they called again — the newer voicemail "
                        "is folded into this thread.").strip()
                    clouddb.ingest_shadow(stamp2, r2)
                    stats.setdefault("vm_dedup", 0)
                    stats["vm_dedup"] += 1
                    changed = True
                    break

    # 8) JOBBER EVENTS wear their customer (Dallon, Jul 10: 'quote
    #    approved' rows with no name/address — recurring). Dress
    #    from the quote itself; merge into the customer's record.
    if rec.get("kind") == "jobber_event" and not rec.get("merged_into"):
        try:
            import jobber_sync
            global _EVQ
            try:
                _EVQ
            except NameError:
                import scoreboard as _sb
                _EVQ = _sb.fetch_recent_quotes(150)
            if jobber_sync.dress_event(rec, all_records=_ROWS_CACHE,
                                       quotes=_EVQ):
                tgt = rec.pop("_merge_target", None)
                if tgt:
                    t_rec = dict(_ROWS_CACHE).get(tgt)
                    if t_rec:
                        clouddb.ingest_shadow(tgt, t_rec)
                stats.setdefault("events_dressed", 0)
                stats["events_dressed"] += 1
                changed = True
        except Exception:
            pass

    return changed


_ROWS_CACHE = []


if __name__ == "__main__":
    run()
