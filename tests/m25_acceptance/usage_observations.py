"""Acceptance-only observations, queried through a real dbt adapter session.

The audit project is temporary and separate from the approved HG project. It uses
the same adapter/profile connection values as the warehouse being compared.
No HG models, fixtures, expected results or shared runtime behavior are changed.
"""
import json
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from dpagent.pipelines import fixture, loader, runtime

MARKER = "DPAGENT_USAGE_SESSION="


def observe(warehouse, source):
    pipeline = loader.load(source.name, source.parent)
    pipeline = replace(pipeline, warehouse=replace(
        pipeline.warehouse, host=warehouse.host, port=warehouse.port,
        database=warehouse.database, user=warehouse.user, password=warehouse.password))
    query = """select json_build_object(
      'database', current_database(), 'user', current_user,
      'timezone', current_setting('TimeZone'),
      'seed_rows', (select count(*) from silver.manual_excluded_stock_codes),
      'excluded_rows', (select count(*) from silver.int_excluded_stock_codes),
      'timestamp_types', (select json_object_agg(table_schema || '.' || table_name || '.' || column_name, data_type)
        from information_schema.columns where
        (table_schema='staging' and table_name='channel_video_info' and column_name='PublishedAt') or
        (table_schema='silver' and table_name='dim_video' and column_name='published_date') or
        (table_schema='gold' and table_name='resources_useage_number' and column_name='published_date')),
      'landing_columns', (select json_object_agg(table_name, columns) from
        (select table_name, json_object_agg(column_name, data_type) as columns
         from information_schema.columns where table_schema='staging'
         group by table_name) t),
      'gold_rows', (select json_agg(t) from
        (select * from gold.resources_useage_number order by video_id, first_position, hg_stock_id) t),
      'utc_cast_probe', ('2024-01-01T00:00:00+00'::timestamptz::timestamp)::text
    )::text"""
    with tempfile.TemporaryDirectory(prefix="dpagent_usage_audit_") as tmp:
        project = Path(tmp)
        (project / "macros").mkdir()
        (project / "dbt_project.yml").write_text(
            "name: usage_session_audit\nversion: '1.0.0'\nconfig-version: 2\n"
            + "profile: " + runtime._dbt_profile_name() + "\nmacro-paths: [macros]\n")
        macro = ("{% macro usage_session_audit() %}{% if execute %}"
                 + "{% set rows = run_query(" + json.dumps(query) + ") %}"
                 + "{{ log('" + MARKER + "' ~ rows.columns[0].values()[0], info=True) }}"
                 + "{% endif %}{% endmacro %}")
        (project / "macros" / "audit.sql").write_text(macro)
        with runtime._dbt_profiles_dir(pipeline) as profiles:
            proc = subprocess.run(
                [runtime._dbt_bin(), "--no-use-colors", "run-operation", "usage_session_audit",
                 "--project-dir", str(project), "--profiles-dir", str(profiles)],
                capture_output=True, text=True, timeout=120)
        if proc.returncode:
            raise RuntimeError("dbt session audit failed: " + (proc.stderr or proc.stdout)[-2000:])
        records = [json.loads(line.split(MARKER, 1)[1]) for line in proc.stdout.splitlines()
                   if MARKER in line]
        if len(records) != 1:
            raise RuntimeError("dbt session audit did not emit exactly one observation")
        return records[0]


def problems(observations, source, required):
    pipeline = loader.load(source.name, source.parent)
    contract = next(g.params["tables"] for g in pipeline.stages[0].gates
                    if g.type == "schema_contract")
    seed_count = len((source / "dwh_dbt/seeds/manual_excluded_stock_codes.csv").read_text().splitlines()) - 1
    errors = []
    if len(observations) != required:
        errors.append(f"expected {required} dbt session observations, got {len(observations)}")
    expected_types = {"staging.channel_video_info.PublishedAt": "timestamp with time zone",
                      "silver.dim_video.published_date": "timestamp without time zone",
                      "gold.resources_useage_number.published_date": "timestamp without time zone"}
    for index, row in enumerate(observations, 1):
        if row.get("seed_rows") != seed_count or seed_count != 5787 or row.get("excluded_rows") != seed_count:
            errors.append(f"observation {index}: seed/exclusion row count mismatch")
        if row.get("timezone") not in ("UTC", "Etc/UTC") or row.get("utc_cast_probe") != "2024-01-01 00:00:00":
            errors.append(f"observation {index}: dbt session is not proven UTC: {row.get('timezone')}")
        if row.get("timestamp_types") != expected_types:
            errors.append(f"observation {index}: timestamp types mismatch")
        actual = row.get("landing_columns") or {}
        for table, columns in contract.items():
            if any((actual.get(table) or {}).get(name) != kind for name, kind in columns.items()):
                errors.append(f"observation {index}: Bronze CamelCase/type mismatch for {table}")
    return errors
