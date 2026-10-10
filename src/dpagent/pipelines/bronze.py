"""dpagent's side of the bronze EXTRACT/LOAD split (docs/hg-bronze-staging.md).

Resolves every `${VAR}` this pipeline references, then runs
`bronze_worker.py` in the dlt pack's own venv with configuration passed
only through a minimal environment - nothing third-party is ever imported
here, the same rule `runtime.run_extract` follows for dlt itself.

The one guarantee that matters most is in `run_load`: it never resolves,
and never passes, a single source-connection value. LOAD gets the
warehouse, the object store, and a batch id - nothing else. That is what
makes "LOAD still works with the source gone" a property of the code, not
of whichever environment happened to be around when it ran.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..engine import state
from ..engine.params import resolve_refs
from . import extract, loader, runtime

WORKER = Path(__file__).with_name("bronze_worker.py")

# Everything else in the caller's environment is deliberately not passed on:
# a worker that could read SRC_* (or any secret) from an inherited
# environment would make the source-independence of LOAD an accident.
_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


class BronzeFailed(runtime.GateFailed):
    """A failed bronze EXTRACT/LOAD - a GateFailed subclass, so the DAG
    task fails exactly like a failed dlt extract would."""


def _require_bronze(pipeline: loader.Pipeline) -> loader.BronzeStorage:
    if not pipeline.bronze_staging or pipeline.bronze is None:
        raise BronzeFailed(f"{pipeline.name!r} does not use bronze_staging")
    return pipeline.bronze


def _base_env(pipeline: loader.Pipeline) -> dict[str, str]:
    bronze = _require_bronze(pipeline)
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    storage = resolve_refs(
        {"endpoint": bronze.endpoint, "bucket": bronze.bucket,
         "access_key": bronze.access_key, "secret_key": bronze.secret_key,
         "region": bronze.region, "prefix": bronze.prefix},
        path=f"{pipeline.name}.bronze")
    runtime._register_secrets(storage)
    warehouse = resolve_refs(
        {"host": pipeline.warehouse.host, "port": pipeline.warehouse.port,
         "database": pipeline.warehouse.database, "user": pipeline.warehouse.user,
         "password": pipeline.warehouse.password},
        path=f"{pipeline.name}.warehouse")
    runtime._register_secrets(warehouse)
    env.update({
        "PIPELINE_NAME": pipeline.name,
        "BRONZE_ENDPOINT": str(storage["endpoint"]),
        "BRONZE_BUCKET": str(storage["bucket"]),
        "BRONZE_ACCESS_KEY": str(storage["access_key"]),
        "BRONZE_SECRET_KEY": str(storage["secret_key"]),
        "BRONZE_REGION": str(storage["region"]),
        "BRONZE_PREFIX": str(storage["prefix"]),
        "WH_HOST": str(warehouse["host"]), "WH_PORT": str(warehouse["port"]),
        "WH_DATABASE": str(warehouse["database"]), "WH_USER": str(warehouse["user"]),
        "WH_PASSWORD": str(warehouse["password"]),
    })
    return env


def _parse(stdout: str, kind: str) -> dict:
    marker = f"DPAGENT_BRONZE_{kind} "
    for line in (stdout or "").splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise BronzeFailed(f"bronze {kind.lower()} produced no result line")


def run_extract(*, pipeline_name: str | None = None, run_id: int | None = None,
                pipeline: loader.Pipeline | None = None) -> str:
    """EXTRACT one full snapshot of the pipeline's single source table into
    bronze object storage; returns the new batch id (the DAG pushes it to
    XCom, so a retried LOAD task always loads this same batch). `pipeline`
    lets a caller that already holds one (fixture validation, whose clone is
    published elsewhere than `loader.PIPELINES_DIR`) skip the by-name load."""
    pipeline = pipeline or loader.load(pipeline_name)
    pipeline_name = pipeline.name
    env = _base_env(pipeline)
    src = resolve_refs(pipeline.source.connection, path=f"{pipeline_name}.source.connection")
    runtime._register_secrets(src)
    env.update({
        "SRC_HOST": str(src.get("host", "")), "SRC_PORT": str(src.get("port", "5432")),
        "SRC_DATABASE": str(src.get("database", "")), "SRC_USER": str(src.get("user", "")),
        "SRC_PASSWORD": str(src.get("password", "")),
        "SOURCE_TABLE": pipeline.source.tables[0],
        "BRONZE_CHUNK_ROWS": str(pipeline.bronze.chunk_rows),
    })
    state.event("bronze.extract.start", f"{pipeline_name}: {pipeline.source.tables[0]} -> bronze",
                run_id=run_id)
    proc = runtime._run([runtime._dlt_python(), str(WORKER), "extract"], pipeline=pipeline,
                        kind="extract", what=f"the bronze extract for {pipeline_name!r}", env=env)
    if proc.returncode != 0:
        detail = runtime._detail(proc.stderr)
        state.event("bronze.extract.failed", f"{pipeline_name}: {detail}",
                    run_id=run_id, level="error")
        raise BronzeFailed(f"bronze extract failed for {pipeline_name!r}:\n{detail}")
    result = _parse(proc.stdout, "EXTRACT")
    state.event("bronze.extract.done",
                f"{pipeline_name}: batch {result['batch_id']} - {result['total_rows']} row(s) "
                f"in {result['objects']} object(s)", run_id=run_id)
    return result["batch_id"]


def run_load(*, pipeline_name: str | None = None, batch_id: str,
             run_id: int | None = None, pipeline: loader.Pipeline | None = None) -> dict:
    """LOAD one already-extracted batch into the landing schema. Takes the
    warehouse, the object store and the batch id - never the source."""
    pipeline = pipeline or loader.load(pipeline_name)
    pipeline_name = pipeline.name
    if not batch_id:
        raise BronzeFailed(f"no batch id to load for {pipeline_name!r} - the EXTRACT task "
                           f"did not hand one over")
    env = _base_env(pipeline)
    env["LANDING_SCHEMA"] = extract.landing_dataset(pipeline)
    state.event("bronze.load.start", f"{pipeline_name}: batch {batch_id}", run_id=run_id)
    proc = runtime._run([runtime._dlt_python(), str(WORKER), "load", batch_id],
                        pipeline=pipeline, kind="extract",
                        what=f"the bronze load for {pipeline_name!r}", env=env)
    if proc.returncode != 0:
        detail = runtime._detail(proc.stderr)
        state.event("bronze.load.failed", f"{pipeline_name}: batch {batch_id}: {detail}",
                    run_id=run_id, level="error")
        raise BronzeFailed(f"bronze load failed for {pipeline_name!r} batch {batch_id}:\n{detail}")
    result = _parse(proc.stdout, "LOAD")
    if result["outcome"] == "already_loaded":
        message = f"{pipeline_name}: batch {batch_id} was already loaded - nothing changed"
    else:
        message = (f"{pipeline_name}: batch {batch_id} loaded - {result['rows']} row(s) "
                   f"from {result['objects']} object(s)")
    state.event("bronze.load.done", message, run_id=run_id)
    return result


# ----------------------------------------- object-store housekeeping (fixture validation)

def _storage_env(pipeline: loader.Pipeline) -> dict[str, str]:
    """Object store only - no warehouse, no source."""
    env = _base_env(pipeline)
    return {k: v for k, v in env.items() if not k.startswith("WH_")}


def _call_worker(pipeline: loader.Pipeline, op: str, kind: str, extra_env: dict | None = None):
    env = _storage_env(pipeline)
    env.update(extra_env or {})
    proc = runtime._run([runtime._dlt_python(), str(WORKER), op], pipeline=pipeline,
                        kind="extract", what=f"bronze {op} for {pipeline.name!r}", env=env)
    if proc.returncode != 0:
        raise BronzeFailed(f"bronze {op} failed: {runtime._detail(proc.stderr)}")
    return _parse(proc.stdout, kind)


def check_storage(pipeline: loader.Pipeline) -> tuple[bool, str]:
    """(reachable, detail): can the dlt venv's worker open this pipeline's
    object store and see its bucket? Never raises - a preflight asks."""
    try:
        _call_worker(pipeline, "ping", "PING")
        return True, ""
    except (BronzeFailed, runtime.GateFailed, Exception) as exc:   # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc).strip()[-400:]}"


def count_namespace(pipeline: loader.Pipeline, namespace: str) -> int:
    return int(_call_worker(pipeline, "count", "COUNT", {"BRONZE_NAMESPACE": namespace})["objects"])


def purge_namespace(pipeline: loader.Pipeline, namespace: str) -> dict:
    """Deletes everything under `namespace` and reports what the store says is
    still there afterwards (`remaining`)."""
    return _call_worker(pipeline, "purge", "PURGE", {"BRONZE_NAMESPACE": namespace})
