# Synthetic benchmark — "synthetic benchmark, not HG load"

Measures the quickstart pipeline (`pipelines/quickstart_sales`, docs/quickstart.md) on generated data, on a
disposable host built the same way as the quickstart acceptance. It answers "how long does each stage take
and how much memory/disk does this pipeline use at N rows on this host". It does **not** answer anything about HG:
the table shape, value distribution, skew, volumes, SLA and timezone are made up. Those can only come from the
data owner.

```bash
bash scripts/quickstart-acceptance-ci.sh --bench smoke          # Ubuntu 22.04 container
bash scripts/quickstart-acceptance-ci.sh --bench smoke --os ol8 # Oracle Linux 8.10 container
```

| Piece | What it does |
|---|---|
| `config.yaml` | fixed seed, month range, status weights (incl. `" DONE "` for normalisation), amount range; scales `smoke` 20k / `small` 1M / `medium` 10M rows with the host's minimum free disk and available memory, and a run timeout |
| `preflight.py` | refuses a scale (exit 4) **before** a host is built if the Docker root's free disk or `MemAvailable` is below the scale's minimum; repeated inside the host before generating |
| `generate.py` | deterministic CSV (same seed and scale → same sha256) and the answer key, computed while generating in integer cents, in plain Python, independently of SQL/dlt/dbt |
| `bench.sh` | stack and pipeline through the product (`walkthrough.sh install … deploy`), generate, load the source table, run the deployed pipeline once (timed), per-stage durations/rows from dpagent's journal, memory and database size sampled every 5 s, independent check, then cleanup |
| `check.py` | reads the warehouse with `psql` and compares it with the answer key: monthly rows exact, landing rows = generated rows, ids unique, totals |

Results land in `docs/evidence/benchmark/<sha>/<utc>-<scale>[-ol8]/bench/`: `measurements.json` (stages,
wall seconds per step, peak RSS of all processes, minimum available memory, peak database size),
`check.json`, `expected.json`, `timings.txt`, `samples.csv` and the logs. The generated data is never kept.

Cleanup (on every exit after setup, verified): the pipeline undeployed (DAG gone), the `quickstart_sales*`
warehouse schemas dropped, the source table dropped, the data file removed. The disposable host itself is then
removed by the runner.

## Limits

* One table and one full-snapshot pipeline; no incremental, no bronze, no BI queries.
* Stage times come from the journal at 1 s resolution; the run's wall time includes Airflow scheduling.
* Memory is the sum of RSS of all processes in the container (it includes the idle stack); the host is shared,
  so numbers vary with its load - every result records the host preflight next to it.
* Scales above `smoke` need a host with the free disk and memory in `config.yaml`; the shared development host
  (disk 96 % full) is not one.

## Runs

| Run | Result |
|---|---|
| `24b6a7d/20261021T140603Z-smoke` (Ubuntu container; host preflight: 39.1 GB free, 3.1 GB available) | **PASS**: 20,000 rows, 24 months, warehouse = answer key (all 5 checks), cleanup verified, secret scan clean. Pipeline run 32.6 s wall (mostly Airflow scheduling: the journal's stages took 0-1 s at 1 s resolution); generate 0.2 s, load 0.2 s; peak RSS of all processes 4.8 GB (the whole idle stack), minimum available memory 1.9 GB, peak database size 69.5 MB. Not a performance claim: one run, a shared loaded host |

What smoke does not show: stage timing needs a larger scale (`small`/`medium`) to be meaningful, and the journal
records no row count for dbt stages (`rows: null`) - the independent check counts rows itself.
