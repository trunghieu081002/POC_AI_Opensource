"""`dpagent pipeline ...` - Layer 2's staged-ingestion manifests.

Mirrors install/verify/test's own verbs (docs/layer2.md, "CLI") for the same
reason those exist: `lint` catches a mistake in the manifest itself, cheaply,
before anything is generated or run against a real warehouse.
"""
from __future__ import annotations

import getpass
import os
import sys
import time
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..engine import state
from ..llm import client as llm
from ..pipelines import approval as approval_mod
from ..pipelines import deploy as deploy_mod
from ..pipelines import extract as extract_mod
from ..pipelines import fixture as fixture_mod
from ..pipelines import generator as generator_mod
from ..pipelines import loader as pipelines_mod
from ..pipelines import synth as synth_mod
from ..pipelines import validate as validate_mod
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


_STANDARD_WAREHOUSE_REFS = {
    "host": "${WAREHOUSE_DB_HOST:-localhost}",
    "port": "${WAREHOUSE_DB_PORT:-5432}",
    "database": "${WAREHOUSE_DB_NAME:-warehouse}",
    "user": "${WAREHOUSE_DB_USER}",
    "password": "${WAREHOUSE_DB_PASSWORD}",
}

_STEP_STYLE = {"pass": "green", "fail": "red", "skipped": "dim"}


def _print_validation_steps(report) -> None:
    console.print(f"[bold]load:[/bold] "
                 f"[{'green' if report.load_ok else 'red'}]"
                 f"{'pass' if report.load_ok else 'fail'}[/]")
    if report.dbt is not None:
        colour = _STEP_STYLE.get(report.dbt.status, "yellow")
        console.print(f"[bold]dbt project/Jinja parse:[/bold] "
                     f"[{colour}]{report.dbt.status}[/{colour}]"
                     + (f" - {report.dbt.detail}" if report.dbt.detail else ""))
        console.print("[dim]  (structure/Jinja/config only, no live database - does NOT "
                      "confirm the SQL itself is valid Postgres, e.g. a 'SELEC ...' typo "
                      "still parses clean; not dbt compile/run; the SQL is only actually "
                      "proven by steps 4-5's real dbt run against a fixture)[/dim]")
    if report.procedures is not None:
        colour = _STEP_STYLE.get(report.procedures.status, "yellow")
        console.print(f"[bold]procedures:[/bold] [{colour}]{report.procedures.status}[/{colour}]"
                     + (f" - {report.procedures.detail}" if report.procedures.detail else ""))
    if report.dbt_dependencies is not None:
        colour = _STEP_STYLE.get(report.dbt_dependencies.status, "yellow")
        console.print(f"[bold]dbt ref()/source() check:[/bold] "
                     f"[{colour}]{report.dbt_dependencies.status}[/{colour}]"
                     + (f" - {report.dbt_dependencies.detail}"
                        if report.dbt_dependencies.detail else ""))
    if report.content_hash:
        console.print(f"[dim]content hash at validation time: {report.content_hash}[/dim]")


