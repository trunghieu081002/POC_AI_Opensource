"""A model drafts a dashboard SPEC; DPAgent checks it and renders the native Superset bundle (docs/ai-dashboards.md).

What the model is given - all written or verified by a person:
  * the report request (what the dashboard is for, who reads it);
  * the verified gold schema: tables and columns that exist in the warehouse, with types and meanings;
  * the KPI definitions: each metric's name, meaning and SQL aggregate - the ONLY metrics a dashboard may show;
  * the catalog of what DPAgent can render: chart type `table`, filter type `select`.

What the model returns is a small JSON spec - never Superset YAML, never SQL, never a UUID:
  {"blockers": [{"question", "why_it_matters"}]}                       when it cannot build it as asked, or
  {"title", "dataset": {"schema", "table"}, "charts": [{"name", "title", "type", "groupby", "metrics"}],
   "filters": [{"name", "label", "column", "type"}], "mapping"}

DPAgent then checks every name against the schema, the KPI definitions and the catalog, checks that the views the
reviewer requires are present, and renders the bundle itself: metric expressions copied from the KPI definitions,
column types from the schema, stable uuid5 identities from (project, kind, name) so a re-render updates the same
Superset objects instead of creating new ones. Nothing here talks to Superset: validation against real data, the
reviewer's approval, deploy, redeploy and undeploy are the superset-assets pack's existing modes.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dpagent.llm import client as llm

CATALOG = {"chart_types": {"table": "a table: one row per combination of its `groupby` columns, one column per metric"},
           "filter_types": {"select": "a dropdown of the values of one column of the dashboard's dataset; it applies "
                                      "to every chart of that dataset"}}
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_AGG = re.compile(r"^(SUM|COUNT|AVG|MIN|MAX)\((\*|DISTINCT [A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*)\)$", re.I)


class DashboardError(ValueError):
    pass


@dataclass
class Inputs:
    project: str
    request: str
    schema: dict          # {"tables": [{"schema", "table", "columns": [{"name", "type", "meaning"}]}]}
    kpis: list            # [{"name", "label", "meaning", "expression"}]
    required: list = field(default_factory=list)   # [{"groupby": [...], "metrics": [...], "filter": col|None}]

    def hashes(self) -> dict:
        def h(obj) -> str:
            return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
        return {"request": h(self.request), "schema": h(self.schema), "kpis": h(self.kpis),
                "required": h(self.required), "system_prompt": h(llm.load_prompt("synth_dashboard"))}


def load_inputs(project: str, request: Path, schema: Path, kpis: Path, required: Path | None = None) -> Inputs:
    if not _PROJECT.match(project):
        raise DashboardError(f"project name {project!r}: lowercase letters, digits and '-'")
    sch = yaml.safe_load(schema.read_text())
    kp = yaml.safe_load(kpis.read_text())
    if not isinstance(sch, dict) or not isinstance(sch.get("tables"), list):
        raise DashboardError(f"{schema}: needs `tables:`")
    for t in sch["tables"]:
        if not (_IDENT.match(str(t.get("schema", ""))) and _IDENT.match(str(t.get("table", "")))):
            raise DashboardError(f"{schema}: every table needs a plain `schema` and `table`")
    kp = kp.get("kpis") if isinstance(kp, dict) else kp
    if not isinstance(kp, list):
        raise DashboardError(f"{kpis}: needs `kpis:`")
    for k in kp:
        if not _NAME.match(str(k.get("name", ""))) or not _AGG.match(str(k.get("expression", "")).strip()):
            raise DashboardError(f"{kpis}: KPI {k.get('name')!r} needs a name and one aggregate expression "
                                 f"(SUM/COUNT/AVG/MIN/MAX of one column)")
    req = []
    if required is not None:
        doc = yaml.safe_load(required.read_text()) or {}
        for c in doc.get("checks", []):
            req.append({"groupby": list(c["groupby"]), "metrics": list(c["metrics"]),
                        "filters": [f["column"] for f in c.get("filters", [])]})
    return Inputs(project, request.read_text(), sch, kp, req)


def user_message(inp: Inputs, previous: dict | None = None, problems: list[str] | None = None) -> str:
    parts = ["# Report request\n", inp.request.strip(),
             "\n\n# Verified gold schema (the only tables and columns that exist)\n",
             yaml.safe_dump(inp.schema, sort_keys=False, allow_unicode=True),
             "\n# KPI definitions (the only metrics a dashboard may show; use their `name`)\n",
             yaml.safe_dump({"kpis": [{k: v for k, v in x.items() if k != "expression"} for x in inp.kpis]},
                            sort_keys=False, allow_unicode=True),
             "\n# What DPAgent can render\n", yaml.safe_dump(CATALOG, sort_keys=False)]
    if previous is not None:
        parts += ["\n# Your previous spec\n", json.dumps(previous, indent=1, ensure_ascii=False),
                  "\n\n# It was refused for these reasons - return a corrected spec\n",
                  "\n".join(f"- {p}" for p in problems or [])]
    return "".join(parts)


def _table(inp: Inputs, schema: str, table: str) -> dict | None:
    return next((t for t in inp.schema["tables"] if t["schema"] == schema and t["table"] == table), None)


def check_spec(inp: Inputs, spec: dict) -> list[str]:
    """Every problem with a model's spec, in words the model can act on. [] means it can be rendered."""
    p: list[str] = []
    if not isinstance(spec, dict):
        return ["the reply is not a JSON object"]
    allowed = {"title", "dataset", "charts", "filters", "mapping"}
    p += [f"unknown key {k!r} (allowed: {sorted(allowed)})" for k in spec if k not in allowed]
    ds = spec.get("dataset") or {}
    table = _table(inp, str(ds.get("schema")), str(ds.get("table")))
    if table is None:
        p.append(f"dataset {ds.get('schema')}.{ds.get('table')} is not a table of the verified gold schema")
        return p
    cols = {c["name"] for c in table["columns"]}
    kpis = {k["name"]: k for k in inp.kpis}
    if not str(spec.get("title", "")).strip():
        p.append("`title` is empty")
    charts, filters = spec.get("charts") or [], spec.get("filters") or []
    if not charts:
        p.append("no chart")
    seen: set = set()
    for c in charts:
        n = str(c.get("name", ""))
        if not _NAME.match(n) or n in seen:
            p.append(f"chart name {n!r} must be unique, lowercase letters/digits/_")
        seen.add(n)
        if c.get("type") not in CATALOG["chart_types"]:
            p.append(f"chart {n!r}: type {c.get('type')!r} is not supported (supported: {sorted(CATALOG['chart_types'])})")
        p += [f"chart {n!r}: groupby column {g!r} is not a column of {ds['table']}" for g in c.get("groupby") or []
              if g not in cols]
        if not c.get("metrics"):
            p.append(f"chart {n!r} shows no metric")
        p += [f"chart {n!r}: metric {m!r} is not a defined KPI (defined: {sorted(kpis)})" for m in c.get("metrics") or []
              if m not in kpis]
        p += [f"chart {n!r}: KPI {m!r} reads column(s) not in {ds['table']}" for m in c.get("metrics") or []
              if m in kpis and not _kpi_columns(kpis[m]) <= cols]
        if not str(c.get("title", "")).strip():
            p.append(f"chart {n!r}: `title` is empty")
    fseen: set = set()
    for f in filters:
        n = str(f.get("name", ""))
        if not _NAME.match(n) or n in fseen:
            p.append(f"filter name {n!r} must be unique, lowercase letters/digits/_")
        fseen.add(n)
        if f.get("type") not in CATALOG["filter_types"]:
            p.append(f"filter {n!r}: type {f.get('type')!r} is not supported (supported: {sorted(CATALOG['filter_types'])})")
        if f.get("column") not in cols:
            p.append(f"filter {n!r}: column {f.get('column')!r} is not a column of {ds['table']}")
    for r in inp.required:
        if not any(set(c.get("groupby") or []) == set(r["groupby"]) and set(r["metrics"]) <= set(c.get("metrics") or [])
                   for c in charts):
            p.append(f"the request needs a view by {r['groupby']} showing {r['metrics']}; no chart has it")
        p += [f"the request needs a filter on {col!r}; there is none" for col in r["filters"]
              if not any(f.get("column") == col for f in filters)]
    return p


