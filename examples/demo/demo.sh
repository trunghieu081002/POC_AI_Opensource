#!/usr/bin/env bash
# DPAgent 3-layer demo (docs/demo.md), step by step, product commands only. Run as root (sudo -E) with the environment
# file loaded:   set -a; . examples/demo/dev.env; set +a;   bash examples/demo/demo.sh <step>
# Every step can be run again. Nothing here approves anything: `approve` and `bi-deploy` stop without a person's name
# (and, for BI, the fingerprint that person reviewed).
#   Layer 1  preflight   dpagent doctor against the demo stack (installs nothing)
#            install     install + verify + acceptance-test every pack of the stack
#            source      the demo source tables (sale_order, stock_picking) from the fulfil fixture
#   Layer 3  synth       DEMO_DRAFT=ai (default): the model drafts pipelines/fulfilment_sla from the BRD + verified
#                        schema, validated for real against the hand-written fixture/expected, re-asked at most
#                        DEMO_MAX_REVISIONS times within DEMO_MAX_CALLS calls. DEMO_DRAFT=reference: the hand-written
#                        reference instead (examples/demo/reference, labelled; never reported as the model's work)
#            validate    validate the draft again (sealed evidence) - e.g. after reading or editing it
#            approve     a person promotes it on that evidence (needs DEMO_APPROVED_BY)
#   Layer 2  deploy      deploy + run once and wait
#            check       the warehouse table vs the hand-calculated expected (independent psql check)
#            schedule    the pipeline's schedule, deployment and last run (dpagent pipeline list)
#   BI       bi-validate Superset + the fulfil-sla dashboard on throwaway clones; prints the fingerprint to review
#            bi-deploy   live dashboard + its viewer account (needs DEMO_BI_APPROVED_BY + DEMO_BI_APPROVED_SHA256)
#   Ops      bad-data    a repeated stock_picking.id in the source, then a run: it must stop at landing
#            fix-data    the source restored, then a run: it must recover
#            status      pipeline status, last run's audit, installed/proven packs
#            undeploy    the dashboard (project-scoped), then the pipeline
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
D=examples/demo
P=fulfilment_sla
CASE=tests/llm_verification/cases/fulfilment_sla
FX=$CASE/fixture.yaml
EX=$CASE/expected.yaml
PY="${DEMO_PY:-/opt/dpagent/.venv/bin/python}"
need() { for v in "$@"; do [ -n "${!v:-}" ] || { echo "set $v (see $D/dev.env.example)" >&2; exit 2; }; done; }
src_psql() { PGPASSWORD="$FS_SRC_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 -h "${FS_SRC_HOST:-localhost}" \
               -p "${FS_SRC_PORT:-5432}" -U "$FS_SRC_USER" -d "$FS_SRC_NAME" "$@"; }
origin_file() { echo "pipelines/$P/.draft-origin"; }

case "${1:-}" in
  preflight)
    dpagent doctor --spec "$D/stack.yaml"
    ;;
  install)
    need FS_SRC_NAME FS_SRC_USER FS_SRC_PASSWORD WAREHOUSE_DB_USER WAREHOUSE_DB_PASSWORD AIRFLOW_DB_PASSWORD \
         AIRFLOW_ADMIN_PASSWORD DLT_DB_PASSWORD SUPERSET_METADATA_PASSWORD
    dpagent doctor --spec "$D/stack.yaml"
    dpagent spec "$D/stack.yaml" --yes
    ;;
  source)
    need FS_SRC_NAME FS_SRC_USER FS_SRC_PASSWORD
    dpagent pipeline fixture-sql "$FX" --replace | src_psql
    ;;
  synth)
    if [ "${DEMO_DRAFT:-ai}" = reference ]; then
      # the labelled hand-written reference; replaced only if it is not already exactly this reference
      if [ -d "pipelines/$P" ] && ! diff -rq -x .draft-origin -x '.synth-*' -x '.approved.yaml' "$D/reference/$P" "pipelines/$P" >/dev/null 2>&1; then
        [ "${DEMO_REDRAFT:-0}" = 1 ] || { echo "pipelines/$P exists and is not the reference - DEMO_REDRAFT=1 to replace it" >&2; exit 2; }
      fi
      rm -rf "pipelines/$P"; cp -r "$D/reference/$P" "pipelines/$P"; rm -f "pipelines/$P/ORIGIN.md"
      echo "reference (hand-written, examples/demo/reference/$P) - not drafted by a model" > "$(origin_file)"
      dpagent pipeline validate "$P" --fixture "$FX" --expected "$EX"
    else
      need DPAGENT_MODEL
      over=(); [ "${DEMO_REDRAFT:-0}" = 1 ] && over=(--overwrite)
      dpagent pipeline synth "$P" --brd "$CASE/brd.md" --schema "$CASE/source_schema.md" \
        --secret "FS_SRC_HOST=source Postgres host" --secret "FS_SRC_PORT=source Postgres port" \
        --secret "FS_SRC_NAME=source database name" --secret "FS_SRC_USER=source database user" \
        --secret "FS_SRC_PASSWORD=source database password" \
        --hint "Source connection fields use the secret refs above. Use the standard warehouse refs." \
        --fixture "$FX" --expected "$EX" --max-revisions "${DEMO_MAX_REVISIONS:-2}" \
        --max-calls "${DEMO_MAX_CALLS:-6}" ${DEMO_TRACE_DIR:+--trace-dir "$DEMO_TRACE_DIR"} "${over[@]}"
      echo "model $DPAGENT_MODEL via dpagent pipeline synth (classification in the trace)" > "$(origin_file)"
    fi
    ;;
  validate)
    dpagent pipeline lint "$P"
    dpagent pipeline validate "$P" --fixture "$FX" --expected "$EX"
    ;;
  approve)
    need DEMO_APPROVED_BY
    echo "draft origin: $(cat "$(origin_file)" 2>/dev/null || echo unknown)"
    dpagent pipeline promote "$P" --fixture "$FX" --expected "$EX" --approved-by "$DEMO_APPROVED_BY" --yes
    ;;
  deploy)
    need FS_SRC_NAME FS_SRC_USER FS_SRC_PASSWORD WAREHOUSE_DB_USER WAREHOUSE_DB_PASSWORD
    dpagent pipeline deploy "$P" --yes
    dpagent pipeline run "$P" --yes --wait --timeout 1800
    ;;
  check)
    "$PY" "$D/check_fulfil.py" "$EX"
    ;;
  schedule)
    dpagent pipeline list
    ;;
  bi-validate)
    need SUPERSET_METADATA_PASSWORD SUPERSET_SECRET_KEY SUPERSET_ADMIN_PASSWORD SUPERSET_WAREHOUSE_PASSWORD WAREHOUSE_DB_USER
    DEMO_BI_MODE=validate dpagent spec "$D/bi.yaml" --yes --allow-draft
    python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("fulfil-sla", "status="+r["status"], "fingerprint="+r["fingerprint"])' \
      /var/lib/dpagent-bi-assets/fulfil-sla/validation.json
    echo "review /var/lib/dpagent-bi-assets/fulfil-sla/validation.json, then approve that fingerprint (bi-deploy)"
    ;;
  bi-deploy)
    need DEMO_BI_APPROVED_BY DEMO_BI_APPROVED_SHA256 FS_VIEWER_PASSWORD
    DEMO_BI_MODE=deploy dpagent spec "$D/bi.yaml" --yes --allow-draft --no-test
    DEMO_BI_MODE=deploy dpagent verify superset-assets
    ;;
  bad-data)
    need FS_SRC_NAME FS_SRC_USER FS_SRC_PASSWORD
    # a transfer id repeated by the source system: the BRD says the run must stop at landing
    src_psql -c "INSERT INTO stock_picking (id, sale_id, picking_type, state, date_done)
                 SELECT 101, 6, 'outgoing', 'done', TIMESTAMP '2026-02-03 08:00:00'
                 WHERE (SELECT count(*) FROM stock_picking WHERE id = 101) = 1"
    set +e; dpagent pipeline run "$P" --yes --wait --timeout 1800; rc=$?; set -e
    echo "run with a repeated source id: rc=$rc (must be non-zero)"
    [ "$rc" -ne 0 ] || { echo "the run did not stop on bad source data" >&2; exit 1; }
    ;;
  fix-data)
    need FS_SRC_NAME FS_SRC_USER FS_SRC_PASSWORD
    dpagent pipeline fixture-sql "$FX" --replace | src_psql
    dpagent pipeline run "$P" --yes --wait --timeout 1800
    ;;
  status)
    dpagent pipeline status "$P"
    dpagent pipeline audit
    dpagent status
    ;;
  undeploy)
    DEMO_BI_MODE=undeploy dpagent spec "$D/bi.yaml" --yes --allow-draft --no-test
    dpagent pipeline undeploy "$P" --yes
    ;;
  *)
    sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 2 ;;
esac
