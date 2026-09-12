"""Operating an installed system: verify, test, rollback."""
from __future__ import annotations

import json
import sys

import click
from rich.panel import Panel
from rich.table import Table

from ..engine import executor, os_detect, params as params_mod, state
from ..library import loader as packs
from ..suites import loader as suites_loader
from ..suites import runner as suites_runner
from .render import (console, confirm, echo_output, fail, suite_summary,
                     SuiteReporter)

FAKE_OS_CHOICES = sorted(os_detect.FAKE)


def _stored_params(pack) -> dict:
    """Params recorded at install time, with masked secrets dropped.

    A masked value must never be replayed as if it were the real one — better to
    fall back to the pack default and be obviously wrong than to silently use
    the literal string "***REDACTED***" as a password.

    A `required` secret has no default, so dropping its masked value would make
    `resolve()` refuse the whole pack as missing a required param - on every
    post-install operation (verify/test/rollback), forever, for any pack that
    requires a secret. That's not a install-time validation failure; the secret
    genuinely cannot be recovered, by design. A suite that needs it is expected
    to read it from what install actually configured (a generated profile, an
    env file) rather than from resolved params - see the note in run_suites().
    Fill it with an obviously-fake placeholder purely so resolve() doesn't
    treat "reconstructible" as a precondition for "installed".
    """
    record = state.get_install(pack.name)
    supplied = json.loads(record["params_json"]) if record else {}
    supplied = {k: v for k, v in supplied.items() if v != params_mod.MASK}
    for name, rule in (pack.param_schema or {}).items():
        if (rule or {}).get("secret") and (rule or {}).get("required") and name not in supplied:
            supplied[name] = "(redacted - see the pack's own generated config)"
    return params_mod.resolve(pack.param_schema, supplied, pack=pack.name)


def run_suites(pack_names: list[str], os_info, run_id: int,
               only: list[str] | None = None) -> list:
    """Run every acceptance suite covered by the given packs.

    Shared by `dpagent test` and by `dpagent install`, which calls it before it
    is allowed to report success.
    """
    if only:
        suites = []
        for name in only:
            try:
                suites.append(suites_loader.load(name))
            except suites_loader.SuiteError as exc:
                fail(str(exc))
    else:
        suites = suites_loader.for_packs(pack_names)

    if not suites:
        return []

    reporter = SuiteReporter()
    outcomes = []
    for suite in suites:
        missing = []
        for required in suite.requires:
            record = state.get_install(required)
            if record is None or record["status"] != "installed":
                missing.append(required)
        if missing:
            console.print(f"[dim]skipping suite {suite.name}: "
                          f"{', '.join(missing)} not installed[/dim]")
            continue
        try:
            # Params come from the first required pack. A cross-component suite
            # that needs more than one pack's params should read them from the
            # environment it sets up rather than expecting them here.
            pack = packs.load(suite.requires[0] if suite.requires else suite.name)
            resolved = _stored_params(pack)
        except (packs.PackError, params_mod.ParamError) as exc:
            console.print(f"[yellow]skipping suite {suite.name}: {exc}[/yellow]")
            continue

        outcome = suites_runner.run_suite(suite, os_info, resolved, run_id,
                                          reporter=reporter)
        outcomes.append(outcome)

        # Record the verdict against every pack the suite covers, so `status`
        # can never show a component as good on the strength of verify alone.
        verdict = "passed" if outcome.ok else "failed"
        for pack_name in (suite.requires or [suite.name]):
            if state.get_install(pack_name):
                state.record_tested(pack_name, verdict)

    return outcomes


@click.command("test")
@click.argument("names", nargs=-1)
@click.option("--fake-os", type=click.Choice(FAKE_OS_CHOICES))
def test_cmd(names, fake_os):
    """Run acceptance suites against the installed system.

    \b
    Deeper than `verify`: these write and read real data, restart services to
    prove durability, and assert that things which should be refused are.
    """
    try:
        os_info = os_detect.resolve(fake_os)
    except RuntimeError as exc:
        fail(str(exc))

    installed = [row["pack"] for row in state.list_installs()
                 if row["status"] == "installed"]
    if not installed and not names:
        console.print("[yellow]nothing installed to test[/yellow]")
        return

    run_id = state.start_run("test", ",".join(names or installed), os_info.as_dict())
    state.event("run.start", f"acceptance test {list(names) or installed}",
                run_id=run_id, actor="user")

    outcomes = run_suites(installed, os_info, run_id, only=list(names) or None)

    if not outcomes:
        state.finish_run(run_id, "ok")
        console.print(Panel(
            "[yellow]no acceptance suite covers what is installed[/yellow]\n"
            "[dim]An install with no suite is unproven, whatever verify said.\n"
            f"Write one under {suites_loader.SUITES_DIR}/<name>/suite.yaml.[/dim]",
            border_style="yellow", expand=False))
        return

    suite_summary(outcomes)
    ok = all(o.ok for o in outcomes)
    state.finish_run(run_id, "ok" if ok else "failed")
    if not ok:
        console.print(f"[dim]detail: dpagent audit {run_id}[/dim]")
        sys.exit(1)


