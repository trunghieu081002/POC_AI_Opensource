"""Read the machine's real state — memory, disk, ports, commands — without
shelling out to tools a fresh host might not have yet.

This backs `dpagent doctor`, which has to work *before* the `base` pack has
installed anything. Port checking in particular binds a real socket rather than
parsing `ss` output, so it needs nothing beyond the Python standard library.
"""
from __future__ import annotations

import shutil
import socket
from pathlib import Path


def _cgroup_memory_limit_mb(root: Path) -> int | None:
    """The container's own memory ceiling, if this process is confined by one.

    ``/proc/meminfo``'s ``MemTotal`` is never cgroup-aware in mainline Linux —
    it always reports the physical host's RAM, regardless of a container's
    ``--memory`` limit or a Kubernetes pod's resource limit. Every runtime
    that needs an accurate figure inside a container (the JVM, Node.js, ...)
    reads the cgroup limit file directly instead; do the same here so
    `dpagent doctor` does not wave through a host that will OOM-kill the
    pack it just approved.
    """
    v2 = root / "sys/fs/cgroup/memory.max"
    if v2.exists():
        try:
            raw = v2.read_text().strip()
        except OSError:
            return None
        if raw == "max":
            return None
        try:
            return int(raw) // (1024 * 1024)
        except ValueError:
            return None

    v1 = root / "sys/fs/cgroup/memory/memory.limit_in_bytes"
    if v1.exists():
        try:
            raw = int(v1.read_text().strip())
        except (OSError, ValueError):
            return None
        # v1's "unlimited" sentinel is the largest page-aligned value below
        # INT64_MAX, not a round number — anything above ~4PB is not a real
        # limit anyone set.
        if raw >= (1 << 62):
            return None
        return raw // (1024 * 1024)

    return None


def memory_mb(root: Path | str = "/") -> int | None:
    root = Path(root)
    try:
        text = (root / "proc/meminfo").read_text()
    except OSError:
        return None
    total_mb = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            kb = int(line.split()[1])
            total_mb = kb // 1024
            break
    if total_mb is None:
        return None
    cgroup_mb = _cgroup_memory_limit_mb(root)
    if cgroup_mb is not None and cgroup_mb < total_mb:
        return cgroup_mb
    return total_mb


def disk_free_mb(path: str) -> int | None:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free // (1024 * 1024)
    except OSError:
        return None


def port_free(port: int, host: str = "0.0.0.0") -> bool:
    """True if nothing is listening — checked by trying to bind, not by
    parsing another tool's output, so it works with nothing else installed."""
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host if family == socket.AF_INET else "::", port))
        except OSError:
            return False
        except (socket.gaierror, OverflowError):
            continue
    return True


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None
