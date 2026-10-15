"""Actual pack-composed pipeline/BI operations; business cases live in config."""
from __future__ import annotations
import contextlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests/m25_acceptance')]
from dpagent.engine import state
from dpagent.pipelines import approval,bronze,deploy,evidence,fixture,loader,pg_throwaway,validate
import run_matrix as matrix
OUT=Path('/root/data-platform-evidence');OUT.mkdir(exist_ok=True)
CONFIG=yaml.safe_load((ROOT/'tests/data_platform_acceptance/projects.yaml').read_text())['projects']
REPORT={'status':'fail','projects':{},'cleanup':{}}

def write(name,value):
    (OUT/name).write_text(json.dumps(value,indent=2,default=str)+'\n')
def bi(action,name=None):
    args=['/opt/superset/.venv/bin/python',str(Path(__file__).with_name('bi_phase.py')),action]+([name] if name else [])
    output=subprocess.check_output(args,cwd=ROOT,text=True)
    value=json.loads(output);write('bi-'+action+('-'+name if name else '')+'.json',value);return value

def sql(db,statement):
    return subprocess.check_output(['runuser','-u','postgres','--','psql','-X','-At','-v','ON_ERROR_STOP=1','-d',db,'-c',statement],cwd='/').decode().strip()
def pause(name):subprocess.run(deploy._airflow_cli_command('dags','pause',name),check=True)
def wait(run_id):
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        row=state.get_run(run_id)
        if row and row['status']!='running':return row['status']
        time.sleep(3)
    raise TimeoutError('Pipeline run '+str(run_id))
def run(p,expected,warehouse,label,want='ok'):
    run_id=state.start_run('data',p.name);deploy.trigger_dag(p.name,run_id)
    status=wait(run_id);events=state.events_for(run_id)
    write(p.name+'-'+label+'-journal.json',[dict(e) for e in events])
    if status!=want:raise AssertionError('Unexpected pipeline status: '+p.name+' '+status+' expected '+want)
    result={'run_id':run_id,'status':status}
    if status=='ok':
        cmp=fixture.compare_all(expected,warehouse,p.warehouse.schema)
        result['comparison']={'ok':cmp.ok,'detail':cmp.detail}
        if not cmp.ok:raise AssertionError('Independent warehouse expected mismatch')
    return result

