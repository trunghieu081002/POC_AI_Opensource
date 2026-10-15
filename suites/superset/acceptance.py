"""Real API/database acceptance. No project-specific names or expected queries."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import requests
from psycopg2 import sql

sys.path.insert(0,"/opt/superset")
from env_exec import load_environment

ENV=load_environment("/opt/superset/superset.env")
NS=os.environ["DP_TEST_NS"]
if not re.fullmatch(r"dpagent_selftest_[0-9]+",NS): raise RuntimeError("Unsafe suite namespace")
STATE=Path("/var/tmp")/(NS+"_superset.json")
CONFIG=json.loads(Path("/opt/superset/reader-owned.json").read_text())
SCHEMA=CONFIG["schemas"][0]
CREATOR=CONFIG["creators"][0]
BASE="http://127.0.0.1:"+os.environ.get("DP_PARAM_WEBSERVER_PORT","8088")

def ident(value): return '"'+value.replace('"','""')+'"'

def owner_sql(statement):
    command="SET ROLE "+ident(CREATOR)+"; "+statement
    subprocess.run(["runuser","-u","postgres","--","psql","-X","-q","-v","ON_ERROR_STOP=1","-p",str(CONFIG["port"]),"-d",CONFIG["database"]],input=command.encode(),check=True,cwd="/")

def table(): return ident(SCHEMA)+"."+ident(NS)

def create_table():
    owner_sql("CREATE TABLE "+table()+" (id integer PRIMARY KEY, amount integer NOT NULL); INSERT INTO "+table()+" VALUES (1,10),(2,20);")

def session():
    s=requests.Session()
    r=s.post(BASE+"/api/v1/security/login",json={"username":ENV["SUPERSET_ADMIN_USER"],"password":ENV["SUPERSET_ADMIN_PASSWORD"],"provider":"db","refresh":True},timeout=20)
    r.raise_for_status()
    s.headers["Authorization"]="Bearer "+r.json()["access_token"]
    r=s.get(BASE+"/api/v1/security/csrf_token/",timeout=20);r.raise_for_status()
    s.headers["X-CSRFToken"]=r.json()["result"]
    s.headers["Referer"]=BASE+"/"
    return s

def state(): return json.loads(STATE.read_text())

def execute(s,statement):
    return s.post(BASE+"/api/v1/sqllab/execute/",json={"database_id":state()["database_id"],"sql":statement,"schema":SCHEMA,"runAsync":False,"queryLimit":100},timeout=45)

def query():
    s=session()
    r=execute(s,"SELECT count(*)::integer AS n, sum(amount)::integer AS total FROM "+table())
    r.raise_for_status()
    body=r.json()
    if body.get("status") != "success" or body.get("data") != [{"n":2,"total":30}]:
        raise AssertionError("Authenticated query differs from independent fixture totals")
    print("Authenticated warehouse query matches independent expected n=2,total=30")

def setup():
    if STATE.exists(): raise RuntimeError("Suite receipt already exists; refusing to overwrite")
    STATE.write_text(json.dumps({"database_id":None}));STATE.chmod(0o600)
    create_table()
    s=session()
    r=s.post(BASE+"/api/v1/database/",json={"database_name":NS,"sqlalchemy_uri":ENV["SUPERSET_WAREHOUSE_URI"],"expose_in_sqllab":True,"allow_dml":False,"allow_ctas":False,"allow_cvas":False,"allow_file_upload":False},timeout=30)
    r.raise_for_status()
    STATE.write_text(json.dumps({"database_id":r.json()["id"]}))
    print("Namespaced warehouse table and authenticated connection created")

def readonly():
    s=session()
    r=execute(s,"INSERT INTO "+table()+" VALUES (3,30)")
    body=r.json()
    if r.status_code < 400 and body.get("status") not in ("failed","error"):
        raise AssertionError("SQL Lab accepted a write")
    uri=ENV["SUPERSET_WAREHOUSE_URI"].replace("postgresql+psycopg2://","postgresql://",1)
    for statement in ("INSERT INTO "+table()+" VALUES (3,30)","UPDATE "+table()+" SET amount=0","DELETE FROM "+table(),"TRUNCATE "+table(),"DROP TABLE "+table()):
        with psycopg2.connect(uri) as db:
            with db.cursor() as cur:
                cur.execute("SET default_transaction_read_only=off")
                try: cur.execute(statement)
                except psycopg2.Error as exc:
                    if exc.pgcode != "42501": raise
                    db.rollback()
                else: raise AssertionError("PostgreSQL accepted a write: "+statement.split()[0])
    query()
    print("SQL Lab and PostgreSQL independently refuse writes (SQLSTATE 42501)")

def oid():
    uri=ENV["SUPERSET_WAREHOUSE_URI"].replace("postgresql+psycopg2://","postgresql://",1)
    with psycopg2.connect(uri) as db:
        with db.cursor() as cur:
            cur.execute("SELECT %s::regclass::oid",(table(),));return cur.fetchone()[0]

def recreate():
    before=oid()
    owner_sql("DROP TABLE "+table()+";")
    create_table()
    after=oid()
    if before == after: raise AssertionError("Table was not recreated")
    query();readonly()
    print("Creator recreated table OID",before,"->",after,"; reader retained SELECT and no writes")

def restart():
    before=state()["database_id"]
    subprocess.run(["systemctl","restart","superset"],check=True)
    for attempt in range(60):
        try:
            if requests.get(BASE+"/health",timeout=2).status_code == 200: break
        except requests.RequestException: pass
        time.sleep(2)
    else: raise RuntimeError("Superset did not recover after restart")
    s=session()
    r=s.get(BASE+"/api/v1/database/"+str(before),timeout=20);r.raise_for_status()
    if r.json()["result"]["database_name"] != NS: raise AssertionError("Metadata identity changed")
    query()
    print("Same metadata connection survived restart")

def teardown():
    if STATE.exists():
        info=state()
        if info.get("database_id") is not None:
            s=session()
            r=s.delete(BASE+"/api/v1/database/"+str(info["database_id"]),timeout=30)
            if r.status_code not in (200,204,404): r.raise_for_status()
        owner_sql("DROP TABLE IF EXISTS "+table()+";")
        STATE.unlink()
    print("Removed only this suite namespace")

if __name__ == "__main__":
    {"setup":setup,"query":query,"readonly":readonly,"recreate":recreate,"restart":restart,"teardown":teardown}[sys.argv[1]]()
