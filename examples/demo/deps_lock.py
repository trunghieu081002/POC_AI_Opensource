"""Bring the demo's Python environments to the exact versions of the accepted demo run, and check them.

  deps_lock.py check   every package installed in each locked venv has its locked version (exit 1 if not)
  deps_lock.py apply   install the locked version of every installed package that differs (--no-deps, so nothing
                       else is resolved anew), then `pip check` and `check`

One lock per venv (examples/demo/lock/<name>.txt, generated from the accepted run's dependency-versions.txt): the
venvs hold different versions of the same packages (Airflow's own constraints pin older ones), so one constraints
file for all of them cannot work. Only installed packages are touched - a host without the [llm] extra does not get
litellm. A venv without a lock (superset: pinned by packs/superset/requirements.txt) is reported, not changed.
Scope: this demo's stack; the product's dependency policy (pack version params) is unchanged.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

LOCKS = Path(__file__).resolve().parent / "lock"
VENVS = {"dpagent": "/opt/dpagent/.venv", "airflow": "/opt/airflow/.venv", "dbt": "/opt/dbt/.venv",
         "dlt": "/opt/dlt/.venv", "superset": "/opt/superset/.venv"}
SKIP = {"pip", "setuptools", "wheel", "dpagent"}      # the venv's tooling; dpagent itself is the checkout


def norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def lock(name: str) -> dict[str, str] | None:
    p = LOCKS / f"{name}.txt"
    if not p.is_file():
        return None
    pins = {}
    for line in p.read_text().splitlines():
        if line and not line.startswith("#"):
            n, v = line.split("==", 1)
            pins[norm(n)] = v
    return pins


def installed(venv: str) -> dict[str, str]:
    out = subprocess.run([f"{venv}/bin/python", "-c", "import importlib.metadata as m, json; print(json.dumps("
                          "{d.metadata['Name']: d.version for d in m.distributions()}))"],
                         capture_output=True, text=True, check=True).stdout
    return {norm(n): v for n, v in json.loads(out).items() if norm(n) not in SKIP}


def diff(name: str, venv: str) -> dict:
    pins, have = lock(name), installed(venv)
    return {"differs": {n: [v, pins[n]] for n, v in have.items() if n in pins and pins[n] != v},
            "not_in_lock": sorted(n for n in have if n not in pins)}


def main(what: str) -> int:
    report, bad = {}, False
    for name, venv in VENVS.items():
        if not Path(venv, "bin/python").exists():
            report[name] = "not installed"
            continue
        if lock(name) is None:
            report[name] = "no lock (pinned by its pack)"
            continue
        d = diff(name, venv)
        if what == "apply" and d["differs"]:
            pins = lock(name)
            subprocess.run([f"{venv}/bin/pip", "install", "--quiet", "--no-deps",
                            *[f"{n}=={pins[n]}" for n in d["differs"]]], check=True)
            subprocess.run([f"{venv}/bin/pip", "check"], check=True)
            d = {**diff(name, venv), "applied": d["differs"]}
        report[name] = d
        bad |= bool(d["differs"] or d["not_in_lock"])
    print(json.dumps(report, indent=1, sort_keys=True))
    return 1 if bad else 0


if __name__ == "__main__":
    if sys.argv[1:] not in (["check"], ["apply"]):
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
