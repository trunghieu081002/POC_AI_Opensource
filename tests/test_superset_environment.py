"""Protected service environment must preserve secrets without shell evaluation."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("superset_env_exec", Path(__file__).parents[1]/"packs/superset/env_exec.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_secret_roundtrip_is_literal(tmp_path):
    canary=tmp_path/"should-not-exist"
    value='quotes" apostrophe\' backslash\\ $HOME $(touch '+str(canary)+') `id` unicode-?'
    path=tmp_path/"service.env"
    path.write_text("SECRET="+json.dumps(value,ensure_ascii=False)+"\n")
    assert module.load_environment(path)=={"SECRET":value}
    assert not canary.exists()

@pytest.mark.parametrize("value",["line\nfeed","tab\tvalue","nul\x00value",42,False,None])
def test_environment_rejects_controls_and_non_strings(tmp_path,value):
    path=tmp_path/"service.env"
    path.write_text("SECRET="+json.dumps(value)+"\n")
    with pytest.raises(ValueError): module.load_environment(path)

def _manager(monkeypatch,tmp_path):
    spec=importlib.util.spec_from_file_location('superset_manage',Path(__file__).parents[1]/'packs/superset/manage.py')
    manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)
    monkeypatch.setattr(manager,'ROOT',tmp_path)
    monkeypatch.setattr(manager,'MARKER',tmp_path/'.dpagent-owned')
    (tmp_path/'.dpagent-owned').write_text('dpagent:superset:v1\n')
    import os
    from types import SimpleNamespace
    monkeypatch.setattr(manager.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid()))
    monkeypatch.setattr(manager.os,'chown',lambda *args:None)
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'packs/superset'))
    for name,value in {'SECRET_KEY':'a'*40,'BACKEND_HOST':'metadata.example','BACKEND_PORT':'5544','BACKEND_DB':'bi_meta','BACKEND_USER':'meta@user','BACKEND_PASSWORD':'meta:p@ss/%','WAREHOUSE_HOST':'127.0.0.1','WAREHOUSE_PORT':'5545','WAREHOUSE_DB':'analytics','WAREHOUSE_ROLE':'report reader','WAREHOUSE_PASSWORD':'reader:p@ss/%','ADMIN_PASSWORD':'admin literal $value'}.items():
        monkeypatch.setenv('DP_PARAM_'+name,value)
    return manager

def test_metadata_and_warehouse_bindings_are_independent(monkeypatch,tmp_path):
    from urllib.parse import urlsplit,unquote
    manager=_manager(monkeypatch,tmp_path)
    manager.config()
    env=module.load_environment(tmp_path/'superset.env')
    meta=urlsplit(env['SUPERSET_METADATA_URI']);warehouse=urlsplit(env['SUPERSET_WAREHOUSE_URI'])
    assert (meta.hostname,meta.port,meta.path)==('metadata.example',5544,'/bi_meta')
    assert (warehouse.hostname,warehouse.port,warehouse.path)==('127.0.0.1',5545,'/analytics')
    assert (unquote(meta.username),unquote(meta.password))==('meta@user','meta:p@ss/%')
    assert (unquote(warehouse.username),unquote(warehouse.password))==('report reader','reader:p@ss/%')
    assert (tmp_path/'superset.env').stat().st_mode & 0o777 == 0o600

def test_reconfigure_cannot_silently_replace_metadata_encryption_key(monkeypatch,tmp_path):
    manager=_manager(monkeypatch,tmp_path)
    manager.config()
    before=(tmp_path/'superset.env').read_bytes()
    monkeypatch.setenv('DP_PARAM_SECRET_KEY','b'*40)
    with pytest.raises(RuntimeError,match='key rotation'):manager.config()
    assert (tmp_path/'superset.env').read_bytes()==before
