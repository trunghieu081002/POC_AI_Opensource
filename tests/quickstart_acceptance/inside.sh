#!/usr/bin/env bash
# Runs INSIDE the disposable host, from the clean source copy at /opt/dpagent (scripts/quickstart-acceptance-ci.sh).
# Plays the operator of docs/quickstart.md: fills an environment file, runs examples/quickstart/walkthrough.sh
# step by step (product commands only), and reads values a person would read (the BI fingerprints). After each
# milestone an independent check (tests/quickstart_acceptance/check*.py) compares with hand-calculated values.
#   inside.sh first    install .. BI deploy with viewer accounts, viewer API checks
#   inside.sh second   (after the host's viewer browser check) update, rerun, status, undeploy
set -euo pipefail
PHASE="${1:?first|second}"
cd /opt/dpagent
OUT=/root/quickstart-evidence
mkdir -p "$OUT"
W=examples/quickstart/walkthrough.sh
PY=/opt/dpagent/.venv/bin/python
SPY=/opt/superset/.venv/bin/python

# --- the operator's environment file (random secrets; never written to evidence)
ENV=/root/quickstart-dev.env
if [ ! -f "$ENV" ]; then
  umask 077
  cp examples/quickstart/dev.env.example "$ENV"
  for key in QS_SRC_PASSWORD QS_WH_PASSWORD AIRFLOW_DB_PASSWORD AIRFLOW_ADMIN_PASSWORD DLT_DB_PASSWORD \
             SUPERSET_METADATA_PASSWORD SUPERSET_ADMIN_PASSWORD SUPERSET_WAREHOUSE_PASSWORD \
             QS_SALES_VIEWER_PASSWORD QS_OPS_VIEWER_PASSWORD; do
    sed -i "s/^${key}=.*/${key}=$(openssl rand -hex 16)/" "$ENV"
  done
  sed -i -e "s/^SUPERSET_SECRET_KEY=.*/SUPERSET_SECRET_KEY=$(openssl rand -hex 32)/" \
         -e "s/^QS_APPROVED_BY=.*/QS_APPROVED_BY=quickstart.reviewer/" "$ENV"
  umask 022
