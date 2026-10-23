#!/usr/bin/env bash
# Quickstart acceptance (docs/quickstart.md) on a NEW disposable systemd host built from packs: a clean copy of
# the committed source, the operator steps of examples/quickstart/walkthrough.sh (product commands only), and
# independent checks after each milestone (tests/quickstart_acceptance/). No paid model call. Evidence lands in
# docs/evidence/quickstart/<sha>/<utc>/ (or $QUICKSTART_RESULTS_DIR); failed runs are kept too.
#   bash scripts/quickstart-acceptance-ci.sh [--keep] [--os ubuntu|ol8] [--bench smoke|small|medium]
# --bench SCALE runs the synthetic benchmark (benchmarks/synthetic/README.md - "synthetic benchmark, not HG load")
# on the same kind of host instead of the acceptance; it is refused (exit 4) before any build if the host's free
# disk / available memory are below the scale's minimums. Evidence: docs/evidence/benchmark/<sha>/<utc>-<scale>/.
# --os ol8 runs the SAME acceptance in an Oracle Linux 8.10 CONTAINER (systemd, dnf, EL8 python 3.6): it proves the
# EL8 userland, not a native install - the container shares the Docker host's kernel and SELinux mode and has no
# firewalld. The evidence says so (os-scope.txt).
# --demo reference|ai runs the 3-layer demo (docs/demo.md, examples/demo/demo.sh) instead: layer 1 install, layer 3
# draft (ai: `dpagent pipeline synth` with the model named by DPAGENT_MODEL - its credential is passed into the host
# by NAME, never written anywhere; reference: the labelled hand-written pipeline), approval, layer 2 deploy/run,
# dashboard + viewer, schedule, bad data blocked, recovery, undeploy. Evidence: docs/evidence/demo/<sha>/<utc>-<draft>/.
# An ai run needs DPAGENT_LLM_LEDGER: an initialised request ledger on this host (python -m dpagent.llm.ledger init,
# src/dpagent/llm/ledger.py). Every request - the probe (part `probe`), synth on the host (part `main`, or
# DEMO_LEDGER_SEGMENT) and any later rerun - is counted there before it is sent; it is never reset by a new run.
# --demo artifact (normally with --show): the accepted ai draft of DEMO_ARTIFACT, checked against its trace's hashes,
# validated on the new host - no model call and no credential on that host, no approval carried over.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
KEEP=0
OS=ubuntu
BENCH=""
DEMO=""
SHOW=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1; shift ;;
    --os) OS="${2:?--os needs ubuntu or ol8}"; shift 2 ;;
    --bench) BENCH="${2:?--bench needs a scale}"; shift 2 ;;
    --demo) DEMO="${2:?--demo needs reference, ai or artifact}"; shift 2 ;;
    --show) SHOW=1; KEEP=1; shift ;;         # with --demo: a host for a live demo, stopped before approval
    *) echo "usage: quickstart-acceptance-ci.sh [--keep] [--os ubuntu|ol8] [--bench SCALE | --demo reference|ai|artifact] [--show]" >&2; exit 2 ;;
  esac
