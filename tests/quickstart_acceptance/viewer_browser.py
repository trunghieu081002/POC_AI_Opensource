"""Viewer RBAC in a real Chromium (the pinned Playwright image of tests/data_platform_acceptance/browser.Dockerfile),
logged in AS THE VIEWER. /run/viewer-auth.json: {"username", "password"}; /run/viewer-targets.json:
{"own": {"url", "rows", "filter_values", "filtered_rows"}, "other": {"url"}}. Writes /output/viewer-browser.json.

Own dashboard: the table renders the expected rows (and the chart-data response says the same), the native filter
gives the filtered rows, and there is no "Edit dashboard" control. Other project's dashboard: no table renders and
no chart-data request succeeds."""
import json
import re
import time
import traceback
from collections import Counter
from decimal import Decimal
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/output"); OUT.mkdir(exist_ok=True)
AUTH = json.loads(Path("/run/viewer-auth.json").read_text())
T = json.loads(Path("/run/viewer-targets.json").read_text(), parse_float=Decimal)
BASE = "http://127.0.0.1:8088"


def canon(rows):
    def v(x):
        try:
            return ("n", str(Decimal(str(x).replace(",", "")).normalize()))
        except Exception:
            return ("s", str(x))
    return Counter(json.dumps(sorted((k, v(x)) for k, x in r.items())) for r in rows)


def rendered(page):
    headers = [h.strip() for h in page.locator("table").first.locator("th").all_text_contents()]
    cells = page.locator("table").last.locator("tbody tr").evaluate_all(
        "(rows)=>rows.map(r=>Array.from(r.querySelectorAll('td')).map(c=>c.innerText.trim()))")
    return [dict(zip(headers, c)) for c in cells if len(c) == len(headers)], headers


def wait_rows(page, want, responses, start):
    deadline, last = time.monotonic() + 60, None
    while time.monotonic() < deadline:
        try:
            rows, headers = rendered(page)
            api = [r for r in responses[start:] if canon(r) == canon(want)]
            if canon(rows) == canon(want) and api:
                return {"rendered": rows, "headers": headers, "api_rows": api[-1]}
            last = rows
        except Exception as exc:
            last = type(exc).__name__
        page.wait_for_timeout(250)
    raise AssertionError(f"rendered/API rows did not match expected; last={last}")


report = {"status": "fail", "user": AUTH["username"]}
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(BASE + "/login/", wait_until="networkidle")
        page.locator("#username").fill(AUTH["username"]); page.locator("#password").fill(AUTH["password"])
        page.get_by_role("button", name=re.compile("Sign In|Log in|Login", re.I)).first.click()
        page.wait_for_load_state("networkidle")
        if "/login" in page.url:
            raise AssertionError("viewer login failed")
        responses, statuses = [], []

        def record(resp):
            if "/api/v1/chart/data" in resp.url and resp.request.method == "POST":
                statuses.append(resp.status)
                if resp.ok:
                    for item in json.loads(resp.text(), parse_float=Decimal).get("result", []):
                        if "data" in item:
                            responses.append(item["data"])
        page.on("response", record)

        own = T["own"]
        page.goto(BASE + own["url"], wait_until="networkidle")
        report["own_unfiltered"] = wait_rows(page, own["rows"], responses, 0)
        report["edit_controls"] = page.get_by_text(re.compile(r"^Edit dashboard$", re.I)).count()
        if report["edit_controls"]:
            raise AssertionError("the viewer sees an 'Edit dashboard' control")
        page.screenshot(path=str(OUT / "viewer-own.png"), full_page=True)
        page.locator(".ant-select-selector").first.click()
        for value in own["filter_values"]:
            page.locator(".ant-select-dropdown:visible").get_by_text(str(value), exact=True).click()
        page.locator("body").click(position={"x": 1000, "y": 120})
        start = len(responses)
        page.get_by_role("button", name="Apply filters", exact=True).click()
        report["own_filtered"] = wait_rows(page, own["filtered_rows"], responses, start)
        page.screenshot(path=str(OUT / "viewer-own-filtered.png"), full_page=True)

        responses.clear(); statuses.clear()
        page.goto(BASE + T["other"]["url"], wait_until="networkidle")
        page.wait_for_timeout(5000)
        tables = page.locator("table tbody tr").count()
        report["other"] = {"final_url": page.url.replace(BASE, ""), "table_rows": tables,
                           "chart_data_statuses": statuses, "chart_data_ok": len(responses)}
        page.screenshot(path=str(OUT / "viewer-other.png"), full_page=True)
        if tables or responses:
            raise AssertionError("the viewer could see the other project's dashboard data")
        browser.close()
    report["status"] = "pass"
except Exception:
    report["error"] = traceback.format_exc().replace(AUTH["password"], "<redacted>")
    print(report["error"])
finally:
    (OUT / "viewer-browser.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
if report["status"] != "pass":
    raise SystemExit(1)
print("viewer browser: own dashboard and filter match expected, no edit control, other project not visible")
