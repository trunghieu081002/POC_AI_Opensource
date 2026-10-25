#!/usr/bin/env bash
# Runs INSIDE a demo host built by `quickstart-acceptance-ci.sh --demo artifact --show` (stack installed, the accepted
# draft validated, nothing approved). Driven by scripts/ops-acceptance-ci.sh:
#   source  : approval stand-in, deploy + run (= expected), dashboard + viewer -> the state to protect; baseline
#   backup  : dpagent backup create (timed)
#   later   : one more run AFTER the backup (it must NOT be in the restored host: that is the RPO)
#   target  : a clean host of the same release gets Superset installed (bi-validate), as the source had
#   restore : dpagent backup restore (timed), then every check against the source's baseline
set -euo pipefail
PHASE="${1:?source|backup|later|target|restore}"
cd /opt/dpagent
OUT=/root/ops-evidence; mkdir -p "$OUT"
PY=/opt/dpagent/.venv/bin/python
SPY=/opt/superset/.venv/bin/python
EX=tests/llm_verification/cases/fulfilment_sla/expected.yaml
D="bash examples/demo/demo.sh"
set -a; . /root/demo-dev.env; set +a
step() { local label="$1"; shift; echo "== $label: $*" | tee -a "$OUT/steps.log"
         set +e; "$@" > "$OUT/$label.log" 2>&1; local rc=$?; set -e
         echo "$label rc=$rc" | tee -a "$OUT/steps.log"; [ "$rc" -eq 0 ] || { tail -40 "$OUT/$label.log"; exit "$rc"; }; }
ok() { echo "check $1 ok" | tee -a "$OUT/steps.log"; }
runs() { "$PY" -c 'import sqlite3; print(sqlite3.connect("file:/var/lib/dpagent/dpagent.db?mode=ro", uri=True).execute("SELECT count(*) FROM runs").fetchone()[0])'; }
fp() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' /var/lib/dpagent-bi-assets/fulfil-sla/validation.json; }
health() { for _ in $(seq 1 90); do curl -fsS --noproxy '*' -o /dev/null "$1" 2>/dev/null && return 0; sleep 2; done; return 1; }
state() {   # what must survive: journal runs, deployed pipelines, the DAG, dashboards, viewer accounts, the report
  echo "runs=$(runs)"
  dpagent pipeline list 2>/dev/null | grep fulfilment_sla | tr -s ' '
  echo "dag=$(runuser -u postgres -- psql -X -At -d airflow_meta -c "SELECT is_paused FROM dag WHERE dag_id='fulfilment_sla'")"
  echo "dashboards=$(runuser -u postgres -- psql -X -At -d superset_meta -c 'SELECT count(*) FROM dashboards')"
  echo "viewer=$(runuser -u postgres -- psql -X -At -d superset_meta -c "SELECT count(*) FROM ab_user WHERE username='fulfil.viewer'")"
  echo "report=$(runuser -u postgres -- psql -X -At -d warehouse -c 'SELECT md5(string_agg(t::text, chr(10) ORDER BY month)) FROM fulfilment_sla.fct_delivery_sla t')"
}

case "$PHASE" in
  source)
    DEMO_APPROVED_BY=ops.test step s1-approve $D approve
    step s2-deploy $D deploy
    "$PY" examples/demo/check_fulfil.py "$EX" > "$OUT/s-check-run.json"
    step s3-bi-validate $D bi-validate
    DEMO_BI_APPROVED_BY=ops.test DEMO_BI_APPROVED_SHA256="$(fp)" step s4-bi-deploy $D bi-deploy
    "$SPY" tests/quickstart_acceptance/check_viewer.py fulfil.viewer FS_VIEWER_PASSWORD fulfil-sla - > "$OUT/s-check-viewer.json"
    ok "source state (approved, deployed, run = expected, dashboard + viewer = expected)"
    # ---- overlap: two runs triggered back to back never run at the same time (max_active_runs=1)
    r1=$(dpagent pipeline run fulfilment_sla --yes 2>&1 | sed -n 's/.*dpagent run \([0-9][0-9]*\).*/\1/p' | tail -1)
    r2=$(dpagent pipeline run fulfilment_sla --yes 2>&1 | sed -n 's/.*dpagent run \([0-9][0-9]*\).*/\1/p' | tail -1)
    [ -n "$r1" ] && [ -n "$r2" ] || { echo "no run ids for the overlap check" >&2; exit 1; }
    "$PY" - "$r1" "$r2" > "$OUT/s-check-overlap.json" <<'PY'
