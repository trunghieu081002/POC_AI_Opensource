"""Longer lived real API probe across reinstall and rollback, on a disposable host."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

spec=importlib.util.spec_from_file_location('superset_probe',Path(__file__).parents[2]/'suites/superset/acceptance.py')
probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

def sql(database,statement):
    return subprocess.check_output(['runuser','-u','postgres','--','psql','-X','-At','-v','ON_ERROR_STOP=1','-d',database,'-c',statement],cwd='/').decode().strip()

def metadata_identity():
    username=probe.ENV['SUPERSET_ADMIN_USER'].replace("'","''")
    users=sql('superset_meta',"SELECT count(*) FROM ab_user WHERE username='"+username+"'")
    connections=sql('superset_meta',"SELECT count(*) FROM dbs WHERE database_name='"+probe.NS+"'")
    if users != '1' or connections != '1': raise AssertionError('Duplicate/missing admin or lifecycle connection')
    return {'admin_count':1,'connection_count':1,'database_id':probe.state()['database_id']}

def begin():
    probe.setup();probe.query();probe.readonly()
    # The actual dbt pack/profile uses the configured table-creating warehouse role.
    before=probe.oid()
    with tempfile.TemporaryDirectory(prefix='dpagent-superset-dbt-') as tmp:
        project=Path(tmp);(project/'models').mkdir()
        (project/'dbt_project.yml').write_text("name: superset_reader_probe\nversion: '1.0'\nconfig-version: 2\nprofile: default\nmodel-paths: [models]\nmodels:\n  superset_reader_probe:\n    +materialized: table\n")
        (project/'models'/(probe.NS+'.sql')).write_text('select 1::integer as id, 10::integer as amount union all select 2::integer, 20::integer\n')
        subprocess.run(['/opt/dbt/.venv/bin/dbt','--no-use-colors','run','--full-refresh','--project-dir',str(project),'--profiles-dir','/opt/dbt/profiles'],check=True,cwd='/')
    after=probe.oid()
    if before == after: raise AssertionError('dbt did not recreate the table')
    probe.query();probe.readonly()
    identity=metadata_identity()
    Path('/root/superset-lifecycle-before.json').write_text(json.dumps(identity))
    print('Real dbt full-refresh changed table OID',before,'->',after,'; authenticated SELECT/write denial hold')

def after_reinstall():
    before=json.loads(Path('/root/superset-lifecycle-before.json').read_text())
    if metadata_identity() != before: raise AssertionError('Metadata identity changed on reinstall')
    probe.query();probe.readonly()
    probe.restart()
    print('Reinstall preserved one admin, one lifecycle connection and the same metadata id')

def after_rollback():
    if Path('/opt/superset/.venv').exists() or Path('/opt/superset/superset.env').exists() or Path('/etc/systemd/system/superset.service').exists():
        raise AssertionError('Rollback left a runtime/service/secret file')
    # The interpreter executing this mode is the dpagent venv, with probe deps;
    # import-time environment has already been loaded by the parent process.
    if metadata_identity() != json.loads(Path('/root/superset-lifecycle-before.json').read_text()):
        raise AssertionError('Rollback changed metadata')
    if sql('warehouse','SELECT count(*)::text||\',\'||sum(amount)::text FROM '+probe.table()) != '2,30':
        raise AssertionError('Rollback changed warehouse data')
    print('Rollback removed owned runtime and retained metadata and warehouse rows')

if __name__=='__main__':
    if sys.argv[1]=='begin': begin()
    elif sys.argv[1]=='after-reinstall': after_reinstall()
    elif sys.argv[1]=='rollback':
        # Keep probe modules/env in this process while the installed runtime is removed.
        subprocess.run(['dpagent','rollback','superset','--yes'],check=True)
        after_rollback()
        print('Owned lifecycle fixtures retained for container teardown; no database decommission during rollback')
    else: raise SystemExit('Unknown lifecycle phase')
