"""Refuse a benchmark scale the machine cannot hold, before anything is built or generated.

  preflight.py SCALE --disk-path PATH [--config config.yaml]

Prints one JSON object (free disk at PATH, MemAvailable, the scale's minimums, ok); exit 0 if the scale fits,
4 if it does not. Reads only: /proc/meminfo and statvfs."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
GB = 1024 ** 3


def mem_available_bytes(meminfo: str = "/proc/meminfo") -> int:
    for line in Path(meminfo).read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("MemAvailable not found in " + meminfo)


def decide(scale: dict, free_disk: int, mem_available: int) -> dict:
    need_disk, need_ram = scale["min_free_disk_gb"] * GB, scale["min_available_ram_gb"] * GB
    reasons = []
    if free_disk < need_disk:
        reasons.append(f"free disk {free_disk / GB:.1f} GB < {scale['min_free_disk_gb']} GB")
    if mem_available < need_ram:
        reasons.append(f"available memory {mem_available / GB:.1f} GB < {scale['min_available_ram_gb']} GB")
    return {"ok": not reasons, "reasons": reasons, "free_disk_gb": round(free_disk / GB, 1),
            "mem_available_gb": round(mem_available / GB, 1), "needs": {k: scale[k] for k in
            ("rows", "min_free_disk_gb", "min_available_ram_gb", "timeout_s")}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("scale")
    ap.add_argument("--disk-path", required=True)
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    a = ap.parse_args(argv)
    config = yaml.safe_load(Path(a.config).read_text())
    if a.scale not in config["scales"]:
        ap.error(f"unknown scale {a.scale!r}: {sorted(config['scales'])}")
    st = os.statvfs(a.disk_path)
    result = dict(decide(config["scales"][a.scale], st.f_bavail * st.f_frsize, mem_available_bytes()),
                  scale=a.scale, disk_path=a.disk_path, label=config["label"])
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 4)


if __name__ == "__main__":
    main()
