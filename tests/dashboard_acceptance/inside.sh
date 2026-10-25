#!/usr/bin/env bash
# Runs INSIDE a host where the project's gold table holds the fixture's data and Superset runs: a demo host
# (fulfil_sla) or a quickstart host (sales_revenue) kept after its acceptance (scripts/dashboard-acceptance-ci.sh).
# Plays the operator of docs/ai-dashboards.md with product commands, checks each milestone independently:
#   draft (DASH_DRAFT=scripted: a hand-written spec as the reply | ai: the model named by DPAGENT_MODEL, within the
#   ledger) -> superset-assets validate on throwaway clones -> deploy refused with an unreviewed fingerprint ->
#   deploy with the reviewed one + viewer -> viewer API = expected -> redeploy: same objects, no duplicates ->
#   update (v2): new fingerprint, the old one refused, same UUIDs/IDs, new title -> undeploy: only this project's
#   objects gone, everything else in Superset unchanged.
#   inside.sh CASE PROJECT FLAVOR(demo|quickstart) [phase: before-browser|after-browser]
set -euo pipefail
CASE="${1:?case}"; PROJECT="${2:?project}"; FLAVOR="${3:?demo|quickstart}"; PHASE="${4:-before-browser}"
DRAFT="${DASH_DRAFT:-scripted}"; export CASE
cd /opt/dpagent
OUT=/root/dashboard-evidence/$PROJECT; mkdir -p "$OUT"
PY=/opt/dpagent/.venv/bin/python
SPY=/opt/superset/.venv/bin/python
C=tests/llm_verification/dashboard_cases/$CASE
case "$FLAVOR" in
  demo) ENV=/root/demo-dev.env; TEMPLATE=examples/demo/bi.yaml ;;
  quickstart) ENV=/root/quickstart-dev.env; TEMPLATE=examples/quickstart/bi.yaml ;;
  *) echo "flavor: demo|quickstart" >&2; exit 2 ;;
esac
set -a; . "$ENV"; set +a
grep -q '^DASH_VIEWER_PASSWORD=' "$ENV" || { umask 077; echo "DASH_VIEWER_PASSWORD=$(openssl rand -hex 16)" >> "$ENV"; umask 022; }
set -a; . "$ENV"; set +a
SPEC=/root/dash-bi-$PROJECT.yaml
step() { local label="$1"; shift; echo "== $label: $*" | tee -a "$OUT/steps.log"
         set +e; "$@" > "$OUT/$label.log" 2>&1; local rc=$?; set -e
         echo "$label rc=$rc" | tee -a "$OUT/steps.log"; [ "$rc" -eq 0 ] || { tail -40 "$OUT/$label.log"; exit "$rc"; }; }
ok() { echo "check $1 ok" | tee -a "$OUT/steps.log"; }
meta() { runuser -u postgres -- psql -X -At -v ON_ERROR_STOP=1 -d superset_meta -c "$1"; }
fp() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' "/var/lib/dpagent-bi-assets/$PROJECT/validation.json"; }
uuids() { "$PY" - "$PROJECT" <<'PY'
import sys, yaml
from pathlib import Path
b = Path("projects") / sys.argv[1] / "bundle"
print(" ".join(sorted(f"'{yaml.safe_load(p.read_text())['uuid']}'" for p in b.rglob("*.yaml") if p.name != "metadata.yaml")))
PY
}
owned() {   # this project's objects in Superset: kind:id:uuid, by the bundle's UUIDs
  local u; u=$(uuids | tr ' ' ',')
  meta "SELECT 'db:'||id||':'||uuid FROM dbs WHERE uuid::text IN ($u) UNION ALL SELECT 'ds:'||id||':'||uuid FROM tables WHERE uuid::text IN ($u)
        UNION ALL SELECT 'chart:'||id||':'||uuid FROM slices WHERE uuid::text IN ($u) UNION ALL SELECT 'dash:'||id||':'||uuid FROM dashboards WHERE uuid::text IN ($u) ORDER BY 1"
}
others() {  # everything else in Superset (must never change)
  local u; u=$(uuids | tr ' ' ',')
  meta "SELECT (SELECT count(*) FROM dbs WHERE uuid::text NOT IN ($u))||' '||(SELECT count(*) FROM tables WHERE uuid::text NOT IN ($u))||' '||
        (SELECT count(*) FROM slices WHERE uuid::text NOT IN ($u))||' '||(SELECT count(*) FROM dashboards WHERE uuid::text NOT IN ($u))"
}
bi() {   # bi MODE [approved-sha]: the superset-assets pack through `dpagent spec`
  local mode=$1 sha=${2:-}
  if [ "$mode" = validate ]; then DASH_MODE=validate dpagent spec "$SPEC" --yes --allow-draft
  else DASH_MODE=$mode DASH_APPROVED_BY="${DASH_APPROVED_BY:-dash.reviewer}" DASH_APPROVED_SHA256="$sha" \
         dpagent spec "$SPEC" --yes --allow-draft --no-test; fi
}

