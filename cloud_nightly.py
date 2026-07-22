"""
MASTER BUTLER — THE CLOUD NIGHTLY (Dallon, Jul 16: "cloud-ify the
automation… anything dependent on my mac we should automate too").

The office-essential nightly refreshes, run FROM RENDER so they no
longer depend on Dallon's laptop being awake. Fired once a day by a
daily-marker guard inside render_start's background thread.

Deliberately LIGHT — and since Jul 22 only the steps the RENDER CRON
does NOT already run (Dallon's ruling: "clean up the dual-nightly
overlap"). The cron fires night_run.py at 9:00pm with service-history
refresh, yoy, and win-back; this thread duplicated all three in the
same hour — wasted Jobber calls, and the overlap was the last source
of the archive-truncation race the Jul 21 night sweep closed. What
stays here is what the cron doesn't do:
  · inbox reconcile — recover anything the 2-day poll window missed
  · mirror sweep — the nightly Gmail+Jobber reconciliation pass

(History/yoy/win-back live in night_run.py on the cron. If that cron
is ever retired, those steps must move back here — the office numbers
must never depend on one machine.)
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

    # (service_history / yoy / winback moved OUT Jul 22 — the 9:00pm
    #  Render cron's night_run.py owns them; running them twice in the
    #  same hour was pure overlap. See module docstring.)

    # 1 · inbox reconcile — recover anything the 2-day poll window missed
    #     (#49: a Squarespace lead that landed in neither dashboard nor
    #     the office's working view). Idempotent; usually a no-op.
    def _recon():
        import gmail_poller
        n = gmail_poller.reconcile_inbox(verbose=False)
        return f"recovered {n}" if n else "0 missed"
    step("inbox_reconcile", _recon)

    # 2 · mirror sweep — file everything verifiably done in BOTH Jobber
    #     and Gmail (Dallon, Jul 21: "systematically taking away")
    def _msweep():
        import mirror_sweep
        mirror_sweep.sweep(verbose=False)
    step("mirror_sweep", _msweep)

    # only stamp the day DONE if something actually succeeded — a
    # fully-down night (Jobber+Gmail both unreachable) used to mark
    # itself complete and never retry (Jul 21 night sweep note)
    if any(v == "ok" for v in out["steps"].values()):
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
