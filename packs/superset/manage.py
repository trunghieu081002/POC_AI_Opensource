"""Pack-local lifecycle; called through dp_run so dry-run never mutates."""
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path("/opt/superset")
SOURCE = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
MARKER = ROOT / ".dpagent-owned"

def param(name, default=None):
    value = os.environ.get("DP_PARAM_" + name.upper(), default)
    if value is None or value == "": raise ValueError("Missing parameter " + name)
    return value

def run(args, **kw):
    return subprocess.run([str(x) for x in args], check=True, cwd="/", **kw)

def write(path, text, mode=0o644, service=False):
    path = Path(path)
    path.write_text(text)
    path.chmod(mode)
    if service:
        user = pwd.getpwnam("superset")
        os.chown(str(path), user.pw_uid, user.pw_gid)

def app(args):
    run(["runuser", "-u", "superset", "--", VENV/"bin/python", ROOT/"env_exec.py", ROOT/"superset.env"] + args)

def preflight():
    if os.geteuid() != 0: raise RuntimeError("Superset pack requires root")
    if os.environ.get("DP_SVC_MGR") != "systemd": raise RuntimeError("Superset requires systemd")
    if len(param("secret_key")) < 32: raise ValueError("secret_key requires at least 32 characters")
    for name in ("backend_password", "secret_key", "admin_password", "warehouse_password"):
        if any(ord(c) < 32 for c in param(name)): raise ValueError("Control character in " + name)
    if ROOT.exists() and not MARKER.exists(): raise RuntimeError("Refusing an unowned install directory")
    try: existing_user=pwd.getpwnam("superset")
    except KeyError: existing_user=None
    if existing_user is not None and not MARKER.exists(): raise RuntimeError("Refusing an unowned OS user")
    unit=Path("/etc/systemd/system/superset.service")
    if unit.exists() and not MARKER.exists(): raise RuntimeError("Refusing an unowned service")
    if not MARKER.exists():
        with socket.socket() as s:
            if s.connect_ex((param("bind_host","127.0.0.1"),int(param("webserver_port","8088")))) == 0:
                raise RuntimeError("Superset port is already in use")
    for name in ("warehouse_schemas", "warehouse_creator_roles"):
        values=json.loads(param(name))
        if not isinstance(values,list) or not values or any(not isinstance(v,str) or not v for v in values):
            raise ValueError(name + " requires a nonempty string list")
    print("Superset preflight passed")

def runtime():
    if ROOT.exists() and not MARKER.exists(): raise RuntimeError("Refusing an unowned install directory")
    try: pwd.getpwnam("superset")
    except KeyError: run(["useradd","--system","--home-dir",str(ROOT),"--shell","/usr/sbin/nologin","superset"])
    ROOT.mkdir(exist_ok=True)
    write(MARKER,"dpagent:superset:v1\n",0o600)
    candidates=[shutil.which("python3."+str(v)) for v in (11,10,12)]
    interpreter=next((p for p in candidates if p),None)
    if not interpreter: raise RuntimeError("Superset requires Python 3.10-3.12; system interpreter is unchanged")
    run([interpreter,"-m","venv",VENV])
    (ROOT/"home").mkdir(exist_ok=True)
    user=pwd.getpwnam("superset")
    os.chown(str(ROOT/"home"),user.pw_uid,user.pw_gid)

def install():
    run([VENV/"bin/pip","install","--upgrade","pip","wheel"])
    run([VENV/"bin/pip","install","-r",SOURCE/"requirements.txt"])
    run([VENV/"bin/pip","check"])

def config():
    if not MARKER.exists(): raise RuntimeError("Missing ownership marker")
    def uri(user,password,database,host,port):
        return "postgresql+psycopg2://{}:{}@{}:{}/{}".format(quote(user,safe=""),quote(password,safe=""),("["+host+"]" if ":" in host else host),port,quote(database,safe=""))
    env={"SUPERSET_ADMIN_USER":param("admin_user","admin"),"SUPERSET_ADMIN_PASSWORD":param("admin_password"),"SUPERSET_SECRET_KEY":param("secret_key"),"SUPERSET_METADATA_URI":uri(param("backend_user","superset_meta"),param("backend_password"),param("backend_db","superset_meta"),param("backend_host","localhost"),param("backend_port","5432")),"SUPERSET_WAREHOUSE_URI":uri(param("warehouse_role","superset_reader"),param("warehouse_password"),param("warehouse_db","warehouse"),param("warehouse_host","localhost"),param("warehouse_port","5432")),"SUPERSET_CONFIG_PATH":str(ROOT/"superset_config.py"),"SUPERSET_HOME":str(ROOT/"home"),"FLASK_APP":"superset"}
    if (ROOT/"superset.env").exists():
        from env_exec import load_environment
        old=load_environment(ROOT/"superset.env")
        if old["SUPERSET_SECRET_KEY"] != env["SUPERSET_SECRET_KEY"]:
            raise RuntimeError("Changing the encryption key requires an explicit metadata key rotation")
    write(ROOT/"superset.env","".join(k+"="+json.dumps(v,ensure_ascii=False)+"\n" for k,v in env.items()),0o600,True)
    for name in ("env_exec.py","superset_config.py","reader.py"):
        shutil.copyfile(str(SOURCE/name),str(ROOT/name));(ROOT/name).chmod(0o644)

