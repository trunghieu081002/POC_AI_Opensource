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
    assert bench.index("preflight.py") < bench.index("generate.py")
    runner = (ROOT / "scripts/quickstart-acceptance-ci.sh").read_text()
    assert runner.index("preflight.py") < runner.index("docker build")
