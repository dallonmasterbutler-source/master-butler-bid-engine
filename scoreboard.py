"""
MASTER BUTLER — SHADOW SCOREBOARD

The report card that earns (or revokes) trust: for every request the
system shadow-drafted, find the quote the OFFICE actually created in
Jobber for the same customer, and put the two numbers side by side.

  system total  vs  office total  →  gap, and a running accuracy tally

Matching: customer email first (exact), then name + street word overlap.
Read-only against Jobber. Results → data/scoreboard.json; the dashboard
shows the running score.

Run:  python3 scoreboard.py [how_many_recent_quotes]
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import jobber_client as jc

BASE = Path(__file__).parent
SHADOW = BASE / "data" / "shadow_bids"
OUT = BASE / "data" / "scoreboard.json"

QUOTES_QUERY = """
query Recent($first: Int!, $after: String) {
  quotes(first: $first, after: $after, sort: {key: CREATED_AT, direction: DESCENDING}) {
    pageInfo { hasNextPage endCursor }
    nodes { quoteNumber quoteStatus createdAt jobberWebUri
            client { name emails { address } }
            property { address { street city } }
            amounts { total }
            lineItems { nodes { name totalPrice } } }
  }
}
"""


def _norm_words(s):
    return set(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())


# staff / manager test entries are not real customers — never score
# them (LaRee, Jul 10: 'remove Jessica Jensen's job, she's our manager').
# Office-editable via the scoreboard_exclude blob.
SCOREBOARD_EXCLUDE = ("jessica jensen",)


def _excluded(rec):
    frm = (rec.get("from") or "").lower()
    names = list(SCOREBOARD_EXCLUDE)
    try:
        import clouddb
        names += [n.lower() for n in (clouddb.get_blob("scoreboard_exclude") or [])]
    except Exception:
        pass
    return any(n and n in frm for n in names)


def load_shadows():
    """Shadow records that actually produced a priced draft — local files
    PLUS the cloud's records (manual entries never touch this Mac)."""
    out, seen = [], set()

    def add(rec, stamp):
        if not stamp or stamp in seen:
            return
        # folded duplicates and filtered spam never make scoreboard rows
        # (Dallon Jul 9: 'there are duplicates in the scoreboard')
        if rec.get("merged_into") or rec.get("spam_auto") \
                or rec.get("tech_sender") or _excluded(rec):
            return
        try:                    # pre-gate spam records never score
            import spam_filter
            if spam_filter.looks_spam(rec.get("from"), rec.get("subject"),
                                      rec.get("newest_message") or "")[0]:
                return
        except Exception:
            pass
        rec["stamp"] = stamp
        if rec.get("draft") and rec["draft"].get("total"):
            out.append(rec); seen.add(stamp)
        elif rec.get("pipeline_output") and "TOTAL" in rec.get("pipeline_output", ""):
            m = re.search(r"TOTAL\s+\$(\d+)", rec["pipeline_output"])
            if m:
                rec["draft"] = {"total": float(m.group(1))}
                out.append(rec); seen.add(stamp)

    try:
        import clouddb                     # when we ARE the cloud
        if clouddb.available():
            for stamp, rec in clouddb.all_shadow():
                add(rec, stamp)
    except Exception:
        pass
    try:                                   # cloud first (the full truth)
        import urllib.request
        from base64 import b64encode
        from cloudpush import _cfg
        url, pw = _cfg("DASHBOARD_URL"), _cfg("DASHBOARD_PASSWORD")
        if url and pw:
            req = urllib.request.Request(
                url.rstrip("/") + "/api/records",
                headers={"Authorization": "Basic "
                         + b64encode(f"office:{pw}".encode()).decode()})
            for rec in json.load(urllib.request.urlopen(req, timeout=60)):
                add(rec, rec.get("stamp"))
    except Exception:
        pass
    for p in sorted(SHADOW.glob("*.json")):
        add(json.loads(p.read_text()), p.stem)
    return out


def fetch_recent_quotes(limit=60):
    """Politely page recent office quotes (same throttle manners as the
    reconciler — quotes queries are cost-heavy)."""
    jc.DRY_RUN = False
    quotes, cursor = [], None
    while len(quotes) < limit:
        data = jc._post(QUOTES_QUERY,
                        {"first": min(10, limit - len(quotes)), "after": cursor},
                        "recent quotes")
        if data.get("error"):
            body = str(data.get("body", ""))
            if "THROTTLED" in body.upper():
                time.sleep(20)
                continue
            raise SystemExit(f"quotes query failed: {data}")
        block = data["quotes"]
        quotes += block["nodes"]
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(2)
    return quotes


