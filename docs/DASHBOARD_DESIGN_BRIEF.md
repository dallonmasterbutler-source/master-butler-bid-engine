# Dashboard Design Brief — Built From the Office's Own Answers
*(Synthesized July 7, 2026 from Jessica, Martha, and LaRee's questionnaires.
LaRee's answers weighted heaviest — incoming Office Manager, primary user.)*

## The One-Sentence Mission
A bid gets reviewed, adjusted, and approved on ONE screen in under 3 minutes —
with every note, past price, and safety fact already in front of the reviewer.

## What All Three Agreed On (settled, no debate)
- **Bid AGE is critical** — all three picked "how long waiting" top-3.
  Every list row shows an age timer; red as it nears one business day (their
  own 24-hour SLA).
- **They'll feed the learning loop generously** — Martha & LaRee chose
  "I'll write a sentence every time"; Jessica tap+note. LaRee: *"It's a mom
  thing. I don't want to address the same issue again and again."*
  → Reason buttons + optional note; never required, always welcomed.
- **Trust is earned by accuracy over weeks** — they expect a ramp
  ("needs time to learn... we do so many one-offs"). Shadow mode IS the answer.
- **Desktop first, iPad capable. Phone is not a target.** Fast, light pages
  (Martha's machine loads slow — performance is a requirement).
- Middle information density; standard text; "looks like Jobber" is a
  compliment here.

## The First Screen (conflict resolved by role)
Jessica wanted schedule-first, Martha oldest-first, LaRee "emergent issues
of customers or techs." Resolution:
1. **Top band: NEEDS ATTENTION** — emergent items: aging bids, flags,
   escalations, tech questions (LaRee's triage instinct, Jessica's separate
   section, Martha's color-coding all served)
2. **Below: bid list, oldest first** (Martha), each row: age • name •
   services • flags • confidence • total
3. Small schedule glance widget (Jessica) — not the centerpiece

## Reviewing One Bid (the core screen)
Show, collapsible, per Martha+LaRee: the photos it used, the measurements
(sqft/roof/pitch), confidence, and **what we charged similar homes before**
(reconciler history powers this). Jessica needs only the confidence number —
fine, it's the headline anyway.

**When the system is UNSURE (LaRee's rule, and it wins):** leave THAT LINE
blank, alert why, ask the human to fill it. Not a fake guess, not a full
hold. (Engine already behaves this way for unmeasured PW — validated.)

**ALL notes in one place.** Martha's biggest pain and her
"make-it-impossible" mistake: notes scattered across fields get missed.
The bid view aggregates every note — profile, property, must-know, history —
into one visible stack. Zero hunting.

## The Real Reason Buttons (their words, replacing our guesses)
specialty windows · heavy tree coverage · difficult roof ·
rate/sqft pricing update · new info or photos · tech adjustment from last
job · last quote too old · underbid on review · difficult customer premium ·
other (+optional note)

## New Requirements Their Answers Surfaced
1. **TAX auto-check (2 of 3 flagged it!):** correct tax rate attached by
   address on every quote/invoice. Wire taxRateId into quote creation.
2. **Property-first history (LaRee):** homes get serviced under DIFFERENT
   OWNERS — match history by ADDRESS, not just customer. Show "we've serviced
   this home before" regardless of who owns it now.
3. **Multi-property clients (LaRee):** per-home view + per-home Must Know.
   (Schema already property-scoped; dashboard must show it that way.)
4. **Standardized escalation form (LaRee):** "Escalate to Dallon/Tom"
   generates a CONSISTENT template — same format every time so "questions
   don't get missed." Replaces the Exp Tech folder decipher-dance.
5. **Auto photo-request drafting (Martha's trust condition):** when photos
   are needed/bad, the system drafts the ask-for-pictures email.
6. **All sides of the home (LaRee's magic wish):** multi-angle imagery for
   windows/PW — "most often underbid and frustrating to techs." Street View
   multi-angle + eventual aerial = the answer; priority raised.
7. **Old-pricing mistakes (LaRee):** e.g. moss product billed $13 vs current
   $14.50 — solved structurally: system always prices from current config.
8. **Contextual reply drafting (Martha):** "thanks for reaching back out"
   for repeats; auto-surface applicable discounts (their real discount menu
   lives in Jobber field 49).
9. Service combinability rules (Martha): what can be booked solo, by season,
   with dry-day-only notes on the quote. (CUSTOMER: note lane handles the
   dry-day display.)

## Known Out-of-Scope (noted, not lost)
- "When is my tech arriving" daily barrage (LaRee) — scheduling comms,
  a future phase beyond bidding.

## Open Rulings for Dallon/Tom
- Roof blow-off WITHOUT gutter cleaning: allowed solo? price change?
- $250 gutter-cleaning minimum (Jessica) vs $150 universal: year-round rule?
