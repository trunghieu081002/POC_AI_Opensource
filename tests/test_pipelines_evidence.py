"""A2: `promote()` requires sealed validation evidence bound to exactly this
content, produced by the controlled validation path, consistent with the
journal. Reading a `pass` out of a YAML file is not evidence, so the central
cases here are the ones where the YAML says pass and promote must still refuse.

Reports are built with the real `FixtureRunReport` + `fixture_report_dict`
(the producer), sealed with the real `evidence.seal`, and the journal is
populated with real `state` rows - only the Airflow run itself is not
performed (that is `scripts/m25-acceptance-ci.sh`, docs/promote-evidence.md).
"""
import json
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from approval_helpers import approve_legacy
from dpagent.cli.pipeline import pipeline_group
from dpagent.engine import state
from dpagent.pipelines import approval, evidence, fixture, loader, validate
from dpagent.pipelines.fixture import ComparisonResult, FixtureRunReport, SourceDownProof

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state" / "dpagent.db")
    state.close()
    yield
    state.close()


def _make_pipeline(root: Path, name: str = "demo"):
    data = {
        "name": name, "summary": "test",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}, "tables": ["t"]},
        "warehouse": {"host": "localhost", "database": "warehouse"},
        "stages": [
            {"name": "landing", "gates": [{"type": "row_count_bounds", "table": "t", "min": 1}]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing", "models": ["stg_a"],
             "gates": [{"type": "not_null", "table": "stg_a", "columns": ["id"]}]},
            {"name": "curated", "engine": "procedure", "depends_on": "raw",
             "procedure": "procedures/build.sql",
             "gates": [{"type": "not_null", "table": "fct", "columns": ["id"]}]},
        ],
    }
    d = root / name
    (d / "procedures").mkdir(parents=True)
    (d / "models").mkdir()
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (d / "procedures" / "build.sql").write_text("-- v1\n")
    (d / "models" / "stg_a.sql").write_text("select 1 as id\n")
    (d / "fixture.yaml").write_text("tables: []\n")
    (d / "expected.yaml").write_text("table: fct\nrows: [{id: 1}]\n")
    return loader.load(name, root)


@pytest.fixture
def pipeline(tmp_path):
    return _make_pipeline(tmp_path / "pipelines")


def _paths(pipeline):
    return pipeline.root / "fixture.yaml", pipeline.root / "expected.yaml"


def _journal_run(pipeline, clone, *, status="ok", gate_status="passed", gated=True):
    rid = state.start_run("data", clone)
    if gated:
        for stage in pipeline.stages:
            srow = state.start_stage(rid, clone, stage.name)
            state.finish_stage(srow, "passed", 1)
            for gate in stage.gates:
                state.record_gate(srow, gate.type, gate_status, 1, 0, "")
    state.finish_run(rid, status)
    return rid


def _good_report(pipeline, clone="demo__validate_abc", **journal_kw):
    run_ids = [_journal_run(pipeline, clone, **journal_kw) for _ in range(2)]
    return FixtureRunReport(
        clone_name=clone, seeded=True, deployed=True, run_ids=run_ids,
        run1_status="ok", run2_status="ok",
        comparison_after_run1=ComparisonResult(True), comparison_after_run2=ComparisonResult(True),
        cleanup_attempted=True, cleanup_ok=True, cleanup_detail="clean",
        source_database_dropped=True, source_role_dropped=True,
        warehouse_database_dropped=True, warehouse_role_dropped=True,
        source_db_created=True, warehouse_db_created=True,
        versions={"dpagent": "0.4.0", "postgres": "PostgreSQL 15", "dbt": "1.8.10", "report": 2},
    )


