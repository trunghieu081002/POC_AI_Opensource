"""The quickstart project (docs/quickstart.md), offline: its answer key is the BRD applied in plain Python,
the BI checks agree with it, the bundle is valid for the superset-assets reader, `fixture-sql` emits exactly
what validation seeds, and the walkthrough never approves anything by itself."""
import importlib.util
import re
import subprocess
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import yaml
from click.testing import CliRunner

from dpagent.cli import main as cli
from dpagent.pipelines import fixture, loader

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "pipelines" / "quickstart_sales"
BI = ROOT / "projects" / "quickstart-sales"
Q = ROOT / "examples" / "quickstart"


def test_expected_is_the_rule_applied_in_plain_python():
    rows = yaml.safe_load((P / "fixture.yaml").read_text())["tables"][0]["rows"]
    agg = defaultdict(lambda: [0, Decimal("0")])
    for r in rows:
        if r["status"].strip().lower() == "done":
            month = r["order_date"][:7]
            agg[month][0] += 1
            agg[month][1] += Decimal(r["amount"])
    want = sorted((m, n, s) for m, (n, s) in agg.items())
    exp = yaml.safe_load((P / "expected.yaml").read_text())
    got = sorted((r["month"], r["orders"], Decimal(r["revenue"])) for r in exp["rows"])
    assert got == want and exp["row_count"] == len(want) == 2


def test_the_bi_checks_say_the_same_numbers_as_the_pipeline_expected():
    exp = {r["month"]: (r["orders"], Decimal(r["revenue"])) for r in yaml.safe_load((P / "expected.yaml").read_text())["rows"]}
    check = yaml.safe_load((BI / "expected.yaml").read_text())["checks"][0]
    assert {str(r["month"]): (r["orders"], Decimal(str(r["revenue"]))) for r in check["rows"]} == exp
    jan = check["filters"][0]
    assert jan["values"] == ["2026-01"] and [str(r["month"]) for r in jan["rows"]] == ["2026-01"]


def test_the_pipeline_loads_lints_and_keeps_every_connection_in_the_environment():
    p = loader.load("quickstart_sales", ROOT / "pipelines")
    assert p.maturity == "draft" and [s.name for s in p.stages] == ["landing", "raw", "curated"]
    text = (P / "pipeline.yaml").read_text()
    for key in ("host", "database", "user", "password"):
        assert re.search(rf"{key}: \"\$\{{QS_(SRC|WH)_", text), key
    assert CliRunner().invoke(cli, ["pipeline", "lint", "quickstart_sales"]).exit_code == 0


def test_the_bundle_is_valid_for_the_assets_reader_and_owns_new_uuids():
    spec = importlib.util.spec_from_file_location("assets", ROOT / "packs/superset-assets/assets.py")
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    cfg = {"name": "quickstart-sales", "revision": "v1", "bundle_dir": "projects/quickstart-sales/bundle",
           "expected_file": "projects/quickstart-sales/expected.yaml"}
    import os
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        project = a.read_project(cfg, {"environment": "dev"})
    finally:
        os.chdir(cwd)
    mine = {k for v in project["resources"].values() for k in v}
    others = set()
    for other in ("hg-usage", "monthly-sales"):
        for f in (ROOT / "projects" / other / "bundle").rglob("*.yaml"):
            others |= set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", f.read_text()))
    assert len(mine) == 4 and not mine & others


def test_fixture_sql_is_exactly_what_validation_seeds():
    fx = fixture.load_fixture(P / "fixture.yaml")
    statements = [s for _, s in fixture.seed_statements(fx)]
    out = CliRunner().invoke(cli, ["pipeline", "fixture-sql", str(P / "fixture.yaml")])
    assert out.exit_code == 0, out.output
    lines = out.output.strip().splitlines()
    assert lines[0] == "BEGIN;" and lines[-1] == "COMMIT;" and lines[1:-1] == statements
    assert len(statements) == 1 + 6 and statements[0].startswith('CREATE TABLE "orders"')


