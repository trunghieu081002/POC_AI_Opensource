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

## Runs

Every run is kept (`docs/evidence/hg-load/`).

| Run | Result |
|---|---|
| `a0ba1d8/20261101T144006Z` | the runner stopped before measuring: it read the M2.5 runner's exit 3 ("one requested scenario of the profile, ran clean") as a failure. The host was fine and was reused |
| `c3a4f5c/20261101T150042Z` | **correct** at 10k / 100k / 1M; CPU/memory/IO **not recorded** (the container has a private cgroup namespace) |
| `66843c2/20261101T152602Z` | **PASS** at all three scales, with resources sampled by `docker stats` on the Docker host (load average ~2 then; 72 cores; 66 GB free) |

| scale (resource_editings) | gold rows | correct (first / rerun / recovery) | fault: failed + gold kept | load s | first run s | rerun s | peak mem GB | avg / peak CPU cores | write MB | source / warehouse MB | dashboard SQL median ms (all / one video) | cleanup |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10,000 | 5,693 | True / True / True | True + True | 1.0 | 75.7 | 72.7 | 1.59 | 0.74 / 1.17 | 40.0 | 8.8 / 12.0 | 49.5 / 43.7 | True |
| 100,000 | 59,985 | True / True / True | True + True | 1.3 | 103.4 | 75.2 | 1.69 | 0.75 / 1.23 | 130.0 | 17.1 / 34.5 | 172.6 / 45.7 | True |
| 1,000,000 | 690,171 | True / True / True | True + True | 2.9 | 346.6 | 114.6 | 2.37 | 2.22 / 3.44 | 1340.0 | 99.6 / 277.3 | 1671.3 / 89.2 | True |

How to read it:
- Every scale is correct: the first run, the rerun and the recovery all equal the answer key (row digest). The fault
  failed the run and left gold at its previous result. Cleanup was complete.
- Times are wall times from trigger to finish, Airflow scheduling included. The journal's stage times have 1 s
  resolution, so the per-stage split is in `result.json`, not here. The first run of a scale is slower than the rerun
  (346 s vs 115 s at 1M). The cause was not measured; a first run on a new
  deployment (cold caches, first dbt invocation) is the likely reason, but this run does not show it.
- **Measured bottleneck: the dashboard's audit SQL** (four `COUNT(DISTINCT …)` over gold): 1.7 s at 690k gold rows,
  growing about linearly. Nothing was optimised. There is no SLA to measure it against, and the dashboard's business
  output must not change. A pre-aggregated table or indexes on `video_id`/`hg_stock_id` are the candidates once HG
  gives a latency target.
- Synthetic, in HG's table shape. **Not HG's load.** Full snapshot only; incremental loading was not measured.
