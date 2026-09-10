"""Authoring packs: synth a draft, lint it, promote it once it is proven."""
from __future__ import annotations

import sys

import click
from rich.panel import Panel

from ..engine import state
from ..library import lint as lint_mod
from ..library import loader as packs
from ..library import synth
from ..llm import client as llm
from ..suites import loader as suites_loader
from .render import console, confirm, fail, print_lint


@click.command("synth")
@click.argument("tool")
@click.option("--hint", default="",
              help="Anything the author should know (version, edition, source).")
@click.option("--family", "families", multiple=True,
              type=click.Choice(["debian", "rhel"]), help="Target families. Repeatable.")
@click.option("--overwrite", is_flag=True, help="Redraft over an existing pack.")
def synth_cmd(tool, hint, families, overwrite):
    """Draft a pack for a tool the agent does not know yet.

    Writes a DRAFT. Read it, prove it on a throwaway VM, then promote it.
    """
    if not llm.available():
        fail("synth needs an LLM credential. Set GEMINI_API_KEY "
             "(free at https://aistudio.google.com/app/apikey).")

    console.print(f"[dim]drafting pack for[/dim] [bold]{tool}[/bold] "
                  f"[dim]with[/dim] [magenta]{llm.get_model()}[/magenta]")
    try:
        result = synth.synthesize(tool, families=list(families) or None, hint=hint,
                                  overwrite=overwrite)
    except (llm.LLMError, FileExistsError, ValueError) as exc:
        fail(str(exc))

    console.print(f"\n[green]wrote[/green] {result.root}")
    for name in result.files:
        console.print(f"  {name}")

    if result.notes:
        console.print(Panel(result.notes, title="author's notes — read these first",
                            border_style="yellow", expand=False))

    print_lint(result.issues)

    console.print(Panel(
        f"This pack is a [yellow]DRAFT[/yellow]. Nothing will run it until you say so.\n\n"
        f"  1. read every script in {result.root}\n"
        f"  2. dry run:  [cyan]dpagent install {tool} --allow-draft --dry-run[/cyan]\n"
        f"  3. throwaway VM: [cyan]sudo -E dpagent install {tool} --allow-draft[/cyan]\n"
        f"  4. write an acceptance suite: [cyan]suites/{tool}/suite.yaml[/cyan]\n"
        f"  5. when it passes: [cyan]dpagent promote {tool}[/cyan]\n\n"
        f"[dim]After step 5 this tool is in the familiar half — no model is involved\n"
        f"in installing it again, on this or any other project.[/dim]",
        title="next", border_style="cyan", expand=False))


@click.command("lint")
@click.argument("names", nargs=-1)
def lint_cmd(names):
    """Static-check packs: manifest, bash syntax, blacklist, catalog shape.

    With no arguments, also checks the shared shell library (`packs/_lib/`) —
    a bug there is inherited silently by every pack that calls it.
    """
    total_blocking = 0

    if not names:
        console.print("[bold]_lib[/bold]  [dim](shared by every pack)[/dim]")
        lib_issues = lint_mod.lint_lib()
        print_lint(lib_issues)
        total_blocking += len(lint_mod.blocking(lib_issues))

    targets = list(names) or packs.available()
    if not targets:
        console.print("[yellow]no packs to lint[/yellow]")
    for name in targets:
        issues = lint_mod.lint_pack(name)
        console.print(f"\n[bold]{name}[/bold]")
        print_lint(issues)
        total_blocking += len(lint_mod.blocking(issues))

    if total_blocking:
        sys.exit(1)


@click.command("promote")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
def promote_cmd(name, yes):
    """Mark a draft pack stable, once it has been read and proven."""
    try:
        pack = packs.load(name)
    except packs.PackError as exc:
        fail(str(exc))
    if not pack.is_draft:
        console.print(f"[dim]{name} is already stable[/dim]")
        return

    record = state.get_install(name)
    installed = record is not None and record["status"] == "installed"
    tested = (record["tested"] if record and "tested" in record.keys() else "untested")

    blockers = []
    if not installed:
        blockers.append("it has never installed successfully on this host")
    if not suites_loader.exists(name) and not any(
            name in (suites_loader.load(s).requires or [])
            for s in suites_loader.available()):
        blockers.append("no acceptance suite covers it, so 'proven' means nothing yet")
    elif tested != "passed":
        blockers.append(f"its acceptance suite has not passed (currently: {tested})")

    if blockers:
        console.print(Panel(
            "[yellow]this pack is not ready to be promoted[/yellow]\n\n"
            + "\n".join(f"  · {b}" for b in blockers)
            + "\n\n[dim]Promoting an unproven pack is how a bad recipe spreads to\n"
              "every future project — that is the cost, and it is paid later.[/dim]",
            border_style="yellow", expand=False))
        if not yes and not confirm("Promote anyway?", default=False):
            sys.exit(1)

    if not yes and not confirm(f"Have you read every script in {pack.root}?",
                               default=False):
        sys.exit(1)

    try:
        synth.promote(name)
    except ValueError as exc:
        fail(str(exc))
    console.print(f"[green]{name} is now stable[/green] — future installs use it "
                  f"with no model involved.")
