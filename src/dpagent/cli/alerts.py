"""`dpagent alerts check|install-timer` (src/dpagent/ops/alerts.py, docs/operations.md)."""
from __future__ import annotations

import json
import os
import shlex

import click

from dpagent.ops import alerts as al


@click.group("alerts")
def alerts_group():
    """Failed runs, failed gates (bad data), stuck runs, low disk/memory - from what DPAgent records."""


@alerts_group.command("check")
@click.option("--min-free-gb", type=float, default=5.0, show_default=True)
@click.option("--min-avail-gb", type=float, default=1.0, show_default=True)
@click.option("--stuck-hours", type=float, default=6.0, show_default=True)
@click.option("--notify-command", default="", help="Run this (shell) with the alerts as JSON on stdin, if any.")
@click.option("--dry-run", is_flag=True, help="Report only: no journal event, the watermark is not moved.")
def check_cmd(min_free_gb, min_avail_gb, stuck_hours, notify_command, dry_run):
    """Print the alerts since the previous check as JSON; exit 1 when there is any (2 if the notifier failed)."""
    found = al.check(min_free_gb=min_free_gb, min_avail_gb=min_avail_gb, stuck_hours=stuck_hours, record=not dry_run)
    click.echo(json.dumps({"alerts": found}, indent=1))
    if found and notify_command:
        rc = al.notify(found, notify_command)
        if rc != 0:
            click.echo(f"the notify command exited {rc}", err=True)
            raise SystemExit(2)
    raise SystemExit(1 if found else 0)


@alerts_group.command("install-timer")
@click.option("--every-minutes", type=int, default=5, show_default=True)
@click.option("--notify-command", default="")
@click.option("--min-free-gb", type=float, default=5.0, show_default=True)
@click.option("--min-avail-gb", type=float, default=1.0, show_default=True)
def install_timer_cmd(every_minutes, notify_command, min_free_gb, min_avail_gb):
    """Install and start a systemd timer that runs `dpagent alerts check` every N minutes (root)."""
    if os.geteuid() != 0:
        raise click.ClickException("run as root")
    args = f"--min-free-gb {min_free_gb} --min-avail-gb {min_avail_gb}"
    if notify_command:
        args += f" --notify-command {shlex.quote(notify_command)}"
    for p in al.install_timer(every_minutes, args):
        click.echo(f"wrote {p}")
