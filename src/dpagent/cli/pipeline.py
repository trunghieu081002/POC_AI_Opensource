"""`dpagent pipeline ...` - Layer 2's staged-ingestion manifests.

Mirrors install/verify/test's own verbs (docs/layer2.md, "CLI") for the same
reason those exist: `lint` catches a mistake in the manifest itself, cheaply,
before anything is generated or run against a real warehouse.
"""
from __future__ import annotations

import sys
import time

import click
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..engine import state
from ..pipelines import deploy as deploy_mod
from ..pipelines import extract as extract_mod
from ..pipelines import generator as generator_mod
from ..pipelines import loader as pipelines_mod
from ..pipelines.loader import quarantine_table_for
from .render import confirm, console, fail


def _load_or_fail(name):
    try:
        return pipelines_mod.load(name)
    except pipelines_mod.PipelineError as exc:
        fail(str(exc))


def _pack_installed(name: str) -> bool:
    record = state.get_install(name)
    return bool(record and record["status"] == "installed")


def _missing_layer2_prerequisites(pipeline) -> list[str]:
    """What must already be installed for `deploy`/`run` to actually work -
    checked here so a missing pack fails with one clear, consolidated
    message up front, not as a raw PackError/DeployError deep inside
    whichever step happens to need it first (install_dbt_models() reaching
    for a dbt pack that was never installed, e.g.)."""
    missing = []
    if not _pack_installed("dlt"):
        missing.append("dlt (sudo -E dpagent install dlt) - every pipeline's "
                       "landing stage needs it to extract")
    if any(s.engine == "dbt" for s in pipeline.stages) and not _pack_installed("dbt"):
        missing.append("dbt (sudo -E dpagent install dbt) - this pipeline has "
                       "a dbt-engine stage")
    if not _pack_installed("airflow"):
        missing.append("airflow (sudo -E dpagent install airflow) - deploy "
                       "installs the DAG there; run triggers it")
    return missing


def _require_layer2_prerequisites(pipeline) -> None:
    missing = _missing_layer2_prerequisites(pipeline)
    if missing:
        fail("this pipeline cannot be deployed/run yet - missing:\n  "
             + "\n  ".join(missing))


@click.group("pipeline")
def pipeline_group():
    """Staged-ingestion pipelines (Layer 2) - see docs/layer2.md."""


@pipeline_group.command("lint")
@click.argument("name")
def lint_cmd(name):
    """Validate a pipeline manifest: stages, engines, gates, quarantine.

    Every rule this enforces traces back to docs/layer2.md's "Concepts"
    section - a manifest that passes lint is one the generator can turn into
    an Airflow DAG and dbt schema/test YAML without guessing.
    """
    pipeline = _load_or_fail(name)

    table = Table(box=None)
    table.add_column("stage", style="bold")
    table.add_column("engine")
    table.add_column("depends_on", style="dim")
    table.add_column("gates")
    table.add_column("quarantine", style="dim")
    for stage in pipeline.stages:
        quarantine_tables = sorted({
            quarantine_table_for(g["table"]) for g in stage.gates
            if stage.quarantine and "table" in g.params
        })
        table.add_row(
            stage.name,
            stage.engine or "[dim]dlt[/dim]",
            stage.depends_on or "[dim]-[/dim]",
            ", ".join(g.type for g in stage.gates) or "[dim]none[/dim]",
            ", ".join(quarantine_tables) if quarantine_tables else "[dim]-[/dim]",
        )
    console.print(Panel(table, title=f"{pipeline.name} · {pipeline.source.connector}",
                        border_style="cyan", expand=False))
    console.print("[green]lint clean[/green]")


