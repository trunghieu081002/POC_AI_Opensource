"""Disposable-host native BI acceptance; all expected values are literal fixtures."""
import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('bi_assets',ROOT/'packs/superset-assets/assets.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
sys.path.insert(0,str(Path(__file__).parent))
from native_fixture import create
NS=os.environ['DP_TEST_NS']
if not re.fullmatch(r'dpagent_selftest_[0-9]+',NS): raise RuntimeError('Unsafe fixture namespace')
RECEIPT=Path('/var/tmp')/(NS+'_bi.json')
CONFIG=json.loads(Path('/opt/superset/reader-owned.json').read_text())
CREATOR=CONFIG['creators'][0]; READER=CONFIG['role']; SCHEMA=CONFIG['schemas'][0]
BASE='http://127.0.0.1:'+os.environ.get('DP_PARAM_WEBSERVER_PORT','8088')

def ident(v): return '"'+v.replace('"','""')+'"'
def sql(statement):
    return subprocess.check_output(['runuser','-u','postgres','--','psql','-X','-At','-v','ON_ERROR_STOP=1','-p',str(CONFIG['port']),'-d',CONFIG['database'],'-c',statement],cwd='/').decode().strip()
def client(): return a.Client(BASE,a.environment())
def project(c,index):
    folder=ROOT/'projects'/(NS+'-'+str(index));binding=c.binding
    return a.read_project({'name':NS+'-'+str(index),'bundle_dir':str(folder.relative_to(ROOT)/'bundle'),'expected_file':str(folder.relative_to(ROOT)/'expected.yaml')},binding)
def approve(p): p['config']['approved_sha256']=p['fingerprint'];return p
def receipt(): return json.loads(RECEIPT.read_text())
def save(value): a.write_state(RECEIPT,value)
def baseline(c):
    with c.db.cursor() as cur:
        return {kind:fetch(cur,table) for kind,table in a.KINDS.items()}
def fetch(cur,table):
    cur.execute('SELECT id,uuid::text FROM "'+table+'" ORDER BY id');return [list(row) for row in cur.fetchall()]
def setup():
    if RECEIPT.exists(): raise RuntimeError('Existing BI fixture receipt')
    c=client();record={'baseline':baseline(c),'projects':[],'tables':[]};save(record)
    for index,dimension,measure in [(1,'region','amount'),(2,'category','net_units')]:
        name=NS+'-'+str(index);table=NS+'_bi_'+str(index);folder=ROOT/'projects'/name
        if folder.exists(): raise RuntimeError('Existing BI fixture directory')
        create(folder,name,SCHEMA,table,dimension,measure)
        statement='SET ROLE '+ident(CREATOR)+'; CREATE TABLE '+ident(SCHEMA)+'.'+ident(table)+' ('+ident(dimension)+' text, '+ident(measure)+' integer); INSERT INTO '+ident(SCHEMA)+'.'+ident(table)+" VALUES ('north',10),('north',20),('south',40);"
        sql(statement);record['tables'].append(table);record['projects'].append(name);save(record)
    print('Two configuration-only projects created; literal fixture expected preserved')
def approval():
    c=client();before=baseline(c)
    try:a.deploy(c,project(c,1),'')
    except RuntimeError:pass
    else:raise AssertionError('Unapproved BI deployment succeeded')
    if baseline(c)!=before:raise AssertionError('Approval rejection mutated metadata')
    if a.deployment_path(project(c,1)).exists():raise AssertionError('Approval rejection changed receipt')
    print('Unapproved deployment refused without metadata or approval mutation')
def results():
    c=client();record=receipt();record['ids']={};record['comparisons']={}
    for index in (1,2):
        p=approve(project(c,index));record['ids'][str(index)]=a.deploy(c,p,'acceptance-fixture');_,observations=a.check_results(c,p);record['comparisons'][str(index)]=observations;save(record)
    print('Both project charts/native filters and independent SQL match literal expected')
def redeploy():
    c=client();record=receipt();before=baseline(c)
    for index in (1,2):
        if a.deploy(c,approve(project(c,index)),'acceptance-fixture')!=record['ids'][str(index)]:raise AssertionError('Redeploy changed IDs')
    if baseline(c)!=before:raise AssertionError('Redeploy duplicated native metadata')
    print('Redeploy preserved exact UUID/ID sets for both projects')
def negative():
    c=client();p=project(c,1);p['expected']=copy.deepcopy(p['expected']);p['expected']['checks'][0]['rows'][0]['total']=999
    old=a.deployment_path(p).read_bytes();before=baseline(c)
    try:a.validate(c,p)
    except a.ComparisonFailure:pass
    else:raise AssertionError('Wrong independent expected accepted')
    if baseline(c)!=before or a.deployment_path(p).read_bytes()!=old:raise AssertionError('Failed validation changed metadata/approval')
    report=json.loads((a.STATE/p['name']/'validation.json').read_text())
    if report['status']!='fail' or report['cleanup']!='pass':raise AssertionError('Negative validation cleanup not proven')
    print('Incorrect literal expected refused, failed report retained, clones removed, approval unchanged')
def scope():
    c=client();p=project(c,1);path=ROOT/'projects'/p['name']/'bundle/dashboards/overview.yaml'
    value=a.yaml.safe_load(path.read_text());value['dashboard_title']='Updated fixture overview';path.write_text(a.yaml.safe_dump(value,sort_keys=False))
    updated=approve(project(c,1));old=receipt()['ids']['1']
    if a.deploy(c,updated,'acceptance-updated')!=old:raise AssertionError('Artifact update replaced IDs')
    response=c.request('GET','/api/v1/dashboard/'+str(next(iter(old['dashboards'].values())))).json()['result']
    if response['dashboard_title']!='Updated fixture overview':raise AssertionError('Artifact update not applied')
    a.undeploy(c,p['name']);a.undeploy(c,p['name'])
    if any(c.lookup(p['resources']).values()):raise AssertionError('Undeploy left project metadata')
    a.check_results(c,project(c,2))
    print('Artifact update retained IDs; idempotent undeploy left second project querying correctly')
def teardown():
    if not RECEIPT.exists():return
    record=receipt();c=client()
    for name in record['projects']:
        a.undeploy(c,name)
        p=project(c,int(name.rsplit('-',1)[1]))
        if any(c.lookup(p['resources']).values()):raise AssertionError('Owned BI UUID leaked')
    if baseline(c)!=record['baseline']:raise AssertionError('BI metadata differs from pre-suite baseline')
    for table in record['tables']:
        sql('SET ROLE '+ident(CREATOR)+'; DROP TABLE '+ident(SCHEMA)+'.'+ident(table)+';')
    record['leaks']=0;record['cleanup']='pass';save(record)
    print('leaks=0; metadata baseline exact; owned warehouse fixture tables removed')
if __name__=='__main__':globals()[sys.argv[1]]()
