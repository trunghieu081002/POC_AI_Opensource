"""Real Chromium interaction; target/expected data stays in project contracts."""
import json,re,time,traceback
from collections import Counter
from decimal import Decimal
from pathlib import Path
from playwright.sync_api import sync_playwright
OUT=Path('/output');OUT.mkdir(exist_ok=True)
AUTH=json.loads(Path('/run/bi-browser-auth.json').read_text())
TARGETS=json.loads(Path('/run/bi-browser-targets.json').read_text(),parse_float=Decimal)

def canon(v):
 if v is None:return ['null']
 if isinstance(v,bool):return ['boolean',v]
 if isinstance(v,(int,float,Decimal)):
  n=Decimal(str(v))
  if not n.is_finite():raise ValueError('Non-finite chart value')
  if n==0:return ['number','0']
  sign,digits,exponent=n.as_tuple();digits=list(digits)
  while digits and digits[-1]==0:digits.pop();exponent+=1
  return ['number',sign,''.join(map(str,digits)),exponent]
 if isinstance(v,str):return ['string',v]
 if isinstance(v,list):return ['list',[canon(x) for x in v]]
 if isinstance(v,dict):return ['object',[[k,canon(x)] for k,x in sorted(v.items())]]
 raise ValueError(type(v).__name__)
def same(a,b):return Counter(json.dumps(canon(r)) for r in a)==Counter(json.dumps(canon(r)) for r in b)
def table(page,expected):
 headers=[v.strip() for v in page.locator('table').first.locator('th').all_text_contents()]
 cells=page.locator('table').last.locator('tbody tr').evaluate_all('(rows)=>rows.map(r=>Array.from(r.querySelectorAll("td")).map(c=>c.innerText.trim()))')
 if not headers or set(headers)!=set(expected[0]):raise AssertionError('Rendered table headers differ from expected')
 types={k:type(next(r[k] for r in expected if r[k] is not None)) for k in headers}
 rows=[]
 for values in cells:
  if len(values)!=len(headers):raise AssertionError('Rendered cell count mismatch')
  row={}
  for key,value in zip(headers,values):
   row[key]=Decimal(value.replace(',','')) if types[key] in (int,float,Decimal) else value
  rows.append(row)
 return rows,{'headers':headers,'cells':cells}
def check(page,expected,responses,start):
 deadline=time.monotonic()+60
 last=None
 while time.monotonic()<deadline:
  try:
   actual,rendered=table(page,expected)
   matches=[r for r in responses[start:] if same(r,expected)]
   if same(actual,expected) and matches:return {'expected':expected,'api_rows':matches[-1],'rendered':rendered,'matched':True}
  except Exception as exc:last=type(exc).__name__
  page.wait_for_timeout(250)
 raise AssertionError('Rendered chart/API did not match independent expected; last='+str(last))

report={'status':'fail','projects':{}}
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1440,'height':1000})
  page.goto('http://127.0.0.1:8088/login/',wait_until='networkidle')
  page.locator('#username').fill(AUTH['username']);page.locator('#password').fill(AUTH['password'])
  page.get_by_role('button',name=re.compile('Sign In|Log in|Login',re.I)).first.click();page.wait_for_load_state('networkidle')
  if '/login' in page.url:raise AssertionError('Authenticated browser login failed')
  responses=[]
  def record(response):
   if '/api/v1/chart/data' in response.url and response.request.method=='POST' and response.ok:
    body=json.loads(response.text(),parse_float=Decimal)
    for item in body.get('result',[]):
     if 'data' in item:responses.append(item['data'])
  page.on('response',record)
  for target in TARGETS:
   responses.clear();page.goto('http://127.0.0.1:8088'+target['url'],wait_until='networkidle')
   before=check(page,target['rows'],responses,0)
   page.screenshot(path=str(OUT/(target['name']+'-unfiltered.png')),full_page=True)
   selected=target['filter_values']
   page.locator('.ant-select-selector').first.click()
   for value in selected:page.locator('.ant-select-dropdown:visible').get_by_text(str(value),exact=True).click()
   page.get_by_role('heading').first.click() if page.get_by_role('heading').count() else page.locator('body').click(position={'x':1000,'y':120})
   start=len(responses);page.get_by_role('button',name='Apply filters',exact=True).click()
   filtered=check(page,target['filtered_rows'],responses,start)
   page.screenshot(path=str(OUT/(target['name']+'-filtered.png')),full_page=True)
   report['projects'][target['name']]={'unfiltered':before,'selected_values':selected,'filtered':filtered}
  browser.close()
 report['status']='pass'
except Exception:
 error=traceback.format_exc()
 for value in AUTH.values():error=error.replace(value,'<redacted>')
 report['error']=error;print(error)
finally:
 (OUT/'browser-comparison.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
if report['status']!='pass':raise SystemExit(1)
print('Browser rendered chart and native filter match independent expected for '+str(len(TARGETS))+' projects')
