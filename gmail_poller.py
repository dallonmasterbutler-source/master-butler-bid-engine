"""
MASTER BUTLER — GMAIL SHADOW POLLER

Watches the office inbox over IMAP and runs every NEW message through the
pipeline in SHADOW MODE:

  * READONLY connection + BODY.PEEK — physically cannot mark anything read,
    move, label, or delete. The office's unread-count workflow is sacred.
  * Nothing is pushed to Jobber. Shadow drafts land in data/shadow_bids/
    for the scoreboard (system's number vs what the office actually quotes).
  * Already-processed messages are remembered in OUR ledger
    (data/processed_ids.txt) — Gmail itself is never used as a checklist.

Run once:      python3 gmail_poller.py
Keep watching: python3 gmail_poller.py --watch   (polls every 2 minutes)
"""

import email
import email.policy
import imaplib
import json
import re as _re
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
LEDGER = BASE / "data" / "processed_ids.txt"
SHADOW_DIR = BASE / "data" / "shadow_bids"


def _creds():
    import os
    creds = {}
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    addr = creds.get("GMAIL_ADDRESS") or os.environ.get("GMAIL_ADDRESS")
    pw = (creds.get("GMAIL_APP_PASSWORD")
          or os.environ.get("GMAIL_APP_PASSWORD", ""))
    return addr, pw.replace(" ", "")


def _processed():
    """The already-seen ledger: local file, PLUS the cloud's own memory
    when the database is reachable (so a cloud watcher never re-processes
    anything the Mac already captured, and vice versa)."""
    seen = set()
    if LEDGER.exists():
        seen |= set(LEDGER.read_text().split())
    try:
        import clouddb
        if clouddb.available():
            seen |= clouddb.seen_message_ids()
    except Exception:
        pass
    return seen


