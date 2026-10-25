"""`retries:` in pipeline.yaml -> Airflow default_args on every task; absent -> the DAG is byte-for-byte unchanged."""
import ast
import shutil
from pathlib import Path

import pytest

from dpagent.pipelines import deploy, loader

REPO = Path(__file__).resolve().parents[1]


def _pipeline(tmp_path, extra: str):
    work = tmp_path / "quickstart_sales"
    shutil.copytree(REPO / "pipelines" / "quickstart_sales", work)
    (work / "pipeline.yaml").write_text((work / "pipeline.yaml").read_text() + extra)
    return loader.load("quickstart_sales", tmp_path)


def test_retries_reach_every_task_and_the_dag_compiles(tmp_path):
    p = _pipeline(tmp_path, "\nretries: {count: 2, delay_seconds: 30}\n")
    src = deploy.render_dag(p)
    ast.parse(src)
    assert 'default_args={"retries": 2, "retry_delay": timedelta(seconds=30)},' in src
    assert "from datetime import timedelta" in src and src.index("default_args") < src.index(") as dag:")


def test_no_retries_leaves_the_dag_unchanged(tmp_path):
    base = deploy.render_dag(loader.load("quickstart_sales", REPO / "pipelines"))
    assert "default_args" not in base and "timedelta" not in base
    assert deploy.render_dag(_pipeline(tmp_path, "\nretries: {count: 0}\n")) == base


@pytest.mark.parametrize("bad", ["retries: 3", "retries: {count: 9}", "retries: {count: 1, delay_seconds: 0}",
                                 "retries: {count: 1, wait: 5}", "retries: {count: true}"])
def test_bad_retries_are_refused_when_loading(tmp_path, bad):
    with pytest.raises(loader.PipelineError, match="retries"):
        _pipeline(tmp_path, "\n" + bad + "\n")
