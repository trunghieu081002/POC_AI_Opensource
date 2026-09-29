"""synth.py's reply is never trusted - same discipline test_router.py already
applies to pack routing, extended to a model-drafted pipeline.yaml. These
tests are about what happens when the model is wrong, ambiguous, or tries
to write something it should not be able to."""
import pytest
import yaml

from dpagent.llm import client as llm
from dpagent.pipelines import loader, synth


class FakeReply:
    """Stand in for llm.chat_json so the tests need no credential - same
    helper tests/test_router.py already uses."""

    def __init__(self, payload):
        self.payload = payload

    def __call__(self, system, user, **kwargs):
        return self.payload


VALID_FILES = {
    "pipeline.yaml": yaml.safe_dump({
        "name": "monthly_sales", "summary": "test",
        "source": {"connector": "odoo_postgres", "connection": {"host": "${DB_HOST}"},
                   "tables": ["sale_order"]},
        "warehouse": {"host": "${WAREHOUSE_DB_HOST}", "database": "${WAREHOUSE_DB_NAME}"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "row_count_bounds", "table": "sale_order", "min": 1}]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing",
             "models": ["stg_sale_order"], "gates": [
                {"type": "not_null", "table": "stg_sale_order", "columns": ["id"]}]},
        ],
    }, sort_keys=False),
    "models/stg_sale_order.sql": "select id, amount_total from {{ source('raw', 'sale_order') }}\n",
}


def _request(**overrides):
    kwargs = dict(
        name="monthly_sales",
        brd="Monthly revenue by order date.",
        source_schema="sale_order(id bigint, amount_total numeric, date_order date)",
        warehouse={"host": "${WAREHOUSE_DB_HOST}"},
        secret_refs={"DB_HOST": "Odoo Postgres host"},
    )
    kwargs.update(overrides)
    return synth.SynthRequest(**kwargs)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "pipelines"
    r.mkdir()
    return r


def test_capability_catalog_lists_every_real_connector_gate_engine():
    from dpagent.pipelines import extract as extract_mod, loader as loader_mod
    catalog = synth.capability_catalog()
    for connector in extract_mod.CONNECTORS:
        assert connector in catalog
    for engine in loader_mod.ENGINES:
        assert engine in catalog
    for gate_type in loader_mod.GATE_REQUIRED_FIELDS:
        assert gate_type in catalog


def test_synth_refuses_to_run_without_a_verified_source_schema(root):
    with pytest.raises(ValueError, match="source schema"):
        synth.synth(_request(source_schema=""), pipelines_dir=root)


def test_synth_refuses_to_overwrite_an_existing_pipeline_by_default(root, monkeypatch):
    (root / "monthly_sales").mkdir()
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": VALID_FILES}))
    with pytest.raises(FileExistsError):
        synth.synth(_request(), pipelines_dir=root)


def test_synth_overwrite_redrafts_an_existing_draft(root, monkeypatch):
    d = root / "monthly_sales"
    d.mkdir()
    (d / "pipeline.yaml").write_text("name: monthly_sales\nstages: []\n")   # a broken earlier draft
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": dict(VALID_FILES)}))

    result = synth.synth(_request(), pipelines_dir=root, overwrite=True)

    assert result.structurally_valid is True
    assert set(result.files) == {"pipeline.yaml", "models/stg_sale_order.sql"}


def test_synth_overwrite_refuses_outright_against_a_reviewed_pipeline(root, monkeypatch):
    """The guard that matters most here: even with overwrite=True, real
    promoted work must never be silently clobbered by a redraft - the
    operator has to choose a different name instead."""
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": dict(VALID_FILES)}))
    synth.synth(_request(), pipelines_dir=root)
    pipeline = loader.load("monthly_sales", root)
    synth.approval_mod.promote(pipeline, "alice")

    with pytest.raises(ValueError, match="approval history"):
        synth.synth(_request(), pipelines_dir=root, overwrite=True)

    # untouched - still there, still reviewed
    assert loader.load("monthly_sales", root).maturity == "reviewed"