import json, sqlite3, sys, time
a, b = map(int, sys.argv[1:])
db = "file:/var/lib/dpagent/dpagent.db?mode=ro"
deadline = time.time() + 1800
while time.time() < deadline:
    c = sqlite3.connect(db, uri=True)
    st = {r[0]: r[1] for r in c.execute("SELECT id, status FROM runs WHERE id IN (?, ?)", (a, b))}
    if all(v != "running" for v in st.values()):
        break
    time.sleep(5)
span = {rid: c.execute("SELECT min(started_at), max(finished_at) FROM stage_runs WHERE run_id = ?", (rid,)).fetchone()
        for rid in (a, b)}
ok = st.get(a) == st.get(b) == "ok" and span[a][1] <= span[b][0]
print(json.dumps({"ok": ok, "status": st, "stage_spans": span}))
sys.exit(0 if ok else 1)
PY
    ok "overlap (two back-to-back runs: both ok, the second started after the first finished)"
    # ---- alerts: bad data -> failed run + failed gate reported (and once), then recovery
    dpagent alerts check --min-free-gb 0 --min-avail-gb 0 > /dev/null || true        # watermark
    step s5-bad-data $D bad-data
    set +e; dpagent alerts check --min-free-gb 0 --min-avail-gb 0 --notify-command "cat > $OUT/s-alert-notified.json" \
      > "$OUT/s-alerts.json"; rc=$?; set -e
    [ "$rc" = 1 ] && grep -q '"failed_gate"' "$OUT/s-alerts.json" && grep -q '"failed_run"' "$OUT/s-alerts.json" \
      && grep -q stock_picking "$OUT/s-alert-notified.json" || { cat "$OUT/s-alerts.json"; exit 1; }
    dpagent alerts check --min-free-gb 0 --min-avail-gb 0 > "$OUT/s-alerts-again.json"
    step s6-fix-data $D fix-data
    "$PY" examples/demo/check_fulfil.py "$EX" > "$OUT/s-check-recovered.json"
    ok "alerts (bad data: failed run + the stock_picking gate, notified once) and recovery = expected"
    # ---- least privilege (each refusal is the FIRST line of psql's answer; a refusal exits non-zero: not fatal here)
    set +e
    {
      stat -c '%n %U %a' /opt/airflow/home/pipelines.env /root/demo-dev.env /var/lib/dpagent/evidence.key /opt/superset/superset.env
      export PGPASSWORD="$SUPERSET_WAREHOUSE_PASSWORD"
      psql -X -h localhost -U superset_reader -d warehouse -c "CREATE TABLE fulfilment_sla.x (i int)" 2>&1 | head -1
      psql -X -h localhost -U superset_reader -d warehouse -c "DELETE FROM fulfilment_sla.fct_delivery_sla" 2>&1 | head -1
      export PGPASSWORD="$WAREHOUSE_DB_PASSWORD"
      psql -X -h localhost -U "$WAREHOUSE_DB_USER" -d airflow_meta -c "SELECT 1" 2>&1 | head -1
      psql -X -h localhost -U "$WAREHOUSE_DB_USER" -d superset_meta -c "SELECT 1" 2>&1 | head -1
      unset PGPASSWORD
    } > "$OUT/s-privileges.txt" 2>&1
    set -e
    "$PY" - "$OUT/s-privileges.txt" > "$OUT/s-privileges.json" <<'PY'
