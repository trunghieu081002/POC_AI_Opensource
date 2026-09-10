"""Installing: by pack name, from a spec, or from a plain-language request."""
from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml
from rich.panel import Panel
from rich.table import Table

from ..engine import os_detect, resolver, runner, state
from ..library import loader as packs
from ..library import synth
from ..llm import client as llm
from ..llm import router
from .operate import run_suites
from .render import (console, confirm, fail, parse_set, suite_summary,
                     InstallReporter)

FAKE_OS_CHOICES = sorted(os_detect.FAKE)


def _do_install(names, params_by_name, fake_os, dry_run, force, yes, allow_draft,
                run_tests=True):
    try:
        os_info = os_detect.resolve(fake_os)
    except RuntimeError as exc:
        fail(str(exc))

    try:
        resolution = resolver.resolve(names, params_by_name, family=os_info.family)
    except (resolver.ResolveError, packs.PackError) as exc:
        fail(str(exc))

    if resolution.missing:
        console.print(f"[yellow]no pack for:[/yellow] {', '.join(resolution.missing)}")
        console.print(f"[dim]draft one with:[/dim] dpagent synth {resolution.missing[0]}")
        if not confirm("Continue with the packs that do exist?", default=False):
            sys.exit(1)

    drafts = [p.name for p in resolution.order if p.is_draft]
    if drafts and not allow_draft:
        fail(f"pack(s) {drafts} are drafts and have not been proven on a host. "
             f"Read them, then re-run with --allow-draft (and use a test VM).")

    table = Table(box=None)
    table.add_column("#", style="dim")
    table.add_column("pack", style="bold")
    table.add_column("steps", justify="right")
    table.add_column("why", style="dim")
    for index, pack in enumerate(resolution.order, start=1):
        why = "requested" if resolution.requests[pack.name].requested else "dependency"
        table.add_row(str(index), pack.name, str(len(pack.steps)), why)
    console.print(Panel(table, title=f"plan · {os_info.id} {os_info.version} "
                                     f"({os_info.family}/{os_info.pkg_mgr})",
                        border_style="cyan", expand=False))

    if dry_run:
        console.print("[yellow]dry-run: commands are printed, nothing is executed[/yellow]")
    elif not yes and not confirm("Apply this plan?", default=True):
        sys.exit(1)

    run_id = state.start_run("install", ",".join(p.name for p in resolution.order),
                             os_info.as_dict(), dry_run=dry_run,
                             meta={"requested": names})
    state.event("run.start", f"install {names}", run_id=run_id, actor="user",
                data={"dry_run": dry_run, "force": force})

    engine = runner.Engine(os_info, run_id, dry_run=dry_run, force=force,
                           reporter=InstallReporter(),
                           diagnose=synth.diagnose if llm.available() else None)
    outcome = engine.install(resolution)

    console.print()
    if not outcome.ok:
        _report_failure(outcome, run_id)
        sys.exit(3)

    installed = [p.pack for p in outcome.packs]

    # An install is not finished because the installer finished. Nothing is
    # reported as successful until an acceptance suite says the system works.
    if dry_run:
        console.print(Panel("[yellow]dry-run complete — nothing was executed, "
                            "and nothing was proven[/yellow]",
                            border_style="yellow", expand=False))
        return

    if not run_tests:
        console.print(Panel(
            "[yellow]installed, but NOT tested[/yellow]\n"
            "[dim]--no-test was given. `dpagent status` will show these as untested\n"
            "until `dpagent test` has run.[/dim]",
            border_style="yellow", expand=False))
        return

    test_run = state.start_run("test", ",".join(installed), os_info.as_dict(),
                               meta={"after_install": run_id})
    outcomes = run_suites(installed, os_info, test_run)

    if not outcomes:
        state.finish_run(test_run, "ok")
        console.print(Panel(
            f"[green]installed[/green] — but [yellow]unproven[/yellow]\n\n"
            f"No acceptance suite covers {', '.join(installed)}, so nothing beyond\n"
            f"each pack's own liveness check was tested.\n\n"
            f"[dim]Write one: {Path('suites') / installed[-1] / 'suite.yaml'}[/dim]",
            border_style="yellow", expand=False))
        return

    suite_summary(outcomes)
    passed = all(o.ok for o in outcomes)
    state.finish_run(test_run, "ok" if passed else "failed")

    if passed:
        console.print(Panel(
            f"[bold green]installed and proven working[/bold green]\n"
            f"[dim]install run {run_id} · acceptance run {test_run}[/dim]",
            border_style="green", expand=False))
        return

    console.print(Panel(
        "[bold red]the installer finished, but the system does not work[/bold red]\n\n"
        "Every step succeeded and verify passed. The acceptance suite found the\n"
        "system failing anyway — which is the entire reason it runs.\n\n"
        f"[dim]what failed: dpagent audit {test_run}[/dim]\n"
        f"[dim]re-run just the tests: dpagent test[/dim]",
        border_style="red", expand=False))
    sys.exit(4)


