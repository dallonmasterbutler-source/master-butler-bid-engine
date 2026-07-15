"""
MASTER BUTLER — THE HOURLY LANE REVIEWER
(Dallon, Jul 14: "maybe we need an hourly review program … that goes
through everything on EACH tab hourly and makes sure it's in the right
spot. I would like to catch the problems before the office does.")

This is the Jul-14 hand read-through of all 146 rows, codified. Every
hour it reads every live record + its newest message and flags what a
sharp human would catch. It NEVER moves a row by itself (the Jul-13
kill-switch lesson: guessing + auto-moving under the office's hands
burns trust) — findings land in the `lane_review` blob, show on the
build board, and the worst go to the review log.

The checks, each born from a real catch:
  · DECLINE       "no longer need / went another route / we moved" —
                  still sitting in a working lane (carrell_s, Donna
                  Simpson, familymchenry)
  · CHURN RISK    "have to try someone else / do it myself / stager
                  coming" (Wendy Sklar's stager deadline)
  · MONEY WAITING approval or go-forward language, 4+ hours old, and
                  our last word is older than theirs (nataliegarkusha's
                  'we'd like to go forward' buried in Waiting)
  · SPAM LEAK     sell-language rows that slipped the filter into a
                  working lane (kielermill, worldbestwebsitedesign)
  · OVERPARSE     7+ services parsed — almost always the parser eating
                  a quoted reminder email (irenehwang's 10-service
                  $1,408 draft for a gutter+windows ask)
  · EMPTY ROW     a blank non-voicemail message holding a bold slot
"""

import re
from datetime import datetime, timezone

DECLINE_RX = re.compile(
    r"no longer (need|in need|interested)|go(ing)? (with )?another "
    r"(route|company|provider)|went with another|found (someone|another)"
    r"|we('ve| have)? moved|i (have )?moved|cancel (the|my|our) "
    r"(quote|order|service)|not (be )?moving forward|hold off on",
    re.I)
CHURN_RX = re.compile(
    r"try someone else|do it (myself|ourselves)|another company as well"
    r"|need(ed)? (it|this) (done )?(before|by) |deadline|stager|listing "
    r"goes live|closing (is|date)", re.I)
MONEY_RX = re.compile(
    r"\bapprov\w+|go (ahead|forward)|let'?s do it|i('|’)?ll take it|"
    r"works for (me|us)|(would|that) works?\b|please schedule|"
    r"book (it|us|me)", re.I)
SPAM_RX = re.compile(
    r"google rank|seo|guaranteed (leads|meetings)|booked \d+k|"
    r"cost estimation services|takeoff|target audience|franchise|"
    r"funding|loan offer|web design|marketing campaign", re.I)


