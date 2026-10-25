# HG-shaped synthetic load benchmark - "synthetic, in HG's table shape; not HG's data, not an HG load acceptance"

Measures HG's `gold.resources_useage_number` pipeline (`pipelines/hg_resources_useage_number`: 6 source tables → one
bronze batch on SeaweedFS → staging → silver → gold, HG's own SQL) on **generated** data at increasing scales, on a
disposable host. It answers: does it stay correct, how long does each stage take, and how much CPU, memory, I/O and disk
does it use at N rows on this host. It does **not** answer whether HG's real load meets HG's SLA. That needs the
owner's data, volumes, skew, schedule, timezone, SLA and resource budget (`docs/hg-representative-load-plan.md`).

```bash
bash scripts/hg-load-bench.sh 10000 100000 1000000     # SCALE = rows of resource_editings
```

| Piece | What it does |
|---|---|
| `generate.py` | deterministic CSVs for the 6 tables (same seed and scale → same bytes). Every HG rule is exercised at every scale (see its docstring); resource use is Zipf-skewed and cut lengths vary |
| `answer.py` | the answer key, from the CSVs only, in plain Python. It is an O(n log n) version of `tests/hg_resources_reference.py`, the naive implementation checked against the hand-written `expected.yaml`. `tests/test_hg_usage_benchmark.py` shows the two agree row for row on generated data |
| `bench.py` | inside the host, per scale: generate → COPY into a throwaway source → deploy → run (stage times from the journal; container CPU/memory/IO from its cgroup; DB sizes) → gold = answer key (count, digest of every row, distinct counts, groups) → rerun = same, no duplicates → a mid-run failure (the warehouse role is refused during the silver transform: the run fails, gold keeps the last result) → recovery = answer key → the dashboard's SQL 20× (on the warehouse, not through Superset) → cleanup (undeploy, bronze purge, databases, files) |
| `scripts/hg-load-bench.sh` | builds a bronze host with the M2.5 runner (one fixture scenario must pass first), runs `bench.py`, copies evidence, removes the host. Stops before the build if the Docker host has < 25 GB free. Inside: a scale and every larger one are refused below `HGBENCH_MIN_FREE_GB` / `HGBENCH_MIN_AVAIL_GB`, or after a smaller scale failed or timed out |

Evidence: `docs/evidence/hg-load/<sha>/<utc>/` (`summary.json`; per scale `result.json`, `answer.json`, `samples.json`).

## Limits

- Synthetic data. Real HG volumes, value distributions and growth may change every number here.
- Full snapshot only: every run re-extracts and rebuilds everything. Incremental loading is **not** measured or
  claimed.
- One shared Docker host, other services included. The cgroup numbers are the container's own; wall times include
  Airflow scheduling and vary with the host's load (the preflight records it).
- The dashboard's SQL is timed with `psql` on the warehouse, not through Superset's API or a browser.
