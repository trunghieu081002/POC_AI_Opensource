"""The bronze EXTRACT/LOAD worker - HG (phulee9/hgmedia)'s own split, built
for dpagent (docs/hg-bronze-staging.md).

Runs *inside the dlt pack's venv* (psycopg2, pyarrow, boto3 live there),
never imported into dpagent's own process - the same discipline
runtime._dlt_python()'s docstring describes for dlt itself. `bronze.py` is
the dpagent-side caller: it resolves every `${VAR}`, then runs this file as
a script with configuration passed only through environment variables.
Third-party imports are done inside the functions that need them, so the
pure parts below (layout, type mapping, manifest building/verification) are
importable and unit-tested without those packages installed.

    python bronze_worker.py extract
    python bronze_worker.py load <batch_id>

The protocol, exactly as reviewed:

EXTRACT (needs source + warehouse + storage)
  1. registry row (batch_id, table) inserted as `extracting`
  2. source table read once, inside one REPEATABLE READ READ ONLY
     transaction (one consistent snapshot even across many chunks), via a
     server-side cursor, `chunk_rows` rows per bronze object
  3. each object uploaded, then read back and its sha256/size checked
  4. only then the manifest is published (last), read back and checked
  5. registry row -> `extracted` with the manifest's own sha256
  Any failure -> `failed` + error text. A batch that is not `extracted`
  is never loadable, so objects left behind by a failed extract are inert.

LOAD (needs warehouse + storage + batch_id ONLY - never the source)
  1. registry row must be `extracted` (or `loaded` -> no-op)
  2. manifest downloaded, its sha256 checked against the registry, its
     pipeline/table/batch checked against the registry row
  3. every object downloaded, sha256 + size + row count checked
  4. one Postgres transaction: per-table advisory lock, row lock on the
     registry row, re-check status (a concurrent LOAD that won -> no-op),
     refuse a batch older than one already loaded, rows into a TEMP table,
     count checked, landing table schema checked against the manifest,
     TRUNCATE + INSERT into landing, registry -> `loaded`, COMMIT.
  Retrying, or two LOADs of the same batch at once, can never duplicate
  rows: the landing table is replaced, not appended, and the second
  writer sees `loaded` under the lock. An empty snapshot loads exactly 0
  rows - it is never "skipped" leaving the previous data in place.

SEVERAL TABLES IN ONE BATCH (`SOURCE_TABLES=a,b`)
  EXTRACT reads every table inside the same REPEATABLE READ transaction, so
  the tables describe one instant. Each table keeps its own objects and
  manifest; a *batch manifest* (`_batches/<batch_id>/_batch.json`) is
  published last and names every table with its manifest's sha256. The
  registry rows of all tables become `extracted` in ONE statement, or all
  become `failed` - a batch is complete or it is not loadable at all.
  LOAD verifies the batch manifest, every table manifest and every object
  BEFORE opening the transaction (a missing table, an extra table, a changed
  byte -> nothing is touched), then replaces all landing tables in a single
  transaction (tables locked in name order): a reader sees the previous set
  or the new set, never one table of each. A single-table batch has no batch
  manifest and behaves exactly as before.

Normalisation level, stated rather than implied: column names unchanged;
no rows added, dropped or reordered beyond the server cursor's own order;
no metadata columns added (unlike dlt's `_dlt_load_id`/`_dlt_id`); ints,
booleans, floats, text, date and timestamp(tz) values stored natively in
Parquet; numeric, json/jsonb and uuid stored as their exact text form and
cast back to the original Postgres type on LOAD (the manifest records
both). Any other source type is refused at EXTRACT rather than mangled.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone

FORMAT_VERSION = 1
REGISTRY_SCHEMA = "dpagent_meta"
REGISTRY_TABLE = "bronze_batches"

_INT_TYPES = {"smallint", "integer", "bigint"}
_FLOAT_TYPES = {"real", "double precision"}
_TEXT_PREFIXES = ("text", "character varying", "character", "varchar", "char")
_EXACT_TEXT_PREFIXES = ("numeric", "json", "jsonb", "uuid")


class BronzeError(Exception):
    pass


# ------------------------------------------------------------------ pure parts

def arrow_type_for(source_type: str) -> str:
    """Postgres `format_type()` text -> the Arrow type name this worker
    stores it as. Raises BronzeError for anything outside the supported
    set - refused, never silently stringified."""
    t = source_type.strip().lower()
    if t in _INT_TYPES:
        return "int64"
    if t == "boolean":
        return "bool"
    if t in _FLOAT_TYPES:
        return "float64"
    if t == "date":
        return "date32"
    if t == "timestamp without time zone" or t.startswith("timestamp(") and "without" in t:
        return "timestamp_us"
    if t == "timestamp with time zone" or t.startswith("timestamp(") and "with time zone" in t:
        return "timestamp_us_utc"
    if t.startswith(_TEXT_PREFIXES):
        return "string"
    if t.startswith(_EXACT_TEXT_PREFIXES):
        return "string_exact"
    raise BronzeError(
        f"source column type {source_type!r} is not supported by the bronze "
        f"split yet - refusing rather than storing a lossy representation")


def batch_prefix(prefix: str, pipeline: str, table: str, batch_id: str) -> str:
    return f"{prefix.strip('/')}/{pipeline}/{table}/{batch_id}"


def object_key(prefix: str, pipeline: str, table: str, batch_id: str, part: int) -> str:
    return f"{batch_prefix(prefix, pipeline, table, batch_id)}/part-{part:05d}.parquet"


def manifest_key(prefix: str, pipeline: str, table: str, batch_id: str) -> str:
    return f"{batch_prefix(prefix, pipeline, table, batch_id)}/_manifest.json"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(*, pipeline: str, table: str, batch_id: str, relation: str,
                   columns: list[dict], objects: list[dict], extracted_at: str) -> bytes:
    """Canonical bytes - the registry stores sha256 of exactly these bytes,
    so a manifest swapped or truncated in storage is detected on LOAD."""
    doc = {
        "format_version": FORMAT_VERSION,
        "pipeline": pipeline,
        "table": table,
        "batch_id": batch_id,
        "source": {"connector": "odoo_postgres", "relation": relation},
        "extracted_at": extracted_at,
        "normalization": "names unchanged; no rows or columns added/removed; "
                         "numeric/json/jsonb/uuid as exact text, cast back on load",
        "columns": columns,
        "objects": objects,
        "total_rows": sum(o["rows"] for o in objects),
    }
    return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")


def check_manifest(raw: bytes, *, expected_sha256: str, pipeline: str, table: str,
                   batch_id: str) -> dict:
    """Every check LOAD does on the manifest before trusting a single object
    it names. Raises BronzeError naming exactly what did not match."""
    actual = sha256_hex(raw)
    if actual != expected_sha256:
        raise BronzeError(f"manifest sha256 {actual} does not match the registry's "
                          f"{expected_sha256} - refusing a manifest that changed after "
                          f"it was published")
    doc = json.loads(raw)
    if doc.get("format_version") != FORMAT_VERSION:
        raise BronzeError(f"unsupported manifest format_version {doc.get('format_version')!r}")
    for key, want in (("pipeline", pipeline), ("table", table), ("batch_id", batch_id)):
        if doc.get(key) != want:
            raise BronzeError(f"manifest {key} {doc.get(key)!r} does not match the "
                              f"registry's {want!r}")
    if doc.get("total_rows") != sum(o["rows"] for o in doc.get("objects", [])):
        raise BronzeError("manifest total_rows does not equal the sum of its objects' rows")
    if not doc.get("columns"):
        raise BronzeError("manifest has no columns - cannot build or check a landing table")
    return doc


def batch_manifest_key(prefix: str, pipeline: str, batch_id: str) -> str:
    return f"{prefix.strip('/')}/{pipeline}/_batches/{batch_id}/_batch.json"


def build_batch_manifest(*, pipeline: str, batch_id: str, extracted_at: str,
                         tables: list[dict]) -> bytes:
    """`tables`: one {table, relation, manifest_key, manifest_sha256, total_rows, objects}
    per table. Canonical bytes, like the table manifests: the registry keeps their sha256."""
    doc = {
        "format_version": FORMAT_VERSION,
        "kind": "batch",
        "pipeline": pipeline,
        "batch_id": batch_id,
        "extracted_at": extracted_at,
        "snapshot": "all tables read inside one REPEATABLE READ READ ONLY transaction",
        "tables": sorted(tables, key=lambda t: t["table"]),
    }
    return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")


def check_batch_manifest(raw: bytes, *, expected_sha256: str, pipeline: str, batch_id: str,
                         registry: dict[str, str]) -> dict:
    """`registry`: table -> manifest_sha256 as the registry holds it. The batch is
    complete only if the manifest names EXACTLY those tables with EXACTLY those
    table-manifest hashes."""
    actual = sha256_hex(raw)
    if actual != expected_sha256:
        raise BronzeError(f"batch manifest sha256 {actual} does not match the registry's "
                          f"{expected_sha256} - refusing a batch manifest that changed after "
                          f"it was published")
    doc = json.loads(raw)
    if doc.get("format_version") != FORMAT_VERSION or doc.get("kind") != "batch":
        raise BronzeError(f"not a batch manifest this worker understands "
                          f"(format_version {doc.get('format_version')!r}, kind {doc.get('kind')!r})")
    for key, want in (("pipeline", pipeline), ("batch_id", batch_id)):
        if doc.get(key) != want:
            raise BronzeError(f"batch manifest {key} {doc.get(key)!r} does not match {want!r}")
    named = {t["table"]: t["manifest_sha256"] for t in doc.get("tables", [])}
    if len(named) != len(doc.get("tables", [])):
        raise BronzeError("batch manifest names the same table twice")
    missing = sorted(set(registry) - set(named))
    extra = sorted(set(named) - set(registry))
    if missing or extra:
        raise BronzeError(f"batch is incomplete or inconsistent: the registry has "
                          f"{sorted(registry)}, the batch manifest names {sorted(named)} "
                          f"(missing from manifest: {missing}, not in registry: {extra})")
    for table, sha in registry.items():
        if named[table] != sha:
            raise BronzeError(f"table {table!r}: its manifest hash in the registry ({sha}) "
                              f"differs from the one the batch manifest recorded ({named[table]})")
    return doc


def check_object(data: bytes, entry: dict) -> None:
    if len(data) != entry["size_bytes"]:
        raise BronzeError(f"{entry['key']}: size {len(data)} != manifest {entry['size_bytes']}")
    actual = sha256_hex(data)
    if actual != entry["sha256"]:
        raise BronzeError(f"{entry['key']}: sha256 {actual} != manifest {entry['sha256']}")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# -------------------------------------------------------------- I/O helpers

def _env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise BronzeError(f"{name} is not set")
    return value


def _s3():
    import boto3
    from botocore.client import Config
    return boto3.client(
        "s3", endpoint_url=_env("BRONZE_ENDPOINT"),
        aws_access_key_id=_env("BRONZE_ACCESS_KEY"),
        aws_secret_access_key=_env("BRONZE_SECRET_KEY"),
        region_name=os.environ.get("BRONZE_REGION") or "us-east-1",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _get(s3, key: str) -> bytes:
    return s3.get_object(Bucket=_env("BRONZE_BUCKET"), Key=key)["Body"].read()


def _put_verified(s3, key: str, data: bytes) -> None:
    s3.put_object(Bucket=_env("BRONZE_BUCKET"), Key=key, Body=data)
    back = _get(s3, key)
    if back != data:
        raise BronzeError(f"{key}: object read back after upload does not match what "
                          f"was written")


def _connect(prefix: str):
    import psycopg2
    return psycopg2.connect(
        host=_env(f"{prefix}_HOST"), port=os.environ.get(f"{prefix}_PORT") or "5432",
        dbname=_env(f"{prefix}_DATABASE"), user=_env(f"{prefix}_USER"),
        password=os.environ.get(f"{prefix}_PASSWORD", ""), connect_timeout=10,
        application_name=f"dpagent-bronze-{prefix.lower()}",
    )


_REGISTRY_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {REGISTRY_SCHEMA};
CREATE TABLE IF NOT EXISTS {REGISTRY_SCHEMA}.{REGISTRY_TABLE} (
    batch_id        uuid        NOT NULL,
    pipeline        text        NOT NULL,
    table_name      text        NOT NULL,
    status          text        NOT NULL
        CHECK (status IN ('extracting', 'extracted', 'loaded', 'failed')),
    manifest_key    text,
    manifest_sha256 text,
    object_count    integer,
    total_rows      bigint,
    loaded_rows     bigint,
    error           text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    extracted_at    timestamptz,
    loaded_at       timestamptz,
    batch_tables         integer,
    batch_manifest_key   text,
    batch_manifest_sha256 text,
    PRIMARY KEY (batch_id, table_name)
);
ALTER TABLE {REGISTRY_SCHEMA}.{REGISTRY_TABLE}
    ADD COLUMN IF NOT EXISTS batch_tables integer,
    ADD COLUMN IF NOT EXISTS batch_manifest_key text,
    ADD COLUMN IF NOT EXISTS batch_manifest_sha256 text;
"""