def _remember(msg_id):
    LEDGER.parent.mkdir(exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(msg_id + "\n")


# Real bid requests land in spam sometimes (seen in the Takeout mining) —
# sweep it too, same readonly guarantee. Spam-found requests get flagged
# so the office knows to fish the original out.
FOLDERS = ["INBOX", "[Gmail]/Spam"]


def poll_once():
    """One pass: fetch unseen-by-US messages, shadow-process each."""
    # relay any cloud-queued internal mail while we're here (the cloud
    # host blocks SMTP; the Mac doesn't)
    try:
        import mailer
        n = mailer.drain_outbox()
        if n:
            print(f"  relayed {n} queued internal email(s)")
    except Exception:
        pass
    addr, pw = _creds()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(addr, pw)
    try:
        seen = _processed()
        new_count = 0

        for folder in FOLDERS:
            typ, _ = M.select(f'"{folder}"', readonly=True)  # the safety guarantee
            if typ != "OK":
                print(f"  (cannot open {folder} — skipped)")
                continue
            typ, data = M.search(None, "ALL")
            ids = data[0].split() if data and data[0] else []

            for num in ids:
                # stable Message-ID header (num changes; Message-ID doesn't)
                typ, hdr = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                raw_hdr = hdr[0][1].decode(errors="replace")
                msg_id = raw_hdr.split(":", 1)[-1].strip() or f"no-id-{num.decode()}"
                if msg_id in seen:
                    continue

                typ, full = M.fetch(num, "(BODY.PEEK[])")   # untouched
                raw = full[0][1]
                new_count += 1
                shadow_process(raw, msg_id, folder=folder)
                _remember(msg_id)
                seen.add(msg_id)

        # LIVE MESSAGES: also sweep the Sent folder, so replies the office
        # sends from Gmail still show up in the dashboard conversation.
        try:
            _sweep_sent(M, seen)
        except Exception as e:
            print(f"  (sent sweep skipped: {e})")
    finally:
        try:
            M.logout()      # NEVER leak an IMAP connection —
        except Exception:   # Gmail caps simultaneous logins
            pass
    # pull the office's Settings edits down so THIS machine prices and
    # drafts with the same numbers the cloud uses
    try:
        import urllib.request
        from base64 import b64encode
        from cloudpush import _cfg
        url, pw = _cfg("DASHBOARD_URL"), _cfg("DASHBOARD_PASSWORD")
        if url and pw:
            for key in ("pricing_overrides", "canned_replies"):
                req = urllib.request.Request(
                    url.rstrip("/") + f"/api/blob/{key}",
                    headers={"Authorization": "Basic "
                             + b64encode(f"office:{pw}".encode()).decode()})
                val = json.load(urllib.request.urlopen(req, timeout=30))
                if val is not None:
                    (BASE / "data" / f"{key}.json").write_text(
                        json.dumps(val))
    except Exception:
        pass
    _keep_cloud_warm()          # heartbeat on EVERY poll, however invoked

    # QUOTE SYNC (Dallon's rule: dashboard mirrors Jobber, read-only,
    # no quote creation) — refresh which office quotes match our
    # records. Throttled to hourly: the 10-minute ears loop shouldn't
    # hammer Jobber's API all day.
    marker = BASE / "data" / "last_quote_sync.txt"
    try:
        last = datetime.fromisoformat(marker.read_text().strip())
        fresh = (datetime.now() - last).total_seconds() < 3600
    except Exception:
        fresh = False
    if not fresh:
        try:
            import scoreboard
            scoreboard.run(limit=40)
            marker.write_text(datetime.now().isoformat(timespec="seconds"))
            try:
                import autoreview
                autoreview.run(verbose=False)   # matches review themselves
            except Exception:
                pass
            try:
                sb = json.loads((BASE / "data" / "scoreboard.json").read_text())
                import clouddb
                if clouddb.available():
                    clouddb.put_blob("scoreboard", sb)
                else:
                    from cloudpush import push
                    push(blobs={"scoreboard": sb})
            except Exception:
                pass
        except Exception as e:
            print(f"  (quote sync skipped: {e})")
    return new_count


def _sweep_sent(M, seen):
    """Log outbound mail (office replies from Gmail) to the message log.
    Uses the same Message-ID ledger — each sent mail processed once."""
    import email as _email
    typ, _ = M.select('"[Gmail]/Sent Mail"', readonly=True)
    if typ != "OK":
        return
    from datetime import timedelta
    since = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'SINCE {since}')
    ids = data[0].split() if data and data[0] else []
    import msglog
    for num in ids[-40:]:
        typ, hdr = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        mid = "sent-" + (hdr[0][1].decode(errors="replace")
                         .split(":", 1)[-1].strip() or num.decode())
        if mid in seen:
            continue
        typ, full = M.fetch(num, "(BODY.PEEK[])")
        msg = _email.message_from_bytes(full[0][1],
                                        policy=_email.policy.default)
        to_addr = _email.utils.parseaddr(msg.get("To", ""))[1]
        body = ""
        plain = msg.get_body(preferencelist=("plain",))
        if plain is not None:
            body = plain.get_content()
        else:
            h = msg.get_body(preferencelist=("html",))
            if h is not None:
                body = _re.sub(r"<[^>]+>", " ", h.get_content())
        sent_at = None
        try:
            from email.utils import parsedate_to_datetime
            if msg.get("Date"):
                sent_at = parsedate_to_datetime(msg["Date"]) \
                    .isoformat(timespec="seconds")
        except Exception:
            pass
        msglog.record("out", to_addr, subject=msg.get("Subject", ""),
                      body=body, at=sent_at)
        _remember(mid)
        seen.add(mid)


def _bid_for_quote(quote_number):
    """Which of OUR bids does this Jobber quote belong to? (scoreboard)"""
    if not quote_number:
        return None
    try:
        import clouddb
        if clouddb.available():
            sb = clouddb.get_blob("scoreboard") or {}
        else:
            f = BASE / "data" / "scoreboard.json"
            sb = json.loads(f.read_text()) if f.exists() else {}
        for row in sb.get("rows", []):
            if str(row.get("office_quote")) == str(quote_number):
                return row["stamp"]
    except Exception:
        pass
    return None


