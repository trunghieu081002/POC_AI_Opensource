"""Turns a validated `Pipeline` into the artifacts `dpagent pipeline deploy`
would write and the DAG `dpagent pipeline run` would execute - without
writing or executing anything. This is `plan`'s entire job, same as
`--dry-run` elsewhere in this project: print every command, change nothing.

Deploy/run themselves (actually writing the DAG file, applying a procedure
migration, executing gate SQL against a real warehouse, recording
stage_runs/gate_runs) are not built yet - this module is the compiler those
will call into, not a stand-in for them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .loader import Gate, Pipeline, Stage

_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_DURATION = re.compile(r"^(\d+)([smhd])$")


def _duration_to_interval(duration: str) -> str:
    match = _DURATION.match(duration)
    n, unit = match.group(1), match.group(2)
    return f"{n} {_DURATION_UNITS[unit]}"


@dataclass
class Query:
    name: str
    sql: str


@dataclass
class CompiledGate:
    gate: Gate
    verdict: str        # how the query result(s) decide pass/fail - documentation,
                        # not executed here (that is the gate runner's job, not built yet)
    queries: list[Query]


@dataclass
class Task:
    id: str
    kind: str            # extract | transform | gate
    description: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Artifact:
    path: str
    kind: str             # dag | dbt_schema | procedure_migration
    description: str


@dataclass
class Plan:
    pipeline: Pipeline
    tasks: list[Task]
    artifacts: list[Artifact]
    gates: dict[str, list[CompiledGate]]   # stage name -> its compiled gates


def compile_gate(gate: Gate, stage: Stage) -> CompiledGate:
    if gate.type == "schema_contract":
        tables = gate["tables"]
        queries = [
            Query(f"columns_{table}",
                  f"select column_name, data_type from information_schema.columns "
                  f"where table_schema = current_schema() and table_name = '{table}'")
            for table in tables
        ]
        verdict = ("fails if any table's actual columns (name + type, after "
                   "normalising synonyms like bigint/int8) differ from the "
                   "contract declared in the manifest")

    elif gate.type == "freshness":
        col = gate["column"]
        interval = _duration_to_interval(gate["max_age"])
        queries = [
            Query(f"freshness_{table}",
                  f"select max({col}) < now() - interval '{interval}' as is_stale from {table}")
            for table in gate["tables"]
        ]
        verdict = f"fails if any table's most recent {col} is older than {gate['max_age']}"

    elif gate.type == "row_count_bounds":
        table = gate["table"]
        queries = [Query("row_count", f"select count(*) as n from {table}")]
        bounds = []
        if "min" in gate.params:
            bounds.append(f"below {gate['min']}")
        if "max" in gate.params:
            bounds.append(f"above {gate['max']}")
        if "max_delta_pct" in gate.params:
            bounds.append(
                f"more than {gate['max_delta_pct']}% different from the previous "
                f"run's count (looked up from stage_runs)")
        verdict = f"fails if {table}'s row count is " + " or ".join(bounds)

    elif gate.type == "not_null":
        table = gate["table"]
        cond = " or ".join(f"{c} is null" for c in gate["columns"])
        queries = [Query("failing_rows", f"select * from {table} where {cond}")]
        verdict = ("every row this query returns is quarantined; the stage fails "
                   "if that exceeds the quarantine threshold")

    elif gate.type == "unique":
        table = gate["table"]
        cols_csv = ", ".join(gate["columns"])
        queries = [
            Query("duplicate_keys",
                  f"select {cols_csv}, count(*) as n from {table} "
                  f"group by {cols_csv} having count(*) > 1"),
            Query("failing_rows",
                  f"select t.* from {table} t join (select {cols_csv} from {table} "
                  f"group by {cols_csv} having count(*) > 1) d using ({cols_csv})"),
        ]
        verdict = ("every row sharing a duplicate key is quarantined, including "
                   "the first occurrence - the manifest does not declare which "
                   "copy is 'correct', so none is assumed to be")

    elif gate.type == "referential_integrity":
        table, col = gate["table"], gate["column"]
        ref = gate["references"]
        ref_table, ref_col = ref["table"], ref["column"]
        queries = [Query(
            "failing_rows",
            f"select t.* from {table} t left join {ref_table} r "
            f"on t.{col} = r.{ref_col} where t.{col} is not null and r.{ref_col} is null")]
        verdict = f"rows whose {col} does not exist in {ref_table}.{ref_col} are quarantined"

    elif gate.type == "business_rule":
        queries = [Query("rule", gate["sql"])]
        verdict = (
            f"fails if the query returns any rows ({gate['expect']}); by "
            f"convention its first returned column identifies which row(s) to "
            f"quarantine - the manifest author's responsibility to get right, "
            f"same as a procedure's idempotency (docs/layer2.md, Concepts #3)")

    else:  # pragma: no cover - loader.py already rejects this at lint time
        raise ValueError(f"no SQL compiler for gate type {gate.type!r}")

    return CompiledGate(gate=gate, verdict=verdict, queries=queries)


def dag_tasks(pipeline: Pipeline) -> list[Task]:
    landing = pipeline.landing
    tasks = [
        Task(id="extract", kind="extract",
             description=f"dlt: {pipeline.source.connector} -> {landing.name}"),
        Task(id=f"gate_{landing.name}", kind="gate",
             description=f"{len(landing.gates)} gate(s) on {landing.name}",
             depends_on=["extract"]),
    ]
    previous_gate = tasks[-1].id

    for stage in pipeline.stages[1:]:
        transform_id = f"transform_{stage.name}"
        if stage.engine == "dbt":
            description = f"dbt run --select {' '.join(stage.models)}"
        else:
            description = f"CALL <procedure defined in {stage.procedure}>"
        tasks.append(Task(id=transform_id, kind="transform", description=description,
                          depends_on=[previous_gate]))

        gate_id = f"gate_{stage.name}"
        tasks.append(Task(id=gate_id, kind="gate",
                          description=f"{len(stage.gates)} gate(s) on {stage.name}"
                                      + (f", quarantine -> {stage.quarantine.table}"
                                         if stage.quarantine else ""),
                          depends_on=[transform_id]))
        previous_gate = gate_id

    return tasks


def artifacts(pipeline: Pipeline) -> list[Artifact]:
    out = [Artifact(path=f"dags/{pipeline.name}.py", kind="dag",
                    description="generated Airflow DAG - the task graph from dag_tasks()")]
    for stage in pipeline.stages[1:]:
        if stage.engine == "dbt":
            out.append(Artifact(
                path=f"models/{pipeline.name}/{stage.name}/schema.yml", kind="dbt_schema",
                description=f"dbt schema/doc block for {', '.join(stage.models)}"))
        else:
            out.append(Artifact(
                path=stage.procedure, kind="procedure_migration",
                description=f"applied via CREATE OR REPLACE PROCEDURE (idempotent by "
                           f"construction) before stage {stage.name!r} runs"))
    return out


def plan(pipeline: Pipeline) -> Plan:
    gates_by_stage = {
        stage.name: [compile_gate(gate, stage) for gate in stage.gates]
        for stage in pipeline.stages
    }
    return Plan(
        pipeline=pipeline,
        tasks=dag_tasks(pipeline),
        artifacts=artifacts(pipeline),
        gates=gates_by_stage,
    )
