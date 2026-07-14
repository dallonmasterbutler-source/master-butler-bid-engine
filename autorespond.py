"""
MASTER BUTLER — AUTO-RESPOND, STAGE 1: SHADOW DRAFTS
(Tom's ask, Dallon's GO Jul 14: "when the office goes into the customer
profile to answer a question, it is pre drafted so they just click send
... but only if it's a customer reply that came in.")

Stage 1 is SHADOW ONLY: build_draft() proposes what the pre-filled box
WOULD say; the /autodrafts review page shows the proposals next to what
the office actually sent, so Dallon grades the voice before any office
member ever sees a draft. Nothing here sends anything, ever.

Voice sources (mined Jul 14):
  · blob `office_voice` — 513 real Gmail replies distilled to templates
  · blob `canned_replies` — the office's OWN quick responses (their
    words, office-editable, [DATE]/[SERVICES] placeholders included)

THE HARD GATES (Tom & Dallon's rule):
  · a draft exists ONLY when the thread's newest message is a genuine
    customer INBOUND — never our own outbound, an office note, a tech,
    a partner, a Jobber notification, spam, or a DNS-listed sender
  · complaints / fix-its and price negotiations NEVER get a draft
  · a template needing a fact we don't hold → no draft (flag-don't-
    guess); the one exception is the office's own [DATE] placeholder
    habit, kept visibly unfilled exactly like their canned replies
"""

import re

# ── message classification (regexes proven on the Jul-14 mining of
#    4 weeks of inbound: 72 thanks / 33 approve+date / 29 amendments…) ──

_RX = {
    # Jul-14 language dive (4,597 real messages): a customer who praises
    # first and pivots with but/however is REPORTING A PROBLEM politely
    # (178 found — Hope Todd: "prompt and courteous… but the star is
    # swaying"). And a self-correction rewrites their own last message —
    # only a human should untangle which instruction stands (19 found).
    "self_correction": r"never ?mind|i meant|my (mistake|bad)|"
                       r"please disregard|ignore my (last|previous)|"
                       r"sorry,? i (said|sent|meant)",
    "fixit": r"missed|redo|not (done|cleaned)|still dirty|streak|"
             r"complaint|unhappy|left a mess|damage|"
             r"(great|good|excellent|wonderful|courteous|prompt|"
             r"satisfied)[^.!?]{0,80}[.!?][^?]{0,140}\b(but|however|"
             r"although|except|one issue|noticed (a|that|one))",
    "price_discount": r"discount|cheaper|price match|too (high|expensive)"
                      r"|lower price|better price|beat (the|their) price",
    "approve_wants_date": r"(approv\w+|accepted|go ahead|sounds good)"
                          r"[\s\S]{0,220}?\b(when|what day|which day|"
                          r"date|schedule|come out|available)|"
                          r"\b(when|what day)\b[\s\S]{0,80}(come|start|"
                          r"service|schedule)",
    "date_confirm": r"\b((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|"
                    r"dec)[a-z]*\.?\s+\d{1,2}(st|nd|rd|th)?|\d{1,2}/"
                    r"\d{1,2})\b[\s\S]{0,120}?(work|works|fine|good|"
                    r"great|ok|okay|perfect|confirm)|"
                    r"(work|works|fine|good|great|ok|okay|perfect|"
                    r"confirm\w*)[\s\S]{0,60}?\b((jan|feb|mar|apr|may|"
                    r"jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
                    r"\d{1,2}(st|nd|rd|th)?|\d{1,2}/\d{1,2})\b",
    "amendment": r"\binstead\b|\balso add\b|can you add|please add|"
                 r"\bremove\b|take (that|it|this)? ?off|only want|"
                 r"just (want|need) (the|my)|change (the|my) (quote|"
                 r"service)",
    "status_chaser": r"did you (get|receive)|following up|any update|"
                     r"\bstatus\b|haven'?t heard|checking in",
    "approval_only": r"\bapprov\w+\b|accepted (the|your) quote|"
                     r"go ahead|let'?s do it|book (it|us|me)",
    "thanks_ack": r"^\W*(thank(s| you)|great|perfect|awesome|sounds "
                  r"good|got it|ok(ay)?|will do|yes|yep|yup|sure)\b",
}

# order matters: specific before generic; the first two never draft
# date_confirm outranks approve_wants_date: "I approved. July 8th
# works" carries a real date — confirm THAT, don't ask for one (Durga)
_ORDER = ("self_correction", "fixit", "price_discount", "date_confirm",
          "approve_wants_date", "amendment", "status_chaser",
          "approval_only", "thanks_ack")
