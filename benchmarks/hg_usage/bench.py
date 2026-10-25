"""HG-shaped SYNTHETIC load benchmark - runs INSIDE a bronze host (scripts/hg-load-bench.sh). Not HG's data and not an
HG load acceptance: volumes, skew, schedule and SLA are the owner's inputs (docs/hg-representative-load-plan.md).

  bench.py SCALE [SCALE ...] [--timeout S] [--min-free-gb G] [--min-avail-gb G]

For each scale, in order, on the real stack (the HG pipeline `hg_resources_useage_number` deployed under its own name,
extracting into a real bronze batch on the host's SeaweedFS, dbt into a throwaway warehouse):
  1. generate the six source tables (generate.py) and the answer key (answer.py), before anything runs;
  2. load them into a throwaway source database (COPY), timed;
  3. deploy, run once: time per stage from DPAgent's journal, the container's CPU/memory/IO from its cgroup and the
     databases' sizes sampled every 2 s; compare gold with the answer key (count, digest of every row, distinct
     counts, position groups);
  4. run again: the same answer, no duplicates (full snapshot);
  5. a mid-run failure: the warehouse role is refused while the silver transform runs -> the run fails and gold still
     holds the previous result; then recovery: a new run = the answer key;
  6. the dashboard's SQL (projects/hg-usage: the four audit measures, and filtered to one video) 20 times each,
     directly on the warehouse - NOT through Superset;
  7. cleanup: undeploy, bronze prefix purged, throwaway databases dropped, generated files removed - each recorded.
Before each scale it is refused (with every larger one) when free disk or available memory are below the thresholds,
or a smaller scale failed or exceeded the timeout. Evidence: /root/hgbench-evidence/.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO = Path("/opt/dpagent")
sys.path.insert(0, str(REPO / "tests" / "m25_acceptance"))
sys.path.insert(0, str(REPO / "benchmarks" / "hg_usage"))
import answer  # noqa: E402
import generate as gen  # noqa: E402
import run_matrix as rm  # noqa: E402
from dpagent.engine import state  # noqa: E402
from dpagent.pipelines import bronze, deploy as deploy_mod, fixture, pg_throwaway  # noqa: E402

OUT = Path("/root/hgbench-evidence")
WORK = Path("/var/tmp/hgbench")
GOLD_SQL = ("SELECT count(*) || ' ' || md5(coalesce(string_agg(line, E'\\n' ORDER BY line COLLATE \"C\"), '')) || ' ' || "
            "count(DISTINCT video_id) || ' ' || count(DISTINCT hg_stock_id) FROM (SELECT video_id, hg_stock_id, "
            "video_id || '|' || hg_stock_id || '|' || to_char(published_date, 'YYYY-MM-DD HH24:MI:SS') || '|' || "
            "first_position || '|' || position_group AS line FROM gold.resources_useage_number) t")
GROUPS_SQL = ("SELECT coalesce(json_object_agg(position_group, n ORDER BY position_group), '{}') FROM "
              "(SELECT position_group, count(*) n FROM gold.resources_useage_number GROUP BY 1) t")
AUDIT_SQL = ("SELECT COUNT(*), COUNT(DISTINCT resource_video_sk), COUNT(DISTINCT hg_stock_id), COUNT(DISTINCT video_id) "
             "FROM gold.resources_useage_number")


def psql(db: str, sql: str, timeout: int = 600) -> str:
    return subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-d", db,
                           "-c", sql], cwd="/", capture_output=True, text=True, check=True, timeout=timeout).stdout.strip()


def gold_summary(db: str) -> dict:
    try:
        n, digest, v, s = psql(db, GOLD_SQL).split()
        return {"rows": int(n), "digest": digest, "distinct_videos": int(v), "distinct_stock": int(s),
                "position_groups": json.loads(psql(db, GROUPS_SQL))}
    except subprocess.CalledProcessError as exc:
        return {"error": (exc.stderr or "")[:300]}


def matches(got: dict, key: dict) -> bool:
    return all(got.get(k) == key[k] for k in ("rows", "digest", "distinct_videos", "distinct_stock", "position_groups"))


def free_gb(path: str = "/") -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def avail_gb() -> float:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) / 1048576
    return 0.0


def cgroup_dir() -> Path | None:
    """The container's own cgroup (v2) - its root scope, so every process of the stack is counted."""
    try:
        rel = open("/proc/1/cgroup").read().strip().split("::", 1)[1]
    except (OSError, IndexError):
        return None
    parts = [p for p in rel.strip("/").split("/") if p]
    for i, p in enumerate(parts):
        if p.startswith(("docker-", "libpod-")):
            return Path("/sys/fs/cgroup", *parts[:i + 1])
    return None