def _report_failure(outcome, run_id: int) -> None:
    last = outcome.packs[-1] if outcome.packs else None
    lines = [f"[bold red]halted[/bold red] on [bold]{last.pack if last else '?'}[/bold]"]
    if last:
        lines.append(f"[dim]{last.reason}[/dim]")
    if last and last.status == runner.NEEDS_REVIEW:
        pack_dir = packs.PACKS_DIR / last.pack
        lines.append("")
        lines.append("This failure is not in the catalog yet.")
        if (pack_dir / "errors.proposed.yaml").exists():
            lines.append(f"A proposed entry was drafted at "
                         f"[cyan]{pack_dir / 'errors.proposed.yaml'}[/cyan].")
            lines.append("Review it, move it into errors.yaml, and re-run.")
        else:
            lines.append(f"Run [cyan]dpagent audit {run_id}[/cyan] to see the output, "
                         f"then add an entry to {last.pack}/errors.yaml.")
    lines.append("")
    lines.append("[dim]re-run to resume — completed steps are skipped[/dim]")
    console.print(Panel("\n".join(lines), border_style="red", expand=False))


_shared_options = [
    click.option("--fake-os", type=click.Choice(FAKE_OS_CHOICES),
                 help="Pretend to be this distro (plan from a dev machine)."),
    click.option("--dry-run", is_flag=True, help="Print every command, execute nothing."),
    click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt."),
    click.option("--allow-draft", is_flag=True, help="Permit packs that are still drafts."),
    click.option("--no-test", is_flag=True,
                 help="Skip the acceptance suite. The result is then unproven."),
]


def shared(func):
    for option in reversed(_shared_options):
        func = option(func)
    return func


@click.command("install")
@click.argument("names", nargs=-1, required=True)
@click.option("--set", "settings", multiple=True, metavar="[PACK.]KEY=VALUE",
              help="Override a pack parameter. Repeatable.")
@click.option("--force", is_flag=True, help="Re-run steps even if checkpointed.")
@shared
def install_cmd(names, settings, force, fake_os, dry_run, yes, allow_draft, no_test):
    """Install one or more packs by name, then prove they work.

    \b
      dpagent install postgres
      dpagent install postgres --set port=5433 --set databases=warehouse,staging
    """
    names = list(names)
    overrides = parse_set(settings, names[0] if len(names) == 1 else None)
    _do_install(names, overrides, fake_os, dry_run, force, yes, allow_draft,
                run_tests=not no_test)


@click.command("spec")
@click.argument("spec_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--force", is_flag=True)
@shared
def spec_cmd(spec_path, force, fake_os, dry_run, yes, allow_draft, no_test):
    """Install from a project.yaml — the reproducible form of an install."""
    spec = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")) or {}
    names, params_by_name = router.spec_to_requests(spec)
    if not names:
        fail(f"{spec_path} has no `components:` entries")
    console.print(f"[dim]project:[/dim] {spec.get('project', '(unnamed)')}")
    _do_install(names, params_by_name, fake_os, dry_run, force, yes, allow_draft,
                run_tests=not no_test)


@click.command("do")
@click.argument("request", nargs=-1, required=True)
@click.option("--save", type=click.Path(dir_okay=False),
              help="Write the routing to a project.yaml instead of installing.")
@shared
def do_cmd(request, save, fake_os, dry_run, yes, allow_draft, no_test):
    """Install from a plain-language request.

    \b
      dpagent do "cài postgres 15 với database warehouse"
      dpagent do "dựng full ETL stack" --save project.yaml
    """
    text = " ".join(request)
    if not llm.available():
        fail("`do` needs an LLM credential to route the request. "
             "Set GEMINI_API_KEY, or use `dpagent install <pack>` which needs none.")

    console.print(f"[dim]routing with[/dim] [magenta]{llm.get_model()}[/magenta]")
    try:
        routed = router.route(text)
    except llm.LLMError as exc:
        fail(str(exc))

    if routed.notes:
        console.print(Panel(routed.notes, title="router notes",
                            border_style="magenta", expand=False))
    for rejected in routed.rejected:
        console.print(f"[yellow]dropped:[/yellow] {rejected}")

    if routed.unknown:
        console.print(f"\n[yellow]no pack yet for:[/yellow] {', '.join(routed.unknown)}")
        console.print("[dim]these need a draft pack first:[/dim]")
        for name in routed.unknown:
            console.print(f"  dpagent synth {name}")

    if not routed.names:
        fail("nothing to install — the request did not map onto any pack")

    console.print(f"\n[bold]routed to:[/bold] {', '.join(routed.names)}")

    if save:
        Path(save).write_text(router.to_spec(routed), encoding="utf-8")
        console.print(f"[green]wrote[/green] {save}")
        console.print(f"[dim]review it, then:[/dim] dpagent spec {save}")
        return

    _do_install(routed.names, routed.params, fake_os, dry_run, False, yes,
                allow_draft, run_tests=not no_test)