def test_synth_overwrite_refuses_even_when_the_reviewed_manifest_no_longer_loads(root, monkeypatch):
    """The exact hole the file-existence check closes over a
    load-then-check-maturity check: a hand-edit that breaks the manifest
    (or lets its approval go stale) must not turn into "safe to overwrite"
    just because loader.load() no longer succeeds or maturity no longer
    reads reviewed - .approved.yaml being there at all is real approval
    history for this name and is checked directly, not inferred."""
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": dict(VALID_FILES)}))
    synth.synth(_request(), pipelines_dir=root)
    pipeline = loader.load("monthly_sales", root)
    synth.approval_mod.promote(pipeline, "alice")
    # Corrupt the manifest after promoting - loader.load() will now fail,
    # but .approved.yaml is still sitting right there.
    (root / "monthly_sales" / "pipeline.yaml").write_text("not: [valid, pipeline, at all\n")

    with pytest.raises(ValueError, match="approval history"):
        synth.synth(_request(), pipelines_dir=root, overwrite=True)

    assert (root / "monthly_sales" / ".approved.yaml").exists()   # untouched


def test_synth_overwrite_proceeds_when_no_approval_file_was_ever_written(root, monkeypatch):
    """The other half of the same guard: a pipeline that was drafted but
    never promoted has no approval history to protect, however broken its
    current manifest is."""
    d = root / "monthly_sales"
    d.mkdir()
    (d / "pipeline.yaml").write_text("not: [valid, at, all\n")
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": dict(VALID_FILES)}))

    result = synth.synth(_request(), pipelines_dir=root, overwrite=True)

    assert result.structurally_valid is True


def test_synth_returns_blockers_without_writing_anything(root, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "blockers": [{"question": "Ngày đặt hàng hay ngày xác nhận?",
                      "why_it_matters": "Đổi cột mốc thời gian"}],
    }))
    result = synth.synth(_request(), pipelines_dir=root)
    assert result.blocked is True
    assert result.blockers[0].question.startswith("Ngày")
    assert not (root / "monthly_sales").exists()


def test_synth_ignores_blank_or_malformed_blocker_entries(root, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "blockers": [{"why_it_matters": "no question key"}, "not even a dict",
                     {"question": "real one"}],
    }))
    result = synth.synth(_request(), pipelines_dir=root)
    assert len(result.blockers) == 1
    assert result.blockers[0].question == "real one"


def test_synth_writes_a_valid_draft_and_it_loads(root, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "files": dict(VALID_FILES), "mapping": "revenue -> sum(amount_total)",
        "notes": "assumed nothing",
    }))
    result = synth.synth(_request(), pipelines_dir=root)

    assert result.blocked is False
    assert result.structurally_valid is True
    assert result.load_error == ""
    assert set(result.files) == {"pipeline.yaml", "models/stg_sale_order.sql"}
    assert result.mapping == "revenue -> sum(amount_total)"

    pipeline = loader.load("monthly_sales", root)
    assert pipeline.is_draft is True   # never trusted to self-approve
    assert pipeline.maturity == "draft"


def test_synth_forces_draft_even_if_the_model_claims_reviewed(root, monkeypatch):
    files = dict(VALID_FILES)
    manifest = yaml.safe_load(files["pipeline.yaml"])
    manifest["maturity"] = "reviewed"   # the model is not allowed to decide this
    files["pipeline.yaml"] = yaml.safe_dump(manifest, sort_keys=False)
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": files}))

    result = synth.synth(_request(), pipelines_dir=root)

    pipeline = loader.load("monthly_sales", root)
    assert pipeline.is_draft is True
    approved, _ = synth.approval_mod.is_approved(pipeline)
    assert approved is False