def validated(pipeline, *, report_tweak=None, section_tweak=None, step3_tweak=None,
              fixture_hash=None, expected_hash=None, seal=True, **journal_kw):
    """Run the producer side for real (minus Airflow) and return the evidence id."""
    report = _good_report(pipeline, **journal_kw)
    if report_tweak:
        report_tweak(report)
    fx_path, ex_path = _paths(pipeline)
    section = fixture.fixture_report_dict(
        report, pipeline_hash=approval.content_hash(pipeline),
        fixture_hash=fixture_hash or fixture.hash_file(fx_path),
        expected_hash=expected_hash or fixture.hash_file(ex_path))
    step3 = {"dbt": {"status": "pass", "detail": ""}, "procedures": {"status": "pass", "detail": ""},
             "dbt_dependencies": {"status": "pass", "detail": ""}, "load_ok": True}
    if step3_tweak:
        step3_tweak(step3)
    if section_tweak:
        section_tweak(section)
    return evidence.seal(pipeline, section, step3=step3) if seal else None


def promote(pipeline, **kw):
    fx_path, ex_path = _paths(pipeline)
    return approval.promote(pipeline, "alice", fixture_path=fx_path, expected_path=ex_path, **kw)


def untouched(pipeline):
    """Snapshot of what a refusal must not change."""
    manifest = pipeline.path("pipeline.yaml").read_bytes()
    app = approval.approval_path(pipeline)
    return manifest, (app.read_bytes() if app.exists() else None)


def assert_refused(pipeline, *needles, **kw):
    before = untouched(pipeline)
    with pytest.raises(evidence.EvidenceRefused) as exc:
        promote(pipeline, **kw)
    text = " | ".join(exc.value.reasons)
    for needle in needles:
        assert needle in text, text
    assert untouched(pipeline) == before, "a refusal changed approval/maturity"
    return exc.value


# ------------------------------------------------------------------ accepted

def test_valid_sealed_evidence_promotes_and_links_the_evidence(pipeline):
    evidence_id = validated(pipeline)
    approved = promote(pipeline)
    reloaded = loader.load("demo", pipeline.root.parent)
    assert reloaded.maturity == "reviewed"
    assert approval.is_approved(reloaded) == (True, "")
    stored = approval.read_approval(reloaded)
    assert stored.evidence["id"] == evidence_id == approved.evidence["id"]
    assert stored.evidence["sealed_sha256"].startswith("sha256:")
    assert stored.evidence["pipeline_hash"] == approval.content_hash(reloaded)
    assert stored.evidence["fixture"]["hash"] == fixture.hash_file(_paths(pipeline)[0])
    assert stored.evidence["expected"]["hash"] == fixture.hash_file(_paths(pipeline)[1])
    assert len(stored.evidence["run_ids"]) == 2
    assert approval.verification(reloaded)[0] == "verified"


def test_a_specific_evidence_id_can_be_named(pipeline):
    first = validated(pipeline)
    validated(pipeline, report_tweak=lambda r: setattr(r, "run2_status", "failed"))
    assert_refused(pipeline, "run 2 status")                 # newest is the failing one
    promote(pipeline, evidence_id=first)                     # naming the good one works
    assert approval.verification(loader.load("demo", pipeline.root.parent))[0] == "verified"


# ------------------------------------------------------------------ old approvals

def test_a_pre_evidence_approval_still_deploys_but_is_never_called_verified(pipeline):
    approve_legacy(pipeline, "bob")
    reloaded = loader.load("demo", pipeline.root.parent)
    assert approval.is_approved(reloaded) == (True, "")          # not revoked behind anyone's back
    label, why = approval.verification(reloaded)
    assert label == "unverified" and "no validation evidence" in why
    assert approval.read_approval(reloaded).evidence is None


def test_upgrading_an_old_approval_requires_real_evidence(pipeline):
    approve_legacy(pipeline, "bob")
    old = untouched(loader.load("demo", pipeline.root.parent))
    with pytest.raises(evidence.EvidenceRefused):
        promote(loader.load("demo", pipeline.root.parent))
    assert untouched(loader.load("demo", pipeline.root.parent)) == old
    validated(pipeline)
    promote(loader.load("demo", pipeline.root.parent))
    assert approval.verification(loader.load("demo", pipeline.root.parent))[0] == "verified"


