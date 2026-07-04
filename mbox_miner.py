"""
MASTER BUTLER — MAILBOX MINER

One streaming pass over the giant Takeout .mbox that does two jobs:

  JOB 1 (parser stress-test): find every website form submission and
         check how well we extract name / email / phone / address / services.

  JOB 2 (pricing archaeology): find every email that mentions a service
         next to a dollar amount (quote-change requests, office quotes)
         and collect service->price pairs as calibration anchors.

Outputs land in data/ (gitignored — customer info never goes to GitHub).
"""

import email
import email.policy
import re
import csv
from pathlib import Path

from email_parser import find_services, find_address, find_phone

MBOX = Path.home() / "Downloads" / "All mail Including Spam and Trash-002.mbox"
OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)


# ── helpers ──────────────────────────────────────────────────

def iter_messages(path):
    """Stream messages out of an mbox without loading 8 GB into memory."""
    buf = []
    with open(path, "rb") as f:
        for line in f:
            if line.startswith(b"From ") and buf:
                yield b"".join(buf)
                buf = [line]
            else:
                buf.append(line)
        if buf:
            yield b"".join(buf)


def body_text(msg):
    """Best-effort plain text of a message (falls back to stripped HTML)."""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return part.get_content()
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            txt = re.sub(r"<[^>]+>", " ", part.get_content())
            return re.sub(r"&nbsp;|&amp;", " ", txt)
    except Exception:
        pass
    return ""


# ── JOB 1: form submissions ─────────────────────────────────

FORM_FIELD = re.compile(r"^\s*(Name|Email Address|Phone|Address|IN-HOUSE SERVICES|"
                        r"Additional information for us to note:?):[ \t]*(.*)$",
                        re.IGNORECASE | re.MULTILINE)

def mine_form(text):
    """Pull the labeled fields out of a Squarespace form email."""
    fields = {}
    for m in FORM_FIELD.finditer(text):
        key = m.group(1).lower().strip().rstrip(":")
        val = m.group(2).strip()
        if val and key not in fields:
            fields[key] = val
    return fields


# ── JOB 2: service + price pairs ────────────────────────────

# e.g. "pressure wash pathway (currently billed at $75)"
#      "Gutter cleaning: $372"   "Sunroom: $100 for exterior"
PRICE_NEAR_SERVICE = re.compile(
    r"([A-Za-z][A-Za-z /&\-]{3,40}?)"          # a service-ish phrase
    r"[^$\n]{0,40}?"                            # a little glue text
    r"\$\s?(\d{2,4}(?:\.\d{2})?)",              # the dollar amount
)

SERVICE_WORDS = ("gutter", "roof", "moss", "window", "pressure", "wash",
                 "driveway", "patio", "sidewalk", "pathway", "deck",
                 "dryer", "house", "sunroom", "blow")

def mine_prices(text):
    """Find 'service ... $amount' pairs worth keeping."""
    out = []
    for m in PRICE_NEAR_SERVICE.finditer(text):
        phrase = m.group(1).strip().lower()
        amount = float(m.group(2))
        if any(w in phrase for w in SERVICE_WORDS) and 20 <= amount <= 5000:
            out.append((phrase, amount))
    return out


# ── the single pass ─────────────────────────────────────────

def main():
    forms, prices = [], []
    n = form_n = price_n = 0

    for raw in iter_messages(MBOX):
        n += 1
        if n % 10000 == 0:
            print(f"  ...{n} messages scanned "
                  f"({form_n} forms, {price_n} price emails so far)")

        # cheap byte-level pre-filters before expensive parsing
        is_form = b"form-submission@squarespace" in raw or b"Form Submission" in raw[:2000]
        maybe_price = (b"quote #" in raw.lower()[:3000] or b"billed at" in raw
                       or b"Changes requested" in raw[:2000])
        if not (is_form or maybe_price):
            continue

        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            continue
        text = body_text(msg)
        if not text:
            continue

        if is_form:
            fields = mine_form(text)
            if fields.get("name") or fields.get("email address"):
                form_n += 1
                services = find_services(fields.get("in-house services", "")
                                         + " " + fields.get("additional information for us to note", ""))
                forms.append({
                    "date": msg.get("Date", "")[:31],
                    "name": fields.get("name", ""),
                    "email": fields.get("email address", ""),
                    "phone": fields.get("phone", "") or (find_phone(text) or ""),
                    "address": fields.get("address", "") or (find_address(text) or ""),
                    "services_raw": fields.get("in-house services", ""),
                    "services_mapped": "+".join(services),
                    "notes": fields.get("additional information for us to note", "")[:150],
                })

        if maybe_price:
            found = mine_prices(text)
            if found:
                price_n += 1
                subj = str(msg.get("Subject", ""))[:70]
                for phrase, amount in found:
                    prices.append({"date": msg.get("Date", "")[:31],
                                   "subject": subj,
                                   "service_phrase": phrase,
                                   "amount": amount})

    # write results
    if forms:
        with open(OUT / "form_submissions.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=forms[0].keys())
            w.writeheader(); w.writerows(forms)
    if prices:
        with open(OUT / "service_prices.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=prices[0].keys())
            w.writeheader(); w.writerows(prices)

    # report
    print(f"\nScanned {n} messages total")
    print(f"FORM SUBMISSIONS: {len(forms)} parsed -> data/form_submissions.csv")
    if forms:
        have = lambda k: sum(1 for r in forms if r[k])
        print(f"  with name: {have('name')}, email: {have('email')}, "
              f"phone: {have('phone')}, address: {have('address')}, "
              f"services mapped: {have('services_mapped')}")
    print(f"PRICE PAIRS: {len(prices)} from {price_n} emails -> data/service_prices.csv")


if __name__ == "__main__":
    main()
