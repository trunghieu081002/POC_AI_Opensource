"""Run the same native-asset adapter for every configured project."""
import importlib.util,json,sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('assets',ROOT/'packs/superset-assets/assets.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
config=yaml.safe_load((ROOT/'tests/data_platform_acceptance/projects.yaml').read_text())
c=a.Client('http://127.0.0.1:8088',a.environment())
reports={}
for item in config['projects']:
    name=item['bi']
    if len(sys.argv)>2 and name!=sys.argv[2]:continue
    p=a.read_project(yaml.safe_load((ROOT/'projects'/name/'bi.yaml').read_text()),c.binding)
    action=sys.argv[1]
    if action in ('deploy','redeploy'):
        before=c.lookup(p['resources']) if action=='redeploy' else None
        p['config']['approved_sha256']=p['fingerprint']
        ids=a.deploy(c,p,'disposable-integration-acceptance')
        if before is not None and ids!=before:raise AssertionError('Redeploy changed native UUID/ID mapping')
        _,comparisons=a.check_results(c,p)
        reports[name]={'ids':ids,'fingerprint':p['fingerprint'],'stable_ids':before is None or before==ids,'comparisons':comparisons}
    elif action=='verify':
        ids,observations=a.check_results(c,p);reports[name]={'ids':ids,'comparisons':observations}
    elif action=='undeploy':
        a.undeploy(c,name)
        if any(c.lookup(p['resources']).values()):raise AssertionError('Native project UUID leak')
        reports[name]={'removed':True}
    else:raise ValueError(action)
print(a.json.dumps(reports,indent=2,default=str))