done
case "$BENCH" in ''|[a-z]*) ;; *) echo "invalid --bench scale" >&2; exit 2 ;; esac
case "$BENCH" in *[!a-z]*) echo "invalid --bench scale" >&2; exit 2 ;; esac
case "$DEMO" in ''|reference|ai|artifact) ;; *) echo "--demo must be reference, ai or artifact" >&2; exit 2 ;; esac
[ -z "$BENCH" ] || [ -z "$DEMO" ] || { echo "--bench and --demo are separate runs" >&2; exit 2; }
[ "$SHOW" = 0 ] || [ -n "$DEMO" ] || { echo "--show needs --demo reference|ai" >&2; exit 2; }
PASSENV=()
WITH_LLM=""                                     # bootstrap's [llm] extra (litellm): only an ai demo calls a model
if [ "$DEMO" = ai ]; then
  WITH_LLM=1
  [ -n "${DPAGENT_MODEL:-}" ] || { echo "--demo ai needs DPAGENT_MODEL (and that provider's key, e.g. GEMINI_API_KEY) in the environment" >&2; exit 2; }
  case "${DPAGENT_LLM_LEDGER:-}" in /*) ;; *) echo "--demo ai needs DPAGENT_LLM_LEDGER: the absolute path of an initialised request ledger" >&2; exit 2 ;; esac
  [ -f "$DPAGENT_LLM_LEDGER" ] || { echo "no request ledger at $DPAGENT_LLM_LEDGER (python -m dpagent.llm.ledger init ...)" >&2; exit 2; }
  LEDGER_DIR=$(dirname "$DPAGENT_LLM_LEDGER")
  # the same file inside the host (bind mount): the container's requests count in the same budget
  PASSENV+=(-e "DPAGENT_LLM_LEDGER=/var/lib/dpagent-ledger/$(basename "$DPAGENT_LLM_LEDGER")"
            -e "DPAGENT_LLM_LEDGER_SEGMENT=${DEMO_LEDGER_SEGMENT:-main}")
  for v in DPAGENT_MODEL GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY DPAGENT_LLM_REASONING_EFFORT \
           DEMO_MAX_CALLS DEMO_MAX_REVISIONS DEMO_TRANSIENT_RETRIES DEMO_RETRY_WAIT; do
    [ -n "${!v:-}" ] && PASSENV+=(-e "$v")          # by name: docker reads the value from this environment
  done
  # the same limits the A3 runner uses (scripts/m25-acceptance-ci.sh): a reasoning model's hidden thinking shares
  # the output-token limit, and it is slow
  PASSENV+=(-e "DPAGENT_LLM_TIMEOUT=${DPAGENT_LLM_TIMEOUT:-900}" -e "DPAGENT_LLM_MAX_TOKENS=${DPAGENT_LLM_MAX_TOKENS:-48000}")
fi
if [ "$DEMO" = artifact ]; then
  # an accepted ai draft (DEMO_ARTIFACT: its committed evidence directory, relative to the repo) - no model, no key,
  # no probe on this host; DEMO_ARTIFACT_TRACE_SHA256 pins the trace that was accepted (examples/demo/import_ai_draft.py)
  [ -f "$ROOT/${DEMO_ARTIFACT:?--demo artifact needs DEMO_ARTIFACT}/ai-trace/trace.json" ] \
    || { echo "no ai-trace/trace.json under $DEMO_ARTIFACT" >&2; exit 2; }
  git -C "$ROOT" ls-files --error-unmatch "$DEMO_ARTIFACT/ai-trace/trace.json" >/dev/null 2>&1 \
    || { echo "$DEMO_ARTIFACT is not committed - only committed files reach the host" >&2; exit 2; }
  PASSENV+=(-e DEMO_ARTIFACT)
  [ -z "${DEMO_ARTIFACT_TRACE_SHA256:-}" ] || PASSENV+=(-e DEMO_ARTIFACT_TRACE_SHA256)
fi
# what the acceptance phases use (quickstart, or the demo)
INSIDE=tests/quickstart_acceptance/inside.sh; ENVFILE=/root/quickstart-dev.env
OWN=quickstart-sales; OTHER=quickstart-ops; VIEWER=qs.sales.viewer; PWVAR=QS_SALES_VIEWER_PASSWORD
if [ -n "$DEMO" ]; then
  INSIDE=tests/demo_acceptance/inside.sh; ENVFILE=/root/demo-dev.env
  OWN=fulfil-sla; OTHER=""; VIEWER=fulfil.viewer; PWVAR=FS_VIEWER_PASSWORD
fi
case "$OS" in
  ubuntu) IMAGE=dpagent-m25-disposable; DOCKERFILE=m25-disposable-host.Dockerfile; SUFFIX="" ;;
  ol8)    IMAGE=dpagent-ol8-disposable;  DOCKERFILE=m25-disposable-host-ol8.Dockerfile; SUFFIX="-ol8" ;;
  *) echo "--os must be ubuntu or ol8" >&2; exit 2 ;;
esac
command -v docker >/dev/null && docker info >/dev/null
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all -- . ':!docs/evidence')" \
  || { echo 'Commit source first: evidence must name a commit' >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse HEAD)
if [ -n "$BENCH" ]; then
  OUT=${QUICKSTART_RESULTS_DIR:-$ROOT/docs/evidence/benchmark/$SHA/$(date -u +%Y%m%dT%H%M%SZ)-$BENCH$SUFFIX}
  mkdir -p "$OUT"
  PYH="$ROOT/.venv/bin/python"; [ -x "$PYH" ] || PYH=python3
  "$PYH" "$ROOT/benchmarks/synthetic/preflight.py" "$BENCH" --disk-path "$(docker info -f '{{.DockerRootDir}}')" \
    > "$OUT/preflight-host.json" || { echo "benchmark refused by the host preflight: $(cat "$OUT/preflight-host.json")" >&2; exit 4; }
elif [ -n "$DEMO" ]; then
  OUT=${QUICKSTART_RESULTS_DIR:-$ROOT/docs/evidence/demo$([ "$SHOW" = 1 ] && echo -show)/$SHA/$(date -u +%Y%m%dT%H%M%SZ)-$DEMO$SUFFIX}
  mkdir -p "$OUT"
  if [ "$DEMO" = ai ]; then
    # one tiny call before any host is built: key, model name, quota (it counts in the overall call budget)
    PYH="$ROOT/.venv/bin/python"; [ -x "$PYH" ] || PYH=python3
    if [ "${DEMO_PROBE_ATTEMPTS:-2}" = 0 ]; then
      # a probe already passed for this model: reuse its evidence and spend no call on another one
      [ -f "${DEMO_PROBE_EVIDENCE:-}" ] && grep -q '"ok": true' "$DEMO_PROBE_EVIDENCE" \
        || { echo "DEMO_PROBE_ATTEMPTS=0 needs DEMO_PROBE_EVIDENCE: the probe.json of a passed probe" >&2; exit 2; }
      printf '{"skipped": true, "reason": "a passed probe is reused, no call spent", "evidence": "%s"}\n' \
        "${DEMO_PROBE_EVIDENCE#$ROOT/}" > "$OUT/probe.json"
    else
    # llm_verify probe: at most DEMO_PROBE_ATTEMPTS calls (default 2) counting retries; only a transient answer
    # (503/429/timeout) is retried - a key, model-name or quota error stops at once
    ( cd "$ROOT" && DPAGENT_LLM_VERIFY=1 DPAGENT_LLM_LEDGER_SEGMENT=probe PYTHONPATH=src "$PYH" -c 'import json, os, sys
from dpagent.pipelines.llm_verify import probe
r = probe(attempts=int(os.environ.get("DEMO_PROBE_ATTEMPTS", "2")), wait_s=30)
print(json.dumps(r, indent=1)); sys.exit(0 if r.get("ok") else 1)' ) > "$OUT/probe.json" 2>&1 \
      || { cat "$OUT/probe.json"; echo "the model probe failed - fix the key / model name / quota first; nothing was built" >&2; exit 2; }
    fi
  fi
else
  OUT=${QUICKSTART_RESULTS_DIR:-$ROOT/docs/evidence/quickstart/$SHA/$(date -u +%Y%m%dT%H%M%SZ)$SUFFIX}
  mkdir -p "$OUT"
fi
git -C "$ROOT" rev-parse HEAD 'HEAD^{tree}' > "$OUT/provenance.txt"
NAME="dpagent-quickstart-acceptance-$$"
[ "$SHOW" = 0 ] || NAME="dpagent-demo-show-$DEMO-$$"     # never confused with an acceptance host
docker build -q -t "$IMAGE" -f "$ROOT/scripts/$DOCKERFILE" "$ROOT" > "$OUT/image.txt"
CID=''
cleanup() {
  rc=$?; trap - EXIT
  echo "$rc" > "$OUT/exit-code.txt"
  [ -z "${LEDGER_DIR:-}" ] || ( cd "$ROOT" && PYTHONPATH=src "${PYH:-python3}" -m dpagent.llm.ledger status "$DPAGENT_LLM_LEDGER" ) \
    > "$OUT/request-ledger.json" 2>&1 || true
  if [ -n "$CID" ]; then
    docker cp "$NAME:/root/quickstart-evidence/." "$OUT/" 2>/dev/null || true
    if [ "$KEEP" -eq 0 ]; then
      test "$(docker inspect -f '{{.Id}}' "$NAME")" = "$CID" && docker rm -f "$CID" > "$OUT/container-teardown.txt"
    else
      echo "kept $NAME ($CID) for inspection: docker rm -f $CID" | tee "$OUT/container-kept.txt"
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT
CID=$(docker run -d --name "$NAME" --privileged --cgroupns host --tmpfs /run --tmpfs /run/lock \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "$ROOT:/opt/src/dpagent-src:ro" \
  ${LEDGER_DIR:+-v "$LEDGER_DIR:/var/lib/dpagent-ledger:rw"} "$IMAGE")
echo "$CID" > "$OUT/container-id.txt"
for _ in $(seq 1 30); do
  [ "$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)" = running ] && break; sleep 1
done
docker exec "$NAME" systemctl is-system-running
# A clean checkout: only committed files reach the host (git archive), never this working tree's extras.
git -C "$ROOT" archive --format=tar HEAD | docker exec -i "$NAME" bash -c 'mkdir -p /opt/src/clean && tar -C /opt/src/clean -xf -'
if [ "$OS" = ol8 ]; then
  # What this run does and does not prove, written next to the results (the Docker host's own state is read,
  # never changed: SELinux and the firewall are not touched).
  {
    echo "scope: Oracle Linux 8.10 CONTAINER on a Docker host - not a native/VM install"
    echo "container os-release: $(docker exec "$NAME" sh -c '. /etc/os-release; echo "$PRETTY_NAME"')"
    echo "container kernel (shared with the host): $(docker exec "$NAME" uname -r)"
    echo "container getenforce: $(docker exec "$NAME" sh -c 'getenforce 2>&1 || true')   (SELinux is NOT exercised)"
    echo "container firewalld: $(docker exec "$NAME" sh -c 'systemctl is-active firewalld 2>&1 || true')   (no firewall is exercised)"
    echo "docker host getenforce: $(getenforce 2>&1 || true)"
    echo "docker host firewalld: $(systemctl is-active firewalld 2>&1 || true)"
    echo "bare image python3 (before the OS packages python3/python3.11 are installed): $(docker exec "$NAME" sh -c 'command -v python3 >/dev/null && python3 --version || echo none')"
    echo "system python baseline = after those OS packages, before DPAgent (system-python-before.txt); compared at the end"
  } > "$OUT/os-scope.txt"
  docker exec -e "DPAGENT_WITH_LLM=$WITH_LLM" "$NAME" bash -lc '
    set -euo pipefail
    dnf -y install python3 python3.11 python3.11-pip >/dev/null
    PYBIN=$(readlink -f /usr/bin/python3); echo "$PYBIN $(python3 --version)" > /root/system-python-before.txt
    rpm -V python3 > /root/system-python-rpmv-before.txt 2>&1 || true
    DPAGENT_PYTHON=/usr/bin/python3.11 DPAGENT_SOURCE_DIR=/opt/src/clean bash /opt/src/clean/scripts/bootstrap.sh
  ' > "$OUT/bootstrap.log" 2>&1
else
  docker exec -e "DPAGENT_WITH_LLM=$WITH_LLM" "$NAME" bash -lc '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq sudo ca-certificates curl openssl python3 python3-venv python3-pip >/dev/null
    DPAGENT_SOURCE_DIR=/opt/src/clean bash /opt/src/clean/scripts/bootstrap.sh
  ' > "$OUT/bootstrap.log" 2>&1
fi
if [ -n "$BENCH" ]; then
  docker exec "$NAME" bash -lc "bash /opt/dpagent/tests/quickstart_acceptance/inside.sh bench $BENCH" 2>&1 | tee "$OUT/run-bench.log"
  exit 0
fi
if [ "$SHOW" = 1 ]; then
  docker exec "${PASSENV[@]}" -e "DEMO_DRAFT=$DEMO" "$NAME" bash -lc "bash /opt/dpagent/$INSIDE show" 2>&1 | tee "$OUT/run-show.log"
  echo "live-demo host: $NAME (docs/demo-show.md)" | tee "$OUT/show-host.txt"
  exit 0
fi
docker exec "${PASSENV[@]}" -e "DEMO_DRAFT=$DEMO" "$NAME" bash -lc "bash /opt/dpagent/$INSIDE first" 2>&1 | tee "$OUT/run-first.log"

# Viewer RBAC in a real browser, logged in as the sales viewer: a Playwright container sharing the host's network
# namespace (Superset binds loopback). The password goes through a 0600 file that is removed right after.
docker build -q -t dpagent-quickstart-browser -f "$ROOT/tests/data_platform_acceptance/browser.Dockerfile" \
  "$ROOT/tests/data_platform_acceptance" > "$OUT/browser-image.txt"
RUNDIR=$(mktemp -d); chmod 700 "$RUNDIR"
docker exec -e "ENVFILE=$ENVFILE" -e "VIEWER=$VIEWER" -e "PWVAR=$PWVAR" "$NAME" bash -c 'set -a; . "$ENVFILE"; set +a
  printf "{\"username\": \"%s\", \"password\": \"%s\"}" "$VIEWER" "${!PWVAR}"' > "$RUNDIR/viewer-auth.json"
chmod 600 "$RUNDIR/viewer-auth.json"
docker exec -i -e "OWN=$OWN" -e "OTHER=$OTHER" "$NAME" /opt/superset/.venv/bin/python - > "$RUNDIR/viewer-targets.json" <<'PY'
import json, subprocess, yaml
from pathlib import Path
def dash_id(project):
    key = yaml.safe_load(Path(f"/opt/dpagent/projects/{project}/bundle/dashboards/overview.yaml").read_text())["uuid"]
    return subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-d", "superset_meta",
                                    "-c", f"SELECT id FROM dashboards WHERE uuid = '{key}'"], cwd="/", text=True).strip()
import os
own, other = os.environ["OWN"], os.environ.get("OTHER") or ""
check = yaml.safe_load(Path(f"/opt/dpagent/projects/{own}/expected.yaml").read_text())["checks"][0]
f = check["filters"][0]
targets = {"own": {"url": f"/superset/dashboard/{dash_id(own)}/", "rows": check["rows"],
                   "filter_values": [str(v) for v in f["values"]], "filtered_rows": f["rows"]}}
if other:
    targets["other"] = {"url": f"/superset/dashboard/{dash_id(other)}/"}
print(json.dumps(targets, default=str))
PY
mkdir -p "$OUT/browser"
set +e
docker run --rm --network "container:$NAME" -v "$RUNDIR:/run:ro" -v "$OUT/browser:/output" \
  -v "$ROOT/tests/quickstart_acceptance/viewer_browser.py:/viewer_browser.py:ro" \
  dpagent-quickstart-browser python /viewer_browser.py > "$OUT/browser/run.log" 2>&1
BROWSER_RC=$?
set -e
[ -s "$RUNDIR/viewer-targets.json" ] || { echo "viewer-targets.json is empty - harness error, not a product result" >&2; exit 97; }
if grep -rqF -- "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["password"])' "$RUNDIR/viewer-auth.json")" "$OUT/browser"; then
  rm -rf "$RUNDIR" "$OUT/browser"; echo "viewer password found in browser evidence - removed" >&2; exit 99
fi
rm -rf "$RUNDIR"
echo "viewer browser rc=$BROWSER_RC" | tee -a "$OUT/browser/run.log"
[ "$BROWSER_RC" -eq 0 ] || { tail -30 "$OUT/browser/run.log"; exit 1; }

docker exec "${PASSENV[@]}" -e "DEMO_DRAFT=$DEMO" "$NAME" bash -lc "bash /opt/dpagent/$INSIDE second" 2>&1 | tee "$OUT/run-second.log"
if [ "$OS" = ol8 ]; then
  docker exec "$NAME" bash -lc '
    echo "$(readlink -f /usr/bin/python3) $(python3 --version)" > /root/system-python-after.txt
    rpm -V python3 > /root/system-python-rpmv-after.txt 2>&1 || true
    diff /root/system-python-before.txt /root/system-python-after.txt && diff /root/system-python-rpmv-before.txt /root/system-python-rpmv-after.txt
  ' > "$OUT/system-python-unchanged.txt" 2>&1 \
    && { echo "system python unchanged: $(docker exec "$NAME" cat /root/system-python-after.txt)" | tee -a "$OUT/os-scope.txt"; } \
    || { echo "SYSTEM PYTHON CHANGED" | tee -a "$OUT/os-scope.txt"; cat "$OUT/system-python-unchanged.txt"; exit 1; }
fi