NO_DRAFT = {"self_correction", "fixit", "price_discount"}

# a 👍 Gmail emoji reaction IS an approval/ack (Lijun Chen, Jul-14 dive)
_EMOJI_ACK = re.compile(r"^\W*(👍|🙏|❤️|reacted via gmail)", re.I)

# approval that carries a RIDER instruction ("I approved it. They will
# need to check the other sides…") — the instruction must reach a
# human/the tech, so the box stays empty (137 of these in a year)
_RIDER = re.compile(r"(please|make sure|need to|don'?t forget|"
                    r"also .{0,30}(check|clean|replace|fix))", re.I)


def classify(text):
    """The customer's newest message → template key, or None."""
    if _EMOJI_ACK.match((text or "").strip()):
        return "thanks_ack"
    t = re.sub(r"\s+", " ", (text or "")).strip().lower()
    # strip quoted reply tails so old office text can't trigger a match
    t = re.split(r"\bon (mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|"
                 r"may|jun|jul|aug|sep|oct|nov|dec)[a-z]*,? .{0,60}"
                 r"(wrote|master butler)", t)[0]
    if not t:
        return None
    for key in _ORDER:
        if re.search(_RX[key], t):
            # thanks_ack must be a SHORT ack — a long message that
            # happens to open with "thanks" deserves a human read
            if key == "thanks_ack" and (len(t) > 220 or "?" in t):
                return None
            return key
    return None


_DATE_RX = re.compile(
    r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    r"\s+\d{1,2}(?:st|nd|rd|th)?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b", re.I)


def _their_date(text):
    m = _DATE_RX.search(text or "")
    return m.group(1) if m else None


def _signature(user, voice):
    titles = (voice.get("signature") or {}).get("titles") or {}
    name = (user or "").strip().title() or "LaRee"
    name = {"Laree": "LaRee"}.get(name, name)
    title = titles.get(name.lower(), "Scheduling Coordinator")
    return (f"At your service,\n{name}\n{title}\nMaster Butler, inc\n"
            f"customercare@masterbutlerinc.com")


def _services_phrase(rec):
    lines = ((rec or {}).get("draft") or {}).get("line_items") or \
            ((rec or {}).get("draft") or {}).get("lines") or []
    names = []
    for li in lines:
        n = (li.get("name") or li.get("label") or "").strip()
        if n and "product" not in n.lower() and "adjustment" not in n.lower():
            names.append(n.lower())
    if not names:
        return None
    if len(names) == 1:
        return "a " + names[0]
    return "a " + ", ".join(names[:-1]) + ", and " + names[-1]


