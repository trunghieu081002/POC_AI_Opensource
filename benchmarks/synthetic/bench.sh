#!/usr/bin/env bash
# Synthetic benchmark of the quickstart_sales pipeline, INSIDE a disposable host (scripts/quickstart-acceptance-ci.sh
# --bench SCALE runs it through tests/quickstart_acceptance/inside.sh, which provides the environment file).
# "synthetic benchmark, not HG load": generated data of a made-up shape - it measures this pipeline on this host.
#   bench.sh SCALE OUT_DIR
# 1. the stack and the pipeline through the product (walkthrough install/source/validate/approve/deploy)
# 2. preflight of the scale inside the host; refuse (exit 4) before generating anything if it does not fit
# 3. generate (fixed seed) -> load into the source table -> run the deployed pipeline once, timed
# 4. per-stage durations and row counts from dpagent's journal; peak memory / database size sampled every 5 s
# 5. independent check (psql vs the generator's answer key), then cleanup of everything the benchmark created
set -euo pipefail
SCALE="${1:?scale}"; OUT="${2:?out dir}/bench"
mkdir -p "$OUT"
cd /opt/dpagent
W=examples/quickstart/walkthrough.sh
PY=/opt/dpagent/.venv/bin/python
B=benchmarks/synthetic
DATA=/var/tmp/dpagent-bench-orders.csv
log() { echo "$*" | tee -a "$OUT/steps.log"; }
timed() {  # timed <label> <command...>: run, keep its log, record wall seconds
  local label="$1"; shift; local start end rc
  start=$(date +%s.%N); set +e; "$@" > "$OUT/$label.log" 2>&1; rc=$?; set -e; end=$(date +%s.%N)
  awk -v l="$label" -v a="$start" -v b="$end" 'BEGIN{printf "%s %.1f\n", l, b-a}' >> "$OUT/timings.txt"
  log "$label rc=$rc"
  [ "$rc" -eq 0 ] || { tail -30 "$OUT/$label.log"; exit "$rc"; }
}
pg() { runuser -u postgres -- psql -X -At -v ON_ERROR_STOP=1 "$@"; }

for s in install source validate approve deploy; do timed "setup-$s" bash "$W" "$s"; done

"$PY" "$B/preflight.py" "$SCALE" --disk-path /var/lib > "$OUT/preflight-host-inside.json" \
  || { log "preflight refused inside the host: $(cat "$OUT/preflight-host-inside.json")"; exit 4; }
TIMEOUT=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["needs"]["timeout_s"])' "$OUT/preflight-host-inside.json")

cleanup() {   # everything the benchmark created, verified gone; runs on any exit after this point
  local rc=$?
  set +e
  dpagent pipeline undeploy quickstart_sales --yes > "$OUT/cleanup-undeploy.log" 2>&1
  for s in $(pg -d warehouse -c "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'quickstart\_sales%'"); do
    pg -d warehouse -c "DROP SCHEMA \"$s\" CASCADE" >/dev/null
  done
  pg -d "$QS_SRC_NAME" -c "DROP TABLE IF EXISTS public.orders" >/dev/null
  rm -f "$DATA"
  [ -n "${SAMPLER:-}" ] && kill "$SAMPLER" 2>/dev/null
  left=$(pg -d warehouse -c "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'quickstart\_sales%'")
  src=$(pg -d "$QS_SRC_NAME" -c "SELECT count(*) FROM pg_tables WHERE tablename = 'orders'")
  dag=$(su airflow -s /bin/bash -c "AIRFLOW_HOME=/opt/airflow/home /opt/airflow/.venv/bin/airflow dags list 2>/dev/null" \
        | grep -c quickstart_sales)
  if [ "$left" = 0 ] && [ "$src" = 0 ] && [ "$dag" = 0 ] && [ ! -e "$DATA" ]; then log "cleanup ok (warehouse schemas, source table, data file, DAG)"
  else log "CLEANUP INCOMPLETE: schemas=$left source_tables=$src dag=$dag"; [ "$rc" -ne 0 ] || rc=5; fi
  exit "$rc"
}
trap cleanup EXIT

timed generate "$PY" "$B/generate.py" "$SCALE" --csv "$DATA" --expected "$OUT/expected.json"
load_data() {   # as the source's own user, like any loader of that database
  PGPASSWORD="$QS_SRC_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 -h "${QS_SRC_HOST:-localhost}" -U "$QS_SRC_USER" \
    -d "$QS_SRC_NAME" -c 'TRUNCATE public.orders' \
    -c "\\copy public.orders (id, order_date, status, amount, customer) FROM '$DATA' WITH (FORMAT csv)"
}
timed load load_data

( while :; do
    echo "$(date +%s),$(ps -eo rss= | awk '{s+=$1} END{print s}'),$(awk '/MemAvailable/{print $2}' /proc/meminfo),$(pg -d postgres -c 'SELECT sum(pg_database_size(datname)) FROM pg_database' || true)"
    sleep 5
  done ) > "$OUT/samples.csv" 2>/dev/null &
SAMPLER=$!
timed run dpagent pipeline run quickstart_sales --yes --wait --timeout "$TIMEOUT"
kill "$SAMPLER" 2>/dev/null; SAMPLER=

"$PY" - "$OUT" <<'PY'
import json, sqlite3, sys
from datetime import datetime
out = sys.argv[1]
db = sqlite3.connect("file:/var/lib/dpagent/dpagent.db?mode=ro", uri=True)
run = db.execute("SELECT max(run_id) FROM stage_runs WHERE pipeline='quickstart_sales'").fetchone()[0]
stages = [{"stage": s, "status": st, "rows": n,
           "seconds": (datetime.fromisoformat(f) - datetime.fromisoformat(b)).total_seconds() if f else None}
          for s, st, n, b, f in db.execute("SELECT stage, status, row_count, started_at, finished_at FROM stage_runs "
                                           "WHERE pipeline='quickstart_sales' AND run_id=? ORDER BY id", (run,))]
samples = [list(map(int, l.split(","))) for l in open(f"{out}/samples.csv") if l.count(",") == 3 and l.strip().split(",")[3]]
timings = dict(l.split() for l in open(f"{out}/timings.txt"))
json.dump({"run_id": run, "stages": stages, "stage_time_resolution_s": 1,
           "wall_seconds": {k: float(v) for k, v in timings.items()},
           "peak_rss_all_processes_mb": max(s[1] for s in samples) // 1024 if samples else None,
           "min_mem_available_mb": min(s[2] for s in samples) // 1024 if samples else None,
           "peak_database_bytes": max(s[3] for s in samples) if samples else None, "samples": len(samples)},
          open(f"{out}/measurements.json", "w"), indent=1)
PY
"$PY" "$B/check.py" "$OUT/expected.json" > "$OUT/check.json" || { cat "$OUT/check.json"; log "CHECK FAILED"; exit 1; }
log "check ok: warehouse equals the generator's answer key ($(cat "$OUT/check.json" | head -c 300))"
log "BENCH PASS ($SCALE) - synthetic benchmark, not HG load"
