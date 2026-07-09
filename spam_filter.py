"""
MASTER BUTLER — SPAM / SOLICITATION FILTER
(Dallon, Jul 8 night: "go through our most recent 200 emails and learn
what a spam email looks like, and what a bid email looks like... teach
our program... to avoid putting it in the dashboard.")

Learned from hand-labeling all 179 emails in the account (Jul 8, 2026).
What spam looks like HERE is remarkably consistent — it always sells
something TO the business:

  · financing/loans/credit pitches   (private lenders, 0% APR cards)
  · lead lists & marketing services  ("verified list of...", "Best of
    Monroe", design/SEO shops)
  · industry tools & suppliers       (roofing SaaS, Cambodian holiday-
    light manufacturers — Tom's verdict on that one: "Junk")
  · newsletters & get-rich content   (trade mags, "dead woman returns
    from heaven")

What a REAL bid/customer email looks like: a Squarespace "Get a Quote"
form, a personal address replying inside one of OUR threads, a plain
ask mentioning THEIR home ("we are in need of window and roof
cleaning... you have done our Christmas lights"), a CopyCall voicemail,
or a Jobber event.

DOCTRINE: hiding a customer is the one unforgivable failure. Every
rule errs toward keeping mail visible; the guards below clear anything
that smells like a homeowner, and whatever this module flags still
shows on the dashboard in its own drawer — filtered, never vanished.
"""

import re

# senders that must NEVER be filed as spam, no matter the words —
# these are the pipes real work arrives through
PROTECTED_SENDERS = ("squarespace", "form-submission", "copycall",
                     "getjobber", "masterbutlerinc.com",
                     "@txn.", "jobber")

# a homeowner talking about THEIR property (any one clears the email)
_HOMEOWNER_PATS = [
    r"\b(quote|estimate|bid|price|pricing)\b.{0,30}\b(my|our|the)\s+"
    r"(house|home|property|address|roof|gutters?|windows?|driveway|"
    r"patio|deck|moss)",
    r"\b(my|our)\s+(house|home|property|address|roof|gutters?|windows?|"
    r"driveway|patio|deck|rental|residence|skylights?)\b",
    r"\bin need of\b.{0,50}\b(clean|wash|gutter|roof|window|moss|vent)",
    r"\byou\s*(?:'ve| have)?\s*(did|done|cleaned|serviced|washed|"
    r"installed)\b.{0,30}\b(our|my)\b",
    r"\b(schedule|scheduled|appointment|reschedul)",
    r"\b(approve|approving|approved)\b.{0,20}\bquote\b",
]
_HOMEOWNER_PATS = [re.compile(p, re.I | re.S) for p in _HOMEOWNER_PATS]

# selling-to-us phrases, each worth a point (learned from the real 14)
SELL_PHRASES = (
    # financing / credit (3 of the 14)
    "private lender", "business loan", "line of credit", "0% intro apr",
    "intro apr", "personal guarantee", "hard credit pull", "credit pull",
    "working capital", "business owners", "your savings",
    "your investments",
    # lead lists / marketing services
    "list of property management", "verified list", "decision-makers",
    "lead generation", "leads for", "we did not hear back",
    "don't miss this", "you were recommended", "best of monroe",
    "award", "nomination", "featured in",
    # vendors / suppliers
    "manufacturer", "wholesale", "factory price", "product line",
    "oem", "catalog", "extend your current",
    # tools / SaaS pitched at contractors
    "roofing companies", "built something specifically", "schedule a demo",
    "book a call", "grow your business", "increase your revenue",
    "seo", "google ranking", "in your market", "commercial quotes",
    "circling back", "quick call", "per service area",
    "we only work with one company", "guarantee you",
    # newsletter / content machinery
    "view in browser", "view online", "unsubscribe",
    "subscription is pending", "documentary", "webinar", "whitepaper",
)

# marketing-mill sender shapes: news.foo.com, mail.foo.com, sales@ …
_MKT_SENDER = re.compile(
    r"@(?:news|mail|email|pro|reports|update|updates|marketing|hello|"
    r"info|e|em|go|try)\.|^(?:sales|newsletter|marketing|promo|offers|"
    r"deals|hello|info)@|\.info\b", re.I)

# confirmed spam domains from the Jul 8 study (Tom/Dallon-verified junk)
KNOWN_SPAM_DOMAINS = (
    "theprosperityprinciples.com", "milkymoneyway.com",
    "globalaxisintel.com", "99designs.com", "kaizendirectmedia.com",
    "wavevector.info", "lamues.com", "themaverickaiservice.com",
    "reliablebackground", "automationworld", "controldesign",
    "stockalert", "callclnr.co", "propertyleads",
)


def looks_spam(sender, subject, body, has_address=False,
               list_unsub=False, kind=""):
    """(is_spam, reason). Errs toward NOT spam — a hidden customer is
    worse than ten visible solicitations."""
    sender = (sender or "").lower()
    text = f"{subject or ''} {body or ''}"
    low = text.lower()

    # ── the guards: anything homeowner-shaped is never spam ──
    if has_address or kind in ("phone_lead", "jobber_event"):
        return False, ""
    if any(p in sender for p in PROTECTED_SENDERS):
        return False, ""
    if "customercare@masterbutlerinc" in low:      # replying in OUR thread
        return False, ""
    for pat in _HOMEOWNER_PATS:
        if pat.search(text):
            return False, ""

    # ── scoring ──
    score, why = 0, []
    dom = sender.split("@")[-1].rstrip(">") if "@" in sender else sender
    if any(d in dom for d in KNOWN_SPAM_DOMAINS):
        score += 2
        why.append("known junk sender")
    if _MKT_SENDER.search(sender):
        score += 1
        why.append("marketing-mill address")
    if list_unsub:
        score += 1
        why.append("bulk-mail unsubscribe header")
    hits = [p for p in SELL_PHRASES if p in low]
    if hits:
        score += min(len(hits), 3)
        why.append("selling to us: " + ", ".join(f"“{h}”" for h in hits[:3]))

    return score >= 2, "; ".join(why)
