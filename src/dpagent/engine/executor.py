"""Run a pack script (or a fix command) as a subprocess, with a JSONL log per run.

The engine never builds shell strings out of model output. It executes files that
live in `packs/`, which a human reviewed before they were committed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import params as params_mod
from ..library import loader as packs

if sys.platform == "win32":
    DEFAULT_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "dpagent" / "logs"
else:
    DEFAULT_LOG_DIR = Path("/var/log/dpagent")

LOG_DIR = Path(os.environ.get("DPAGENT_LOG_DIR", DEFAULT_LOG_DIR))

MAX_CAPTURE = 64_000  # keep the tail of very chatty installs, not the whole thing


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    log_path: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def output(self) -> str:
        """Both streams together — what the error catalog matches against."""
        return f"{self.stdout}\n{self.stderr}".strip()


def _tail(text: str, limit: int = MAX_CAPTURE) -> str:
    if len(text) <= limit:
        return text
    return f"...[{len(text) - limit} bytes trimmed]...\n" + text[-limit:]


def _log_line(run_id: str, record: dict) -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{run_id}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return str(path)


def build_env(os_env: dict, resolved_params: dict, dry_run: bool,
              extra: dict | None = None) -> dict:
    """Environment handed to a step script.

    Params arrive as DP_PARAM_<UPPER>; anything non-scalar is JSON so bash can
    hand it to python3 -c and stay readable.
    """
    env = os.environ.copy()
    env.update(os_env)
    env["DP_DRY_RUN"] = "1" if dry_run else "0"
    env["DP_LIB"] = str(packs.PACKS_DIR / "_lib" / "dp.sh")
    # Suites need it to reuse a pack's helpers (e.g. postgres/pg-lib.sh) rather
    # than restating how to reach the component under test.
    env["DP_PACKS_DIR"] = str(packs.PACKS_DIR)
    for key, value in resolved_params.items():
        name = "DP_PARAM_" + key.upper().replace("-", "_")
        if isinstance(value, (dict, list)):
            env[name] = json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(value, bool):
            env[name] = "1" if value else "0"
        elif value is None:
            env[name] = ""
        else:
            env[name] = str(value)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def run_script(script: Path, env: dict, run_id: str, timeout: int = 600,
               cwd: Path | None = None) -> Result:
    """Execute one script file under bash. Never raises on a non-zero exit."""
    started = time.monotonic()
    timed_out = False

    if not script.exists():
        return Result(rc=127, stdout="", stderr=f"script not found: {script}",
                      duration_ms=0)

    try:
        proc = subprocess.run(
            ["bash", str(script)],
            env=env,
            cwd=str(cwd) if cwd else str(script.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = 124
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        err += f"\n[dpagent] timed out after {timeout}s"
    except FileNotFoundError:
        return Result(rc=127, stdout="",
                      stderr="bash not found — dpagent needs bash on the target host",
                      duration_ms=int((time.monotonic() - started) * 1000))

    duration_ms = int((time.monotonic() - started) * 1000)
    out, err = _tail(out), _tail(err)

    log_path = _log_line(run_id, {
        "ts": time.time(),
        "type": "script",
        "script": str(script),
        "rc": rc,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "dry_run": env.get("DP_DRY_RUN") == "1",
        "stdout": params_mod.redact(out),
        "stderr": params_mod.redact(err),
    })

    return Result(rc=rc, stdout=out, stderr=err, duration_ms=duration_ms,
                  timed_out=timed_out, log_path=log_path)


def run_command(command: str, env: dict, run_id: str, timeout: int = 300,
                dry_run: bool = False) -> Result:
    """Execute a single autofix command string.

    Only reached after safety.check() cleared it. Kept separate from run_script
    so the audit log can tell a reviewed pack step apart from a repair action.
    """
    if dry_run:
        _log_line(run_id, {"ts": time.time(), "type": "fix.dry", "cmd": command})
        return Result(rc=0, stdout=f"DRY: {command}", stderr="", duration_ms=0)

    started = time.monotonic()
    try:
        proc = subprocess.run(["bash", "-c", command], env=env, capture_output=True,
                              text=True, timeout=timeout, errors="replace")
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", f"[dpagent] fix timed out after {timeout}s"

    duration_ms = int((time.monotonic() - started) * 1000)
    log_path = _log_line(run_id, {
        "ts": time.time(),
        "type": "fix",
        "cmd": command,
        "rc": rc,
        "duration_ms": duration_ms,
        "stdout": params_mod.redact(_tail(out, 8000)),
        "stderr": params_mod.redact(_tail(err, 8000)),
    })
    return Result(rc=rc, stdout=out, stderr=err, duration_ms=duration_ms,
                  log_path=log_path)
