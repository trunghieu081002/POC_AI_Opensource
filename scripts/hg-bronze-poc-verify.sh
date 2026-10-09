#!/usr/bin/env bash
# Real verification of the HG bronze EXTRACT/LOAD split (docs/hg-bronze-staging.md,
# pipelines/hg_bronze_poc). Every scenario below runs against real services -
# a source Postgres in its OWN container, SeaweedFS, and the dlt/Postgres/
# Airflow stack inside a dpagent disposable-host container - and asserts on
# what actually happened; the script exits non-zero at the first assertion
# that does not hold. Nothing here is mocked.
#
# Prerequisites this script checks and names rather than assumes:
#   * docker usable by this user
#   * DEV container (default: dpagent-hg-dev) - a systemd container with the
#     layer2 stack installed (scripts/m25-acceptance-ci.sh shows how), the
#     repo's current src/ + pipelines/ copied into /opt/dpagent, and a
#     warehouse database/role (hgwh/hgwh) - see SETUP below
#   * SeaweedFS S3 gateway (default container: seaweedfs-poc) with bucket hg-bronze
#   * python3 + boto3 on THIS host (used to inspect/corrupt bronze objects)
#
#   bash scripts/hg-bronze-poc-verify.sh [--skip-airflow]
set -uo pipefail

DEV="${DEV:-dpagent-hg-dev}"
S3C="${S3C:-seaweedfs-poc}"
SRC="${SRC:-hg-src-pg}"
S3_LOCAL="${S3_LOCAL:-http://localhost:8333}"
S3_AK="${S3_AK:-m25-poc-access-key}"
S3_SK="${S3_SK:-m25-poc-secret-key-change-me}"
BUCKET="hg-bronze"
SKIP_AIRFLOW=0
[ "${1:-}" = "--skip-airflow" ] && SKIP_AIRFLOW=1

PASS=0
say()  { printf '\n\033[36m== %s\033[0m\n' "$*"; }
pass() { PASS=$((PASS+1)); printf '\033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
eq()   { [ "$1" = "$2" ] && pass "$3 ($1)" || fail "$3: got [$1] expected [$2]"; }
contains() {   # whitespace-normalised: the CLI's rich output wraps long lines mid-phrase
  local hay; hay="$(printf '%s' "$1" | tr '\n' ' ' | tr -s ' ')"
  case "$hay" in *"$2"*) pass "$3";; *) fail "$3: [$2] not in output: $1";; esac; }

command -v docker >/dev/null && docker info >/dev/null 2>&1 || fail "docker is not usable by $(id -un)"
python3 -c 'import boto3' 2>/dev/null || fail "python3 boto3 is needed on this host"
for c in "$DEV" "$S3C"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] || fail "container $c is not running"
done
S3_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$S3C")"

# ----------------------------------------------------------------- helpers
write_env() {   # (re)written whenever the source container (re)starts - its IP can change
  local ip; ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$SRC")"
  docker exec -i "$DEV" bash -c "cat > /root/hgenv.sh" <<EOF
export HG_POC_SOURCE_HOST=$ip HG_POC_SOURCE_NAME=odoo_src HG_POC_SOURCE_USER=postgres HG_POC_SOURCE_PASSWORD=srcpw
export HG_POC_WH_HOST=localhost HG_POC_WH_NAME=hgwh HG_POC_WH_USER=hgwh HG_POC_WH_PASSWORD=whpw
export HG_POC_BRONZE_ENDPOINT=http://$S3_IP:8333 HG_POC_BRONZE_BUCKET=$BUCKET
export HG_POC_BRONZE_ACCESS_KEY=$S3_AK HG_POC_BRONZE_SECRET_KEY=$S3_SK
EOF
}
dev()      { docker exec "$DEV" bash -c "source /root/hgenv.sh; cd /opt/dpagent; $*" 2>&1; }
dev_nosrc(){ docker exec "$DEV" bash -c "source /root/hgenv.sh; unset HG_POC_SOURCE_HOST HG_POC_SOURCE_NAME HG_POC_SOURCE_USER HG_POC_SOURCE_PASSWORD; cd /opt/dpagent; $*" 2>&1; }
src_sql()  { docker exec -i "$SRC" psql -U postgres -d odoo_src -v ON_ERROR_STOP=1 -At "$@"; }
wh_sql()   { docker exec "$DEV" env PGPASSWORD=whpw psql -h localhost -U hgwh -d hgwh -v ON_ERROR_STOP=1 -At -c "$1"; }
src_sum()  { src_sql -c "select count(*)||'|'||coalesce(md5(string_agg(t::text,'|' order by id)),'') from res_partner t"; }
land_sum() { wh_sql "select count(*)||'|'||coalesce(md5(string_agg(t::text,'|' order by id)),'') from hg_bronze_poc_landing.res_partner t"; }
status_of(){ wh_sql "select status from dpagent_meta.bronze_batches where batch_id='$1'"; }
extract()  { local out; out="$(dev "dpagent pipeline bronze-extract hg_bronze_poc")" || { echo "$out" >&2; return 1; }
             echo "$out" | tr -d '\n' | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1; }