def test_state_labels(pipeline):
    assert approval.verification(pipeline)[0] == "none"
    validated(pipeline)
    promote(pipeline)
    reloaded = loader.load("demo", pipeline.root.parent)
    assert approval.verification(reloaded)[0] == "verified"
    (reloaded.root / "procedures" / "build.sql").write_text("-- v2\n")
    assert approval.verification(loader.load("demo", pipeline.root.parent))[0] == "stale"


# ------------------------------------------------------------------ not evidence at all

def test_no_evidence_at_all(pipeline):
    assert_refused(pipeline, "no sealed evidence")


def test_a_hand_written_report_that_says_pass_is_not_evidence(pipeline):
    """The YAML a human (or a model) can type: every field right, hashes
    right, overall pass - and no row in the journal."""
    fx_path, ex_path = _paths(pipeline)
    report = _good_report(pipeline)
    section = fixture.fixture_report_dict(
        report, pipeline_hash=approval.content_hash(pipeline),
        fixture_hash=fixture.hash_file(fx_path), expected_hash=fixture.hash_file(ex_path))
    assert section["overall"] == "pass"
    pipeline.path(validate.VALIDATION_REPORT_FILENAME).write_text(
        yaml.safe_dump({"content_hash": approval.content_hash(pipeline), "fixture": section}))
    assert_refused(pipeline, "no sealed evidence")


def test_altering_the_stored_record_breaks_its_mac(pipeline):
    evidence_id = validated(pipeline, report_tweak=lambda r: setattr(r, "run2_status", "failed"))
    row = state.get_evidence(evidence_id)
    doctored = json.loads(row["payload_json"])
    doctored["fixture"]["run_status"]["run_2"] = "ok"
    doctored["fixture"]["overall"] = "pass"
    state.conn().execute("UPDATE validation_evidence SET payload_json=? WHERE id=?",
                         (json.dumps(doctored, sort_keys=True, separators=(",", ":")), evidence_id))
    state.conn().commit()
    assert_refused(pipeline, "fails its MAC check")


def test_a_record_inserted_without_the_key_is_refused(pipeline):
    validated(pipeline)                                        # creates the key
    payload = json.loads(state.latest_evidence("demo")["payload_json"])
    state.record_evidence("ev-forged", "demo", evidence._canonical(payload), "0" * 64)
    assert_refused(pipeline, "fails its MAC check")


def test_missing_key_means_nothing_was_sealed_here(pipeline):
    evidence_id = validated(pipeline)
    evidence._key_path().unlink()
    assert_refused(pipeline, "no seal key", evidence_id=evidence_id)


def test_the_key_is_created_private_and_a_leaky_key_is_not_trusted(pipeline):
    validated(pipeline)
    key = evidence._key_path()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    key.chmod(0o644)
    assert_refused(pipeline, "readable by group/other")


def test_evidence_of_another_pipeline_is_refused(tmp_path, pipeline):
    other = _make_pipeline(tmp_path / "other_root", "demo2")
    evidence_id = validated(pipeline)
    before = untouched(other)
    with pytest.raises(evidence.EvidenceRefused, match="is for pipeline 'demo', not 'demo2'"):
        approval.promote(other, "a", fixture_path=other.root / "fixture.yaml",
                         expected_path=other.root / "expected.yaml", evidence_id=evidence_id)
    assert untouched(other) == before


def test_unknown_report_version_is_refused(pipeline):
    validated(pipeline, section_tweak=lambda s: s.update(report_version=3))
    assert_refused(pipeline, "report_version 3 is not supported")


def test_old_report_version_is_refused(pipeline):
    validated(pipeline, section_tweak=lambda s: s.update(report_version=1))
    assert_refused(pipeline, "report_version 1 is not supported")


# ------------------------------------------------------------------ changed since validation

