"""_stored_params rebuilds a pack's params for a post-install operation
(verify/test/rollback) from what was recorded at install time."""
import json
from pathlib import Path

from dpagent.cli import operate
from dpagent.engine import params as params_mod
from dpagent.library.loader import Pack


def _pack(schema: dict) -> Pack:
    return Pack(name="airflow", version="1.0.0", summary="", maturity="stable",
               root=Path("."), param_schema=schema)


def test_required_secret_is_filled_with_a_placeholder_not_left_missing(monkeypatch):
    """The regression: airflow's backend_password/admin_password are both
    `required` and `secret`. Once masked at rest, dropping the masked value
    made resolve() refuse the whole pack as missing a required param -
    forever, on every post-install operation, since a required secret can
    never be reconstructed from storage by design. The suite/verify scripts
    that actually need such a value are expected to read it from what
    install configured (a generated profile, an env file), not from here."""
    schema = {
        "backend_password": {"type": "string", "required": True, "secret": True},
        "webserver_port": {"type": "int", "default": 8090},
    }
    monkeypatch.setattr(operate.state, "get_install", lambda name: {
        "params_json": json.dumps({"backend_password": params_mod.MASK, "webserver_port": 8090}),
    })

    resolved = operate._stored_params(_pack(schema))

    assert resolved["webserver_port"] == 8090
    assert resolved["backend_password"]
    assert resolved["backend_password"] != params_mod.MASK


def test_non_required_secret_still_falls_back_to_default(monkeypatch):
    """Only the required+secret combination needed the placeholder - an
    optional secret with a default must keep resolving normally."""
    schema = {
        "db_password": {"type": "string", "default": "", "secret": True},
    }
    monkeypatch.setattr(operate.state, "get_install", lambda name: {
        "params_json": json.dumps({"db_password": params_mod.MASK}),
    })

    resolved = operate._stored_params(_pack(schema))

    assert resolved["db_password"] == ""


def test_no_install_record_still_raises_for_a_required_non_secret_param(monkeypatch):
    """The fix is scoped to secrets - a required, non-secret param that is
    genuinely missing must still fail loudly, not be papered over."""
    schema = {"version": {"type": "string", "required": True}}
    monkeypatch.setattr(operate.state, "get_install", lambda name: None)

    try:
        operate._stored_params(_pack(schema))
        assert False, "expected a ParamError"
    except params_mod.ParamError:
        pass
