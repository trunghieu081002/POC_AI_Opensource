"""Read-only commands: info, packs, suites, status, audit, errors."""
from __future__ import annotations

import json

import click
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..engine import errors as errors_mod
from ..engine import executor, os_detect, state
from ..library import loader as packs
from ..llm import client as llm
from ..suites import loader as suites_loader
from .render import console


@click.command("info")
def info_cmd():
    """Show the target OS, the libraries, and the LLM config. Calls nothing."""
    try:
        os_info = os_detect.detect()
        table = Table(show_header=False, box=None)
        for key, value in os_info.as_dict().items():
            table.add_row(f"[dim]{key}[/dim]", str(value))
        console.print(Panel(table, title="target OS", border_style="cyan", expand=False))
    except RuntimeError as exc:
        console.print(Panel(f"[yellow]{exc}[/yellow]", title="target OS",
                            border_style="yellow", expand=False))

    catalog = packs.catalog()
    stable = sum(1 for p in catalog if not p.is_draft)
    suite_names = suites_loader.available()
    console.print(Panel(
        f"packs:  [bold]{len(catalog)}[/bold] ({stable} stable, {len(catalog) - stable} draft)\n"
        f"[dim]{packs.PACKS_DIR}[/dim]\n"
        f"suites: [bold]{len(suite_names)}[/bold]  {', '.join(suite_names) or '(none)'}\n"
        f"[dim]{suites_loader.SUITES_DIR}[/dim]",
        title="libraries", border_style="cyan", expand=False))

    shared = errors_mod.Catalog.shared()
    console.print(Panel(
        f"model: [bold]{llm.get_model()}[/bold]\n"
        f"credential: "
        f"{'[green]present[/green]' if llm.available() else '[yellow]not set — install, verify and test still work[/yellow]'}\n"
        f"shared error catalog: [bold]{len(shared)}[/bold] entries",
        title="LLM (routing, authoring, diagnosis only)",
        border_style="magenta", expand=False))

    try:
        console.print(Panel(json.dumps(state.stats(), indent=2),
                            title=f"journal · {state.DB_PATH}",
                            border_style="dim", expand=False))
    except Exception as exc:
        console.print(f"[yellow]journal unavailable: {exc}[/yellow]")


@click.command("packs")
@click.option("--verbose", "-v", is_flag=True, help="Show params and dependencies.")
def packs_cmd(verbose):
    """List the packs this agent can install."""
    catalog = packs.catalog()
    if not catalog:
        console.print(f"[yellow]no packs found in {packs.PACKS_DIR}[/yellow]")
        return

    covered = set()
    for name in suites_loader.available():
        try:
            covered.update(suites_loader.load(name).requires or [name])
        except suites_loader.SuiteError:
            continue

    table = Table(box=None)
    table.add_column("pack", style="bold")
    table.add_column("ver")
    table.add_column("maturity")
    table.add_column("suite")
    table.add_column("families", style="dim")
    table.add_column("summary")
    for pack in catalog:
        maturity = ("[green]stable[/green]" if not pack.is_draft
                    else "[yellow]draft[/yellow]")
        suite = "[green]yes[/green]" if pack.name in covered else "[yellow]none[/yellow]"
        table.add_row(pack.name, pack.version, maturity, suite,
                      ",".join(pack.families) or "any", pack.summary)
    console.print(table)
    console.print("[dim]a pack with no suite can only be checked for liveness, "
                  "never proven[/dim]")

    if verbose:
        for pack in catalog:
            console.print(f"\n[bold]{pack.name}[/bold]")
            if pack.requires:
                console.print(f"  requires: {', '.join(pack.requires)}")
            if pack.provides:
                console.print(f"  provides: {', '.join(pack.provides)}")
            for name, rule in (pack.param_schema or {}).items():
                rule = rule or {}
                bits = [rule.get("type", "string")]
                if "default" in rule:
                    bits.append(f"default={rule['default']!r}")
                if rule.get("enum"):
                    bits.append(f"one of {rule['enum']}")
                if rule.get("secret") or rule.get("secret_fields"):
                    bits.append("[red]secret[/red]")
                console.print(f"    [cyan]{name}[/cyan] — {', '.join(str(b) for b in bits)}")


