"""Artifact safety, exact comparison and rollback: no Superset instance required."""
import copy
import importlib.util
import json
import uuid
import zipfile
import io
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

ROOT=Path(__file__).parents[1]


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


assets=load(ROOT/'packs/superset-assets/assets.py','bi_assets')
fixture=load(ROOT/'tests/bi_acceptance/native_fixture.py','bi_native_fixture')


@pytest.fixture
def project(tmp_path,monkeypatch):
    monkeypatch.setattr(assets,'ROOT',tmp_path)
    monkeypatch.setattr(assets,'STATE',tmp_path/'state')
    directory=tmp_path/'projects'/'retail'
    fixture.create(directory,'retail','public','orders_gold')
    config={'name':'retail','bundle_dir':'projects/retail/bundle','expected_file':'projects/retail/expected.yaml'}
    return assets.read_project(config,{'environment':'unit'})


def test_numeric_comparison_does_not_round_large_or_precise_values():
    assert not assets.same_rows([{'n':10**100+1}],[{'n':10**100+2}])
    assert not assets.same_rows([{'n':Decimal('0.1234567890123456789012345678901')}],[{'n':Decimal('0.1234567890123456789012345678902')}])
    assert assets.same_rows([{'n':Decimal('1.00')}],[{'n':1}])
    assert assets.same_rows([{'n':Decimal('-0.00')}],[{'n':0}])


@pytest.mark.parametrize('actual,expected',[(True,1),(False,0),('1',1),('',None)])
def test_values_remain_typed(actual,expected):
    assert not assets.same_rows([{'v':actual}],[{'v':expected}])


def test_rows_are_bags_not_sets():
    assert not assets.same_rows([{'v':1},{'v':1}],[{'v':1}])
    assert assets.same_rows([{'v':1},{'v':2}],[{'v':2},{'v':1}])


def test_approval_changes_with_input_or_environment(project):
    changed=dict(project['config'],name='other-project')
    assert assets.read_project(changed,project['binding'])['fingerprint']!=project['fingerprint']
    assert assets.read_project(project['config'],{'environment':'other'})['fingerprint']!=project['fingerprint']
    path=assets.safe_path(project['config']['expected_file'])
    path.write_text(path.read_text().replace('total: 30','total: 31'))
    assert assets.read_project(project['config'],project['binding'])['fingerprint']!=project['fingerprint']


def test_expected_decimal_literal_is_not_converted_to_binary_float(project):
    path=assets.safe_path(project['config']['expected_file'])
    path.write_text(path.read_text().replace('total: 30','total: 0.1234567890123456789012345678901'))
    parsed=assets.read_project(project['config'],project['binding'])
    assert parsed['expected']['checks'][0]['rows'][0]['total']==Decimal('0.1234567890123456789012345678901')


@pytest.mark.parametrize('raw',['/etc/passwd','projects/../../outside','other/project'])
def test_paths_cannot_escape_project_directory(tmp_path,monkeypatch,raw):
    monkeypatch.setattr(assets,'ROOT',tmp_path)
    with pytest.raises(ValueError): assets.safe_path(raw)


def test_symlink_is_refused_before_read(project,tmp_path):
    target=tmp_path/'private';target.write_text('private sentinel')
    (assets.safe_path(project['config']['bundle_dir'])/'leak.yaml').symlink_to(target)
    with pytest.raises(ValueError,match='symlink'):assets.read_project(project['config'],project['binding'])


@pytest.mark.parametrize('mutation',[{'allow_dml':True},{'password':'embedded-password'},{'extra':{'engine_params':{'connect_args':{'user':'writer'}}}},{'ssh_tunnel':{'server_address':'foreign'}}])
def test_artifact_cannot_override_readonly_environment_connection(project,mutation):
    path=assets.safe_path(project['config']['bundle_dir'])/'databases/warehouse.yaml'
    item=yaml.safe_load(path.read_text());item.update(mutation);path.write_text(yaml.safe_dump(item))
    with pytest.raises(ValueError):assets.read_project(project['config'],project['binding'])


class FakeClient:
    def __init__(self,binding):
        self.env={'SUPERSET_WAREHOUSE_URI':'postgresql://reader:unit-placeholder@localhost/warehouse'}
        self.binding=binding;self.store={k:{} for k in assets.KINDS};self.next=1;self.imports=0;self.fail_live=False;self.leak=False
    def lookup(self,resources,mapping=None):
        return {k:{(mapping or {}).get(key,key):self.store[k][(mapping or {}).get(key,key)][0] for key in values if (mapping or {}).get(key,key) in self.store[k]} for k,values in resources.items()}
    def import_bundle(self,data):
        self.imports+=1
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            docs={name.split('/',1)[1]:yaml.safe_load(archive.read(name)) for name in archive.namelist()}
        for kind in assets.KINDS:
            for path,doc in docs.items():
                if path.startswith(kind+'/'):
                    key=doc['uuid'];old=self.store[kind].get(key)
                    self.store[kind][key]=(old[0] if old else self.next,copy.deepcopy(doc))
                    if not old:self.next+=1
    def cleanup(self,resources,mapping=None,receipt=None):
        for kind,values in resources.items():
            for key in values:
                actual=(mapping or {}).get(key,key)
                if not self.leak:self.store[kind].pop(actual,None)
        if any(self.lookup(resources,mapping).values()):raise RuntimeError('cleanup leak')
    def rows(self,filtered):
        return [{'region':'north','n':2,'total':30}] if filtered else [{'region':'north','n':2,'total':30},{'region':'south','n':1,'total':40}]
    def chart(self,identifier,filters=None,dataset_id=None):
        chart=next(item for item in self.store['charts'].values() if item[0]==identifier)
        dataset=self.store['datasets'][chart[1]['dataset_uuid']][1]
        database=self.store['databases'][dataset['database_uuid']][1]
        if self.fail_live and '-validate-' not in database['database_name']:
            self.fail_live=False
            raise RuntimeError('injected live import failure')
        return self.rows(bool(filters))
    def query(self,database,statement,expected_count):return self.rows(' WHERE ' in statement)
    def filter_column(self,*args):return 'region'
    def assert_names_owned(self,*args):pass


