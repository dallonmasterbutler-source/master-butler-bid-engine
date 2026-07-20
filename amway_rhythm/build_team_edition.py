#!/usr/bin/env python3
"""
Refresh the shareable page's inlined product library.

`team_edition.html` must be fully self-contained (it runs offline, no fetch),
so the product cards are baked into its `const PRODUCTS = [...]` array. The
canonical data lives in `products.json` (also read by the Python app via
store._seed_products). Run this after editing products.json to copy that data
into the HTML:

    python3 build_team_edition.py

It rewrites only the PRODUCTS array; everything else in the page is untouched.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
data = json.loads((HERE / "products.json").read_text())
html_path = HERE / "team_edition.html"
html = html_path.read_text()

block = "const PRODUCTS = " + json.dumps(data, ensure_ascii=False, indent=2) + ";"
new_html, n = re.subn(r"const PRODUCTS = \[.*?\n\];", block, html, count=1, flags=re.S)
if n != 1:
    raise SystemExit("could not find the PRODUCTS array in team_edition.html")

html_path.write_text(new_html)
print(f"Injected {len(data)} products into team_edition.html")
