"""
MASTER BUTLER — VOICEMAIL PEOPLE GET THE FULL CARD (Dallon, Jul 10:
Terry Brower transcribed but was 'just an empty shell with a name and
the voicemail. It needs everything else too.')

The Kevin Pham standing principle, applied to CALLERS: anyone the
system recognizes must arrive wearing their history. A transcribed
voicemail from a known client gets, automatically:
  · Jobber profile link + returning-customer status (from caller-ID)
  · address written back from their Jobber file
  · a PRICED shadow draft when the transcript names services
  · their Jobber photos ported to the gallery

Called from gmail_poller (at transcription) and complete_sweep (the
hourly self-heal retry). Read-only against Jobber; caller persists.
"""


def enrich(rec, stamp=""):
    """Mutates a voicemail record toward a complete card. Returns True
    when anything changed."""
    import jobber_client as jc
    changed = False

    cid = rec.get("caller_id") or {}
    if not cid and rec.get("phone"):
        try:
            cid = jc.caller_id(rec["phone"]) or {}
        except Exception:
            cid = {}
        if cid:
            rec["caller_id"] = cid
            changed = True

    if cid.get("url") and not rec.get("jobber_client_url"):
        rec["jobber_client_url"] = cid["url"]
        changed = True
    if cid and not rec.get("customer_status"):
        inv = cid.get("invoices") or 0
        rec["customer_status"] = (f"returning ({inv} jobs)" if inv
                                  else "in Jobber — no completed jobs yet")
        changed = True
    if cid.get("address") and not rec.get("address"):
        rec["address"] = cid["address"]
        changed = True

    # open-quote context (works off their email from caller-ID)
    if cid.get("email") and not rec.get("open_quote_ctx"):
        try:
            oq = jc.find_open_quote(cid["email"])
        except Exception:
            oq = None
        if oq:
            rec["open_quote_ctx"] = {
                "number": oq["quoteNumber"], "status": oq["quoteStatus"],
                "total": oq["amounts"]["total"],
                "created": (oq.get("createdAt") or "")[:10],
                "url": oq.get("jobberWebUri"),
                "lines": [{"name": li["name"], "price": li.get("totalPrice")}
                          for li in (oq.get("lineItems") or {})
                          .get("nodes", [])][:8]}
            changed = True

    # PRICED DRAFT from the transcript's services at their address
    if rec.get("services") and rec.get("address") \
            and not (((rec.get("draft") or {}).get("bid") or {})
                     .get("services")):
        try:
            import shadow_from_quote
            customer = {"name": cid.get("name")
                        or (rec.get("from") or "").split("<")[0].strip(),
                        "email": cid.get("email") or "",
                        "phone": rec.get("phone") or "",
                        "address": rec.get("address")}
            draft = shadow_from_quote.build(
                rec["address"], rec["services"], customer,
                source="Priced from their voicemail — the transcript "
                       "named the services; verify scope when replying.")
            if draft:
                rec["draft"] = draft
                changed = True
        except Exception:
            pass

    # their Jobber photos into the gallery (known clients only)
    if cid.get("id"):
        try:
            import clouddb
            if clouddb.available():
                from enrich_sweep import _port_photos
                if _port_photos(cid["id"], rec, stamp or ""):
                    changed = True
        except Exception:
            pass

    return changed
