#!/usr/bin/env bash
# Real verification of the "HG-style dbt project" milestone (docs/hg-dbt-project.md,
# pipelines/hg_dbt_branch): bronze landing -> a dbt project the pipeline OWNS
# (packages, macro, seed, sources, ref(), silver + gold schemas) -> gates, through
# a real Airflow DAG, against a real source Postgres container and SeaweedFS.
#
# Same prerequisites as scripts/hg-bronze-poc-verify.sh (which it reuses the
# containers of): DEV (dpagent-hg-dev, layer2 stack + current src/ + pipelines/),
# SRC (hg-src-pg, a running source Postgres with public.res_partner), S3C
# (seaweedfs-poc, bucket hg-bronze), /root/hgenv.sh inside DEV, boto3 on this host.
# Asserts as it goes; exits non-zero at the first assertion that does not hold.
set -uo pipefail
DEV="${DEV:-dpagent-hg-dev}"; SRC="${SRC:-hg-src-pg}"; S3C="${S3C:-seaweedfs-poc}"
S3_LOCAL="${S3_LOCAL:-http://localhost:8333}"; S3_AK="${S3_AK:-m25-poc-access-key}"; S3_SK="${S3_SK:-m25-poc-secret-key-change-me}"
PIPE=hg_dbt_branch
PASS=0
say()  { printf '\n\033[36m== %s\033[0m\n' "$*"; }
pass() { PASS=$((PASS+1)); printf '\033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
eq()   { [ "$1" = "$2" ] && pass "$3 ($1)" || fail "$3: got [$1] expected [$2]"; }
contains() { local h; h="$(printf '%s' "$1" | tr '\n' ' ' | tr -s ' ')"; case "$h" in *"$2"*) pass "$3";; *) fail "$3: [$2] not in: $1";; esac; }

