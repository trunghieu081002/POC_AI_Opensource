"""Project-neutral native Superset assets; all business SQL lives in project files."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
try:
    import simplejson as json
except ImportError:  # Static dry-run also works before the Superset venv exists.
    import json
import os
import re
import shlex
import sys
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

class ExpectedLoader(yaml.SafeLoader):
    pass

ExpectedLoader.add_constructor("tag:yaml.org,2002:float", lambda loader,node: Decimal(loader.construct_scalar(node).replace("_", "")))

class ComparisonFailure(RuntimeError):
    def __init__(self, observations):
        super().__init__("BI chart/independent SQL differs from expected")
        self.observations = observations

KINDS = {"databases": "dbs", "datasets": "tables", "charts": "slices", "dashboards": "dashboards"}
ROOT = Path(__file__).resolve().parents[2]
STATE = Path("/var/lib/dpagent-bi-assets")


def safe_path(raw: str) -> Path:
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "projects":
        raise ValueError("BI artifact paths must be relative to projects/")
    current = ROOT
    for part in relative.parts:
        if part in ("..", "."):
            raise ValueError("BI path traversal refused")
        current = current / part
        if current.is_symlink():
            raise ValueError("BI symlinks refused")
    current.resolve().relative_to((ROOT / "projects").resolve())
    return current


def identity(uri: str) -> dict:
    value = urlsplit(uri)
    return {"scheme": value.scheme, "host": value.hostname, "port": value.port or 5432,
            "user": unquote(value.username or ""), "database": unquote(value.path)}


def canonical(value):
    if value is None: return ["null"]
    if isinstance(value, bool): return ["boolean", value]
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite(): raise ValueError("Non-finite result")
        if number == 0: return ["number", "0"]
        sign, digits, exponent = number.as_tuple()
        digits = list(digits)
        while digits and digits[-1] == 0:
            digits.pop(); exponent += 1
        return ["number", sign, "".join(str(d) for d in digits), exponent]
    if isinstance(value, str): return ["string", value]
    if isinstance(value, list): return ["list", [canonical(v) for v in value]]
    if isinstance(value, dict): return ["object", [[k, canonical(v)] for k, v in sorted(value.items())]]
    raise ValueError("Unsupported expected/result type")


def same_rows(actual, expected) -> bool:
    return Counter(json.dumps(canonical(r)) for r in actual) == Counter(json.dumps(canonical(r)) for r in expected)


def read_project(config: dict, binding: dict) -> dict:
    name = config.get("name", "")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", name):
        raise ValueError("Invalid BI project name")
    directory = safe_path(config["bundle_dir"])
    expected_path = safe_path(config["expected_file"])
    content = {}
    hashes = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink(): raise ValueError("BI symlinks refused")
        if not path.is_file(): continue
        relative = path.relative_to(directory).as_posix()
        if path.suffix not in (".yaml", ".yml"): raise ValueError("Native BI bundle must contain YAML only")
        if len(content) >= 1000 or path.stat().st_size > 2_000_000: raise ValueError("BI bundle exceeds size limit")
        raw = path.read_bytes()
        if len(raw) > 2_000_000: raise ValueError("BI asset exceeds size limit")
        hashes[relative] = hashlib.sha256(raw).hexdigest()
        content[relative] = yaml.safe_load(raw)
    if not content or len(content) > 1000: raise ValueError("Invalid BI bundle size")
    if content.get("metadata.yaml", {}).get("type") != "assets":
        raise ValueError("Expected native assets metadata")
    resources = {kind: {} for kind in KINDS}
    seen = set()
    for path, item in content.items():
        if path == "metadata.yaml": continue
        kind = path.split("/", 1)[0]
        if kind not in KINDS or not isinstance(item, dict): raise ValueError("Unsupported BI resource")
        key = str(uuid.UUID(item["uuid"]))
        if key != item["uuid"]: raise ValueError("Use canonical lowercase BI UUIDs")
        if key in seen: raise ValueError("Duplicate BI UUID")
        seen.add(key); resources[kind][key] = item
        if kind == "databases":
            if any(item.get(field) for field in ("allow_dml", "allow_ctas", "allow_cvas", "allow_file_upload", "allow_run_async", "impersonate_user")):
                raise ValueError("BI warehouse connections must be SELECT-only")
            if any(item.get(field) for field in ("ssh_tunnel","encrypted_extra","masked_encrypted_extra","server_cert","parameters")):
                raise ValueError("BI connections use the installed environment URI only")
            extra = item.get("extra") or {}
            if isinstance(extra,str): extra=json.loads(extra)
            if extra.get("engine_params") or extra.get("metadata_params"):
                raise ValueError("BI connection overrides are not allowed in artifacts")
            if item.get("password"): raise ValueError("Store BI passwords in environment, not artifacts")
            supplied = urlsplit(item.get("sqlalchemy_uri", ""))
            if supplied.password and supplied.password not in ("XXXXXXXXXX", "**********"):
                raise ValueError("Store BI credentials in environment, not artifacts")
    if any(not resources[k] for k in KINDS): raise ValueError("Project needs database, dataset, chart and dashboard")
    names=[item["database_name"] for item in resources["databases"].values()]
    if len(set(names)) != len(names): raise ValueError("Duplicate BI database name")
    tables=[(item["database_uuid"],item.get("schema"),item["table_name"]) for item in resources["datasets"].values()]
    if len(set(tables)) != len(tables): raise ValueError("Duplicate BI dataset identity")
    slugs=[item.get("slug",key) for key,item in resources["dashboards"].items()]
    if len(set(slugs)) != len(slugs): raise ValueError("Duplicate BI dashboard slug")
    for item in resources["datasets"].values():
        if item.get("data") or item.get("data_file"): raise ValueError("BI artifacts cannot import warehouse data")
        if item["database_uuid"] not in resources["databases"]: raise ValueError("Foreign database UUID")
    for item in resources["charts"].values():
        if item["dataset_uuid"] not in resources["datasets"]: raise ValueError("Foreign dataset UUID")
    raw_expected = expected_path.read_bytes()
    expected = yaml.load(raw_expected, Loader=ExpectedLoader)
    if not isinstance(expected, dict) or not expected.get("checks"): raise ValueError("Independent BI checks are required")
    for check in expected["checks"]:
        if check["chart_uuid"] not in resources["charts"]: raise ValueError("Expected names an unknown chart")
        if not isinstance(check.get("sql"), str) or not isinstance(check.get("rows"), list):
            raise ValueError("Every chart check needs independent SQL and rows")
        canonical(check["rows"])
        for filtered in check.get("filters", []):
            if filtered["dashboard_uuid"] not in resources["dashboards"]: raise ValueError("Unknown dashboard filter")
            if not isinstance(filtered.get("rows"), list) or not isinstance(filtered.get("sql"), str):
                raise ValueError("Every filter check needs independent SQL and rows")
            canonical(filtered["rows"])
    fingerprint = hashlib.sha256(json.dumps({"project": name, "files": hashes,
        "expected": hashlib.sha256(raw_expected).hexdigest(), "binding": binding}, sort_keys=True).encode()).hexdigest()
    return {"name": name, "content": content, "resources": resources, "expected": expected,
            "fingerprint": fingerprint, "binding": binding, "config": config}


def rewrite(value, mapping):
    if isinstance(value, dict): return {k: rewrite(v, mapping) for k, v in value.items()}
    if isinstance(value, list): return [rewrite(v, mapping) for v in value]
    if isinstance(value, str):
        if value in mapping: return mapping[value]
        if value.startswith(("{", "[")):
            try: return json.dumps(rewrite(json.loads(value), mapping))
            except ValueError: pass
    return value


def bundle(project, uri, namespace=None):
    mapping = {key: str(uuid.uuid5(namespace, key)) for values in project["resources"].values() for key in values} if namespace else {}
    content = rewrite(copy.deepcopy(project["content"]), mapping)
    prefix = project["name"] + ("-validate-" + namespace.hex[:12] if namespace else "")
    for path, item in content.items():
        kind = path.split("/", 1)[0]
        if kind == "databases":
            item["database_name"] = prefix + " / " + item["database_name"]
            item["sqlalchemy_uri"] = uri
            for field in ("allow_dml", "allow_ctas", "allow_cvas", "allow_file_upload"): item[field] = False
        elif kind == "dashboards": item["slug"] = prefix + "-" + item.get("slug", item["uuid"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, item in content.items(): archive.writestr("assets/" + path, yaml.safe_dump(item, sort_keys=False))
    return buffer.getvalue(), mapping


def assert_managed_endpoint(base_url, unit_path=Path("/etc/systemd/system/superset.service")):
    parsed=urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1","localhost","::1") or parsed.username or parsed.password:
        raise ValueError("BI API must use the local managed Superset endpoint without URL credentials")
    lines=unit_path.read_text().splitlines()
    commands=[line.split("=",1)[1] for line in lines if line.startswith("ExecStart=")]
    if len(commands)!=1: raise ValueError("Cannot identify managed Superset service")
    args=shlex.split(commands[0]); binding=args[args.index("--bind")+1]
    host,port=binding.rsplit(":",1)
    if host.strip("[]") not in ("127.0.0.1","0.0.0.0","::1","::","localhost") or parsed.port != int(port):
        raise ValueError("BI API port does not match the local managed Superset service")


class Client:
    def __init__(self, base_url, env):
        assert_managed_endpoint(base_url)
        import requests
        import psycopg2
        self.env = env; self.base = base_url.rstrip("/"); self.http = requests.Session()
        self.binding = {"environment": os.environ.get("DP_PARAM_ENVIRONMENT","dev"), "superset": base_url, "warehouse": identity(env["SUPERSET_WAREHOUSE_URI"])}
        self.db = psycopg2.connect(env["SUPERSET_METADATA_URI"].replace("postgresql+psycopg2://", "postgresql://", 1))
        self.db.set_session(readonly=True, autocommit=True)
        self.login()

    def login(self):
        r = self.http.post(self.base + "/api/v1/security/login", json={"username": self.env["SUPERSET_ADMIN_USER"], "password": self.env["SUPERSET_ADMIN_PASSWORD"], "provider": "db", "refresh": True}, timeout=20)
        r.raise_for_status(); self.http.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        r = self.http.get(self.base + "/api/v1/security/csrf_token/", timeout=20); r.raise_for_status()
        self.http.headers["X-CSRFToken"] = r.json()["result"]; self.http.headers["Referer"] = self.base + "/"

    def request(self, method, path, **kwargs):
        response = self.http.request(method, self.base + path, timeout=60, **kwargs)
        if response.status_code == 401:
            self.login(); response = self.http.request(method, self.base + path, timeout=60, **kwargs)
        response.raise_for_status()
        return response

    def lookup(self, resources, mapping=None):
        from psycopg2 import sql
        result = {}
        with self.db.cursor() as cur:
            for kind, values in resources.items():
                keys = [(mapping or {}).get(k, k) for k in values]
                if not keys: result[kind] = {}; continue
                cur.execute(sql.SQL("SELECT id, uuid::text FROM {} WHERE uuid=ANY(%s::uuid[])").format(sql.Identifier(KINDS[kind])), (keys,))
                result[kind] = {key: identifier for identifier, key in cur.fetchall()}
        return result

    def import_bundle(self, data):
        self.request("POST", "/api/v1/assets/import/", files={"bundle": ("assets.zip", data, "application/zip")})

    def assert_no_foreign_references(self, ids):
        with self.db.cursor() as cur:
            charts=list(ids["charts"].values()); datasets=list(ids["datasets"].values()); databases=list(ids["databases"].values())
            if charts:
                cur.execute("SELECT dashboard_id FROM dashboard_slices WHERE slice_id=ANY(%s)",(charts,))
                if any(row[0] not in ids["dashboards"].values() for row in cur.fetchall()): raise RuntimeError("Refusing unowned Superset dashboard reference")
            if datasets:
                cur.execute("SELECT id FROM slices WHERE datasource_type='table' AND datasource_id=ANY(%s)",(datasets,))
                if any(row[0] not in charts for row in cur.fetchall()): raise RuntimeError("Refusing unowned Superset chart reference")
            if databases:
                cur.execute('SELECT id FROM "tables" WHERE database_id=ANY(%s)',(databases,))
                if any(row[0] not in datasets for row in cur.fetchall()): raise RuntimeError("Refusing unowned Superset dataset reference")
                cur.execute('SELECT id FROM saved_query WHERE db_id=ANY(%s)',(databases,))
                if cur.fetchone(): raise RuntimeError("Refusing unowned Superset saved query reference")

    def assert_names_owned(self, project, previous):
        with self.db.cursor() as cur:
            for key,item in project["resources"]["databases"].items():
                cur.execute("SELECT uuid::text FROM dbs WHERE database_name=%s",(project["name"]+" / "+item["database_name"],))
                found=cur.fetchone()
                if found and found[0]!=key: raise RuntimeError("Refusing unowned Superset database name")
            for key,item in project["resources"]["datasets"].items():
                cur.execute('SELECT t.uuid::text FROM "tables" t JOIN dbs d ON t.database_id=d.id WHERE d.uuid=%s::uuid AND t.table_name=%s AND t.schema IS NOT DISTINCT FROM %s',(item["database_uuid"],item["table_name"],item.get("schema")))
                found=cur.fetchone()
                if found and found[0]!=key: raise RuntimeError("Refusing unowned Superset dataset identity")
            for key,item in project["resources"]["dashboards"].items():
                cur.execute("SELECT uuid::text FROM dashboards WHERE slug=%s",(project["name"]+"-"+item.get("slug",key),))
                found=cur.fetchone()
                if found and found[0]!=key: raise RuntimeError("Refusing unowned Superset dashboard slug")

    def cleanup(self, resources, mapping=None, receipt=None):
        ids = self.lookup(resources, mapping)
        if receipt is not None:
            for kind,values in ids.items():
                if any(receipt.get(kind,{}).get(key) != identifier for key,identifier in values.items()):
                    raise RuntimeError("Refusing unowned Superset id changed after deployment")
        self.assert_no_foreign_references(ids)
        for kind in ("dashboards", "charts", "datasets", "databases"):
            resource = {"dashboards": "dashboard", "charts": "chart", "datasets": "dataset", "databases": "database"}[kind]
            for key, identifier in ids[kind].items():
                self.request("DELETE", "/api/v1/" + resource + "/" + str(identifier))
        if any(self.lookup(resources, mapping).values()): raise RuntimeError("BI cleanup leaked owned UUIDs")

    def query(self, database_id, statement, expected_count):
        limit=expected_count+1
        if limit>10000: raise ValueError("BI expected queries are limited to fewer than 10000 result rows")
        result = self.request("POST", "/api/v1/sqllab/execute/", json={"database_id": database_id, "sql": statement, "runAsync": False, "queryLimit": limit}).json(parse_float=Decimal)
        if result.get("status") != "success": raise RuntimeError("Independent BI SQL did not succeed")
        actual_limit=result.get("query",{}).get("limit")
        if not isinstance(actual_limit,int) or actual_limit<limit or len(result["data"])>=limit:
            raise RuntimeError("Independent BI SQL reached an unverified row limit")
        return result["data"]

    def chart(self, identifier, filters=None, dataset_id=None):
        chart = self.request("GET", "/api/v1/chart/" + str(identifier)).json()["result"]
        if dataset_id is not None and chart["datasource_id"] != dataset_id: raise RuntimeError("Chart binds a different dataset")
        context = json.loads(chart["query_context"],use_decimal=True)
        context.setdefault("form_data",{})["slice_id"] = identifier
        context["form_data"]["datasource"] = str(chart["datasource_id"])+"__"+chart["datasource_type"]
        if chart["viz_type"] == "table":
            params = json.loads(chart["params"])
            if len(context["queries"]) != 1 or params.get("metrics",[]) != context["queries"][0].get("metrics",[]) or params.get("groupby",[]) != context["queries"][0].get("columns",[]):
                raise RuntimeError("Saved table controls and chart query context disagree")
        context["datasource"] = {"id": chart["datasource_id"], "type": chart["datasource_type"]}
        context["force"] = True; context["result_format"] = "json"; context["result_type"] = "full"
        for query in context["queries"]:
            query.setdefault("filters", []).extend(filters or [])
        response = self.request("POST", "/api/v1/chart/data", json=context).json(parse_float=Decimal)
        results = response.get("result", [])
        if len(results) != 1 or results[0].get("error"): raise RuntimeError("Expected one successful chart query")
        return results[0]["data"]

    def filter_column(self, dashboard_id, filter_id, dataset_id, chart_id):
        dashboard = self.request("GET", "/api/v1/dashboard/" + str(dashboard_id)).json()["result"]
        metadata = json.loads(dashboard["json_metadata"])
        candidates = [f for f in metadata.get("native_filter_configuration", []) if f["id"] == filter_id]
        if len(candidates) != 1 or len(candidates[0].get("targets", [])) != 1:
            raise RuntimeError("Expected one declared native filter target")
        chosen = candidates[0]
        scope = chosen.get("scope", {})
        if scope.get("rootPath") != ["ROOT_ID"] or chart_id in scope.get("excluded", []):
            raise RuntimeError("This verifier supports global native filters applying to the chart")
        position = json.loads(dashboard["position_json"])
        if not any(isinstance(v,dict) and v.get("type")=="CHART" and v.get("meta",{}).get("chartId")==chart_id for v in position.values()):
            raise RuntimeError("Chart is not part of the dashboard")
        target = chosen["targets"][0]
        if target["datasetId"] != dataset_id: raise RuntimeError("Native filter binds a different dataset")
        return target["column"]["name"]


def check_results(client, project, mapping=None):
    mapping = mapping or {}
    ids = client.lookup(project["resources"], mapping)
    if any(len(ids[k]) != len(project["resources"][k]) for k in KINDS): raise RuntimeError("Missing imported BI UUID")
    observations = []
    for check in project["expected"]["checks"]:
        chart_uuid = check["chart_uuid"]; chart = project["resources"]["charts"][chart_uuid]
        dataset_uuid = chart["dataset_uuid"]; dataset = project["resources"]["datasets"][dataset_uuid]
        database_id = ids["databases"][mapping.get(dataset["database_uuid"], dataset["database_uuid"])]
        chart_id = ids["charts"][mapping.get(chart_uuid, chart_uuid)]
        dataset_id = ids["datasets"][mapping.get(dataset_uuid, dataset_uuid)]
        cases = [(check, [])]
        for filtered in check.get("filters", []):
            key = filtered["dashboard_uuid"]
            column = client.filter_column(ids["dashboards"][mapping.get(key, key)], filtered["native_filter_id"], dataset_id, chart_id)
            cases.append((filtered, [{"col": column, "op": "IN", "val": filtered["values"]}]))
        for expected, filters in cases:
            actual = client.chart(chart_id, filters, dataset_id)
            independent = client.query(database_id, expected["sql"],len(expected["rows"]))
            observation = {"chart_uuid": chart_uuid, "filters": filters, "expected": expected["rows"], "chart_rows": actual, "sql_rows": independent}
            observation["matched"] = same_rows(actual, expected["rows"]) and same_rows(independent, expected["rows"])
            observations.append(observation)
            if not observation["matched"]: raise ComparisonFailure(observations)
    return ids, observations


def write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n"); temporary.chmod(0o600)
    os.replace(temporary, path)


def validate(client, project):
    namespace = uuid.uuid4(); data, mapping = bundle(project, client.env["SUPERSET_WAREHOUSE_URI"], namespace)
    report = {"fingerprint": project["fingerprint"], "project": project["name"], "status": "fail", "cleanup": "not_run", "observations": [], "uuid_map": mapping, "resources": {kind:list(values) for kind,values in project["resources"].items()}}
    try:
        if any(client.lookup(project["resources"], mapping).values()): raise RuntimeError("Validation UUID collision")
        client.import_bundle(data)
        report["ids"] = client.lookup(project["resources"], mapping)
        _, report["observations"] = check_results(client, project, mapping)
        report["status"] = "pass"
    except Exception as exc:
        report["error"] = type(exc).__name__
        report["observations"] = getattr(exc,"observations",report["observations"])
        raise
    finally:
        try:
            client.cleanup(project["resources"], mapping)
            report["cleanup"] = "pass"
        except Exception as exc:
            report["status"] = "fail"; report["cleanup"] = "fail"; report["cleanup_error"] = type(exc).__name__
            raise
        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            write_state(STATE / project["name"] / ("validation-" + namespace.hex + ".json"), report)
            write_state(STATE / project["name"] / "validation.json", report)
    return report


def deployment_path(project): return STATE / project["name"] / "deployment.json"


def deploy(client, project, approved_by):
    if not approved_by or project["config"].get("approved_sha256") != project["fingerprint"]:
        raise RuntimeError("BI approval does not match artifact, expected and environment binding")
    path = deployment_path(project); previous = json.loads(path.read_text(),use_decimal=True) if path.exists() else None
    if previous and previous["status"] == "deployed" and previous["binding"] != project["binding"]:
        raise RuntimeError("BI binding migration requires explicit undeploy on the old target")
    if previous and previous["status"] != "deployed": previous=None
    if previous and any(set(previous["resources"][kind])-set(project["resources"][kind]) for kind in KINDS):
        raise RuntimeError("Removing BI UUIDs requires explicit project undeploy before redeployment")
    existing = client.lookup(project["resources"])
    for kind, values in existing.items():
        for key, identifier in values.items():
            if not previous or previous["ids"].get(kind, {}).get(key) != identifier:
                raise RuntimeError("Refusing unowned Superset UUID")
    client.assert_names_owned(project, previous)
    validate(client, project)
    data, _ = bundle(project, client.env["SUPERSET_WAREHOUSE_URI"])
    try:
        client.import_bundle(data)
        ids, observations = check_results(client, project)
    except Exception as exc:
        attempt={"status":"fail","fingerprint":project["fingerprint"],"error":type(exc).__name__,"observations":getattr(exc,"observations",[])}
        try:
            if previous:
                old = dict(project, content=previous["content"], resources=previous["resources"], expected=previous["expected"])
                restored, _ = bundle(old, client.env["SUPERSET_WAREHOUSE_URI"])
                client.import_bundle(restored)
                extra={kind:{key:item for key,item in project["resources"][kind].items() if key not in previous["resources"][kind]} for kind in KINDS}
                client.cleanup(extra)
                attempt["restored_previous_configuration"] = True
            else: client.cleanup(project["resources"])
        except Exception as restore_error:
            attempt["restore_error"]=type(restore_error).__name__
            raise
        finally: write_state(path.parent/("deployment-attempt-"+uuid.uuid4().hex+".json"),attempt)
        raise
    if previous: write_state(path.parent/"history"/(previous["fingerprint"]+".json"),previous)
    write_state(path, {"name": project["name"], "fingerprint": project["fingerprint"], "binding": project["binding"], "approved_by": approved_by, "ids": ids, "content": project["content"], "resources": project["resources"], "expected": project["expected"], "observations": observations, "status": "deployed"})
    return ids


def undeploy(client, name):
    path = STATE / name / "deployment.json"
    if not path.exists(): return
    record = json.loads(path.read_text(),use_decimal=True)
    if record["binding"] != client.binding: raise RuntimeError("BI undeploy target differs from the ownership receipt")
    if record["status"] == "undeployed": return
    client.cleanup(record["resources"], receipt=record["ids"])
    record["status"] = "undeployed"; write_state(path, record)


def environment():
    spec = importlib.util.spec_from_file_location("superset_pack_env", "/opt/superset/env_exec.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.load_environment("/opt/superset/superset.env")


def main(action):
    if action not in ("preflight-static","preflight","apply","verify","rollback"): raise ValueError("Unknown BI action")
    configs = json.loads(os.environ["DP_PARAM_PROJECTS"])
    if not isinstance(configs, list) or not configs: raise ValueError("At least one BI project is required")
    names = [c.get("name", "") for c in configs]
    if len(set(names)) != len(names) or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", n) for n in names): raise ValueError("Invalid or duplicate BI project")
    if action == "preflight-static":
        if os.environ.get("DP_PARAM_MODE","validate") != "undeploy":
            for config in configs: read_project(config,{})
        return
    env = environment(); base = os.environ.get("DP_PARAM_BASE_URL", "http://127.0.0.1:8088")
    binding = {"environment": os.environ.get("DP_PARAM_ENVIRONMENT", "dev"), "superset": base, "warehouse": identity(env["SUPERSET_WAREHOUSE_URI"])}
    mode = os.environ.get("DP_PARAM_MODE", "validate")
    if mode not in ("validate","deploy","undeploy"): raise ValueError("Unknown BI mode")
    projects = [read_project(config, binding) for config in configs] if mode != "undeploy" and action != "rollback" else []
    seen=set()
    for project in projects:
        keys={key for values in project["resources"].values() for key in values}
        if seen & keys: raise ValueError("BI projects cannot share owned UUIDs")
        seen.update(keys)
    if action == "preflight": return
    client = Client(base, env)
    if mode == "undeploy" and action == "verify":
        for name in names:
            path = STATE / name / "deployment.json"
            if path.exists():
                record = json.loads(path.read_text(),use_decimal=True)
                if record["binding"] != client.binding or record["status"] != "undeployed" or any(client.lookup(record["resources"]).values()):
                    raise RuntimeError("BI undeploy is incomplete")
    elif action == "rollback" or mode == "undeploy":
        for name in names: undeploy(client, name)
    elif action == "apply":
        for project in projects:
            if mode == "validate": validate(client, project)
            else: deploy(client, project, os.environ.get("DP_PARAM_APPROVED_BY", ""))
            print(project["name"], "mode=" + mode, "fingerprint=" + project["fingerprint"])
    elif action == "verify":
        for project in projects:
            path = STATE / project["name"] / ("deployment.json" if mode == "deploy" else "validation.json")
            record = json.loads(path.read_text(),use_decimal=True)
            if record["fingerprint"] != project["fingerprint"]: raise RuntimeError("BI state fingerprint is stale")
            if mode == "validate":
                if record["status"] != "pass" or record["cleanup"] != "pass": raise RuntimeError("BI validation/cleanup did not pass")
            else:
                ids, _ = check_results(client, project)
                if record["status"] != "deployed" or ids != record["ids"]: raise RuntimeError("BI deployed UUID identities changed")
            print(project["name"], "verified", mode)


if __name__ == "__main__": main(sys.argv[1])