S3PY=/tmp/hg_bronze_s3.py
cat > "$S3PY" <<PY
import sys, boto3
from botocore.client import Config
s3 = boto3.client("s3", endpoint_url="$S3_LOCAL", aws_access_key_id="$S3_AK", aws_secret_access_key="$S3_SK",
                  config=Config(signature_version="s3v4"), region_name="us-east-1")
op, *a = sys.argv[1:]; B = "$BUCKET"
if op == "list":
    for o in s3.list_objects_v2(Bucket=B, Prefix=a[0]).get("Contents", []): print(o["Key"])
elif op == "get":  sys.stdout.buffer.write(s3.get_object(Bucket=B, Key=a[0])["Body"].read())
elif op == "put":  s3.put_object(Bucket=B, Key=a[0], Body=open(a[1], "rb").read())
elif op == "putstr": s3.put_object(Bucket=B, Key=a[0], Body=a[1].encode())
elif op == "wipe":
    for o in s3.list_objects_v2(Bucket=B, Prefix=a[0]).get("Contents", []): s3.delete_object(Bucket=B, Key=o["Key"])
PY
s3py_real() { python3 "$S3PY" "$@"; }

start_source() {
  docker rm -f "$SRC" >/dev/null 2>&1
  docker run -d --name "$SRC" -e POSTGRES_PASSWORD=srcpw -e POSTGRES_DB=odoo_src postgres:16-alpine >/dev/null
  for _ in $(seq 1 40); do docker exec "$SRC" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
  src_sql <<'SQL' >/dev/null
CREATE TABLE res_partner (
  id bigint PRIMARY KEY, name text NOT NULL, active boolean, credit_limit numeric(12,2),
  write_date timestamp, create_date timestamptz, meta jsonb, ref uuid, birthday date, note varchar(40));
INSERT INTO res_partner VALUES
 (1,'Alice',true,1000.50,'2026-01-05 10:00:00','2026-01-05 10:00:00+00','{"vip": true}','11111111-1111-1111-1111-111111111111','1990-02-03','first'),
 (2,'Bob',false,0.00,'2026-01-06 11:30:15.123456','2026-01-06 04:30:15+00','{"tags":["a","b"]}',NULL,NULL,NULL),
 (3,'Chị Thảo',true,12345678.99,NULL,'2026-01-07 00:00:00+07',NULL,'33333333-3333-3333-3333-333333333333','2000-12-31',''),
 (4,'D',NULL,-5.25,'2026-02-01 00:00:00','2026-02-01 00:00:00+00','{}','44444444-4444-4444-4444-444444444444','1985-07-04','x'),
 (5,'Eve',true,99.99,'2026-02-02 23:59:59.999999','2026-02-02 23:59:59+00','{"n": 1.5}',NULL,'2010-01-01','last');
SQL
  write_env
}