def main():
    pipelines=[];sources=[];deployed=[]
    warehouse=pg_throwaway.ThrowawayDB('localhost','5432','warehouse','dbt_user',os.environ['DBT_DB_PASSWORD'])
    with contextlib.ExitStack() as stack:
      try:
        work=OUT/'pipeline-sources';work.mkdir()
        for item in CONFIG:
            name=item['pipeline'];source=ROOT/'pipelines'/name
            if source.joinpath('dwh_dbt').exists():p,fx,ex=matrix._hg_pipeline(work,source=source)
            else:
                target=work/name;shutil.copytree(source,target,ignore=shutil.ignore_patterns('.approved.yaml','.synth-validation.yaml','build'))
                for k in ('M25_ODOO_HOST','M25_ODOO_NAME','M25_ODOO_USER','M25_ODOO_PASSWORD','M25_WH_USER','M25_WH_PASSWORD'):os.environ.setdefault(k,'unused')
                p=loader.load(name,work);fx=p.root/'fixture.yaml';ex=p.root/'expected.yaml'
            if item.get('schedule'):
                path=p.root/'pipeline.yaml';doc=yaml.safe_load(path.read_text());doc['schedule']=item['schedule'];path.write_text(yaml.safe_dump(doc,sort_keys=False));p=loader.load(name,work)
            report,result,eid=evidence.run_and_seal(p,validate.validate_pipeline(p),fx,ex)
            write(name+'-validation.json',result)
            if result['overall']!='pass' or result['cleanup']['overall']!='pass':raise AssertionError('Pipeline validation failed')
            approval.promote(p,'disposable-integration-acceptance',fixture_path=fx,expected_path=ex,evidence_id=eid)
            p=loader.load(name,work)
            src=stack.enter_context(pg_throwaway.throwaway_database(prefix=item['source_prefix']));sources.append(src)
            fixture.seed_source(fixture.load_fixture(fx),src)
            overrides={**fixture.env_overrides_for_source(p,src),**fixture.env_overrides_for_warehouse(p,warehouse)}
            with fixture._temporarily(overrides):
                deploy.deploy(p);deployed.append(p);deploy.unpause_dag(p.name)
                record={'first_run':run(p,fixture.load_expected(ex),warehouse,'first')}
                if item.get('schedule'):
                    deadline=time.monotonic()+240
                    while time.monotonic()<deadline:
                        raw=sql('airflow_meta',"SELECT coalesce(json_agg(x),'[]'::json) FROM (SELECT run_id,state FROM dag_run WHERE dag_id='"+p.name+"' AND run_type='scheduled' AND state='success') x")
                        scheduled=json.loads(raw)
                        if scheduled:record['scheduled_runs']=scheduled;break
                        time.sleep(5)
                    else:raise AssertionError('No real successful scheduled DAG run observed')
                record['second_run']=run(p,fixture.load_expected(ex),warehouse,'second')
                if item.get('schedule'):pause(p.name)
                published=deploy.SHARED_PIPELINES_DIR/p.name
                diffs=[rel for rel in approval.hashed_paths(p) if matrix._without_label(rel,(p.root/rel).read_bytes())!=matrix._without_label(rel,(published/rel).read_bytes())]
                if diffs:raise AssertionError('Published artifact differs: '+repr(diffs))
                record['published_files_checked']=len(approval.hashed_paths(p));REPORT['projects'][p.name]=record
            pipelines.append((item,p,src,fx,ex))
        # Schemas have been created by the real pipeline creator before reader grants.
        subprocess.run(['dpagent','spec',str(ROOT/'tests/data_platform_acceptance/stack-bi.yaml'),'--yes','--allow-draft','--no-test'],check=True)
        bi('deploy');initial=bi('verify')
        for item,p,src,fx,ex in pipelines:
            if item.get('fault_sql'):
                before=approval.approval_path(p).read_bytes();sql(src.database,item['fault_sql'])
                bad=run(p,fixture.load_expected(ex),warehouse,'injected-failure',want='failed')
                events=state.events_for(bad['run_id']);parsed=[{**dict(e),'stage':json.loads(e['data_json'] or '{}').get('stage')} for e in events]
                if not any(e['kind']=='transform.start' and e['stage']==item['failing_stage'] for e in parsed):raise AssertionError('Failing stage did not start')
                if not any(e['kind']=='dbt.test.failed' and e['stage']==item['failing_stage'] and item['unique_model'] in e['message'] for e in parsed):raise AssertionError('Expected unique failure not observed')
                if any(e['kind']=='transform.start' and e['stage']==item['blocked_stage'] for e in parsed):raise AssertionError('Downstream transform incorrectly started')
                if approval.approval_path(p).read_bytes()!=before:raise AssertionError('Data failure changed content approval')
                sql(src.database,item['restore_sql']);REPORT['projects'][p.name]['failure']=bad
                REPORT['projects'][p.name]['recovery']=run(p,fixture.load_expected(ex),warehouse,'recovery')
                recovered=bi('verify')
                if {n:v['ids'] for n,v in recovered.items()}!={n:v['ids'] for n,v in initial.items()}:raise AssertionError('BI identities changed after dbt recreate')
        # Scoped removal must preserve the other project's running service/query.
        first=pipelines[0][1];bi('undeploy',CONFIG[0]['bi']);deploy.undeploy(first);deployed.remove(first)
        bi('verify',CONFIG[1]['bi'])
        REPORT['scoped_undeploy']='pass';REPORT['status']='pass'
      finally:
        # Preserve failed reports/journals; attempt only receipt-owned cleanup.
        if Path('/opt/superset/.venv/bin/python').exists():
            try:bi('undeploy');REPORT['cleanup']['bi']='pass'
            except Exception as exc:REPORT['cleanup']['bi']=type(exc).__name__;REPORT['status']='fail'
        for p in reversed(deployed):
            try:
                undone=deploy.undeploy(p);ok,detail=fixture._verify_cleanup_complete(p,undone,deploy)
                REPORT['cleanup'][p.name]={'ok':ok,'detail':detail}
                if not ok:REPORT['status']='fail'
            except Exception as exc:REPORT['cleanup'][p.name]={'error':type(exc).__name__};REPORT['status']='fail'
        for _,p,_,_,_ in pipelines:
            if p.bronze_staging:
                bronze.purge_namespace(p,p.bronze.prefix);remaining=bronze.count_namespace(p,p.bronze.prefix)
                REPORT['cleanup']['bronze_remaining']=remaining
                if remaining:REPORT['status']='fail'
        write('summary.json',REPORT)
    REPORT['cleanup']['source_databases_removed']=all(s.cleanup_ok for s in sources)
    if not REPORT['cleanup']['source_databases_removed']:REPORT['status']='fail'
    write('summary.json',REPORT)
    if REPORT['status']!='pass':raise AssertionError('Composed acceptance not complete')
if __name__=='__main__':
    try:main()
    except Exception:
        REPORT['status']='fail';write('summary.json',REPORT);raise
