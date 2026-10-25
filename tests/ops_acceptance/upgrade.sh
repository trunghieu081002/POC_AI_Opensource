#!/usr/bin/env bash
# Runs INSIDE a demo host built from the PREVIOUS release (scripts/upgrade-acceptance-ci.sh). Phases:
#   baseline : approval stand-in, deploy + run, dashboard + viewer on the old release; record the state
#   backup   : `dpagent backup create` with the NEW release's code (the old release has no backup command)
#   upgrade  : bootstrap from the new source over /opt/dpagent, `dpagent spec` with the same stack -> checks
#   rollback : bootstrap from the old source, `dpagent backup restore` -> checks
# The new/old sources are at /opt/src/new and /opt/src/old (git archives copied in by the runner).
set -euo pipefail
PHASE="${1:?baseline|backup|upgrade|rollback}"
cd /opt/dpagent
OUT=/root/upgrade-evidence; mkdir -p "$OUT"
PY=/opt/dpagent/.venv/bin/python
SPY=/opt/superset/.venv/bin/python
EX=tests/llm_verification/cases/fulfilment_sla/expected.yaml
D="bash examples/demo/demo.sh"
set -a; . /root/demo-dev.env; set +a
step() { local label="$1"; shift; echo "== $label: $*" | tee -a "$OUT/steps.log"
         set +e; "$@" > "$OUT/$label.log" 2>&1; local rc=$?; set -e
         echo "$label rc=$rc" | tee -a "$OUT/steps.log"; [ "$rc" -eq 0 ] || { tail -40 "$OUT/$label.log"; exit "$rc"; }; }
ok() { echo "check $1 ok" | tee -a "$OUT/steps.log"; }
fp() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' /var/lib/dpagent-bi-assets/fulfil-sla/validation.json; }
health() { for _ in $(seq 1 90); do curl -fsS --noproxy '*' -o /dev/null "$1" 2>/dev/null && return 0; sleep 2; done; return 1; }
works() {   # works LABEL: the pipeline is still approved and runs = expected; the viewer still sees the expected rows
  health http://127.0.0.1:8090/health && health http://127.0.0.1:8088/health
  dpagent --version > "$OUT/$1-version.txt"
  dpagent pipeline list > "$OUT/$1-list.txt" 2>&1; grep fulfilment_sla "$OUT/$1-list.txt" | grep -q reviewed
  step "$1-run" dpagent pipeline run fulfilment_sla --yes --wait --timeout 1800
  "$PY" examples/demo/check_fulfil.py "$EX" > "$OUT/$1-check-run.json"
  "$SPY" tests/quickstart_acceptance/check_viewer.py fulfil.viewer FS_VIEWER_PASSWORD fulfil-sla - > "$OUT/$1-check-viewer.json"
  ok "$1 ($(cat "$OUT/$1-version.txt"): pipeline still reviewed, run = expected, viewer = expected)"
}
bootstrap_from() {
  DPAGENT_SOURCE_DIR="$1" bash "$1/scripts/bootstrap.sh"
}
runs() { "$PY" -c 'import sqlite3; print(sqlite3.connect("file:/var/lib/dpagent/dpagent.db?mode=ro", uri=True).execute("SELECT count(*) FROM runs").fetchone()[0])'; }

case "$PHASE" in
  baseline)
    DEMO_APPROVED_BY=upgrade.test step b1-approve $D approve
    step b2-deploy $D deploy
    step b3-bi-validate $D bi-validate
    DEMO_BI_APPROVED_BY=upgrade.test DEMO_BI_APPROVED_SHA256="$(fp)" step b4-bi-deploy $D bi-deploy
    works old-baseline
    ;;
  backup)
    echo "$(runs)" > "$OUT/runs-at-backup.txt"
    step k1-backup env PYTHONPATH=/opt/src/new/src "$PY" -m dpagent backup create /root/backup --schema fulfilment_sla \
      --passphrase-env OPS_BACKUP_PASSPHRASE
    ;;
  upgrade)
    step u1-bootstrap-new bootstrap_from /opt/src/new
    step u2-spec dpagent spec examples/demo/stack.yaml --yes
    works upgraded
    "$PY" -c 'import sqlite3; c=sqlite3.connect("file:/var/lib/dpagent/dpagent.db?mode=ro", uri=True); print(c.execute("PRAGMA user_version").fetchone()[0], [r[1] for r in c.execute("PRAGMA table_info(runs)")])' \
      > "$OUT/upgraded-journal-schema.txt"
    ok "journal kept every run ($(runs) runs; $(cat "$OUT/runs-at-backup.txt") at backup)"
    ;;
  rollback)
    step r1-bootstrap-old bootstrap_from /opt/src/old
    step r2-restore env PYTHONPATH=/opt/src/new/src "$PY" -m dpagent backup restore "$(ls /root/backup/*.tar.enc)" \
      --passphrase-env OPS_BACKUP_PASSPHRASE --yes
    [ "$(runs)" = "$(cat "$OUT/runs-at-backup.txt")" ] || { echo "the journal is not the one at backup" >&2; exit 1; }
    works rolled-back
    echo PASS | tee -a "$OUT/steps.log"
    ;;
esac
