"""
READ-ONLY Jobber sweep — finds the damage from the create-property bug
(LaRee, Jul 17): for existing customers, every approved quote was creating
a DUPLICATE "home" instead of reusing the real one, so the quote hung off
the duplicate (reads as the billing address) and missed the tax the real
home carries.

This makes NO changes. It walks recent quotes, then for each client checks
whether they have two-or-more properties that are the SAME address — those
are our duplicates. The quote sitting on the duplicate is the one to move
back onto the real home (which fixes the tax too). Output → console + the
`jobber_dupe_sweep` blob so the office can see the list on the dashboard.

Run:  python3 jobber_dupe_sweep.py [max_quotes]
"""
import sys
import jobber_client as jc

SWEEP_QUOTES = """
query Sweep($first: Int!, $after: String) {
  quotes(first: $first, after: $after,
         sort: {key: CREATED_AT, direction: DESCENDING}) {
    pageInfo { hasNextPage endCursor }
    nodes { quoteNumber quoteStatus createdAt jobberWebUri
            amounts { total }
            client { id name }
            property { address { street } } }
  }
}
"""


def _quotes(cap):
    out, after = [], None
    while len(out) < cap:
        data = jc._post(SWEEP_QUOTES, {"first": min(75, cap - len(out)),
                                       "after": after}, "sweep quotes")
        q = data.get("quotes") or {}
        out += q.get("nodes") or []
        pi = q.get("pageInfo") or {}
        after = pi.get("endCursor")
        if not pi.get("hasNextPage") or not after:
            break
    return out


def run(cap=200, verbose=True):
    quotes = _quotes(cap)
    # unique clients seen on recent quotes, with the quotes each one carries
    by_client = {}
    for qn in quotes:
        cl = qn.get("client") or {}
        cid = cl.get("id")
        if not cid:
            continue
        by_client.setdefault(cid, {"name": cl.get("name"), "quotes": []})
        by_client[cid]["quotes"].append({
            "num": qn.get("quoteNumber"),
            "status": qn.get("quoteStatus"),
            "total": (qn.get("amounts") or {}).get("total"),
            "addr": ((qn.get("property") or {}).get("address")
                     or {}).get("street"),
            "uri": qn.get("jobberWebUri")})

    flagged = []
    for cid, info in by_client.items():
        data = jc._post(jc._CLIENT_PROPERTIES, {"id": cid}, "list properties")
        props = (data.get("client") or {}).get("properties") or []
        # group the client's properties by same-address
        groups = []
        for p in props:
            st = (p.get("address") or {}).get("street1") or ""
            for g in groups:
                if jc._same_property(g["addr"], st):
                    g["ids"].append(p["id"])
                    break
            else:
                groups.append({"addr": st, "ids": [p["id"]]})
        dups = [g for g in groups if len(g["ids"]) > 1]
        if dups:
            flagged.append({
                "client": info["name"], "client_id": cid,
                "duplicate_homes": [{"address": g["addr"],
                                     "count": len(g["ids"])} for g in dups],
                "recent_quotes": info["quotes"][:6]})

    report = {
        "quotes_scanned": len(quotes),
        "clients_checked": len(by_client),
        "clients_with_duplicate_homes": len(flagged),
        "flagged": sorted(flagged,
                          key=lambda f: -sum(h["count"]
                                             for h in f["duplicate_homes"])),
    }
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob("jobber_dupe_sweep", report)
    except Exception:
        pass

    if verbose:
        print(f"scanned {report['quotes_scanned']} recent quotes across "
              f"{report['clients_checked']} clients")
        print(f"clients with duplicate homes: "
              f"{report['clients_with_duplicate_homes']}\n")
        for f in report["flagged"]:
            print(f"• {f['client']}")
            for h in f["duplicate_homes"]:
                print(f"    ⚠ {h['count']}× {h['address']}")
            for q in f["recent_quotes"]:
                t = q.get("total")
                print(f"      quote #{q['num']} [{q['status']}] "
                      f"${t or 0} — {q.get('addr')}")
    return report


if __name__ == "__main__":
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    run(cap)
