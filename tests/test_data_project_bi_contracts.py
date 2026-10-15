"""Project expectations come from source fixtures, not the actual warehouse."""
import importlib.util
from decimal import Decimal
from pathlib import Path

import yaml

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('project_bi',ROOT/'packs/superset-assets/assets.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

def test_hg_bi_measures_match_independent_pipeline_expected():
    project=a.read_project(yaml.safe_load((ROOT/'projects/hg-usage/bi.yaml').read_text()),{})
    rows=yaml.safe_load((ROOT/'pipelines/hg_resources_useage_number/expected.yaml').read_text())['rows']
    def measures(selected):return {'rows':len(selected),'resource_video_pairs':len({r['resource_video_sk'] for r in selected}),'stock_resources':len({r['hg_stock_id'] for r in selected}),'videos':len({r['video_id'] for r in selected})}
    check=project['expected']['checks'][0]
    assert check['rows']==[measures(rows)]
    assert check['filters'][0]['rows']==[measures([r for r in rows if r['video_id']=='V1'])]

def test_monthly_bi_expected_comes_from_source_arithmetic():
    project=a.read_project(yaml.safe_load((ROOT/'projects/monthly-sales/bi.yaml').read_text()),{})
    source=yaml.load((ROOT/'pipelines/m25_monthly_sales/fixture.yaml').read_text(),Loader=a.ExpectedLoader)['tables'][0]['rows']
    totals={}
    for row in source:
        month=row['write_date'][:7];totals[month]=totals.get(month,Decimal(0))+row['amount_total']
    check=project['expected']['checks'][0]
    assert a.same_rows(check['rows'],[{'month':month,'revenue':total} for month,total in totals.items()])
    assert check['filters'][0]['rows']==[{'month':'2026-01','revenue':Decimal('350.50')}]

def test_projects_use_distinct_schemas_with_same_pack_contract():
    projects=[a.read_project(yaml.safe_load((ROOT/'projects'/name/'bi.yaml').read_text()),{}) for name in ('hg-usage','monthly-sales')]
    assert [next(iter(p['resources']['datasets'].values()))['schema'] for p in projects]==['gold','m25_monthly_sales']
    assert not (set(k for values in projects[0]['resources'].values() for k in values)&set(k for values in projects[1]['resources'].values() for k in values))


def _journal_checker():
    import sys
    sys.path.insert(0,str(ROOT/'tests/m25_acceptance'))
    spec=importlib.util.spec_from_file_location('platform_acceptance',ROOT/'tests/data_platform_acceptance/run.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.assert_failure_journal


def test_failure_journal_requires_exact_run_model_and_no_downstream_start():
    import json,pytest
    check=_journal_checker()
    config={'failing_stage':'silver','blocked_stage':'gold','unique_model':'key','failing_model':'fact'}
    events=[{'run_id':123,'kind':'transform.start','message':'p/silver via dbt','data_json':None},
      {'run_id':123,'kind':'dbt.test.failed','message':'failure','data_json':json.dumps({'stage':'silver','tests':[{'name':'unique','column':'key','models':['fact'],'status':'fail'}]})}]
    check(events,123,'p',config)
    with pytest.raises(AssertionError):check(events,124,'p',config)
    with pytest.raises(AssertionError):check(events+[{'run_id':123,'kind':'transform.start','message':'p/gold via dbt','data_json':None}],123,'p',config)
    wrong=[events[0],{**events[1],'data_json':json.dumps({'stage':'silver','tests':[{'name':'unique','column':'key','models':['other'],'status':'fail'}]})}]
    with pytest.raises(AssertionError):check(wrong,123,'p',config)