@pytest.mark.parametrize("rel,suffix", [
    ("pipeline.yaml", "# touched\n"),
    ("procedures/build.sql", "-- v2\n"),
    ("models/stg_a.sql", "-- v2\n"),
])
def test_changing_hashed_content_after_validation_is_refused(pipeline, rel, suffix):
    validated(pipeline)
    target = pipeline.root / rel
    target.write_text(target.read_text() + suffix)
    assert_refused(loader.load("demo", pipeline.root.parent), "the pipeline changed since it was validated")


def test_changing_the_fixture_after_validation_is_refused(pipeline):
    validated(pipeline)
    fx_path, _ = _paths(pipeline)
    fx_path.write_text(fx_path.read_text() + "# edited\n")
    assert_refused(pipeline, "fixture file is not the one that was validated")


def test_changing_the_expected_after_validation_is_refused(pipeline):
    validated(pipeline)
    _, ex_path = _paths(pipeline)
    ex_path.write_text("table: fct\nrows: [{id: 2}]\n")
    assert_refused(pipeline, "expected file is not the one that was validated")


def test_a_different_but_equal_looking_fixture_file_is_refused(pipeline, tmp_path):
    validated(pipeline)
    other = tmp_path / "other_fixture.yaml"
    other.write_text("tables: []\n# same meaning, other bytes\n")
    before = untouched(pipeline)
    with pytest.raises(evidence.EvidenceRefused, match="fixture file is not the one"):
        approval.promote(pipeline, "a", fixture_path=other, expected_path=_paths(pipeline)[1])
    assert untouched(pipeline) == before


def test_missing_fixture_or_expected_file(pipeline, tmp_path):
    validated(pipeline)
    with pytest.raises(evidence.EvidenceRefused, match="does not exist"):
        approval.promote(pipeline, "a", fixture_path=tmp_path / "nope.yaml",
                         expected_path=_paths(pipeline)[1])


def test_the_promote_signature_has_no_way_to_skip_evidence(pipeline):
    with pytest.raises(TypeError):
        approval.promote(pipeline, "alice")                    # type: ignore[call-arg]


# ------------------------------------------------------------------ the validation did not succeed

@pytest.mark.parametrize("tweak,needle", [
    (lambda r: setattr(r, "run1_status", "failed"), "run 1 status is 'failed'"),
    (lambda r: setattr(r, "run2_status", "timeout"), "run 2 timed out"),
    (lambda r: setattr(r, "comparison_after_run1", ComparisonResult(False, "x")),
     "run 1 comparison"),
    (lambda r: setattr(r, "comparison_after_run2", ComparisonResult(False, "x")),
     "run 2 comparison"),
    (lambda r: setattr(r, "unavailable_reason", "no sudo"), "could not complete"),
    (lambda r: setattr(r, "deploy_error", "boom"), "deploy failed"),
    (lambda r: setattr(r, "cleanup_ok", False), "cleanup pipeline_artifacts"),
    (lambda r: setattr(r, "warehouse_database_dropped", False), "cleanup warehouse_database"),
    (lambda r: setattr(r, "source_role_dropped", False), "cleanup source_role"),
    (lambda r: setattr(r, "run_ids", r.run_ids[:1]), "exactly 2 run ids"),
])
def test_unsuccessful_validation_is_refused(pipeline, tweak, needle):
    validated(pipeline, report_tweak=tweak)
    assert_refused(pipeline, needle)


def test_all_reasons_are_reported_not_just_the_first(pipeline):
    def many(r):
        r.run2_status = "timeout"
        r.cleanup_ok = False
        r.comparison_after_run2 = ComparisonResult(False)
    validated(pipeline, report_tweak=many)
    err = assert_refused(pipeline)
    assert len(err.reasons) >= 4


def test_a_step3_failure_is_refused(pipeline):
    validated(pipeline, step3_tweak=lambda s: s.update(
        dbt={"status": "fail", "detail": "does not compile"}))
    assert_refused(pipeline, "step 3 dbt: fail")