def _attach_event(target_stamp, ev, event_stamp):
    """Write the lifecycle event ONTO the customer's own bid record."""
    try:
        import clouddb
        rec = None
        if clouddb.available():
            rec = dict(clouddb.all_shadow()).get(target_stamp)
        if rec is None:
            f = BASE / "data" / "shadow_bids" / f"{target_stamp}.json"
            rec = json.loads(f.read_text()) if f.exists() else None
        if rec is None:
            return
        rec.setdefault("events", []).append(
            {"at": datetime.now().isoformat(timespec="seconds"),
             "event": ev.get("event"), "quote": ev.get("quote_number"),
             "detail": (ev.get("client") or "")[:60]})
        labels = {"quote_approved":
                  f"🎉 QUOTE #{ev.get('quote_number')} APPROVED — convert "
                  "it to a job in Jobber.",
                  "changes_requested":
                  f"✏️ Customer requested CHANGES on quote "
                  f"#{ev.get('quote_number')} — needs office attention.",
                  "request_received":
                  f"📥 Jobber request received (quote "
                  f"#{ev.get('quote_number')})."}
        if ev.get("event") in labels:
            rec["office_alert"] = labels[ev["event"]]
        if clouddb.available():
            clouddb.ingest_shadow(target_stamp, rec)
        else:
            f = BASE / "data" / "shadow_bids" / f"{target_stamp}.json"
            f.write_text(json.dumps(rec, indent=1))
            try:
                from cloudpush import push
                push(records=[(target_stamp, rec)])
            except Exception:
                pass
    except Exception as e:
        print(f"  (event attach failed: {e})")


def _transcribe_voicemail(record, parsed, raw_bytes, stamp, who):
    """Audio attachment -> words on the dashboard (Dallon: 'extract the
    info from the audio'). Returns True when a transcript landed."""
    try:
        import email as _em
        import transcribe
        msg_obj = _em.message_from_bytes(raw_bytes, policy=_em.policy.default)
        fn, audio = transcribe.extract_audio(msg_obj)
        if not audio:
            return False
        vdir = BASE / "data" / "voicemail"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / f"{stamp}-{(fn or 'vm.wav')}").write_bytes(audio)
        text = transcribe.transcribe(audio, fn or "")
        if not text:
            record["office_alert"] = (
                f"VOICEMAIL AUDIO from {who} attached but transcription "
                "unavailable — audio saved to data/voicemail; dial in or "
                "enable Speech-to-Text (one click, ask Claude).")
            return False
        record["newest_message"] = f"🎙 VOICEMAIL: “{text}”"
        record["kind"] = "new_request"
        from email_parser import find_services, find_address
        record["services"] = find_services(text) or record.get("services")
        record["address"] = record.get("address") or find_address(text)
        record["office_alert"] = (f"🎙 Voicemail from {who} — transcribed "
                                  "automatically; their words are below.")
        try:
            import msglog
            msglog.record("in", parsed.get("sender_email") or "",
                          name=who, subject="Voicemail", body=text,
                          stamp=stamp)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"  (voicemail transcription skipped: {e})")
        return False


def _shadow_sources_for_dedupe():
    """(stamp, record) pairs from wherever records live — small helper
    shared by the voicemail fold check."""
    try:
        import clouddb
        if clouddb.available():
            return clouddb.all_shadow()
    except Exception:
        pass
    try:
        return [(p.stem, json.loads(p.read_text()))
                for p in sorted(SHADOW_DIR.glob("*.json"))]
    except Exception:
        return []


