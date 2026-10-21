"""Independent check of a benchmark run: the warehouse is read with psql (not the product's code) and compared
with the answer key generate.py computed in plain Python. Prints one JSON object; exit 1 on any mismatch.

  check.py EXPECTED.json"""
from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal


def psql(sql: str) -> str:
    return subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
                                    "-d", "warehouse", "-c", sql], cwd="/", text=True).strip()


def compare(expected: dict, fact_rows: list, landing: int, distinct: int) -> dict:
    key = lambda r: (str(r["month"]), int(r["orders"]), Decimal(str(r["revenue"])).quantize(Decimal("0.01")))
    got, want = sorted(map(key, fact_rows)), sorted(map(key, expected["expected"]))
    inv = expected["invariants"]
    checks = {
        "fact_equals_expected": got == want,
        "landing_rows": landing == inv["landing_rows"],
        "landing_ids_unique": distinct == inv["landing_distinct_ids"],
        "done_orders_total": sum(r[1] for r in got) == inv["done_orders"],
        "done_revenue_total": sum((r[2] for r in got), Decimal(0)) == Decimal(inv["done_revenue"]),
    }
    return {"ok": all(checks.values()), "checks": checks, "months": len(got), "landing_rows": landing,
            "first_mismatch": next(([g, w] for g, w in zip(got, want) if g != w), None)}


def main():
    expected = json.loads(open(sys.argv[1]).read())
    rows = json.loads(psql("SELECT coalesce(json_agg(t),'[]') FROM (SELECT month, orders, revenue "
                           "FROM quickstart_sales.fct_monthly_revenue) t"))
    landing = int(psql("SELECT count(*) FROM quickstart_sales_landing.orders"))
    distinct = int(psql("SELECT count(DISTINCT id) FROM quickstart_sales_landing.orders"))
    out = dict(compare(expected, rows, landing, distinct), label=expected["label"])
    print(json.dumps(out, default=str))
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
