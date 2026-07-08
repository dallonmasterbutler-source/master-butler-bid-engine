"""
MASTER BUTLER — IMAGE PREP (works on the Mac AND in the cloud)

One job: any image file → resized JPEG bytes. On macOS it uses the
built-in `sips` (no installs, as always). On Linux/Render it falls back
to Pillow (cloud-only dependency, listed in requirements.txt).
"""

import base64
import shutil
import subprocess
from pathlib import Path


def prep_jpeg_bytes(path, max_px=1400, quality=78):
    """Image file → resized/compressed JPEG bytes."""
    path = Path(path)
    if shutil.which("sips"):                      # macOS
        tmp = Path("/tmp/imgprep") / (path.stem + ".jpg")
        tmp.parent.mkdir(exist_ok=True)
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(max_px),
                        "-s", "formatOptions", str(quality), str(path),
                        "--out", str(tmp)], check=True, capture_output=True)
        return tmp.read_bytes()
    from PIL import Image                          # Linux / Render
    import io
    im = Image.open(path)
    im.thumbnail((max_px, max_px))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def prep_jpeg_b64(path, max_px=1400, quality=78):
    return base64.standard_b64encode(
        prep_jpeg_bytes(path, max_px, quality)).decode()