def test_step3_skipped_for_what_the_pipeline_does_not_have_is_accepted_for_hg(hg):
    """(covered by the bronze/owned-project promote test above: procedures and
    dbt_dependencies are 'skipped' there, as the real step 3 reports them)"""
    _hg_validated(hg)
    _hg_promote(hg)


def test_step3_skipped_where_it_applies_is_not_a_verification(pipeline):
    """dbt not installed / no sudo on the validating host: the check did not run."""
    validated(pipeline, step3_tweak=lambda s: s.update(
        dbt={"status": "skipped", "detail": "dbt is not installed"},
        procedures={"status": "skipped", "detail": "no passwordless sudo"}))
    assert_refused(pipeline, "step 3 dbt: skipped", "step 3 procedures: skipped")


def test_a_failed_negative_validation_cannot_be_promoted(pipeline):
    """The acceptance matrix's deliberately-wrong-expected case yields a sealed
    record too (it is audit history) - and it must never promote."""
    validated(pipeline, report_tweak=lambda r: setattr(
        r, "comparison_after_run1", ComparisonResult(False, "999 != 350")))
    assert_refused(pipeline, "comparison")


# ------------------------------------------------------------------ the journal disagrees with the report

def test_run_missing_from_the_journal(pipeline):
    def forget(section):
        section["run_ids"] = [9001, 9002]
    validated(pipeline, section_tweak=forget)
    assert_refused(pipeline, "not in this host's journal")


def test_run_that_the_journal_says_failed(pipeline):
    validated(pipeline, status="failed")
    assert_refused(pipeline, "finished 'failed' in the journal")


def test_gate_that_the_journal_says_failed(pipeline):
    validated(pipeline, gate_status="failed")
    assert_refused(pipeline, "gate(s) not passed")


def test_report_claiming_different_gates_than_the_journal(pipeline):
    def lie(section):
        section["gates"]["run_1"]["landing"][0]["status"] = "passed"
        section["gates"]["run_1"]["landing"][0]["rows_checked"] = 999
    validated(pipeline, section_tweak=lie)
    assert_refused(pipeline, "gate verdicts in the report differ from the journal")


def test_pipeline_with_gates_but_no_gate_rows_in_the_journal(pipeline):
    validated(pipeline, gated=False)
    assert_refused(pipeline, "has no gate verdict in the journal")


def test_run_that_is_not_a_run_of_the_validation_clone(pipeline):
    def retarget(section):
        section["clone_name"] = "some_other_pipeline"
    validated(pipeline, section_tweak=retarget)
    assert_refused(pipeline, "is not a real data run of")


# ------------------------------------------------------------------ rollback on partial failure

def test_approval_is_rolled_back_if_the_label_cannot_be_written(pipeline, monkeypatch):
    validated(pipeline)
    before = untouched(pipeline)

    def boom(_):
        raise OSError("disk full")
    monkeypatch.setattr(approval, "set_maturity_reviewed", boom)
    with pytest.raises(OSError):
        promote(pipeline)
    assert untouched(pipeline) == before
    assert not approval.approval_path(pipeline).exists()


# ------------------------------------------------------------------ bronze + owned dbt project

BRONZE_ENV = {"HG_POC_BRONZE_ENDPOINT": "http://s3.example:8333", "HG_POC_BRONZE_BUCKET": "hg-bronze",
              "HG_POC_BRONZE_ACCESS_KEY": "AK", "HG_POC_BRONZE_SECRET_KEY": "SK"}


@pytest.fixture
def hg(tmp_path, monkeypatch):
    root = tmp_path / "hgroot"
    shutil.copytree(REPO / "pipelines" / "hg_dbt_branch", root / "hg_dbt_branch",
                    ignore=shutil.ignore_patterns(".synth-validation.yaml", ".approved.yaml"))
    for k, v in BRONZE_ENV.items():
        monkeypatch.setenv(k, v)
    return loader.load("hg_dbt_branch", root)