def _ensure_registry(wh) -> None:
    # Serialised so two first-ever EXTRACTs cannot race each other's
    # CREATE SCHEMA into a unique-violation on pg_namespace.
    with wh.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{REGISTRY_SCHEMA}.{REGISTRY_TABLE}",))
        cur.execute(_REGISTRY_DDL)
    wh.commit()


def _result(kind: str, payload: dict) -> None:
    print(f"DPAGENT_BRONZE_{kind} {json.dumps(payload, sort_keys=True)}", flush=True)


# ------------------------------------------------------------------ EXTRACT

def _source_columns(cur, schema: str, table: str) -> list[dict]:
    cur.execute(
        """SELECT a.attname, format_type(a.atttypid, a.atttypmod)
             FROM pg_attribute a
             JOIN pg_class c ON c.oid = a.attrelid
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum""", (schema, table))
    rows = cur.fetchall()
    if not rows:
        raise BronzeError(f"source table {schema}.{table} not found (or has no columns)")
    return [{"name": n, "source_type": t, "arrow_type": arrow_type_for(t)} for n, t in rows]


def _to_parquet(columns: list[dict], rows: list[tuple]) -> bytes:
    import io
    import pyarrow as pa
    import pyarrow.parquet as pq

    arrow = {
        "int64": pa.int64(), "bool": pa.bool_(), "float64": pa.float64(),
        "date32": pa.date32(), "timestamp_us": pa.timestamp("us"),
        "timestamp_us_utc": pa.timestamp("us", tz="UTC"),
        "string": pa.string(), "string_exact": pa.string(),
    }
    arrays = []
    for i, col in enumerate(columns):
        values = [r[i] for r in rows]
        if col["arrow_type"] == "string_exact":
            values = [None if v is None else
                      (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                      for v in values]
        arrays.append(pa.array(values, type=arrow[col["arrow_type"]]))
    table = pa.Table.from_arrays(arrays, names=[c["name"] for c in columns])
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def _source_relations() -> list[tuple[str, str]]:
    raw = os.environ.get("SOURCE_TABLES") or _env("SOURCE_TABLE")
    out = []
    for relation in [r.strip() for r in raw.split(",") if r.strip()]:
        schema, _, table = relation.rpartition(".")
        out.append((schema or "public", table))
    if not out:
        raise BronzeError("no source table given")
    if len({t for _, t in out}) != len(out):
        raise BronzeError(f"the same table named twice in {raw!r}")
    return out


def _read_table(src, s3, *, schema: str, table: str, pipeline: str, prefix: str,
                batch_id: str, chunk_rows: int, index: int) -> tuple[bytes, str, dict]:
    """One table of the snapshot -> verified objects + its (verified) manifest.
    Returns (manifest bytes, manifest key, {rows, objects})."""
    objects: list[dict] = []
    with src.cursor() as meta:
        columns = _source_columns(meta, schema, table)
    col_list = ", ".join(_quote_ident(c["name"]) for c in columns)
    with src.cursor(name=f"bronze_{batch_id.replace('-', '')}_{index}") as cur:
        cur.itersize = chunk_rows
        cur.execute(f"SELECT {col_list} FROM {_quote_ident(schema)}.{_quote_ident(table)}")
        part = 0
        while True:
            rows = cur.fetchmany(chunk_rows)
            if not rows:
                break
            data = _to_parquet(columns, rows)
            key = object_key(prefix, pipeline, table, batch_id, part)
            _put_verified(s3, key, data)
            objects.append({"key": key, "sha256": sha256_hex(data),
                            "size_bytes": len(data), "rows": len(rows)})
            part += 1
    manifest = build_manifest(pipeline=pipeline, table=table, batch_id=batch_id,
                              relation=f"{schema}.{table}", columns=columns, objects=objects,
                              extracted_at=datetime.now(timezone.utc).isoformat())
    mkey = manifest_key(prefix, pipeline, table, batch_id)
    _put_verified(s3, mkey, manifest)      # after every one of its objects was checked
    return manifest, mkey, {"rows": sum(o["rows"] for o in objects), "objects": len(objects)}


def extract() -> str:
    pipeline = _env("PIPELINE_NAME")
    relations = _source_relations()
    tables = [t for _, t in relations]
    prefix = _env("BRONZE_PREFIX")
    chunk_rows = int(os.environ.get("BRONZE_CHUNK_ROWS") or "50000")
    batch_id = str(uuid.uuid4())

    wh = _connect("WH")
    _ensure_registry(wh)
    with wh.cursor() as cur:
        for table in tables:
            cur.execute(f"INSERT INTO {REGISTRY_SCHEMA}.{REGISTRY_TABLE} "
                        f"(batch_id, pipeline, table_name, status, batch_tables) "
                        f"VALUES (%s, %s, %s, 'extracting', %s)",
                        (batch_id, pipeline, table, len(tables)))
    wh.commit()

    try:
        s3 = _s3()
        src = _connect("SRC")
        # One transaction for ALL tables: the snapshot is taken by its first query and every
        # table below is read from it, so the tables describe the same instant.
        src.set_session(isolation_level="REPEATABLE READ", readonly=True)
        done: list[tuple[str, str, str, bytes, dict]] = []     # table, relation, mkey, manifest, counts
        for i, (schema, table) in enumerate(relations):
            manifest, mkey, counts = _read_table(
                src, s3, schema=schema, table=table, pipeline=pipeline, prefix=prefix,
                batch_id=batch_id, chunk_rows=chunk_rows, index=i)
            done.append((table, f"{schema}.{table}", mkey, manifest, counts))
        src.rollback()
        src.close()

        batch_key = batch_sha = None
        if len(tables) > 1:
            batch = build_batch_manifest(
                pipeline=pipeline, batch_id=batch_id,
                extracted_at=datetime.now(timezone.utc).isoformat(),
                tables=[{"table": t, "relation": rel, "manifest_key": mk,
                         "manifest_sha256": sha256_hex(m), "total_rows": c["rows"],
                         "objects": c["objects"]} for t, rel, mk, m, c in done])
            batch_key = batch_manifest_key(prefix, pipeline, batch_id)
            _put_verified(s3, batch_key, batch)    # published last: every table is complete
            batch_sha = sha256_hex(batch)

        with wh.cursor() as cur:
            for table, _rel, mkey, manifest, counts in done:
                cur.execute(f"UPDATE {REGISTRY_SCHEMA}.{REGISTRY_TABLE} SET status='extracted', "
                            f"manifest_key=%s, manifest_sha256=%s, object_count=%s, "
                            f"total_rows=%s, extracted_at=now(), batch_manifest_key=%s, "
                            f"batch_manifest_sha256=%s "
                            f"WHERE batch_id=%s AND table_name=%s AND status='extracting'",
                            (mkey, sha256_hex(manifest), counts["objects"], counts["rows"],
                             batch_key, batch_sha, batch_id, table))
                if cur.rowcount != 1:
                    raise BronzeError(f"registry row for {table} was not 'extracting' when "
                                      f"marking it extracted - refusing to publish the batch")
        wh.commit()                                # all tables extracted together, or none
    except Exception as exc:
        wh.rollback()
        with wh.cursor() as cur:
            cur.execute(f"UPDATE {REGISTRY_SCHEMA}.{REGISTRY_TABLE} SET status='failed', "
                        f"error=%s WHERE batch_id=%s",
                        (f"{type(exc).__name__}: {exc}"[:2000], batch_id))
        wh.commit()
        wh.close()
        raise
    wh.close()
    payload = {"batch_id": batch_id, "tables": {t: c for t, _r, _m, _b, c in done},
               "objects": sum(c["objects"] for *_x, c in done),
               "total_rows": sum(c["rows"] for *_x, c in done)}
    if len(tables) == 1:
        payload["table"] = tables[0]
        payload["manifest_key"] = done[0][2]
    else:
        payload["batch_manifest_key"] = batch_key
    _result("EXTRACT", payload)
    return batch_id


# --------------------------------------------------------------------- LOAD

def _read_rows(data: bytes) -> tuple[list[str], list[tuple]]:
    import io
    import pyarrow.parquet as pq
    table = pq.read_table(io.BytesIO(data))
    names = table.column_names
    cols = [table.column(n).to_pylist() for n in names]
    return names, list(zip(*cols)) if cols else []


def _verify_table(s3, *, pipeline: str, table: str, batch_id: str, mkey: str, msha: str):
    """Everything about one table that can be checked without touching the warehouse:
    manifest hash, every object's hash/size/columns/row count. Returns (manifest, rows)."""
    manifest = check_manifest(_get(s3, mkey), expected_sha256=msha, pipeline=pipeline,
                              table=table, batch_id=batch_id)
    names = [c["name"] for c in manifest["columns"]]
    all_rows: list[tuple] = []
    for entry in manifest["objects"]:
        data = _get(s3, entry["key"])
        check_object(data, entry)
        got_names, rows = _read_rows(data)
        if got_names != names:
            raise BronzeError(f"{entry['key']}: columns {got_names} != manifest {names}")
        if len(rows) != entry["rows"]:
            raise BronzeError(f"{entry['key']}: {len(rows)} rows != manifest {entry['rows']}")
        all_rows.extend(rows)
    if len(all_rows) != manifest["total_rows"]:
        raise BronzeError(f"{len(all_rows)} rows read != manifest total {manifest['total_rows']}")
    return manifest, all_rows


def _replace_landing(cur, *, landing: str, table: str, manifest: dict, rows: list[tuple],
                     index: int) -> int:
    from psycopg2.extras import execute_values
    columns = manifest["columns"]
    names = [c["name"] for c in columns]
    col_defs = ", ".join(f"{_quote_ident(c['name'])} {c['source_type']}" for c in columns)
    col_list = ", ".join(_quote_ident(n) for n in names)
    target = f"{_quote_ident(landing)}.{_quote_ident(table)}"
    temp = f"_bronze_load_{index}"
    cur.execute(f"CREATE TEMP TABLE {temp} ({col_defs}) ON COMMIT DROP")
    if rows:
        execute_values(cur, f"INSERT INTO {temp} ({col_list}) VALUES %s", rows, page_size=1000)
    cur.execute(f"SELECT count(*) FROM {temp}")
    staged = cur.fetchone()[0]
    if staged != manifest["total_rows"]:
        raise BronzeError(f"{table}: {staged} rows staged != manifest {manifest['total_rows']}")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {target} ({col_defs})")
    cur.execute(
        """SELECT a.attname, format_type(a.atttypid, a.atttypmod)
             FROM pg_attribute a WHERE a.attrelid = %s::regclass
              AND a.attnum > 0 AND NOT a.attisdropped""", (target,))
    existing = sorted(cur.fetchall())
    wanted = sorted((c["name"], c["source_type"]) for c in columns)
    if existing != wanted:
        raise BronzeError(f"landing table {landing}.{table} columns {existing} do not "
                          f"match the manifest's {wanted} - schema drift is refused, "
                          f"not silently adapted")
    cur.execute(f"TRUNCATE {target}")
    cur.execute(f"INSERT INTO {target} ({col_list}) SELECT {col_list} FROM {temp}")
    return staged


def load(batch_id: str) -> dict:
    pipeline = _env("PIPELINE_NAME")
    landing = _env("LANDING_SCHEMA")
    try:
        uuid.UUID(batch_id)
    except ValueError:
        raise BronzeError(f"{batch_id!r} is not a batch id") from None

    wh = _connect("WH")
    with wh.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{REGISTRY_SCHEMA}.{REGISTRY_TABLE}",))
        if cur.fetchone()[0] is None:
            raise BronzeError("no bronze registry in this warehouse - nothing was ever extracted")
        cur.execute(f"SELECT table_name, status, manifest_key, manifest_sha256, pipeline, "
                    f"batch_tables, batch_manifest_key, batch_manifest_sha256 "
                    f"FROM {REGISTRY_SCHEMA}.{REGISTRY_TABLE} WHERE batch_id=%s "
                    f"ORDER BY table_name", (batch_id,))
        rows = cur.fetchall()
    wh.rollback()
    if not rows:
        raise BronzeError(f"batch {batch_id} is not in the registry")
    owners = {r[4] for r in rows}
    if owners != {pipeline}:
        raise BronzeError(f"batch {batch_id} belongs to pipeline(s) {sorted(owners)}, "
                          f"not {pipeline!r}")
    expected_tables = rows[0][5] or 1
    if len(rows) != expected_tables:
        raise BronzeError(f"batch {batch_id} is incomplete: it should hold {expected_tables} "
                          f"table(s), the registry has {len(rows)} - a partial batch is "
                          f"never loaded")
    statuses = {r[1] for r in rows}
    tables = [r[0] for r in rows]
    if statuses == {"loaded"}:
        wh.close()
        result = {"batch_id": batch_id, "tables": tables, "outcome": "already_loaded"}
        _result("LOAD", result)
        return result
    if statuses != {"extracted"}:
        raise BronzeError(f"batch {batch_id} is {sorted(statuses)} (per table: "
                          f"{dict((r[0], r[1]) for r in rows)}), not entirely 'extracted' - an "
                          f"incomplete, failed or half-loaded batch is never loaded")

    s3 = _s3()
    if expected_tables > 1:
        shas = {r[7] for r in rows}
        keys = {r[6] for r in rows}
        if len(shas) != 1 or len(keys) != 1 or None in shas:
            raise BronzeError("the tables of this batch do not agree on one batch manifest")
        check_batch_manifest(_get(s3, rows[0][6]), expected_sha256=rows[0][7],
                             pipeline=pipeline, batch_id=batch_id,
                             registry={r[0]: r[3] for r in rows})

    # Verify EVERYTHING before the warehouse is touched: a bad byte in the last table must
    # not leave the first table's landing already replaced.
    verified = {}
    for table, _st, mkey, msha, *_rest in rows:
        verified[table] = _verify_table(s3, pipeline=pipeline, table=table, batch_id=batch_id,
                                        mkey=mkey, msha=msha)

    try:
        with wh.cursor() as cur:
            for table in sorted(tables):     # a fixed order: two LOADs cannot deadlock
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"bronze-load:{pipeline}.{table}",))
            cur.execute(f"SELECT table_name, status, extracted_at FROM "
                        f"{REGISTRY_SCHEMA}.{REGISTRY_TABLE} WHERE batch_id=%s "
                        f"ORDER BY table_name FOR UPDATE", (batch_id,))
            locked = cur.fetchall()
            now_status = {r[1] for r in locked}
            if now_status == {"loaded"}:
                wh.rollback()
                result = {"batch_id": batch_id, "tables": tables, "outcome": "already_loaded"}
                _result("LOAD", result)
                return result
            if now_status != {"extracted"}:
                raise BronzeError(f"batch {batch_id} became {sorted(now_status)} before it "
                                  f"could be loaded")
            for table, _status, extracted_at in locked:
                cur.execute(f"SELECT batch_id FROM {REGISTRY_SCHEMA}.{REGISTRY_TABLE} "
                            f"WHERE pipeline=%s AND table_name=%s AND status='loaded' "
                            f"AND extracted_at > %s LIMIT 1", (pipeline, table, extracted_at))
                newer = cur.fetchone()
                if newer:
                    raise BronzeError(f"a newer batch ({newer[0]}) is already loaded for "
                                      f"{table} - refusing to replace landing with older data")
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(landing)}")
            loaded_rows = {}
            for i, table in enumerate(sorted(tables)):
                manifest, trows = verified[table]
                loaded_rows[table] = _replace_landing(
                    cur, landing=landing, table=table, manifest=manifest, rows=trows, index=i)
            for table, n in loaded_rows.items():
                cur.execute(f"UPDATE {REGISTRY_SCHEMA}.{REGISTRY_TABLE} SET status='loaded', "
                            f"loaded_at=now(), loaded_rows=%s WHERE batch_id=%s AND table_name=%s",
                            (n, batch_id, table))
        wh.commit()                      # every landing table replaced together, or none
    except Exception:
        wh.rollback()
        raise
    finally:
        wh.close()
    result = {"batch_id": batch_id, "outcome": "loaded",
              "rows": sum(m["total_rows"] for m, _ in verified.values()),
              "objects": sum(len(m["objects"]) for m, _ in verified.values()),
              "tables": {t: {"rows": m["total_rows"], "objects": len(m["objects"])}
                         for t, (m, _) in verified.items()}}
    if len(tables) == 1:
        result["table"] = tables[0]
    _result("LOAD", result)
    return result