# ------------------------------------------------------------------- SETUP
say "SETUP: fresh source container, empty registry/landing, empty bronze prefix"
start_source
dev "sudo -u postgres psql -tAc \"select 1 from pg_roles where rolname='hgwh'\"" | grep -q 1 || \
  dev "sudo -u postgres psql -c \"CREATE ROLE hgwh LOGIN PASSWORD 'whpw'\" -c 'CREATE DATABASE hgwh OWNER hgwh'" >/dev/null
wh_sql "DROP SCHEMA IF EXISTS dpagent_meta CASCADE; DROP SCHEMA IF EXISTS hg_bronze_poc_landing CASCADE" >/dev/null
s3py_real wipe bronze/hg_bronze_poc/
eq "$(s3py_real list bronze/hg_bronze_poc/ | wc -l | tr -d ' ')" "0" "bronze prefix is empty"
eq "$(src_sum | cut -d'|' -f1)" "5" "source has 5 typed rows"

# ----------------------------------------------- S1+S3: the source-down proof
say "S1/S3: EXTRACT, then STOP the source container, then LOAD in a fresh process without any source value"
B1="$(extract)"; [ -n "$B1" ] || fail "extract produced no batch id"
eq "$(status_of "$B1")" "extracted" "registry: batch is extracted"
eq "$(s3py_real list "bronze/hg_bronze_poc/res_partner/$B1/" | grep -c parquet)" "3" "5 rows at chunk_rows=2 -> 3 parquet objects (multi-object manifest)"
contains "$(s3py_real list "bronze/hg_bronze_poc/res_partner/$B1/")" "_manifest.json" "manifest published"
SRC_SUM="$(src_sum)"
docker stop "$SRC" >/dev/null
eq "$(docker inspect -f '{{.State.Running}}' "$SRC")" "false" "source container is stopped"
SRC_IP="$(grep -oE 'SOURCE_HOST=[0-9.]+' <(docker exec "$DEV" cat /root/hgenv.sh) | cut -d= -f2)"
OUT="$(docker exec "$DEV" env PGCONNECT_TIMEOUT=4 PGPASSWORD=srcpw psql -h "$SRC_IP" -U postgres -d odoo_src -c 'select 1' 2>&1)"; RC=$?
[ $RC -ne 0 ] && pass "connecting to the stopped source fails (rc=$RC): $(echo "$OUT" | tail -1)" || fail "source still reachable"
OUT="$(dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B1")"; RC=$?
eq "$RC" "0" "LOAD (new process, SOURCE vars unset, source down) exits 0"
contains "$OUT" "loaded batch $B1: 5 row(s) from 3 object(s)" "LOAD reports 5 rows from 3 objects"
eq "$(land_sum)" "$SRC_SUM" "landing == source snapshot, row for row (md5 of every column, all types)"
eq "$(status_of "$B1")" "loaded" "registry: batch is loaded"
docker start "$SRC" >/dev/null; for _ in $(seq 1 40); do docker exec "$SRC" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done; write_env

# ------------------------------------------------------------ S2: idempotency
say "S2: re-LOAD of an already-loaded batch changes nothing"
LOADED_AT="$(wh_sql "select loaded_at from dpagent_meta.bronze_batches where batch_id='$B1'")"
OUT="$(dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B1")"
contains "$OUT" "already loaded" "second LOAD says already loaded"
eq "$(land_sum)" "$SRC_SUM" "landing unchanged"
eq "$(wh_sql "select loaded_at from dpagent_meta.bronze_batches where batch_id='$B1'")" "$LOADED_AT" "loaded_at not rewritten"