def _hg_validated(hg, *, source_down=True, s3_remaining=0, batches=3, batch_status="loaded",
                  report_tweak=None):
    def tweak(report):
        report.bronze_staging = True
        report.dbt_project = True
        report.bronze_namespace = "dpagent-validate/abc"
        report.bronze_batches = [{"batch_id": f"b{i}", "table": "res_partner", "status": batch_status,
                                  "objects": 1, "rows": 6, "manifest_sha256": "x"}
                                 for i in range(batches)]
        report.source_down = SourceDownProof(
            batch_id="b3" if source_down else "", source_dropped=source_down,
            source_unreachable=source_down, loaded=source_down,
            landing_matches_fixture=source_down)
        report.s3_found, report.s3_remaining = 6, s3_remaining
        if report_tweak:
            report_tweak(report)

    fx = hg.root / "fixture.yaml"
    ex = hg.root / "expected.yaml"
    report = _good_report(hg, clone="hg_dbt_branch__validate_abc")
    tweak(report)
    section = fixture.fixture_report_dict(
        report, pipeline_hash=approval.content_hash(hg),
        fixture_hash=fixture.hash_file(fx), expected_hash=fixture.hash_file(ex))
    # exactly what the real step 3 reports for this pipeline (seen on the clean host)
    step3 = {"dbt": {"status": "pass", "detail": "own dbt project parsed clean"},
             "procedures": {"status": "skipped", "detail": "no procedure-engine stage in this pipeline"},
             "dbt_dependencies": {"status": "skipped",
                                  "detail": "models live in the pipeline's own dbt project"},
             "load_ok": True}
    return evidence.seal(hg, section, step3=step3)


def _hg_promote(hg, **kw):
    return approval.promote(hg, "alice", fixture_path=hg.root / "fixture.yaml",
                            expected_path=hg.root / "expected.yaml", **kw)


def _hg_refused(hg, *needles):
    before = untouched(hg)
    with pytest.raises(evidence.EvidenceRefused) as exc:
        _hg_promote(hg)
    text = " | ".join(exc.value.reasons)
    for n in needles:
        assert n in text, text
    assert untouched(hg) == before


def test_bronze_with_owned_dbt_project_promotes_on_complete_evidence(hg):
    _hg_validated(hg)
    approved = _hg_promote(hg)
    assert approved.evidence["bronze"] is True
    assert approval.verification(loader.load("hg_dbt_branch", hg.root.parent))[0] == "verified"


def test_bronze_without_the_source_down_proof_is_refused(hg):
    _hg_validated(hg, source_down=False)
    _hg_refused(hg, "source-down proof")


def test_bronze_with_s3_objects_remaining_is_refused(hg):
    _hg_validated(hg, s3_remaining=2)
    _hg_refused(hg, "S3: 2 object(s) remain", "cleanup s3_objects")


def test_bronze_with_an_unloaded_batch_is_refused(hg):
    _hg_validated(hg, batch_status="extracted")
    _hg_refused(hg, "not 'loaded'")


def test_bronze_with_too_few_batches_is_refused(hg):
    _hg_validated(hg, batches=1)
    _hg_refused(hg, "at least 3 bronze batches")


def test_bronze_evidence_that_omits_the_bronze_section_is_refused(hg):
    """A non-bronze record cannot promote a bronze pipeline."""
    def drop(r):
        r.bronze_staging = False
    _hg_validated(hg, report_tweak=drop)
    _hg_refused(hg, "disagree on bronze_staging", "no source-removed proof")


@pytest.mark.parametrize("rel", [
    "dwh_dbt/seeds/manual_excluded_partner_ids.csv",
    "dwh_dbt/models/silver/dim_artist_active.sql",
    "dwh_dbt/dbt_project.yml",
])
def test_editing_the_owned_dbt_project_after_validation_is_refused(hg, rel):
    _hg_validated(hg)
    target = hg.root / rel
    marker = {"csv": "\n", "yml": "\n# edit\n", "sql": "\n-- edit\n"}[rel.rsplit(".", 1)[1]]
    target.write_text(target.read_text() + marker)
    _hg_refused(loader.load("hg_dbt_branch", hg.root.parent), "the pipeline changed since")


