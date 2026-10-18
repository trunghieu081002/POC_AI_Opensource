"""Independent checks for the quickstart acceptance (docs/quickstart.md). Deliberately NOT the product's own
code: the warehouse is read with psql, Superset objects are found by UUID in its metadata database, chart data
comes from Superset's HTTP API, and every value is compared with the hand-calculated
pipelines/quickstart_sales/expected.yaml. Prints one JSON object; exit 1 if a check fails.

  check.py warehouse            fct_monthly_revenue == expected; landing holds each source row once
  check.py bi [TITLE]           dashboard/chart/dataset exist once by UUID; chart data == expected
  check.py bi-absent            none of the project's UUIDs remain in Superset metadata
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = yaml.safe_load((ROOT / "pipelines/quickstart_sales/expected.yaml").read_text())
FIXTURE = yaml.safe_load((ROOT / "pipelines/quickstart_sales/fixture.yaml").read_text())
BUNDLE = ROOT / "projects/quickstart-sales/bundle"
UUIDS = {kind: yaml.safe_load(next((BUNDLE / sub).rglob("*.yaml")).read_text())["uuid"]
         for kind, sub in (("dashboards", "dashboards"), ("slices", "charts"), ("tables", "datasets"), ("dbs", "databases"))}


def psql(database: str, sql: str) -> str:
    return subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
                                    "-d", database, "-c", sql], cwd="/", text=True).strip()


def norm(rows):
    return sorted((str(r["month"]), int(r["orders"]), Decimal(str(r["revenue"])).quantize(Decimal("0.01"))) for r in rows)


def warehouse() -> dict:
    got = json.loads(psql("warehouse", "SELECT coalesce(json_agg(t),'[]') FROM (SELECT month, orders, revenue "
                                       "FROM quickstart_sales.fct_monthly_revenue) t"))
    landing = int(psql("warehouse", "SELECT count(*) FROM quickstart_sales_landing.orders"))
    distinct = int(psql("warehouse", "SELECT count(DISTINCT id) FROM quickstart_sales_landing.orders"))
    source_rows = len(FIXTURE["tables"][0]["rows"])
    ok = norm(got) == norm(EXPECTED["rows"]) and landing == distinct == source_rows
    return {"ok": ok, "rows": got, "expected": EXPECTED["rows"], "landing_rows": landing,
            "landing_distinct_ids": distinct, "source_rows": source_rows}


def metadata_ids() -> dict:
    out = {}
    for table, key in UUIDS.items():
        rows = psql("superset_meta", f"SELECT id FROM {table} WHERE uuid = '{key}'")
        out[table] = [int(x) for x in rows.split()] if rows else []
    return out


def superset_session():
    import requests
    base = "http://127.0.0.1:8088"
    s = requests.Session()
    r = s.post(base + "/api/v1/security/login", json={"username": os.environ.get("SUPERSET_ADMIN_USER", "admin"),
               "password": os.environ["SUPERSET_ADMIN_PASSWORD"], "provider": "db", "refresh": True}, timeout=30)
    r.raise_for_status()
    s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    return s, base


def bi(title: str | None = None) -> dict:
    ids = metadata_ids()
    once = all(len(v) == 1 for v in ids.values())
    result = {"ids": ids, "exactly_once": once}
    if not once:
        return {**result, "ok": False}
    s, base = superset_session()
    r = s.get(f"{base}/api/v1/chart/{ids['slices'][0]}/data/", params={"format": "json", "type": "full"}, timeout=120)
    r.raise_for_status()
    data = r.json(parse_float=Decimal)["result"][0]["data"]
    dash = s.get(f"{base}/api/v1/dashboard/{ids['dashboards'][0]}", timeout=30).json()["result"]
    result.update({"chart_rows": data, "dashboard_title": dash["dashboard_title"],
                   "data_ok": norm(data) == norm(EXPECTED["rows"])})
    result["ok"] = result["data_ok"] and (title is None or dash["dashboard_title"] == title)
    return result


def bi_absent() -> dict:
    ids = metadata_ids()
    return {"ok": not any(ids.values()), "ids": ids}


if __name__ == "__main__":
    what = sys.argv[1]
    out = {"warehouse": warehouse, "bi-absent": bi_absent}.get(what, lambda: bi(*sys.argv[2:3]))()
    print(json.dumps(out, default=str, ensure_ascii=False))
    sys.exit(0 if out["ok"] else 1)