@click.command("suites")
def suites_cmd():
    """List the acceptance suites and what each one asserts."""
    names = suites_loader.available()
    if not names:
        console.print(f"[yellow]no suites found in {suites_loader.SUITES_DIR}[/yellow]")
        return
    for name in names:
        try:
            suite = suites_loader.load(name)
        except suites_loader.SuiteError as exc:
            console.print(f"[red]{name}: {exc}[/red]")
            continue
        console.print(f"\n[bold]{suite.name}[/bold] — {suite.summary}")
        console.print(f"  [dim]requires: {', '.join(suite.requires) or '-'}[/dim]")
        for check in suite.checks:
            tags = []
            if check.critical:
                tags.append("[red]critical[/red]")
            if check.negative:
                tags.append("[magenta]negative[/magenta]")
            suffix = f"  ({', '.join(tags)})" if tags else ""
            console.print(f"    [cyan]{check.id}[/cyan] — {check.description}{suffix}")


@click.command("status")
def status_cmd():
    """What this host has installed, and whether it was ever proven to work."""
    rows = state.list_installs()
    if not rows:
        console.print("[dim]nothing installed yet[/dim]")
        return

    table = Table(box=None)
    table.add_column("pack", style="bold")
    table.add_column("version")
    table.add_column("installed")
    table.add_column("tested")
    table.add_column("config", style="dim")
    table.add_column("when", style="dim")

    untested = 0
    for row in rows:
        colour = {"installed": "green", "failed": "red"}.get(row["status"], "yellow")
        tested = row["tested"] if "tested" in row.keys() else "untested"
        if tested == "passed":
            tested_cell = "[green]passed[/green]"
        elif tested == "failed":
            tested_cell = "[red]FAILED[/red]"
        else:
            tested_cell = "[yellow]untested[/yellow]"
            if row["status"] == "installed":
                untested += 1
        table.add_row(row["pack"], row["pack_version"],
                      f"[{colour}]{row['status']}[/{colour}]", tested_cell,
                      row["params_hash"], row["installed_at"])
    console.print(table)

    if untested:
        console.print(f"\n[yellow]{untested} installed component(s) have never been "
                      f"proven to work.[/yellow]")
        console.print("[dim]run: dpagent test[/dim]")


@click.command("audit")
@click.argument("run_id", type=int, required=False)
@click.option("--level", type=click.Choice(["debug", "info", "warn", "error"]),
              default="info", help="Minimum level to show.")
def audit_cmd(run_id, level):
    """Replay the audit trail for a run — every decision and who made it."""
    order = {"debug": 0, "info": 1, "warn": 2, "error": 3}
    if run_id is None:
        row = state.latest_run()
        if not row:
            console.print("[dim]no runs recorded[/dim]")
            return
        run_id = row["id"]

    rows = state.events_for(run_id)
    if not rows:
        console.print(f"[yellow]no events for run {run_id}[/yellow]")
        return

    table = Table(box=None, title=f"run {run_id}")
    table.add_column("time", style="dim")
    table.add_column("actor", style="cyan")
    table.add_column("kind", style="magenta")
    table.add_column("message")
    for row in rows:
        if order.get(row["level"], 1) < order[level]:
            continue
        text = Text(row["message"])
        if row["level"] == "error":
            text.stylize("red")
        elif row["level"] == "warn":
            text.stylize("yellow")
        table.add_row(row["ts"].split("T")[-1], row["actor"], row["kind"], text)
    console.print(table)
    console.print(f"[dim]full command log: "
                  f"{executor.LOG_DIR / f'run-{run_id}.jsonl'}[/dim]")


@click.command("errors")
@click.option("--limit", default=20)
def errors_cmd(limit):
    """Failures the catalog had no entry for — the queue of rules to write."""
    rows = state.unmatched_errors(limit)
    if not rows:
        console.print("[green]no unmatched failures — the catalog covered everything[/green]")
        return
    table = Table(box=None)
    table.add_column("when", style="dim")
    table.add_column("pack", style="bold")
    table.add_column("run", justify="right")
    for row in rows:
        table.add_row(row["ts"], row["pack"], str(row["run_id"]))
    console.print(table)
    console.print("\n[dim]Each of these needs an entry in the pack's errors.yaml. "
                  "Check for a drafted one at packs/<pack>/errors.proposed.yaml.[/dim]")
