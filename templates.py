"""
MASTER BUTLER — OFFICE DRAFT TEMPLATES

Two standardized drafts the questionnaires asked for:

  * PHOTO REQUEST (Martha's trust condition): when photos are missing or
    unusable, the system writes the ask-for-pictures email FOR the office.
  * ESCALATION (LaRee's rule): "Escalate to Dallon/Tom" always produces
    the SAME format, so questions stop getting missed in the shuffle.

HARD SAFETY RULE: nothing here sends anything. Drafts are text files in
data/outbox_drafts/ and data/escalations/ — a human copies them into
Gmail/Jobber if and when they choose.
"""

from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
OUTBOX = BASE / "data" / "outbox_drafts"
ESCALATIONS = BASE / "data" / "escalations"

# What a photo needs to show, per service — so customers send USEFUL pictures.
PHOTO_GUIDANCE = {
    "driveway": "the full driveway from the street, so the whole length is in frame",
    "patio":    "the whole patio from a corner, with a door or table visible for scale",
    "sidewalk": "the walkway end-to-end (a couple of photos is fine if it's long)",
    "deck":     "the deck surface and railings",
    "gutters":  "the front of the house showing the roofline",
    "roof":     "the roof from across the street (whole slope in frame)",
    "moss":     "the mossy areas of the roof, plus one wider shot of the roof",
    "windows":  "the sides of the home showing the windows",
    "house_wash": "each side of the house you'd like washed",
}


def draft_photo_request(customer_name, services, reason=""):
    """Write a ready-to-send photo-request email draft. Returns the path."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    first = (customer_name or "there").split()[0]
    wants = [PHOTO_GUIDANCE[s] for s in services if s in PHOTO_GUIDANCE]
    bullets = "\n".join(f"  • A photo of {w}" for w in wants) or \
              "  • A few photos of the areas you'd like serviced"
    body = f"""Subject: Quick photos so we can finalize your quote

Hi {first},

Thanks for reaching out to Master Butler! To get you an accurate quote
(and often a better price than a sight-unseen estimate), could you snap
a few quick phone pictures for us?

{bullets}

No need for anything fancy — regular phone photos work great. Just reply
to this email with them attached and we'll have your quote right over.

At your service,

Master Butler Inc.
customercare@masterbutlerinc.com
"""
    if reason:
        body = f"[WHY THIS DRAFT EXISTS: {reason}]\n\n" + body
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUTBOX / f"photo-request-{stamp}.txt"
    path.write_text(body)
    return path


def draft_escalation(bid_ref, customer, address, question, to="dallon",
                     services=None, system_total=None, confidence=None,
                     notes=None):
    """Write LaRee's standardized escalation form. Returns the path.

    Same fields, same order, every time — that's the whole point."""
    ESCALATIONS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    lines = [
        "══════════ ESCALATION — BID REVIEW ══════════",
        f"TO:            {to.upper()}",
        f"DATE:          {datetime.now():%B %d, %Y %I:%M %p}",
        f"BID:           {bid_ref}",
        f"CUSTOMER:      {customer}",
        f"ADDRESS:       {address}",
        f"SERVICES:      {', '.join(services) if services else '—'}",
        f"SYSTEM PRICE:  {'$%.0f' % system_total if system_total else '—'}",
        f"CONFIDENCE:    {str(confidence) + '%' if confidence is not None else '—'}",
        "",
        "THE QUESTION (one thing we need from you):",
        f"  {question}",
        "",
        "FLAGS ON THIS BID:",
    ]
    lines += [f"  ⚠ {n}" for n in (notes or [])] or ["  (none)"]
    lines += ["", "REPLY WITH: a price, a rule, or 'come see it'.",
              "═════════════════════════════════════════════"]
    path = ESCALATIONS / f"escalation-{stamp}.txt"
    path.write_text("\n".join(lines))
    return path
