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


def memory_mb() -> int | None:
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            kb = int(line.split()[1])
            return kb // 1024
    return None


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
