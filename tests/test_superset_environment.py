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