@pipeline_group.command("list")
def list_cmd():
    """Every pipeline: its connector, whether it is deployed, and its last run.

    Covers the manifests in this checkout plus any pipeline still deployed
    whose manifest is no longer here (removable with `undeploy`).
    """
    deployed = set(deploy_mod.deployed_names())
    table = Table(box=None)
    for column in ("pipeline", "connector", "stages", "deployed", "last run"):
        table.add_column(column, style="bold" if column == "pipeline" else "")

    def last_run(name):
        run = state.latest_run(kind="data", target=name)
        if not run:
            return "[dim]never[/dim]"
        colour = {"ok": "green", "failed": "red"}.get(run["status"], "yellow")
        return f"[{colour}]{run['status']}[/{colour}] [dim]#{run['id']} {run['started_at']}[/dim]"

    in_checkout = pipelines_mod.available()
    for name in in_checkout:
        yes_no = "[green]yes[/green]" if name in deployed else "[dim]no[/dim]"
        try:
            p = pipelines_mod.load(name)
        except pipelines_mod.PipelineError as exc:
            table.add_row(name, "[red]invalid[/red]", str(exc).split(": ")[-1][:60],
                          yes_no, last_run(name))
            continue
        table.add_row(name, p.source.connector, " > ".join(s.name for s in p.stages),
                      yes_no, last_run(name))
    orphans = sorted(deployed - set(in_checkout))
    if not (in_checkout or orphans):
        console.print("[dim]no pipelines in this checkout and none deployed[/dim]")
        return
    if in_checkout:
        console.print(table)
    if orphans:
        console.print(f"[yellow]deployed but not in this checkout:[/yellow] {', '.join(orphans)} "
                      f"[dim](remove with: dpagent pipeline undeploy <name>)[/dim]")


@pipeline_group.command("plan")
@click.argument("name")
def plan_cmd(name):
    """Print every artifact and command `deploy`/`run` would produce. Changes nothing.

    Same promise as `dpagent install --dry-run`: every DAG task, every file
    that would be written, and the exact SQL each gate would run - printed,
    never executed, nothing written to disk or to a warehouse.
    """
    pipeline = _load_or_fail(name)
    result = generator_mod.plan(pipeline)

    tasks = Table(box=None, title="DAG tasks")
    tasks.add_column("id", style="bold")
    tasks.add_column("kind", style="dim")
    tasks.add_column("depends_on", style="dim")
    tasks.add_column("would run")
    for task in result.tasks:
        tasks.add_row(task.id, task.kind, ", ".join(task.depends_on) or "-", task.description)
    console.print(Panel(tasks, border_style="cyan", expand=False))

    artifacts = Table(box=None, title="artifacts deploy would write")
    artifacts.add_column("path", style="bold")
    artifacts.add_column("kind", style="dim")
    artifacts.add_column("what")
    for artifact in result.artifacts:
        artifacts.add_row(artifact.path, artifact.kind, artifact.description)
    console.print(Panel(artifacts, border_style="cyan", expand=False))

    for stage in pipeline.stages:
        compiled = result.gates[stage.name]
        if not compiled:
            continue
        console.print(f"\n[bold]gates · {stage.name}[/bold]")
        for cg in compiled:
            console.print(f"  [cyan]{cg.gate.type}[/cyan] — {cg.verdict}")
            for query in cg.queries:
                console.print(f"      [dim][{query.name}][/dim] {query.sql}")

    console.print(
        "\n[yellow]plan complete — nothing was written, nothing was executed[/yellow]")


@pipeline_group.command("deploy")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
@click.option("--no-db", is_flag=True,
              help="Write the DAG/schema files only - skip applying procedure "
                   "migrations and publishing dbt models.")
@click.option("--no-airflow", is_flag=True,
              help="Skip installing the DAG into Airflow's real DAGS_FOLDER.")