class Sampler(threading.Thread):
    def __init__(self, dbs: list[str]):
        super().__init__(daemon=True)
        self.dbs, self.stop_, self.cg, self.rows = dbs, threading.Event(), cgroup_dir(), []

    def read(self) -> dict:
        r = {"t": round(time.monotonic(), 1), "host_mem_available_gb": round(avail_gb(), 2),
             "disk_free_gb": round(free_gb(), 2)}
        if self.cg:
            try:
                r["cg_memory_bytes"] = int((self.cg / "memory.current").read_text())
                cpu = dict(line.split() for line in (self.cg / "cpu.stat").read_text().splitlines())
                r["cg_cpu_usec"] = int(cpu["usage_usec"])
                rb = wb = 0
                for line in (self.cg / "io.stat").read_text().splitlines():
                    kv = dict(x.split("=") for x in line.split()[1:] if "=" in x)
                    rb += int(kv.get("rbytes", 0)); wb += int(kv.get("wbytes", 0))
                r["cg_io_read_bytes"], r["cg_io_write_bytes"] = rb, wb
            except (OSError, KeyError, ValueError):
                pass
        return r

    def run(self):
        n = 0
        while not self.stop_.is_set():
            r = self.read()
            if n % 5 == 0:
                for db in self.dbs:
                    try:
                        r[f"db_bytes_{db}"] = int(psql("postgres", f"SELECT pg_database_size('{db}')", timeout=20))
                    except Exception:            # noqa: BLE001
                        pass
            self.rows.append(r)
            n += 1
            self.stop_.wait(2)

    def summary(self) -> dict:
        if not self.rows:
            return {}
        s = {"samples": len(self.rows), "min_host_mem_available_gb": min(r["host_mem_available_gb"] for r in self.rows),
             "min_disk_free_gb": min(r["disk_free_gb"] for r in self.rows), "container_cgroup": str(self.cg)}
        first, last = self.rows[0], self.rows[-1]
        if "cg_cpu_usec" in first and "cg_cpu_usec" in last:
            wall = max(last["t"] - first["t"], 0.001)
            cpu = (last["cg_cpu_usec"] - first["cg_cpu_usec"]) / 1e6
            s.update({"peak_container_memory_gb": round(max(r.get("cg_memory_bytes", 0) for r in self.rows) / 1e9, 2),
                      "container_cpu_seconds": round(cpu, 1), "avg_container_cpu_cores": round(cpu / wall, 2),
                      "io_read_mb": round((last.get("cg_io_read_bytes", 0) - first.get("cg_io_read_bytes", 0)) / 1e6, 1),
                      "io_write_mb": round((last.get("cg_io_write_bytes", 0) - first.get("cg_io_write_bytes", 0)) / 1e6, 1)})
        for db in self.dbs:
            vals = [r[f"db_bytes_{db}"] for r in self.rows if f"db_bytes_{db}" in r]
            if vals:
                s[f"peak_db_mb_{db}"] = round(max(vals) / 1e6, 1)
        return s


def load_source(data: Path, db) -> dict:
    t0 = time.monotonic()
    for table, cols in gen.TYPES.items():
        ddl = ", ".join(f'"{c}" {t}' for c, t in cols.items())
        psql(db.database, f'CREATE TABLE public.{table} ({ddl}); ALTER TABLE public.{table} OWNER TO "{db.user}";')
        psql(db.database, f"\\copy public.{table} FROM '{data / (table + '.csv')}' WITH (FORMAT csv, HEADER true)",
             timeout=3600)
    return {"seconds": round(time.monotonic() - t0, 1)}


