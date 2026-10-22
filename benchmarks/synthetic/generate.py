"""Deterministic synthetic orders for the quickstart_sales source table, and the answer key computed while
generating - in plain Python integer cents, independently of SQL, dlt and dbt.

  generate.py SCALE --csv OUT.csv|- --expected OUT.json [--config config.yaml]

The CSV columns are the source table's: id, order_date, status, amount, customer. Same seed + scale ->
byte-identical CSV (its sha256 is in the expected file)."""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import random
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def load_config(path: Path = HERE / "config.yaml") -> dict:
    config = yaml.safe_load(path.read_text())
    for name, scale in config["scales"].items():
        if not (isinstance(scale.get("rows"), int) and scale["rows"] > 0):
            raise ValueError(f"scale {name}: rows must be a positive integer")
    return config


def _months(first: str, last: str) -> list[tuple[int, int]]:
    (y, m), (ly, lm) = (tuple(map(int, first.split("-"))), tuple(map(int, last.split("-"))))
    out = []
    while (y, m) <= (ly, lm):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def rows(config: dict, count: int):
    """Yields (id, 'YYYY-MM-DD', raw status, amount in integer cents, customer)."""
    rng = random.Random(config["seed"])
    months = _months(*config["months"])
    statuses = list(config["statuses"])
    weights = [config["statuses"][s] for s in statuses]
    low, high = config["amount_cents"]
    for i in range(1, count + 1):
        y, m = months[rng.randrange(len(months))]
        day = rng.randint(1, calendar.monthrange(y, m)[1])
        yield (i, f"{y:04d}-{m:02d}-{day:02d}", rng.choices(statuses, weights)[0], rng.randint(low, high),
               f"c{rng.randrange(1, max(2, count // 20)):07d}")


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def generate(config: dict, scale: str, out) -> dict:
    count = config["scales"][scale]["rows"]
    digest = hashlib.sha256()
    by_month: dict[str, list[int]] = {}
    total_done = total_cents = 0
    for i, day, status, cents, customer in rows(config, count):
        line = f"{i},{day},{status},{money(cents)},{customer}\n"
        digest.update(line.encode())
        out.write(line)
        if status.strip().lower() == "done":          # the business rule, restated here on purpose
            agg = by_month.setdefault(day[:7], [0, 0])
            agg[0] += 1; agg[1] += cents
            total_done += 1; total_cents += cents
    return {"label": config["label"], "seed": config["seed"], "scale": scale, "rows": count,
            "csv_sha256": digest.hexdigest(),
            "expected": [{"month": k, "orders": v[0], "revenue": money(v[1])} for k, v in sorted(by_month.items())],
            "invariants": {"landing_rows": count, "landing_distinct_ids": count, "done_orders": total_done,
                           "done_revenue": money(total_cents)}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("scale")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--expected", required=True)
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    a = ap.parse_args(argv)
    config = load_config(Path(a.config))
    if a.scale not in config["scales"]:
        ap.error(f"unknown scale {a.scale!r}: {sorted(config['scales'])}")
    out = sys.stdout if a.csv == "-" else open(a.csv, "w", newline="")
    try:
        result = generate(config, a.scale, out)
    finally:
        if out is not sys.stdout:
            out.close()
    Path(a.expected).write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
