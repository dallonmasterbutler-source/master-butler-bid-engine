"""Locks the office playbook rules (seasons.py) to the office's own
documents — dates from '10. Winter Gutters', dependencies and skylight/
french-pane/new-construction rules from '9. Spring_Summer Services',
referrals from '4. Office Procedures and Help'."""

from datetime import date

import seasons


def run():
    P = 0
    F = []

    def case(name, want_alert, want_note, parsed, prop=None, when=None):
        nonlocal P
        alert, notes = seasons.check(parsed, prop, when)
        blob = " ".join(notes).lower()
        ok = True
        if want_alert is True and not alert:
            ok = False
        if want_alert is False and alert:
            ok = False
        if isinstance(want_alert, str) and want_alert not in (alert or "").lower():
            ok = False
        if want_note and want_note not in blob:
            ok = False
        if want_note is False and notes:
            ok = False
        if ok:
            P += 1
        else:
            F.append(f"{name}: alert={alert!r} notes={notes!r}")

    JUL = date(2026, 7, 9)
    DEC = date(2026, 12, 10)
    MAR = date(2026, 3, 10)
    FEB_EARLY = date(2026, 2, 10)

    # 1 roof blow-off dependency
    case("rbo alone → alert", "gutter cleaning", None,
         {"services": ["roof_blow_off"]}, when=JUL)
    case("rbo + gutters → clean", False, False,
         {"services": ["roof_blow_off", "gutter_cleaning"]}, when=JUL)
    case("rbo + guards svc → clean", False, False,
         {"services": ["roof_blow_off_guards"]}, when=JUL)
    case("rbo, guards in text → clean", False, None,
         {"services": ["roof_blow_off"],
          "newest_message": "we have gutter guards installed"}, when=JUL)

    # 2 moss removal timing
    case("moss removal in Dec → next August", False, "august-only",
         {"services": ["moss_removal"]}, when=DEC)
    case("moss removal in June → treatment lead", False, "4–6 weeks",
         {"services": ["moss_removal"]}, when=date(2026, 6, 1))
    case("moss removal in Aug → season now", False, "season is now",
         {"services": ["moss_removal"]}, when=date(2026, 8, 5))

    # 3 winter pause
    case("windows in Dec → paused note", False, "suspended",
         {"services": ["windows_unspecified"]}, when=DEC)
    case("pressure wash in Feb 10 → paused", False, "suspended",
         {"services": ["pressure_washing"]}, when=FEB_EARLY)
    case("windows in Mar → no pause note", False, False,
         {"services": ["windows_unspecified"]}, when=MAR)
    case("windows in Jul → no pause note", False, False,
         {"services": ["windows_unspecified"]}, when=JUL)

    # 4 holiday-lights disclaimer
    case("gutters in Dec → lights disclaimer", False, "holiday lights",
         {"services": ["gutter_cleaning"]}, when=DEC)
    case("gutters in Jul → no disclaimer", False, False,
         {"services": ["gutter_cleaning"]}, when=JUL)

    # 5 skylights
    case("skylight + windows → note", False, "skylights not included",
         {"services": ["windows_unspecified"],
          "newest_message": "please include the skylights"}, when=MAR)
    case("skylight + windows + metal roof → alert", "skylights", None,
         {"services": ["windows_unspecified"],
          "newest_message": "please include the skylights"},
         prop={"roof_material": "metal"}, when=MAR)

    # 6 patio furniture
    case("patio pressure wash → move furniture", False, "move furniture",
         {"services": ["pressure_washing"],
          "newest_message": "back patio pressure washing please"},
         when=JUL)

    # 7 new construction
    case("new construction windows → alert", "new-construction", None,
         {"services": ["windows_unspecified"],
          "newest_message": "new construction, windows have stickers"},
         when=JUL)

    # 8 french panes
    case("french panes → rate note", False, "french panes",
         {"services": ["windows_unspecified"],
          "newest_message": "the house has french panes throughout"},
         when=JUL)

    # 10 referrals
    case("Seattle address → Fricke", "david fricke", None,
         {"services": ["gutter_cleaning"],
          "address": "123 Pine St, Seattle, WA 98101"}, when=JUL)
    case("Everett → Fricke", "david fricke", None,
         {"services": ["gutter_cleaning"],
          "address": "500 Broadway, Everett WA 98201"}, when=JUL)
    case("Monroe → in area, quiet", False, False,
         {"services": ["gutter_cleaning"],
          "address": "421 Maple Street, Monroe, WA 98272"}, when=JUL)
    case("Bellevue → in area", False, False,
         {"services": ["gutter_cleaning"],
          "address": "64 158th Place SE, Bellevue, WA 98008"}, when=JUL)
    case("Tacoma → maybe out, Nielsen", "nielsen", None,
         {"services": ["gutter_cleaning"],
          "address": "12 Elm St, Tacoma, WA 98402"}, when=JUL)
    case("no address → quiet", False, False,
         {"services": ["gutter_cleaning"]}, when=JUL)
    case("Utah address → out-of-state alert (Martha's test)",
         "outside washington", None,
         {"services": ["gutter_cleaning"],
          "address": "969 S 770 E Heber City UT 84032"}, when=JUL)

    print("=" * 50)
    for f in F:
        print("FAIL", f)
    print(f"RESULT: {P} passed, {len(F)} failed")
    return not F


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
