"""`dpagent backup create|verify|restore` (src/dpagent/ops/backup.py, docs/operations.md)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import click

from dpagent.ops import backup as bk


@click.group("backup")
def backup_group():
    """Back up / restore DPAgent's state on this host (journal, approvals, Airflow/Superset metadata, warehouse)."""


def _root():
    if os.geteuid() != 0:
        raise click.ClickException("run as root (the backup reads and the restore writes service files and databases)")


@backup_group.command("create")
@click.argument("out_dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--warehouse", default="warehouse", show_default=True)
@click.option("--schema", "schemas", multiple=True, help="Only these warehouse schemas (default: the whole warehouse).")
@click.option("--passphrase-env", default="", help="Encrypt with the passphrase in this environment variable.")
def create_cmd(out_dir, warehouse, schemas, passphrase_env):
    """Write OUT_DIR/dpagent-backup-<utc>.tar[.enc] (0600; it contains secrets) and its manifest."""
    _root()
    try:
        m = bk.create(out_dir, warehouse=warehouse, schemas=list(schemas) or None, passphrase_env=passphrase_env)
    except bk.BackupError as exc:
        raise click.ClickException(str(exc))
    click.echo(json.dumps({k: m[k] for k in ("archive", "archive_sha256", "databases", "timings", "encrypted")}, indent=1))
    if not passphrase_env:
        click.echo("NOTE: not encrypted - the archive holds secrets; keep it 0600 and off shared storage", err=True)


@backup_group.command("verify")
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--passphrase-env", default="")
def verify_cmd(archive, passphrase_env):
    """Check every member of ARCHIVE against its manifest."""
    try:
        click.echo(json.dumps(bk.verify(archive, passphrase_env), indent=1))
    except bk.BackupError as exc:
        raise click.ClickException(str(exc))


@backup_group.command("restore")
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--passphrase-env", default="")
@click.option("--yes", is_flag=True, help="Required: the restore replaces the databases and files in scope.")
def restore_cmd(archive, passphrase_env, yes):
    """Replace this host's DPAgent state with ARCHIVE's (same release and packs installed). Stops and starts the services."""
    _root()
    if not yes:
        raise click.ClickException("the restore replaces Airflow/Superset metadata, the warehouse and DPAgent's journal "
                                   "- re-run with --yes")
    try:
        click.echo(json.dumps(bk.restore(archive, passphrase_env=passphrase_env), indent=1))
    except bk.BackupError as exc:
        raise click.ClickException(str(exc))