command -v docker >/dev/null && docker info >/dev/null 2>&1 || fail "docker is not usable"
for c in "$DEV" "$SRC" "$S3C"; do [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || fail "container $c is not running (run scripts/hg-bronze-poc-verify.sh first, or start it)"; done
docker exec "$DEV" test -f /root/hgenv.sh || fail "/root/hgenv.sh missing in $DEV"

dev()     { docker exec "$DEV" bash -c "source /root/hgenv.sh; cd /opt/dpagent; $*" 2>&1; }
wh()      { docker exec "$DEV" env PGPASSWORD=whpw psql -h localhost -U hgwh -d hgwh -v ON_ERROR_STOP=1 -At -c "$1"; }
src()     { docker exec -i "$SRC" psql -U postgres -d odoo_src -v ON_ERROR_STOP=1 -At "$@"; }
unpause() { dev "/opt/dpagent/.venv/bin/python -c \"from dpagent.pipelines import deploy; print(deploy.unpause_dag('$PIPE').returncode)\"" | tail -1; }
run_dag() { dev "dpagent pipeline run $PIPE --yes --wait --timeout 900"; }
gold()    { wh "select artists||'|'||distinct_names||'|'||min_platform_id||'|'||max_platform_id from gold.mart_artist_summary"; }
expect_gold() {   # independent of dbt: straight from the source table, minus the given excluded ids
  src -c "select count(*)||'|'||count(distinct name)||'|'||min(id)||'|'||max(id) from res_partner where id not in ($1)"; }

say "SETUP: clean slate for $PIPE (undeploy, drop staging/silver/gold, clear its registry rows and bronze objects)"
dev "dpagent pipeline undeploy $PIPE --yes" >/dev/null
wh "DROP SCHEMA IF EXISTS staging CASCADE; DROP SCHEMA IF EXISTS silver CASCADE; DROP SCHEMA IF EXISTS gold CASCADE; DELETE FROM dpagent_meta.bronze_batches WHERE pipeline='$PIPE'" >/dev/null
python3 - <<PY
import boto3
from botocore.client import Config
s3 = boto3.client("s3", endpoint_url="$S3_LOCAL", aws_access_key_id="$S3_AK", aws_secret_access_key="$S3_SK", config=Config(signature_version="s3v4"), region_name="us-east-1")
for o in s3.list_objects_v2(Bucket="hg-bronze", Prefix="bronze/$PIPE/").get("Contents", []): s3.delete_object(Bucket="hg-bronze", Key=o["Key"])
PY
src -c "select count(*) from res_partner" | grep -qE '^[1-9]' && pass "source has rows" || fail "source res_partner is empty"
eq "$(docker exec "$DEV" ls /opt/dbt/project/models 2>/dev/null | grep -c "$PIPE")" "0" "shared dbt project has no models for $PIPE before the run"

say "S1: step-3 validate = a REAL 'dbt deps' + 'dbt parse' of the pipeline's own project"
OUT="$(dev "dpagent pipeline validate $PIPE")"
contains "$OUT" "own dbt project dwh_dbt/ parsed clean" "dbt parse of the owned project passes (packages resolved, ref()/source()/macro/seed all resolved)"
contains "$OUT" "ref()/source() check: skipped" "the shared-project ref/source ban is skipped for an owned project, with the reason"

say "S2: deploy --allow-draft and run the real DAG: extract_bronze -> load_bronze -> gate -> dbt silver -> gate -> dbt gold -> gate"
OUT="$(dev "dpagent pipeline deploy $PIPE --allow-draft --yes")"; RC=$?
[ $RC -eq 0 ] && pass "deploy succeeds" || { echo "$OUT"; fail "deploy failed"; }
eq "$(unpause)" "0" "DAG unpaused (draft deploys never unpause themselves)"
OUT="$(run_dag)"; RC=$?
[ $RC -eq 0 ] && pass "DAG run finishes ok" || { echo "$OUT" | tail -30; fail "DAG run failed"; }
contains "$OUT" "landing passed" "landing stage passed"; contains "$OUT" "silver passed" "silver stage passed"; contains "$OUT" "gold passed" "gold stage passed"
TABLES="$(wh "select string_agg(table_schema||'.'||table_name, ',' order by table_schema||'.'||table_name) from information_schema.tables where table_schema in ('staging','silver','gold')")"
eq "$TABLES" "gold.mart_artist_summary,silver.dim_artist,silver.dim_artist_active,silver.manual_excluded_partner_ids,staging.res_partner" \
   "tables landed in the schemas HG's own generate_schema_name macro dictates (staging from landing_dataset_name, silver/gold from the project)"
eq "$(gold)" "$(expect_gold 2,4)" "gold summary == an independent calculation from the SOURCE (all rows minus the seed's excluded ids 2,4)"
eq "$(wh "select count(*) from silver.dim_artist")" "$(src -c 'select count(*) from res_partner')" "silver.dim_artist has every source row"
eq "$(wh "select dim_artist_sk from silver.dim_artist where platform_id='1'")" "$(wh "select md5('1')")" "dbt_utils.generate_surrogate_key matches an independent md5 of the id"
eq "$(wh "select string_agg(platform_id, ',' order by platform_id) from silver.manual_excluded_partner_ids")" "2,4" "seed loaded"

say "S3: the pipeline's models never entered the dbt pack's shared project"
eq "$(docker exec "$DEV" ls /opt/dbt/project/models | grep -c "$PIPE")" "0" "no $PIPE directory under /opt/dbt/project/models"
eq "$(docker exec "$DEV" bash -c 'ls /opt/dbt/project/macros; ls /opt/dbt/project/seeds 2>/dev/null' | grep -c -E 'manual_excluded|dim_artist')" "0" "no seed/model of the owned project in the shared project"

say "S4: a second run replaces, never duplicates"
OUT="$(run_dag)"; [ $? -eq 0 ] && pass "second DAG run ok" || fail "second run failed"
eq "$(gold)" "$(expect_gold 2,4)" "gold unchanged"
eq "$(wh "select count(*) from silver.dim_artist")" "$(src -c 'select count(*) from res_partner')" "silver.dim_artist not doubled"

say "S5: a new source row AND a changed seed flow through both hops (gold must change, not just stay equal)"
src -c "update res_partner set name='Renamed' where id=1; insert into res_partner values (7,'Grace',true,7.7,'2026-04-01 00:00:00','2026-04-01 00:00:00+00','{}',NULL,'2001-01-01','seven')" >/dev/null
docker exec -i "$DEV" bash -c "printf 'platform_id\n1\n2\n4\n' > /opt/dpagent/pipelines/$PIPE/dwh_dbt/seeds/manual_excluded_partner_ids.csv"
OUT="$(run_dag)"; [ $? -eq 0 ] && pass "run after the change ok" || { echo "$OUT" | tail; fail "run failed"; }
AFTER="$(gold)"
[ "$AFTER" != "4|4|1|6" ] && pass "gold actually CHANGED ($AFTER) - a stale result could not pass this" || fail "gold did not change"
eq "$AFTER" "$(expect_gold 1,2,4)" "gold == independent calculation with the new row 7 and the seed now excluding 1,2,4"
eq "$(wh "select platform_name from silver.dim_artist where platform_id='1'")" "Renamed" "the source rename reached silver"

say "S6: a broken model fails the stage with dbt's own error and leaves the earlier gold untouched"
BEFORE="$(gold)"
docker exec "$DEV" bash -c "cp /opt/dpagent/pipelines/$PIPE/dwh_dbt/models/silver/dim_artist_active.sql /tmp/dim_artist_active.sql.good; sed -i 's/left join/left joinn/' /opt/dpagent/pipelines/$PIPE/dwh_dbt/models/silver/dim_artist_active.sql"
OUT="$(run_dag)"; RC=$?
[ $RC -ne 0 ] && pass "run with a broken model fails (rc=$RC)" || fail "broken model did not fail the run"
eq "$(gold)" "$BEFORE" "gold untouched by the failed run (the gold stage never ran)"
docker exec "$DEV" bash -c "cp /tmp/dim_artist_active.sql.good /opt/dpagent/pipelines/$PIPE/dwh_dbt/models/silver/dim_artist_active.sql"
OUT="$(run_dag)"; [ $? -eq 0 ] && pass "run after restoring the model ok" || fail "run after restore failed"
eq "$(gold)" "$(expect_gold 1,2,4)" "gold correct again"

say "CLEANUP: restore the seed file, undeploy"
docker exec -i "$DEV" bash -c "printf 'platform_id\n2\n4\n' > /opt/dpagent/pipelines/$PIPE/dwh_dbt/seeds/manual_excluded_partner_ids.csv"
src -c "delete from res_partner where id=7; update res_partner set name='Alice 5' where id=1" >/dev/null
dev "dpagent pipeline undeploy $PIPE --yes" >/dev/null
say "ALL $PASS ASSERTIONS PASSED"