def deploy_cmd(name, yes, no_db, no_airflow):
    """Write the DAG + dbt schema, apply procedures, publish dbt models, install the DAG.

    Writing files under pipelines/<name>/build/ is always safe to repeat
    (each run overwrites the last). The rest happens against a real
    system, so it asks first unless --yes: applying a procedure runs
    `CREATE OR REPLACE PROCEDURE` against `warehouse:` (idempotent by
    construction) and a dbt-engine stage's models are copied into the dbt
    pack's own real project (needs root; `dbt run` only ever looks inside
    its own project, never at this pipeline's directory) - both under
    --no-db; and installing the DAG copies it into Airflow's DAGS_FOLDER
    as the airflow OS user (needs root) so `dpagent pipeline run` has
    something real to trigger - under --no-airflow.
    """
    pipeline = _load_or_fail(name)
    _require_layer2_prerequisites(pipeline)

    if not no_db and not yes and not confirm(
            f"Apply {name}'s procedure migration(s) against "
            f"{pipeline.warehouse.host}:{pipeline.warehouse.port}/"
            f"{pipeline.warehouse.database}, and publish its dbt models (needs root)?",
            default=True):
        no_db = True
        console.print("[yellow]skipping procedure migrations/dbt models - files only[/yellow]")

    if not no_airflow and not yes and not confirm(
            f"Publish {name}'s files to {deploy_mod.SHARED_PIPELINES_DIR}, make sure "
            f"Airflow can actually run pipelines (a shared group, an ACL grant, an "
            f"editable dpagent install into its venv - only what is not already true), "
            f"sync this pipeline's secrets into Airflow's own environment (restarting "
            f"airflow-scheduler if that changed anything), and install its DAG into "
            f"Airflow's real DAGS_FOLDER (needs root)?",
            default=True):
        no_airflow = True
        console.print("[yellow]skipping Airflow install - files only[/yellow]")

    try:
        result = deploy_mod.deploy(pipeline, apply_db=not no_db,
                                   install_dag_to_airflow=not no_airflow)
    except deploy_mod.DeployError as exc:
        fail(str(exc))

    console.print("[bold]written:[/bold]")
    for path in result.written:
        console.print(f"  {path}")
    if result.schema_ensured:
        console.print(f"[bold]schema ensured:[/bold] {result.schema_ensured}")
    if result.procedures_applied:
        console.print("[bold]procedures applied:[/bold] "
                     + ", ".join(result.procedures_applied))
    if result.dbt_models_published:
        console.print("[bold]dbt models published:[/bold]")
        for path in result.dbt_models_published:
            console.print(f"  {path}")
    if result.pipeline_files_published:
        console.print(f"[bold]pipeline files published:[/bold] "
                     f"{result.pipeline_files_published}")
    if result.airflow_bridge_actions:
        console.print("[bold]airflow bridge:[/bold]")
        for action in result.airflow_bridge_actions:
            console.print(f"  {action}")
    if result.pipeline_secrets_synced:
        console.print("[bold]pipeline secrets:[/bold] synced to Airflow's "
                     "own environment, airflow-scheduler restarted")
    if result.dag_installed:
        console.print(f"[bold]DAG installed:[/bold] {result.dag_installed}")
        if result.dag_unpaused:
            console.print("[bold]DAG unpaused:[/bold] a run can start (manual-only DAG)")
        else:
            console.print(
                f"[yellow]could not unpause the DAG[/yellow] - until it is unpaused a "
                f"`pipeline run` is created but never starts. Do it by hand: "
                f"airflow dags unpause {name}\n  {result.dag_unpause_error}")
    console.print("[green]deployed[/green]")


def _wait_for_run(run_id: int, *, timeout: float, interval: float = 3.0,
                  sleep=time.sleep, clock=time.monotonic) -> str:
    """Polls dpagent's own journal until the run leaves "running" (the DAG's
    own success/failure callback is what moves it - runtime.finish_pipeline_run)
    or `timeout` seconds pass. Returns the final status, or "running" if it
    timed out. Reads the journal, not Airflow: same source `status` uses."""
    deadline = clock() + timeout
    while True:
        row = state.get_run(run_id)
        if row is not None and row["status"] != "running":
            return row["status"]
        if clock() >= deadline:
            return "running"
        sleep(interval)


def _load_for_undeploy(name):
    """The repo's manifest if it still exists, else the published copy under
    the shared directory - a pipeline whose source was already deleted from
    git must still be removable from Airflow."""
    try:
        return pipelines_mod.load(name)
    except pipelines_mod.PipelineError as repo_exc:
        try:
            return pipelines_mod.load(name, deploy_mod.SHARED_PIPELINES_DIR)
        except pipelines_mod.PipelineError:
            fail(str(repo_exc))


