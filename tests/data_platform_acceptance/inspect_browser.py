import json,re,traceback
from pathlib import Path
from playwright.sync_api import sync_playwright
secrets=json.loads(Path('/run/bi-browser-auth.json').read_text())
out=Path('/output');out.mkdir(exist_ok=True)
report={}
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1440,'height':1000})
  page.goto('http://127.0.0.1:8088/login/',wait_until='networkidle')
  page.locator('input[name="username"]').fill(secrets['username'])
  page.locator('input[name="password"]').fill(secrets['password'])
  page.get_by_role('button',name=re.compile('Sign In|Log in|Login',re.I)).first.click()
  page.wait_for_load_state('networkidle')
  if '/login' in page.url:raise AssertionError('Browser authentication did not complete')
  for name in ('hg-usage','monthly-sales'):
   page.goto('http://127.0.0.1:8088/superset/dashboard/'+name+'-overview/',wait_until='networkidle')
   page.wait_for_timeout(4000)
   page.screenshot(path=str(out/(name+'.png')),full_page=True)
   report[name]={'body':page.locator('body').inner_text(),'tables':page.locator('table').all_text_contents(),'comboboxes':page.get_by_role('combobox').evaluate_all('(els)=>els.map(e=>({tag:e.outerHTML,visible:!!(e.offsetWidth||e.offsetHeight)}))')}
  browser.close()
 (out/'inspection.json').write_text(json.dumps(report,indent=2))
except Exception:
 text=traceback.format_exc()
 for value in secrets.values():text=text.replace(value,'<redacted>')
 (out/'error.txt').write_text(text)
 print(text)
 raise SystemExit(1)
