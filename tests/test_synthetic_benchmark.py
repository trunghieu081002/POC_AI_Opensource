"""The synthetic benchmark (benchmarks/synthetic/), offline: fixed-seed data is reproducible, the answer key the
generator computes equals the business rule re-applied to the generated CSV by plain parsing, the independent
check catches a wrong warehouse, and the preflight refuses a scale the machine cannot hold."""
import copy
import csv
import importlib.util
import io
import json
import subprocess
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
B = ROOT / "benchmarks" / "synthetic"


def load(name):
    spec = importlib.util.spec_from_file_location("bench_" + name, B / f"{name}.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


gen, check, preflight = load("generate"), load("check"), load("preflight")


@pytest.fixture
def config():
    c = gen.load_config()
    c["scales"]["unit"] = {"rows": 3000, "min_free_disk_gb": 1, "min_available_ram_gb": 1, "timeout_s": 60}
    return c


def run(config, scale="unit"):
    out = io.StringIO()
    expected = gen.generate(config, scale, out)
    return expected, out.getvalue()


def test_the_label_never_claims_hg_load_and_scales_grow():
    c = gen.load_config()
    assert c["label"] == "synthetic benchmark, not HG load"
    rows = [s["rows"] for s in c["scales"].values()]
    assert rows == sorted(rows) and len(set(rows)) == len(rows)


def test_same_seed_same_bytes_other_seed_other_bytes(config):
    (a, csv_a), (b, csv_b) = run(config), run(config)
    assert csv_a == csv_b and a == b
    other = copy.deepcopy(config); other["seed"] += 1
    assert run(other)[0]["csv_sha256"] != a["csv_sha256"]


def test_the_answer_key_is_the_rule_reapplied_to_the_csv(config):
    expected, text = run(config)
    months, ids = defaultdict(lambda: [0, Decimal(0)]), []
    for i, day, status, amount, customer in csv.reader(io.StringIO(text)):
        ids.append(int(i))
        if status.strip().lower() == "done":
            months[day[:7]][0] += 1; months[day[:7]][1] += Decimal(amount)
    assert len(ids) == len(set(ids)) == expected["invariants"]["landing_rows"] == 3000
    assert {r["month"]: (r["orders"], Decimal(r["revenue"])) for r in expected["expected"]} == \
           {k: (v[0], v[1]) for k, v in months.items()}
    assert sum(v[0] for v in months.values()) == expected["invariants"]["done_orders"]
    statuses = {row[2] for row in csv.reader(io.StringIO(text))}
    assert {" DONE ", "done", "cancelled", "draft"} == statuses   # the normalisation is exercised


def test_the_independent_check_passes_on_the_key_and_catches_a_wrong_warehouse(config):
    expected, _ = run(config)
    n = expected["invariants"]["landing_rows"]
    assert check.compare(expected, expected["expected"], n, n)["ok"]
    wrong = copy.deepcopy(expected["expected"]); wrong[0]["revenue"] = str(Decimal(wrong[0]["revenue"]) + Decimal("0.01"))
    result = check.compare(expected, wrong, n, n)
    assert not result["ok"] and not result["checks"]["fact_equals_expected"] and result["first_mismatch"]
    assert not check.compare(expected, expected["expected"], n + 1, n + 1)["ok"]     # a duplicated landing row
    assert not check.compare(expected, expected["expected"], n, n - 1)["ok"]         # a repeated id


def test_preflight_refuses_what_the_machine_cannot_hold():
    scale = {"rows": 10, "min_free_disk_gb": 40, "min_available_ram_gb": 8, "timeout_s": 60}
    gb = preflight.GB
    assert preflight.decide(scale, 50 * gb, 16 * gb)["ok"]
    refused = preflight.decide(scale, 39 * gb, 3 * gb)
    assert not refused["ok"] and len(refused["reasons"]) == 2


def test_preflight_cli_exits_4_when_refused(tmp_path):
    c = yaml.safe_load((B / "config.yaml").read_text())
    c["scales"]["huge"] = {"rows": 1, "min_free_disk_gb": 10 ** 9, "min_available_ram_gb": 1, "timeout_s": 1}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(c))
    proc = subprocess.run(["python3", str(B / "preflight.py"), "huge", "--disk-path", str(tmp_path),
                           "--config", str(tmp_path / "c.yaml")], capture_output=True, text=True)
    assert proc.returncode == 4 and json.loads(proc.stdout)["ok"] is False


