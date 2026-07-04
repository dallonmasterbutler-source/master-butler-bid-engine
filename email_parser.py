"""
MASTER BUTLER — EMAIL PARSER

Reads a customer email (.eml file or raw message) and extracts the facts
the bid engine needs:

  - who sent it (name + email + phone if present)
  - the property address (if they gave one)
  - which services they're asking about (mapped to OUR service names,
    even when the customer uses their own words)
  - what KIND of email it is (new request / question / scheduling reply)
  - only the NEWEST message — quoted reply-chains are stripped away

Plain-English notes throughout. This runs on saved .eml files today;
later the same code will run on live Gmail messages (same format).
"""

import email
import email.policy
import re
from pathlib import Path


# ─────────────────────────────────────────────────────────────
# STEP 1: CUSTOMER LANGUAGE → OUR SERVICES
# Customers never use our exact service names. This table maps the
# words they actually write to the services we actually sell.
# Order matters: more specific phrases are checked first.
# ─────────────────────────────────────────────────────────────

SERVICE_KEYWORDS = [
    # (phrase to look for, our service name)
    ("moss treatment and removal", "moss_treatment+moss_removal"),
    ("moss treatment & removal",   "moss_treatment+moss_removal"),
    ("moss removal",               "moss_removal"),
    ("moss treatment",             "moss_treatment"),
    ("white powder on the roof",   "moss_treatment"),   # real customer phrasing (Vadim)
    ("white powder",               "moss_treatment"),
    ("moss",                       "moss_treatment"),
    ("gutter guard",               "roof_blow_off_guards"),  # blow-off over installed guards
    ("gutter blowout",             "roof_blow_off"),
    ("gutter cleaning",            "gutter_cleaning"),
    ("gutters",                    "gutter_cleaning"),
    ("gutter",                     "gutter_cleaning"),
    ("roof cleaning",              "roof_blow_off"),    # customers say "roof cleaning"
    ("roof blow off",              "roof_blow_off"),
    ("roof blow-off",              "roof_blow_off"),
    ("roof maintenance",           "roof_blow_off"),
    ("inside/outside window",      "windows_in_out"),
    ("in/out window",              "windows_in_out"),
    ("interior and exterior window", "windows_in_out"),
    ("window cleaning in & out",   "windows_in_out"),
    ("exterior window",            "windows_exterior"),
    ("external window",            "windows_exterior"),
    ("window cleaning",            "windows_unspecified"),  # in or out? office confirms
    ("windows",                    "windows_unspecified"),
    ("house wash",                 "house_wash"),
    ("house washing",              "house_wash"),
    ("pressure wash",              "pressure_washing"),
    ("pressure washing",           "pressure_washing"),
    ("power wash",                 "pressure_washing"),
    ("driveway",                   "pw_driveway"),
    ("patio",                      "pw_patio"),
    ("paver",                      "pw_patio"),
    ("sidewalk",                   "pw_sidewalk"),
    ("walkway",                    "pw_sidewalk"),
    ("deck",                       "pw_deck"),
    ("dryer vent",                 "dryer_vent"),
    ("holiday light",              "holiday_lights"),
    ("christmas light",            "holiday_lights"),
    ("bird control",               "bird_control"),
    ("bird mesh",                  "bird_control"),
    ("gutter whitening",           "exterior_gutter_cleaning"),
    ("exterior gutter",            "exterior_gutter_cleaning"),
]

# Phrases that signal the email is a QUESTION or scheduling reply,
# not a new bid request. These route to a human, not the bid engine.
QUESTION_SIGNALS = [
    "what is the difference", "what's the difference", "can you remind me",
    "how much", "what does", "what do you", "i don't remember",
    "can you explain", "?",
]

SCHEDULING_SIGNALS = [
    "reschedule", "that works", "works for us", "works for me",
    "confirm", "appointment", "will not be home", "not available",
    "what time", "when would", "arriving", "opening", "spot",
    "unable to do", "does 7/", "does 6/", "will august", "will july",
]


# ─────────────────────────────────────────────────────────────
# STEP 2: PULL THE NEWEST MESSAGE OUT OF A REPLY CHAIN
# Real emails carry years of quoted history. We keep only the text
# the customer just wrote, and throw away everything they quoted.
# ─────────────────────────────────────────────────────────────

# Lines/blocks that mark "everything below this is old quoted mail"
QUOTE_MARKERS = [
    re.compile(r"^\s*On .{5,80} wrote:\s*$", re.MULTILINE),      # "On Jun 24, 2026, at 10:42 AM, X wrote:"
    re.compile(r"^\s*On .{5,120} wrote:", re.MULTILINE),
    re.compile(r"^_{10,}\s*$", re.MULTILINE),                      # Outlook's ____________ separator
    re.compile(r"^\s*From:\s.+", re.MULTILINE),                    # forwarded headers block
    re.compile(r"^\s*-{3,}\s*Original Message\s*-{3,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Begin forwarded message:", re.MULTILINE),
]


