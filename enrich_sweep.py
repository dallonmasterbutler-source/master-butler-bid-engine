"""
MASTER BUTLER — DASHBOARD ENRICHMENT SWEEP (Dallon, Jul 9 pm: "go back
and link all the info we have in every name on the dashboard. if there
is an open quote, link it. bring over all data we have and make this
dashboard as clean as we can, as complete as we can" — LaRee goes live
on it for a full day after tomorrow's call.)

For every real customer record (no spam / robots / techs / merged):
  1. missing Jobber profile link -> client_summary
  2. missing open-quote context  -> find_open_quote (+approved case)
  3. voicemails: caller-ID -> name/address/email/profile; then the
     open-quote check runs on the client's email too
  4. missing address -> pipeline-found or Jobber address written back
  5. known client -> port their Jobber profile photos (kind 'jobber')

Run with DATABASE_URL set (direct cloud writes):
  DATABASE_URL=... python3 enrich_sweep.py
Idempotent — safe to rerun; only fills gaps, never overwrites data.
"""

import json
import re
import sys

import clouddb
import jobber_client as jc
from techs import tech_for


def _slug(address):
    return re.sub(r"[^a-z0-9]+", "-",
                  (address or "").lower()).strip("-")[:60]


def _email_of(rec):
    m = re.search(r"<([^>]+)>", rec.get("from") or "")
    return m.group(1).lower() if m else None


def _attach_open_quote(rec, email):
    if rec.get("open_quote_ctx") or not email:
        return False
    try:
        oq = jc.find_open_quote(email, scan=80)
    except Exception:
        return False
    if not oq:
        return False
    rec["open_quote_ctx"] = {
        "number": oq["quoteNumber"], "status": oq["quoteStatus"],
        "total": oq["amounts"]["total"],
        "created": (oq.get("createdAt") or "")[:10],
        "url": oq.get("jobberWebUri"),
        "lines": [{"name": li["name"], "price": li.get("totalPrice")}
                  for li in (oq.get("lineItems") or {})
                  .get("nodes", [])][:8]}
    return True


def _port_photos(client_id, rec, stamp):
    photos = jc.client_photos(client_id, limit=8)
    if not photos:
        return 0
    ref = _slug(rec.get("address")) or stamp
    have = {(r, k, i) for r, k, i in clouddb.photos_index([ref])}
    n = 0
    import urllib.request
    from imgprep import prep_jpeg_from_bytes
    for i, (_fn, url) in enumerate(photos):
        if (ref, "jobber", i) in have:
            continue
        try:
            data = urllib.request.urlopen(url, timeout=25).read()
            if len(data) > 4_000_000:
                continue
            clouddb.put_photo(ref, "jobber", i,
                              prep_jpeg_from_bytes(data, 1000, 72))
            n += 1
        except Exception:
            continue
    return n


def run(limit=None):
    if not clouddb.available():
        sys.exit("DATABASE_URL not set — this sweep writes to the cloud")
    rows = clouddb.all_shadow()
    stats = {"seen": 0, "linked": 0, "quotes": 0, "cids": 0,
             "addrs": 0, "photos": 0}
    for stamp, rec in rows:
        if rec.get("merged_into") or rec.get("spam_auto") \
                or rec.get("tech_sender"):
            continue
        email = _email_of(rec)
        if email and (tech_for(email) or "copycall" in email
                      or "getjobber" in email or "noreply" in email
                      or "masterbutlerinc" in email):
            email = None
        if not email and not rec.get("lead") and not rec.get("phone"):
            continue
        stats["seen"] += 1
        changed = False

        # voicemail people first — the caller IS the customer
        cid = rec.get("caller_id")
        if (rec.get("lead") or rec.get("phone")) and not cid:
            try:
                cid = jc.caller_id(rec.get("phone"))
            except Exception:
                cid = None
            if cid:
                rec["caller_id"] = cid
                stats["cids"] += 1
                changed = True
        if cid and not email:
            email = cid.get("email")
        if cid and not rec.get("address") and cid.get("address"):
            rec["address"] = cid["address"]
            stats["addrs"] += 1
            changed = True
        if cid and not rec.get("jobber_client_url") and cid.get("url"):
            rec["jobber_client_url"] = cid["url"]
            changed = True

        # profile link + status
        cs = None
        if email and not rec.get("jobber_client_url"):
            try:
                cs = jc.client_summary(email)
            except Exception:
                cs = None
            if cs and cs.get("url"):
                rec["jobber_client_url"] = cs["url"]
                if not rec.get("customer_status"):
                    rec["customer_status"] = (
                        "new" if not cs["known"] else
                        f"returning ({cs['invoices']} jobs)"
                        if cs["invoices"] else
                        "in Jobber — no completed jobs yet")
                stats["linked"] += 1
                changed = True

        # open quote (Dallon: 'if there is an open quote, link it')
        if _attach_open_quote(rec, email):
            stats["quotes"] += 1
            changed = True

        # address write-back from the draft's pipeline find
        if not rec.get("address"):
            a = ((rec.get("draft") or {}).get("customer") or {}) \
                .get("address")
            if a:
                rec["address"] = a
                stats["addrs"] += 1
                changed = True

        # port their Jobber photos (known clients only)
        client_gid = (cid or {}).get("id") or (cs or {}).get("id")
        if client_gid:
            try:
                n = _port_photos(client_gid, rec, stamp)
                if n:
                    stats["photos"] += n
            except Exception:
                pass

        if changed:
            clouddb.ingest_shadow(stamp, rec)
            print(f"  ✓ {stamp} {(rec.get('from') or '')[:40]}", flush=True)
        if limit and stats["seen"] >= limit:
            break
    print("SWEEP DONE:", json.dumps(stats), flush=True)


if __name__ == "__main__":
    run()