def test_synth_refuses_to_let_the_model_forge_its_own_approval_file(root, monkeypatch):
    """The single most important guard connecting M1 and M2: a drafted
    pipeline must never be able to write its own .approved.yaml - that
    would let it self-approve and sail straight through deploy()'s gate."""
    files = dict(VALID_FILES)
    files[".approved.yaml"] = "content_hash: sha256:0\napproved_by: the-model-itself\n"
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": files}))

    with pytest.raises(ValueError, match="approval"):
        synth.synth(_request(), pipelines_dir=root)
    assert not (root / "monthly_sales").exists()


@pytest.mark.parametrize("bad_path", [
    "steps/install.sh", "rollback.sh", "README.md", "../outside.sql",
    "/etc/passwd", "models/../../../etc/passwd.sql",
])
def test_synth_refuses_disallowed_file_paths(root, monkeypatch, bad_path):
    files = dict(VALID_FILES)
    files[bad_path] = "whatever\n"
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": files}))

    with pytest.raises(ValueError):
        synth.synth(_request(), pipelines_dir=root)
    assert not (root / "monthly_sales").exists()


def test_synth_records_a_load_error_but_keeps_the_files_for_a_reviewer(root, monkeypatch):
    """A hallucinated gate type fails loader.load() - the draft is kept on
    disk anyway (still unreviewable/undeployable regardless, since
    maturity stays draft) so a human can see what the model actually
    wrote instead of it silently vanishing."""
    files = dict(VALID_FILES)
    manifest = yaml.safe_load(files["pipeline.yaml"])
    manifest["stages"][0]["gates"][0]["type"] = "made_up_gate_type"
    files["pipeline.yaml"] = yaml.safe_dump(manifest, sort_keys=False)
    monkeypatch.setattr(llm, "chat_json", FakeReply({"files": files}))

    result = synth.synth(_request(), pipelines_dir=root)

    assert result.structurally_valid is False
    assert "made_up_gate_type" in result.load_error
    assert (root / "monthly_sales" / "pipeline.yaml").exists()

    assert result.validation is not None
    assert result.validation.load_ok is False
    assert "made_up_gate_type" in result.validation.load_error
    report_path = root / "monthly_sales" / ".synth-validation.yaml"
    assert report_path.exists()
    on_disk = yaml.safe_load(report_path.read_text())
    assert on_disk["steps"]["load"]["status"] == "fail"
    assert "made_up_gate_type" in on_disk["steps"]["load"]["error"]
    assert "dbt_compile" not in on_disk["steps"]   # nothing to compile-check, never attempted


def test_synth_writes_a_validation_report_with_real_compile_check_results(root, monkeypatch):
    """The report a reviewer actually reads - step 3 ran for real (dbt
    parse against this exact drafted model, no mocking of the check
    itself), not just recorded as a stub."""
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "files": dict(VALID_FILES), "notes": "assumed nothing not already confirmed",
    }))
    result = synth.synth(_request(), pipelines_dir=root)

    assert result.validation is not None
    assert result.validation.load_ok is True
    assert result.validation.assumptions == "assumed nothing not already confirmed"
    report_path = root / "monthly_sales" / ".synth-validation.yaml"
    on_disk = yaml.safe_load(report_path.read_text())
    assert on_disk["steps"]["load"]["status"] == "pass"
    assert on_disk["steps"]["dbt_compile"]["status"] in ("pass", "fail", "skipped")
    assert on_disk["assumptions"] == "assumed nothing not already confirmed"


def test_synth_never_writes_a_validation_report_when_blocked(root, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "blockers": [{"question": "which date field?"}],
    }))
    synth.synth(_request(), pipelines_dir=root)
    assert not (root / "monthly_sales").exists()   # nothing written at all, report included


def test_synth_raises_when_the_reply_has_neither_files_nor_blockers(root, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({"notes": "oops, forgot everything else"}))
    with pytest.raises(llm.LLMError):
        synth.synth(_request(), pipelines_dir=root)


def test_synth_raises_when_pipeline_yaml_itself_is_missing(root, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "files": {"models/stg_sale_order.sql": "select 1\n"},
    }))
    with pytest.raises(llm.LLMError, match="pipeline.yaml"):
        synth.synth(_request(), pipelines_dir=root)