@pipeline_group.command("synth")
@click.argument("name")
@click.option("--brd", "brd_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="File containing the BRD/report spec text, verbatim.")
@click.option("--schema", "schema_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="File containing the verified source schema: real table/column "
                   "names, types, and a one-line meaning for anything not obvious. "
                   "Not a live database connection - an operator confirms this by hand.")
@click.option("--secret", "secrets", multiple=True, metavar="NAME=DESCRIPTION",
              help="A secret this pipeline may reference as ${NAME} (e.g. "
                   "SRC_DB_PASSWORD='Odoo replica password'). Repeatable.")
@click.option("--warehouse-schema", default=None,
              help="This pipeline's own warehouse schema - defaults to NAME, same "
                   "convention every hand-written pipeline in this repo uses.")
@click.option("--hint", default="", help="Anything else the model should know.")
@click.option("--overwrite", is_flag=True,
              help="Redraft over an existing draft pipeline of the same name. Refused "
                   "outright if that pipeline is maturity: reviewed - choose a "
                   "different name instead of clobbering real, promoted work.")
def synth_cmd(name, brd_path, schema_path, secrets, warehouse_schema, hint, overwrite):
    """Draft a pipeline from a BRD - a model writes pipeline.yaml + its SQL.

    Writes a DRAFT (docs/layer2.md, "Authoring pipelines with a model") -
    `deploy()` refuses it until `dpagent pipeline promote NAME` runs, which
    itself refuses while `dpagent pipeline lint NAME` still fails. If the
    BRD is ambiguous about anything that would change the actual numbers, no
    files are written at all - the model is required to ask instead of
    guessing, and this command prints exactly what it needs answered.
    """
    if not llm.available():
        fail("no LLM credential found. Set GEMINI_API_KEY (free tier at "
            "https://aistudio.google.com/app/apikey) or point DPAGENT_MODEL at "
            "a local ollama/ model.")

    secret_refs = {}
    for item in secrets:
        if "=" not in item:
            fail(f"--secret must be NAME=DESCRIPTION, got {item!r}")
        key, _, description = item.partition("=")
        secret_refs[key] = description

    request = synth_mod.SynthRequest(
        name=name,
        brd=Path(brd_path).read_text(encoding="utf-8"),
        source_schema=Path(schema_path).read_text(encoding="utf-8"),
        warehouse=dict(_STANDARD_WAREHOUSE_REFS, schema=warehouse_schema or name),
        secret_refs=secret_refs,
        hint=hint,
    )
    try:
        result = synth_mod.synth(request, overwrite=overwrite)
    except (FileExistsError, ValueError, llm.LLMError) as exc:
        fail(str(exc))

    if result.blocked:
        lines = []
        for b in result.blockers:
            lines.append(f"[bold]?[/bold] {b.question}")
            if b.why_it_matters:
                lines.append(f"  [dim]{b.why_it_matters}[/dim]")
        console.print(Panel("\n".join(lines),
                            title="cannot draft yet - needs clarification",
                            border_style="red", expand=False))
        sys.exit(1)

    console.print(Panel("\n".join(f"  · {f}" for f in result.files),
                        title=f"drafted {name} ({len(result.files)} file(s)) - {result.root}",
                        border_style="cyan", expand=False))
    if result.mapping:
        console.print(Panel(result.mapping, title="BRD -> implementation mapping",
                            border_style="dim", expand=False))
    if result.load_error:
        console.print(f"[red]structural validation failed:[/red] {result.load_error}\n"
                     f"Fix it by hand, then re-check with `dpagent pipeline lint {name}`.")
    else:
        console.print("[green]structurally valid[/green] - loads and lints clean.")
    if result.validation is not None:
        _print_validation_steps(result.validation)
    if result.notes:
        console.print(f"[dim]model notes: {result.notes}[/dim]")
    console.print(f"\n[yellow]This is a draft, maturity: draft.[/yellow] Read every "
                 f"file under {result.root} before `dpagent pipeline promote {name}` - "
                 f"deploy refuses it until then.")


@pipeline_group.command("validate")
@click.argument("name")
@click.option("--fixture", "fixture_path", type=click.Path(exists=True, dir_okay=False),
              help="Steps 4-5 too: seed this fixture (YAML) into a throwaway source, "
                   "deploy --allow-draft an isolated, uniquely-named clone of this "
                   "pipeline (never the real pipeline's own artifacts/secrets), run it "
                   "twice through a real Airflow, compare the real curated output "
                   "against --expected, then undeploy the clone. Needs root (deploy's "
                   "own Airflow-facing steps) and passwordless sudo to postgres twice "
                   "over - reported as unavailable (exit 2), not failed, when either is "
                   "missing. Exit codes: 0 pass, 1 mismatch/failed run, 2 unavailable, "
                   "3 timeout, 4 data matched but cleanup did not complete.")
@click.option("--expected", "expected_path", type=click.Path(exists=True, dir_okay=False),
              help="The independently-authored expected result (YAML) --fixture's real "
                   "curated output is compared against. Required together with --fixture.")
def validate_cmd(name, fixture_path, expected_path):
    """Step 3 (always): does the SQL this pipeline references actually
    compile/apply for real, in isolation - never the real shared dbt
    project or the warehouse a promoted pipeline would use.

    Steps 4-5 (with --fixture/--expected): does the pipeline's *output*
    match an independently-authored expected result, run twice to catch a
    non-idempotent transform - the check that actually proves the numbers
    are right, not just that the manifest is well-formed.

    Works on any pipeline, hand-written or drafted by `dpagent pipeline
    synth` (which already runs step 3 once itself, right after drafting) -
    useful to re-check after editing a draft by hand. Writes/overwrites
    `.synth-validation.yaml` next to the pipeline - a report for a
    reviewer, never part of what gets promoted or executed.
    """
    if bool(fixture_path) != bool(expected_path):
        fail("--fixture and --expected must be given together")

    pipeline = _load_or_fail(name)
    report = validate_mod.validate_pipeline(pipeline, generator="dpagent pipeline validate")
    _print_validation_steps(report)
    path = pipeline.path(validate_mod.VALIDATION_REPORT_FILENAME)
    console.print(f"\n[dim]written: {path}[/dim]")
    step3_ok = ((report.dbt is None or report.dbt.ok)
               and (report.procedures is None or report.procedures.ok)
               and (report.dbt_dependencies is None or report.dbt_dependencies.ok))

    if not fixture_path:
        if not step3_ok:
            sys.exit(1)
        return

    if not step3_ok:
        fail("step 3 failed - fix that before running the fixture (steps 4-5 would only "
             "confirm the same broken SQL against real data)")

    fx = fixture_mod.load_fixture(Path(fixture_path))
    expected = fixture_mod.load_expected(Path(expected_path))
    console.print(f"\n[bold]running fixture through a real, --allow-draft deploy of an "
                 f"isolated validation clone (2 runs, for idempotency)...[/bold]")
    result = fixture_mod.run_fixture(pipeline, fx, expected)
    if result.clone_name:
        console.print(f"[dim]validation clone: {result.clone_name}[/dim]")

    # Merge steps 4-5 into the same report step 3 already wrote, hashed
    # against the exact pipeline/fixture/expected content this run used -
    # the report is visibly stale the moment any of the three changes.
    fixture_section = fixture_mod.fixture_report_dict(
        result,
        pipeline_hash=report.content_hash,
        fixture_hash=fixture_mod.hash_file(Path(fixture_path)),
        expected_hash=fixture_mod.hash_file(Path(expected_path)),
    )
    report.fixture = fixture_section
    validate_mod.write_validation_report(pipeline.root, report)

    # Comparisons and cleanup are always printed *before* any exit path
    # below, unconditionally - an earlier version exited (unavailable_reason
    # or seed_error) before ever reaching this, which hid a real cleanup
    # outcome whenever unavailable_reason was set *after* deploy() already
    # ran (e.g. "could not unpause validation DAG"): cleanup_attempted/
    # cleanup_ok/the throwaway drop flags were already correct on the
    # result by then, just never printed (M2.4.3 review: "CLI thoát sớm khi
    # unavailable_reason hoặc seed lỗi, trước phần in cleanup").
    for label, comparison in (("run 1", result.comparison_after_run1),
                              ("run 2", result.comparison_after_run2)):
        if comparison is None:
            continue
        colour = "green" if comparison.ok else "red"
        console.print(f"[bold]{label} vs expected:[/bold] "
                     f"[{colour}]{'match' if comparison.ok else 'mismatch'}[/{colour}]"
                     + (f" - {comparison.detail}" if comparison.detail else ""))

    if result.cleanup_attempted:
        colour = "green" if result.cleanup_ok else "red"
        console.print(f"[bold]cleanup (pipeline artifacts):[/bold] [{colour}]"
                     f"{'complete' if result.cleanup_ok else 'FAILED'}[/{colour}]"
                     f" - {result.cleanup_detail}")
    # Gated on whether each throwaway database was actually *created*, not
    # on cleanup_attempted (pipeline-artifact cleanup, a different thing
    # that never even starts on a seed failure) - a seed failure still
    # creates and tears down both throwaway databases, and that real
    # outcome must still be visible here (same M2.4.3 finding as above).
    for label, created, dropped, error in (
        ("source database", result.source_db_created, result.source_database_dropped,
         result.source_database_drop_error),
        ("source role", result.source_db_created, result.source_role_dropped,
         result.source_role_drop_error),
        ("warehouse database", result.warehouse_db_created, result.warehouse_database_dropped,
         result.warehouse_database_drop_error),
        ("warehouse role", result.warehouse_db_created, result.warehouse_role_dropped,
         result.warehouse_role_drop_error),
    ):
        if not created:
            continue
        colour = "green" if dropped else "red"
        console.print(f"[bold]cleanup ({label}):[/bold] [{colour}]"
                     f"{'dropped' if dropped else 'FAILED'}[/{colour}]"
                     + (f" - {error}" if error and not dropped else ""))

    sd = result.source_down
    if result.bronze_staging and sd is not None:
        colour = "green" if sd.ok else "red"
        console.print(f"[bold]bronze, source removed:[/bold] [{colour}]"
                     f"{'LOAD succeeded with the source dropped' if sd.ok else 'FAILED'}[/{colour}]"
                     f" - batch {sd.batch_id or '-'}; source dropped={sd.source_dropped}, "
                     f"unreachable={sd.source_unreachable}, loaded={sd.loaded}, landing matches "
                     f"fixture={sd.landing_matches_fixture}" + (f" - {sd.error}" if sd.error else ""))
    if result.bronze_staging and result.cleanup_attempted:
        colour = "green" if result.s3_cleanup_ok else "red"
        console.print(f"[bold]cleanup (S3 namespace {result.bronze_namespace}/):[/bold] "
                     f"[{colour}]{'purged and verified empty' if result.s3_cleanup_ok else 'FAILED'}"
                     f"[/{colour}] - found {result.s3_found}, remaining {result.s3_remaining}"
                     + (f" - {result.s3_error}" if result.s3_error else ""))

    if result.unavailable_reason:
        console.print(f"[yellow]steps 4-5 unavailable:[/yellow] {result.unavailable_reason}")
        if not result.clone_name:
            # Only the preflight branch (run_fixture's very first action,
            # before make_validation_clone is even called) can actually
            # back this claim - every other unavailable_reason fires after
            # some real side effect already happened, whose own outcome is
            # exactly what was just printed above (M2.4.3 review: "Giới
            # hạn cam kết zero-mutation đúng nhánh preflight").
            console.print("[dim](preflight failed before anything was created - no "
                          "database/role, no seed, no deployed artifact)[/dim]")
        else:
            console.print("[dim](its absence says nothing about the pipeline's own "
                          "correctness - it means this operator/host could not finish "
                          "running it; see the cleanup lines above for what, if "
                          "anything, was already created and whether it was torn "
                          "down)[/dim]")
        sys.exit(2)

    if result.deploy_error:
        console.print("fixture deploy failed: "
                      + result.deploy_error, markup=False)
        sys.exit(1)

    if result.seed_error:
        # A validation failure, not "unavailable": the fixture itself (or
        # the throwaway database) rejected it - exit 1, same as a real
        # mismatch, never exit 2 (which means "could not even attempt it").
        console.print(f"[red]fixture seed failed:[/red] {result.seed_error}")
        sys.exit(1)

    data_ok = (result.seeded and result.deployed
              and result.run1_status == "ok" and result.run2_status == "ok"
              and result.idempotent and result.source_down_ok)

    if result.run1_status == "timeout" or result.run2_status == "timeout":
        console.print(f"\n[red]fixture run timed out[/red] "
                      f"(run1={result.run1_status!r}, run2={result.run2_status!r})")
        sys.exit(3)

    if not data_ok:
        console.print(f"\n[red]fixture run failed[/red] "
                      f"(run1={result.run1_status!r}, run2={result.run2_status!r})")
        sys.exit(1)

    if not (result.cleanup_attempted and result.cleanup_ok and result.throwaway_cleanup_ok
            and result.s3_cleanup_ok):
        console.print(f"\n[red]fixture data matched, but cleanup did not complete[/red] - "
                      f"a passing comparison does not count as done until cleanup (pipeline "
                      f"artifacts, both throwaway databases AND the S3 namespace) does too. Check by hand: "
                      f"dpagent pipeline undeploy {result.clone_name}")
        sys.exit(4)

    console.print("\n[green]fixture run: both runs matched expected, idempotent, "
                  "cleanup complete (pipeline artifacts + both throwaway databases)[/green]")


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


@pipeline_group.command("promote")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
@click.option("--approved-by", default="",
              help="Who is approving this (defaults to the OS user running the command).")
def promote_cmd(name, yes, approved_by):
    """Mark this pipeline's current manifest/procedures/models as reviewed.

    `deploy()` refuses to apply an unreviewed pipeline for real
    (docs/layer2.md, "Authoring pipelines with a model") - this is what
    actually clears that: it hashes the manifest plus every procedure/dbt
    model it references right now, records that hash next to the pipeline
    (`.approved.yaml`, git-tracked - review it in a PR like anything else
    here), and sets `maturity: reviewed`. Editing any of those files again
    afterward - even without touching `maturity` - invalidates this and
    `deploy` refuses again, until promote runs once more.
    """
    pipeline = _load_or_fail(name)
    approved, reason = approval_mod.is_approved(pipeline)
    if approved:
        console.print(f"[dim]{name} is already reviewed and matches its approval[/dim]")
        return

    paths = approval_mod.hashed_paths(pipeline)
    console.print(Panel(
        "\n".join(f"  · {p}" for p in paths),
        title=f"approving {name} means having read every line of these {len(paths)} file(s)",
        border_style="yellow", expand=False))
    if not pipeline.is_draft:
        console.print(f"[yellow]note:[/yellow] {reason}")

    if not yes and not confirm(f"Have you read every line above for {name!r}?",
                               default=False):
        sys.exit(1)

    approver = approved_by or getpass.getuser()
    approval = approval_mod.promote(pipeline, approver)
    console.print(f"[green]{name} is now maturity: reviewed[/green] "
                 f"(approved by {approval.approved_by!r} at {approval.approved_at}) - "
                 f"`dpagent pipeline deploy {name}` will apply it for real.")


@pipeline_group.command("list")
def list_cmd():
    """Every pipeline: its connector, whether it is deployed, and its last run.

    Covers the manifests in this checkout plus any pipeline still deployed
    whose manifest is no longer here (removable with `undeploy`).
    """
    deployed = set(deploy_mod.deployed_names())
    table = Table(box=None)
    for column in ("pipeline", "connector", "maturity", "schedule", "deployed", "last run"):
        table.add_column(column, style="bold" if column == "pipeline" else "")

    def last_run(name):
        run = state.latest_run(kind="data", target=name)
        if not run:
            return "[dim]never[/dim]"
        colour = {"ok": "green", "failed": "red"}.get(run["status"], "yellow")
        return f"[{colour}]{run['status']}[/{colour}] [dim]#{run['id']} {run['started_at']}[/dim]"

    def maturity_cell(p):
        approved, _ = approval_mod.is_approved(p)
        if approved:
            return "[green]reviewed[/green]"
        return "[yellow]draft[/yellow]" if p.is_draft else "[yellow]stale[/yellow]"

    in_checkout = pipelines_mod.available()
    for name in in_checkout:
        yes_no = "[green]yes[/green]" if name in deployed else "[dim]no[/dim]"
        try:
            p = pipelines_mod.load(name)
        except pipelines_mod.PipelineError as exc:
            table.add_row(name, "[red]invalid[/red]", "", str(exc).split(": ")[-1][:40],
                          yes_no, last_run(name))
            continue
        table.add_row(name, p.source.connector, maturity_cell(p),
                      p.schedule or "[dim]manual[/dim]", yes_no, last_run(name))
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

    console.print(f"[bold]schedule:[/bold] "
                  + (f"{pipeline.schedule} [dim](UTC; deploy unpauses the DAG, so it starts running "
                     f"on this schedule)[/dim]" if pipeline.schedule
                     else "manual-only [dim](runs only on `dpagent pipeline run`)[/dim]"))

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
@click.option("--allow-draft", is_flag=True,
              help="Deploy a pipeline that has not been promoted (or whose approved "
                   "content has since changed) - manual-only regardless of its own "
                   "schedule:, and never unpaused. For proving a draft on a real host "
                   "before anyone has reviewed it, never for production use.")
def deploy_cmd(name, yes, no_db, no_airflow, allow_draft):
    """Write the DAG + dbt schema, apply procedures, publish dbt models, install the DAG.

    Refuses a pipeline that is `maturity: draft`, or whose approved content
    no longer matches what is on disk, unless --allow-draft is given
    explicitly (docs/layer2.md, "Authoring pipelines with a model") -
    `dpagent pipeline promote NAME` is the real fix. Writing files under
    pipelines/<name>/build/ is always safe to repeat (each run overwrites
    the last), but this refusal happens before even that: --allow-draft is
    the only way past it, not --yes.

    The rest happens against a real system, so it asks first unless --yes:
    applying a procedure runs `CREATE OR REPLACE PROCEDURE` against
    `warehouse:` (idempotent by construction) and a dbt-engine stage's
    models are copied into the dbt pack's own real project (needs root;
    `dbt run` only ever looks inside its own project, never at this
    pipeline's directory) - both under --no-db; and installing the DAG
    copies it into Airflow's DAGS_FOLDER as the airflow OS user (needs
    root) so `dpagent pipeline run` has something real to trigger - under
    --no-airflow.
    """
    pipeline = _load_or_fail(name)
    _require_layer2_prerequisites(pipeline)

    approved, reason = approval_mod.is_approved(pipeline)
    if not approved and not allow_draft:
        fail(f"{reason}\n\nEither `dpagent pipeline promote {name}` it, or pass "
             f"--allow-draft to test it explicitly (manual-only, never unpaused).")
    if not approved:
        console.print(f"[yellow]--allow-draft:[/yellow] {reason} - deploying anyway, "
                      f"manual-only, DAG will stay paused")

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
                                   install_dag_to_airflow=not no_airflow,
                                   allow_draft=allow_draft)
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
        if result.dag_paused_for_draft:
            console.print(
                "[yellow]DAG left paused on purpose[/yellow] - this is an "
                "--allow-draft deploy of an unreviewed pipeline, and it stays "
                "manual-only until it is promoted and deployed again. Do NOT "
                f"unpause it by hand. `dpagent pipeline run {name}` still works "
                "for a manual test.")
        elif result.dag_unpaused:
            console.print(
                f"[bold]DAG unpaused:[/bold] runs on schedule {pipeline.schedule!r} (UTC)"
                if pipeline.schedule else
                "[bold]DAG unpaused:[/bold] a run can start (manual-only DAG)")
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
    if result.runs_cancelled:
        console.print(f"  [yellow]cancelled[/yellow] run(s) still marked running: "
                      + ", ".join(f"#{r}" for r in result.runs_cancelled))
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
@click.option("--full-refresh", "full_refresh", is_flag=True,
              help="Reload every table from scratch, dropping the landing tables and the "
                   "saved incremental cursors of an incremental source (a backfill, or "
                   "after the source's schema changed).")
