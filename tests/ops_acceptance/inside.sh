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