# --------------------------------------------------- S4: concurrent LOAD, same batch
say "S4: two LOADs of the same batch at the same instant"
src_sql -c "update res_partner set name='Alice 2', credit_limit=1.00 where id=1; insert into res_partner values (6,'Frank',true,6.60,'2026-03-01 00:00:00','2026-03-01 00:00:00+00','{\"k\":[1,2]}',NULL,'1999-09-09','six')" >/dev/null
B2="$(extract)"; SRC_SUM2="$(src_sum)"
dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B2" > /tmp/hg_c1.out & P1=$!
dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B2" > /tmp/hg_c2.out & P2=$!
wait $P1; R1=$?; wait $P2; R2=$?
eq "$R1$R2" "00" "both concurrent LOADs exit 0"
eq "$(cat /tmp/hg_c1.out /tmp/hg_c2.out | grep -c 'loaded batch')" "1" "exactly one actually loaded"
eq "$(cat /tmp/hg_c1.out /tmp/hg_c2.out | grep -c 'already loaded')" "1" "the other saw 'already loaded'"
eq "$(land_sum)" "$SRC_SUM2" "landing == source (6 rows), not doubled"
eq "$(wh_sql "select count(*) from dpagent_meta.bronze_batches where status='loaded' and batch_id='$B2'")" "1" "registry: loaded once"

# --------------------------------------- S5: an older batch can never overwrite a newer one
say "S5: loading an older extracted batch after a newer one is refused"
OLD="$(extract)"
src_sql -c "update res_partner set name='Alice 3' where id=1" >/dev/null
NEW="$(extract)"; SRC_SUM3="$(src_sum)"
dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $NEW" >/dev/null
eq "$(land_sum)" "$SRC_SUM3" "newer batch loaded"
OUT="$(dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $OLD")"; RC=$?
[ $RC -ne 0 ] && pass "older batch LOAD refused (rc=$RC)" || fail "older batch was loaded"
contains "$OUT" "newer batch" "refusal names the reason"
eq "$(land_sum)" "$SRC_SUM3" "landing still the newer data"
eq "$(status_of "$OLD")" "extracted" "older batch stays extracted (not marked loaded)"

# -------------------------------- S6/S7: corruption is detected, nothing half-applied, retry works
say "S6: a corrupted bronze object is refused; fixing it makes a plain retry succeed"
src_sql -c "update res_partner set name='Alice 4' where id=1" >/dev/null
B6="$(extract)"; SRC_SUM6="$(src_sum)"; BEFORE="$(land_sum)"
KEY="$(s3py_real list "bronze/hg_bronze_poc/res_partner/$B6/" | grep part-00001)"
s3py_real get "$KEY" > /tmp/hg_good.parquet
head -c 100 /tmp/hg_good.parquet > /tmp/hg_bad.parquet; head -c $(( $(stat -c%s /tmp/hg_good.parquet) - 100 )) /dev/urandom >> /tmp/hg_bad.parquet
s3py_real put "$KEY" /tmp/hg_bad.parquet
OUT="$(dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B6")"; RC=$?
[ $RC -ne 0 ] && pass "LOAD of a corrupted object fails (rc=$RC)" || fail "corrupted object was loaded"
contains "$OUT" "sha256" "failure names the checksum mismatch"
eq "$(land_sum)" "$BEFORE" "landing untouched by the failed LOAD"
eq "$(status_of "$B6")" "extracted" "batch still extracted, retryable"
s3py_real put "$KEY" /tmp/hg_good.parquet
dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B6" >/dev/null; eq "$?" "0" "retry after the object is restored succeeds"
eq "$(land_sum)" "$SRC_SUM6" "landing == source after the retry"

say "S7: a manifest changed after publication is refused"
src_sql -c "update res_partner set name='Alice 5' where id=1" >/dev/null
B7="$(extract)"; BEFORE="$(land_sum)"
MK="bronze/hg_bronze_poc/res_partner/$B7/_manifest.json"
s3py_real get "$MK" > /tmp/hg_manifest.json
s3py_real putstr "$MK" "$(sed 's/"total_rows": [0-9]*/"total_rows": 999/' /tmp/hg_manifest.json)"
OUT="$(dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B7")"; RC=$?
[ $RC -ne 0 ] && pass "tampered manifest refused (rc=$RC)" || fail "tampered manifest accepted"
contains "$OUT" "manifest sha256" "failure names the manifest checksum"
eq "$(land_sum)" "$BEFORE" "landing untouched"
s3py_real put "$MK" /tmp/hg_manifest.json
dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B7" >/dev/null; eq "$?" "0" "loads once the manifest is restored"