@click.option("--wait", is_flag=True,
              help="Block until the run reaches a terminal state; exit 0 if ok, "
                   "1 if failed, 3 if --timeout passes first.")
@click.option("--timeout", type=int, default=1800, show_default=True,
              help="Seconds --wait gives up after (the run itself keeps going).")
def run_cmd(name, yes, full_refresh, wait, timeout):
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
            f"Trigger the deployed {name!r} DAG now (runs as the airflow OS user)"
            + (" as a FULL REFRESH - landing tables and incremental cursors are dropped "
               "and everything is reloaded?" if full_refresh else "?"),
            default=not full_refresh):
        sys.exit(1)

    run_id = state.start_run("data", name)
    state.event("pipeline.trigger", f"triggering Airflow DAG {name!r}"
                + (" (full refresh)" if full_refresh else ""),
                run_id=run_id, actor="user")

    result = (deploy_mod.trigger_dag(name, run_id, full_refresh=True) if full_refresh
              else deploy_mod.trigger_dag(name, run_id))
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


@pipeline_group.command("bronze-extract")
@click.argument("name")
def bronze_extract_cmd(name):
    """Run a bronze_staging pipeline's EXTRACT by hand: one full snapshot of
    its source table into object storage. Prints the new batch id - the only
    thing `bronze-load` needs."""
    from ..pipelines import bronze as bronze_mod
    pipeline = _load_or_fail(name)
    if not pipeline.bronze_staging:
        fail(f"{name!r} does not use bronze_staging (docs/hg-bronze-staging.md)")
    try:
        batch_id = bronze_mod.run_extract(pipeline_name=name)
    except bronze_mod.BronzeFailed as exc:
        fail(str(exc))
    console.print(f"extracted batch [bold]{batch_id}[/bold]")
    console.print(f"[dim]load it: dpagent pipeline bronze-load {name} --batch {batch_id}[/dim]")