@pipeline_group.command("undeploy")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
def undeploy_cmd(name, yes):
    """Remove a deployed pipeline from Airflow and the shared locations.

    Removes the DAG (file, then its Airflow registration and run history),
    the published copy under /opt/dpagent/pipelines, its published dbt models
    and dlt's local working state, and releases the ${VAR} secrets no other
    deployed pipeline still uses (restarting airflow-scheduler if that changed
    anything). It never touches the warehouse - schemas, tables, quarantine
    tables and applied procedures are data and stay - nor dpagent's own run
    journal, so `status`/`audit` history survives. Needs root.
    """
    pipeline = _load_for_undeploy(name)
    landing = extract_mod.landing_dataset(pipeline)

    if not yes and not confirm(
            f"Remove {name!r} from Airflow (DAG + run history), "
            f"{deploy_mod.SHARED_PIPELINES_DIR / name}, its dbt models and dlt state, "
            f"and release its now-unused secrets? Warehouse data is NOT touched "
            f"(schemas {pipeline.warehouse.schema!r} and {landing!r} stay).",
            default=False):
        console.print("[yellow]nothing removed[/yellow]")
        sys.exit(1)

    try:
        result = deploy_mod.undeploy(pipeline)
    except deploy_mod.DeployError as exc:
        fail(str(exc))

    def mark(done, what):
        console.print(f"  {'[green]removed[/green]' if done else '[dim]absent [/dim]'}  {what}")

    console.print(f"[bold]undeployed {name}[/bold]")
    mark(result.dag_file_removed, "DAG file in Airflow's DAGS_FOLDER")
    if result.dag_delete_failed:
        console.print(f"  [red]failed [/red]  DAG registration and run history in Airflow: "
                      f"{result.dag_delete_note}")
        console.print(f"          [dim]retry once Airflow is reachable: dpagent pipeline "
                      f"undeploy {name}[/dim]")
    else:
        mark(result.dag_deleted_from_airflow, "DAG registration and run history in Airflow")
    mark(result.published_files_removed, f"published copy {deploy_mod.SHARED_PIPELINES_DIR / name}")
    mark(result.dbt_models_removed, "published dbt models")
    mark(result.dlt_state_removed, "dlt local working state")
    if result.secrets_removed:
        console.print(f"  [green]released[/green] secrets: {', '.join(result.secrets_removed)}"
                      + ("  (airflow-scheduler restarted)" if result.scheduler_restarted else ""))
    for var, users in result.secrets_kept.items():
        console.print(f"  [dim]kept[/dim]     {var} - still used by: {', '.join(users)}")
    if result.secrets_note:
        console.print(f"  [yellow]{result.secrets_note}[/yellow]")
    console.print(f"[dim]left in place: warehouse schemas {pipeline.warehouse.schema!r} and "
                  f"{landing!r} with all their tables, and dpagent's run history[/dim]")


@pipeline_group.command("run")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
@click.option("--wait", is_flag=True,
              help="Block until the run reaches a terminal state; exit 0 if ok, "
                   "1 if failed, 3 if --timeout passes first.")
@click.option("--timeout", type=int, default=1800, show_default=True,
              help="Seconds --wait gives up after (the run itself keeps going).")
def run_cmd(name, yes, wait, timeout):
    """Trigger this pipeline's deployed Airflow DAG - through Airflow, not around it.

    Records a `runs` row (kind='data') first and passes its id into the DAG
    run's `conf`, so every stage/gate the DAG's tasks execute ties back to
    it - `dpagent pipeline status`/`audit` read exactly that row, nothing
    dpagent executes itself. Triggering runs the CLI as the `airflow` OS
    user (same as `packs/airflow`'s own af_run), a real command against this
    host's real Airflow, so it asks first unless --yes. dpagent does not
    wait for the DAG to finish unless --wait is given - Airflow runs it
    asynchronously.
    """
    pipeline = _load_or_fail(name)   # fail before touching state if the manifest itself is broken
    _require_layer2_prerequisites(pipeline)

    if not yes and not confirm(
            f"Trigger the deployed {name!r} DAG now (runs as the airflow OS user)?",
            default=True):
        sys.exit(1)

    run_id = state.start_run("data", name)
    state.event("pipeline.trigger", f"triggering Airflow DAG {name!r}",
               run_id=run_id, actor="user")

    result = deploy_mod.trigger_dag(name, run_id)
    if result.returncode != 0:
        state.finish_run(run_id, "failed")
        state.event("pipeline.trigger_failed", result.stderr.strip(),
                   run_id=run_id, level="error")
        fail(f"could not trigger the Airflow DAG for {name!r} (run {run_id}):\n"
             f"{result.stderr.strip()}")

    console.print(f"[green]triggered[/green] — dpagent run {run_id}")
    if not wait:
        console.print(f"[dim]dpagent does not wait for Airflow to finish this run. "
                     f"Check progress with: dpagent pipeline status {name} "
                     f"(or re-run with --wait)[/dim]")
        return

    console.print(f"[dim]waiting up to {timeout}s for run {run_id} to finish...[/dim]")
    final = _wait_for_run(run_id, timeout=timeout)
    _render_status(name, state.get_run(run_id))
    if final == "ok":
        return
    if final == "running":
        console.print(f"[yellow]still running after {timeout}s[/yellow] - it was not "
                      f"stopped; check: dpagent pipeline status {name}")
        if not state.stages_for_run(run_id):
            console.print(
                "[yellow]no stage ever reported in[/yellow] - the DAG's tasks never "
                "started. Usual causes: the DAG is paused (airflow dags unpause "
                f"{name}) or airflow-scheduler is not running "
                "(systemctl status airflow-scheduler).")
        sys.exit(3)
    console.print(f"[red]run {run_id} {final}[/red] - why: dpagent pipeline audit {run_id}")
    sys.exit(1)