def test_bench_cleans_up_on_any_exit_and_the_runner_preflights_before_building():
    bench = (B / "bench.sh").read_text()
    assert "trap cleanup EXIT" in bench and "DROP SCHEMA" in bench and "dpagent pipeline undeploy" in bench
    assert bench.index("preflight.py") < bench.index("trap cleanup EXIT") < bench.index("setup-source")
    runner = (ROOT / "scripts/quickstart-acceptance-ci.sh").read_text()
    assert runner.index("preflight.py") < runner.index("docker build")


# ---- bench.sh cleanup under failure, with fake host commands (state kept in files)

FAKE_PG = r'''#!/usr/bin/env python3
# runuser -u postgres -- psql -X -At -v ON_ERROR_STOP=1 -d DB -c SQL   (fake: state in $FAKE_STATE)
import os, re, sys
from pathlib import Path
st = Path(os.environ["FAKE_STATE"]); a = sys.argv
sql = a[a.index("-c") + 1] if "-c" in a else ""
def lines(n): p = st / n; return [l for l in p.read_text().split("\n") if l] if p.exists() else []
def save(n, v): (st / n).write_text("".join(x + "\n" for x in v))
if os.environ.get("FAKE_PG_FAIL") and re.search(os.environ["FAKE_PG_FAIL"], sql):
    sys.exit("psql: connection refused (injected)")
if "SELECT nspname" in sql: print("\n".join(lines("schemas")))
elif sql.startswith("DROP SCHEMA"): save("schemas", [s for s in lines("schemas") if s != sql.split('"')[1]])
elif "FROM pg_namespace" in sql: print(len(lines("schemas")))
elif sql.startswith("DROP TABLE"): save("tables", [t for t in lines("tables") if t != "orders"])
elif "FROM pg_tables" in sql: print(sum(1 for t in lines("tables") if t == "orders"))
elif "pg_database_size" in sql: print(1000)
'''
FAKE_SU = r'''#!/usr/bin/env bash
# su airflow -s /bin/bash -c "... airflow dags list -o plain"
[ -n "${FAKE_AIRFLOW_FAIL:-}" ] && { echo "airflow: database is locked (injected)" >&2; exit 1; }
echo "dag_id | fileloc"; cat "$FAKE_STATE/dags" 2>/dev/null; true
'''
FAKE_DPAGENT = r'''#!/usr/bin/env bash
case "$1 $2" in
  "pipeline undeploy") [ -n "${FAKE_UNDEPLOY_FAIL:-}" ] && exit 1; : > "$FAKE_STATE/dags" ;;
  "pipeline run") [ -n "${FAKE_RUN_FAIL:-}" ] && { echo "run failed (injected)"; exit 1; } ;;
esac
'''
FAKE_WALKTHROUGH = r'''#!/usr/bin/env bash
# fake product steps: each creates what the real one creates, and can fail half way (FAKE_FAIL_AT=<step>)
echo "$1" >> "$FAKE_STATE/calls"
case "$1" in
  source)   echo orders >> "$FAKE_STATE/tables" ;;
  validate) echo quickstart_sales__validation_1 >> "$FAKE_STATE/schemas" ;;
  deploy)   echo quickstart_sales_landing >> "$FAKE_STATE/schemas"; echo quickstart_sales >> "$FAKE_STATE/dags" ;;
esac
[ "${FAKE_FAIL_AT:-}" = "$1" ] && { echo "$1 failed half way (injected)"; exit 1; }
exit 0
'''
FAKE_PSQL = "#!/usr/bin/env bash\nexit 0\n"   # the source load


@pytest.fixture
def host(tmp_path):
    import os
    import shutil
    import sys
    repo, bin_, st, out = (tmp_path / d for d in ("repo", "bin", "state", "out"))
    for d in (repo, bin_, st, out):
        d.mkdir()
    shutil.copytree(B, repo / "benchmarks" / "synthetic")
    c = yaml.safe_load((B / "config.yaml").read_text())
    c["scales"]["unit"] = {"rows": 200, "min_free_disk_gb": 0, "min_available_ram_gb": 0, "timeout_s": 60}
    c["scales"]["huge"] = {"rows": 200, "min_free_disk_gb": 10 ** 9, "min_available_ram_gb": 0, "timeout_s": 60}
    (repo / "test-config.yaml").write_text(yaml.safe_dump(c))
    for name, body in (("runuser", FAKE_PG), ("su", FAKE_SU), ("dpagent", FAKE_DPAGENT), ("psql", FAKE_PSQL)):
        (bin_ / name).write_text(body); (bin_ / name).chmod(0o755)
    (repo / "wt.sh").write_text(FAKE_WALKTHROUGH)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", FAKE_STATE=str(st), BENCH_REPO=str(repo),
               BENCH_WALKTHROUGH=str(repo / "wt.sh"), BENCH_PY=sys.executable, BENCH_CONFIG=str(repo / "test-config.yaml"),
               BENCH_DATA=str(tmp_path / "orders.csv"), BENCH_JOURNAL=str(tmp_path / "none.db"),
               BENCH_DISK_PATH=str(tmp_path), QS_SRC_NAME="qs_source", QS_SRC_USER="u", QS_SRC_PASSWORD="p")
    return {"env": env, "state": st, "out": out, "data": tmp_path / "orders.csv"}


