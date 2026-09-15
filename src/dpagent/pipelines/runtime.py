"""The gate/transform execution engine - what a deployed DAG's tasks actually
call. Not built yet: `dpagent pipeline deploy` already generates DAGs that
import this module and call these three functions (see `deploy.render_dag`),
so the DAG's *structure* is reviewable and correct today, and it becomes
runnable the moment this file's bodies are, with no regeneration needed.

Each function's job, once implemented:
  run_extract   - invoke the dlt pack's source for this pipeline, load into
                  the warehouse's landing tables.
  run_transform - dbt run --select <models>, or psql -f <procedure>, per the
                  stage's declared engine (generator.dag_tasks already picked
                  the right one per stage).
  run_gate      - execute generator.compile_gate()'s queries for the stage
                  against the warehouse, quarantine failing rows where the
                  gate is row-level, record the verdict in stage_runs/
                  gate_runs, and raise if the quarantine's reject_threshold_pct
                  is exceeded (docs/layer2.md, "Failure semantics").
"""
from __future__ import annotations


def run_extract(*, pipeline_name: str) -> None:
    raise NotImplementedError(
        f"run_extract({pipeline_name!r}): the dlt pack this calls into does "
        f"not exist yet - see docs/layer2.md's 'In scope (MVP)'")


def run_transform(*, pipeline_name: str, stage: str) -> None:
    raise NotImplementedError(
        f"run_transform({pipeline_name!r}, {stage!r}): not built yet")


def run_gate(*, pipeline_name: str, stage: str) -> None:
    raise NotImplementedError(
        f"run_gate({pipeline_name!r}, {stage!r}): not built yet - "
        f"generator.compile_gate() already has the SQL this will execute")