# ------------------------------------------------------------------ the CLI is the same gate

@pytest.fixture
def cli_pipeline(pipeline, monkeypatch):
    from dpagent.cli import pipeline as pipeline_cli
    monkeypatch.setattr(pipeline_cli.pipelines_mod, "PIPELINES_DIR", pipeline.root.parent)
    return pipeline


def _cli(pipeline, *extra):
    fx, ex = _paths(pipeline)
    return CliRunner().invoke(pipeline_group, ["promote", "demo", "--yes", "--fixture", str(fx),
                                               "--expected", str(ex), *extra])


def test_cli_promote_requires_fixture_and_expected(cli_pipeline):
    result = CliRunner().invoke(pipeline_group, ["promote", "demo", "--yes"])
    assert result.exit_code != 0
    assert "--fixture" in result.output


def test_cli_refuses_without_evidence_and_changes_nothing(cli_pipeline):
    before = untouched(cli_pipeline)
    result = _cli(cli_pipeline)
    assert result.exit_code == 1
    assert "validation evidence not accepted" in result.output
    assert "nothing was changed" in result.output
    assert untouched(cli_pipeline) == before


def test_cli_refuses_a_failed_validation_and_prints_every_reason(cli_pipeline):
    validated(cli_pipeline, report_tweak=lambda r: (setattr(r, "run2_status", "timeout"),
                                                    setattr(r, "cleanup_ok", False)))
    before = untouched(cli_pipeline)
    result = _cli(cli_pipeline)
    assert result.exit_code == 1
    assert "timed out" in result.output and "cleanup pipeline_artifacts" in result.output
    assert untouched(cli_pipeline) == before


def test_cli_promotes_on_valid_evidence(cli_pipeline):
    evidence_id = validated(cli_pipeline)
    result = _cli(cli_pipeline)
    assert result.exit_code == 0, result.output
    assert evidence_id in result.output
    assert approval.verification(loader.load("demo", cli_pipeline.root.parent))[0] == "verified"
    again = _cli(cli_pipeline)
    assert "approval links validation evidence" in " ".join(again.output.split())


def test_cli_and_python_agree(cli_pipeline):
    """Same inputs, same verdict, same reasons through both doors."""
    validated(cli_pipeline, report_tweak=lambda r: setattr(r, "run1_status", "failed"))
    with pytest.raises(evidence.EvidenceRefused) as exc:
        promote(cli_pipeline)
    result = _cli(cli_pipeline)
    assert result.exit_code == 1
    for reason in exc.value.reasons:
        assert reason.split(" (")[0][:40] in " ".join(result.output.split())


def test_the_sqlite_table_is_append_only_by_construction(pipeline):
    """No product code path updates or deletes evidence; ids are content-derived."""
    first = validated(pipeline)
    second = validated(pipeline)
    assert first == second or state.get_evidence(second) is not None
    with pytest.raises(sqlite3.IntegrityError):
        state.record_evidence(first, "demo", "{}", "x")


@pytest.mark.skipif(hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0,
                    reason="root ignores file permissions")
def test_reading_evidence_from_a_read_only_journal_that_predates_the_table(tmp_path):
    """The evidence table is created lazily, not in SCHEMA: a non-root user on
    an existing root-owned journal must still be able to run every read-only
    command (found when the unit suite itself could not even collect against a
    host journal after the table was first added to SCHEMA)."""
    state.conn()                       # the journal as an older version left it
    state.conn().execute("DROP TABLE IF EXISTS validation_evidence")
    state.conn().commit()
    state.close()
    state.DB_PATH.chmod(0o444)
    try:
        assert state.get_evidence("ev-x") is None
        assert state.latest_evidence("demo") is None
        assert state.list_installs() == []
    finally:
        state.close()
        state.DB_PATH.chmod(0o644)
