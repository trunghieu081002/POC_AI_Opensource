#!/usr/bin/env bash
# The quickstart, step by step, using product commands only (docs/quickstart.md). Run as root (sudo -E) from
# anywhere, with the environment file loaded:   set -a; . examples/quickstart/dev.env; set +a
#   bash examples/quickstart/walkthrough.sh <step>
# Steps, in order:
#   install      doctor + install the data stack
#   source       create the demo source tables (a real project points at its real source instead)
#   validate     prove the pipeline on a throwaway source/warehouse against the hand-written expected
#   approve      promote on that sealed evidence (needs QS_APPROVED_BY: a person)
#   deploy       deploy for real, run once and wait
#   bi-validate  install Superset, validate the dashboard on throwaway clones, print its fingerprint
#   bi-deploy    deploy both dashboards and their viewer accounts (needs QS_BI_APPROVED_BY and the reviewed
#                fingerprints QS_BI_APPROVED_SHA256 / QS_OPS_BI_APPROVED_SHA256)
#   rerun        run the pipeline again (full snapshot: no duplicates)
#   status       pipeline status, the last run's audit, installed/proven packs, the BI deployment check
#   undeploy     remove the dashboard (project-scoped), then the pipeline
# Nothing here approves anything by itself: `approve` and `bi-deploy` refuse without the person and, for
# BI, the fingerprint the operator read from `bi-validate`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
Q=examples/quickstart
P=quickstart_sales
FX=pipelines/$P/fixture.yaml
EX=pipelines/$P/expected.yaml
need() { for v in "$@"; do [ -n "${!v:-}" ] || { echo "set $v (see $Q/dev.env.example)" >&2; exit 2; }; done; }

case "${1:-}" in
  install)
    need QS_WH_USER QS_WH_PASSWORD QS_SRC_NAME QS_SRC_USER QS_SRC_PASSWORD AIRFLOW_DB_PASSWORD AIRFLOW_ADMIN_PASSWORD DLT_DB_PASSWORD SUPERSET_METADATA_PASSWORD
    dpagent doctor --spec "$Q/stack.yaml"
    dpagent spec "$Q/stack.yaml" --yes
    ;;
  source)
    need QS_SRC_NAME QS_SRC_USER QS_SRC_PASSWORD
    dpagent pipeline fixture-sql "$FX" \
      | PGPASSWORD="$QS_SRC_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 -h "${QS_SRC_HOST:-localhost}" \
          -p "${QS_SRC_PORT:-5432}" -U "$QS_SRC_USER" -d "$QS_SRC_NAME"
    ;;
  validate)
    dpagent pipeline lint "$P"
    dpagent pipeline validate "$P" --fixture "$FX" --expected "$EX"
    ;;
  approve)
    need QS_APPROVED_BY
    dpagent pipeline promote "$P" --fixture "$FX" --expected "$EX" --approved-by "$QS_APPROVED_BY" --yes
    ;;
  deploy)
    need QS_SRC_NAME QS_SRC_USER QS_SRC_PASSWORD QS_WH_USER QS_WH_PASSWORD
    dpagent pipeline deploy "$P" --yes
    dpagent pipeline run "$P" --yes --wait --timeout 1800
    ;;
  bi-validate)
    need SUPERSET_METADATA_PASSWORD SUPERSET_SECRET_KEY SUPERSET_ADMIN_PASSWORD SUPERSET_WAREHOUSE_PASSWORD QS_WH_USER
    QS_BI_MODE=validate dpagent spec "$Q/bi.yaml" --yes --allow-draft
    for project in quickstart-sales quickstart-ops; do
      echo "review /var/lib/dpagent-bi-assets/$project/validation.json, then approve its fingerprint:"
      python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(sys.argv[2], "status="+r["status"], "fingerprint="+r["fingerprint"])' \
        "/var/lib/dpagent-bi-assets/$project/validation.json" "$project"
    done
    ;;
  bi-deploy)
    need QS_BI_APPROVED_BY QS_BI_APPROVED_SHA256 QS_OPS_BI_APPROVED_SHA256 QS_SALES_VIEWER_PASSWORD QS_OPS_VIEWER_PASSWORD
    # --no-test: the runtime and assets suites already ran at bi-validate; the deployment itself is checked
    # by the pack (it revalidates before touching live assets) and by `verify` below.
    QS_BI_MODE=deploy dpagent spec "$Q/bi.yaml" --yes --allow-draft --no-test
    QS_BI_MODE=deploy dpagent verify superset-assets
    ;;
  rerun)
    dpagent pipeline run "$P" --yes --wait --timeout 1800
    ;;
  status)
    dpagent pipeline status "$P"
    dpagent pipeline audit
    dpagent status
    ;;
  undeploy)
    QS_BI_MODE=undeploy dpagent spec "$Q/bi.yaml" --yes --allow-draft --no-test
    dpagent pipeline undeploy "$P" --yes
    ;;
  *)
    sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
