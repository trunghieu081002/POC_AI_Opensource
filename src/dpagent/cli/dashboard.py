"""`dpagent dashboard synth`: a model drafts a dashboard spec, DPAgent checks and renders the native bundle
(src/dpagent/dashboards/synth.py, docs/ai-dashboards.md). It never validates against Superset, approves or deploys:
those are the superset-assets pack's own modes, after a person reviews."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from dpagent.dashboards import synth as dash
from dpagent.engine import state
from dpagent.llm import client as llm


@click.group("dashboard")
def dashboard_group():
    """AI-drafted dashboards (rendered for the superset-assets pack)."""


@dashboard_group.command("synth")
@click.argument("project")
@click.option("--request", "request_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True,
              help="The report request a person wrote.")
@click.option("--schema", "schema_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True,
              help="The verified gold schema (tables, columns, types, meanings).")
@click.option("--kpis", "kpis_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True,
              help="The KPI definitions: the only metrics a dashboard may show.")
@click.option("--expected", "expected_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="The reviewer's independent expected file (views, SQL, literal rows). Its views are required; "
                   "its rows are never shown to the model.")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path), default=Path("projects"), show_default=True)
@click.option("--max-calls", type=int, default=4, show_default=True,
              help="Ceiling on requests sent - every request counts: answered, failed, busy, timed out, repairs, re-asks.")
@click.option("--max-revisions", type=int, default=2, show_default=True)
@click.option("--transient-retries", type=int, default=0, show_default=True)
@click.option("--retry-wait", type=float, default=60.0, show_default=True)
@click.option("--trace-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--overwrite", is_flag=True)
def synth_cmd(project, request_path, schema_path, kpis_path, expected_path, out_dir, max_calls, max_revisions,
              transient_retries, retry_wait, trace_dir, overwrite):
    """Draft PROJECT's dashboard with a model, check it, render projects/PROJECT/bundle. Nothing is deployed."""
    try:
        inp = dash.load_inputs(project, request_path, schema_path, kpis_path, expected_path)
    except (dash.DashboardError, OSError, KeyError, TypeError) as exc:
        raise click.ClickException(str(exc))
    if (out_dir / project).exists() and not overwrite:
        raise click.ClickException(f"{out_dir / project} exists - --overwrite to replace it")
    if max_calls < 1:
        raise click.ClickException("--max-calls must be at least 1")
    result = dash.draft(inp, max_calls=max_calls, max_revisions=max_revisions,
                        transient_retries=transient_retries, retry_wait=retry_wait)
    out = trace_dir or (state.DB_PATH.parent / "dashboard-traces" / project /
                        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    trace_path = dash.write_trace(inp, result, Path(out), max_calls=max_calls, max_revisions=max_revisions)
    for a in result.attempts:
        click.echo(f"attempt {a.index} ({a.source}): {a.outcome}")
        for p in a.problems:
            click.echo(f"    - {p}")
    recs = result.records
    busy = sum(1 for r in recs if r.transient)
    click.echo(f"requests sent: {len(recs)} (ceiling {max_calls}; provider busy/timeout: {busy}; token usage reported "
               f"for {sum(1 for r in recs if r.usage_status == 'reported')}, unknown for the rest; cost unknown)")
    click.echo(f"classification: {result.classification}   trace: {trace_path}")
    blocked = result.attempts[-1].blockers if result.attempts else []
    if blocked:
        click.echo("cannot draft yet - the model needs answers:")
        for b in blocked:
            click.echo(f"  ? {b['question']}  ({b['why_it_matters']})")
        raise SystemExit(1)
    if result.spec is None:
        click.echo("not rendered - nothing written")
        raise SystemExit(1)
    origin = (f"# ORIGIN: drafted by a model\n\nmodel {llm.get_model()} via `dpagent dashboard synth` "
              f"({result.classification}); trace {trace_path}.\nDPAgent rendered the bundle from the model's spec "
              f"(spec.json): metric expressions from the KPI definitions, types from the verified schema, uuid5 "
              f"identities. Not validated against data and not approved yet: run the superset-assets pack's "
              f"`mode: validate`, then a person approves the printed fingerprint.\n")
    root = dash.write_project(inp, result.spec, out_dir, expected=expected_path, origin=origin, overwrite=overwrite)
    click.echo(f"rendered {root}/bundle (+ expected.yaml: {'yes' if expected_path else 'no'}) - next: validate it with "
               f"the superset-assets pack (mode: validate); a person approves the fingerprint")
