"""A pipeline-owned dbt project (docs/hg-dbt-project.md): what the approval
hash covers, what the loader refuses, and that a selector selecting nothing
can never be reported as a successful transform.

Uses a COPY of the real `pipelines/hg_dbt_branch` (HG's own dbt files), so
the hash/refusal behaviour is checked against a real project's shape, not a
toy. The dbt-binary-dependent parts use a tiny fake `dbt` script; the real
`dbt ls`/`deps`/`parse`/`run` behaviour is verified against real dbt in
scripts/hg-dbt-branch-verify.sh.
"""
import contextlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from dpagent.pipelines import approval, dbtproject, loader, runtime, validate

REPO = Path(__file__).resolve().parents[1]
PROJ = "dwh_dbt"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "pipelines"
    shutil.copytree(REPO / "pipelines" / "hg_dbt_branch", r / "hg_dbt_branch",
                    ignore=shutil.ignore_patterns(".synth-validation.yaml", ".approved.yaml"))
    monkeypatch.setattr(loader, "PIPELINES_DIR", r)
    return r


def _load(root):
    return loader.load("hg_dbt_branch", root)


def _hash(root):
    return approval.content_hash(_load(root))


def _p(root, rel=""):
    return root / "hg_dbt_branch" / PROJ / rel


# ------------------------------------------------------------------ the hash covers the project

def test_hash_is_stable_and_covers_every_authored_kind_of_file(root):
    assert _hash(root) == _hash(root)
    paths = approval.hashed_paths(_load(root))
    for expected in (f"{PROJ}/dbt_project.yml", f"{PROJ}/packages.yml",
                     f"{PROJ}/package-lock.yml", f"{PROJ}/macros/generate_schema_name.sql",
                     f"{PROJ}/seeds/manual_excluded_partner_ids.csv",
                     f"{PROJ}/seeds/manual_excluded_partner_ids.yml",
                     f"{PROJ}/models/staging/_sources.yml",
                     f"{PROJ}/models/silver/dim/dim_artist.sql", "pipeline.yaml"):
        assert expected in paths, expected


@pytest.mark.parametrize("rel,how", [
    ("models/silver/dim/dim_artist.sql", "edit"),
    ("models/silver/dim_artist_active.sql", "edit"),
    ("seeds/manual_excluded_partner_ids.csv", "edit"),
    ("macros/generate_schema_name.sql", "edit"),
    ("dbt_project.yml", "edit"),
    ("packages.yml", "edit"),
    ("package-lock.yml", "edit"),
    ("models/staging/_sources.yml", "edit"),
    ("models/silver/_silver_models.yml", "edit"),
    ("models/silver/dim_artist_active.sql", "delete"),
    ("seeds/manual_excluded_partner_ids.csv", "delete"),
    ("macros/new_macro.sql", "add"),
    ("models/silver/new_model.sql", "add"),
    ("seeds/another_seed.csv", "add"),
    ("tests/assert_something.sql", "add"),
    ("analyses/a.sql", "add"),
    ("snapshots/s.sql", "add"),
    ("tests/logs/authored.sql", "add"),     # a dir NAMED like dbt output, deep in the tree, is authored
])
def test_changing_adding_or_deleting_project_content_changes_the_hash(root, rel, how):
    before = _hash(root)
    f = _p(root, rel)
    if how == "edit":
        tail = {".yml": "\n# changed\n", ".csv": "9\n"}.get(f.suffix, "\n-- changed\n")
        f.write_text(f.read_text() + tail)
    elif how == "delete":
        f.unlink()
    else:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("select 1\n")
    assert _hash(root) != before


def test_a_whitespace_only_edit_is_still_a_change(root):
    before = _hash(root)
    f = _p(root, "macros/generate_schema_name.sql")
    f.write_text(f.read_text() + " ")
    assert _hash(root) != before


@pytest.mark.parametrize("rel", [
    "target/manifest.json", "target/compiled/x.sql", "logs/dbt.log",
    "dbt_packages/dbt_utils/macros/x.sql", ".user.yml", "macros/__pycache__/x.pyc",
    ".DS_Store", "models/.DS_Store",
])
def test_generated_output_does_not_change_the_hash(root, rel):
    before = _hash(root)
    f = _p(root, rel)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("generated")
    assert _hash(root) == before
    assert not any(rel.startswith(x) for x in approval.hashed_paths(_load(root)))


def test_output_redirected_by_config_is_generated_too(root):
    cfg = _p(root, "dbt_project.yml")
    cfg.write_text(cfg.read_text() + '\ntarget-path: "build_out"\n')
    before = _hash(root)
    (_p(root, "build_out")).mkdir()
    (_p(root, "build_out/compiled.sql")).write_text("generated")
    assert _hash(root) == before


