#!/usr/bin/env bash
set -uo pipefail
out=$(dirname "$0")
exec 9>"$HOME/dpagent-work/pg-suite.lock"
flock -n 9 || exit 73
echo $$ > "$out/pid.txt"
name=dpagent-m25-acceptance-781222
test "$(docker exec "$name" systemctl is-active postgresql@15-main)" = active || exit 74
docker exec "$name" dpagent test postgres > "$out/runner.log" 2>&1
rc=$?
echo "$rc" > "$out/exit-code.txt"
date -u > "$out/finished.txt"
exit "$rc"
