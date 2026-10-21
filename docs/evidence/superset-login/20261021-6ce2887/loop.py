import json, re
from pathlib import Path
from playwright.sync_api import sync_playwright
AUTH = json.loads(Path("/run/auth.json").read_text()); BASE = "http://127.0.0.1:8088"
out = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    for i in range(100):
        ctx = b.new_context(); page = ctx.new_page(); chain = []
        t0 = __import__("time").monotonic()
        page.on("response", lambda r: chain.append([round(__import__("time").monotonic() - t0, 2), r.request.method,
                r.url.replace(BASE, "")[:60], r.status, (r.headers.get("location") or "").replace(BASE, ""),
                "set-cookie" if "session=" in (r.all_headers().get("set-cookie") or "") else ""])
                if r.request.resource_type in ("document", "xhr", "fetch") else None)
        page.goto(BASE + "/login/", wait_until="networkidle")
        page.locator("#username").fill(AUTH["username"]); page.locator("#password").fill(AUTH["password"])
        page.get_by_role("button", name=re.compile("Sign In|Log in|Login", re.I)).first.click()
        try:
            page.wait_for_url(lambda u: "/login" not in u, timeout=20000); ok = True
        except Exception:
            ok = False
        me = page.request.get(BASE + "/api/v1/me/")
        out.append({"i": i, "ok": ok, "final": page.url.replace(BASE, ""), "me": me.status, "chain": chain})
        ctx.close()
    b.close()
print(json.dumps(out))