def shadow_process(raw_bytes, msg_id, folder="INBOX"):
    """Run one raw email through the pipeline; save the shadow draft."""
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    while (SHADOW_DIR / f"{stamp}.json").exists():
        # two emails in the same second must not share a stamp — the
        # second would silently overwrite the first (found in testing)
        from datetime import timedelta
        stamp = (datetime.strptime(stamp, "%Y%m%d-%H%M%S")
                 + timedelta(seconds=1)).strftime("%Y%m%d-%H%M%S")

    # save the raw email so the pipeline (and any re-run) can use it
    eml_path = SHADOW_DIR / f"{stamp}.eml"
    eml_path.write_bytes(raw_bytes)

    from email_parser import parse_eml
    parsed = parse_eml(eml_path)
    disp = (parsed.get("sender_name")
            or (parsed.get("sender_email") or "?").split("@")[0]
            .replace(".", " ").replace("_", " ").title())
    record = {"message_id": msg_id, "received": stamp, "folder": folder,
              "from": f"{disp} <{parsed['sender_email']}>",
              "subject": parsed["subject"], "kind": parsed["kind"],
              "services": parsed["services"], "address": parsed["address"],
              "phone": parsed.get("phone"),
              "sched_pref": parsed.get("sched_pref"),
              "tech_request": parsed.get("tech_request"),
              "newest_message": parsed.get("newest_message")}
    # bulk-mail marker for the spam filter (real customers never have it)
    try:
        import email as _email
        if _email.message_from_bytes(raw_bytes).get("List-Unsubscribe"):
            record["list_unsub"] = True
    except Exception:
        pass

    # TECH GATE (Jessica's call, Jul 9): mail FROM one of our techs is
    # field traffic — a callback, a job question. Tagged and VISIBLE on
    # the inbox, but never priced, never a lead, never spam-filtered.
    try:
        from techs import tech_for
        _tech = tech_for(parsed.get("sender_email"))
    except Exception:
        _tech = None
    if _tech:
        record["tech_sender"] = _tech
        record["kind"] = "tech_note"
        print(f"  👷 tech mail — {_tech}: {record['subject'][:48]} "
              "(tagged, no bid)")
        (SHADOW_DIR / f"{stamp}.json").write_text(json.dumps(record,
                                                             indent=1))
        try:
            import clouddb
            if clouddb.available():
                clouddb.ingest_shadow(stamp, record)
            else:
                from cloudpush import push_or_queue
                push_or_queue(stamp, record)
        except Exception:
            pass
        return

    # SPAM GATE (Dallon Jul 8: "teach our program... avoid putting it in
    # the dashboard"): solicitations get SAVED — visible in the queue's
    # spam drawer, never vanished — but skip pricing, Jobber lookups,
    # satellite imagery, and the conversation log entirely.
    try:
        import spam_filter
        _is_spam, _why = spam_filter.looks_spam(
            record["from"], record["subject"],
            record.get("newest_message") or "",
            has_address=bool(record.get("address")),
            list_unsub=bool(record.get("list_unsub")),
            kind=record["kind"])
    except Exception:
        _is_spam, _why = False, ""
    if _is_spam:
        record["spam_auto"] = _why
        print(f"  🚫 spam — filed without pricing: "
              f"{record['subject'][:48]} ({_why[:60]})")
        (SHADOW_DIR / f"{stamp}.json").write_text(json.dumps(record,
                                                             indent=1))
        try:
            import clouddb
            if clouddb.available():
                clouddb.ingest_shadow(stamp, record)
            else:
                from cloudpush import push_or_queue
                push_or_queue(stamp, record)
        except Exception:
            pass
        return
    # DO-NOT-SERVICE GUARD (Dallon): match by email, phone, ADDRESS
    # (new-email evaders), or name — flag loudly before anyone quotes.
    if parsed["kind"] in ("new_request", "scheduling"):
        try:
            import dns_check
            hit = dns_check.check(email=parsed.get("sender_email"),
                                  phone=parsed.get("phone"),
                                  address=parsed.get("address"),
                                  name=parsed.get("sender_name"))
        except Exception:
            hit = None
        if hit:
            record["dns_match"] = hit
            record["office_alert"] = (
                f"⛔ DO NOT SERVICE — matches '{hit['name']}' in Jobber "
                f"(matched by {hit['matched_by']}; marker: {hit['why'][:80]}). "
                "Do not quote or schedule; tell Dallon/Tom if unsure.")

    # OPEN-QUOTE CHECK (Dallon Jul 8, the Shadi/Nithya lesson): a known
    # customer writing in usually continues an EXISTING quote thread —
    # say so before anyone drafts a duplicate.
    if parsed["kind"] in ("new_request", "scheduling", "other") \
            and parsed.get("sender_email") \
            and not record.get("dns_match"):
        try:
            import jobber_client as jc
            oq = jc.find_open_quote(parsed["sender_email"], scan=80)
        except Exception:
            oq = None
        if oq:
            lines = "; ".join(
                f"{li['name'][:32]} ${li['totalPrice']:.0f}"
                for li in (oq.get("lineItems") or {}).get("nodes", [])
                if (li.get("totalPrice") or 0) > 0)[:150]
            record["open_quote_ctx"] = {
                "number": oq["quoteNumber"], "status": oq["quoteStatus"],
                "total": oq["amounts"]["total"],
                "created": (oq.get("createdAt") or "")[:10],
                "url": oq.get("jobberWebUri"),
                # ALL the info comes in (Dallon Jul 9, the Mia lesson)
                "lines": [{"name": li["name"],
                           "price": li.get("totalPrice")}
                          for li in (oq.get("lineItems") or {})
                          .get("nodes", [])][:8]}
            if oq["quoteStatus"] == "approved":
                record["office_alert"] = (
                    f"📎 CUSTOMER ALREADY APPROVED quote "
                    f"#{oq['quoteNumber']} (${oq['amounts']['total']}, "
                    f"{(oq.get('createdAt') or '')[:10]}: {lines}). This "
                    "email is likely about scheduling that work — not a "
                    "new request.")
            else:
                record["office_alert"] = (
                    f"📎 EXISTING OPEN QUOTE #{oq['quoteNumber']} "
                    f"(${oq['amounts']['total']}, {oq['quoteStatus']} "
                    f"since {(oq.get('createdAt') or '')[:10]}: {lines}). "
                    "Likely a follow-up — reply on that quote, don't send "
                    "a second one.")

    # NEW-or-RETURNING (techs' ask): first job = say so, exactly once.
    if parsed["kind"] == "new_request" and parsed.get("sender_email"):
        try:
            import jobber_client as jc
            cs = jc.client_summary(parsed["sender_email"])
        except Exception:
            cs = None
        if cs is not None:
            if not cs["known"]:
                record["customer_status"] = "new"
            elif cs["invoices"] == 0:
                record["customer_status"] = "in Jobber — no completed jobs yet"
            else:
                record["customer_status"] = f"returning ({cs['invoices']} jobs)"
            if cs.get("url"):
                record["jobber_client_url"] = cs["url"]
    if parsed.get("jobber_event"):
        ev = parsed["jobber_event"]
        record["jobber_event"] = ev
        # ONE CUSTOMER = ONE ROW (Dallon Jul 8): a Jobber event about a
        # quote we track ATTACHES to that customer's bid instead of
        # spawning a second queue item.
        target = _bid_for_quote(ev.get("quote_number"))
        if target:
            _attach_event(target, ev, stamp)
            record["merged_into"] = target
        if ev["event"] == "quote_approved":
            record["office_alert"] = (
                f"🎉 QUOTE #{ev.get('quote_number')} APPROVED"
                + (f" by {ev['client']}" if ev.get("client") else "")
                + (f" (${ev['amount']})" if ev.get("amount") else "")
                + " — convert it to a job in Jobber.")
        elif ev["event"] == "changes_requested":
            record["office_alert"] = (
                f"Quote #{ev.get('quote_number')}: customer requested "
                "changes — needs office attention.")

    if parsed.get("lead"):
        lead = parsed["lead"]
        record["lead"] = lead
        # SAME CALL, SECOND NOTIFICATION (deploy overlaps double-process;
        # CopyCall sometimes re-sends): fold instead of a second entry
        try:
            for _ps, _pr in _shadow_sources_for_dedupe():
                _pl = _pr.get("lead") or {}
                if _pl.get("caller") == lead.get("caller") \
                        and _pl.get("when") == lead.get("when") \
                        and _ps != stamp:
                    record["merged_into"] = _ps
                    break
        except Exception:
            pass
        style = lead.get("style")
        who = parsed.get("phone") or "unknown caller"
        # SOFTWARE CALLER-ID: Jobber usually knows this number already.
        try:
            import jobber_client as jc
            cid = jc.caller_id(parsed.get("phone"))
        except Exception:
            cid = None
        if cid:
            record["caller_id"] = cid
            # the entry becomes a PERSON (Jessica): name + address on it
            if not record.get("address") and cid.get("address"):
                record["address"] = cid["address"]
            who = (f"{cid['name']} ({who}) — EXISTING CLIENT, "
                   f"{cid['invoices']} past job(s)"
                   + (f", {cid['address']}" if cid.get("address") else ""))
        elif parsed.get("phone"):
            who = f"{who} — not in Jobber, likely NEW lead"
        if style in ("notification", "audio") and \
                _transcribe_voicemail(record, parsed, raw_bytes, stamp, who):
            style = "transcript"
        if style == "notification":
            record["office_alert"] = (
                f"VOICEMAIL from {who}"
                + (f" ({lead['duration']}" if lead.get("duration") else " (")
                + (f", mailbox {lead['mailbox']})" if lead.get("mailbox") else ")")
                + " — retrieve + transcribe the message into this record; "
                "reply by EMAIL per office policy.")
        elif style == "audio":
            record["office_alert"] = (
                f"VOICEMAIL AUDIO attached from {who} — transcription "
                "pending; reply by EMAIL per office policy.")
        elif style == "transcript" and parsed["kind"] == "phone_lead":
            record["office_alert"] = (
                f"VOICEMAIL TRANSCRIPT from {who} — no services detected; "
                "read + reply by EMAIL per office policy.")

    if "Spam" in folder and parsed["kind"] == "new_request":
        record["office_alert"] = ("FOUND IN SPAM — real request; office "
                                  "should rescue it from the spam folder")

    # LIVE MESSAGES: every human inbound lands in the conversation log,
    # timestamped by the email's OWN Date header (not when we polled it).
    try:
        import msglog
        if parsed.get("sender_email") and parsed["kind"] != "jobber_event":
            sent_at = None
            try:
                import email as _em
                from email.utils import parsedate_to_datetime
                hdr = _em.message_from_bytes(raw_bytes).get("Date")
                if hdr:
                    sent_at = parsedate_to_datetime(hdr) \
                        .isoformat(timespec="seconds")
            except Exception:
                pass
            msglog.record("in", parsed["sender_email"],
                          name=parsed.get("sender_name") or "",
                          subject=parsed.get("subject") or record["subject"],
                          body=parsed.get("newest_message") or "",
                          stamp=stamp, at=sent_at)
    except Exception:
        pass

    # DUPLICATE LINKING: same person/thread/address within 30 days gets
    # LINKED, never dropped — the office decides "same job" vs "new job".
    try:
        from dedup import check_duplicate
        priors = []
        try:                    # records live in the DB on Render — the
            import clouddb as _cdb   # file glob found NOTHING there and
            if _cdb.available():     # the dup guard never fired (Mia bug)
                _source = _cdb.all_shadow()
            else:
                _source = [(pj.stem, json.loads(pj.read_text()))
                           for pj in sorted(SHADOW_DIR.glob("*.json"))]
        except Exception:
            _source = []
        for _st, pr in _source:
            m = _re.search(r"<([^>]+)>", pr.get("from", ""))
            try:
                _rcv = datetime.strptime(_st, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            priors.append({
                "stamp": _st,
                "sender_email": m.group(1) if m else "",
                "phone": pr.get("phone"),
                "address": pr.get("address"),
                "thread_id": None,
                "received": _rcv,
            })
        m = _re.search(r"<([^>]+)>", record["from"])
        verdict = check_duplicate(
            {"sender_email": m.group(1) if m else "",
             "phone": record.get("phone"),
             "address": record.get("address"),
             "received": datetime.now()}, priors)
        if verdict["verdict"] == "suspected_duplicate":
            # AUTO-SETTLE the obvious ones (the Fenich lesson): a
            # services-free reply/confirmation in the same thread folds
            # into the earlier bid by itself; the office is only asked
            # when there could be NEW work.
            prior_rec = {}
            try:
                pj = SHADOW_DIR / f"{verdict['match']['stamp']}.json"
                if pj.exists():
                    prior_rec = json.loads(pj.read_text())
                else:
                    import clouddb as _cdb
                    if _cdb.available():
                        prior_rec = dict(_cdb.all_shadow()).get(
                            verdict["match"]["stamp"]) or {}
            except Exception:
                prior_rec = {}
            from dedup import looks_same_job
            if looks_same_job(record.get("subject"),
                              prior_rec.get("subject"),
                              record.get("newest_message"),
                              bool(parsed.get("services"))):
                record["merged_into"] = verdict["match"]["stamp"]
                record["office_alert"] = None
                print(f"     → auto-folded into {verdict['match']['stamp']}"
                      " (reply/confirmation, no new services)")
                try:
                    from store import save_review as _sr
                except Exception:
                    _sr = None
                try:
                    import clouddb as _cdb
                    entry = {"stamp": stamp, "action": "duplicate_same",
                             "customer": record.get("from"), "by": "auto",
                             "note": f"auto-folded into "
                                     f"{verdict['match']['stamp']} — reply/"
                                     "confirmation, no new services",
                             "at": datetime.now().isoformat(
                                 timespec="seconds")}
                    if _cdb.available():
                        _cdb.add_review(entry)
                except Exception:
                    pass
            else:
                record["duplicate_of"] = verdict["match"]["stamp"]
                record["office_alert"] = (record.get("office_alert", "") +
                    f" POSSIBLE DUPLICATE of {verdict['match']['stamp']} "
                    f"({verdict['reason']}) — same job or new job?").strip()
        elif verdict["verdict"] == "multi_property":
            # realty / property manager: same client, another house —
            # NEW job, own property record, notes stay per-property
            record["same_client_as"] = verdict["match"]["stamp"]
            record["office_alert"] = (record.get("office_alert", "") +
                " MULTI-PROPERTY CLIENT (same contact as "
                f"{verdict['match']['stamp']}, different address) — "
                "separate quote; keep notes on THIS property.").strip()
    except Exception:
        pass    # linking is a bonus, never a blocker

    print(f"  📧 {parsed['subject'][:60]}")
    print(f"     kind={parsed['kind']}  services={parsed['services']}")

    if parsed["kind"] == "new_request" and parsed["services"]:
        # full pipeline run (property lookup + vision on any photos + engine)
        try:
            import io
            from contextlib import redirect_stdout
            from pipeline import process
            buf = io.StringIO()
            with redirect_stdout(buf):
                draft = process(eml_path)
            record["pipeline_output"] = buf.getvalue()
            if draft:                       # structured copy for the dashboard
                record["draft"] = draft
                # a known customer often skips their address — the
                # pipeline found it in Jobber; keep it ON the record
                # (Becky lesson: record stayed address-less)
                if not record.get("address"):
                    record["address"] = (draft.get("customer") or {}) \
                        .get("address")
            # playbook rule that deserves the queue badge (seasons.py)
            if not record.get("office_alert"):
                m_a = _re.search(r"OFFICE_ALERT: (.+)",
                                 record["pipeline_output"])
                if m_a:
                    record["office_alert"] = m_a.group(1).strip()
            print("     → shadow draft generated")
        except Exception as e:
            record["pipeline_error"] = str(e)
            print(f"     → pipeline error: {e}")
    else:
        print("     → no bid needed (question/scheduling/other)")

    out = SHADOW_DIR / f"{stamp}.json"
    out.write_text(json.dumps(record, indent=1))

    # mirror to the cloud: direct database writes when we ARE the cloud
    # (poller running on Render), HTTPS courier when we're the Mac
    try:
        import clouddb
        if clouddb.available():
            clouddb.ingest_shadow(stamp, record)
            clouddb.put_photo(stamp, "eml", 0, raw_bytes)   # raw archive
            try:
                from pipeline import extract_photos
                from imgprep import prep_jpeg_bytes
                for i, p in enumerate(extract_photos(eml_path)):
                    clouddb.put_photo(stamp, "customer", i,
                                      prep_jpeg_bytes(p, 900, 70))
            except Exception:
                pass
            print("     → cloud: written directly")
        else:
            from cloudpush import push_or_queue, flush_pending
            flush_pending()
            ok = push_or_queue(stamp, record)
            print("     → cloud: " + ("synced" if ok else "queued (offline)"))
    except Exception:
        pass                    # cloud mirroring never blocks shadow mode


def _keep_cloud_warm():
    """Each poll: leave a heartbeat (dashboard shows 'ears last heard
    Xm' + alarm if silent) and, from the Mac, ping /health so the free
    tier stays awake."""
    from datetime import timezone
    beat = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        import clouddb
        if clouddb.available():                 # we ARE the cloud
            beat["host"] = "render-cloud"
            clouddb.put_blob("poller_heartbeat", beat)
            return
        import urllib.request
        from cloudpush import _cfg, push
        url = _cfg("DASHBOARD_URL")
        if url:
            urllib.request.urlopen(url.rstrip("/") + "/health", timeout=20)
            beat["host"] = "dallon-mac"
            push(blobs={"poller_heartbeat": beat})
    except Exception:
        pass


if __name__ == "__main__":
    watch = "--watch" in sys.argv
    while True:
        n = poll_once()
        _keep_cloud_warm()
        print(f"[{datetime.now():%H:%M}] poll complete — {n} new message(s)")
        if not watch:
            break
        time.sleep(120)
