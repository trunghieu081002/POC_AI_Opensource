"""install_pack's very first line resolves the pack's params - and used to do
so with nothing catching the error. A missing required param (typically a
secret nothing supplied: no --set, no spec ${ENV_VAR}) raised params.ParamError
straight out of install_pack, through install()'s loop, past state.finish_run(),
and all the way to the terminal as a raw Python traceback - the one place in
the whole install path that broke this project's own rule that a failure is
an instruction, not a stack trace. Found running `dpagent install airflow`
directly (no spec file) against a fresh container."""
from pathlib import Path

from dpagent.engine import runner
from dpagent.library.loader import Pack


def _pack(schema: dict) -> Pack:
    return Pack(name="airflow", version="1.0.0", summary="", maturity="stable",
               root=Path("."), param_schema=schema)


def test_missing_required_param_is_a_failed_outcome_not_a_raised_exception(monkeypatch):
    events = []
    monkeypatch.setattr(runner.state, "event",
                        lambda *a, **k: events.append((a, k)))

    schema = {
        "backend_password": {"type": "string", "required": True, "secret": True},
    }
    engine = runner.Engine(os_info=None, run_id=1)

    outcome = engine.install_pack(_pack(schema), {})

    assert outcome.status == runner.FAILED
    assert "backend_password" in outcome.reason
    # A run this fails inside must still be closeable by install()'s
    # state.finish_run() - it can only get there if install_pack returns
    # instead of propagating the exception.
    assert outcome.steps == []


class _FakeOsInfo:
    family = "rhel"

    def as_env(self):
        return {}


def test_a_satisfiable_schema_is_unaffected(monkeypatch):
    """Guard against the fix over-catching: a pack whose params all resolve
    must run to a normal OK completion, not take the ParamError branch."""
    monkeypatch.setattr(runner.state, "event", lambda *a, **k: None)
    monkeypatch.setattr(runner.state, "get_install", lambda name: None)
    monkeypatch.setattr(runner.state, "params_hash", lambda *a, **k: "deadbeef")
    monkeypatch.setattr(runner.state, "completed_steps", lambda *a, **k: set())
    monkeypatch.setattr(runner.state, "record_install", lambda *a, **k: None)

    schema = {"version": {"type": "string", "default": "1.0"}}
    engine = runner.Engine(os_info=_FakeOsInfo(), run_id=1)

    # No steps/preflight/verify on this fake pack, so install_pack runs
    # straight through to a normal completion.
    outcome = engine.install_pack(_pack(schema), {})

    assert outcome.status == runner.OK


def _installed(monkeypatch, status="installed"):
    monkeypatch.setattr(runner.state, "event", lambda *a, **k: None)
    monkeypatch.setattr(runner.state, "get_install",
                        lambda name: {"status": status, "pack_version": "1.0.0", "params_hash": "other"})
    monkeypatch.setattr(runner.state, "params_hash", lambda *a, **k: "defaults")
    monkeypatch.setattr(runner.state, "completed_steps", lambda *a, **k: set())
    monkeypatch.setattr(runner.state, "record_install", lambda *a, **k: None)


class _Reporter:
    def __init__(self):
        self.skipped = []

    def pack_skipped(self, pack, why):
        self.skipped.append(why)

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_an_installed_pack_pulled_in_only_as_a_dependency_is_not_reconfigured(monkeypatch):
    """A BI-only spec pulled `postgres` in as a dependency with its DEFAULT params and re-ran its config
    step against the running, explicitly configured server (max_connections 200 -> 100). A dependency
    carries no operator intent: if it is installed, it is satisfied as configured."""
    _installed(monkeypatch)
    engine = runner.Engine(os_info=_FakeOsInfo(), run_id=1)
    engine.reporter = _Reporter()
    ran = []
    monkeypatch.setattr(engine, "_run_step", lambda *a, **k: ran.append(a) or None)
    outcome = engine.install_pack(_pack({"max_connections": {"type": "int", "default": 100}}), {},
                                  dependency_only=True)
    assert outcome.status == runner.SKIPPED and ran == []
    assert "not reconfigured with default params" in engine.reporter.skipped[0]


def test_a_requested_pack_with_changed_params_is_still_reconfigured(monkeypatch):
    _installed(monkeypatch)
    engine = runner.Engine(os_info=_FakeOsInfo(), run_id=1)
    outcome = engine.install_pack(_pack({"max_connections": {"type": "int", "default": 100}}),
                                  {"max_connections": 300})
    assert outcome.status == runner.OK          # went through install (no steps on this fake pack)


def test_a_dependency_that_is_not_installed_is_installed(monkeypatch):
    _installed(monkeypatch)
    monkeypatch.setattr(runner.state, "get_install", lambda name: None)
    engine = runner.Engine(os_info=_FakeOsInfo(), run_id=1)
    outcome = engine.install_pack(_pack({"v": {"type": "string", "default": "1"}}), {}, dependency_only=True)
    assert outcome.status == runner.OK
