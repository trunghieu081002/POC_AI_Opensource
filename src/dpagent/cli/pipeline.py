"""`dpagent pipeline ...` - Layer 2's staged-ingestion manifests.

Mirrors install/verify/test's own verbs (docs/layer2.md, "CLI") for the same
reason those exist: `lint` catches a mistake in the manifest itself, cheaply,
before anything is generated or run against a real warehouse.
"""
from __future__ import annotations

import click
from rich.panel import Panel
from rich.table import Table

from ..pipelines import deploy as deploy_mod
from ..pipelines import generator as generator_mod
from ..pipelines import loader as pipelines_mod
from .render import confirm, console, fail


def _load_or_fail(name):
    try:
        return pipelines_mod.load(name)
    except pipelines_mod.PipelineError as exc:
        fail(str(exc))


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
        table.add_row(
            stage.name,
            stage.engine or "[dim]dlt[/dim]",
            stage.depends_on or "[dim]-[/dim]",
            ", ".join(g.type for g in stage.gates) or "[dim]none[/dim]",
            stage.quarantine.table if stage.quarantine else "[dim]-[/dim]",
        )
    console.print(Panel(table, title=f"{pipeline.name} · {pipeline.source.connector}",
                        border_style="cyan", expand=False))
    console.print("[green]lint clean[/green]")


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
              help="Write the DAG/schema files only - skip applying procedure migrations.")
def deploy_cmd(name, yes, no_db):
    """Write the DAG + dbt schema, and apply procedure migrations.

    Writing files is always safe to repeat (each run overwrites the last).
    Applying a procedure runs `CREATE OR REPLACE PROCEDURE` against
    `warehouse:` in the manifest - idempotent by construction, but it is a
    real command against a real database, so it asks first unless --yes.
    """
    pipeline = _load_or_fail(name)

    if not no_db and not yes and not confirm(
            f"Apply {name}'s procedure migration(s) against "
            f"{pipeline.warehouse.host}:{pipeline.warehouse.port}/"
            f"{pipeline.warehouse.database}?", default=True):
        no_db = True
        console.print("[yellow]skipping procedure migrations - files only[/yellow]")

    try:
        result = deploy_mod.deploy(pipeline, apply_db=not no_db)
    except deploy_mod.DeployError as exc:
        fail(str(exc))

    console.print("[bold]written:[/bold]")
    for path in result.written:
        console.print(f"  {path}")
    if result.procedures_applied:
        console.print("[bold]procedures applied:[/bold] "
                     + ", ".join(result.procedures_applied))
    console.print("[green]deployed[/green]")
