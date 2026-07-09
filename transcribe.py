"""
MASTER BUTLER — VOICEMAIL TRANSCRIPTION (Dallon, Jul 8: "the message
comes in and says you have a message — that can't be what goes on the
dashboard. we have to extract the info from the audio.")

Turns an audio attachment (.wav / .mp3 — what CopyCall sends once
attachments are enabled on the account) into TEXT via Google
Speech-to-Text, using the same Google project as everything else.

ONE-CLICK PREREQUISITE (Dallon): enable the API at
https://console.developers.google.com/apis/api/speech.googleapis.com/overview?project=815253616550
Free tier = 60 minutes/month; our voicemail volume (~2/month) never
leaves free.

Voicemails are short (<60s) so the synchronous recognize endpoint is
enough. Returns "" (never raises) when transcription isn't possible —
the caller falls back to the plain notification text.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent


def _key():
    if (BASE / ".env").exists():
        for line in (BASE / ".env").read_text().splitlines():
            if line.startswith("GOOGLE_MAPS_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("GOOGLE_MAPS_API_KEY", "")


def _recognize(payload_cfg, content_b64, key):
    body = {"config": payload_cfg,
            "audio": {"content": content_b64}}
    req = urllib.request.Request(
        "https://speech.googleapis.com/v1p1beta1/speech:recognize?key=" + key,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    return " ".join(
        alt["transcript"].strip()
        for res in r.get("results", [])
        for alt in res.get("alternatives", [])[:1]).strip()


def _wav_rate(b):
    """Sample rate from a PCM WAV header, or None if not a plain WAV."""
    try:
        if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
            import struct
            fmt = b.find(b"fmt ")
            if fmt > 0:
                audio_format, _ch, rate = struct.unpack(
                    "<HHI", b[fmt + 8:fmt + 16])
                if audio_format == 1:            # PCM
                    return rate
    except Exception:
        pass
    return None


def _wav_format(b):
    """(format_code, rate) from a RIFF header, or (None, None)."""
    try:
        if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
            import struct
            fmt = b.find(b"fmt ")
            if fmt > 0:
                code, _ch, rate = struct.unpack("<HHI", b[fmt + 8:fmt + 16])
                return code, rate
    except Exception:
        pass
    return None, None


def _to_pcm(audio_bytes):
    """Decode a non-PCM WAV (CopyCall records GSM 6.10 — Jul 9 finding)
    to raw 16-bit PCM. Cloud: soundfile/libsndfile. Mac: afconvert.
    Returns (pcm_bytes, rate) or (None, None)."""
    try:                                    # Render (pip: soundfile)
        import io
        import soundfile as sf
        data, rate = sf.read(io.BytesIO(audio_bytes), dtype="int16")
        return data.tobytes(), int(rate)
    except Exception:
        pass
    try:                                    # macOS built-in converter
        import subprocess
        import tempfile
        import wave
        with tempfile.TemporaryDirectory() as td:
            src_p = td + "/in.wav"
            dst_p = td + "/out.wav"
            open(src_p, "wb").write(audio_bytes)
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16",
                            src_p, dst_p], check=True, capture_output=True,
                           timeout=60)
            with wave.open(dst_p, "rb") as w:
                return (w.readframes(w.getnframes()), w.getframerate())
    except Exception:
        pass
    return None, None


def transcribe(audio_bytes, filename=""):
    """Audio bytes -> transcript text, or '' if not possible."""
    key = _key()
    if not key or not audio_bytes or len(audio_bytes) > 9_000_000:
        return ""
    content = base64.b64encode(audio_bytes).decode()
    name = (filename or "").lower()
    base_cfg = {"languageCode": "en-US",
                "enableAutomaticPunctuation": True,
                "model": "phone_call",
                "useEnhanced": True}
    attempts = []
    if name.endswith(".mp3"):
        attempts = [dict(base_cfg, encoding="MP3", sampleRateHertz=8000),
                    dict(base_cfg, encoding="MP3", sampleRateHertz=44100)]
    else:
        # the beta API does NOT read WAV headers itself ("bad encoding") —
        # parse the header and say it explicitly (verified Jul 8: header-
        # less config 400s; LINEAR16 @ parsed rate transcribes perfectly)
        code, wrate = _wav_format(audio_bytes)
        if code == 1 and wrate:                    # plain PCM
            attempts.append(dict(base_cfg, encoding="LINEAR16",
                                 sampleRateHertz=wrate))
        elif code not in (None, 1):
            # compressed telephone WAV (CopyCall = GSM 6.10): decode
            # to raw PCM first — Google doesn't speak GSM
            pcm, prate = _to_pcm(audio_bytes)
            if pcm:
                content = base64.b64encode(pcm).decode()
                attempts.append(dict(base_cfg, encoding="LINEAR16",
                                     sampleRateHertz=prate or 8000))
        attempts += [dict(base_cfg, encoding="MULAW", sampleRateHertz=8000),
                     dict(base_cfg, encoding="LINEAR16",
                          sampleRateHertz=8000),
                     dict(base_cfg)]        # FLAC/OGG carry their own info
    for cfg in attempts:
        try:
            text = _recognize(cfg, content, key)
            if text:
                return text
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:200]
            if e.code == 403:                 # API not enabled yet
                print(f"  (transcription needs the one-click enable: {err[:90]})")
                return ""
            continue
        except Exception:
            continue
    return ""


def extract_audio(msg):
    """(filename, bytes) for the first audio attachment in an email."""
    for part in msg.walk():
        ct = part.get_content_type()
        fn = (part.get_filename() or "").lower()
        if ct.startswith("audio/") or fn.endswith(
                (".wav", ".mp3", ".m4a", ".flac", ".ogg")):
            try:
                return part.get_filename() or "voicemail.wav", \
                    part.get_payload(decode=True)
            except Exception:
                continue
    return None, None
