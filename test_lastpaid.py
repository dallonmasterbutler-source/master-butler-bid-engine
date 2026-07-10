"""Martha's rules (Jul 10) locked as tests:
  1. never quote a returning customer less than their last invoice
  2. moss treatment labor always brings Moss Treatment Product $14.50
Run: python3 test_lastpaid.py
"""
import lastpaid

HIST = {"by_property": {"10029-ne-139th-st-kirkland": {
            "gutter": [["2025-08-21", 200.0], ["2023-12-01", 200.0]],
            "moss": [["2025-08-21", 55.0], ["2025-08-21", 13.0]],
        }},
        "by_client": {"robert lin": {
            "gutter": [["2025-08-21", 200.0]]}}}


def run():
    ok, fails = 0, []

    def check(cond, msg):
        nonlocal ok
        ok += 1 if cond else 0
        if not cond:
            fails.append(msg)

    orig = lastpaid._history
    lastpaid._history = lambda: HIST
    try:
        # raise-to-last-paid, property match
        lines = [{"name": "Gutter Cleaning", "price": 175},
                 {"name": "Moss Treatment", "price": 50}]
        notes = lastpaid.apply(lines, address="10029 NE 139th St, Kirkland, WA")
        check(lines[0]["price"] == 200.0, "gutter raised to last-paid 200")
        check(lines[1]["price"] == 55.0,
              "moss raised to LABOR 55, not the 13 product line")
        check(len(notes) == 2 and all("RETURNING" in n for n in notes),
              "both raises noted")

        # higher engine price never lowered
        lines = [{"name": "Gutter Cleaning", "price": 300}]
        notes = lastpaid.apply(lines, address="10029 NE 139th St, Kirkland, WA")
        check(lines[0]["price"] == 300 and not notes,
              "higher price never lowered")

        # unknown customer untouched
        lines = [{"name": "Gutter Cleaning", "price": 175}]
        notes = lastpaid.apply(lines, address="1 Elsewhere Rd, Monroe, WA",
                               client_name="Total Stranger")
        check(lines[0]["price"] == 175 and not notes, "new customer untouched")

        # client-name fallback when address unknown
        lines = [{"name": "Gutter Cleaning", "price": 175}]
        lastpaid.apply(lines, client_name="Robert Lin")
        check(lines[0]["price"] == 200.0, "client-name fallback raises")

        # >50% raise carries the review flag
        big = {"by_property": {"1-big-raise-ln": {
            "gutter": [["2025-01-01", 500.0]]}}, "by_client": {}}
        lastpaid._history = lambda: big
        lines = [{"name": "Gutter Cleaning", "price": 175}]
        notes = lastpaid.apply(lines, address="1 Big Raise Ln")
        check(lines[0]["price"] == 500.0 and any("REVIEW" in n for n in notes),
              ">50% raise review-flagged")

        # companion/product lines never floored
        lastpaid._history = lambda: HIST
        lines = [{"name": "Moss Treatment Product", "price": 14.50}]
        notes = lastpaid.apply(lines, address="10029 NE 139th St, Kirkland, WA")
        check(lines[0]["price"] == 14.50 and not notes,
              "product companion not floored")
    finally:
        lastpaid._history = orig

    print(f"RESULT: {ok} passed, {len(fails)} failed")
    for f in fails:
        print("  FAIL:", f)
    return not fails


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
