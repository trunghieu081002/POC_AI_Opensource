"""A single host-wide lock around anything that actually changes system state.

Nothing else in this project takes any kind of lock: two `dpagent` processes
racing against the same host - two operators, two CI jobs, a cron re-run
overlapping a manual one - can restart the same service or step on each
other's work with no warning. Confirmed for real (docs/deploy-log.md,
2026-09-15): two genuinely concurrent `dpagent install postgres --force`
runs both proceeded past every check, and one's acceptance suite got
"FATAL: the database system is shutting down" mid-check from the other's
step restarting the same unit.

Scoped per-host, not per-pack: a lock this coarse costs almost nothing in
practice (install operations are infrequent, and dpagent already resolves a
multi-pack request into one `Engine.install()` run, so the only thing this
actually serialises is two *separate* invocations) - and a per-pack lock
still would not protect two different packs stepping on a shared resource
(the rpm/dpkg database, a port, a systemd unit another pack's step also
happens to touch).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Callable

from .executor import LOG_DIR

LOCK_PATH = LOG_DIR / "dpagent.lock"
DEFAULT_TIMEOUT = 300.0  # seconds - long enough for a slow mirror, not forever
POLL_INTERVAL = 1.0


class HostLockTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def host_lock(*, timeout: float = DEFAULT_TIMEOUT,
              on_wait: Callable[[], None] | None = None):
    """Block real work until any other dpagent operation on this host finishes.

    Raises HostLockTimeout if still held after `timeout` seconds - a lock
    held by a process that crashed without releasing it (the OS releases a
    flock automatically when the holding process exits or dies, so this
    only happens if something is still genuinely running) must never hang a
    caller forever with no way out.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if on_wait:
                on_wait()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc2:
                    if exc2.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise HostLockTimeout(
                            f"another dpagent operation is still running after "
                            f"{timeout:.0f}s (lock held on {LOCK_PATH}). Wait for "
                            f"it to finish, or investigate a stuck process if none "
                            f"is actually running.")
                    time.sleep(POLL_INTERVAL)

        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()