@click.command("verify")
@click.argument("names", nargs=-1)
@click.option("--fake-os", type=click.Choice(FAKE_OS_CHOICES))
def verify_cmd(names, fake_os):
    """Run each pack's health check — liveness only. See `dpagent test` for proof."""
    try:
        os_info = os_detect.resolve(fake_os)
    except RuntimeError as exc:
        fail(str(exc))

    targets = list(names) or [row["pack"] for row in state.list_installs()
                              if row["status"] == "installed"]
    if not targets:
        console.print("[yellow]nothing installed to verify[/yellow]")
        return

    run_id = state.start_run("verify", ",".join(targets), os_info.as_dict())
    table = Table(box=None)
    table.add_column("pack", style="bold")
    table.add_column("result")
    failed = False

    for name in targets:
        try:
            pack = packs.load(name)
        except packs.PackError as exc:
            table.add_row(name, f"[red]{exc}[/red]")
            failed = True
            continue
        if not pack.verify:
            table.add_row(name, "[yellow]no verify script[/yellow]")
            continue

        resolved = _stored_params(pack)
        env = executor.build_env(os_info.as_env(), resolved, False,
                                 {"DP_PACK": name, "DP_PACK_ROOT": str(pack.root)})
        result = executor.run_script(pack.path(pack.verify), env, f"run-{run_id}",
                                     timeout=180, cwd=pack.root)
        state.event("verify", f"{name} rc={result.rc}", run_id=run_id,
                    level="info" if result.ok else "error",
                    data={"pack": name, "rc": result.rc})
        table.add_row(name, "[green]ok[/green]" if result.ok else "[red]failed[/red]")
        if not result.ok:
            failed = True
            echo_output(result.output, style="red", limit=15)

    console.print(table)
    state.finish_run(run_id, "failed" if failed else "ok")
    console.print("[dim]verify only proves the component is alive. "
                  "`dpagent test` proves it works.[/dim]")
    if failed:
        sys.exit(1)


@click.command("rollback")
@click.argument("name")
@click.option("--fake-os", type=click.Choice(FAKE_OS_CHOICES))
@click.option("--dry-run", is_flag=True)
@click.option("--yes", "-y", is_flag=True)
def rollback_cmd(name, fake_os, dry_run, yes):
    """Remove a pack. Destroys its data — asks first."""
    try:
        pack = packs.load(name)
    except packs.PackError as exc:
        fail(str(exc))
    if not pack.rollback:
        fail(f"{name} declares no rollback script")

    try:
        os_info = os_detect.resolve(fake_os)
    except RuntimeError as exc:
        fail(str(exc))

    dependents = [p.name for p in packs.catalog()
                  if name in p.requires and state.get_install(p.name)]
    lines = [f"[bold red]{name}[/bold red] will be removed, including its data.",
             f"[dim]{pack.path(pack.rollback)}[/dim]"]
    if dependents:
        lines.append("")
        lines.append(f"[yellow]still depended on by: {', '.join(dependents)}[/yellow]")
    console.print(Panel("\n".join(lines), border_style="red", expand=False))

    if not dry_run and not yes and not confirm(
            f"Really roll back {name}?", default=False):
        sys.exit(1)

    resolved = _stored_params(pack)
    run_id = state.start_run("rollback", name, os_info.as_dict(), dry_run=dry_run)
    state.event("rollback.start", f"rolling back {name}", run_id=run_id, actor="user")
    env = executor.build_env(os_info.as_env(), resolved, dry_run,
                             {"DP_PACK": name, "DP_PACK_ROOT": str(pack.root)})
    result = executor.run_script(pack.path(pack.rollback), env, f"run-{run_id}",
                                 timeout=900, cwd=pack.root)
    echo_output(result.output, style="dim" if result.ok else "red")

    if result.ok and not dry_run:
        state.record_install(name, pack.version, {}, "", os_info.family,
                             "rolled_back", run_id)
    state.finish_run(run_id, "ok" if result.ok else "failed")
    console.print("[green]rolled back[/green]" if result.ok
                  else "[red]rollback failed[/red]")
    if not result.ok:
        sys.exit(1)