@pipeline_group.command("bronze-load")
@click.argument("name")
@click.option("--batch", "batch_id", required=True, help="Batch id printed by bronze-extract.")
def bronze_load_cmd(name, batch_id):
    """Run a bronze_staging pipeline's LOAD by hand for one batch. Uses only
    the warehouse and the object store - never the source - so it works
    with the source gone. Loading an already-loaded batch changes nothing."""
    from ..pipelines import bronze as bronze_mod
    pipeline = _load_or_fail(name)
    if not pipeline.bronze_staging:
        fail(f"{name!r} does not use bronze_staging (docs/hg-bronze-staging.md)")
    try:
        result = bronze_mod.run_load(pipeline_name=name, batch_id=batch_id)
    except bronze_mod.BronzeFailed as exc:
        fail(str(exc))
    if result["outcome"] == "already_loaded":
        console.print(f"batch {batch_id} was already loaded - nothing changed")
    else:
        console.print(f"loaded batch {batch_id}: {result['rows']} row(s) from "
                      f"{result['objects']} object(s) into {result['table']}")


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


@pipeline_group.command("prune")
@click.argument("name", required=False)
@click.option("--older-than-days", type=int, required=True,
              help="Delete finished runs whose finished_at is older than this many days.")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted, delete nothing.")
