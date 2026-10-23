#!/usr/bin/env bash
# AI dashboard acceptance (docs/ai-dashboards.md) on a NEW disposable host built from the committed source:
#   1. the host: scripts/quickstart-acceptance-ci.sh --keep (flavor quickstart) or --demo artifact --keep (flavor demo)
#      - its own acceptance must pass; afterwards the project's gold table still holds the fixture's data
#   2. tests/dashboard_acceptance/inside.sh: draft -> validate -> refusals -> deploy + viewer -> (3) -> redeploy ->
#      update -> undeploy
#   3. between them, the viewer in a real browser (Playwright sharing the host's network)
#   bash scripts/dashboard-acceptance-ci.sh --flavor demo|quickstart --case CASE --project NAME [--draft scripted|ai] [--keep]
# --draft scripted (default): the reply is a hand-written spec - it proves the chain, never anything about a model.
# --draft ai: DPAGENT_MODEL + its key + DPAGENT_LLM_LEDGER (part DASH_LEDGER_SEGMENT, default `dash`) must be set.
# Evidence: docs/evidence/dashboards/<sha>/<utc>-<project>-<draft>/ (the host build's evidence is linked from it).
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FLAVOR=""; CASE=""; PROJECT=""; DRAFT=scripted; KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --flavor) FLAVOR="${2:?}"; shift 2 ;;
    --case) CASE="${2:?}"; shift 2 ;;
    --project) PROJECT="${2:?}"; shift 2 ;;
    --draft) DRAFT="${2:?}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "usage: dashboard-acceptance-ci.sh --flavor demo|quickstart --case CASE --project NAME [--draft scripted|ai] [--keep]" >&2; exit 2 ;;
  esac
done
case "$FLAVOR" in demo|quickstart) ;; *) echo "--flavor demo|quickstart" >&2; exit 2 ;; esac
case "$DRAFT" in scripted|ai) ;; *) echo "--draft scripted|ai" >&2; exit 2 ;; esac
[ -f "$ROOT/tests/llm_verification/dashboard_cases/$CASE/expected.yaml" ] || { echo "no case $CASE with an expected.yaml" >&2; exit 2; }
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{1,40}$ ]] || { echo "--project: lowercase letters, digits, '-'" >&2; exit 2; }
PASSENV=(-e "DASH_DRAFT=$DRAFT")
for v in DASH_MAX_CALLS DASH_MAX_REVISIONS DASH_TRANSIENT_RETRIES; do [ -z "${!v:-}" ] || PASSENV+=(-e "$v"); done
if [ "$DRAFT" = ai ]; then
  [ -n "${DPAGENT_MODEL:-}" ] || { echo "--draft ai needs DPAGENT_MODEL and its key" >&2; exit 2; }
  case "${DPAGENT_LLM_LEDGER:-}" in /*) [ -f "$DPAGENT_LLM_LEDGER" ] ;; *) false ;; esac \
    || { echo "--draft ai needs DPAGENT_LLM_LEDGER (an initialised request ledger, absolute path)" >&2; exit 2; }
  for v in DPAGENT_MODEL GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do [ -z "${!v:-}" ] || PASSENV+=(-e "$v"); done
  PASSENV+=(-e "DPAGENT_LLM_LEDGER=/var/lib/dpagent-ledger/$(basename "$DPAGENT_LLM_LEDGER")"
            -e "DPAGENT_LLM_LEDGER_SEGMENT=${DASH_LEDGER_SEGMENT:-dash}"
            -e "DPAGENT_LLM_TIMEOUT=${DPAGENT_LLM_TIMEOUT:-900}" -e "DPAGENT_LLM_MAX_TOKENS=${DPAGENT_LLM_MAX_TOKENS:-16000}")
fi
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all -- . ':!docs/evidence')" \
  || { echo 'Commit source first: evidence must name a commit' >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse HEAD)
OUT=${DASH_RESULTS_DIR:-$ROOT/docs/evidence/dashboards/$SHA/$(date -u +%Y%m%dT%H%M%SZ)-$PROJECT-$DRAFT}
mkdir -p "$OUT"; git -C "$ROOT" rev-parse HEAD 'HEAD^{tree}' > "$OUT/provenance.txt"
HOSTEV="$OUT/host"; mkdir -p "$HOSTEV"
trap 'rc=$?; echo "$rc" > "$OUT/exit-code.txt"; exit $rc' EXIT

# ---- 1. the host, from its own acceptance (kept); its evidence goes next to ours
args=(--keep); [ "$FLAVOR" = demo ] && args+=(--demo artifact)
[ "$FLAVOR" = demo ] && export DEMO_ARTIFACT=docs/evidence/demo/4fce834eb1c1582bb405121038cdf7b9eec6f5fa/20261023T052117Z-ai \
  DEMO_ARTIFACT_TRACE_SHA256=2cbfa2905235ee0f2f7af91fea2fff25f800fca54cb679d0a1128ba28e1b1bd8
# the host is built without a model credential; the AI draft gets it only in step 2
# (the ledger's directory is mounted into the host for an ai draft; nothing is sent while the host is built)
MOUNT=""; [ "$DRAFT" = ai ] && MOUNT=$(dirname "$DPAGENT_LLM_LEDGER")
env -u GEMINI_API_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u DPAGENT_MODEL -u DPAGENT_LLM_LEDGER \
  MOUNT_LEDGER_DIR="$MOUNT" QUICKSTART_RESULTS_DIR="$HOSTEV" \
  bash "$ROOT/scripts/quickstart-acceptance-ci.sh" "${args[@]}" > "$OUT/host-build.log" 2>&1 \
  || { tail -30 "$OUT/host-build.log"; echo "the host's own acceptance failed - see host/" >&2; exit 3; }
NAME=$(sed -n 's/^kept \([^ ]*\) .*/\1/p' "$HOSTEV/container-kept.txt")
[ -n "$NAME" ] || { echo "no kept host" >&2; exit 3; }
CID=$(docker inspect -f '{{.Id}}' "$NAME")
cleanup() {
  rc=$?; trap - EXIT; echo "$rc" > "$OUT/exit-code.txt"
  docker cp "$NAME:/root/dashboard-evidence/$PROJECT/." "$OUT/" 2>/dev/null || true
  if [ "$KEEP" = 0 ]; then docker rm -f "$CID" > "$OUT/container-teardown.txt"; else echo "kept $NAME" > "$OUT/container-kept.txt"; fi
  exit "$rc"
}
trap cleanup EXIT

