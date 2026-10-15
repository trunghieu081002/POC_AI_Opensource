# Representative HG load: pending owner inputs

Correctness passed on synthetic fixtures. No representative throughput or real
HG-source acceptance is claimed. The 5787-code operational exclusion seed is
real input to the model, but the small fixture is not a representative workload.

| Required owner input | What must be recorded before running |
| --- | --- |
| Permitted data | Approved synthetic profile or authorized sanitized snapshot; location/access and retention rules |
| Representative scale | Rows/bytes for all six source tables, editing/resource fanout and skew, output pairs, exclusion/duplicate ratios; full-snapshot frequency |
| Time criteria | Agreed end-to-end and stage limits, dashboard latency limits, cold/warm distinction |
| Resource/window budget | Approved CPU/RAM/swap/disk/IO limits and time window on a disposable host |
| Business clock | Intended publication timezone and meaning of timestamp-without-time-zone output |

The correctness container's 8 CPU / 6 GiB cap is an isolation setting, not an
agreed performance target. The Oracle Linux host is shared; its preflight had
existing services and limited free disk. Do not infer a safe load budget from
CPU count alone. No company/demo dataset has been read for a load profile.

Once inputs are approved, pin the source revision and data/expected manifests,
measure extraction/Bronze bytes and time, load time, each dbt stage/tests, total
elapsed time, CPU/RSS/swap/temp-disk/IO, and repeated authenticated dashboard
queries. Keep failures, exit codes, per-stage journal, actual resource settings,
independent expected/invariants and cleanup audit. Use enough repeated query
samples to support any percentile claim. Re-run the snapshot to verify stable
results and no duplication; include source fault and recovery at agreed scale.
Expected data must be produced independently of warehouse output. Report scope
and sample/coverage explicitly if a large dataset cannot be exhaustively compared.

Use the existing pipeline/pack/suite contracts and add a connector only if the
approved source contract requires it. Do not add unrelated stacks or call a
paid model. A proposed plan or generated fixture is not a completed benchmark.
