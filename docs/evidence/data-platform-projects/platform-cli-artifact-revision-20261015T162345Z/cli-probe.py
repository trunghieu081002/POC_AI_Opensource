import importlib.util,json,os,subprocess,uuid
from pathlib import Path
import yaml
ROOT=Path('/opt/dpagent');spec=importlib.util.spec_from_file_location('assets',ROOT/'packs/superset-assets/assets.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
c=a.Client('http://127.0.0.1:8088',a.environment());name='platform-cli-sales-v2';folder=ROOT/'projects'/name
if folder.exists():raise RuntimeError('Existing CLI fixture; refusing overwrite')
original=a.read_project(yaml.safe_load((ROOT/'projects/monthly-sales/bi.yaml').read_text()),c.binding)
ns=uuid.uuid5(uuid.NAMESPACE_URL,'dpagent-cli-acceptance:'+name)
mapping={key:str(uuid.uuid5(ns,key)) for values in original['resources'].values() for key in values}
for relative,content in original['content'].items():
 p=folder/'bundle'/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(yaml.safe_dump(a.rewrite(content,mapping),sort_keys=False))
(folder/'expected.yaml').write_text(a.json.dumps(a.rewrite(original['expected'],mapping)))
config={'revision':'fixture-v1','name':name,'bundle_dir':'projects/'+name+'/bundle','expected_file':'projects/'+name+'/expected.yaml'}
stack=yaml.safe_load((ROOT/'tests/data_platform_acceptance/stack-bi.yaml').read_text())
params=next(item['superset-assets'] for item in stack['components'] if 'superset-assets' in item);params.update({'projects':[config],'mode':'validate','approved_by':''})
path=Path('/root/platform-cli-artifact-spec.yaml');env=dict(os.environ,**json.loads(Path('/root/platform-test-secrets.json').read_text()))
report={'status':'fail'}
def snapshot():
 with c.db.cursor() as cur:
  result={}
  for kind,table in a.KINDS.items():
   cur.execute('SELECT id,uuid::text FROM "'+table+'" ORDER BY id');result[kind]=[list(row) for row in cur.fetchall()]
  return result
def apply(mode,approve=False):
 p=a.read_project(config,c.binding)
 params['mode']=mode;params['approved_by']='cli-fixture-acceptance' if approve else ''
 if approve:config['approved_sha256']=p['fingerprint']
 else:config.pop('approved_sha256',None)
 path.write_text(yaml.safe_dump(stack,sort_keys=False));path.chmod(0o600)
 subprocess.run(['dpagent','spec',str(path),'--yes','--allow-draft','--no-test'],env=env,check=True)
 return p
before=snapshot()
try:
 apply('validate');p=apply('deploy',True);ids,_=a.check_results(c,p);report['initial_ids']=ids
 # Identical deploy may hit the install checkpoint; verify exact identities/results.
 apply('deploy',True)
 if a.check_results(c,p)[0]!=ids:raise AssertionError('CLI redeploy changed native IDs')
 dashboard_file=folder/'bundle/dashboards/overview.yaml';content=yaml.safe_load(dashboard_file.read_text());content['dashboard_title']='Updated via approved CLI fixture';dashboard_file.write_text(yaml.safe_dump(content,sort_keys=False))
 config['revision']='fixture-v2'
 changed=apply('validate')
 validation=a.json.loads((a.STATE/name/'validation.json').read_text(),parse_float=a.Decimal)
 if validation['status']!='pass' or validation['fingerprint']!=changed['fingerprint']:raise AssertionError('Updated CLI validation not fresh')
 updated=apply('deploy',True)
 after,comparisons=a.check_results(c,updated)
 if after!=ids:raise AssertionError('CLI artifact update changed IDs')
 dashboard=c.request('GET','/api/v1/dashboard/'+str(next(iter(ids['dashboards'].values())))).json()['result']
 if dashboard['dashboard_title']!='Updated via approved CLI fixture':raise AssertionError('CLI artifact update not applied')
 report.update({'stable_ids':True,'fresh_validation':True,'comparisons':comparisons})
 apply('undeploy');apply('undeploy')
 if snapshot()!=before:raise AssertionError('CLI scope cleanup changed the metadata baseline')
 report.update({'status':'pass','metadata_baseline_restored':True,'leaks':0})
finally:
 # Keep a failed result and recover only this receipt-owned fixture if necessary.
 if report['status']!='pass':a.undeploy(c,name)
 Path('/root/platform-cli-artifact-report.json').write_text(a.json.dumps(report,indent=2))