@click.option("--yes", "-y", is_flag=True)
def prune_cmd(name, older_than_days, dry_run, yes):
    """Delete old pipeline runs from dpagent's own journal.

    `status`/`audit` history (docs/layer2.md, "Run ledger") is otherwise kept
    forever, on purpose - a schedule ticking every 2 minutes leaves nothing to
    ever bound its own growth (207 runs in 7 hours, in one real soak). This is
    the one place that journal is allowed to shrink, and only because an
    operator explicitly asked: it deletes runs and their stage/gate verdicts
    and events, same as `undeploy` deliberately never does on its own.

    NAME limits this to one pipeline; omitted, it applies across every
    pipeline's history, deployed or not. A `running` run is never a
    candidate, regardless of age.

    `--dry-run` needs no privilege (the journal is world-readable). Actually
    deleting does - the database is owned root:dpagent - so this needs
    sudo, same as deploy/undeploy.
    """
    if older_than_days < 1:
        fail("--older-than-days must be at least 1")
    if name is not None:
        _load_or_fail(name)

    counts = state.prune_data_runs(older_than_days, target=name, dry_run=True)
    if counts["runs"] == 0:
        console.print(f"[dim]nothing older than {older_than_days} day(s)"
                      + (f" for {name!r}" if name else "") + "[/dim]")
        return

    scope = f"{name!r}" if name else "every pipeline"
    console.print(f"would delete {counts['runs']} run(s) for {scope} older than "
                  f"{older_than_days} day(s): {counts['stage_runs']} stage result(s), "
                  f"{counts['gate_runs']} gate result(s), {counts['events']} event(s)")
    if dry_run:
        return
    if not yes and not confirm("Delete these permanently? This cannot be undone.",
                                default=False):
        console.print("[yellow]nothing deleted[/yellow]")
        sys.exit(1)

    # Real gap found running this for real on the production journal: the
    # dry-run's SELECTs succeeded for any user (world-readable), but the
    # actual DELETE needs write access to a database file owned root:dpagent
    # - the group Airflow's own tasks run as, not necessarily the operator
    # typing this command. Caught here with an actionable message instead of
    # a raw sqlite3.OperationalError("attempt to write a readonly database").
    if hasattr(os, "access") and not os.access(state.DB_PATH, os.W_OK):
        fail(f"no write access to {state.DB_PATH} - re-run as "
             f"sudo -E dpagent pipeline prune ...")

    state.prune_data_runs(older_than_days, target=name, dry_run=False)
    console.print(f"[green]deleted[/green] {counts['runs']} run(s)")
