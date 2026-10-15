"""Synthetic native assets; expected totals are literals independent of query output."""
import json
import uuid
from pathlib import Path

import yaml


def create(directory: Path, name: str, schema: str, table: str, dimension="region", measure="amount"):
    keys={kind:str(uuid.uuid5(uuid.NAMESPACE_URL,"dpagent-fixture:"+name+":"+kind)) for kind in ("database","dataset","chart","dashboard")}
    metric_names=["n","total"]
    params={"datasource":"0__table","slice_id":100,"viz_type":"table","query_mode":"aggregate","groupby":[dimension],"metrics":metric_names,"adhoc_filters":[],"row_limit":1000,"order_desc":False,"show_cell_bars":False}
    query={"datasource":{"id":0,"type":"table"},"force":True,"queries":[{"columns":[dimension],"metrics":metric_names,"filters":[],"time_range":"No filter","orderby":[],"row_limit":1000,"extras":{"having":"","where":""}}],"form_data":params,"result_format":"json","result_type":"full"}
    position={"DASHBOARD_VERSION_KEY":"v2","ROOT_ID":{"id":"ROOT_ID","type":"ROOT","children":["GRID_ID"]},"GRID_ID":{"id":"GRID_ID","type":"GRID","children":["ROW-1"],"parents":["ROOT_ID"]},"ROW-1":{"id":"ROW-1","type":"ROW","children":["CHART-1"],"parents":["ROOT_ID","GRID_ID"],"meta":{"background":"BACKGROUND_TRANSPARENT"}},"CHART-1":{"id":"CHART-1","type":"CHART","children":[],"parents":["ROOT_ID","GRID_ID","ROW-1"],"meta":{"chartId":100,"uuid":keys["chart"],"width":12,"height":50,"sliceName":"Independent fixture totals"}}}
    native_filter={"id":"NATIVE_FILTER-dimension","name":dimension.title(),"filterType":"filter_select","targets":[{"datasetUuid":keys["dataset"],"column":{"name":dimension}}],"defaultDataMask":{"extraFormData":{},"filterState":{},"ownState":{}},"cascadeParentIds":[],"scope":{"rootPath":["ROOT_ID"],"excluded":[]},"controlValues":{"enableEmptyFilter":False,"multiSelect":True,"searchAllOptions":False,"inverseSelection":False},"type":"NATIVE_FILTER"}
    configs={
      "metadata.yaml":{"version":"1.0.0","type":"assets","timestamp":"2026-01-01T00:00:00+00:00"},
      "databases/warehouse.yaml":{"database_name":"Warehouse","sqlalchemy_uri":"postgresql://","uuid":keys["database"],"version":"1.0.0","expose_in_sqllab":True,"allow_dml":False,"allow_ctas":False,"allow_cvas":False,"allow_file_upload":False,"extra":{}},
      "datasets/warehouse/data.yaml":{"table_name":table,"schema":schema,"uuid":keys["dataset"],"database_uuid":keys["database"],"version":"1.0.0","filter_select_enabled":True,"columns":[{"column_name":dimension,"type":"TEXT","groupby":True,"filterable":True,"is_dttm":False},{"column_name":measure,"type":"INTEGER","groupby":True,"filterable":True,"is_dttm":False}],"metrics":[{"metric_name":"n","expression":"COUNT(*)","metric_type":"count"},{"metric_name":"total","expression":"SUM(\""+measure+"\")","metric_type":"sum"}]},
      "charts/totals.yaml":{"slice_name":"Independent fixture totals","viz_type":"table","params":params,"query_context":json.dumps(query),"uuid":keys["chart"],"dataset_uuid":keys["dataset"],"version":"1.0.0"},
      "dashboards/overview.yaml":{"dashboard_title":"Fixture overview","slug":"overview","uuid":keys["dashboard"],"version":"1.0.0","position":position,"metadata":{"native_filter_configuration":[native_filter],"color_scheme":"supersetColors","refresh_frequency":0,"timed_refresh_immune_slices":[],"expanded_slices":{},"cross_filters_enabled":False}},
    }
    bundle=directory/"bundle"
    for relative,value in configs.items():
        path=bundle/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(yaml.safe_dump(value,sort_keys=False))
    table_sql='"'+schema.replace('"','""')+'"."'+table.replace('"','""')+'"'
    statement='SELECT "'+dimension+'", count(*)::integer AS n, sum("'+measure+'")::integer AS total FROM '+table_sql
    # This independent expectation is deliberately not computed from database/chart output.
    quoted_dimension='"'+dimension.replace('"','""')+'"'
    filtered_sql=statement+' WHERE '+quoted_dimension+" = 'north' GROUP BY "+quoted_dimension
    expected={"checks":[{"chart_uuid":keys["chart"],"sql":statement+' GROUP BY '+quoted_dimension,
        "rows":[{dimension:"north","n":2,"total":30},{dimension:"south","n":1,"total":40}],
        "filters":[{"dashboard_uuid":keys["dashboard"],"native_filter_id":"NATIVE_FILTER-dimension",
            "values":["north"],"sql":filtered_sql,"rows":[{dimension:"north","n":2,"total":30}]}]}]}
    (directory/"expected.yaml").write_text(yaml.safe_dump(expected,sort_keys=False))
    return keys