def test_validation_removes_its_clones_and_records_independent_comparison(project):
    client=FakeClient(project['binding'])
    report=assets.validate(client,project)
    assert report['status']=='pass' and report['cleanup']=='pass'
    assert all(o['matched'] for o in report['observations'])
    assert not any(client.store.values())
    assert not assets.deployment_path(project).exists()


def test_bad_expected_fails_and_cleans_without_creating_approval(project):
    client=FakeClient(project['binding'])
    project['expected']['checks'][0]['rows'][0]['total']=31
    with pytest.raises(assets.ComparisonFailure):assets.validate(client,project)
    record=json.loads((assets.STATE/project['name']/'validation.json').read_text())
    assert record['status']=='fail' and record['cleanup']=='pass'
    assert record['observations'] and not record['observations'][0]['matched']
    assert not any(client.store.values()) and not assets.deployment_path(project).exists()


def test_cleanup_failure_never_becomes_a_pass(project):
    client=FakeClient(project['binding']);client.leak=True
    with pytest.raises(RuntimeError,match='cleanup leak'):assets.validate(client,project)
    record=json.loads((assets.STATE/project['name']/'validation.json').read_text())
    assert record['status']=='fail' and record['cleanup']=='fail'
    assert record['uuid_map']


def test_unapproved_deploy_has_no_remote_mutation(project):
    client=FakeClient(project['binding'])
    with pytest.raises(RuntimeError,match='approval'):assets.deploy(client,project,'operator')
    assert client.imports==0 and not assets.deployment_path(project).exists()


def test_redeploy_keeps_uuid_ids_and_foreign_uuid_cannot_be_adopted(project):
    client=FakeClient(project['binding']);project['config']['approved_sha256']=project['fingerprint']
    first=assets.deploy(client,project,'operator')
    assert assets.deploy(client,project,'operator')==first
    other=copy.deepcopy(project);other['name']='foreign'
    with pytest.raises(RuntimeError,match='unowned'):assets.deploy(client,other,'operator')
    assert client.lookup(project['resources'])==first


def test_stale_receipt_refuses_all_deletions_before_touching_remote_objects(project):
    client=assets.Client.__new__(assets.Client)
    ids={k:{key:1 for key in values} for k,values in project['resources'].items()}
    client.lookup=lambda *args:ids
    client.assert_no_foreign_references=lambda *args:None
    calls=[];client.request=lambda *args:calls.append(args)
    stale=copy.deepcopy(ids);stale['charts'][next(iter(stale['charts']))]=99
    with pytest.raises(RuntimeError,match='unowned'):client.cleanup(project['resources'],receipt=stale)
    assert calls==[]


def test_api_endpoint_cannot_diverge_from_the_managed_metadata_instance(tmp_path):
    unit=tmp_path/'superset.service'
    unit.write_text('[Service]\nExecStart=/opt/superset/.venv/bin/gunicorn --bind 127.0.0.1:8088 superset.app:create_app()\n')
    assets.assert_managed_endpoint('http://127.0.0.1:8088',unit)
    for value in ('https://foreign.example:8088','http://127.0.0.1:9999','http://user:secret@127.0.0.1:8088'):
        with pytest.raises(ValueError):assets.assert_managed_endpoint(value,unit)


def test_failed_live_update_restores_previous_owned_configuration(project):
    client=FakeClient(project['binding']);project['config']['approved_sha256']=project['fingerprint']
    before=assets.deploy(client,project,'operator')
    receipt=assets.deployment_path(project).read_bytes()
    updated=copy.deepcopy(project)
    updated['content']['charts/totals.yaml']['slice_name']='Changed title'
    updated['fingerprint']='f'*64;updated['config']['approved_sha256']=updated['fingerprint']
    client.fail_live=True
    with pytest.raises(RuntimeError,match='injected live'):assets.deploy(client,updated,'operator')
    assert assets.deployment_path(project).read_bytes()==receipt
    assert client.lookup(project['resources'])==before
    chart=next(iter(client.store['charts'].values()))[1]
    assert chart['slice_name']=='Independent fixture totals'


def test_undeploy_one_project_keeps_the_other_project(project):
    directory=assets.ROOT/'projects'/'other'
    fixture.create(directory,'other','public','other_gold')
    config={'name':'other','bundle_dir':'projects/other/bundle','expected_file':'projects/other/expected.yaml'}
    other=assets.read_project(config,project['binding'])
    client=FakeClient(project['binding'])
    for item in (project,other):
        item['config']['approved_sha256']=item['fingerprint'];assets.deploy(client,item,'operator')
    before=client.lookup(other['resources'])
    assets.undeploy(client,project['name'])
    assert not any(client.lookup(project['resources']).values())
    assert client.lookup(other['resources'])==before
    assets.check_results(client,other)


def test_shipped_bi_suite_has_real_negative_and_scoped_cleanup_checks():
    from dpagent.suites.loader import load
    suite=load('superset-assets')
    assert len(suite.checks)==5
    assert {'reject-unapproved','reject-bad-expectation'} <= {c.id for c in suite.checks if c.negative}
    assert suite.setup and suite.teardown
