"""A rollback must leave nothing for the next install to mistake for
"already done" - that's the whole point of clear_step_runs()."""
from dpagent.engine import state


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "dpagent-test.db")
    state.close()
    return state.conn()


def test_clear_step_runs_forgets_a_pack_checkpoint(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    run_id = state.start_run("install", "postgres")
    row_id = state.start_step(run_id, "postgres", "initdb", "steps/30-initdb.sh",
                              params_hash_="abc123")
    state.finish_step(row_id, "ok")

    assert state.completed_steps("postgres", "abc123") == {"initdb"}

    state.clear_step_runs("postgres")

    assert state.completed_steps("postgres", "abc123") == set()


def test_clear_step_runs_only_touches_the_named_pack(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    pg_run = state.start_run("install", "postgres")
    pg_row = state.start_step(pg_run, "postgres", "initdb", "steps/30-initdb.sh",
                              params_hash_="abc123")
    state.finish_step(pg_row, "ok")

    dbt_run = state.start_run("install", "dbt")
    dbt_row = state.start_step(dbt_run, "dbt", "venv", "steps/10-venv.sh",
                               params_hash_="def456")
    state.finish_step(dbt_row, "ok")

    state.clear_step_runs("postgres")

    assert state.completed_steps("postgres", "abc123") == set()
    assert state.completed_steps("dbt", "def456") == {"venv"}


def test_rollback_then_reinstall_does_not_skip_steps(tmp_path, monkeypatch):
    """The actual regression: without clear_step_runs, a rollback followed by
    a same-params install would see the old checkpoint and skip every step,
    then fail verify against a system rollback just tore down."""
    _fresh_db(tmp_path, monkeypatch)
    PHASH = "same-params-hash"

    run_id = state.start_run("install", "postgres")
    for step_id in ("repo", "install", "initdb", "config", "databases"):
        row = state.start_step(run_id, "postgres", step_id, f"steps/{step_id}.sh",
                               params_hash_=PHASH)
        state.finish_step(row, "ok")
    assert len(state.completed_steps("postgres", PHASH)) == 5

    # rollback_cmd calls this after a successful rollback script
    state.clear_step_runs("postgres")

    # a fresh install with the exact same params must see nothing done
    assert state.completed_steps("postgres", PHASH) == set()