import json, sys
t = open(sys.argv[1]).read().splitlines()
denied = lambda l: any(w in l for w in ("permission denied", "not permitted", "authentication failed", "no pg_hba"))
checks = {"secret files 0600/0640, root/service owner": all(m.split()[1] in ("root", "airflow", "superset")
                                                            and m.split()[2] in ("600", "400", "640") for m in t[:4]),
          "BI reader cannot CREATE": denied(t[4]), "BI reader cannot DELETE": denied(t[5]),
          "warehouse writer cannot reach airflow_meta": denied(t[6]),
          "warehouse writer cannot reach superset_meta": denied(t[7])}
print(json.dumps({"ok": all(checks.values()), "checks": checks, "raw": t}, indent=1))
PY
    if grep -q '"ok": true' "$OUT/s-privileges.json"; then ok "least privilege (all checks: s-privileges.json)"
    else echo "FINDING least privilege: $(python3 -c 'import json,sys; print([k for k,v in json.load(open(sys.argv[1]))["checks"].items() if not v])' "$OUT/s-privileges.json") - recorded, not fixed here" | tee -a "$OUT/steps.log"; fi
    ;;
  backup)
    state > "$OUT/state-at-backup.txt"
    t0=$(date +%s.%N)
    step b1-backup dpagent backup create /root/backup --schema fulfilment_sla --passphrase-env OPS_BACKUP_PASSPHRASE
    echo "backup_seconds=$(python3 -c "print(round($(date +%s.%N) - $t0, 1))")" | tee -a "$OUT/steps.log"
    ls /root/backup/*.enc > "$OUT/archive-name.txt"
    step b2-verify dpagent backup verify "$(cat "$OUT/archive-name.txt")" --passphrase-env OPS_BACKUP_PASSPHRASE
    ;;
  later)
    step l1-run-after-backup dpagent pipeline run fulfilment_sla --yes --wait --timeout 1800
    state > "$OUT/state-after-later-run.txt"
    ;;
  target)
    step t1-bi-validate $D bi-validate                      # installs Superset, like the source had
    state > "$OUT/state-before-restore.txt"
    ;;
  restore)
    ARCHIVE=$(ls /root/backup/*.enc)
    t0=$(date +%s.%N)
    step r1-restore dpagent backup restore "$ARCHIVE" --passphrase-env OPS_BACKUP_PASSPHRASE --yes
    health http://127.0.0.1:8090/health && health http://127.0.0.1:8088/health
    echo "rto_seconds=$(python3 -c "print(round($(date +%s.%N) - $t0, 1))") (restore start -> Airflow and Superset healthy)" | tee -a "$OUT/steps.log"
    systemctl is-active -q airflow-scheduler
    state > "$OUT/state-after-restore.txt"
    diff /root/ops-src/state-at-backup.txt "$OUT/state-after-restore.txt" \
      || { echo "the restored state differs from the state at backup time" >&2; exit 1; }
    ok "restored = state at backup time (journal runs, pipeline deployed+reviewed, DAG unpaused, dashboards, viewer, report rows)"
    grep -q "runs=$(runs)$" /root/ops-src/state-after-later-run.txt && { echo "the post-backup run is in the restore?!" >&2; exit 1; }
    ok "rpo (the run made after the backup is not in the restored host - data since the last backup is lost by design)"
    "$SPY" tests/quickstart_acceptance/check_viewer.py fulfil.viewer FS_VIEWER_PASSWORD fulfil-sla - > "$OUT/r-check-viewer.json"
    ok "viewer after restore (API: chart and filter = expected, writes refused)"
    dpagent pipeline status fulfilment_sla > "$OUT/r-status.log" 2>&1
    step r2-run-after-restore dpagent pipeline run fulfilment_sla --yes --wait --timeout 1800
    "$PY" examples/demo/check_fulfil.py "$EX" > "$OUT/r-check-run.json"
    ok "run on the restored host = expected"
    echo PASS | tee -a "$OUT/steps.log"
    ;;
esac