if [ "$PHASE" = before-browser ]; then
  # ---- 1. the draft: request + verified gold schema + KPI definitions -> spec -> rendered bundle
  synth_args=(dashboard synth "$PROJECT" --request "$C/request.md" --schema "$C/gold_schema.yaml" --kpis "$C/kpis.yaml"
              --expected "$C/expected.yaml" --trace-dir "$OUT/trace-v1" --overwrite
              --max-calls "${DASH_MAX_CALLS:-4}" --max-revisions "${DASH_MAX_REVISIONS:-2}"
              --transient-retries "${DASH_TRANSIENT_RETRIES:-0}")
  if [ "$DRAFT" = scripted ]; then
    step 01-synth "$PY" tests/dashboard_acceptance/scripted_synth.py "tests/dashboard_acceptance/scripted/$CASE.json" -- "${synth_args[@]}"
    echo "scripted: the reply was the hand-written spec tests/dashboard_acceptance/scripted/$CASE.json - not a model" > "$OUT/draft-origin.txt"
  else
    step 01-synth dpagent "${synth_args[@]}"
    echo "model $DPAGENT_MODEL via dpagent dashboard synth" > "$OUT/draft-origin.txt"
  fi
  cp -r "projects/$PROJECT" "$OUT/project-v1"
  # ---- 2. the BI spec: the host's own superset component unchanged, this one project with its viewer
  "$PY" - "$TEMPLATE" "$PROJECT" "$SPEC" <<'PY'
import sys, yaml
tpl, project, out = sys.argv[1:]
doc = yaml.safe_load(open(tpl))
for comp in doc["components"]:
    if "superset-assets" in comp:
        a = comp["superset-assets"]
        a["mode"], a["approved_by"] = "${DASH_MODE:-validate}", "${DASH_APPROVED_BY:-}"
        a["projects"] = [{"name": project, "revision": "${DASH_REVISION:-v1}", "bundle_dir": f"projects/{project}/bundle",
                          "expected_file": f"projects/{project}/expected.yaml", "approved_sha256": "${DASH_APPROVED_SHA256:-}",
                          "viewers": [{"username": "dash.viewer", "password_env": "DASH_VIEWER_PASSWORD"}]}]
doc["project"] = f"dashboard-{project}"
open(out, "w").write(yaml.safe_dump(doc, sort_keys=False))
PY
  others > "$OUT/others-before.txt"
  step 02-validate bi validate
  fp > "$OUT/fingerprint-v1.txt"
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); assert r["status"]=="pass" and r["cleanup"]=="pass", (r["status"], r["cleanup"])' \
    "/var/lib/dpagent-bi-assets/$PROJECT/validation.json"
  ok "validated-on-clones (chart and filter = the reviewer's independent SQL and rows)"
  [ -z "$(owned)" ] || { echo "validation left live objects" >&2; exit 1; }
  set +e; bi deploy "$(printf '0%.0s' $(seq 64))" > "$OUT/03a-deploy-unreviewed.log" 2>&1; rc=$?; set -e
  echo "03a-deploy-unreviewed rc=$rc (want non-zero)" | tee -a "$OUT/steps.log"
  [ "$rc" -ne 0 ] && [ -z "$(owned)" ] || { echo "an unreviewed fingerprint deployed something" >&2; exit 1; }
  ok "unreviewed-fingerprint-refused (nothing in Superset)"
  step 03-deploy bi deploy "$(cat "$OUT/fingerprint-v1.txt")"
  owned > "$OUT/owned-v1.txt"; [ "$(wc -l < "$OUT/owned-v1.txt")" = "$(uuids | wc -w)" ]
  "$SPY" tests/quickstart_acceptance/check_viewer.py dash.viewer DASH_VIEWER_PASSWORD "$PROJECT" - > "$OUT/check-viewer-api.json" \
    || { cat "$OUT/check-viewer-api.json"; exit 1; }
  ok "viewer-api (as dash.viewer: chart and filter = expected, every write refused)"
  echo "PHASE before-browser PASS" | tee -a "$OUT/steps.log"
  exit 0