def newest_message_only(body: str) -> str:
    """Cut the body at the first sign of quoted/forwarded history."""
    cut_at = len(body)
    for marker in QUOTE_MARKERS:
        m = marker.search(body)
        if m:
            cut_at = min(cut_at, m.start())
    fresh = body[:cut_at]
    # Also drop any leftover "> quoted" lines and signature noise
    lines = []
    for line in fresh.splitlines():
        if line.strip().startswith(">"):
            continue
        if line.strip().lower() in ("sent from my iphone", "sent from my ipad"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# ─────────────────────────────────────────────────────────────
# STEP 3: FIND FACTS IN THE TEXT (address, phone)
# ─────────────────────────────────────────────────────────────

# US street address like "24323 SE 42nd Pl, Sammamish WA 98029"
ADDRESS_RE = re.compile(
    r"\b(\d{2,6}\s+(?:[NSEW]{1,2}\s+)?[\w\s\.]{2,30}?"
    r"(?:St|Street|Ave|Avenue|Pl|Place|Rd|Road|Dr|Drive|Way|Ln|Lane|Blvd|Ct|Court)\b"
    r"(?:\s*(?:SE|SW|NE|NW|S|N|E|W))?"
    r"[,\s]+[\w\s]{2,25}?,?\s*(?:WA|Washington)\.?\s*\d{5})",
    re.IGNORECASE,
)

PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")


def find_address(text: str):
    m = ADDRESS_RE.search(text)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def find_phone(text: str):
    m = PHONE_RE.search(text)
    return m.group(0).strip() if m else None


# ─────────────────────────────────────────────────────────────
# STEP 4: WHICH SERVICES ARE THEY ASKING FOR?
# ─────────────────────────────────────────────────────────────

def find_services(text: str):
    """Return our service names found in the customer's words (deduped)."""
    # Collapse line breaks/extra spaces so phrases split across lines still match
    lowered = re.sub(r"\s+", " ", text.lower())
    found = []
    for phrase, service in SERVICE_KEYWORDS:
        if phrase in lowered:
            for s in service.split("+"):
                if s not in found:
                    found.append(s)
    # "windows_unspecified" is redundant if a specific window service matched
    if "windows_unspecified" in found and (
        "windows_exterior" in found or "windows_in_out" in found
    ):
        found.remove("windows_unspecified")
    # generic "pressure_washing" is redundant if a specific surface matched
    if "pressure_washing" in found and any(
        s.startswith("pw_") for s in found
    ):
        found.remove("pressure_washing")
    return found


# ─────────────────────────────────────────────────────────────
# STEP 5: WHAT KIND OF EMAIL IS THIS?
# new_request  -> feed the bid engine
# question     -> a human should reply
# scheduling   -> office handles the calendar
# ─────────────────────────────────────────────────────────────

def classify(text: str, services: list) -> str:
    lowered = text.lower()
    sched_hits = sum(1 for s in SCHEDULING_SIGNALS if s in lowered)
    question_hits = sum(1 for q in QUESTION_SIGNALS if q in lowered)

    if services and question_hits <= 1 and sched_hits == 0:
        return "new_request"
    if sched_hits >= 1 and not services:
        return "scheduling"
    if question_hits >= 2:
        return "question"
    if services:
        return "new_request"
    return "scheduling" if sched_hits else "other"


# ─────────────────────────────────────────────────────────────
# STEP 6: PUT IT ALL TOGETHER — parse one .eml file
# ─────────────────────────────────────────────────────────────

def parse_eml(path) -> dict:
    raw = Path(path).read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    # Sender name + email from the From: header
    from_header = msg.get("From", "")
    sender_email = email.utils.parseaddr(from_header)[1]
    sender_name = email.utils.parseaddr(from_header)[0] or None

    # Prefer the plain-text body; fall back to stripped HTML
    body = ""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        body = plain.get_content()
    else:
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            body = re.sub(r"<[^>]+>", " ", html_part.get_content())
            body = re.sub(r"&nbsp;", " ", body)

    fresh = newest_message_only(body)
    services = find_services(fresh)

    return {
        "file": Path(path).name,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": msg.get("Subject", "").strip(),
        "newest_message": fresh[:300],   # preview
        "address": find_address(fresh) or find_address(body),
        "phone": find_phone(fresh),
        "services": services,
        "kind": classify(fresh, services),
        "has_attachments": any(
            part.get_content_disposition() == "attachment"
            for part in msg.walk()
        ),
    }


# ─────────────────────────────────────────────────────────────
# RUN IT: parse every email in test_emails/ and print a report
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    folder = Path(__file__).parent / "test_emails"
    results = [parse_eml(p) for p in sorted(folder.glob("*.eml"))]

    print(f"Parsed {len(results)} emails\n" + "=" * 70)
    for r in results:
        print(f"\n📧 {r['file']}")
        print(f"   From:     {r['sender_name']} <{r['sender_email']}>")
        print(f"   Kind:     {r['kind'].upper()}")
        if r["services"]:
            print(f"   Services: {', '.join(r['services'])}")
        if r["address"]:
            print(f"   Address:  {r['address']}")
        if r["phone"]:
            print(f"   Phone:    {r['phone']}")

    # Summary
    print("\n" + "=" * 70)
    kinds = {}
    for r in results:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print("SUMMARY:", ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())))
    with_addr = sum(1 for r in results if r["address"])
    with_svc = sum(1 for r in results if r["services"])
    print(f"Emails with an address found: {with_addr}/{len(results)}")
    print(f"Emails with services identified: {with_svc}/{len(results)}")
