"""`dpagent dashboard synth` with a FAKE provider (tests/llm_verification/scripted.py): the spec checks, the re-ask,
blockers, the request ceiling, the rendering into the superset-assets bundle format (checked with that pack's own
static reader), stable identities, and that the model never sees the expected rows or the KPI SQL. Nothing here calls
a real provider or a real Superset."""
import importlib.util
import json
import sys
from pathlib import Path

import litellm
import pytest
import yaml
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent / "llm_verification"))
import scripted  # noqa: E402

from dpagent.cli import main as cli  # noqa: E402
from dpagent.dashboards import synth as dash  # noqa: E402
from dpagent.llm import client as llm  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "tests" / "llm_verification" / "dashboard_cases"
spec_ = importlib.util.spec_from_file_location("assets", REPO / "packs" / "superset-assets" / "assets.py")
assets = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(assets)
BUSY = Exception("litellm.ServiceUnavailableError: 503 high demand")

FULFIL = {"title": "Delivery SLA by month", "dataset": {"schema": "fulfilment_sla", "table": "fct_delivery_sla"},
          "charts": [{"name": "sla_by_month", "title": "On-time delivery by order month", "type": "table",
                      "groupby": ["month"],
                      "metrics": ["eligible_orders", "delivered_orders", "on_time_orders", "on_time_rate"]}],
          "filters": [{"name": "month", "label": "Month", "column": "month", "type": "select"}],
          "mapping": "- per month -> sla_by_month\n- pick months -> month"}
SALES = {"title": "Monthly revenue", "dataset": {"schema": "quickstart_sales", "table": "fct_monthly_revenue"},
         "charts": [{"name": "revenue_by_month", "title": "Orders and revenue", "type": "table", "groupby": ["month"],
                     "metrics": ["orders", "revenue"]}],
         "filters": [{"name": "month", "label": "Month", "column": "month", "type": "select"}], "mapping": "-"}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key-not-real-0123456789")
    monkeypatch.setenv("DPAGENT_MODEL", "gemini/test-model")
    monkeypatch.delenv("DPAGENT_LLM_LEDGER", raising=False)
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    monkeypatch.setattr(llm, "_abandoned", [])

    def install(replies):
        p = scripted.Provider(replies)
        monkeypatch.setattr(litellm, "completion", p)
        return p
    return install


def inputs(case, project):
    d = CASES / case
    return dash.load_inputs(project, d / "request.md", d / "gold_schema.yaml", d / "kpis.yaml",
                            d / "expected.yaml" if (d / "expected.yaml").exists() else None)


def synth(tmp_path, case, project, *extra):
    d = CASES / case
    args = ["dashboard", "synth", project, "--request", str(d / "request.md"), "--schema", str(d / "gold_schema.yaml"),
            "--kpis", str(d / "kpis.yaml"), "--out-dir", str(tmp_path / "projects"),
            "--trace-dir", str(tmp_path / "trace"), *extra]
    if (d / "expected.yaml").exists():
        args += ["--expected", str(d / "expected.yaml")]
    return CliRunner().invoke(cli, args)


def static_read(tmp_path, monkeypatch, project):
    """The superset-assets pack's own reader: UUIDs, references, SELECT-only connection, expected binding."""
    monkeypatch.setattr(assets, "ROOT", tmp_path)
    return assets.read_project({"name": project, "bundle_dir": f"projects/{project}/bundle",
                                "expected_file": f"projects/{project}/expected.yaml"}, {"host": "x"})


@pytest.mark.parametrize("case, project, spec", [("fulfil_sla", "fulfil-sla-ai", FULFIL),
                                                 ("sales_revenue", "sales-revenue-ai", SALES)])
def test_two_projects_render_into_bundles_the_pack_accepts(provider, tmp_path, monkeypatch, case, project, spec):
    p = provider([spec])
    r = synth(tmp_path, case, project)
    assert r.exit_code == 0, r.output
    assert "classification: llm-first-draft" in r.output and "requests sent: 1 (ceiling 4" in r.output
    proj = static_read(tmp_path, monkeypatch, project)
    assert len(proj["resources"]["charts"]) == 1 and len(proj["expected"]["checks"]) == 1
    ds = next(iter(proj["resources"]["datasets"].values()))
    kpis = {k["name"]: k["expression"] for k in yaml.safe_load((CASES / case / "kpis.yaml").read_text())["kpis"]}
    assert {m["metric_name"]: m["expression"] for m in ds["metrics"]} == {m: kpis[m] for m in spec["charts"][0]["metrics"]}
    check = proj["expected"]["checks"][0]
    assert check["chart_uuid"] in proj["resources"]["charts"]
    assert check["filters"][0]["native_filter_id"] == "NATIVE_FILTER-month"
    origin = (tmp_path / "projects" / project / "ORIGIN.md").read_text()
    assert "drafted by a model" in origin and "not approved" in origin.lower()
    user = p.calls[0]["user"]
    for leak in ("66.7", "200.5", "SUM(", "MAX(", "expected"):
        assert leak not in user                    # no expected rows, no KPI SQL
    assert json.loads((tmp_path / "trace" / "trace.json").read_text())["classification"] == "llm-first-draft"


