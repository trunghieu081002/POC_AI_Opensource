#!/usr/bin/env bash
# Synthetic benchmark of the quickstart_sales pipeline, INSIDE a disposable host (scripts/quickstart-acceptance-ci.sh
# --bench SCALE runs it through tests/quickstart_acceptance/inside.sh, which provides the environment file).
# "synthetic benchmark, not HG load": generated data of a made-up shape - it measures this pipeline on this host.
#   bench.sh SCALE OUT_DIR
# 1. preflight of the scale inside the host; refuse (exit 4) before anything is created if it does not fit
# 2. cleanup is registered BEFORE the first resource is created; each resource is recorded just before the step
#    that creates it (a step failing half way still gets its resource removed)
# 3. the stack and the pipeline through the product (walkthrough install/source/validate/approve/deploy)
# 4. generate (fixed seed) -> load into the source table -> run the deployed pipeline once, timed
# 5. per-stage durations and row counts from dpagent's journal; peak memory / database size sampled every 5 s
# 6. independent check (psql vs the generator's answer key)
# Cleanup checks the exit code of every removal and every audit query: a query that fails is "could not
# verify", never "nothing left". The result is cleanup.json; the exit code is 5 if cleanup is not proven.
# The stack itself (installed packages, databases) belongs to the disposable host and goes with it.
# Paths can be overridden for tests (BENCH_*); the defaults are the disposable host's.
set -euo pipefail
SCALE="${1:?scale}"; OUT="${2:?out dir}/bench"
mkdir -p "$OUT"
REPO="${BENCH_REPO:-/opt/dpagent}"
cd "$REPO"
W="${BENCH_WALKTHROUGH:-examples/quickstart/walkthrough.sh}"
PY="${BENCH_PY:-/opt/dpagent/.venv/bin/python}"
B=benchmarks/synthetic
CONFIG="${BENCH_CONFIG:-$B/config.yaml}"
DATA="${BENCH_DATA:-/var/tmp/dpagent-bench-orders.csv}"
JOURNAL="${BENCH_JOURNAL:-/var/lib/dpagent/dpagent.db}"
DISK_PATH="${BENCH_DISK_PATH:-/var/lib}"
P=quickstart_sales
log() { echo "$*" | tee -a "$OUT/steps.log"; }
timed() {  # timed <label> <command...>: run, keep its log, record wall seconds
  local label="$1"; shift; local start end rc
  start=$(date +%s.%N); set +e; "$@" > "$OUT/$label.log" 2>&1; rc=$?; set -e; end=$(date +%s.%N)
  awk -v l="$label" -v a="$start" -v b="$end" 'BEGIN{printf "%s %.1f\n", l, b-a}' >> "$OUT/timings.txt"
  log "$label rc=$rc"
  [ "$rc" -eq 0 ] || { tail -30 "$OUT/$label.log"; exit "$rc"; }
}
pg() { runuser -u postgres -- psql -X -At -v ON_ERROR_STOP=1 "$@"; }
airflow_dags() {  # the product's own way to call the Airflow CLI (as `airflow`, with its env file and venv)
  "$PY" -c 'import subprocess, sys; from dpagent.pipelines import deploy
sys.exit(subprocess.run(deploy._airflow_cli_command("dags", "list", "-o", "plain")).returncode)'
}

# ---- 1. refuse before anything exists
set +e; "$PY" "$B/preflight.py" "$SCALE" --disk-path "$DISK_PATH" --config "$CONFIG" > "$OUT/preflight-host-inside.json"; prc=$?; set -e
if [ "$prc" -ne 0 ]; then
  log "preflight refused inside the host (rc=$prc), nothing created: $(cat "$OUT/preflight-host-inside.json")"
  echo '{"ok": true, "created": [], "resources": []}' > "$OUT/cleanup.json"
  exit 4
fi
TIMEOUT=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["needs"]["timeout_s"])' "$OUT/preflight-host-inside.json")