def match(shadow, quotes):
    """Find the office quote for this shadow draft, or None.

    Only quotes CREATED AFTER the request arrived count — an old quote
    for the same customer is property history, not the office's answer
    (learned from Nithya Kannan: tonight's PW request nearly graded
    against an archived gutter quote from weeks earlier)."""
    stamp = shadow.get("stamp", "")
    if len(stamp) >= 8 and stamp[:8].isdigit():
        req_day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        quotes = [q for q in quotes
                  if (q.get("createdAt") or "9999")[:10] >= req_day]
    # prefer the PARSED customer (form submissions arrive 'from Squarespace' —
    # the real person is in the parsed body, not the envelope)
    cust = (shadow.get("draft") or {}).get("customer") or {}
    email = (cust.get("email") or "").lower()
    if not email:
        m = re.search(r"<([^>]+)>", shadow.get("from", ""))
        email = (m.group(1).lower() if m else "")
        if "squarespace" in email or "form-submission" in email:
            email = ""
    name_words = _norm_words(cust.get("name")
                             or shadow.get("from", "").split("<")[0])
    addr_words = _norm_words(cust.get("address") or shadow.get("address") or "")

    for q in quotes:
        q_emails = [e["address"].lower() for e in q["client"].get("emails", [])]
        if email and email in q_emails:
            return q, "email"
    for q in quotes:
        q_name = _norm_words(q["client"]["name"])
        street = _norm_words((q.get("property") or {}).get("address", {})
                             .get("street", ""))
        name_hit = q_name and name_words and \
            len(q_name & name_words) >= min(2, len(q_name))
        addr_hit = street and addr_words and len(street & addr_words) >= 2
        if name_hit and (addr_hit or not addr_words):
            return q, "name+address" if addr_hit else "name"
    # ADDRESS-ONLY: form relays bury the real name ('from Squarespace'),
    # but a house number + street match is unambiguous on its own.
    shadow_num = next((w for w in addr_words if w.isdigit()), None)
    if shadow_num:
        for q in quotes:
            street = _norm_words((q.get("property") or {}).get("address", {})
                                 .get("street", ""))
            if shadow_num in street and len(street & addr_words) >= 3:
                return q, "address"
    return None, None


def run(limit=60):
    shadows = load_shadows()
    # ONE row per customer: several emails from the same person collapse
    # to their NEWEST priced draft (matches the Inbox's one-entry rule)
    by_cust = {}
    for s in sorted(shadows, key=lambda r: r["stamp"]):
        m = re.search(r"<([^>]+)>", s.get("from") or "")
        key = (m.group(1).lower() if m else s["stamp"])
        by_cust[key] = s
    shadows = list(by_cust.values())
    quotes = fetch_recent_quotes(limit)
    rows, matched = [], 0
    used_quotes = set()
    for s in shadows:
        q, how = match(s, quotes)
        if q and q["quoteNumber"] in used_quotes:
            q, how = None, None       # a quote only ever matches once
        if q:
            used_quotes.add(q["quoteNumber"])
        row = {"stamp": s["stamp"], "customer": s.get("from"),
               "services": s.get("services"),
               "system_total": s["draft"]["total"]}
        if q:
            matched += 1
            office = q["amounts"]["total"]
            row.update({
                "office_quote": q["quoteNumber"], "office_total": office,
                "office_status": q["quoteStatus"], "matched_by": how,
                "salesperson": ((q.get("salesperson") or {}).get("name")
                                or {}).get("full"),
                "jobber_url": q.get("jobberWebUri"),
                "gap": round(s["draft"]["total"] - office, 2),
                "gap_pct": (round(100 * (s["draft"]["total"] - office) / office, 1)
                            if office else None),
                "office_lines": [{"name": li["name"], "price": li["totalPrice"]}
                                 for li in q["lineItems"]["nodes"]],
            })
        else:
            row["office_quote"] = None      # office hasn't quoted it yet
        rows.append(row)
    report = {"generated": datetime.now().isoformat(timespec="seconds"),
              "shadow_drafts": len(shadows), "matched": matched,
              "quotes_scanned": len(quotes), "rows": rows}
    OUT.write_text(json.dumps(report, indent=1))
    return report


if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    r = run(limit)
    print(f"Shadow drafts: {r['shadow_drafts']}   matched to office quotes: "
          f"{r['matched']}   (scanned {r['quotes_scanned']} recent quotes)\n")
    for row in r["rows"]:
        if row.get("office_quote"):
            print(f"  {row['customer'][:38]:<38} system ${row['system_total']:<7} "
                  f"office ${row['office_total']:<8} gap {row['gap_pct']}% "
                  f"(#{row['office_quote']}, {row['matched_by']})")
        else:
            print(f"  {row['customer'][:38]:<38} system ${row['system_total']:<7} "
                  f"office: not quoted yet")
    print(f"\nSaved -> {OUT}")