def test_the_same_spec_renders_the_same_bytes_and_identities(tmp_path):
    inp = inputs("fulfil_sla", "fulfil-sla-ai")
    assert dash.render(inp, FULFIL) == dash.render(inp, json.loads(json.dumps(FULFIL)))
    changed = {**FULFIL, "title": "Delivery SLA (v2)"}
    a, b = dash.render(inp, FULFIL), dash.render(inp, changed)
    assert [d.get("uuid") for d in a.values()] == [d.get("uuid") for d in b.values()]   # an update, not a copy
    other = dash.render(inputs("fulfil_sla", "another-project"), FULFIL)
    assert not {d.get("uuid") for d in a.values()} - {None} & {d.get("uuid") for d in other.values()}


@pytest.mark.parametrize("bad, problem", [
    ({"charts": [{**FULFIL["charts"][0], "metrics": ["eligible_orders", "late_orders"]}]}, "'late_orders' is not a defined KPI"),
    ({"charts": [{**FULFIL["charts"][0], "groupby": ["week"]}]}, "groupby column 'week' is not a column"),
    ({"charts": [{**FULFIL["charts"][0], "type": "pie"}]}, "type 'pie' is not supported"),
    ({"filters": [{"name": "m", "label": "M", "column": "month", "type": "date_range"}]}, "type 'date_range' is not supported"),
    ({"dataset": {"schema": "fulfilment_sla", "table": "orders"}}, "is not a table of the verified gold schema"),
    ({"filters": []}, "needs a filter on 'month'"),
    ({"charts": [{**FULFIL["charts"][0], "metrics": ["eligible_orders"]}]}, "needs a view by ['month']"),
    ({"sql": "SELECT 1"}, "unknown key 'sql'"),
])
def test_every_name_is_checked(bad, problem):
    problems = dash.check_spec(inputs("fulfil_sla", "x-ai"), {**FULFIL, **bad})
    assert any(problem in p for p in problems), problems


def test_a_refused_spec_is_reasked_with_the_problems_and_fixed(provider, tmp_path):
    wrong = {**FULFIL, "charts": [{**FULFIL["charts"][0], "metrics": ["eligible_orders", "on_time_pct"]}]}
    p = provider([wrong, FULFIL])
    r = synth(tmp_path, "fulfil_sla", "fulfil-sla-ai")
    assert r.exit_code == 0 and "llm-after-1-revision(s)" in r.output
    assert "'on_time_pct' is not a defined KPI" in p.calls[1]["user"] and "66.7" not in p.calls[1]["user"]


def test_still_wrong_after_the_revisions_writes_nothing(provider, tmp_path):
    wrong = {**FULFIL, "filters": []}
    provider([wrong, wrong, wrong])
    r = synth(tmp_path, "fulfil_sla", "fulfil-sla-ai")
    assert r.exit_code == 1 and "not-rendered" in r.output and not (tmp_path / "projects").exists()


@pytest.mark.parametrize("case", ["sales_aov_undefined", "sales_pie_unsupported"])
def test_a_blocker_is_shown_and_nothing_is_written(provider, tmp_path, case):
    provider([{"blockers": [{"question": "How is it defined?", "why_it_matters": "it changes the numbers"}]}])
    r = synth(tmp_path, case, "sales-ai")
    assert r.exit_code == 1 and "asked-blocker" in r.output and "? How is it defined?" in r.output
    assert not (tmp_path / "projects").exists()


def test_a_busy_provider_counts_against_the_ceiling(provider, tmp_path):
    p = provider([BUSY, BUSY, BUSY])
    r = synth(tmp_path, "fulfil_sla", "fulfil-sla-ai", "--max-calls", "2", "--transient-retries", "5")
    assert r.exit_code == 1 and len(p.calls) == 2 and "inconclusive-provider-unavailable" in r.output


def test_an_existing_project_is_kept(provider, tmp_path):
    (tmp_path / "projects" / "fulfil-sla-ai").mkdir(parents=True)
    r = synth(tmp_path, "fulfil_sla", "fulfil-sla-ai")
    assert r.exit_code == 1 and "exists" in r.output


def test_the_shared_prompt_and_code_name_no_project():
    texts = [llm.load_prompt("synth_dashboard"), (REPO / "src/dpagent/dashboards/synth.py").read_text()]
    for t in texts:
        for name in ("fulfil", "sales", "revenue", "delivery", "HG", "hg_"):
            assert name not in t
