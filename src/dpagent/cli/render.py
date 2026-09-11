"""Console output. The engine stays print-free; everything visual lives here."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..engine import params as params_mod, runner
from ..library import lint as lint_mod
from ..suites import runner as suites_runner

console = Console()
err_console = Console(stderr=True)


def fail(message: str, code: int = 1):
    err_console.print(f"[red]error:[/red] {message}")
    sys.exit(code)


def echo_output(text: str, style: str = "dim", limit: int = 40) -> None:
    lines = [l for l in params_mod.redact(text).splitlines() if l.strip()]
    for line in lines[-limit:]:
        console.print(f"      [{style}]{line}[/{style}]")


def parse_set(pairs: tuple[str, ...], default_pack: str | None) -> dict[str, dict]:
    """--set postgres.port=5433 / --set port=5433 -> {"postgres": {"port": 5433}}"""
    out: dict[str, dict] = {}
    for pair in pairs:
        if "=" not in pair:
            fail(f"--set expects key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        if "." in key:
            pack_name, _, param = key.partition(".")
        elif default_pack:
            pack_name, param = default_pack, key
        else:
            fail(f"--set {key}=... is ambiguous with several packs; "
                 f"write --set <pack>.{key}=...")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # Leave it as the raw string. Whether a comma means "split this
            # into a list" depends on the param's declared type, which isn't
            # known here - params.resolve() does that split for `list` typed
            # params. A `string` param (e.g. postgres.listen_addresses, whose
            # own GUC syntax is itself a comma-joined value) must reach there
            # unsplit, or it round-trips through str(a_list) into garbage.
            value = raw
        out.setdefault(pack_name, {})[param] = value
    return out


def print_lint(issues) -> None:
    if not issues:
        console.print("[green]lint clean[/green]")
        return
    table = Table(box=None)
    table.add_column("level")
    table.add_column("where", style="dim")
    table.add_column("issue")
    for issue in issues:
        colour = "red" if issue.level == lint_mod.ERROR else "yellow"
        table.add_row(f"[{colour}]{issue.level}[/{colour}]", issue.where, issue.message)
    console.print(table)
    blocking = lint_mod.blocking(issues)
    if blocking:
        console.print(f"[red]{len(blocking)} blocking issue(s) — fix before running[/red]")


# ------------------------------------------------------------------ install

class InstallReporter(runner.Reporter):
    def pack_start(self, pack, resolved):
        badge = "" if pack.maturity == "stable" else "  [yellow]DRAFT[/yellow]"
        console.print()
        console.print(Panel(
            f"[bold]{pack.name}[/bold] v{pack.version}{badge}\n[dim]{pack.summary}[/dim]",
            border_style="cyan", expand=False))
        shown = {k: v for k, v in resolved.items() if v not in (None, "", [], {})}
        if shown:
            console.print(f"  [dim]params:[/dim] {json.dumps(shown, ensure_ascii=False)}")

    def pack_skipped(self, pack, reason):
        console.print(f"[green]=[/green] [bold]{pack.name}[/bold] — {reason}")

    def preflight(self, pack, result):
        if result.ok:
            console.print("  [green]preflight ok[/green]")
        else:
            console.print("  [red]preflight failed[/red]")
            echo_output(result.output, style="red")

    def step_start(self, pack, step, index, total):
        console.print(f"  [cyan]{index}/{total}[/cyan] {step.id} · [dim]{step.description}[/dim]")

    def step_done(self, outcome):
        if outcome.status == runner.OK:
            fixed = (f" [yellow](after {len(outcome.fixes_applied)} fix)[/yellow]"
                     if outcome.fixes_applied else "")
            console.print(f"      [green]ok[/green] {outcome.duration_ms}ms{fixed}")
        elif outcome.status == runner.SKIPPED:
            console.print(f"      [dim]skip — {outcome.reason}[/dim]")
        else:
            console.print(f"      [red]{outcome.status}[/red] rc={outcome.rc} — {outcome.reason}")

    def fixing(self, match, attempt):
        console.print(f"      [yellow]matched[/yellow] [bold]{match.entry.id}[/bold] "
                      f"[dim]({Path(match.entry.source).name})[/dim]")
        console.print(f"      [dim]cause:[/dim] {match.entry.cause.strip()}")
        for command in match.entry.autofix:
            console.print(f"      [yellow]fix $[/yellow] {command}")

    def fix_blocked(self, command, verdict):
        console.print(f"      [red]autofix blocked[/red] ({verdict.rule_id}): {verdict.reason}")
        console.print(f"      [dim]$ {command}[/dim]")

    def unmatched(self, pack, step, result):
        console.print("      [red]no catalog entry matched this failure[/red]")
        echo_output(result.output, style="dim", limit=25)

    def verify(self, pack, result):
        style = "green" if result.ok else "red"
        console.print(f"  [{style}]verify {'ok' if result.ok else 'failed'}[/{style}]")
        echo_output(result.output, style="dim" if result.ok else "red")

    def pack_done(self, outcome):
        if outcome.status in (runner.OK, runner.SKIPPED):
            return
        console.print(f"  [red]{outcome.pack}: {outcome.reason}[/red]")


# ------------------------------------------------------------------ suites

class SuiteReporter(suites_runner.Reporter):
    def suite_start(self, suite):
        console.print()
        console.print(Panel(
            f"[bold]{suite.name}[/bold]\n[dim]{suite.summary}[/dim]",
            title="acceptance suite", border_style="blue", expand=False))

    def phase(self, name):
        console.print(f"  [dim]{name}[/dim]")

    def check_done(self, outcome):
        label = outcome.description or outcome.id
        tag = " [magenta](negative)[/magenta]" if outcome.negative else ""
        if outcome.status == suites_runner.PASSED:
            console.print(f"    [green]PASS[/green] {label}{tag} "
                          f"[dim]{outcome.duration_ms}ms[/dim]")
        elif outcome.status == suites_runner.SKIPPED:
            console.print(f"    [dim]SKIP {label} — {outcome.output}[/dim]")
        else:
            marker = "[red]FAIL[/red]" if not outcome.critical else "[red]FAIL (critical)[/red]"
            console.print(f"    {marker} {label}{tag}")
            echo_output(outcome.output, style="red", limit=20)

    def suite_done(self, outcome):
        if outcome.ok:
            console.print(f"  [green]{outcome.suite}: {outcome.passed} checks passed[/green]")
        else:
            console.print(f"  [red]{outcome.suite}: {outcome.reason}[/red]")


def suite_summary(outcomes: list) -> None:
    """The line that decides whether an install may be called successful."""
    if not outcomes:
        return
    table = Table(box=None)
    table.add_column("suite", style="bold")
    table.add_column("result")
    table.add_column("checks", justify="right")
    for outcome in outcomes:
        if outcome.status == suites_runner.PASSED:
            result = "[green]passed[/green]"
        elif outcome.status == suites_runner.ERROR:
            result = "[yellow]could not run[/yellow]"
        else:
            result = "[red]FAILED[/red]"
        table.add_row(outcome.suite, result,
                      f"{outcome.passed}/{len(outcome.checks)}")
    console.print()
    console.print(Panel(table, title="acceptance", border_style="blue", expand=False))


def confirm(message: str, default: bool = True) -> bool:
    return click.confirm(message, default=default)
