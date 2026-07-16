"""
MASTER BUTLER — PRESSURE-WASHING WIN-BACK LIST
(Dallon's pick off the running list, Jul 15 — the day-matched race
showed PW down $4,790 this July and April down ~$50k. This is the
call-back list: everyone who bought pressure washing before and
hasn't this year.)

Built from data/service_history.json (local, throttle-immune, line
entries to 2017, refreshed nightly). No Jobber calls, no writes.

Tiers (best call first):
  A · REPEATERS GONE QUIET — PW in 2+ recent years, none this year.
      Proven habit, broken. The $4,790 lives here.
  B · LAST YEAR ONLY — PW last year, nothing this year.
  C · STILL A CUSTOMER — bought OTHER work this year but skipped the
      PW they used to buy (easy add-on ask, truck already visits).
      (A property can be A+C or B+C; C is shown as a flag, not a tier.)
"""

import json
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent
PW = ("driveway", "patio", "sidewalk", "house wash")


def build(today=None):
    today = today or date.today()
    yr = today.year
    from servicehistory import load_history   # file on Mac, blob on cloud
    d = load_history()
    rows = []
    for who, svcs in (d.get("by_client") or {}).items():
        pw_years, pw_last, pw_total_last = set(), None, 0.0
        for s in PW:
            for dt, amt in svcs.get(s) or []:
                if not (dt and dt[:4].isdigit()):
                    continue              # undated invoice lines exist
                y = int(dt[:4])
                pw_years.add(y)
                if not pw_last or dt > pw_last[0]:
                    pw_last = (dt, s)
        if not pw_last or yr in pw_years:
            continue                      # never bought PW, or already back
        if max(pw_years) < yr - 2:
            continue                      # cold >2yrs — not a win-back call
        # what their last PW visit was worth (all PW lines that day)
        pw_total_last = round(sum(
            amt for s in PW for dt, amt in svcs.get(s) or []
            if dt == pw_last[0]), 2)
        other_this_year = sorted({
            s for s, lines in svcs.items() if s not in PW
            for dt, _ in lines or [] if dt.startswith(str(yr))})
        tier = "A" if len({y for y in pw_years if y >= yr - 3}) >= 2 else "B"
        rows.append({
            "who": who.title(), "tier": tier,
            "last_pw": pw_last[0], "last_pw_service": pw_last[1],
            "last_pw_total": pw_total_last,
            "pw_years": sorted(pw_years),
            "still_customer": bool(other_this_year),
            "this_year_bought": other_this_year[:4]})
    rows.sort(key=lambda r: (r["tier"], not r["still_customer"],
                             -r["last_pw_total"]))
    return {"built": today.isoformat(),
            "value": round(sum(r["last_pw_total"] for r in rows), 2),
            "tiers": {t: sum(1 for r in rows if r["tier"] == t)
                      for t in "AB"},
            "still_customer": sum(1 for r in rows if r["still_customer"]),
            "rows": rows}


def save():
    """Push the built list to the pw_winback blob (Render has no
    data/ archive — the Mac builds, the cloud renders)."""
    out = build()
    try:
        import clouddb
        if clouddb.available():
            clouddb.put_blob("pw_winback", out)
            return out
    except Exception:
        pass
    try:
        from cloudpush import push
        push(blobs={"pw_winback": out})
    except Exception:
        pass
    return out


def load():
    """Blob when the local archive is missing (Render); fresh build
    when it's here (the Mac)."""
    if (BASE / "data" / "service_history.json").exists():
        return build()
    import clouddb
    if clouddb.available():
        return clouddb.get_blob("pw_winback") or {}
    return {}


if __name__ == "__main__":
    out = build()
    print(f"{len(out['rows'])} win-backs · ${out['value']:,.0f} at last-"
          f"visit prices · tiers {out['tiers']} · "
          f"{out['still_customer']} still buy other work")
    for r in out["rows"][:8]:
        print(f"  {r['tier']} {r['who'][:28]:28} last {r['last_pw']} "
              f"${r['last_pw_total']:>7,.0f} yrs={r['pw_years']} "
              f"{'· STILL BUYS: ' + ','.join(r['this_year_bought']) if r['still_customer'] else ''}")
