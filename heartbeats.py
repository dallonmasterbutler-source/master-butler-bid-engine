"""
COLLECTOR HEARTBEATS (Dallon, Jul 23: "can we make sure all the things
are in order so that things are caught in the way it should" — after
the scorecard ran green for a week while capturing nothing, same class
as the learning store dying silently Jul 16-22).

Every data-collecting subsystem calls beat(name, **facts) when it does
real work. Beats land atomically in the `heartbeats` blob; the nightly
pipeline check asserts each collector's CADENCE (it ran recently) and
its PULSE (it's actually accumulating, not just running). A collector
that runs but collects nothing now trips an alarm instead of hiding
for a week. No-op off-cloud and in MB_SANDBOX.
"""
from datetime import datetime, timezone


def beat(name, **facts):
    try:
        import clouddb
        if not clouddb.available():
            return
        clouddb.merge_blob("heartbeats", {name: {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **facts}})
    except Exception:
        pass                      # a heartbeat must never break the host