def bench(host, scale="unit", **faults):
    env = dict(host["env"], **{k: "1" if v is True else v for k, v in faults.items()})
    proc = subprocess.run(["bash", str(B / "bench.sh"), scale, str(host["out"])], env=env,
                          capture_output=True, text=True, timeout=120)
    cleanup = json.loads((host["out"] / "bench" / "cleanup.json").read_text())
    return proc, cleanup


def left(host, name):
    p = host["state"] / name
    return [l for l in p.read_text().split("\n") if l] if p.exists() else []


def test_a_setup_step_failing_half_way_still_gets_everything_it_created_removed(host):
    proc, cleanup = bench(host, FAKE_FAIL_AT="deploy")          # deploy created the DAG and a schema, then failed
    assert proc.returncode == 1                                   # the step's failure, not hidden by cleanup
    assert cleanup["ok"] is True and cleanup["created"] == ["source_table", "warehouse_schemas", "pipeline"]
    assert left(host, "dags") == [] and left(host, "schemas") == [] and left(host, "tables") == []
    for r in cleanup["resources"]:
        assert all(s["rc"] == 0 for s in r["steps"]) and any(s["step"].startswith("audit") for s in r["steps"])


def test_the_inside_preflight_refuses_before_anything_is_created(host):
    proc, cleanup = bench(host, scale="huge")
    assert proc.returncode == 4
    assert cleanup == {"ok": True, "created": [], "resources": []}
    assert not (host["state"] / "calls").exists()                 # no product step ran
    assert "nothing created" in (host["out"] / "bench" / "steps.log").read_text()


def test_a_failing_airflow_cli_is_not_counted_as_the_dag_being_gone(host):
    proc, cleanup = bench(host, FAKE_FAIL_AT="deploy", FAKE_AIRFLOW_FAIL=True)
    assert proc.returncode == 1                                   # still the original failure
    assert cleanup["ok"] is False
    pipeline = next(r for r in cleanup["resources"] if r["resource"] == "pipeline")
    audit = next(s for s in pipeline["steps"] if s["step"] == "audit-dag")
    assert audit["rc"] != 0 and audit["remaining"] == "unknown" and not pipeline["removed_and_verified"]
    assert "CLEANUP NOT PROVEN" in (host["out"] / "bench" / "steps.log").read_text()


def test_a_failing_cleanup_query_is_never_counted_as_nothing_left(host):
    # the run fails, then the audit query of the source table fails: "unknown", cleanup not proven
    proc, cleanup = bench(host, FAKE_RUN_FAIL=True, FAKE_PG_FAIL="FROM pg_tables")
    assert proc.returncode == 1 and cleanup["ok"] is False
    source = next(r for r in cleanup["resources"] if r["resource"] == "source_table")
    assert next(s for s in source["steps"] if s["step"] == "audit")["remaining"] == "unknown"


def test_a_failed_run_after_load_removes_the_data_file_and_stops_the_sampler(host):
    proc, cleanup = bench(host, FAKE_RUN_FAIL=True)
    assert proc.returncode == 1 and cleanup["ok"] is True
    assert "data_file" in cleanup["created"] and not host["data"].exists()
    assert left(host, "tables") == [] and left(host, "schemas") == [] and left(host, "dags") == []


def test_an_undeploy_failure_is_not_hidden_by_an_empty_dag_list(host):
    proc, cleanup = bench(host, FAKE_FAIL_AT="deploy", FAKE_UNDEPLOY_FAIL=True)
    pipeline = next(r for r in cleanup["resources"] if r["resource"] == "pipeline")
    assert cleanup["ok"] is False and next(s for s in pipeline["steps"] if s["step"] == "undeploy")["rc"] == 1
