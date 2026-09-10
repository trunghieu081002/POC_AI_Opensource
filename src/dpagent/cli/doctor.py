"""`dpagent doctor` — check what the machine can support before installing.

This is the "check version in the machine" half of the tool. It never installs
anything: it resolves the same dependency graph `install` would, then evaluates
each pack's declared `host_needs` (Python version, memory, disk, required
commands) against the real host, using sysinfo/version directly rather than
each pack re-implementing its own ad-hoc checks.

A blocker found here is a blocker `install` will hit too — seeing all of them at
once, before anything runs, is the point.
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml
from rich.panel import Panel
from rich.table import Table

from ..engine import executor, os_detect, params as params_mod, resolver, sysinfo, version
from ..library import loader as packs
from ..llm import router
from .render import console, fail

FAKE_OS_CHOICES = sorted(os_detect.FAKE)


def _check_pack(pack: packs.Pack) -> tuple[list[str], list[str]]:
    """Returns (ok_lines, blocker_lines)."""
    needs = pack.host_needs
    ok: list[str] = []
    blockers: list[str] = []

    if needs.python:
        match = version.find_python(needs.python)
        if match:
            ok.append(f"python {needs.python} -> {match.path} ({match.version})")
        else:
            found = version.all_pythons()
            seen = ", ".join(f"{m.path} ({m.version})" for m in found) or "none found"
            blockers.append(
                f"needs python {needs.python}, none on PATH satisfies it "
                f"[dim](found: {seen})[/dim]"
            )

    if needs.memory_mb:
        actual = sysinfo.memory_mb()
        if actual is None:
            ok.append("memory: could not read /proc/meminfo (not Linux?)")
        elif actual >= needs.memory_mb:
            ok.append(f"memory: {actual}MB available (needs {needs.memory_mb}MB)")
        else:
            blockers.append(f"needs {needs.memory_mb}MB memory, host has {actual}MB")

    for path, need_mb in needs.disk_mb.items():
        actual = sysinfo.disk_free_mb(path)
        if actual is None:
            ok.append(f"disk {path}: could not check")
        elif actual >= need_mb:
            ok.append(f"disk {path}: {actual}MB free (needs {need_mb}MB)")
        else:
            blockers.append(f"needs {need_mb}MB free on {path}, has {actual}MB")

    for cmd in needs.commands:
        if sysinfo.command_exists(cmd):
            ok.append(f"command `{cmd}` present")
        else:
            blockers.append(f"command `{cmd}` is missing")

    # Ports are a resolved param at install time (e.g. --set port=5433), so a
    # static host_needs.ports entry only covers a pack's fixed, non-configurable
    # ports (rare). Dynamic port conflicts are preflight's job, not doctor's.
    for port in needs.ports:
        if sysinfo.port_free(port):
            ok.append(f"port {port} is free")
        else:
            blockers.append(f"port {port} is already in use")

    return ok, blockers


def _run_detect(pack: packs.Pack, os_info) -> list[str]:
    """Run a pack's detect.sh (if any) with default params, for reporting.

    Read-only by convention (detect.sh scripts only ever print facts), and safe
    to run whether or not the pack is installed — every shipped detect.sh
    handles "not installed" by printing installed=0 or empty values rather than
    failing.
    """
    if not pack.detect:
        return []
    try:
        resolved = params_mod.resolve(pack.param_schema, {}, pack=pack.name)
    except params_mod.ParamError:
        # A required secret with no default (e.g. airflow's backend_password)
        # can't be resolved without operator input; detect simply isn't run.
        return []
    env = executor.build_env(os_info.as_env(), resolved, dry_run=True,
                             extra={"DP_PACK": pack.name, "DP_PACK_ROOT": str(pack.root)})
    result = executor.run_script(pack.path(pack.detect), env, "doctor",
                                 timeout=30, cwd=pack.root)
    if not result.ok:
        return [f"[dim](detect script failed: rc={result.rc})[/dim]"]
    return [line for line in result.stdout.splitlines() if "=" in line]


@click.command("doctor")
@click.argument("names", nargs=-1)
@click.option("--spec", "spec_path", type=click.Path(exists=True, dir_okay=False),
              help="Read the pack list from a project.yaml instead of naming packs.")
@click.option("--fake-os", type=click.Choice(FAKE_OS_CHOICES))
def doctor_cmd(names, spec_path, fake_os):
    """Check the machine against what the given packs need. Installs nothing.

    \b
      dpagent doctor postgres dbt airflow
      dpagent doctor --spec examples/etl-stack.yaml
      dpagent doctor              # every pack in the library
    """
    try:
        os_info = os_detect.resolve(fake_os)
    except RuntimeError as exc:
        fail(str(exc))

    table = Table(show_header=False, box=None)
    for key, value in os_info.as_dict().items():
        table.add_row(f"[dim]{key}[/dim]", str(value))
    console.print(Panel(table, title="target OS", border_style="cyan", expand=False))

    targets = list(names)
    if spec_path:
        spec = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")) or {}
        spec_names, _ = router.spec_to_requests(spec)
        targets = spec_names
    targets = targets or packs.available()
    if not targets:
        console.print(f"[yellow]no packs found in {packs.PACKS_DIR}[/yellow]")
        return

    try:
        resolution = resolver.resolve(targets, family=os_info.family)
    except (resolver.ResolveError, packs.PackError) as exc:
        fail(str(exc))

    if resolution.missing:
        console.print(f"[yellow]no pack for:[/yellow] {', '.join(resolution.missing)}")

    any_blocked = False
    for pack in resolution.order:
        ok, blockers = _check_pack(pack)
        if not ok and not blockers:
            console.print(f"\n[bold]{pack.name}[/bold]  [dim](no declared requirements)[/dim]")
            continue

        console.print(f"\n[bold]{pack.name}[/bold]")
        for line in ok:
            console.print(f"  [green]ok[/green]   {line}")
        for line in blockers:
            console.print(f"  [red]BLOCK[/red] {line}")
            any_blocked = True

        for fact in _run_detect(pack, os_info):
            console.print(f"  [dim]· {fact}[/dim]")

    console.print()
    if any_blocked:
        console.print(Panel(
            "[bold red]one or more packs cannot install on this host as configured[/bold red]\n"
            "[dim]Nothing was installed. Resolve the blockers above, or adjust\n"
            "params (e.g. a different port) and run doctor again.[/dim]",
            border_style="red", expand=False))
        sys.exit(1)

    console.print(Panel("[bold green]this host satisfies every declared requirement[/bold green]",
                        border_style="green", expand=False))