fi
grep -q change-me "$ENV" && { echo "unfilled value in the environment file" >&2; exit 2; }
set -a; . "$ENV"; set +a
scan_secrets() {   # never keep evidence that contains a secret value from the environment file (any exit)
  local rc=$?
  while IFS='=' read -r key value; do
    case "$key" in *PASSWORD*|*SECRET*) ;; *) continue ;; esac
    [ ${#value} -ge 8 ] || continue
    if grep -rqF -- "$value" "$OUT"; then
      echo "secret $key found in evidence - evidence removed" >&2; rm -rf "$OUT"; exit 99
    fi
  done < "$ENV"
  echo "secret scan ($PHASE): no environment-file secret value in evidence" >> "$OUT/steps.log"
  exit "$rc"
}
trap scan_secrets EXIT

step() {   # step <label> <command...>: run, keep its full log and exit code, stop on failure
  local label="$1"; shift
  echo "== $label: $*" | tee -a "$OUT/steps.log"
  set +e; "$@" > "$OUT/$label.log" 2>&1; local rc=$?; set -e
  echo "$label rc=$rc" | tee -a "$OUT/steps.log"
  [ "$rc" -eq 0 ] || { tail -40 "$OUT/$label.log"; exit "$rc"; }
}
check() {  # check <label> <script> <args...>   (Superset's venv: it has requests and PyYAML)
  local label="$1" script="$2"; shift 2
  "$SPY" "tests/quickstart_acceptance/$script" "$@" > "$OUT/check-$label.json" 2>&1 \
    || { echo "CHECK FAILED: $label"; cat "$OUT/check-$label.json"; exit 1; }
  echo "check $label ok" | tee -a "$OUT/steps.log"
}
fingerprint() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' \
                  "/var/lib/dpagent-bi-assets/$1/validation.json"; }
meta() { runuser -u postgres -- psql -X -At -v ON_ERROR_STOP=1 -d superset_meta -c "$1"; }
superset_state() {   # users (roles, password hash digest, active), roles (permission count), asset change times, receipts
  meta "SELECT x FROM (
      SELECT 'user '||u.username||' active='||u.active||' pw='||md5(coalesce(u.password,''))||' roles='||
             coalesce((SELECT string_agg(r.name, ',' ORDER BY r.name) FROM ab_user_role ur JOIN ab_role r ON r.id=ur.role_id
                       WHERE ur.user_id=u.id),'') AS x FROM ab_user u
      UNION ALL SELECT 'role '||r.name||' perms='||(SELECT count(*) FROM ab_permission_view_role pr WHERE pr.role_id=r.id) FROM ab_role r
      UNION ALL SELECT 'dashboard '||uuid||' '||changed_on FROM dashboards
      UNION ALL SELECT 'chart '||uuid||' '||changed_on FROM slices
      UNION ALL SELECT 'dataset '||uuid||' '||changed_on FROM tables) s ORDER BY x"
  for f in /var/lib/dpagent-bi-assets/*/deployment.json; do [ -f "$f" ] && sha256sum "$f"; done; true
}
refused_unchanged() {   # refused_unchanged <label> <command...>: must fail and leave Superset + receipts byte-identical
  local label="$1"; shift
  superset_state > "$OUT/$label-state-before.txt"
  set +e; COLUMNS=400 "$@" > "$OUT/$label.log" 2>&1; local rc=$?; set -e   # wide: the reason stays on one line
  superset_state > "$OUT/$label-state-after.txt"
  echo "$label rc=$rc (must be non-zero)" | tee -a "$OUT/steps.log"
  [ "$rc" -ne 0 ] || { echo "$label: was not refused" >&2; exit 1; }
  cmp -s "$OUT/$label-state-before.txt" "$OUT/$label-state-after.txt" \
    || { diff "$OUT/$label-state-before.txt" "$OUT/$label-state-after.txt" | head -20; echo "$label: state changed" >&2; exit 1; }
  echo "check $label-state-unchanged ok" | tee -a "$OUT/steps.log"
}
SALES_TITLE="Quickstart sales: completed orders and revenue by month"
SALES_TITLE_V2="Quickstart sales (v2): completed orders and revenue by month"
export QS_BI_APPROVED_BY=quickstart.reviewer

if [ "$PHASE" = first ]; then
  step 01-install     bash "$W" install
  step 02-source      bash "$W" source
  step 03-validate    bash "$W" validate
  step 04-approve     bash "$W" approve
  step 05-deploy      bash "$W" deploy
  "$PY" tests/quickstart_acceptance/check.py warehouse > "$OUT/check-after-first-run.json" \
    || { cat "$OUT/check-after-first-run.json"; exit 1; }
  echo "check after-first-run ok" | tee -a "$OUT/steps.log"

  step 06-bi-validate bash "$W" bi-validate
  fingerprint quickstart-sales > "$OUT/fingerprint-sales-v1.txt"
  fingerprint quickstart-ops > "$OUT/fingerprint-ops-v1.txt"

  # negative: fingerprints nobody reviewed must not deploy anything (or create any viewer)
  set +e
  QS_BI_APPROVED_SHA256=$(printf '0%.0s' $(seq 64)) QS_OPS_BI_APPROVED_SHA256=$(printf '1%.0s' $(seq 64)) \
    bash "$W" bi-deploy > "$OUT/07-bi-deploy-wrong-fingerprint.log" 2>&1
  rc=$?; set -e
  echo "07-bi-deploy-wrong-fingerprint rc=$rc (must be non-zero)" | tee -a "$OUT/steps.log"
  [ "$rc" -ne 0 ] || { echo "a wrong fingerprint deployed" >&2; exit 1; }
  check after-refused-deploy-sales check.py bi-absent quickstart-sales
  check after-refused-deploy-ops check.py bi-absent quickstart-ops
  n=$(runuser -u postgres -- psql -X -At -d superset_meta -c "SELECT count(*) FROM ab_user WHERE username LIKE 'qs.%'")
  [ "$n" = 0 ] || { echo "viewer accounts exist after a refused deploy" >&2; exit 1; }

  export QS_BI_APPROVED_SHA256="$(cat "$OUT/fingerprint-sales-v1.txt")" QS_OPS_BI_APPROVED_SHA256="$(cat "$OUT/fingerprint-ops-v1.txt")"
  # negative: a role with the project's name that no receipt records (someone else's) is refused before any import
  meta "INSERT INTO ab_role (id, name) VALUES (nextval('ab_role_id_seq'), 'dpagent-viewer-quickstart-sales')" >/dev/null
  refused_unchanged 07b-bi-deploy-squatted-role bash "$W" bi-deploy
  grep -q "Viewer role not owned by this project" "$OUT/07b-bi-deploy-squatted-role.log" || { echo "refused for another reason" >&2; exit 1; }
  check after-squatted-role-sales check.py bi-absent quickstart-sales
  check after-squatted-role-ops check.py bi-absent quickstart-ops
  meta "DELETE FROM ab_role WHERE name = 'dpagent-viewer-quickstart-sales'" >/dev/null

  step 08-bi-deploy bash "$W" bi-deploy
  check after-bi-deploy check.py bi "$SALES_TITLE"
  cp "$OUT/check-after-bi-deploy.json" "$OUT/ids-v1.json"

  # viewer RBAC through the API, as each viewer (never admin)
  check viewer-sales-api check_viewer.py qs.sales.viewer QS_SALES_VIEWER_PASSWORD quickstart-sales quickstart-ops
  check viewer-ops-api   check_viewer.py qs.ops.viewer   QS_OPS_VIEWER_PASSWORD   quickstart-ops   quickstart-sales
  # the viewers' write attempts changed nothing
  check after-viewer-attempts check.py bi "$SALES_TITLE"

  # negative: someone else adds a member to a project's viewer role -> redeploy refused, nothing changed
  meta "INSERT INTO ab_user (id, first_name, last_name, username, email, active, created_on, changed_on)
        VALUES (nextval('ab_user_id_seq'), 'Foreign', 'Member', 'foreign.member', 'foreign.member@example.invalid', true, now(), now());
        INSERT INTO ab_user_role (id, user_id, role_id) SELECT nextval('ab_user_role_id_seq'), u.id, r.id FROM ab_user u, ab_role r
        WHERE u.username = 'foreign.member' AND r.name = 'dpagent-viewer-quickstart-ops'" >/dev/null
  refused_unchanged 08b-bi-redeploy-foreign-member bash "$W" bi-deploy
  grep -q "Viewer role has foreign members" "$OUT/08b-bi-redeploy-foreign-member.log" || { echo "refused for another reason" >&2; exit 1; }
  # ... and an owned viewer that someone granted another role is neither stripped nor deleted
  meta "DELETE FROM ab_user_role WHERE user_id = (SELECT id FROM ab_user WHERE username = 'foreign.member');
        DELETE FROM ab_user WHERE username = 'foreign.member';
        INSERT INTO ab_user_role (id, user_id, role_id) SELECT nextval('ab_user_role_id_seq'), u.id, r.id FROM ab_user u, ab_role r
        WHERE u.username = 'qs.ops.viewer' AND r.name = 'Alpha'" >/dev/null
  refused_unchanged 08c-bi-redeploy-viewer-extra-role bash "$W" bi-deploy
  grep -q "Viewer account holds another role" "$OUT/08c-bi-redeploy-viewer-extra-role.log" || { echo "refused for another reason" >&2; exit 1; }
  meta "DELETE FROM ab_user_role WHERE user_id = (SELECT id FROM ab_user WHERE username = 'qs.ops.viewer')
        AND role_id = (SELECT id FROM ab_role WHERE name = 'Alpha')" >/dev/null
  check after-refusals check.py bi "$SALES_TITLE"
  echo "PHASE first PASS" | tee -a "$OUT/steps.log"
  exit 0
fi

# ---------------------------------------------------------------- second phase
# update: a reviewed content change -> new revision -> validate -> new fingerprint -> deploy, same identities
sed -i "s/^dashboard_title: .*/dashboard_title: '$SALES_TITLE_V2'/" projects/quickstart-sales/bundle/dashboards/overview.yaml
export QS_BI_REVISION=v2
step 09-bi-validate-v2 bash "$W" bi-validate
fingerprint quickstart-sales > "$OUT/fingerprint-sales-v2.txt"
fingerprint quickstart-ops > "$OUT/fingerprint-ops-v2.txt"
cmp -s "$OUT/fingerprint-sales-v1.txt" "$OUT/fingerprint-sales-v2.txt" && { echo "content change kept the fingerprint" >&2; exit 1; }
cmp -s "$OUT/fingerprint-ops-v1.txt" "$OUT/fingerprint-ops-v2.txt" || { echo "an unchanged project changed fingerprint" >&2; exit 1; }
QS_BI_APPROVED_SHA256="$(cat "$OUT/fingerprint-sales-v2.txt")" QS_OPS_BI_APPROVED_SHA256="$(cat "$OUT/fingerprint-ops-v2.txt")" \
  step 10-bi-deploy-v2 bash "$W" bi-deploy
check after-update check.py bi "$SALES_TITLE_V2"
python3 - "$OUT/ids-v1.json" "$OUT/check-after-update.json" <<'PY' | tee -a "$OUT/steps.log"
import json, sys
a, b = (json.load(open(p))["ids"] for p in sys.argv[1:])
assert a == b, f"identities changed on update: {a} -> {b}"
print("check identities-stable-on-update ok", a)
PY
check viewer-sales-api-after-update check_viewer.py qs.sales.viewer QS_SALES_VIEWER_PASSWORD quickstart-sales quickstart-ops

step 11-rerun       bash "$W" rerun
"$PY" tests/quickstart_acceptance/check.py warehouse > "$OUT/check-after-rerun.json" || { cat "$OUT/check-after-rerun.json"; exit 1; }
echo "check after-rerun (no duplicates) ok" | tee -a "$OUT/steps.log"
check after-rerun-bi check.py bi "$SALES_TITLE_V2"

step 12-status      bash "$W" status
grep -q quickstart_sales "$OUT/12-status.log"

step 13-undeploy    bash "$W" undeploy
check after-undeploy-sales check.py bi-absent quickstart-sales
check after-undeploy-ops check.py bi-absent quickstart-ops
v=$(runuser -u postgres -- psql -X -At -d superset_meta -c \
  "SELECT (SELECT count(*) FROM ab_user WHERE username LIKE 'qs.%') + (SELECT count(*) FROM ab_role WHERE name LIKE 'dpagent-viewer-%')")
[ "$v" = 0 ] || { echo "viewer accounts/roles remain after undeploy ($v)" >&2; exit 1; }
if su airflow -s /bin/bash -c "AIRFLOW_HOME=/opt/airflow/home /opt/airflow/.venv/bin/airflow dags list 2>/dev/null" | grep -q quickstart_sales; then
  echo "DAG still present after undeploy" >&2; exit 1; fi
# (the source tree is NOT removed when dpagent runs from /opt/dpagent: published == source there, by design)
MODELS="$("$PY" -c 'from dpagent.pipelines import deploy; print(deploy._dbt_project_dir())')/models/quickstart_sales"
[ ! -e "$MODELS" ] || { echo "published dbt models still present: $MODELS" >&2; exit 1; }
if grep -q '^QS_' /opt/airflow/home/pipelines.env 2>/dev/null; then echo "pipeline secrets not released" >&2; exit 1; fi
[ -d pipelines/quickstart_sales ] || { echo "undeploy removed the operator's source tree" >&2; exit 1; }
curl -fsS --noproxy '*' -o /dev/null http://127.0.0.1:8088/health
echo "check scoped-undeploy ok (DAG, dbt models, secrets, BI assets and viewer accounts gone; source kept; Superset up)" | tee -a "$OUT/steps.log"
echo PASS | tee -a "$OUT/steps.log"
