"""
MASTER BUTLER — HOLIDAY LIGHTS MATERIALS ESTIMATOR

Lights LABOR is Tom's call (custom, $385 minimum) — that rule does not
change here. What THIS does: pre-measure the roofline from the aerial
so Tom's escalation form arrives with the materials math already done.

  2026 policy: C7 bulbs only, $1.45/ft materials.
  Discounts (Sept 15%, Oct 10%, Dec 10%) apply to LABOR only — never
  materials — so they don't touch this estimate.

Run:  python3 lights.py <address>
"""

from pathlib import Path

C7_PER_FT = 1.45          # 2026 materials rate
LABOR_MINIMUM = 385       # Tom's floor — labor itself is Tom's number


def materials_estimate(front_ft_low, front_ft_high,
                       perimeter_low=None, perimeter_high=None):
    """Roofline feet → materials dollars. Front-only is the common ask;
    full perimeter shown when measured, for the 'wrap the house' upsell."""
    est = {
        "front_ft": (front_ft_low, front_ft_high),
        "front_materials": (round(front_ft_low * C7_PER_FT, 2),
                            round(front_ft_high * C7_PER_FT, 2)),
        "labor": f"Tom's call (minimum ${LABOR_MINIMUM})",
    }
    if perimeter_low and perimeter_high:
        est["perimeter_ft"] = (perimeter_low, perimeter_high)
        est["perimeter_materials"] = (round(perimeter_low * C7_PER_FT, 2),
                                      round(perimeter_high * C7_PER_FT, 2))
    return est


def estimate_for(address):
    """Aerial-measured lights materials for one property.
    Returns (estimate_dict or None, note)."""
    from aerial import fetch_tile, analyze_aerial
    tile = fetch_tile(address)
    reading, cost = analyze_aerial(tile)
    r = reading.get("roofline") or {}
    if not r.get("front_ft_low") or r.get("confidence") == "low":
        return None, ("Roofline not measurable from aerial — Tom measures "
                      "on site as usual.")
    est = materials_estimate(r["front_ft_low"], r["front_ft_high"],
                             r.get("full_perimeter_ft_low"),
                             r.get("full_perimeter_ft_high"))
    lo, hi = est["front_materials"]
    note = (f"Lights pre-measure (aerial): front roofline "
            f"~{r['front_ft_low']}-{r['front_ft_high']} ft → C7 materials "
            f"${lo:.0f}-${hi:.0f}. Labor = Tom (min ${LABOR_MINIMUM}). "
            f"{r.get('detail', '')}")
    return est, note


if __name__ == "__main__":
    import sys
    addr = " ".join(sys.argv[1:])
    if not addr:
        raise SystemExit("usage: python3 lights.py <address>")
    est, note = estimate_for(addr)
    print(note)
    if est:
        for k, v in est.items():
            print(f"  {k}: {v}")