def run(verbose=False):
    import clouddb
    if not clouddb.available():
        return {}
    import msglog
    import spam_filter

    def _utc(at):
        try:
            t = datetime.fromisoformat(at)
            return t.replace(tzinfo=timezone.utc) if t.tzinfo is None \
                else t
        except (ValueError, TypeError):
            return None

    # last inbound/outbound per address from the message log
    now = datetime.now(timezone.utc)
    last_in, last_out = {}, {}
    for addr, _n, msgs in msglog.threads():
        a = (addr or "").lower()
        for m in msgs:
            t = _utc(m.get("at"))
            if not t:
                continue
            if m.get("dir") == "in":
                last_in[a] = max(last_in.get(a, t), t)
            else:
                last_out[a] = max(last_out.get(a, t), t)

    marks = clouddb.get_blob("msg_read") or {}
    findings, seen_email = [], set()
    for stamp, rec in sorted(clouddb.all_shadow(), reverse=True):
        if rec.get("merged_into") or rec.get("spam_auto") \
                or rec.get("tech_sender") or rec.get("kind") == "jobber_event":
            continue
        m = re.search(r"<([^>]+)>", rec.get("from") or "")
        e = m.group(1).lower() if m else None
        if not e or e in seen_email:
            continue
        seen_email.add(e)
        if e in marks:                    # cleared = not the office's now
            continue
        name = (rec.get("client_name")
                or (rec.get("from") or "").split("<")[0]).strip()[:34]
        body = re.sub(r"\s+", " ", (rec.get("newest_message") or ""))
        # only judge the CUSTOMER'S newest words, not quoted tails
        body_head = re.split(r"On (Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|"
                             r"Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
                             body)[0][:600]

        def add(check, note, sev="⚠️"):
            findings.append({"email": e, "name": name, "check": check,
                             "note": note[:180], "sev": sev,
                             "stamp": stamp})

        if DECLINE_RX.search(body_head):
            add("decline", f"they're saying NO/leaving — clear the row & "
                f"log the reason: “{body_head[:110]}”", "🚪")
        elif CHURN_RX.search(body_head):
            add("churn-risk", f"deadline/leaving language — needs a "
                f"same-day human: “{body_head[:110]}”", "🔥")
        elif MONEY_RX.search(body_head):
            li, lo = last_in.get(e), last_out.get(e)
            hours = (now - li).total_seconds() / 3600 if li else 0
            if li and (not lo or li > lo) and hours >= 4:
                add("money-waiting", f"approval/booking words, unanswered "
                    f"{hours:.0f}h: “{body_head[:100]}”", "💰")
        if SPAM_RX.search(body_head) and not rec.get("open_quote_ctx"):
            add("spam-leak", "sell-language — teach the spam filter if "
                "it's junk", "🚫")
        svcs = rec.get("services") or []
        if len(svcs) >= 7:
            add("overparse", f"{len(svcs)} services parsed — almost "
                "certainly ate a quoted reminder; NARROW the draft "
                "before anyone quotes it", "✂️")
        if not body.strip() and rec.get("kind") not in ("phone_lead",) \
                and not rec.get("vm"):
            add("empty-row", "blank message holding a queue slot", "👻")

    # ── 💰 DALLON'S EMAIL RULINGS → THE CUSTOMER'S CARD (Dallon,
    # Jul 14: the office emails him photos/questions, he replies terse
    # numbers — "$100" on Re: quote #36433. Those rulings must land on
    # the record, not die in the thread). Quote# in the subject/body is
    # the join key; the note rides bid.notes (lane-safe, Jul-13 lesson).
    try:
        byquote = {}
        for stamp, rec in clouddb.all_shadow():
            qn = str(((rec.get("open_quote_ctx") or {}).get("number")
                      or ""))
            if qn:
                byquote.setdefault(qn, []).append((stamp, rec))
        for addr, _n, msgs in msglog.threads():
            if "dallon.masterbutler" not in (addr or "") \
                    and "tomfricke2007" not in (addr or ""):
                continue
            who = "Dallon" if "dallon" in addr else "Tom"
            for m in msgs:
                if m.get("dir") != "in":
                    continue
                t = _utc(m.get("at"))
                if not t or (now - t).days > 21:
                    continue
                body = (m.get("body") or "")
                subj = (m.get("subject") or "")
                if not re.search(r"\$\s?\d{2,4}|\b\d{2,4}\s?\$", body):
                    continue             # a ruling carries a number
                qm = re.search(r"#\s?(3[0-9]{4})", subj + " " + body)
                if not qm or qm.group(1) not in byquote:
                    continue
                head = re.split(r"On (Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|"
                                r"Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|"
                                r"Nov|Dec)", body)[0].strip()[:140]
                note = (f"💰 {who}'s pricing (office asked by email, "
                        f"{str(t.date())}): “{head}”")
                for stamp, rec in byquote[qm.group(1)]:
                    nl = (rec.setdefault("draft", {})
                          .setdefault("notes", []))
                    if not any("💰" in x and head[:40] in x for x in nl):
                        nl.append(note)
                        clouddb.ingest_shadow(stamp, rec)
                        findings.append({"email": "", "name": who,
                                         "check": "ruling-attached",
                                         "note": f"quote #{qm.group(1)}"
                                                 f": {head[:80]}",
                                         "sev": "💰", "stamp": stamp})
                # → CALIBRATION LEDGER (Dallon, Jul 14 "wire it"): a
                # clean single-price ruling vs the NEWEST record's
                # draft total = one anchor-tuning sample per ruling,
                # tagged DALLON so the suggestion names its source.
                # Karen R: our $225 vs his $100 = the founding sample.
                stamp, rec = max(byquote[qm.group(1)])
                _pm = re.findall(r"\$?\s?(\d{2,4})\s?\$?", head)
                _ours = ((rec.get("draft") or {}).get("total"))
                if len(_pm) == 1 and _ours:
                    _rp = float(_pm[0])
                    _svc = (rec.get("services") or ["?"])[0]
                    _key = {"pw_driveway": "driveway",
                            "pw_patio": "patio",
                            "pw_sidewalk": "patio",
                            "gutter_cleaning": "gutter",
                            "moss_treatment": "moss",
                            "moss_removal": "moss",
                            "roof_blow_off": "roof blow",
                            }.get(_svc, _svc.split("_")[0]
                                  .replace("windows", "window"))
                    if 0 < _rp < 5000 and _ours > 0:
                        led = clouddb.get_blob("calibration_ledger") or {}
                        pct = (float(_ours) - _rp) / _rp * 100
                        row2 = [str(now.date()), round(float(_ours)),
                                round(_rp), round(pct, 1),
                                f"DALLON ruling #{qm.group(1)}"]
                        if not any(len(r) > 4 and r[4] == row2[4]
                                   for r in (led.get(_key) or [])):
                            led.setdefault(_key, []).append(row2)
                            clouddb.put_blob("calibration_ledger", led)
    except Exception:
        pass

    out = {"at": now.isoformat(timespec="seconds"),
           "findings": findings[:60],
           "counts": {}}
    for f in findings:
        out["counts"][f["check"]] = out["counts"].get(f["check"], 0) + 1
    clouddb.put_blob("lane_review", out)
    # the loud ones also hit the review feed so there's a paper trail
    for f in findings:
        if f["sev"] in ("🔥", "🚪"):
            try:
                clouddb.add_review({
                    "action": "lane_review", "by": "auto (hourly review)",
                    "at": out["at"], "customer": f["email"],
                    "note": f"{f['sev']} {f['check']}: {f['note']}"})
            except Exception:
                pass
    if verbose:
        print(f"lane review: {len(findings)} findings {out['counts']}")
        for f in findings:
            print(f"  {f['sev']} {f['check']:13s} {f['name']:24s} "
                  f"{f['note'][:90]}")
    return out


if __name__ == "__main__":
    run(verbose=True)
