"""Viewer RBAC through Superset's HTTP API, logged in AS THE VIEWER (never as admin). Independent of the pack's
code: object IDs come from Superset's metadata by UUID, expected rows from the project's literal expected file.

  check_viewer.py USERNAME PASSWORD_ENV OWN_PROJECT OTHER_PROJECT

Must hold: the account's only role is its project's viewer role; its project's dashboard and chart are readable and
return the expected rows, also with the native filter's value applied; every write (edit/delete a chart or dashboard,
create a chart, create a database connection, run SQL) is refused with a permission status (403/404 - a 400 CSRF
or a 5xx is NOT accepted as a refusal); the other project's dashboard, chart and dataset are not readable and do not
appear in its lists. Prints JSON; exit 1 on any failure."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8088"
DENIED = (403, 404)


def psql(sql: str) -> str:
    return subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
                                    "-d", "superset_meta", "-c", sql], cwd="/", text=True).strip()


def ids(project: str) -> dict:
    bundle = ROOT / "projects" / project / "bundle"
    out = {}
    for kind, sub, table in (("dashboard", "dashboards", "dashboards"), ("chart", "charts", "slices"),
                             ("dataset", "datasets", "tables")):
        key = yaml.safe_load(next((bundle / sub).rglob("*.yaml")).read_text())["uuid"]
        out[kind] = int(psql(f"SELECT id FROM {table} WHERE uuid = '{key}'"))
    return out


def expected(project: str) -> dict:
    return yaml.safe_load((ROOT / "projects" / project / "expected.yaml").read_text())["checks"][0]


def canon(rows):
    def value(v):
        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            return ("n", str(Decimal(str(v)).normalize()))
        return ("s", str(v))
    return Counter(json.dumps(sorted((k, value(v)) for k, v in r.items())) for r in rows)


def main(username: str, password_env: str, own: str, other: str) -> dict:
    report: dict = {"user": username, "problems": []}
    bad = report["problems"].append
    s = requests.Session()
    r = s.post(BASE + "/api/v1/security/login", json={"username": username, "password": os.environ[password_env],
                                                      "provider": "db", "refresh": True}, timeout=30)
    if r.status_code != 200:
        return {**report, "ok": False, "problems": [f"viewer login failed: {r.status_code}"]}
    s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    csrf = s.get(BASE + "/api/v1/security/csrf_token/", timeout=30)
    report["csrf_token_status"] = csrf.status_code
    if csrf.status_code == 200:
        s.headers["X-CSRFToken"] = csrf.json()["result"]
        s.headers["Referer"] = BASE + "/"

    roles = s.get(BASE + "/api/v1/me/roles/", timeout=30)
    report["roles"] = sorted((roles.json().get("result") or {}).get("roles", {}) or []) if roles.ok else roles.status_code
    if report["roles"] != ["dpagent-viewer-" + own]:
        bad(f"roles are {report['roles']}, want only dpagent-viewer-{own}")

    mine, theirs = ids(own), ids(other)
    report["ids"] = {"own": mine, "other": theirs}
    check = expected(own)

    # read: dashboard, chart data, chart data with the filter value
    if s.get(f"{BASE}/api/v1/dashboard/{mine['dashboard']}", timeout=30).status_code != 200:
        bad("own dashboard not readable")
    chart = s.get(f"{BASE}/api/v1/chart/{mine['chart']}", timeout=30)
    if chart.status_code != 200:
        bad(f"own chart not readable: {chart.status_code}")
    else:
        context = json.loads(chart.json()["result"]["query_context"])
        context["datasource"] = {"id": mine["dataset"], "type": "table"}
        context["form_data"]["datasource"] = f"{mine['dataset']}__table"
        context["force"] = True
        data = s.post(BASE + "/api/v1/chart/data", json=context, timeout=120)
        rows = data.json(parse_float=Decimal)["result"][0]["data"] if data.ok else data.status_code
        report["own_rows"] = rows
        if not data.ok or canon(rows) != canon(check["rows"]):
            bad(f"own chart rows {rows} != expected {check['rows']}")
        f = check["filters"][0]
        column = next(iter(f["rows"][0]))
        for q in context["queries"]:
            q.setdefault("filters", []).append({"col": column, "op": "IN", "val": [str(v) for v in f["values"]]})
        data = s.post(BASE + "/api/v1/chart/data", json=context, timeout=120)
        rows = data.json(parse_float=Decimal)["result"][0]["data"] if data.ok else data.status_code
        report["own_filtered_rows"] = rows
        if not data.ok or canon(rows) != canon(f["rows"]):
            bad(f"own filtered rows {rows} != expected {f['rows']}")

    # write attempts: each must be refused with a permission status
    attempts = {
        "edit own chart": ("PUT", f"/api/v1/chart/{mine['chart']}", {"slice_name": "edited by viewer"}),
        "edit own dashboard": ("PUT", f"/api/v1/dashboard/{mine['dashboard']}", {"dashboard_title": "edited by viewer"}),
        "delete own chart": ("DELETE", f"/api/v1/chart/{mine['chart']}", None),
        "delete own dashboard": ("DELETE", f"/api/v1/dashboard/{mine['dashboard']}", None),
        "create chart": ("POST", "/api/v1/chart/", {"slice_name": "viewer chart", "viz_type": "table",
                                                   "datasource_id": mine["dataset"], "datasource_type": "table"}),
        "create database": ("POST", "/api/v1/database/", {"database_name": "viewer db",
                                                         "sqlalchemy_uri": "postgresql://x@127.0.0.1/warehouse"}),
        "run SQL (write)": ("POST", "/api/v1/sqllab/execute/", {"database_id": 1, "runAsync": False,
                            "sql": "INSERT INTO quickstart_sales.stg_orders (id) VALUES (999)"}),
    }
    report["write_attempts"] = {}
    for label, (method, path, body) in attempts.items():
        resp = s.request(method, BASE + path, json=body, timeout=60)
        report["write_attempts"][label] = resp.status_code
        if resp.status_code not in DENIED:
            bad(f"{label}: {resp.status_code} (want a permission refusal 403/404): {resp.text[:200]}")

    # the other project: not readable, not listed
    for kind, path in (("dashboard", f"/api/v1/dashboard/{theirs['dashboard']}"),
                       ("chart", f"/api/v1/chart/{theirs['chart']}"),
                       ("dataset", f"/api/v1/dataset/{theirs['dataset']}")):
        code = s.get(BASE + path, timeout=30).status_code
        report.setdefault("other_project", {})[kind] = code
        if code not in DENIED:
            bad(f"other project's {kind} readable: {code}")
    listed = s.get(BASE + "/api/v1/dashboard/", timeout=30)
    visible = sorted(d["id"] for d in listed.json().get("result", [])) if listed.ok else listed.status_code
    report["dashboards_listed"] = visible
    if visible != [mine["dashboard"]]:
        bad(f"dashboard list shows {visible}, want only {[mine['dashboard']]}")
    report["ok"] = not report["problems"]
    return report


if __name__ == "__main__":
    out = main(*sys.argv[1:5])
    print(json.dumps(out, default=str, ensure_ascii=False))
    sys.exit(0 if out["ok"] else 1)