def _render_status(name, run) -> None:
    stage_rows = state.stages_for_run(run["id"])
    colour = {"ok": "green", "failed": "red"}.get(run["status"], "yellow")
    table = Table(box=None,
                 title=f"{name} · run {run['id']} [{colour}]{run['status']}[/{colour}] "
                       f"· started {run['started_at']}")
    table.add_column("stage", style="bold")
    table.add_column("status")
    table.add_column("rows", justify="right")
    table.add_column("gates")
    table.add_column("started", style="dim")
    for stage_row in stage_rows:
        gates = state.gates_for_stage(stage_row["id"])
        stage_colour = {"passed": "green", "failed": "red"}.get(stage_row["status"], "yellow")
        gate_summary = ", ".join(
            f"[green]{g['gate_type']}[/green]" if g["status"] == "passed"
            else f"[red]{g['gate_type']}[/red]"
            for g in gates) or "[dim]none[/dim]"
        table.add_row(
            stage_row["stage"],
            f"[{stage_colour}]{stage_row['status']}[/{stage_colour}]",
            str(stage_row["row_count"]) if stage_row["row_count"] is not None else "-",
            gate_summary,
            stage_row["started_at"],
        )
    console.print(table)
    if not stage_rows:
        if run["status"] == "running":
            console.print("[dim]triggered but no stage has reported in yet - "
                          "Airflow may still be scheduling it[/dim]")
        else:
            console.print(f"[yellow]run {run['status']} before any stage reported[/yellow] - "
                          f"typically the extract itself failed; why: "
                          f"dpagent pipeline audit {run['id']}")


@pipeline_group.command("status")
@click.argument("name")
def status_cmd(name):
    """Stages, last run, gate verdicts - from dpagent's own journal, not a live Airflow query."""
    _load_or_fail(name)
    run = state.latest_run(kind="data", target=name)
    if not run:
        console.print(f"[dim]no runs recorded for {name!r} yet[/dim]")
        console.print(f"[dim]run: dpagent pipeline run {name}[/dim]")
        return
    _render_status(name, run)


@pipeline_group.command("audit")
@click.argument("run_id", type=int, required=False)
def audit_cmd(run_id):
    """Every stage and gate decision for one pipeline run, and who made it."""
    if run_id is None:
        run = state.latest_run(kind="data")
        if not run:
            console.print("[dim]no pipeline runs recorded[/dim]")
            return
        run_id = run["id"]
    else:
        run = state.get_run(run_id)
        if not run or run["kind"] != "data":
            fail(f"run {run_id} is not a pipeline run (dpagent audit {run_id} "
                 f"shows any run kind)")

    console.print(f"[bold]{run['target']}[/bold] · run {run_id} · {run['status']}")

    # Gate detail/event message come from the manifest's own table/column
    # names and generated SQL, not a fixed vocabulary like gate_type - built
    # as Text rather than interpolated into an f-string, so a stray "["
    # in one can't be parsed as rich markup and silently eaten (the same
    # trap `report.py`'s own audit_cmd avoids the same way).
    for stage_row in state.stages_for_run(run_id):
        colour = {"passed": "green", "failed": "red"}.get(stage_row["status"], "yellow")
        header = Text(f"\n{stage_row['stage']} ")
        header.append(stage_row["status"], style=colour)
        if stage_row["row_count"] is not None:
            header.append(f" · {stage_row['row_count']} rows")
        console.print(header)

        for gate in state.gates_for_stage(stage_row["id"]):
            gate_colour = "green" if gate["status"] == "passed" else "red"
            line = Text("  ")
            line.append(gate["gate_type"], style=gate_colour)
            if gate["detail"]:
                line.append(f" — {gate['detail']}")
            console.print(line)

    events = state.events_for(run_id)
    if events:
        console.print("\n[bold]events[/bold]")
        table = Table(box=None)
        table.add_column("time", style="dim")
        table.add_column("actor", style="cyan")
        table.add_column("kind", style="magenta")
        table.add_column("message")
        for row in events:
            text = Text(row["message"])
            if row["level"] == "error":
                text.stylize("red")
            elif row["level"] == "warn":
                text.stylize("yellow")
            table.add_row(row["ts"].split("T")[-1], row["actor"], row["kind"], text)
        console.print(table)