def reader():
    marker=ROOT/"reader-owned.json"
    identity={"role":param("warehouse_role","superset_reader"),"database":param("warehouse_db","warehouse"),"schemas":json.loads(param("warehouse_schemas")),"creators":json.loads(param("warehouse_creator_roles")),"port":int(param("warehouse_port","5432"))}
    previous=json.loads(marker.read_text()) if marker.exists() else None
    if previous and previous != identity: raise RuntimeError("Reader identity/schema/creator change requires explicit migration")
    payload=dict(identity,password=param("warehouse_password"),owned=previous==identity)
    run(["runuser","-u","postgres","--",VENV/"bin/python",ROOT/"reader.py"],input=json.dumps(payload).encode())
    write(marker,json.dumps(identity),0o600)

def metadata():
    app([str(VENV/"bin/superset"),"db","upgrade"])
    # Application ORM provides exact username identity; never use CLI's human table output.
    code="""import os
from superset.app import create_app
app=create_app()
with app.app_context():
 from flask_appbuilder.security.sqla.models import User
 from superset import db
 from superset.extensions import appbuilder
 username=os.environ['DP_SUPERSET_ADMIN_USER']
 user=db.session.query(User).filter_by(username=username).one_or_none()
 if user is None:
  user=appbuilder.sm.add_user(username,'DPAgent','Admin',os.environ['DP_SUPERSET_ADMIN_EMAIL'],appbuilder.sm.find_role('Admin'),os.environ['DP_SUPERSET_ADMIN_PASSWORD'])
  if not user: raise RuntimeError('Admin creation failed')
 else:
  if not any(r.name=='Admin' for r in user.roles): raise RuntimeError('Existing username is not an administrator')
"""
    env=dict(os.environ,DP_SUPERSET_ADMIN_USER=param("admin_user","admin"),DP_SUPERSET_ADMIN_EMAIL=param("admin_email","admin@example.com"),DP_SUPERSET_ADMIN_PASSWORD=param("admin_password"))
    # runuser preserves these values; env_exec loads the protected app environment.
    run(["runuser","-u","superset","--",VENV/"bin/python",ROOT/"env_exec.py",ROOT/"superset.env",VENV/"bin/python","-c",code],env=env)
    app([str(VENV/"bin/superset"),"init"])

def service():
    host=param("bind_host","127.0.0.1")
    import ipaddress
    ipaddress.ip_address(host)
    port=int(param("webserver_port","8088"))
    if not 1 <= port <= 65535: raise ValueError("Invalid port")
    unit="""[Unit]
Description=Apache Superset (managed by dpagent)
After=network.target postgresql.service
[Service]
User=superset
Group=superset
WorkingDirectory=/opt/superset/home
EnvironmentFile=/opt/superset/superset.env
ExecStart=/opt/superset/.venv/bin/gunicorn --workers 2 --worker-class gevent --timeout 120 --bind {host}:{port} superset.app:create_app()
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
""".format(host=host,port=port)
    write("/etc/systemd/system/superset.service",unit)
    run(["systemctl","daemon-reload"])
    run(["systemctl","enable","superset"])
    run(["systemctl","restart","superset"])

def rollback():
    if not MARKER.exists(): raise RuntimeError("Refusing rollback without ownership marker")
    unit=Path("/etc/systemd/system/superset.service")
    if unit.exists():
        run(["systemctl","disable","--now","superset"])
        unit.unlink()
    run(["systemctl","daemon-reload"])
    # Keep the reader ownership receipt to support safe reinstall. Preserve all DBs.
    for path in (VENV,ROOT/"home"):
        if path.exists(): shutil.rmtree(str(path))
    for name in ("superset.env","superset_config.py","env_exec.py","reader.py"):
        path=ROOT/name
        if path.exists(): path.unlink()
    print("Removed owned service/runtime; metadata, warehouse, reader grants and ownership receipts retained")

if __name__ == "__main__":
    actions={"preflight":preflight,"runtime":runtime,"install":install,"config":config,"reader":reader,"metadata":metadata,"service":service,"rollback":rollback}
    actions[sys.argv[1]]()
