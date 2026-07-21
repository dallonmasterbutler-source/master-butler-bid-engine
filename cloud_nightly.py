"""
MASTER BUTLER — THE CLOUD NIGHTLY (Dallon, Jul 16: "cloud-ify the
automation… anything dependent on my mac we should automate too").

The office-essential nightly refreshes, run FROM RENDER so they no
longer depend on Dallon's laptop being awake. Fired once a day by a
daily-marker guard inside render_start's background thread.

Deliberately LIGHT — only the steps the office sees each morning, each
reading/writing the cloud DB (not local files):
  · service-history refresh (recent invoices) → keeps last-paid, yoy,
    win-back current
  · yoy_july (Tom's race) · pw win-back list

NOT here (heavy / seasonal — stay on the Mac night run, or a future
Render cron): the full 3.5-yr route mine, the Vision lights deep-mine,
the one-time full-history rebuild, the DB backup download. Those going
a day stale when the Mac is off is harmless; mail and the office
numbers are what must never depend on one machine.
"""

from datetime import datetime, timezone


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def already_ran_today():
    try:
        import clouddb
        return (clouddb.get_blob("cloud_nightly_last") or {}).get("day") \
            == _today()
    except Exception:
        return False


def run(verbose=True):
    """The light nightly. Each step isolated — one failure never stops
    the rest. Returns a short status dict."""
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "steps": {}}

    def step(name, fn):
        try:
            fn()
            out["steps"][name] = "ok"
        except Exception as e:
            out["steps"][name] = f"skip: {type(e).__name__}: {e}"[:120]
        if verbose:
            print(f"  [cloud-nightly] {name}: {out['steps'][name]}")

    # 1 · refresh the invoice archive (recent) — feeds everything below
    def _hist():
        import servicehistory
        servicehistory.refresh(recent=120)
    step("service_history", _hist)

    # 2 · Tom's July race
    def _yoy():
        import yoy_compare
        yoy_compare.run_local(verbose=False)
    step("yoy", _yoy)

    # 3 · PW win-back list
    def _wb():
        import winback
        winback.save()
    step("winback", _wb)

    # 4 · inbox reconcile — recover anything the 2-day poll window missed
    #     (#49: a Squarespace lead that landed in neither dashboard nor
    #     the office's working view). Idempotent; usually a no-op.
    def _recon():
        import gmail_poller
        n = gmail_poller.reconcile_inbox(verbose=False)
        return f"recovered {n}" if n else "0 missed"
    step("inbox_reconcile", _recon)

    try:
        import clouddb
        clouddb.put_blob("cloud_nightly_last",
                         {"day": _today(), "ran": out})
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=1))