# ---------------------------------------------- S8: an empty snapshot loads exactly 0 rows
say "S8: an empty source snapshot replaces landing with 0 rows (never 'skip and keep old data')"
src_sql -c "create table res_partner_backup as select * from res_partner; delete from res_partner" >/dev/null
B8="$(extract)"
eq "$(s3py_real list "bronze/hg_bronze_poc/res_partner/$B8/" | grep -c parquet)" "0" "0 data objects"
contains "$(s3py_real list "bronze/hg_bronze_poc/res_partner/$B8/")" "_manifest.json" "manifest still published"
eq "$(land_sum | cut -d'|' -f1)" "6" "landing still has the old 6 rows before the LOAD"
dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B8" >/dev/null; eq "$?" "0" "empty batch loads"
eq "$(land_sum | cut -d'|' -f1)" "0" "landing now has exactly 0 rows"
eq "$(wh_sql "select loaded_rows from dpagent_meta.bronze_batches where batch_id='$B8'")" "0" "registry records 0 loaded rows"
src_sql -c "insert into res_partner select * from res_partner_backup; drop table res_partner_backup" >/dev/null

# ----------------------------- S9: an extract that dies half-way can never be loaded
say "S9: kill -9 the worker mid-extract: batch stays 'extracting' and is never loadable"
src_sql -c "insert into res_partner select g, 'bulk '||g, true, g, now(), now(), '{}', NULL, NULL, NULL from generate_series(100, 60000) g" >/dev/null
BEFORE="$(land_sum)"
docker exec "$DEV" bash -c "source /root/hgenv.sh; export PIPELINE_NAME=hg_bronze_poc SOURCE_TABLE=res_partner BRONZE_CHUNK_ROWS=50 BRONZE_ENDPOINT=\$HG_POC_BRONZE_ENDPOINT BRONZE_BUCKET=\$HG_POC_BRONZE_BUCKET BRONZE_ACCESS_KEY=\$HG_POC_BRONZE_ACCESS_KEY BRONZE_SECRET_KEY=\$HG_POC_BRONZE_SECRET_KEY BRONZE_PREFIX=bronze SRC_HOST=\$HG_POC_SOURCE_HOST SRC_DATABASE=odoo_src SRC_USER=postgres SRC_PASSWORD=srcpw WH_HOST=localhost WH_DATABASE=hgwh WH_USER=hgwh WH_PASSWORD=whpw; /opt/dlt/.venv/bin/python /opt/dpagent/src/dpagent/pipelines/bronze_worker.py extract >/dev/null 2>&1 & W=\$!; sleep 6; kill -9 \$W; wait \$W 2>/dev/null; true"
B9="$(wh_sql "select batch_id from dpagent_meta.bronze_batches where status='extracting' limit 1")"
[ -n "$B9" ] && pass "registry has a batch left in 'extracting' ($B9)" || fail "no 'extracting' batch after the kill"
eq "$(s3py_real list "bronze/hg_bronze_poc/res_partner/$B9/" | grep -c parquet | awk '{print ($1>0)?"some":"none"}')" "some" "partial objects were written"
eq "$(s3py_real list "bronze/hg_bronze_poc/res_partner/$B9/" | grep -c _manifest)" "0" "but no manifest was ever published"
OUT="$(dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B9")"; RC=$?
[ $RC -ne 0 ] && pass "LOAD of the interrupted batch refused (rc=$RC)" || fail "interrupted batch was loaded"
contains "$OUT" "'extracting'" "refusal names the batch state"
eq "$(land_sum)" "$BEFORE" "landing untouched"
src_sql -c "delete from res_partner where id >= 100" >/dev/null

