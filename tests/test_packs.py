"""The shipped packs must pass the same bar every synthesized draft has to."""
import pytest

from dpagent.library import lint
from dpagent.library import loader as packs


def test_postgres_pack_loads():
    pack = packs.load("postgres")
    assert pack.name == "postgres"
    assert pack.maturity == "stable"
    assert pack.families == ["debian", "rhel"]
    assert [s.id for s in pack.steps] == ["repo", "install", "initdb", "config", "databases"]


def test_postgres_declares_the_full_lifecycle():
    pack = packs.load("postgres")
    assert pack.preflight and pack.verify and pack.rollback


@pytest.mark.parametrize("name", packs.available())
def test_every_shipped_pack_lints_clean(name):
    blocking = lint.blocking(lint.lint_pack(name))
    assert not blocking, "\n".join(str(i) for i in blocking)


@pytest.mark.parametrize("name", packs.available())
def test_every_step_script_sources_the_shared_lib(name):
    """A script that skips dp.sh ignores DP_DRY_RUN, which makes --dry-run lie."""
    pack = packs.load(name)
    for step in pack.steps:
        text = pack.path(step.script).read_text(encoding="utf-8")
        assert "DP_LIB" in text, f"{name}/{step.script} does not source dp.sh"


def test_secret_params_are_declared_as_secret():
    pack = packs.load("postgres")
    users = pack.param_schema["users"]
    assert "password" in users.get("secret_fields", []), (
        "a param carrying passwords must declare secret_fields, or they reach the logs")


def test_steps_have_guards_so_reruns_can_skip():
    pack = packs.load("postgres")
    guarded = [s.id for s in pack.steps if s.guard]
    assert {"repo", "install", "initdb"} <= set(guarded)


def test_base_pack_exists_and_is_a_dependency():
    """Foundational tooling is a pack, not something the operator must know to
    install first: a minimal image has no ss, fuser, locale or python3, and the
    failures that causes point nowhere near the real cause."""
    base = packs.load("base")
    assert base.maturity == "stable"
    assert "base" in packs.load("postgres").requires


@pytest.mark.parametrize("name", packs.available())
def test_no_shipped_script_trips_the_set_e_and_trap(name):
    """`A && B` as a bare statement aborts the script when A is false. Three
    shipped scripts had this before the linter learned to catch it."""
    offenders = [str(i) for i in lint.lint_pack(name)
                 if "exits the script under" in i.message]
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("name", packs.available())
def test_no_shipped_script_mutates_outside_dp_run(name):
    """Otherwise --dry-run is decorative."""
    offenders = [str(i) for i in lint.lint_pack(name)
                 if "without dp_run/dp_sh" in i.message]
    assert not offenders, "\n".join(offenders)


def test_capability_lookup():
    assert packs.find_provider("rdbms") == "postgres"
    assert packs.find_provider("postgres") == "postgres"
    assert packs.find_provider("nothing-provides-this") is None


def test_reserved_directories_are_not_packs():
    assert "_lib" not in packs.available()
    assert "_template" not in packs.available()


def test_host_needs_parses_from_the_manifest():
    airflow = packs.load("airflow")
    assert airflow.host_needs.memory_mb == 2048
    assert airflow.host_needs.disk_mb == {"/opt": 2048}


def test_host_needs_defaults_are_empty_when_undeclared():
    postgres = packs.load("postgres")
    assert postgres.host_needs.memory_mb == 0
    assert postgres.host_needs.ports == []


def test_dbt_and_airflow_depend_on_python_modern():
    assert "python-modern" in packs.load("dbt").requires
    assert "python-modern" in packs.load("airflow").requires


def test_airflow_requires_postgres_as_a_declared_dependency():
    """Composition (which database/role) happens via spec params, not
    automatically — but the resolver must still install postgres first."""
    assert "postgres" in packs.load("airflow").requires


def test_airflow_secrets_are_required_not_defaulted():
    """A blank admin/backend password must fail loudly at param resolution,
    not silently proceed and create an unusable install."""
    from dpagent.engine import params as params_mod
    schema = packs.load("airflow").param_schema
    with pytest.raises(params_mod.ParamError):
        params_mod.resolve(schema, {}, pack="airflow")
