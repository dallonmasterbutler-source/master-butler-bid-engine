#!/usr/bin/env python3
"""
Build both phone-app editions from one source.

Source of truth: personal_edition.html (the FULL app, including the Plan tab).
Plan-only code is wrapped in markers:
    /* PLAN:START */ ... /* PLAN:END */      (inside <style> and <script>)
    <!-- PLAN:START --> ... <!-- PLAN:END --> (in HTML)

This script:
  1. Injects products.json into personal_edition.html's PRODUCTS array.
  2. Writes team_edition.html = personal with all PLAN:* regions stripped
     (the share build: training + Core + Run + Study + Progress, no calendar).

Run after editing products.json or the source:
    python3 build_editions.py
"""
import json, re
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "personal_edition.html"
data = json.loads((HERE / "products.json").read_text())

full = SRC.read_text()

# 1) inject products into the source (personal) edition
block = "const PRODUCTS = " + json.dumps(data, ensure_ascii=False, indent=2) + ";"
full, n = re.subn(r"const PRODUCTS = \[.*?\n\];", block, full, count=1, flags=re.S)
if n != 1:
    raise SystemExit("could not find PRODUCTS array in personal_edition.html")
SRC.write_text(full)

# 2) generate the share (team) edition with Plan stripped
share = re.sub(r"/\* PLAN:START \*/.*?/\* PLAN:END \*/", "", full, flags=re.S)
share = re.sub(r"<!-- PLAN:START -->.*?<!-- PLAN:END -->", "", share, flags=re.S)
(HERE / "team_edition.html").write_text(share)

print(f"Injected {len(data)} products.")
print(f"personal_edition.html: full app (with Plan tab).")
print(f"team_edition.html: share build, Plan stripped (has view-plan: {'view-plan' in share}).")
