"""
MASTER BUTLER — TECH ROSTER (Jessica's call, Jul 9: "sometimes we send
an email over to a tech, like a call back for a mistake or something,
those need to be also tagged and labeled appropriately. We dont want
to bid them.")

Mail FROM a tech is field traffic — a callback, a schedule note, a
question about a job. It stays VISIBLE on the inbox (it's usually
about a real customer) but is never priced, never counted as a lead,
never spam-filtered, and never touches the scoreboard.

Roster source: Dallon's Hub manager tab (butlerinventory app), Jul 9
2026. Tom (tomfricke2007) is deliberately NOT here — his mail already
routes to the internal drawer per Dallon's Jul 9 ruling and a tech tag
would pull it back onto the inbox. Blob 'tech_senders' {email: name}
adds techs without a deploy.
"""

TECH_ROSTER = {
    "mcadam2020@gmail.com": "Adam McBride",
    "austin.dayton18@gmail.com": "Austin Dayton",
    "woodcaleb817@gmail.com": "Caleb Wood",
    "conneranderson11@outlook.com": "Connor Anderson",
    "gavin.ojalehto@gmail.com": "Gavin Ojalehto",
    "markjacobproudlock@gmail.com": "MarkJacob Proudlock",
    "nickbush41@yahoo.com": "Nicholas Harms Bush",
    "nickkarvonen@hotmail.com": "Nick Karvonen",
    "stransha@gmail.com": "Shane Strand",
}


def roster():
    out = {k.lower(): v for k, v in TECH_ROSTER.items()}
    try:
        import clouddb
        if clouddb.available():
            out.update({k.lower(): v for k, v in
                        (clouddb.get_blob("tech_senders") or {}).items()})
    except Exception:
        pass
    return out


def tech_for(sender):
    """Tech's name when the sender is on the roster, else None."""
    s = (sender or "").lower()
    for email, name in roster().items():
        if email and email in s:
            return name
    return None