def run_once(name: str, timeout: int, wh_user: str | None = None, fault_at: str | None = None) -> dict:
    run_id = state.start_run("data", name)
    t0 = time.monotonic()
    deploy_mod.trigger_dag(name, run_id)
    status, faulted = "running", False
    while time.monotonic() - t0 < timeout:
        row = state.get_run(run_id)
        if row is not None and row["status"] != "running":
            status = row["status"]
            break
        if fault_at and not faulted and any(s["stage"] == fault_at and s["status"] == "running"
                                            for s in state.stages_for_run(run_id)):
            psql("postgres", f'ALTER ROLE "{wh_user}" NOLOGIN')
            psql("postgres", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{wh_user}'")
            faulted = True
        time.sleep(0.5)
    else:
        status = "timeout"
    stages = []
    for s in state.stages_for_run(run_id):
        d = None
        if s["finished_at"]:
            d = round((datetime.fromisoformat(s["finished_at"]) - datetime.fromisoformat(s["started_at"])).total_seconds(), 1)
        stages.append({"stage": s["stage"], "status": s["status"], "seconds": d, "rows": s["row_count"]})
    return {"run_id": run_id, "status": status, "wall_seconds": round(time.monotonic() - t0, 1), "stages": stages,
            "fault_injected": faulted}


def time_queries(db: str) -> dict:
    vid = psql(db, "SELECT video_id FROM gold.resources_useage_number ORDER BY video_id LIMIT 1")
    out = {}
    for k, sql in {"audit_measures": AUDIT_SQL, "one_video": AUDIT_SQL + f" WHERE video_id = '{vid}'"}.items():
        ms = []
        for _ in range(20):
            t0 = time.monotonic(); psql(db, sql); ms.append((time.monotonic() - t0) * 1000)
        out[k] = {"samples": 20, "median_ms": round(statistics.median(ms), 1), "max_ms": round(max(ms), 1),
                  "note": "psql round trip incl. process start, directly on the warehouse - not through Superset"}
    return out


def scale_run(scale: int, timeout: int) -> dict:
    ev = OUT / f"scale-{scale}"; ev.mkdir(parents=True, exist_ok=True)
    data, tmp = WORK / str(scale), WORK / f"pipelines-{scale}"
    tmp.mkdir(parents=True, exist_ok=True)
    res: dict = {"scale": scale, "synthetic": True}
    t0 = time.monotonic(); res["profile"] = gen.write(gen.generate(scale), data, scale, 20261023)
    res["generate_seconds"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic(); key = answer.summary(answer.gold(data))
    res["answer_key"], res["answer_seconds"] = key, round(time.monotonic() - t0, 1)
    (ev / "answer.json").write_text(json.dumps(key, indent=1))
    name, prefix = f"hg_bench_{scale}", f"hgbench/{uuid.uuid4().hex[:8]}"

    def retarget(work: Path):
        f = work / "pipeline.yaml"
        f.write_text(f.read_text().replace("prefix: bronze", f"prefix: {prefix}", 1).replace("HG_POC_", "HG_BENCH_"))
        for k in [k for k in os.environ if k.startswith("HG_POC_")]:
            os.environ[k.replace("HG_POC_", "HG_BENCH_")] = os.environ[k]

    cleanup: dict = {}
    src = wh = pipeline = None
    try:
        with pg_throwaway.throwaway_database(prefix="hgbench_src") as src, \
             pg_throwaway.throwaway_database(prefix="hgbench_wh") as wh:
            res["load"] = load_source(data, src)
            res["source_db_mb"] = round(int(psql("postgres", f"SELECT pg_database_size('{src.database}')")) / 1e6, 1)
            pipeline, _, _ = rm._hg_pipeline(tmp, mutate=retarget, name=name, source=rm.USAGE_DIR)
            overrides = {**fixture.env_overrides_for_source(pipeline, src),
                         **fixture.env_overrides_for_warehouse(pipeline, wh)}
            res["db_timezone"] = psql(wh.database, "SHOW timezone")
            with fixture._temporarily(overrides):
                try:
                    deploy_mod.deploy(pipeline, allow_draft=True)
                    deploy_mod.unpause_dag(name)
                    sampler = Sampler([src.database, wh.database]); sampler.start()
                    r1 = run_once(name, timeout); g1 = gold_summary(wh.database)
                    sampler.stop_.set(); sampler.join()
                    res["resources_first_run"] = sampler.summary()
                    (ev / "samples.json").write_text(json.dumps(sampler.rows))
                    res["first_run"], res["first_check"] = r1, {"ok": matches(g1, key), "gold": g1}
                    r2 = run_once(name, timeout); g2 = gold_summary(wh.database)
                    res["rerun"], res["rerun_check"] = r2, {"ok": matches(g2, key), "gold": g2}
                    try:
                        r3 = run_once(name, timeout, wh_user=wh.user, fault_at="silver")
                    finally:
                        psql("postgres", f'ALTER ROLE "{wh.user}" LOGIN')
                    g3 = gold_summary(wh.database)
                    res["fault_run"] = r3
                    res["fault_check"] = {"failed_as_expected": r3["status"] == "failed" and r3["fault_injected"],
                                          "gold_kept_previous_result": matches(g3, key), "gold": g3}
                    r4 = run_once(name, timeout); g4 = gold_summary(wh.database)
                    res["recovery"], res["recovery_check"] = r4, {"ok": matches(g4, key), "gold": g4}
                    res["dashboard_sql"] = time_queries(wh.database)
                    res["warehouse_db_mb"] = round(int(psql("postgres", f"SELECT pg_database_size('{wh.database}')")) / 1e6, 1)
                finally:
                    try:
                        undone = deploy_mod.undeploy(pipeline)
                        cleanup["undeploy"] = fixture._verify_cleanup_complete(pipeline, undone, deploy_mod)[0]
                    except Exception as exc:     # noqa: BLE001
                        cleanup["undeploy"] = f"error: {exc}"
                    try:
                        bronze.purge_namespace(pipeline, prefix)
                        cleanup["bronze_objects_left"] = bronze.count_namespace(pipeline, prefix)
                    except Exception as exc:     # noqa: BLE001
                        cleanup["bronze_objects_left"] = f"error: {exc}"
    except Exception as exc:                     # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"[:800]
    finally:
        cleanup["databases_dropped"] = bool(src and wh and src.cleanup_ok and wh.cleanup_ok)
        shutil.rmtree(data, ignore_errors=True); shutil.rmtree(tmp, ignore_errors=True)
        cleanup["generated_files_removed"] = not data.exists()
        res["cleanup"] = cleanup
    res["ok"] = bool(not res.get("error") and res.get("first_check", {}).get("ok") and res.get("rerun_check", {}).get("ok")
                     and res.get("fault_check", {}).get("failed_as_expected")
                     and res.get("fault_check", {}).get("gold_kept_previous_result")
                     and res.get("recovery_check", {}).get("ok") and cleanup.get("undeploy") is True
                     and cleanup.get("bronze_objects_left") == 0 and cleanup.get("databases_dropped"))
    (ev / "result.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def parse(argv: list[str]) -> tuple[list[int], dict]:
    opts = {"--timeout": 3600.0, "--min-free-gb": 10.0, "--min-avail-gb": 3.0}
    scales, i = [], 0
    while i < len(argv):
        if argv[i] in opts:
            opts[argv[i]] = float(argv[i + 1]); i += 2
        else:
            scales.append(int(argv[i])); i += 1
    return scales, opts


def main(argv: list[str]) -> int:
    scales, o = parse(argv)
    timeout = int(o["--timeout"])
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {"synthetic": True, "not_hg_data": True, "scales": [],
               "thresholds": {"timeout_s": timeout, "min_free_gb": o["--min-free-gb"], "min_avail_gb": o["--min-avail-gb"]}}
    stop = ""
    for scale in scales:
        pre = {"free_gb": round(free_gb(), 1), "avail_gb": round(avail_gb(), 1)}
        if stop or pre["free_gb"] < o["--min-free-gb"] or pre["avail_gb"] < o["--min-avail-gb"]:
            stop = stop or f"below a threshold before scale {scale}: {pre}"
            summary["scales"].append({"scale": scale, "refused": stop, "preflight": pre})
            continue
        r = scale_run(scale, timeout)
        r["preflight"] = pre
        summary["scales"].append({k: r.get(k) for k in ("scale", "ok", "error", "preflight", "profile", "load",
                                                         "answer_key", "first_run", "first_check", "resources_first_run",
                                                         "rerun", "fault_run", "fault_check", "recovery",
                                                         "dashboard_sql", "source_db_mb", "warehouse_db_mb", "db_timezone",
                                                         "cleanup")})
        if not r["ok"] or r.get("first_run", {}).get("status") == "timeout":
            stop = f"scale {scale} did not pass - larger scales not run"
        (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps([{k: s.get(k) for k in ("scale", "ok", "refused", "error")} for s in summary["scales"]]))
    return 0 if summary["scales"] and all(s.get("ok") for s in summary["scales"]) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
