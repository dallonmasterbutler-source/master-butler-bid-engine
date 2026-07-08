"""
MASTER BUTLER — DO-NOT-SERVICE SWEEP (Dallon, Jul 8)

Some Jobber clients are marked "do not service" — in a client note, a
tag, or the office's convention of prefixing the NAME (e.g. "***Name").
Some of those people try again with a NEW EMAIL — so the blocklist
keeps every identifier we can match on later: emails, phone digits,
property addresses (canonical form), and name.

Read-only sweep -> data/dns_list.json + cloud blob 'dns_list'.
"""

import json
import re
import time
from pathlib import Path

import jobber_client as jc

MARKERS = ("do not service", "do not schedule", "dns", "do not book",
           "no service", "banned", "blacklist", "do not work")

Q = """query Sweep($first: Int!, $after: String) {
  clients(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { name isArchived
            emails { address }
            phones { number }
            properties { address { street city } }
            tags(first: 10) { nodes { label } }
            notes(first: 8) { nodes { ... on ClientNote { message } } } }
  }
}"""


def _canon(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def is_dns(node):
    """Return the matching marker text, or None.

    Asterisked names ARE the office's do-not-service convention —
    verified empirically Jul 8: every sampled *-name client has ZERO
    invoices and ZERO quotes (a record kept only as a warning)."""
    name = (node.get("name") or "").lower()
    if name.startswith("*") or name.startswith("xx"):
        return f"name marker: {node['name'][:40]}"
    for t in (node.get("tags") or {}).get("nodes", []):
        lbl = (t.get("label") or "").lower()
        if any(m in lbl for m in MARKERS):
            return f"tag: {t['label'][:60]}"
    for nt in (node.get("notes") or {}).get("nodes", []):
        msg = (nt.get("message") or "").lower()
        if any(m in msg for m in MARKERS):
            return f"note: {nt.get('message', '')[:120]}"
    if any(m in name for m in ("do not service", "do not schedule")):
        return f"name: {node['name'][:40]}"
    return None


SVC_WORDS = {"gutter": "gutter", "roof": "roof", "moss": "moss",
             "window": "window", "pressure": "pressure", "pw": "pressure",
             "house wash": "house wash", "driveway": "pressure"}

PRICE_PAT = re.compile(
    r"(?:raise|increase|minimum|min\.?|at least|no less than|charge|"
    r"price to|should be)[^.\n$]{0,40}\$\s?(\d{2,4})", re.I)


def price_notes(node):
    """Tech field notes like 'Raise gutter price to at least $400' ->
    per-client service minimums the engine applies automatically."""
    found = []
    for nt in (node.get("notes") or {}).get("nodes", []):
        msg = nt.get("message") or ""
        for m in PRICE_PAT.finditer(msg):
            amt = int(m.group(1))
            if not 50 <= amt <= 3000:
                continue
            ctx = msg[max(0, m.start()-60):m.end()+20].lower()
            svc = next((v for k, v in SVC_WORDS.items() if k in ctx), "any")
            found.append({"service": svc, "min": amt,
                          "note": msg.strip()[:160]})
    if not found:
        return None
    return {"name": node["name"],
            "emails": [e["address"].lower()
                       for e in node.get("emails") or [] if e.get("address")],
            "phones": ["".join(ch for ch in p["number"] if ch.isdigit())[-10:]
                       for p in node.get("phones") or [] if p.get("number")],
            "addresses": [_canon(f"{a.get('street','')} {a.get('city','')}")
                          for a in [pr.get("address") or {}
                                    for pr in node.get("properties") or []]
                          if a.get("street")],
            "minimums": found}


def sweep(limit=100000):
    jc.DRY_RUN = False
    out, mins, scanned, cursor = [], [], 0, None
    while scanned < limit:
        data = None
        for attempt in range(8):
            try:
                data = jc._post(Q, {"first": 20, "after": cursor}, "dns sweep")
            except Exception as e:
                print(f"  retry {attempt+1} after {type(e).__name__}")
                time.sleep(10 * (attempt + 1))
                continue
            if data.get("error") and "THROTTLED" in str(
                    data.get("body", "")).upper():
                time.sleep(15 * (attempt + 1))
                continue
            break
        if data is None or data.get("error"):
            print(f"stopping at {scanned}: {str(data)[:150]}")
            break
        block = data["clients"]
        for n in block["nodes"]:
            scanned += 1
            pn = price_notes(n)
            if pn:
                mins.append(pn)
                print(f"  💲 {n['name'][:34]}: "
                      + "; ".join(f"{m['service']} ≥ ${m['min']}"
                                  for m in pn["minimums"]))
            why = is_dns(n)
            if not why:
                continue
            out.append({
                "name": n["name"],
                "why": why,
                "archived": n.get("isArchived", False),
                "emails": [e["address"].lower()
                           for e in n.get("emails") or [] if e.get("address")],
                "phones": ["".join(ch for ch in p["number"] if ch.isdigit())[-10:]
                           for p in n.get("phones") or [] if p.get("number")],
                "addresses": [_canon(f"{a['address'].get('street','')} "
                                     f"{a['address'].get('city','')}")
                              for a in [{"address": p.get("address") or {}}
                                        for p in n.get("properties") or []]
                              if a["address"].get("street")]})
            print(f"  ⛔ {n['name'][:36]} ({why[:60]})")
        if scanned % 500 < 20:
            print(f"  ...{scanned} clients, {len(out)} flagged")
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
        time.sleep(1.5)

    Path("data/dns_list.json").write_text(json.dumps(out, indent=1))
    Path("data/client_minimums.json").write_text(json.dumps(mins, indent=1))
    print(f"\nscanned {scanned} clients -> {len(out)} DO-NOT-SERVICE, "
          f"{len(mins)} clients with tech price-notes")
    try:
        from cloudpush import push
        push(blobs={"dns_list": out, "client_minimums": mins})
        print("mirrored to cloud")
    except Exception as e:
        print(f"(cloud mirror skipped: {e})")
    return out, mins


if __name__ == "__main__":
    sweep()
