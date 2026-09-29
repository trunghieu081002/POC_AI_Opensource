"""approval.py pins *which exact content* a `dpagent pipeline promote` was
run against - `maturity: reviewed` alone cannot tell a stale approval from
a fresh one (docs/layer2.md, "Authoring pipelines with a model"). These
tests lock in the hash's stability across a promote and its sensitivity to
every file `deploy()` actually reads for real."""
import yaml

from dpagent.pipelines import approval, loader


def _pipeline(root, *, with_dbt=True, with_procedure=True):
    stages = [{"name": "landing", "gates": [
        {"type": "row_count_bounds", "table": "t", "min": 1}]}]
    if with_dbt:
        stages.append({"name": "raw", "engine": "dbt", "depends_on": "landing",
                       "models": ["stg_a"], "gates": [
                           {"type": "not_null", "table": "stg_a", "columns": ["id"]}]})
    if with_procedure:
        stages.append({"name": "curated", "engine": "procedure",
                       "depends_on": stages[-1]["name"],
                       "procedure": "procedures/build.sql", "gates": [
                           {"type": "not_null", "table": "fct", "columns": ["id"]}]})
    data = {
        "name": "demo", "summary": "test",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}, "tables": ["t"]},
        "warehouse": {"host": "localhost", "database": "warehouse"},
        "stages": stages,
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    if with_procedure:
        (d / "procedures").mkdir()
        (d / "procedures" / "build.sql").write_text("-- v1\n")
    if with_dbt:
        (d / "models").mkdir()
        (d / "models" / "stg_a.sql").write_text("select 1 as id\n")
    return loader.load("demo", root)


def test_is_approved_is_false_for_a_fresh_draft(tmp_path):
    pipeline = _pipeline(tmp_path / "pipelines")
    approved, reason = approval.is_approved(pipeline)
    assert approved is False
    assert "draft" in reason


def test_promote_makes_is_approved_true(tmp_path):
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    approval.promote(pipeline, "alice")
    reloaded = loader.load("demo", root)
    approved, reason = approval.is_approved(reloaded)
    assert approved is True
    assert reason == ""
    assert reloaded.maturity == "reviewed"


def test_promote_writes_a_git_trackable_approval_file(tmp_path):
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    approval.promote(pipeline, "alice")
    stored = approval.read_approval(loader.load("demo", root))
    assert stored.approved_by == "alice"
    assert stored.content_hash.startswith("sha256:")
    assert stored.approved_at   # non-empty ISO timestamp


def test_reviewed_maturity_alone_without_an_approval_file_is_not_approved(tmp_path):
    """An operator hand-editing `maturity: reviewed` into the YAML (or a
    model drafting one that already claims to be reviewed) must not be
    trusted without the recorded hash to back it up."""
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    text = pipeline.path("pipeline.yaml").read_text() + "maturity: reviewed\n"
    pipeline.path("pipeline.yaml").write_text(text)
    reloaded = loader.load("demo", root)
    approved, reason = approval.is_approved(reloaded)
    assert approved is False
    assert "no .approved.yaml" in reason


def test_editing_the_manifest_after_promote_invalidates_it(tmp_path):
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    approval.promote(pipeline, "alice")

    manifest = pipeline.path("pipeline.yaml")
    manifest.write_text(manifest.read_text().replace("localhost", "some-other-host"))

    reloaded = loader.load("demo", root)
    approved, reason = approval.is_approved(reloaded)
    assert approved is False
    assert "changed since it was approved" in reason


def test_editing_a_referenced_procedure_after_promote_invalidates_it(tmp_path):
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    approval.promote(pipeline, "alice")

    proc = pipeline.path("procedures/build.sql")
    proc.write_text(proc.read_text() + "-- v2\n")

    reloaded = loader.load("demo", root)
    approved, _ = approval.is_approved(reloaded)
    assert approved is False


def test_editing_a_referenced_dbt_model_after_promote_invalidates_it(tmp_path):
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    approval.promote(pipeline, "alice")

    model = pipeline.path("models/stg_a.sql")
    model.write_text(model.read_text() + "-- v2\n")

    reloaded = loader.load("demo", root)
    approved, _ = approval.is_approved(reloaded)
    assert approved is False


def test_editing_an_unrelated_file_under_the_pipeline_root_does_not_invalidate_it(tmp_path):
    """content_hash() only covers what deploy() actually reads - a stray
    scratch file elsewhere under the pipeline's own directory (a README, a
    build/ leftover) is not part of what was reviewed and must not make an
    otherwise-untouched approval look stale."""
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    approval.promote(pipeline, "alice")

    (pipeline.root / "NOTES.md").write_text("unrelated scratch notes\n")

    reloaded = loader.load("demo", root)
    approved, _ = approval.is_approved(reloaded)
    assert approved is True


def test_flipping_maturity_back_to_draft_by_hand_does_not_look_like_a_content_change(tmp_path):
    """maturity itself is excluded from the hash - an operator demoting a
    pipeline back to draft on purpose gets exactly that ("draft", a plain
    refusal), not a confusing "content changed" message."""
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    approval.promote(pipeline, "alice")

    manifest = pipeline.path("pipeline.yaml")
    manifest.write_text(manifest.read_text().replace("maturity: reviewed", "maturity: draft"))

    reloaded = loader.load("demo", root)
    approved, reason = approval.is_approved(reloaded)
    assert approved is False
    assert "draft" in reason and "changed since" not in reason


def test_promote_is_idempotent_when_content_has_not_changed(tmp_path):
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    first = approval.promote(pipeline, "alice")
    reloaded = loader.load("demo", root)
    second = approval.promote(reloaded, "bob")
    assert first.content_hash == second.content_hash


def test_content_hash_only_covers_files_deploy_actually_reads(tmp_path):
    """A landing-only pipeline (no dbt, no procedure) hashes cleanly with
    just its own manifest - hashed_paths() must not reach for files a
    pipeline this shape never has."""
    pipeline = _pipeline(tmp_path / "pipelines", with_dbt=False, with_procedure=False)
    assert approval.hashed_paths(pipeline) == ["pipeline.yaml"]


def test_hashed_paths_lists_every_referenced_procedure_and_model(tmp_path):
    pipeline = _pipeline(tmp_path / "pipelines")
    assert approval.hashed_paths(pipeline) == [
        "models/stg_a.sql", "pipeline.yaml", "procedures/build.sql",
    ]
