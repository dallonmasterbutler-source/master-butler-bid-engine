"""
MASTER BUTLER — CLOUD COURIER (runs on Dallon's Mac, stdlib only)

Pushes shadow records and display blobs from this machine up to the
cloud dashboard's /api/ingest endpoint (HTTPS + the office password).

If the internet or the dashboard is down, records queue in
data/pending_cloud/ and the next poll retries them — nothing is lost,
nothing blocks the local pipeline.
"""

import json
import urllib.request
import urllib.error
from base64 import b64encode
from pathlib import Path

BASE = Path(__file__).parent
PENDING = BASE / "data" / "pending_cloud"


def _cfg(name):
    for line in (BASE / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""


def push(records=None, blobs=None, timeout=25):
    """One POST to the cloud. records = [(stamp, record_dict)].
    Raises on failure — callers decide whether to queue."""
    url = _cfg("DASHBOARD_URL")
    pw = _cfg("DASHBOARD_PASSWORD")
    if not url or not pw:
        raise RuntimeError("DASHBOARD_URL / DASHBOARD_PASSWORD not in .env")
    payload = {"records": [{"stamp": s, "record": r}
                           for s, r in (records or [])],
               "blobs": blobs or {}}
    req = urllib.request.Request(
        url.rstrip("/") + "/api/ingest",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic "
                 + b64encode(f"office:{pw}".encode()).decode()})
    resp = json.load(urllib.request.urlopen(req, timeout=timeout))
    if not resp.get("ok"):
        raise RuntimeError(f"ingest rejected: {resp}")
    return resp.get("count", 0)


def push_or_queue(stamp, record):
    """Best-effort single-record push; queue locally on any failure."""
    try:
        push(records=[(stamp, record)])
        return True
    except Exception:
        PENDING.mkdir(parents=True, exist_ok=True)
        (PENDING / f"{stamp}.json").write_text(json.dumps(record))
        return False


def flush_pending():
    """Retry everything that queued while offline. Returns count sent."""
    if not PENDING.exists():
        return 0
    sent = 0
    for p in sorted(PENDING.glob("*.json")):
        try:
            push(records=[(p.stem, json.loads(p.read_text()))])
            p.unlink()
            sent += 1
        except Exception:
            break            # still unreachable — try again next time
    return sent
