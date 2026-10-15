import subprocess,tempfile
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='dpagent-runtime-observe-') as tmp:
 p=Path(tmp);(p/'macros').mkdir()
 (p/'dbt_project.yml').write_text("name: platform_runtime_observation\nversion: '1.0'\nconfig-version: 2\nprofile: default\n")
 (p/'macros/observe.sql').write_text('''{% macro platform_runtime_observation() %}
{% set result = run_query("select current_user, current_setting('TimeZone'), current_schema()") %}
{{ log('PLATFORM_DBT_OBSERVATION=' ~ tojson({'user':result.rows[0][0], 'session_timezone':result.rows[0][1], 'schema':result.rows[0][2]}), info=True) }}
{% endmacro %}''')
 subprocess.run(['/opt/dbt/.venv/bin/dbt','--no-use-colors','run-operation','platform_runtime_observation','--project-dir',tmp,'--profiles-dir','/opt/dbt/profiles'],check=True)