# ---- 2. cleanup, registered before the first resource
: > "$OUT/created.txt"
created() { grep -qx "$1" "$OUT/created.txt" || echo "$1" >> "$OUT/created.txt"; }
SAMPLER=
RESULTS="$OUT/cleanup-results.tsv"
record() {  # record <resource> <step> <rc> <remaining|-> : one line per removal/audit, read into cleanup.json
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$RESULTS"
}
count_query() {  # count_query <db> <sql>: prints the count; returns non-zero if the query failed or is not a number
  local out
  out=$(pg -d "$1" -c "$2") || return 1
  [[ "$out" =~ ^[0-9]+$ ]] || return 1
  echo "$out"
}
cleanup() {
  local rc=$? r n out schemas
  trap - EXIT
  set +e
  : > "$RESULTS"
  if [ -n "$SAMPLER" ]; then kill "$SAMPLER" 2>/dev/null; wait "$SAMPLER" 2>/dev/null; fi
  if grep -qx pipeline "$OUT/created.txt"; then
    dpagent pipeline undeploy "$P" --yes > "$OUT/cleanup-undeploy.log" 2>&1; record pipeline undeploy $? -
    out=$(airflow_dags 2>>"$OUT/cleanup-undeploy.log"); r=$?
    if [ "$r" -eq 0 ]; then n=$(printf '%s\n' "$out" | grep -cw "$P"); record pipeline audit-dag 0 "$n"
    else record pipeline audit-dag "$r" unknown; fi
  fi
  if grep -qx warehouse_schemas "$OUT/created.txt"; then
    schemas=$(pg -d warehouse -c "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'quickstart\_sales%'"); r=$?
    record warehouse_schemas list "$r" -
    if [ "$r" -eq 0 ]; then
      for s in $schemas; do pg -d warehouse -c "DROP SCHEMA \"$s\" CASCADE" >/dev/null; record warehouse_schemas "drop $s" $? -; done
    fi
    n=$(count_query warehouse "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'quickstart\_sales%'"); r=$?
    record warehouse_schemas audit "$r" "${n:-unknown}"
  fi
  if grep -qx source_table "$OUT/created.txt"; then
    pg -d "$QS_SRC_NAME" -c "DROP TABLE IF EXISTS public.orders" >/dev/null; record source_table drop $? -
    n=$(count_query "$QS_SRC_NAME" "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = 'orders'"); r=$?
    record source_table audit "$r" "${n:-unknown}"
  fi
  if grep -qx data_file "$OUT/created.txt"; then
    rm -f "$DATA"; record data_file remove $? -
    if [ -e "$DATA" ]; then record data_file audit 0 1; else record data_file audit 0 0; fi
  fi
  "$PY" - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
created = [l.strip() for l in open(f"{out}/created.txt") if l.strip()]
rows = [l.rstrip("\n").split("\t") for l in open(f"{out}/cleanup-results.tsv") if l.strip()]
resources = []
for name in created:
    steps = [{"step": s, "rc": int(rc), "remaining": None if left == "-" else left} for r, s, rc, left in rows if r == name]
    audits = [s for s in steps if s["step"].startswith("audit")]
    proven = bool(audits) and all(s["rc"] == 0 for s in steps) and all(a["remaining"] == "0" for a in audits)
    resources.append({"resource": name, "removed_and_verified": proven, "steps": steps})
ok = all(r["removed_and_verified"] for r in resources)
json.dump({"ok": ok, "created": created, "resources": resources}, open(f"{out}/cleanup.json", "w"), indent=1)
PY
  if [ $? -eq 0 ] && grep -q '"ok": true' "$OUT/cleanup.json"; then
    log "cleanup ok ($(paste -sd, "$OUT/created.txt"): every removal rc=0, every audit rc=0 and 0 left)"
  else
    log "CLEANUP NOT PROVEN - see cleanup.json"; [ "$rc" -ne 0 ] || rc=5
  fi
  exit "$rc"
}
trap cleanup EXIT

# ---- 3. setup through the product; each resource recorded before the step that creates it
timed setup-install bash "$W" install
created source_table;      timed setup-source bash "$W" source
created warehouse_schemas; timed setup-validate bash "$W" validate
timed setup-approve bash "$W" approve
created pipeline;          timed setup-deploy bash "$W" deploy

# ---- 4. generate, load, run
created data_file
timed generate "$PY" "$B/generate.py" "$SCALE" --config "$CONFIG" --csv "$DATA" --expected "$OUT/expected.json"
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
timed run dpagent pipeline run "$P" --yes --wait --timeout "$TIMEOUT"
kill "$SAMPLER" 2>/dev/null; wait "$SAMPLER" 2>/dev/null || true; SAMPLER=

# ---- 5. measurements
"$PY" - "$OUT" "$JOURNAL" <<'PY'
import json, sqlite3, sys
from datetime import datetime
out, journal = sys.argv[1], sys.argv[2]
db = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
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

# ---- 6. independent check
"$PY" "$B/check.py" "$OUT/expected.json" > "$OUT/check.json" || { cat "$OUT/check.json"; log "CHECK FAILED"; exit 1; }
log "check ok: warehouse equals the generator's answer key ($(head -c 300 "$OUT/check.json"))"
log "BENCH PASS ($SCALE) - synthetic benchmark, not HG load"