# ---- 2. draft, validate, refusals, deploy, viewer API
docker exec "${PASSENV[@]}" "$NAME" bash -lc "bash /opt/dpagent/tests/dashboard_acceptance/inside.sh $CASE $PROJECT $FLAVOR before-browser" \
  2>&1 | tee "$OUT/run-before-browser.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || exit 1

# ---- 3. the viewer in a real browser
case "$FLAVOR" in demo) ENVFILE=/root/demo-dev.env ;; quickstart) ENVFILE=/root/quickstart-dev.env ;; esac
docker build -q -t dpagent-quickstart-browser -f "$ROOT/tests/data_platform_acceptance/browser.Dockerfile" \
  "$ROOT/tests/data_platform_acceptance" > "$OUT/browser-image.txt"
RUNDIR=$(mktemp -d); chmod 700 "$RUNDIR"
docker exec -e "ENVFILE=$ENVFILE" "$NAME" bash -c 'set -a; . "$ENVFILE"; set +a
  printf "{\"username\": \"dash.viewer\", \"password\": \"%s\"}" "$DASH_VIEWER_PASSWORD"' > "$RUNDIR/viewer-auth.json"
chmod 600 "$RUNDIR/viewer-auth.json"
docker exec -i -e "OWN=$PROJECT" "$NAME" /opt/superset/.venv/bin/python - > "$RUNDIR/viewer-targets.json" <<'PY'
import json, os, subprocess, yaml
from pathlib import Path
own = os.environ["OWN"]
key = yaml.safe_load(Path(f"/opt/dpagent/projects/{own}/bundle/dashboards/overview.yaml").read_text())["uuid"]
did = subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-d", "superset_meta",
                               "-c", f"SELECT id FROM dashboards WHERE uuid = '{key}'"], cwd="/", text=True).strip()
check = yaml.safe_load(Path(f"/opt/dpagent/projects/{own}/expected.yaml").read_text())["checks"][0]
f = check["filters"][0]
print(json.dumps({"own": {"url": f"/superset/dashboard/{did}/", "rows": check["rows"],
                          "filter_values": [str(v) for v in f["values"]], "filtered_rows": f["rows"]}}, default=str))
PY
mkdir -p "$OUT/browser"; set +e
docker run --rm --network "container:$NAME" -v "$RUNDIR:/run:ro" -v "$OUT/browser:/output" \
  -v "$ROOT/tests/quickstart_acceptance/viewer_browser.py:/viewer_browser.py:ro" \
  dpagent-quickstart-browser python /viewer_browser.py > "$OUT/browser/run.log" 2>&1
BROWSER_RC=$?; set -e
if grep -rqF -- "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["password"])' "$RUNDIR/viewer-auth.json")" "$OUT/browser"; then
  rm -rf "$RUNDIR" "$OUT/browser"; echo "viewer password found in browser evidence - removed" >&2; exit 99
fi
rm -rf "$RUNDIR"; echo "viewer browser rc=$BROWSER_RC" | tee -a "$OUT/browser/run.log"
[ "$BROWSER_RC" -eq 0 ] || { tail -30 "$OUT/browser/run.log"; exit 1; }

# ---- 4. redeploy, update, undeploy
docker exec "${PASSENV[@]}" "$NAME" bash -lc "bash /opt/dpagent/tests/dashboard_acceptance/inside.sh $CASE $PROJECT $FLAVOR after-browser" \
  2>&1 | tee "$OUT/run-after-browser.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || exit 1
# no secret value of the host's environment file (or the model key) in this evidence
docker cp "$NAME:/root/dashboard-evidence/$PROJECT/." "$OUT/"
[ -z "${GEMINI_API_KEY:-}" ] || ! grep -rqF -- "$GEMINI_API_KEY" "$OUT" || { echo "the model key is in the evidence" >&2; exit 99; }
docker exec "$NAME" bash -c "grep -hE '^[A-Z_]*(PASSWORD|SECRET)[A-Z_]*=' $ENVFILE | cut -d= -f2-" | while read -r v; do
  [ ${#v} -lt 8 ] || ! grep -rqF -- "$v" "$OUT" || { echo "a secret value is in the evidence" >&2; exit 99; }
done
echo "DASHBOARD ACCEPTANCE PASS ($PROJECT, $DRAFT)" | tee -a "$OUT/result.txt"
