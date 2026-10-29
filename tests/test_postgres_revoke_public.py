"""postgres pack, `revoke_public_connect`: when on, PUBLIC loses CONNECT/TEMP on every database the pack lists; the
explicit grants to the listed roles stay. Off (the default), the generated SQL is unchanged. Found by the ops
acceptance: the warehouse writer could connect to the Airflow and Superset metadata databases."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

STEP = Path(__file__).resolve().parents[1] / "packs" / "postgres" / "steps" / "50-databases.sh"


def generate(tmp_path, revoke: str | None) -> str:
    text = STEP.read_text()
    code = re.search(r"python3 - \"\$SQL_FILE\" <<'PY'\n(.*?)\nPY\n", text, re.S).group(1)
    env = {**os.environ, "DP_DATABASES": '["warehouse", "airflow_meta"]',
           "DP_USERS": '[{"name": "dbt_user", "password": "x", "databases": ["warehouse"]}]'}
    if revoke is not None:
        env["DP_REVOKE_PUBLIC"] = revoke
    out = tmp_path / "out.sql"
    subprocess.run([sys.executable, "-c", code, str(out)], env=env, check=True)
    return out.read_text()


def test_revoke_on(tmp_path):
    sql = generate(tmp_path, "true")
    assert 'REVOKE CONNECT, TEMPORARY ON DATABASE "warehouse" FROM PUBLIC;' in sql
    assert 'REVOKE CONNECT, TEMPORARY ON DATABASE "airflow_meta" FROM PUBLIC;' in sql
    assert 'GRANT ALL PRIVILEGES ON DATABASE "warehouse" TO "dbt_user";' in sql


@pytest.mark.parametrize("value", [None, "false"])
def test_revoke_off_changes_nothing(tmp_path, value):
    assert "REVOKE" not in generate(tmp_path, value)


def test_the_demo_stack_turns_it_on():
    assert "revoke_public_connect: true" in (STEP.parents[3] / "examples" / "demo" / "stack.yaml").read_text()