def test_seed_source_uses_the_same_statements(monkeypatch):
    seen = []
    monkeypatch.setattr(fixture, "_psql", lambda db, *args, **kw: seen.append(args[-1]) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    fx = fixture.load_fixture(P / "fixture.yaml")
    fixture.seed_source(fx, object())
    assert seen == [s for _, s in fixture.seed_statements(fx)]


def test_the_walkthrough_never_approves_by_itself():
    text = (Q / "walkthrough.sh").read_text()
    assert "QS_BI_APPROVED_SHA256=" not in text and "approval.promote" not in text
    assert "need QS_APPROVED_BY" in text and "need QS_BI_APPROVED_BY QS_BI_APPROVED_SHA256" in text
    assert re.search(r"dpagent pipeline promote .*--approved-by \"\$QS_APPROVED_BY\"", text)


def test_specs_hold_no_secret_values_only_environment_references():
    for name in ("stack.yaml", "bi.yaml"):
        doc = (Q / name).read_text()
        for key in re.findall(r"(?:password|secret_key)\s*:\s*(.+)", doc):
            assert key.strip().strip('"').startswith("${"), (name, key)
    env = (Q / "dev.env.example").read_text()
    assert all(v in ("change-me", "change-me-viewer", "change-me-to-at-least-32-random-characters") for k, v in
               (line.split("=", 1) for line in env.splitlines() if "=" in line and not line.startswith("#"))
               if "PASSWORD" in k or "SECRET" in k)


def _assets():
    spec = importlib.util.spec_from_file_location("assets", ROOT / "packs/superset-assets/assets.py")
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    return a


def _project(a, name, **extra):
    import os
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        return a.read_project({"name": name, "revision": "v1", "bundle_dir": f"projects/{name}/bundle",
                               "expected_file": f"projects/{name}/expected.yaml", **extra}, {"environment": "dev"})
    finally:
        os.chdir(cwd)


def test_viewers_are_part_of_the_approved_fingerprint_but_passwords_are_not():
    a = _assets()
    plain = _project(a, "quickstart-sales")
    one = _project(a, "quickstart-sales", viewers=[{"username": "qs.sales.viewer", "password_env": "A_PASSWORD"}])
    other_env = _project(a, "quickstart-sales", viewers=[{"username": "qs.sales.viewer", "password_env": "B_PASSWORD"}])
    two = _project(a, "quickstart-sales", viewers=[{"username": "qs.sales.viewer", "password_env": "A_PASSWORD"},
                                                    {"username": "qs.extra", "password_env": "A_PASSWORD"}])
    assert plain["fingerprint"] != one["fingerprint"] != two["fingerprint"]
    assert one["fingerprint"] == other_env["fingerprint"]          # where the password comes from is not access


import pytest  # noqa: E402


@pytest.mark.parametrize("viewers", [
    [{"username": "qs.viewer", "password": "inline-secret"}],                  # a password in config
    [{"username": "qs.viewer", "password_env": "lowercase"}],
    [{"username": "Admin", "password_env": "X_PASSWORD"}],
    [{"username": "qs.viewer", "password_env": "X_PASSWORD"}, {"username": "qs.viewer", "password_env": "X_PASSWORD"}],
    "qs.viewer",
])
def test_a_malformed_viewer_config_is_refused(viewers):
    with pytest.raises(ValueError):
        _assets().read_viewers({"viewers": viewers})


def test_the_two_projects_own_disjoint_uuids_and_different_data():
    a = _assets()
    sales, ops = _project(a, "quickstart-sales"), _project(a, "quickstart-ops")
    keys = lambda p: {k for v in p["resources"].values() for k in v}   # noqa: E731
    assert not keys(sales) & keys(ops)
    ds = lambda p: next(iter(p["resources"]["datasets"].values()))["table_name"]   # noqa: E731
    assert (ds(sales), ds(ops)) == ("fct_monthly_revenue", "stg_orders")


def test_ops_expected_is_the_fixture_counted_by_normalised_status():
    rows = yaml.safe_load((P / "fixture.yaml").read_text())["tables"][0]["rows"]
    from collections import Counter
    want = Counter(r["status"].strip().lower() for r in rows)
    check = yaml.safe_load((ROOT / "projects/quickstart-ops/expected.yaml").read_text())["checks"][0]
    assert {r["status"]: r["orders"] for r in check["rows"]} == dict(want)
    assert check["filters"][0]["rows"] == [{"status": "done", "orders": want["done"]}]


def test_the_bi_spec_names_a_viewer_per_project_by_environment_variable_only():
    doc = yaml.safe_load((Q / "bi.yaml").read_text())
    projects = next(c["superset-assets"] for c in doc["components"] if "superset-assets" in c)["projects"]
    assert {p["name"]: [v["username"] for v in p["viewers"]] for p in projects} == {
        "quickstart-sales": ["qs.sales.viewer"], "quickstart-ops": ["qs.ops.viewer"]}
    assert all(set(v) == {"username", "password_env"} for p in projects for v in p["viewers"])


def test_the_runner_can_target_an_oracle_linux_8_container_and_says_what_that_does_not_prove():
    sh = (ROOT / "scripts/quickstart-acceptance-ci.sh").read_text()
    assert "--os ubuntu|ol8" in sh and "m25-disposable-host-ol8.Dockerfile" in sh
    assert "DPAGENT_PYTHON=/usr/bin/python3.11" in sh                 # EL8's system python is 3.6: never replaced
    assert "SELinux is NOT exercised" in sh and "no firewall is exercised" in sh
    assert "SYSTEM PYTHON CHANGED" in sh                              # the system python is compared before/after
    assert "setenforce" not in sh and "systemctl stop firewalld" not in sh and "disable" not in sh.lower().replace("disabled", "")
    docker = (ROOT / "scripts/m25-disposable-host-ol8.Dockerfile").read_text()
    assert docker.startswith("# An Oracle Linux 8.10 userland") and "FROM oraclelinux:8" in docker
    assert "CONTAINER" in docker and "NOT a native EL8 install" in docker