def build_draft(rec, msgs, user=None, voice=None, auto=None):
    """The pre-filled reply box, or None.

    rec  = the customer's newest queue record (may be None)
    msgs = their msglog thread, chronological [{'dir','body','at'},…]
    Returns {'type','draft','why'} — or None with the gate that stopped
    it in mind (shadow page shows proposals only, so None just means
    'the box stays empty, exactly like today')."""
    voice = voice or {}
    if not msgs:
        return None
    last = msgs[-1]
    if last.get("dir") != "in":          # THE rule: customer inbound only
        return None
    rec = rec or {}
    if rec.get("spam_auto") or rec.get("tech_sender") or rec.get("lead") \
            or rec.get("merged_into") or rec.get("kind") == "jobber_event":
        return None
    body = last.get("body") or last.get("subject") or ""
    kind = classify(body)
    if not kind or kind in NO_DRAFT:
        return None
    if kind == "approval_only" and _RIDER.search(body):
        return None          # approval WITH instructions → human + tech
    sig = _signature(user, voice)

    # AUTO-ADOPTED WORDING (Dallon's ruling, Jul 14): when the office
    # has answered this type of message the same way ≥3 times (≥50% of
    # graded pairs), the draft IS their own repeated reply — tokens
    # re-slotted, signature fresh. The built-in template below becomes
    # the fallback.
    if auto and kind in auto:
        body_t = _fill_tokens(auto[kind]["template"], _their_date(body))
        if body_t:
            return {"type": kind,
                    "why": (f"office's own wording — repeated "
                            f"{auto[kind]['count']}× "
                            f"({int(auto[kind]['share']*100)}% of graded "
                            f"replies), auto-adopted"),
                    "auto": True,
                    "draft": f"{body_t}\n\n{sig}"}

    if kind == "thanks_ack":
        return {"type": kind, "why": "confirmation-only message",
                "draft": f"Great, thank you for confirming!\n\n{sig}"}

    if kind in ("date_confirm", "approve_wants_date"):
        d = _their_date(body)
        if kind == "date_confirm" and d:
            svc = _services_phrase(rec)
            mid = f" for {svc}" if svc else ""
            return {"type": kind, "why": f"customer confirmed {d}",
                    "draft": (f"Great!  We have your appointment "
                              f"confirmed on {d}{mid}.  Thank you for "
                              f"booking with us.  We look forward to "
                              f"servicing your home!  Our technician "
                              f"will reach out prior to arrival.\n\n"
                              f"{sig}")}
        # approved but wants a date we don't hold → the office's own
        # canned shape, [DATE] left visible exactly like their template
        return {"type": "approve_wants_date",
                "why": "approved, asking for a date — [DATE] left for "
                       "the office (we don't guess the schedule)",
                "draft": (f"Thank you for approving your quote!  Our "
                          f"next opening in your area is [DATE].  "
                          f"Please let us know if that will work for "
                          f"you.\n\n{sig}")}

    if kind == "approval_only":
        return {"type": kind, "why": "quote approved, no date asked",
                "draft": (f"Thank you for approving your quote!  Our "
                          f"next opening in your area is [DATE].  "
                          f"Please let us know if that will work for "
                          f"you.\n\n{sig}")}

    if kind == "amendment":
        return {"type": kind, "why": "scope-change language — ack only, "
                                     "revised quote stays human-reviewed",
                "draft": (f"Thank you for letting us know.  I’ll update "
                          f"the quote and send the revised copy over "
                          f"shortly — if you don’t see it, please check "
                          f"your ‘junk’ folder.\n\n{sig}")}

    if kind == "status_chaser":
        oq = rec.get("open_quote_ctx") or {}
        sent = (oq.get("sent_at") or oq.get("created") or "")[:10]
        if not sent:
            return None                  # no quote we can vouch for
        return {"type": kind, "why": f"chasing a quote we sent {sent}",
                "draft": (f"Thank you for following up!  Your quote was "
                          f"sent on {sent} — if you don’t see it, please "
                          f"check your ‘junk’ folder.  Please let us "
                          f"know of any questions and how you’d like to "
                          f"proceed.\n\n{sig}")}
    return None