def test_editing_the_project_invalidates_an_existing_approval(root):
    p = _load(root)
    approval.write_approval(p, "reviewer")
    object.__setattr__(p, "maturity", "reviewed")
    assert approval.is_approved(p) == (True, "")
    f = _p(root, "macros/generate_schema_name.sql")
    f.write_text(f.read_text() + "\n{# sneaky #}\n")
    ok, why = approval.is_approved(_load_reviewed(root))
    assert ok is False and "changed since it was approved" in why


def _load_reviewed(root):
    import dataclasses
    return dataclasses.replace(_load(root), maturity="reviewed")


# ------------------------------------------------------------------ old pipelines unchanged

@pytest.mark.parametrize("name", ["demo", "quickstart", "quickstart_dbt"])
def test_existing_approved_pipelines_still_verify_against_their_committed_hash(name):
    """The pinned hashes in the repo's own .approved.yaml files were written
    before dbt_project existed - they must still match byte for byte."""
    p = loader.load(name, REPO / "pipelines")
    assert p.dbt_project is None
    assert approval.is_approved(p) == (True, "")


def test_a_pipeline_without_dbt_project_hashes_exactly_manifest_procedures_and_models():
    p = loader.load("quickstart_dbt", REPO / "pipelines")
    paths = approval.hashed_paths(p)
    assert paths[0] == "models/stg_orders.sql" or "pipeline.yaml" in paths
    assert not any(x.startswith("dwh_dbt") for x in paths)


def test_for_an_owning_pipeline_model_selectors_are_not_treated_as_files(root):
    # a stray models/<selector>.sql next to the manifest is never read by the run,
    # so it must not be hashed either
    (root / "hg_dbt_branch" / "models").mkdir()
    (root / "hg_dbt_branch" / "models" / "dim_artist.sql").write_text("select 1")
    assert "models/dim_artist.sql" not in approval.hashed_paths(_load(root))


# ------------------------------------------------------------------ refusals

def _expect_refused(root, match):
    with pytest.raises(loader.PipelineError, match=match):
        _load(root)


def test_a_symlinked_file_is_refused(root, tmp_path):
    outside = tmp_path / "secret.sql"
    outside.write_text("select 'outside'")
    os.symlink(outside, _p(root, "models/silver/linked.sql"))
    _expect_refused(root, "symlink")


def test_a_symlinked_directory_is_refused(root, tmp_path):
    outside = tmp_path / "outside_macros"
    outside.mkdir()
    (outside / "m.sql").write_text("{% macro m() %}1{% endmacro %}")
    os.symlink(outside, _p(root, "macros/ext"))
    _expect_refused(root, "symlink")


def test_a_symlink_hidden_inside_a_generated_looking_name_deep_in_the_tree_is_still_refused(root, tmp_path):
    _p(root, "tests").mkdir()
    os.symlink(tmp_path, _p(root, "tests/logs"))
    _expect_refused(root, "symlink")


def test_a_dbt_project_path_that_is_itself_a_symlink_is_refused(root, tmp_path):
    shutil.move(str(_p(root)), str(tmp_path / "real_proj"))
    os.symlink(tmp_path / "real_proj", _p(root))
    _expect_refused(root, "outside the pipeline")


@pytest.mark.parametrize("key,value", [
    ("model-paths", '["../shared_models"]'), ("macro-paths", '["/etc"]'),
    ("seed-paths", '["a/../../b"]'), ("test-paths", '["~/x"]'),
    ("target-path", '"/tmp/out"'), ("packages-install-path", '"../pkgs"'),
    ("log-path", '"../../logs"'),
])
def test_dbt_project_yml_paths_may_not_leave_the_project(root, key, value):
    cfg = _p(root, "dbt_project.yml")
    cfg.write_text(cfg.read_text() + f"\n{key}: {value}\n")
    _expect_refused(root, "relative path inside")


@pytest.mark.parametrize("key", ["macro-paths", "model-paths", "seed-paths"])
def test_output_may_not_be_redirected_onto_authored_content(root, key):
    cfg = _p(root, "dbt_project.yml")
    default_dir = {"macro-paths": "macros", "model-paths": "models", "seed-paths": "seeds"}[key]
    cfg.write_text(cfg.read_text() + f'\ntarget-path: "{default_dir}"\n')
    _expect_refused(root, "generated output")


def test_a_local_package_outside_the_project_is_refused(root, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "dbt_project.yml").write_text("name: pkg\n")
    pk = _p(root, "packages.yml")
    pk.write_text(pk.read_text() + "  - local: ../../../../pkg\n")
    _expect_refused(root, "resolves outside the project")