# --------------------------------------------- S10/S11: schema drift and unsupported types
say "S10: source schema drift is refused, landing untouched"
src_sql -c "alter table res_partner add column extra_col integer" >/dev/null
B10="$(extract)"; BEFORE="$(land_sum)"
OUT="$(dev_nosrc "dpagent pipeline bronze-load hg_bronze_poc --batch $B10")"; RC=$?
[ $RC -ne 0 ] && pass "drifted batch refused (rc=$RC)" || fail "schema drift was silently adapted"
contains "$OUT" "schema drift is refused" "refusal says why"
eq "$(land_sum)" "$BEFORE" "landing untouched"
eq "$(status_of "$B10")" "extracted" "batch stays extracted"
src_sql -c "alter table res_partner drop column extra_col" >/dev/null

say "S11: a column type that cannot be stored exactly is refused at EXTRACT"
src_sql -c "alter table res_partner add column blob bytea" >/dev/null
OUT="$(dev "dpagent pipeline bronze-extract hg_bronze_poc")"; RC=$?
[ $RC -ne 0 ] && pass "extract of an unsupported type fails (rc=$RC)" || fail "bytea was stored"
contains "$OUT" "not supported by the bronze split" "failure names the type problem"
eq "$(wh_sql "select count(*) from dpagent_meta.bronze_batches where status='failed'")" "1" "registry records the failed batch"
src_sql -c "alter table res_partner drop column blob" >/dev/null

# ------------------------------------------ S12: the real Airflow DAG, end to end
if [ "$SKIP_AIRFLOW" = 1 ]; then say "S12 skipped (--skip-airflow)"; else
say "S12: deploy --allow-draft and run the real Airflow DAG (extract_bronze -> load_bronze -> gate_landing)"
# A previous aborted run of this script can leave a queued DagRun behind (a paused DAG
# still accepts triggers); un-deploying first deletes the DAG and its run history, so the
# count assertion below sees only the two runs this script triggers.
dev "dpagent pipeline undeploy hg_bronze_poc --yes" >/dev/null 2>&1
before_loaded="$(wh_sql "select count(*) from dpagent_meta.bronze_batches where status='loaded'")"
OUT="$(dev "dpagent pipeline deploy hg_bronze_poc --allow-draft --yes")"; RC=$?
[ $RC -eq 0 ] && pass "deploy --allow-draft succeeds" || { echo "$OUT"; fail "deploy failed"; }
# --allow-draft never unpauses (M1's guarantee) - the same explicit, scoped unpause
# fixture.run_fixture() does for a validation clone, via the product's own helper.
OUT="$(dev "/opt/dpagent/.venv/bin/python -c \"from dpagent.pipelines import deploy; r = deploy.unpause_dag('hg_bronze_poc'); print(r.returncode, (r.stderr or r.stdout).strip()[-200:])\"")"
case "$OUT" in 0*) pass "DAG unpaused ($OUT)";; *) fail "could not unpause the DAG: $OUT";; esac
SRC_SUM12="$(src_sum)"
for n in 1 2; do
  OUT="$(dev "dpagent pipeline run hg_bronze_poc --yes --wait --timeout 600")"; RC=$?
  [ $RC -eq 0 ] && pass "DAG run $n finishes ok" || { echo "$OUT" | tail -30; fail "DAG run $n failed"; }
done
eq "$(land_sum)" "$SRC_SUM12" "landing == source after two full DAG runs (no duplication)"
eq "$(wh_sql "select count(*) from dpagent_meta.bronze_batches where status='loaded'")" "$((before_loaded+2))" "two more batches loaded via the DAG (XCom handed each batch to its LOAD)"
dev "dpagent pipeline audit" | tail -40 | grep -E "bronze|gate|landing" | head -12
dev "dpagent pipeline undeploy hg_bronze_poc --yes" >/dev/null 2>&1
fi

say "ALL $PASS ASSERTIONS PASSED"