def _norm_body(text):
    """Whole reply → normalized shape with [DATE]/[TIME]/[PRICE]/
    [QUOTE#]/[NAME] tokens, signature stripped. Two office replies with
    the same shape are 'the same reply' even across different jobs."""
    t = re.sub(r"\s+", " ", (text or ""))
    t = re.split(r"at your service", t, flags=re.I)[0]
    t = re.sub(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
               r"[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?",
               "[DATE]", t, flags=re.I)
    t = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", "[DATE]", t)
    t = re.sub(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", "[TIME]", t, flags=re.I)
    t = re.sub(r"\$\s?\d[\d,.]*", "[PRICE]", t)
    t = re.sub(r"#\d{4,6}", "[QUOTE#]", t)
    t = re.sub(r"\b(hi|hello|dear|good (?:morning|afternoon|evening))\s+"
               r"[a-z]+,?", r"\1 [NAME],", t, flags=re.I)
    return t.strip()


def adopt_templates(pairs, off=None):
    """HANDS-OFF WORDING LEARNING (Dallon's ruling, Jul 14: 'automate
    the wording change… it's easier learned' — dates excluded, they
    change every job). The safety that makes automation okay: a
    template auto-changes ONLY to the office's own most-repeated reply,
    verbatim, tokens re-slotted — the system never invents wording.

    pairs = [(kind, office_sent_body), …] from the grading reel.
    Adoption bar: a normalized office shape must appear ≥3 times AND in
    ≥50% of that type's graded pairs. Returns {kind: {'template',
    'count', 'share', 'sample_n'}}. `off` = kinds Dallon disabled."""
    byk = {}
    for kind, sent in pairs:
        shape = _norm_body(sent)
        if 20 < len(shape) < 600:
            byk.setdefault(kind, []).append(shape)
    adopted = {}
    for kind, shapes in byk.items():
        if off and kind in off:
            continue
        best, n = max(((s, shapes.count(s)) for s in set(shapes)),
                      key=lambda x: x[1])
        if n >= 3 and n / len(shapes) >= 0.5:
            adopted[kind] = {"template": best, "count": n,
                             "share": round(n / len(shapes), 2),
                             "sample_n": len(shapes)}
    return adopted


def _fill_tokens(template, their_date, name=None):
    """Re-slot the live facts into an adopted office shape. Unknown
    tokens stay VISIBLE (the office's own [DATE] habit) except [NAME],
    which falls back to nothing rather than a wrong name."""
    t = template
    if their_date:
        t = t.replace("[DATE]", their_date)
    t = re.sub(r"\[NAME\],?", (name + ",") if name else "", t).strip()
    return t


def _norm_sentences(text):
    """Sentences with the always-changing parts blanked (Dallon, Jul 14:
    'if the office changes it the system learns — dates excluding,
    because those change all the time'). Dates, times, prices, quote
    numbers and names-after-greetings all become tokens so only real
    WORDING differences count as learning."""
    t = re.sub(r"\s+", " ", (text or ""))
    t = re.sub(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
               r"[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?",
               "[DATE]", t, flags=re.I)
    t = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", "[DATE]", t)
    t = re.sub(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", "[TIME]", t, flags=re.I)
    t = re.sub(r"\$\s?\d[\d,.]*", "[PRICE]", t)
    t = re.sub(r"#\d{4,6}", "[QUOTE#]", t)
    t = re.sub(r"\b(hi|hello|dear|good (morning|afternoon|evening))\s+"
               r"[a-z]+", r"\1 [NAME]", t, flags=re.I)
    out = []
    for s in re.split(r"(?<=[.!?])\s+", t):
        s = s.strip().lower()
        if 12 < len(s) < 240 and "at your service" not in s \
                and "customercare@" not in s:
            out.append(s)
    return out


def learn_gap(kind, draft, sent):
    """One retro pair (or, in stage 2, one office edit) → what changed.
    Returns {'added': […], 'dropped': […]} of normalized sentences the
    office used that we didn't, and ours they threw away. Dates/times/
    prices/names are normalized out first, so a rescheduled date is NOT
    a lesson but a reworded explanation IS."""
    ours = _norm_sentences(draft)
    theirs = _norm_sentences(sent)
    added = [s for s in theirs if s not in ours]
    dropped = [s for s in ours if s not in theirs]
    return {"kind": kind, "added": added[:6], "dropped": dropped[:6]}


def accuracy(draft, sent):
    """The reply version of the ±10% score (Dallon, Jul 14: 'when the
    office uses it and sends it, id like to see it calculated').
    Similarity of the normalized bodies (dates/times/prices/names
    tokenized out, signatures stripped) as 0–100. 100 = sent verbatim;
    the office rewriting every word ≈ 0."""
    import difflib
    a, b = _norm_body(draft), _norm_body(sent)
    if not a or not b:
        return 0
    return round(difflib.SequenceMatcher(None, a, b).ratio() * 100)


def fold_learning(store, gap):
    """Accumulate a gap into the draft_learnings blob shape:
    {kind: {'added': {sentence: count}, 'dropped': {…}, 'pairs': n}}.
    The office's most-repeated additions float to the top — those are
    the sentences the templates should adopt next (human-approved,
    policy-not-fact doctrine: templates only change via Dallon)."""
    k = store.setdefault(gap["kind"], {"added": {}, "dropped": {},
                                       "pairs": 0})
    k["pairs"] += 1
    for s in gap["added"]:
        k["added"][s] = k["added"].get(s, 0) + 1
    for s in gap["dropped"]:
        k["dropped"][s] = k["dropped"].get(s, 0) + 1
    # keep only the strongest 40 lines each so the blob never bloats
    for side in ("added", "dropped"):
        k[side] = dict(sorted(k[side].items(),
                              key=lambda kv: -kv[1])[:40])
    return store


if __name__ == "__main__":
    # self-check on the mined message shapes (not part of trials)
    cases = [
        ("Thanks for getting back so quickly. I approved the quote. "
         "July 8th date works as well.", "date_confirm"),
        ("I approve the quote — when can you come out?",
         "approve_wants_date"),
        ("Sounds good, thank you!", "thanks_ack"),
        ("Can you remove the gutter cleaning? We had it done recently.",
         "amendment"),
        ("Just following up — did you get my email last week?",
         "status_chaser"),
        ("The tech missed a section and the patio is still dirty.",
         "fixit"),
        ("Is there any discount if my neighbor books too?",
         "price_discount"),
        ("I approve.", "approval_only"),
    ]
    ok = 0
    for body, want in cases:
        got = classify(body)
        print(("✅" if got == want else f"❌ got {got}"), want, "←", body[:50])
        ok += got == want
    print(f"{ok}/{len(cases)}")
    d = build_draft({}, [{"dir": "in", "body": cases[0][0]}], "laree",
                    {"signature": {"titles": {"laree":
                                              "Scheduling Coordinator"}}})
    print("\nsample draft:\n" + d["draft"])