def test_an_absolute_local_package_is_refused(root):
    pk = _p(root, "packages.yml")
    pk.write_text(pk.read_text() + "  - local: /opt/shared_pkg\n")
    _expect_refused(root, "absolute path")


def test_a_local_package_inside_the_project_is_allowed_and_its_files_are_hashed(root):
    (_p(root, "vendored/mypkg/macros")).mkdir(parents=True)
    (_p(root, "vendored/mypkg/dbt_project.yml")).write_text("name: mypkg\n")
    (_p(root, "vendored/mypkg/macros/m.sql")).write_text("{% macro m() %}1{% endmacro %}")
    pk = _p(root, "packages.yml")
    pk.write_text(pk.read_text() + "  - local: vendored/mypkg\n")
    assert f"{PROJ}/vendored/mypkg/macros/m.sql" in approval.hashed_paths(_load(root))
    before = _hash(root)
    (_p(root, "vendored/mypkg/macros/m.sql")).write_text("{% macro m() %}2{% endmacro %}")
    assert _hash(root) != before


def test_remote_packages_without_a_lock_file_are_refused(root):
    _p(root, "package-lock.yml").unlink()
    _expect_refused(root, "no package-lock.yml")


def test_a_project_with_only_local_packages_needs_no_lock(root):
    _p(root, "package-lock.yml").unlink()
    (_p(root, "vendored/p")).mkdir(parents=True)
    (_p(root, "vendored/p/dbt_project.yml")).write_text("name: p\n")
    _p(root, "packages.yml").write_text("packages:\n  - local: vendored/p\n")
    _load(root)


def test_the_unmodified_real_project_loads(root):
    _load(root)


# ------------------------------------------------------------------ copy_project

def test_copy_project_copies_exactly_the_hashed_files(root, tmp_path):
    for rel in ("target/x.json", "logs/l.log", "dbt_packages/p/x.sql", ".user.yml"):
        _p(root, rel).parent.mkdir(parents=True, exist_ok=True)
        _p(root, rel).write_text("generated")
    dest = tmp_path / "copy"
    n = dbtproject.copy_project(_p(root), dest)
    copied = sorted(str(f.relative_to(dest)) for f in dest.rglob("*") if f.is_file())
    hashed = sorted(rel for rel, _ in dbtproject.project_files(_p(root)))
    assert copied == hashed and n == len(hashed)
    assert not (dest / "target").exists() and not (dest / "dbt_packages").exists()


# ------------------------------------------------------------------ selectors (fake dbt binary)