# ---------------------------------------------- namespace housekeeping (validation)

def _namespace() -> str:
    ns = _env("BRONZE_NAMESPACE").strip("/")
    # A namespace operation deletes objects - refuse anything that could be
    # the bucket root or a bare top-level prefix shared with real data.
    if not ns or ns in (".", "..") or ".." in ns.split("/") or "/" not in ns:
        raise BronzeError(f"refusing a namespace operation on {ns!r}: it must name a "
                          f"dedicated 'a/b' prefix, never the bucket root or a shared top level")
    return ns + "/"


def _list_namespace(s3, ns: str) -> list[str]:
    keys, token = [], None
    while True:
        kwargs = {"Bucket": _env("BRONZE_BUCKET"), "Prefix": ns}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return keys
        token = page["NextContinuationToken"]


def ping() -> dict:
    s3 = _s3()
    s3.head_bucket(Bucket=_env("BRONZE_BUCKET"))
    result = {"bucket": _env("BRONZE_BUCKET"), "reachable": True}
    _result("PING", result)
    return result


def count_namespace() -> dict:
    ns = _namespace()
    keys = _list_namespace(_s3(), ns)
    result = {"namespace": ns, "objects": len(keys)}
    _result("COUNT", result)
    return result


def purge_namespace() -> dict:
    """Deletes every object under the (dedicated) namespace, then LISTS it
    again: `remaining` is what is still there, read back from the store, not
    inferred from the delete calls having returned."""
    ns = _namespace()
    s3 = _s3()
    keys = _list_namespace(s3, ns)
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        s3.delete_objects(Bucket=_env("BRONZE_BUCKET"),
                          Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
    remaining = _list_namespace(s3, ns)
    result = {"namespace": ns, "deleted": len(keys) - len(remaining), "found": len(keys),
              "remaining": len(remaining)}
    _result("PURGE", result)
    return result


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["extract"] and len(argv) == 1:
            extract()
        elif argv[:1] == ["load"] and len(argv) == 2:
            load(argv[1])
        elif argv == ["ping"]:
            ping()
        elif argv == ["count"]:
            count_namespace()
        elif argv == ["purge"]:
            purge_namespace()
        else:
            print("usage: bronze_worker.py extract | load <batch_id> | ping | count | purge", file=sys.stderr)
            return 2
    except BronzeError as exc:
        print(f"bronze: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