def _kpi_columns(kpi: dict) -> set:
    arg = _AGG.match(kpi["expression"].strip()).group(2)
    return set() if arg == "*" else {arg.split()[-1]}


def _uid(project: str, kind: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dpagent-bi:{project}:{kind}:{name}"))


def render(inp: Inputs, spec: dict) -> dict[str, dict]:
    """The native bundle (path -> YAML document) for a checked spec. Deterministic: same inputs, same bytes."""
    ds = spec["dataset"]
    table = _table(inp, ds["schema"], ds["table"])
    kpis = {k["name"]: k for k in inp.kpis}
    db_id, ds_id, dash_id = (_uid(inp.project, "database", "warehouse"), _uid(inp.project, "dataset", ds["table"]),
                             _uid(inp.project, "dashboard", "overview"))
    used = sorted({m for c in spec["charts"] for m in c["metrics"]})
    files: dict[str, dict] = {
        "metadata.yaml": {"version": "1.0.0", "type": "assets", "timestamp": "2026-01-01T00:00:00+00:00"},
        "databases/warehouse.yaml": {"database_name": f"Warehouse ({inp.project})", "sqlalchemy_uri": "postgresql://",
                                     "uuid": db_id, "version": "1.0.0", "expose_in_sqllab": True, "allow_dml": False,
                                     "allow_ctas": False, "allow_cvas": False, "allow_file_upload": False, "extra": {}},
        f"datasets/warehouse/{ds['table']}.yaml": {
            "table_name": ds["table"], "schema": ds["schema"], "uuid": ds_id, "database_uuid": db_id,
            "version": "1.0.0", "filter_select_enabled": True,
            "columns": [{"column_name": c["name"], "type": str(c["type"]).upper(), "groupby": True,
                         "filterable": True, "is_dttm": False} for c in table["columns"]],
            "metrics": [{"metric_name": m, "verbose_name": kpis[m].get("label") or m,
                         "expression": kpis[m]["expression"].strip()} for m in used]},
    }
    position = {"DASHBOARD_VERSION_KEY": "v2", "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
                "GRID_ID": {"id": "GRID_ID", "type": "GRID", "children": [], "parents": ["ROOT_ID"]}}
    for i, c in enumerate(spec["charts"], start=1):
        chart_id = _uid(inp.project, "chart", c["name"])
        params = {"datasource": "0__table", "slice_id": 100 + i, "viz_type": "table", "query_mode": "aggregate",
                  "groupby": list(c["groupby"]), "metrics": list(c["metrics"]), "adhoc_filters": [],
                  "row_limit": 1000, "order_desc": False, "show_cell_bars": False}
        query = {"datasource": {"id": 0, "type": "table"}, "force": True,
                 "queries": [{"columns": list(c["groupby"]), "metrics": list(c["metrics"]), "filters": [],
                              "time_range": "No filter", "orderby": [], "row_limit": 1000,
                              "extras": {"having": "", "where": ""}}],
                 "form_data": params, "result_format": "json", "result_type": "full"}
        files[f"charts/{c['name']}.yaml"] = {"slice_name": c["title"], "viz_type": "table", "params": params,
                                             "query_context": json.dumps(query), "uuid": chart_id,
                                             "dataset_uuid": ds_id, "version": "1.0.0"}
        row = f"ROW-{i}"
        position["GRID_ID"]["children"].append(row)
        position[row] = {"id": row, "type": "ROW", "children": [f"CHART-{i}"], "parents": ["ROOT_ID", "GRID_ID"],
                         "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        position[f"CHART-{i}"] = {"id": f"CHART-{i}", "type": "CHART", "children": [],
                                  "parents": ["ROOT_ID", "GRID_ID", row],
                                  "meta": {"chartId": 100 + i, "uuid": chart_id, "width": 12, "height": 50,
                                           "sliceName": c["title"]}}
    native = [{"id": f"NATIVE_FILTER-{f['name']}", "name": f.get("label") or f["name"], "filterType": "filter_select",
               "targets": [{"datasetUuid": ds_id, "column": {"name": f["column"]}}],
               "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}}, "cascadeParentIds": [],
               "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
               "controlValues": {"enableEmptyFilter": False, "multiSelect": True, "searchAllOptions": False,
                                 "inverseSelection": False}, "type": "NATIVE_FILTER"} for f in spec.get("filters") or []]
    files["dashboards/overview.yaml"] = {
        "dashboard_title": spec["title"], "slug": inp.project, "uuid": dash_id, "version": "1.0.0", "published": True,
        "position": position,
        "metadata": {"native_filter_configuration": native, "color_scheme": "supersetColors", "refresh_frequency": 0,
                     "timed_refresh_immune_slices": [], "expanded_slices": {}, "cross_filters_enabled": False}}
    return files


def render_expected(inp: Inputs, spec: dict, expected: Path) -> dict:
    """The person's expected file (written by view: groupby + metrics, independent SQL, literal rows) bound to the
    rendered chart/dashboard identities, in the superset-assets format. The rows are never derived from a chart."""
    doc = yaml.safe_load(expected.read_text())
    out = []
    for c in doc["checks"]:
        chart = next(x for x in spec["charts"] if set(x["groupby"]) == set(c["groupby"])
                     and set(c["metrics"]) <= set(x["metrics"]))
        check = {"chart_uuid": _uid(inp.project, "chart", chart["name"]), "sql": c["sql"], "rows": c["rows"]}
        fl = []
        for f in c.get("filters", []):
            nf = next(x for x in spec.get("filters") or [] if x["column"] == f["column"])
            fl.append({"dashboard_uuid": _uid(inp.project, "dashboard", "overview"),
                       "native_filter_id": f"NATIVE_FILTER-{nf['name']}", "values": f["values"], "sql": f["sql"],
                       "rows": f["rows"]})
        if fl:
            check["filters"] = fl
        out.append(check)
    return {"checks": out}


@dataclass
class Attempt:
    index: int
    source: str                      # llm | llm-revision
    outcome: str = ""                # rendered | refused | blocked | llm-error | provider-unavailable
    problems: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    spec: dict | None = None


@dataclass
class Result:
    attempts: list
    spec: dict | None
    classification: str
    records: list


def classify(attempts: list[Attempt]) -> str:
    last = attempts[-1] if attempts else None
    if last is None:
        return "not-drafted"
    if last.outcome == "provider-unavailable":
        return "inconclusive-provider-unavailable"
    if last.outcome == "blocked":
        return "asked-blocker"
    if last.outcome == "rendered":
        n = sum(1 for a in attempts if a.source == "llm-revision")
        return "llm-first-draft" if n == 0 else f"llm-after-{n}-revision(s)"
    return "not-rendered"


def draft(inp: Inputs, *, max_calls: int, max_revisions: int, transient_retries: int = 0,
          retry_wait: float = 60.0) -> Result:
    """Ask the model, check, re-ask with the problems (never with expected rows) at most max_revisions times."""
    system = llm.load_prompt("synth_dashboard")
    attempts: list[Attempt] = []
    previous, problems = None, None
    with llm.record_calls(max_calls=max_calls, transient_retries=transient_retries, wait_s=retry_wait) as records:
        for i in range(max_revisions + 1):
            a = Attempt(i + 1, "llm" if i == 0 else "llm-revision")
            attempts.append(a)
            try:
                reply = llm.chat_json(system, user_message(inp, previous, problems), max_tokens=8000)
            except llm.ProviderUnavailable as exc:
                a.outcome, a.problems = "provider-unavailable", [str(exc)[:500]]
                break
            except llm.LLMError as exc:
                a.outcome, a.problems = "llm-error", [str(exc)[:500]]
                break
            if isinstance(reply, dict) and reply.get("blockers"):
                a.outcome = "blocked"
                a.blockers = [{"question": str(b.get("question", "")), "why_it_matters": str(b.get("why_it_matters", ""))}
                              for b in reply["blockers"] if isinstance(b, dict)]
                break
            a.spec = reply if isinstance(reply, dict) else None
            a.problems = check_spec(inp, reply)
            if not a.problems:
                a.outcome = "rendered"
                break
            a.outcome = "refused"
            previous, problems = reply, a.problems
    spec = attempts[-1].spec if attempts and attempts[-1].outcome == "rendered" else None
    return Result(attempts, spec, classify(attempts), list(records))


def write_project(inp: Inputs, spec: dict, out_dir: Path, *, expected: Path | None, origin: str,
                  overwrite: bool = False) -> Path:
    """projects/<project>/: bundle/, expected.yaml (when the person's expected file is given), ORIGIN.md."""
    root = out_dir / inp.project
    if root.exists() and not overwrite:
        raise DashboardError(f"{root} exists - --overwrite to replace it")
    files = render(inp, spec)
    for rel, doc in files.items():
        path = root / "bundle" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    if expected is not None:
        (root / "expected.yaml").write_text(
            "# The reviewer's independent expectations (literal rows, independent SQL), bound to the rendered\n"
            "# chart/filter identities by dpagent dashboard synth. Rows were written by a person, not computed.\n"
            + yaml.safe_dump(render_expected(inp, spec, expected), sort_keys=False, allow_unicode=True))
    (root / "spec.json").write_text(json.dumps(spec, indent=1, ensure_ascii=False) + "\n")
    (root / "ORIGIN.md").write_text(origin.rstrip() + "\n")
    return root


def write_trace(inp: Inputs, result: Result, out_dir: Path, *, max_calls: int, max_revisions: int) -> Path:
    """trace.json + calls/NN-user.txt, calls/NN-response.txt. Refuses to write a credential of this environment."""
    from dpagent.pipelines import llm_verify
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = {out_dir / "system-prompt.md": llm.load_prompt("synth_dashboard")}
    for rec in result.records:
        texts[out_dir / "calls" / f"{rec.index:02d}-user.txt"] = rec.user
        texts[out_dir / "calls" / f"{rec.index:02d}-response.txt"] = rec.response
    trace = {"kind": "dashboard", "project": inp.project, "model": llm.get_model(),
             "classification": result.classification, "inputs": inp.hashes(),
             "budget": {"max_calls": max_calls, "max_revisions": max_revisions},
             "calls": [llm_verify._call_summary(r) for r in result.records],
             "attempts": [{"index": a.index, "source": a.source, "outcome": a.outcome, "problems": a.problems,
                           "blockers": a.blockers, "spec": a.spec} for a in result.attempts]}
    body = json.dumps(trace, indent=2, ensure_ascii=False, sort_keys=True)
    for secret in llm_verify._secret_values():
        if secret in body or any(secret in t for t in texts.values()):
            raise DashboardError("a credential from the environment appears in the trace - not written")
    for path, text in texts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (out_dir / "trace.json").write_text(body, encoding="utf-8")
    return out_dir / "trace.json"
