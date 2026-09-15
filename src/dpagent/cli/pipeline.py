"""`dpagent pipeline ...` - Layer 2's staged-ingestion manifests.

Mirrors install/verify/test's own verbs (docs/layer2.md, "CLI") for the same
reason those exist: `lint` catches a mistake in the manifest itself, cheaply,
before anything is generated or run against a real warehouse.
"""
from __future__ import annotations

import click
from rich.panel import Panel
from rich.table import Table

from ..pipelines import loader as pipelines_mod
from .render import console, fail


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
    try:
        pipeline = pipelines_mod.load(name)
    except pipelines_mod.PipelineError as exc:
        fail(str(exc))

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