fi

# ---- after the browser check: redeploy, update, undeploy
step 04-redeploy bi deploy "$(cat "$OUT/fingerprint-v1.txt")"
owned > "$OUT/owned-after-redeploy.txt"
diff "$OUT/owned-v1.txt" "$OUT/owned-after-redeploy.txt" || { echo "redeploy changed or duplicated objects" >&2; exit 1; }
ok "redeploy (same objects, same IDs, no duplicates)"

if [ "$DRAFT" = scripted ]; then
  step 05-synth-v2 "$PY" tests/dashboard_acceptance/scripted_synth.py "tests/dashboard_acceptance/scripted/$CASE-v2.json" -- \
    dashboard synth "$PROJECT" --request "$C/request.md" --schema "$C/gold_schema.yaml" --kpis "$C/kpis.yaml" \
    --expected "$C/expected.yaml" --trace-dir "$OUT/trace-v2" --overwrite
else   # an update by a person to the accepted spec: the title only, no new model request
  "$PY" - "$PROJECT" <<'PY'
import json, sys
from pathlib import Path
from dpagent.dashboards import synth as dash
p = sys.argv[1]; root = Path("projects") / p
spec = json.loads((root / "spec.json").read_text()); spec["title"] += " (v2)"
c = Path("tests/llm_verification/dashboard_cases") / __import__("os").environ["CASE"]
inp = dash.load_inputs(p, c / "request.md", c / "gold_schema.yaml", c / "kpis.yaml", c / "expected.yaml")
assert not dash.check_spec(inp, spec)
dash.write_project(inp, spec, Path("projects"), expected=c / "expected.yaml", overwrite=True,
                   origin=(root / "ORIGIN.md").read_text() + "\nv2: title changed by a person (spec.json), re-rendered.\n")
PY
fi
export DASH_REVISION=v2
step 06-validate-v2 bi validate
fp > "$OUT/fingerprint-v2.txt"
[ "$(cat "$OUT/fingerprint-v1.txt")" != "$(cat "$OUT/fingerprint-v2.txt")" ] || { echo "v2 has the v1 fingerprint" >&2; exit 1; }
set +e; bi deploy "$(cat "$OUT/fingerprint-v1.txt")" > "$OUT/07a-deploy-stale.log" 2>&1; rc=$?; set -e
echo "07a-deploy-stale rc=$rc (want non-zero)" | tee -a "$OUT/steps.log"; [ "$rc" -ne 0 ]
step 07-deploy-v2 bi deploy "$(cat "$OUT/fingerprint-v2.txt")"
owned > "$OUT/owned-v2.txt"
diff "$OUT/owned-v1.txt" "$OUT/owned-v2.txt" || { echo "the update created new objects instead of updating" >&2; exit 1; }
dash_uuid=$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["uuid"])' "projects/$PROJECT/bundle/dashboards/overview.yaml")
title=$(meta "SELECT dashboard_title FROM dashboards WHERE uuid::text = '$dash_uuid'")
case "$title" in *"(v2)") ;; *) echo "title not updated: $title" >&2; exit 1 ;; esac
"$SPY" tests/quickstart_acceptance/check_viewer.py dash.viewer DASH_VIEWER_PASSWORD "$PROJECT" - > "$OUT/check-viewer-api-v2.json" \
  || { cat "$OUT/check-viewer-api-v2.json"; exit 1; }
ok "update (stale fingerprint refused; same UUIDs and IDs; title '$title'; viewer = expected)"

step 08-undeploy bi undeploy
[ -z "$(owned)" ] || { echo "objects remain after undeploy" >&2; exit 1; }
others > "$OUT/others-after.txt"
diff "$OUT/others-before.txt" "$OUT/others-after.txt" || { echo "undeploy touched other Superset objects" >&2; exit 1; }
n=$(meta "SELECT (SELECT count(*) FROM ab_user WHERE username = 'dash.viewer') + (SELECT count(*) FROM ab_role WHERE name = 'dpagent-viewer-$PROJECT')")
[ "$n" = 0 ] || { echo "viewer account/role remain ($n)" >&2; exit 1; }
ok "undeploy (this project's objects and viewer gone; every other Superset object unchanged)"
echo PASS | tee -a "$OUT/steps.log"
