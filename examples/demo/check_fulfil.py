"""Independent check for the demo (docs/demo.md): the warehouse is read with psql - not through the product's code -
and compared with the hand-calculated expected result of the fulfil case. Prints one JSON object; exit 1 on a mismatch.

  check_fulfil.py EXPECTED.yaml [--fact-only]

--fact-only: only the report table (after a run that had to stop at landing, the landing tables may hold the bad
load; the report table must still hold the last good result)."""
from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import yaml


def psql(sql: str) -> str:
    return subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
                                    "-d", "warehouse", "-c", sql], cwd="/", text=True).strip()


def key(r: dict) -> tuple:
    return (str(r["month"]), int(r["eligible_orders"]), int(r["delivered_orders"]), int(r["on_time_orders"]),
            Decimal(str(r["on_time_rate"])).quantize(Decimal("0.1")))


def main(argv: list[str]) -> int:
    expected_path = Path(argv[0])
    expected = yaml.safe_load(expected_path.read_text())
    fixture = yaml.safe_load((expected_path.parent / "fixture.yaml").read_text())
    got = json.loads(psql("SELECT coalesce(json_agg(t), '[]') FROM (SELECT month, eligible_orders, delivered_orders, "
                          "on_time_orders, on_time_rate FROM fulfilment_sla.fct_delivery_sla) t"))
    out = {"rows": got, "expected": expected["rows"],
           "fact_ok": sorted(map(key, got)) == sorted(map(key, expected["rows"]))}
    if "--fact-only" not in argv:
        for table in fixture["tables"]:
            n = int(psql(f"SELECT count(*) FROM fulfilment_sla_landing.{table['name']}"))
            d = int(psql(f"SELECT count(DISTINCT id) FROM fulfilment_sla_landing.{table['name']}"))
            out[f"landing_{table['name']}"] = {"rows": n, "distinct_ids": d, "source_rows": len(table["rows"])}
        landing_ok = all(v["rows"] == v["distinct_ids"] == v["source_rows"]
                         for k, v in out.items() if k.startswith("landing_"))
        out["landing_ok"] = landing_ok
    out["ok"] = out["fact_ok"] and out.get("landing_ok", True)
    print(json.dumps(out, default=str))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
