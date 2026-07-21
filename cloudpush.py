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
    """Config value: .env file on the Mac, os.environ on the cloud.
    (Jul 21 audit: this read the .env FILE unconditionally, so every
    night_run section that used it — review pull, QA self-check, backup,
    cloud mirror — crashed on the cron with FileNotFoundError.)"""
    import os
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def push(records=None, blobs=None, photos=None, timeout=60):
    """One POST to the cloud. records = [(stamp, record_dict)];
    photos = [{"ref","kind","idx","b64"}]. Raises on failure —
    callers decide whether to queue."""
    import os
    if os.environ.get("MB_SANDBOX"):
        # the acceptance trials run a file-mode server that clicks real
        # buttons — its writes must NEVER reach production (Jul 10 pm:
        # every trials run was overwriting the office's learned-spam
        # list and read marks with test data through this exact path)
        raise RuntimeError("MB_SANDBOX set — cloud push refused")
    url = _cfg("DASHBOARD_URL")
    pw = _cfg("DASHBOARD_PASSWORD")
    if not url or not pw:
        raise RuntimeError("DASHBOARD_URL / DASHBOARD_PASSWORD not in .env")
    payload = {"records": [{"stamp": s, "record": r}
                           for s, r in (records or [])],
               "blobs": blobs or {},
               "photos": photos or []}
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


def _shrink_jpeg(path, max_px=900, quality=70):
    """Resize any image to a small JPEG (~100 KB) and return base64 —
    small enough that the free database holds hundreds. Mac + cloud."""
    from imgprep import prep_jpeg_b64
    return prep_jpeg_b64(path, max_px=max_px, quality=quality)


def _addr_slug(address):
    import re
    return re.sub(r"[^a-z0-9]+", "-", (address or "").lower()).strip("-")[:60]


def gather_photos(stamp, record):
    """Everything visual for one bid: customer photos from the saved
    .eml plus any cached aerial/street tiles for the address."""
    photos = []
    eml = BASE / "data" / "shadow_bids" / f"{stamp}.eml"
    if eml.exists():
        try:
            from pipeline import extract_photos
            for i, p in enumerate(extract_photos(eml)):
                photos.append({"ref": stamp, "kind": "customer", "idx": i,
                               "b64": _shrink_jpeg(p)})
        except Exception:
            pass
    slug = _addr_slug(record.get("address"))
    if slug:
        aerial_dir = BASE / "data" / "aerial"
        if aerial_dir.exists():
            for p in aerial_dir.iterdir():
                if not p.name.startswith(slug[:24]):
                    continue
                try:
                    if p.suffix == ".png":
                        photos.append({"ref": slug, "kind": "aerial",
                                       "idx": 0, "b64": _shrink_jpeg(p)})
                    elif p.name.endswith("-street.jpg"):
                        photos.append({"ref": slug, "kind": "street",
                                       "idx": 0, "b64": _shrink_jpeg(p)})
                except Exception:
                    continue
    return photos


def push_or_queue(stamp, record):
    """Best-effort single-record push (with its photos); queue locally
    on any failure."""
    try:
        push(records=[(stamp, record)], photos=gather_photos(stamp, record))
        return True
    except Exception:
        PENDING.mkdir(parents=True, exist_ok=True)
        (PENDING / f"{stamp}.json").write_text(json.dumps(record))
        return False


def pull_reviews():
    """Download every office decision from the cloud (JSON list). The
    learning loop on this Mac feeds on these — LaRee's reason taps in
    the cloud become adjust_reason rows in the local store."""
    url = _cfg("DASHBOARD_URL")
    pw = _cfg("DASHBOARD_PASSWORD")
    if not url or not pw:
        return []
    req = urllib.request.Request(
        url.rstrip("/") + "/api/reviews",
        headers={"Authorization": "Basic "
                 + b64encode(f"office:{pw}".encode()).decode()})
    return json.load(urllib.request.urlopen(req, timeout=30))


def flush_pending():
    """Retry everything that queued while offline. Returns count sent."""
    if not PENDING.exists():
        return 0
    sent = 0
    for p in sorted(PENDING.glob("*.json")):
        try:
            rec = json.loads(p.read_text())
            push(records=[(p.stem, rec)], photos=gather_photos(p.stem, rec))
            p.unlink()
            sent += 1
        except Exception:
            break            # still unreachable — try again next time
    return sent