def _fake_dbt(tmp_path, selectable):
    """A stand-in `dbt` whose `ls --select X` prints X's models from the given
    table (and nothing, exit 0, for an unknown selector - the real behaviour)."""
    lines = "\n".join(f'    "{sel}") echo "{out}";;' for sel, out in selectable.items())
    script = tmp_path / "fakedbt"
    script.write_text(f"""#!/bin/sh
cmd="$1"; sel=""
while [ $# -gt 0 ]; do [ "$1" = "--select" ] && sel="$2"; shift; done
case "$cmd" in
  deps|parse|seed|run) exit 0;;
  ls) case "$sel" in
{lines}
    *) ;;
  esac; exit 0;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _stage(name, models):
    return loader.Stage(name=name, engine="dbt", models=models)


def test_list_selected_models_parses_names_and_treats_no_output_as_empty(tmp_path):
    dbt = _fake_dbt(tmp_path, {"silver": "dim_artist\ndim_artist_active"})
    assert dbtproject.list_selected_models(dbt, [], "silver") == ["dim_artist", "dim_artist_active"]
    assert dbtproject.list_selected_models(dbt, [], "nope") == []


def test_every_selector_must_select_something(tmp_path):
    dbt = _fake_dbt(tmp_path, {"a": "a", "b": "b"})
    assert dbtproject.check_selectors(dbt, [], [_stage("s", ["a", "b"])]) == []
    problems = dbtproject.check_selectors(dbt, [], [_stage("s", ["a", "typo"])])
    assert len(problems) == 1 and "'typo' selects 0 models" in problems[0] and "'s'" in problems[0]


def test_a_stage_whose_only_selector_selects_nothing_fails(tmp_path):
    dbt = _fake_dbt(tmp_path, {"a": "a"})
    problems = dbtproject.check_selectors(dbt, [], [_stage("gold", ["tag:nope"])])
    assert problems and "selects 0 models" in problems[0]


def test_a_dbt_ls_error_is_a_problem_not_an_empty_selection(tmp_path):
    failing = tmp_path / "faildbt"
    failing.write_text("#!/bin/sh\necho 'Compilation Error: bad' >&2\nexit 2\n")
    failing.chmod(0o755)
    problems = dbtproject.check_selectors(str(failing), [], [_stage("s", ["a"])])
    assert problems and "Compilation Error" in problems[0]


# ------------------------------------------------------------------ runtime: never "success" on 0 models

@pytest.fixture
def fake_runtime(root, tmp_path, monkeypatch):
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    for k in ("HG_POC_SOURCE_HOST", "HG_POC_SOURCE_NAME", "HG_POC_SOURCE_USER",
              "HG_POC_SOURCE_PASSWORD", "HG_POC_WH_USER", "HG_POC_WH_PASSWORD",
              "HG_POC_BRONZE_ENDPOINT", "HG_POC_BRONZE_ACCESS_KEY", "HG_POC_BRONZE_SECRET_KEY"):
        monkeypatch.setenv(k, "x")
    calls = []

    @contextlib.contextmanager
    def fake_profiles(pipeline):
        yield str(tmp_path / "profiles")

    def fake_run(cmd, *, pipeline, kind, what, **kw):
        calls.append(cmd[1])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(runtime, "_dbt_profiles_dir", fake_profiles)
    monkeypatch.setattr(runtime, "_run", fake_run)
    monkeypatch.setattr(runtime, "_dbt_profile_name", lambda: "default")
    return calls, monkeypatch, tmp_path


def test_a_selector_that_selects_nothing_fails_the_transform_and_never_runs_dbt(fake_runtime):
    calls, mp, tmp_path = fake_runtime
    mp.setattr(runtime, "_dbt_bin", lambda: _fake_dbt(tmp_path, {"dim_artist": "dim_artist"}))
    p = loader.load("hg_dbt_branch")
    import dataclasses
    stage = dataclasses.replace(p.stages[1], models=["dim_artist", "dim_artsit"])   # typo
    proc = runtime._run_own_dbt_project(p, stage)
    assert proc.returncode != 0
    assert "selector 'dim_artsit' selects 0 models" in proc.stderr
    assert "run" not in calls and "seed" not in calls, calls


def test_run_transform_raises_and_never_reports_done_for_an_empty_selector(fake_runtime):
    calls, mp, tmp_path = fake_runtime
    mp.setattr(runtime, "_dbt_bin", lambda: _fake_dbt(tmp_path, {}))     # nothing selects anything
    from dpagent.engine import state
    with pytest.raises(runtime.GateFailed, match="selects 0 models"):
        runtime.run_transform(pipeline_name="hg_dbt_branch", stage="gold")
    kinds = [r["kind"] for r in state.conn().execute("select kind from events").fetchall()]
    assert "transform.failed" in kinds and "transform.done" not in kinds


def test_when_every_selector_selects_something_dbt_run_does_run(fake_runtime):
    calls, mp, tmp_path = fake_runtime
    mp.setattr(runtime, "_dbt_bin",
               lambda: _fake_dbt(tmp_path, {"dim_artist": "dim_artist",
                                            "dim_artist_active": "dim_artist_active"}))
    runtime.run_transform(pipeline_name="hg_dbt_branch", stage="silver")
    assert calls == ["deps", "seed", "run"], calls


def test_a_lock_that_deps_rewrites_fails_the_run(fake_runtime, root):
    calls, mp, tmp_path = fake_runtime
    mp.setattr(runtime, "_dbt_bin", lambda: _fake_dbt(tmp_path, {"dim_artist": "dim_artist"}))

    def deps_rewrites_lock(cmd, *, pipeline, kind, what, **kw):
        calls.append(cmd[1])
        if cmd[1] == "deps":
            proj = Path(cmd[cmd.index("--project-dir") + 1])
            (proj / "package-lock.yml").write_text("packages: []\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    mp.setattr(runtime, "_run", deps_rewrites_lock)
    p = loader.load("hg_dbt_branch")
    proc = runtime._run_own_dbt_project(p, p.stages[1])
    assert proc.returncode != 0 and "changed package-lock.yml" in proc.stderr
    assert "run" not in calls


# ------------------------------------------------------------------ validate step 3

def test_validate_step_3_fails_on_an_empty_selector_and_passes_otherwise(root, tmp_path, monkeypatch):
    p = _load(root)
    monkeypatch.setattr(validate, "_dbt_bin", lambda: _fake_dbt(
        tmp_path, {"dim_artist": "x", "dim_artist_active": "x", "mart_artist_summary": "x"}))
    assert validate.check_dbt_models(p).status == "pass"
    monkeypatch.setattr(validate, "_dbt_bin", lambda: _fake_dbt(tmp_path, {"dim_artist": "x"}))
    r = validate.check_dbt_models(p)
    assert r.status == "fail" and "selects 0 models" in r.detail
