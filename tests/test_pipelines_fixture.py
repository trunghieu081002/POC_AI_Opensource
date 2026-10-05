"""Steps 4-5 of the validation a drafted pipeline needs before its
*numbers* can be trusted (docs/layer2.md, "Authoring pipelines with a
model" / the Layer 3 M2 design review).

env_overrides_for_source/_warehouse are real-verified against the actual
pipelines/demo manifest (docs/deploy-log.md) - pure inspection of an
already-loaded Pipeline, no subprocess, no database. seed_source/
compare_curated are unit-tested with the psql subprocess call mocked, the
same wall validate.check_procedures already hits on this host (no
passwordless sudo to postgres)."""
import contextlib
import json
import os
import subprocess

import pytest
import yaml

from dpagent.pipelines import fixture, loader, pg_throwaway
from dpagent.pipelines.pg_throwaway import ThrowawayDB


# ---------------------------------------------------------------- env overrides

def _pipeline_with_refs(root, *, source_connection, warehouse):
    data = {
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres", "connection": source_connection,
                   "tables": ["t"]},
        "warehouse": warehouse,
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "t", "min": 1}]}],
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return loader.load("demo", root)


_DB = ThrowawayDB(host="h", port="5433", database="d", user="u", password="p")


def test_env_overrides_for_source_only_covers_ref_fields(tmp_path):
    pipeline = _pipeline_with_refs(
        tmp_path / "pipelines",
        source_connection={"host": "${SRC_HOST}", "port": "5432",  # literal, not a ref
                           "database": "${SRC_DB:-fallback}", "user": "${SRC_USER}",
                           "password": "${SRC_PASSWORD}"},
        warehouse={"host": "h", "database": "d"})
    overrides = fixture.env_overrides_for_source(pipeline, _DB)
    assert overrides == {
        "SRC_HOST": "h", "SRC_DB": "d", "SRC_USER": "u", "SRC_PASSWORD": "p",
    }
    assert "port" not in str(overrides)   # the literal "5432" field produced no override


def test_env_overrides_for_warehouse_only_covers_ref_fields(tmp_path):
    pipeline = _pipeline_with_refs(
        tmp_path / "pipelines",
        source_connection={"host": "h"},
        warehouse={"host": "${WH_HOST}", "database": "literal_db",
                  "user": "${WH_USER}", "password": "${WH_PASSWORD}"})
    overrides = fixture.env_overrides_for_warehouse(pipeline, _DB)
    assert overrides == {"WH_HOST": "h", "WH_USER": "u", "WH_PASSWORD": "p"}


def test_env_overrides_are_empty_when_the_manifest_uses_only_literals(tmp_path):
    pipeline = _pipeline_with_refs(
        tmp_path / "pipelines",
        source_connection={"host": "literal-host"},
        warehouse={"host": "literal-wh", "database": "d"})
    assert fixture.env_overrides_for_source(pipeline, _DB) == {}
    assert fixture.env_overrides_for_warehouse(pipeline, _DB) == {}


def test_env_overrides_real_against_the_actual_demo_pipeline():
    """Real, not synthetic: the exact manifest this repo already runs in
    production."""
    pipeline = loader.load("demo")
    overrides = fixture.env_overrides_for_source(pipeline, _DB)
    assert overrides == {
        "ODOO_DB_HOST": "h", "ODOO_DB_PORT": "5433", "ODOO_DB_NAME": "d",
        "ODOO_DB_USER": "u", "ODOO_DB_PASSWORD": "p",
    }
    wh_overrides = fixture.env_overrides_for_warehouse(pipeline, _DB)
    assert wh_overrides == {
        "WAREHOUSE_DB_HOST": "h", "WAREHOUSE_DB_PORT": "5433", "WAREHOUSE_DB_NAME": "d",
        "WAREHOUSE_DB_USER": "u", "WAREHOUSE_DB_PASSWORD": "p",
    }


# ---------------------------------------------------------------- Fixture loading

def test_load_fixture_parses_tables_columns_and_rows(tmp_path):
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump({
        "tables": [{
            "name": "sale_order",
            "columns": {"id": "bigint", "amount_total": "numeric"},
            "rows": [{"id": 1, "amount_total": 100}, {"id": 2, "amount_total": 200}],
        }],
    }))
    loaded = fixture.load_fixture(path)
    assert len(loaded.tables) == 1
    assert loaded.tables[0].name == "sale_order"
    assert loaded.tables[0].rows[0]["id"] == 1


def test_load_fixture_rejects_a_row_referencing_an_undeclared_column(tmp_path):
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump({
        "tables": [{"name": "t", "columns": {"id": "bigint"},
                   "rows": [{"id": 1, "typo_col": "x"}]}],
    }))
    with pytest.raises(ValueError, match="typo_col"):
        fixture.load_fixture(path)


def test_load_fixture_rejects_no_tables(tmp_path):
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump({"tables": []}))
    with pytest.raises(ValueError, match="no tables"):
        fixture.load_fixture(path)


# ---------------------------------------------------------------- ExpectedResult loading

def test_load_expected_parses_table_and_rows(tmp_path):
    path = tmp_path / "expected.yaml"
    path.write_text(yaml.safe_dump({
        "table": "fct_monthly_sales",
        "rows": [{"month": "2026-01", "revenue": 1500000}],
        "row_count": 1,
    }))
    loaded = fixture.load_expected(path)
    assert loaded.table == "fct_monthly_sales"
    assert loaded.row_count == 1
    assert loaded.rows[0]["revenue"] == 1500000


def test_load_expected_allows_an_explicit_empty_rows_list(tmp_path):
    path = tmp_path / "expected.yaml"
    path.write_text(yaml.safe_dump({"table": "fct_x", "rows": []}))
    loaded = fixture.load_expected(path)
    assert loaded.rows == []


def test_load_expected_requires_table_and_rows(tmp_path):
    path = tmp_path / "expected.yaml"
    path.write_text(yaml.safe_dump({"table": "fct_x"}))
    with pytest.raises(ValueError, match="table.*rows"):
        fixture.load_expected(path)


# ---------------------------------------------------------------- seed_source (mocked)

def test_seed_source_creates_the_table_then_inserts_every_row(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (
        calls.append(cmd[cmd.index("-c") + 1]),
        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))[1])

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint", "amount": "numeric"},
        rows=[{"id": 1, "amount": 100}, {"id": 2, "amount": 200}])])
    fixture.seed_source(fx, _DB)

    assert calls[0].startswith('CREATE TABLE "sale_order"')
    assert 'INSERT INTO "sale_order"' in calls[1] and "1" in calls[1]
    assert 'INSERT INTO "sale_order"' in calls[2] and "200" in calls[2]


def test_seed_source_quotes_string_values_and_passes_null_through(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (
        calls.append(cmd[cmd.index("-c") + 1]),
        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))[1])

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="t", columns={"name": "text", "note": "text"},
        rows=[{"name": "O'Brien", "note": None}])])
    fixture.seed_source(fx, _DB)

    insert_sql = calls[1]
    assert "'O''Brien'" in insert_sql
    assert "NULL" in insert_sql


def test_seed_source_raises_a_seed_error_when_create_table_fails(monkeypatch):
    """The P0 the review found: an earlier version never checked
    `returncode` at all, so a failed CREATE TABLE/INSERT still left
    `report.seeded = True` - a validation that could pass having actually
    validated nothing."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr='relation "sale_order" already exists'))
    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    with pytest.raises(fixture.FixtureSeedError, match="already exists"):
        fixture.seed_source(fx, _DB)


def test_seed_source_raises_a_seed_error_when_an_insert_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **k):
        calls.append(1)
        # CREATE TABLE succeeds, the INSERT fails.
        ok = len(calls) == 1
        return subprocess.CompletedProcess(
            cmd, 0 if ok else 1, stdout="", stderr="" if ok else "duplicate key value")
    monkeypatch.setattr(subprocess, "run", fake_run)

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="t", columns={"id": "bigint"}, rows=[{"id": 1}, {"id": 1}])])
    with pytest.raises(fixture.FixtureSeedError, match="duplicate key"):
        fixture.seed_source(fx, _DB)


# ---------------------------------------------------------------- compare_curated (mocked)
#
# Compares through `row_to_json`, not raw tab-separated text (see
# fixture.compare_curated's own docstring for exactly why: NULL vs "",
# 100 vs 100.00, a value containing a literal tab) - every mocked
# `psql -c "SELECT row_to_json(t) FROM (...) t;"` call below returns one
# JSON object per line, the real shape `-A -t` output takes for that query.

def _json_lines(*rows: dict) -> str:
    return "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else "")


def _mock_compare_curated(monkeypatch, column_types: dict[str, str], data_stdout: str, *,
                          returncode: int = 0, stderr: str = ""):
    """Mocks the two real `psql` calls `compare_curated` now makes -
    `_column_types`'s `information_schema.columns` lookup (M2.4.3: needed
    to decide, per column, whether a numeric-looking string should be
    canonicalized as a number - see `_canon_value`'s own docstring) and the
    `row_to_json` data query - dispatched by which SQL text each call
    carries, not by call order, so this stays correct regardless of which
    one `compare_curated` issues first."""
    def fake_run(cmd, **k):
        sql = " ".join(str(c) for c in cmd)
        if "information_schema.columns" in sql:
            rows = [{"column_name": c, "data_type": t} for c, t in column_types.items()]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(rows), stderr="")
        return subprocess.CompletedProcess(cmd, returncode, stdout=data_stdout, stderr=stderr)
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_compare_curated_passes_when_rows_match_exactly(monkeypatch):
    _mock_compare_curated(
        monkeypatch, {"month": "text", "revenue": "integer"},
        _json_lines({"month": "2026-01", "revenue": 1500000}))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": 1500000}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_fails_when_a_row_differs(monkeypatch):
    _mock_compare_curated(
        monkeypatch, {"month": "text", "revenue": "integer"},
        _json_lines({"month": "2026-01", "revenue": 9999999}))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": 1500000}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False


def test_compare_curated_fails_on_a_row_count_mismatch(monkeypatch):
    _mock_compare_curated(
        monkeypatch, {"month": "text", "revenue": "integer"},
        _json_lines({"month": "2026-01", "revenue": 100}, {"month": "2026-02", "revenue": 200}))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": 100}], row_count=1)
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False
    assert "expected 1" in result.detail


def test_compare_curated_is_order_independent(monkeypatch):
    _mock_compare_curated(
        monkeypatch, {"month": "text", "revenue": "integer"},
        _json_lines({"month": "2026-02", "revenue": 200}, {"month": "2026-01", "revenue": 100}))
    expected = fixture.ExpectedResult(table="fct_x", rows=[
        {"month": "2026-01", "revenue": 100}, {"month": "2026-02", "revenue": 200}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_surfaces_a_real_query_failure(monkeypatch):
    _mock_compare_curated(
        monkeypatch, {"month": "text", "revenue": "integer"}, "",
        returncode=1, stderr='relation "fct_x" does not exist')
    expected = fixture.ExpectedResult(table="fct_x", rows=[])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False
    assert "does not exist" in result.detail


def test_compare_curated_distinguishes_null_from_empty_string(monkeypatch):
    """The exact ambiguity the old tab-separated-text approach had: a
    Postgres NULL and a real empty string both rendered as "" there."""
    _mock_compare_curated(monkeypatch, {"note": "text"}, _json_lines({"note": None}))
    expected = fixture.ExpectedResult(table="t", rows=[{"note": ""}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False   # NULL != "" - must not be treated as a match


def test_compare_curated_treats_equivalent_decimal_formatting_as_equal(monkeypatch):
    _mock_compare_curated(monkeypatch, {"total": "numeric"}, _json_lines({"total": 100.00}))
    expected = fixture.ExpectedResult(table="t", rows=[{"total": 100}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_rejects_expected_rows_with_different_column_sets(monkeypatch):
    _mock_compare_curated(monkeypatch, {"id": "integer", "amount": "integer"}, "")
    expected = fixture.ExpectedResult(table="t", rows=[
        {"id": 1, "amount": 100}, {"id": 2}])   # second row is missing "amount"
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False
    assert "same set of columns" in result.detail


def test_compare_curated_handles_a_value_containing_a_literal_tab(monkeypatch):
    """The other real failure mode the old tab-separated-text approach
    had - JSON round-trips this correctly with no special-case code."""
    _mock_compare_curated(monkeypatch, {"note": "text"}, _json_lines({"note": "a\tb"}))
    expected = fixture.ExpectedResult(table="t", rows=[{"note": "a\tb"}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_does_not_round_a_high_precision_decimal(monkeypatch):
    """`json.loads(..., parse_float=Decimal)` on the actual side, and a
    numeric-looking *string* on the expected side (the M2.4.2 review's own
    recommended authoring convention for a monetary `expected.yaml` value,
    to protect it from YAML's own lossy float parsing) - a raw JSON literal
    with more significant digits than a native float can represent, typed
    directly here (not built through `json.dumps` of a Python float, which
    would already lose the precision this test exists to catch, before
    compare_curated ever runs). "revenue" is reported numeric by Postgres,
    so the expected side's quoted string is canonicalized as a number too."""
    raw = '{"revenue": 123456789012345.123456789}\n'
    _mock_compare_curated(monkeypatch, {"revenue": "numeric"}, raw)
    expected = fixture.ExpectedResult(
        table="t", rows=[{"revenue": "123456789012345.123456789"}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True, result.detail


def test_compare_curated_does_not_confuse_two_decimals_differing_only_past_28_sig_figs(
        monkeypatch):
    """The exact regression `Decimal.normalize()` used to cause (M2.4.3
    review): normalize() rounds to the current thread's context precision
    (28 significant digits by default), so two genuinely different values
    that only differ beyond that many significant digits used to compare
    equal. `_canon_number` must never call it."""
    from decimal import Decimal
    a = Decimal("100.123456789012345678901234567890123")
    b = Decimal("100.123456789012345678901234567890124")
    assert a != b
    assert fixture._canon_number(a) != fixture._canon_number(b)
    _mock_compare_curated(monkeypatch, {"revenue": "numeric"},
                         json.dumps({"revenue": str(a)}) + "\n")
    expected = fixture.ExpectedResult(table="t", rows=[{"revenue": str(b)}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False, "two distinct high-precision decimals must not compare equal"


def test_compare_curated_does_not_coerce_a_leading_zero_text_code_to_a_number(monkeypatch):
    """M2.4.3 review's own repro: a customer code column typed `text`
    whose real value is "00123" must not silently compare equal to "123" -
    only a column Postgres itself reports as numeric gets that treatment."""
    _mock_compare_curated(monkeypatch, {"code": "text"}, _json_lines({"code": "00123"}))
    expected = fixture.ExpectedResult(table="t", rows=[{"code": "123"}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False


def test_compare_curated_still_matches_a_quoted_numeric_string_on_a_numeric_column(monkeypatch):
    """The other side of the same fix: a numeric column must still treat
    `"100.00"` (quoted, e.g. to protect precision) and `100` as equal."""
    _mock_compare_curated(monkeypatch, {"revenue": "numeric"}, _json_lines({"revenue": 100}))
    expected = fixture.ExpectedResult(table="t", rows=[{"revenue": "100.00"}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True, result.detail


def test_canon_number_never_calls_normalize_so_high_precision_values_stay_distinct():
    """format(d, "f") is exact - no context precision involved - unlike
    Decimal.normalize(), which rounds to the current thread's context
    precision (28 significant digits by default)."""
    from decimal import Decimal
    a = Decimal("100.123456789012345678901234567890123")
    b = Decimal("100.123456789012345678901234567890124")
    assert a.normalize() == b.normalize()   # the bug this guards against
    assert fixture._canon_number(a) != fixture._canon_number(b)


def test_canon_number_treats_positive_and_negative_zero_identically():
    from decimal import Decimal
    assert fixture._canon_number(Decimal("-0.00")) == fixture._canon_number(0) == "0"


def test_canon_value_normalizes_decimal_int_float_and_precise_string_identically():
    from decimal import Decimal
    assert (fixture._canon_value(Decimal("100.00"), numeric=True)
           == fixture._canon_value(100, numeric=True)
           == fixture._canon_value(100.0, numeric=True)
           == fixture._canon_value("100.00", numeric=True) == "100")
    assert fixture._canon_value("123456789012345.123456789", numeric=True) == \
        "123456789012345.123456789"
    assert fixture._canon_value(Decimal("123456789012345.123456789"), numeric=True) == \
        fixture._canon_value("123456789012345.123456789", numeric=True)


def test_canon_value_leaves_a_non_numeric_string_untouched():
    assert fixture._canon_value("Acme Corp", numeric=True) == "Acme Corp"
    assert fixture._canon_value("2026-01", numeric=True) == "2026-01"   # not a decimal literal


def test_canon_value_never_coerces_a_numeric_looking_string_on_a_non_numeric_column():
    """The M2.4.3 review's own repro: `numeric=False` must leave "00123"
    exactly as written, even though it matches the decimal-literal regex -
    a text column's leading zeros are business-meaningful, never a number
    formatted with extra precision."""
    assert fixture._canon_value("00123", numeric=False) == "00123"
    assert fixture._canon_value("00123", numeric=False) != fixture._canon_value(
        "123", numeric=False)
    assert fixture._canon_value("00123", numeric=True) == fixture._canon_value(
        "123", numeric=True)


# ---------------------------------------------------------------- _temporarily

def test_temporarily_restores_a_previously_set_value(monkeypatch):
    monkeypatch.setenv("DPAGENT_FIXTURE_TEST_VAR", "original")
    with fixture._temporarily({"DPAGENT_FIXTURE_TEST_VAR": "overridden"}):
        assert os.environ["DPAGENT_FIXTURE_TEST_VAR"] == "overridden"
    assert os.environ["DPAGENT_FIXTURE_TEST_VAR"] == "original"


def test_temporarily_removes_a_previously_unset_value(monkeypatch):
    monkeypatch.delenv("DPAGENT_FIXTURE_TEST_VAR_2", raising=False)
    with fixture._temporarily({"DPAGENT_FIXTURE_TEST_VAR_2": "set-for-a-moment"}):
        assert os.environ["DPAGENT_FIXTURE_TEST_VAR_2"] == "set-for-a-moment"
    assert "DPAGENT_FIXTURE_TEST_VAR_2" not in os.environ


def test_temporarily_restores_even_when_the_block_raises():
    import os as _os
    _os.environ.pop("DPAGENT_FIXTURE_TEST_VAR_3", None)
    with pytest.raises(RuntimeError):
        with fixture._temporarily({"DPAGENT_FIXTURE_TEST_VAR_3": "x"}):
            raise RuntimeError("boom")
    assert "DPAGENT_FIXTURE_TEST_VAR_3" not in _os.environ


# ---------------------------------------------------------------- preflight_fixture_host

def test_preflight_fixture_host_is_really_unavailable_on_this_host(tmp_path):
    """Real, not mocked - this operator has neither root nor passwordless
    sudo to postgres on this host."""
    pipeline = _real_pipeline(tmp_path)
    result = fixture.preflight_fixture_host(pipeline)
    assert result.ok is False
    assert "root" in result.detail
    assert "sudo" in result.detail


def test_preflight_fixture_host_zero_mutation_when_it_fails(tmp_path, monkeypatch):
    """"Nếu thiếu điều kiện: Exit 2. Chưa tạo database/role tạm. Chưa
    seed. Chưa deploy bất cứ artifact nào" - the M2.4.2 review's own
    Definition of Done. Confirmed by making every downstream step raise if
    it is ever reached at all."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)

    def boom(*a, **k):
        raise AssertionError("must not be called when preflight fails")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", boom)
    monkeypatch.setattr(fixture, "seed_source", boom)
    monkeypatch.setattr(deploy_mod, "deploy", boom)

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])
    report = fixture.run_fixture(pipeline, fx, expected)

    assert "preflight failed" in report.unavailable_reason
    assert report.seeded is False
    assert report.deployed is False
    assert report.clone_name == ""   # never even got to building a clone


def _mock_preflight_subprocess(monkeypatch, *, sudo_ok=True, pg_ready_ok=True,
                               scheduler_active=True):
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["sudo", "-n", "-u", "postgres"] and cmd[4:5] == ["true"]:
            return _sp.CompletedProcess(cmd, 0 if sudo_ok else 1, stdout="", stderr="")
        if cmd[:4] == ["sudo", "-n", "-u", "postgres"] and cmd[4:5] == ["pg_isready"]:
            return _sp.CompletedProcess(cmd, 0 if pg_ready_ok else 1, stdout="",
                                        stderr="" if pg_ready_ok else "no response")
        if cmd[:2] == ["systemctl", "is-active"]:
            return _sp.CompletedProcess(
                cmd, 0, stdout="active\n" if scheduler_active else "inactive\n", stderr="")
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)


def _mock_preflight_installed(monkeypatch, *, dlt=True, dbt=True, airflow=True):
    from dpagent.engine import state

    def fake_get_install(pack):
        present = {"dlt": dlt, "dbt": dbt, "airflow": airflow}.get(pack, False)
        return {"pack": pack} if present else None
    monkeypatch.setattr(state, "get_install", fake_get_install)


def test_preflight_fixture_host_passes_when_every_precondition_is_met(tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_preflight_subprocess(monkeypatch)
    _mock_preflight_installed(monkeypatch)
    venv_bin = tmp_path / "airflow_venv"
    venv_bin.mkdir()
    (venv_bin / "airflow").write_text("#!/bin/sh\n")
    monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
    monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", tmp_path)

    result = fixture.preflight_fixture_host(pipeline)
    assert result.ok is True, result.detail


def test_preflight_fixture_host_fails_when_dlt_is_not_installed(tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_preflight_subprocess(monkeypatch)
    _mock_preflight_installed(monkeypatch, dlt=False)
    venv_bin = tmp_path / "airflow_venv"
    venv_bin.mkdir()
    (venv_bin / "airflow").write_text("#!/bin/sh\n")
    monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
    monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", tmp_path)

    result = fixture.preflight_fixture_host(pipeline)
    assert result.ok is False
    assert "dlt" in result.detail


def test_preflight_fixture_host_fails_when_postgres_is_not_accepting_connections(
        tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_preflight_subprocess(monkeypatch, pg_ready_ok=False)
    _mock_preflight_installed(monkeypatch)
    venv_bin = tmp_path / "airflow_venv"
    venv_bin.mkdir()
    (venv_bin / "airflow").write_text("#!/bin/sh\n")
    monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
    monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", tmp_path)

    result = fixture.preflight_fixture_host(pipeline)
    assert result.ok is False
    assert "PostgreSQL" in result.detail


def test_preflight_fixture_host_fails_when_scheduler_is_not_active(tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_preflight_subprocess(monkeypatch, scheduler_active=False)
    _mock_preflight_installed(monkeypatch)
    venv_bin = tmp_path / "airflow_venv"
    venv_bin.mkdir()
    (venv_bin / "airflow").write_text("#!/bin/sh\n")
    monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
    monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", tmp_path)

    result = fixture.preflight_fixture_host(pipeline)
    assert result.ok is False
    assert "scheduler" in result.detail


def test_preflight_fixture_host_requires_dbt_only_when_the_pipeline_has_a_dbt_stage(
        tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    root = tmp_path / "pipelines"
    d = root / "demo_dbt"
    (d / "models").mkdir(parents=True)
    (d / "models" / "stg_a.sql").write_text("select 1 as id\n")
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "demo_dbt", "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/o.csv"}},
        "warehouse": {"host": "h", "database": "d"},
        "stages": [
            {"name": "landing", "gates": [{"type": "row_count_bounds", "table": "o", "min": 1}]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing", "models": ["stg_a"]},
        ],
    }, sort_keys=False))
    pipeline = loader.load("demo_dbt", root)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_preflight_subprocess(monkeypatch)
    _mock_preflight_installed(monkeypatch, dbt=False)
    venv_bin = tmp_path / "airflow_venv"
    venv_bin.mkdir()
    (venv_bin / "airflow").write_text("#!/bin/sh\n")
    monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
    monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", tmp_path)

    result = fixture.preflight_fixture_host(pipeline)
    assert result.ok is False
    assert "dbt" in result.detail


# ----------------------------------------- preflight never crashes (M2.5 prep review)

def test_preflight_fixture_host_reports_a_sudo_timeout_as_a_reason_not_a_crash(
        tmp_path, monkeypatch):
    """An uncaught subprocess.TimeoutExpired from any preflight subprocess
    call used to crash run_fixture() with a raw traceback instead of a
    clean, reported unavailable_reason (exit 2) - sudo -n can still hang in
    some configurations despite -n (M2.5-prep review: "lỗi executable/
    permission/timeout trả unavailable, exit 2, có báo cáo")."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["sudo", "-n", "-u", "postgres"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 10))
        return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    _mock_preflight_installed(monkeypatch)
    venv_bin = tmp_path / "airflow_venv"
    venv_bin.mkdir()
    (venv_bin / "airflow").write_text("#!/bin/sh\n")
    monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
    monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", tmp_path)

    result = fixture.preflight_fixture_host(pipeline)   # must not raise
    assert result.ok is False
    assert "timed out" in result.detail


def test_preflight_fixture_host_reports_a_systemctl_timeout_as_a_reason_not_a_crash(
        tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "is-active"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 10))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    _mock_preflight_installed(monkeypatch)
    venv_bin = tmp_path / "airflow_venv"
    venv_bin.mkdir()
    (venv_bin / "airflow").write_text("#!/bin/sh\n")
    monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
    monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", tmp_path)

    result = fixture.preflight_fixture_host(pipeline)   # must not raise
    assert result.ok is False
    assert "timed out" in result.detail


def test_preflight_fixture_host_treats_a_permission_error_on_the_airflow_binary_as_could_not_check(
        tmp_path, monkeypatch):
    """Real, not simulated: chmod 0o000 on an ancestor directory reproduces
    a genuine PermissionError from Path.exists() - the same real failure
    mode `_verify_cleanup_complete`'s own `_safe_missing` test already
    confirmed for a different path. preflight must report this as "could
    not check", never crash, never silently "assumed present/absent"."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits - cannot reproduce as root")
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_preflight_subprocess(monkeypatch)
    _mock_preflight_installed(monkeypatch)
    locked = tmp_path / "locked"
    locked.mkdir()
    venv_bin = locked / "airflow_venv"
    venv_bin.mkdir()
    locked.chmod(0o000)
    try:
        monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
        monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", tmp_path)
        result = fixture.preflight_fixture_host(pipeline)   # must not raise
    finally:
        locked.chmod(0o755)
    assert result.ok is False
    assert "could not check" in result.detail


def test_preflight_fixture_host_treats_a_permission_error_on_a_writable_check_as_could_not_check(
        tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits - cannot reproduce as root")
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_preflight_subprocess(monkeypatch)
    _mock_preflight_installed(monkeypatch)
    venv_bin = tmp_path / "airflow_venv"
    venv_bin.mkdir()
    (venv_bin / "airflow").write_text("#!/bin/sh\n")
    monkeypatch.setattr(deploy_mod, "_airflow_paths", lambda: (venv_bin, tmp_path / "env"))
    locked = tmp_path / "locked"
    locked.mkdir()
    shared = locked / "pipelines"
    locked.chmod(0o000)
    try:
        monkeypatch.setattr(deploy_mod, "SHARED_PIPELINES_DIR", shared)
        result = fixture.preflight_fixture_host(pipeline)   # must not raise
    finally:
        locked.chmod(0o755)
    assert result.ok is False
    assert "could not check" in result.detail


def test_safe_exists_returns_none_on_a_real_permission_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits - cannot reproduce as root")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "child").mkdir()
    locked.chmod(0o000)
    try:
        assert fixture._safe_exists(locked / "child" / "x") is None
    finally:
        locked.chmod(0o755)


def test_writable_returns_none_on_a_real_permission_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits - cannot reproduce as root")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        assert fixture._writable(locked / "child" / "grandchild") is None
    finally:
        locked.chmod(0o755)


def test_run_preflight_check_returns_none_and_a_reason_on_a_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 10))
    monkeypatch.setattr(subprocess, "run", fake_run)
    proc, detail = fixture._run_preflight_check(["sleep", "99"], timeout=1)
    assert proc is None
    assert "timed out" in detail


def test_run_preflight_check_returns_none_and_a_reason_when_the_binary_is_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(subprocess, "run", fake_run)
    proc, detail = fixture._run_preflight_check(["does-not-exist"])
    assert proc is None
    assert "could not be run" in detail


# ---------------------------------------------------------------- run_fixture (mocked end to end)

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    yield
    state.close()


def _fake_throwaway(*dbs):
    """Replacement for pg_throwaway.throwaway_database that hands out
    `dbs` in order across nested `with` calls, real ContextManager shape -
    and, on exit, marks each db's teardown as having actually succeeded
    (`database_dropped`/`role_dropped = True`), the default assumption
    every "happy path" test below makes. A test that needs to simulate a
    *failed* teardown does not use this helper - see
    `test_run_fixture_is_not_ok_when_a_throwaway_database_fails_to_drop`."""
    it = iter(dbs)

    @contextlib.contextmanager
    def _cm(prefix="x"):
        db = next(it)
        try:
            yield db
        finally:
            db.database_dropped = True
            db.role_dropped = True
    return _cm


def _real_pipeline(tmp_path):
    from dpagent.pipelines import loader
    root = tmp_path / "pipelines"
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres", "connection": {"host": "${SRC_HOST}"},
                  "tables": ["sale_order"]},
        "warehouse": {"host": "${WH_HOST}", "database": "${WH_NAME}", "schema": "demo"},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "sale_order", "min": 1}]}],
    }, sort_keys=False))
    return loader.load("demo", root)


def _mark_run_ok_immediately(monkeypatch):
    """Fakes deploy_mod.trigger_dag as "the DAG ran and finished" - marks
    the just-started run 'ok' synchronously so run_fixture's own wait loop
    exits on its very first poll, with a `sleep` stub that must never
    actually be called (a real wait would."""
    from dpagent.engine import state
    from dpagent.pipelines import deploy as deploy_mod

    def fake_trigger(name, run_id, full_refresh=False):
        state.finish_run(run_id, "ok")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(deploy_mod, "trigger_dag", fake_trigger)


def _mock_deploy_isolation(monkeypatch, tmp_path, *, unpause_ok=True, undeploy_ok=True):
    """Every real, host-touching call `run_fixture` makes around the
    clone's own deploy/unpause/undeploy - `deploy_mod.deploy` itself is
    mocked per-test (its own success/failure is what each test is about),
    but unpause/undeploy/deployed_names are real subprocess/filesystem
    calls that would otherwise fire for real (and fail, on a host with no
    root/no airflow OS user) in every test that does not itself care about
    them. Also points every private path-resolution helper
    `_verify_cleanup_complete` reaches into at empty, throwaway directories
    under `tmp_path` - real host paths under `/opt/airflow` are 700,
    airflow-only (confirmed for real: `Path.exists()` on them raises
    `PermissionError`, not just "False", for this operator), so a mocked
    test needs its own, readable stand-ins to get a deterministic
    "verified gone" instead of always the "could not check" branch."""
    from dpagent.pipelines import deploy as deploy_mod
    import subprocess as _sp

    # preflight_fixture_host() runs real sudo/systemctl/pg_isready/state
    # lookups - real on this host means "not root, no sudo," which would
    # make every mocked test below fail at the very first check instead of
    # exercising what it actually means to test. Bypassed here, for every
    # mocked test; the one test that wants the *real* preflight path
    # (test_run_fixture_reports_unavailable_when_postgres_throwaway_is_missing)
    # deliberately does not call this helper.
    monkeypatch.setattr(fixture, "preflight_fixture_host",
                        lambda pipeline: fixture.PreflightResult(ok=True))

    monkeypatch.setattr(deploy_mod, "deployed_names", lambda: [])
    monkeypatch.setattr(deploy_mod, "unpause_dag", lambda name: _sp.CompletedProcess(
        [], 0 if unpause_ok else 1, stdout="", stderr="" if unpause_ok else "unpause failed"))
    monkeypatch.setattr(deploy_mod, "undeploy", lambda pipeline: _FakeUndeployResult(undeploy_ok))

    airflow_home = tmp_path / "fake_airflow_home"
    (airflow_home / "dags").mkdir(parents=True, exist_ok=True)
    dbt_project = tmp_path / "fake_dbt_project"
    dbt_project.mkdir(parents=True, exist_ok=True)
    secrets_file = airflow_home / "pipelines.env"
    secrets_file.write_text("")
    monkeypatch.setattr(deploy_mod, "_airflow_install_dir", lambda: airflow_home)
    monkeypatch.setattr(deploy_mod, "_dbt_project_dir", lambda: dbt_project)
    monkeypatch.setattr(deploy_mod, "_pipeline_secrets_file", lambda: secrets_file)
    return airflow_home, dbt_project, secrets_file


def test_verify_cleanup_complete_treats_a_permission_error_as_unverified(tmp_path):
    """Real on this host, not synthetic: `install_dag()`'s own docstring
    says `<airflow install_dir>/home` is 700, airflow-only -
    `Path.exists()` on a file inside a directory this operator cannot even
    traverse raises `PermissionError`, not just `False`. Must never be
    silently read as "verified gone" - `_safe_missing()` catches it and
    reports "could not check" instead."""
    from dpagent.pipelines import deploy as deploy_mod
    airflow_home = tmp_path / "airflow_home"
    dags_dir = airflow_home / "home" / "dags"   # _verify_cleanup_complete's own path shape
    dags_dir.mkdir(parents=True)
    dbt_project = tmp_path / "dbt_project"
    dbt_project.mkdir()
    secrets_file = tmp_path / "pipelines.env"
    secrets_file.write_text("")

    dags_dir.chmod(0o000)
    try:
        pipeline = _real_pipeline(tmp_path)
        clone, _ = fixture.make_validation_clone(pipeline, tmp_path / "clones")

        class _Deploy:
            SHARED_PIPELINES_DIR = tmp_path / "shared"

            @staticmethod
            def _airflow_install_dir():
                return airflow_home

            @staticmethod
            def _dbt_project_dir():
                return dbt_project

            @staticmethod
            def _pipeline_secrets_file():
                return secrets_file

            @staticmethod
            def _parse_env_file(path):
                return {}

            @staticmethod
            def _pipeline_env_refs(p):
                return {}

            @staticmethod
            def deployed_names():
                return []

        undeploy_result = _FakeUndeployResult(True)
        ok, detail = fixture._verify_cleanup_complete(clone, undeploy_result, _Deploy)
        assert ok is False
        assert "could not check" in detail
    finally:
        dags_dir.chmod(0o755)   # tmp_path's own cleanup needs traverse access back


def test_verify_cleanup_complete_reports_a_still_running_journal_entry(isolated_db, tmp_path):
    """The DAG/database/secrets can all be gone and this still must not be
    "verified gone" - a run dpagent's own journal still shows `running` for
    this clone means something (a worker, a stuck task) may still be
    touching it."""
    from dpagent.engine import state
    from dpagent.pipelines import deploy as deploy_mod

    pipeline = _real_pipeline(tmp_path)
    clone, _ = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    state.start_run("data", clone.name)   # left running, on purpose

    airflow_home = tmp_path / "airflow_home"
    (airflow_home / "home" / "dags").mkdir(parents=True)
    dbt_project = tmp_path / "dbt_project"
    dbt_project.mkdir()
    secrets_file = tmp_path / "pipelines.env"
    secrets_file.write_text("")

    class _Deploy:
        SHARED_PIPELINES_DIR = tmp_path / "shared"

        @staticmethod
        def _airflow_install_dir():
            return airflow_home

        @staticmethod
        def _dbt_project_dir():
            return dbt_project

        @staticmethod
        def _pipeline_secrets_file():
            return secrets_file

        @staticmethod
        def _parse_env_file(path):
            return {}

        @staticmethod
        def _pipeline_env_refs(p):
            return {}

        @staticmethod
        def deployed_names():
            return []

    undeploy_result = _FakeUndeployResult(True)
    ok, detail = fixture._verify_cleanup_complete(clone, undeploy_result, _Deploy)
    assert ok is False
    assert "running" in detail


class _FakeUndeployResult:
    def __init__(self, ok=True):
        self.dag_file_removed = ok
        self.dag_deleted_from_airflow = ok
        self.dag_delete_failed = not ok
        self.dag_delete_note = "" if ok else "simulated undeploy failure"
        self.published_files_removed = ok
        self.dbt_models_removed = ok
        self.dlt_state_removed = ok
        self.secrets_removed = ["DPAGENT_VALIDATE_X_WH_HOST"] if ok else []
        self.secrets_kept = {}
        self.secrets_note = ""
        self.scheduler_restarted = ok
        self.runs_cancelled = []


def test_run_fixture_reports_unavailable_when_postgres_throwaway_is_missing(tmp_path):
    """Real on this host, not mocked - no passwordless sudo to postgres."""
    pipeline = _real_pipeline(tmp_path)
    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.seeded is False
    assert report.unavailable_reason
    assert "sudo" in report.unavailable_reason


def test_run_fixture_reports_unavailable_when_deploy_needs_root(
        isolated_db, tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    _mock_deploy_isolation(monkeypatch, tmp_path)

    def boom(pipeline, **kwargs):
        raise deploy_mod.DeployError("publishing dbt models needs root")
    monkeypatch.setattr(deploy_mod, "deploy", boom)

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.seeded is True
    assert report.deployed is False
    assert "root" in report.unavailable_reason
    # The P0 the review found: cleanup must still be attempted even when
    # deploy() raised immediately - deploy() writes several real things in
    # sequence (procedures, dbt models, published files, secrets, the DAG
    # itself) and can fail partway through any one of them, after earlier
    # steps already had a real effect. undeploy() is idempotent, so calling
    # it here is always safe even when deploy() got nowhere at all.
    assert report.cleanup_attempted is True


def test_run_fixture_happy_path_both_runs_ok_and_idempotent(isolated_db, tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path)
    _mark_run_ok_immediately(monkeypatch)
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda expected, db, schema: fixture.ComparisonResult(True, "matched"))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[{"a": 1}])

    called_sleep = []
    report = fixture.run_fixture(pipeline, fx, expected, sleep=lambda s: called_sleep.append(s))

    assert report.ok is True
    assert report.idempotent is True
    assert report.run1_status == "ok" and report.run2_status == "ok"
    assert called_sleep == []   # the wait loop never had to actually poll-and-wait
    assert report.clone_name.startswith("demo__validate__")
    assert len(report.run_ids) == 2
    # Cleanup ran for real (mocked undeploy) and succeeded - a pass that
    # left a mess behind is not what `.ok` means (see FixtureRunReport.ok).
    assert report.cleanup_attempted is True
    assert report.cleanup_ok is True


def test_run_fixture_stops_after_the_first_run_fails(isolated_db, tmp_path, monkeypatch):
    from dpagent.engine import state
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path)

    def fake_trigger(name, run_id, full_refresh=False):
        state.finish_run(run_id, "failed")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(deploy_mod, "trigger_dag", fake_trigger)
    compare_calls = []
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda *a, **k: compare_calls.append(1) or fixture.ComparisonResult(True))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.run1_status == "failed"
    assert report.run2_status == ""   # never attempted
    assert compare_calls == []        # never compared a failed run's output
    assert report.ok is False
    # A failed run still gets cleaned up - deploy() did succeed, so the
    # clone's artifacts are real and must still be undeployed.
    assert report.cleanup_attempted is True
    assert report.cleanup_ok is True


def test_run_fixture_detects_a_non_idempotent_second_run(isolated_db, tmp_path, monkeypatch):
    """The real thing this exists to catch: a run that completes without
    error but silently duplicates revenue on a re-run."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path)
    _mark_run_ok_immediately(monkeypatch)

    comparisons = [fixture.ComparisonResult(True, "matched"),
                  fixture.ComparisonResult(False, "revenue doubled")]
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda *a, **k: comparisons.pop(0))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[{"a": 1}])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.run1_status == "ok" and report.run2_status == "ok"
    assert report.comparison_after_run1.ok is True
    assert report.comparison_after_run2.ok is False
    assert report.idempotent is False
    assert report.ok is False
    assert report.cleanup_attempted is True and report.cleanup_ok is True


def test_run_fixture_reports_unavailable_when_the_dag_cannot_be_unpaused(
        isolated_db, tmp_path, monkeypatch):
    """--allow-draft never unpauses (M1's own guarantee) - run_fixture must
    unpause the validation clone's DAG itself, explicitly, or a manual run
    of it is created queued and never starts (deploy.unpause_dag's own
    docstring). This is the P0 the review found: fixture.py used to trigger
    straight after deploy() with no unpause at all."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path, unpause_ok=False)
    triggered = []
    monkeypatch.setattr(deploy_mod, "trigger_dag", lambda *a, **k: triggered.append(1))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.deployed is True
    assert "unpause" in report.unavailable_reason
    assert triggered == []   # never even tried to trigger a still-paused DAG
    # deploy() did succeed, so the clone is real and must still be cleaned up.
    assert report.cleanup_attempted is True


def test_run_fixture_is_not_ok_when_data_matches_but_cleanup_fails(
        isolated_db, tmp_path, monkeypatch):
    """The user's own review: "Validation không được coi là hoàn chỉnh nếu
    chạy pass nhưng cleanup fail" - a passing comparison alone must not be
    `report.ok`."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path, undeploy_ok=False)
    _mark_run_ok_immediately(monkeypatch)
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda expected, db, schema: fixture.ComparisonResult(True, "matched"))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[{"a": 1}])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.run1_status == "ok" and report.run2_status == "ok"
    assert report.idempotent is True
    assert report.cleanup_attempted is True
    assert report.cleanup_ok is False
    assert report.ok is False   # data matched, but cleanup failing still fails the whole run


def test_run_fixture_refuses_a_clone_name_collision(isolated_db, tmp_path, monkeypatch):
    """Astronomically unlikely with a random 8-hex suffix, but must refuse
    outright rather than deploy over whatever is already there under that
    name - the user's own review item 5 ("refuse if collision detected")."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(fixture, "preflight_fixture_host",
                        lambda pipeline: fixture.PreflightResult(ok=True))
    monkeypatch.setattr(fixture, "_validation_suffix", lambda: "deadbeef")
    monkeypatch.setattr(deploy_mod, "deployed_names", lambda: ["demo__validate__deadbeef"])
    deploy_calls = []
    monkeypatch.setattr(deploy_mod, "deploy", lambda *a, **k: deploy_calls.append(1))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert "collision" in report.unavailable_reason
    assert deploy_calls == []   # refused before even provisioning a throwaway database
    assert report.seeded is False


# ---------------------------------------------------------------- make_validation_clone

def test_make_validation_clone_gets_a_unique_namespaced_name(tmp_path):
    pipeline = _real_pipeline(tmp_path)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    assert clone.name == f"demo__validate__{suffix}"
    assert clone.name != pipeline.name
    assert clone.maturity == "draft"
    assert clone.schedule is None


def test_make_validation_clone_renames_every_ref_uniquely(tmp_path):
    pipeline = _real_pipeline(tmp_path)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    prefix = f"DPAGENT_VALIDATE_{suffix.upper()}_"
    assert clone.source.connection["host"] == f"${{{prefix}SRC_HOST}}"
    assert clone.warehouse.host == f"${{{prefix}WH_HOST}}"
    assert clone.warehouse.database == f"${{{prefix}WH_DATABASE}}"
    # None of the clone's own refs collide with the real pipeline's own ref
    # names - this is what keeps ensure_pipeline_secrets_available()'s
    # *shared* pipelines.env file from ever conflating the two.
    assert clone.source.connection["host"] != pipeline.source.connection["host"]
    assert clone.warehouse.host != pipeline.warehouse.host


def test_make_validation_clone_redirects_a_literal_warehouse_into_the_throwaway_too(tmp_path):
    """M2.4.4 review's own P0, reproduced: an earlier version only renamed
    a `${VAR}` ref, leaving a *literal* warehouse host/database completely
    untouched - env_overrides_for_warehouse() then had nothing to override
    (no ref to match), so the clone's dbt/procedure stages connected to the
    real, literal warehouse regardless of whatever throwaway database
    run_fixture() had just provisioned. Every connection field must be
    forced into a throwaway-only ref whether the manifest wrote it as a
    literal, a ${VAR} ref, or a mix of both."""
    root = tmp_path / "pipelines"
    d = root / "quickstart"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "quickstart", "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/orders.csv"}},
        "warehouse": {"host": "literal-host", "port": "5432", "database": "literal-db",
                     "user": "literal-user", "password": "literal-pw",
                     "schema": "quickstart"},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "orders", "min": 1}]}],
    }, sort_keys=False))
    pipeline = loader.load("quickstart", root)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    prefix = f"DPAGENT_VALIDATE_{suffix.upper()}_WH_"
    assert clone.warehouse.host == f"${{{prefix}HOST}}"
    assert clone.warehouse.database == f"${{{prefix}DATABASE}}"
    assert clone.warehouse.user == f"${{{prefix}USER}}"
    assert clone.warehouse.password == f"${{{prefix}PASSWORD}}"
    # None of the literal production values survive anywhere on the clone.
    for value in (clone.warehouse.host, clone.warehouse.database,
                 clone.warehouse.user, clone.warehouse.password):
        assert value not in ("literal-host", "literal-db", "literal-user", "literal-pw")
    assert clone.warehouse.schema == "quickstart"   # schema is deliberately not renamed

    # And the overrides built from these refs actually cover every one of
    # them - the whole point of forcing them into refs in the first place.
    db = ThrowawayDB(host="tw-host", port="5433", database="tw-db",
                     user="tw-user", password="tw-pw")
    overrides = fixture.env_overrides_for_warehouse(clone, db)
    assert overrides == {
        f"{prefix}HOST": "tw-host", f"{prefix}PORT": "5433", f"{prefix}DATABASE": "tw-db",
        f"{prefix}USER": "tw-user", f"{prefix}PASSWORD": "tw-pw",
    }


def test_make_validation_clone_redirects_a_literal_odoo_postgres_source_too(tmp_path):
    """Same fix, source side: a literal odoo_postgres connection must be
    forced into the throwaway source, not left pointed at the real one."""
    root = tmp_path / "pipelines"
    d = root / "odoo_demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "odoo_demo", "summary": "t",
        "source": {"connector": "odoo_postgres",
                  "connection": {"host": "prod-odoo-host", "database": "prod_odoo_db",
                                "user": "prod_user", "password": "prod_pw"},
                  "tables": ["res_partner"]},
        "warehouse": {"host": "h", "database": "d", "schema": "odoo_demo"},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "res_partner", "min": 1}]}],
    }, sort_keys=False))
    pipeline = loader.load("odoo_demo", root)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    prefix = f"DPAGENT_VALIDATE_{suffix.upper()}_SRC_"
    assert clone.source.connection["host"] == f"${{{prefix}HOST}}"
    assert clone.source.connection["database"] == f"${{{prefix}DATABASE}}"
    assert clone.source.connection["user"] == f"${{{prefix}USER}}"
    assert clone.source.connection["password"] == f"${{{prefix}PASSWORD}}"
    for value in clone.source.connection.values():
        assert value not in ("prod-odoo-host", "prod_odoo_db", "prod_user", "prod_pw")


# ------------------------------------------------ unsupported source connectors (M2.4.4)

def _sql_server_pipeline(tmp_path):
    root = tmp_path / "pipelines"
    d = root / "mssql_demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "mssql_demo", "summary": "t",
        "source": {"connector": "sql_server",
                  "connection": {"host": "mssql-host", "user": "u", "password": "p",
                                "database": "d"},
                  "tables": ["orders"]},
        "warehouse": {"host": "h", "database": "d", "schema": "mssql_demo"},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "orders", "min": 1}]}],
    }, sort_keys=False))
    return loader.load("mssql_demo", root)


def test_unsupported_source_connector_reason_is_none_for_odoo_postgres(tmp_path):
    pipeline = _real_pipeline(tmp_path)
    assert fixture._unsupported_source_connector_reason(pipeline) is None


def _csv_pipeline(tmp_path, name="quickstart"):
    root = tmp_path / "pipelines"
    d = root / name
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": name, "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/orders.csv"}},
        "warehouse": {"host": "h", "database": "d", "schema": name},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "orders", "min": 1}]}],
    }, sort_keys=False))
    return loader.load(name, root)


def test_unsupported_source_connector_reason_refuses_csv_by_default(tmp_path):
    """M2.5-prep review: `--fixture` now refuses csv too (the strict=True
    default, what preflight_fixture_host always uses) - a csv-connector
    pipeline's own extract step reads its literal file directly, never a
    throwaway database, so a human-authored fixture's rows were being
    silently ignored rather than actually validated. This is a limit of
    fixture validation only; a normal deploy/run for csv is unaffected -
    nothing here touches deploy()/run_extract()."""
    pipeline = _csv_pipeline(tmp_path)
    reason = fixture._unsupported_source_connector_reason(pipeline)
    assert reason is not None
    assert "csv" in reason
    assert "fixture" in reason


def test_unsupported_source_connector_reason_non_strict_still_allows_csv(tmp_path):
    """make_validation_clone()'s own defense-in-depth use (strict=False):
    csv has no live connection to leak, so building a clone of one
    directly - for a reason unrelated to --fixture, e.g. testing dbt model
    renaming - stays allowed. Only preflight_fixture_host (the entry gate
    for --fixture itself) is strict."""
    pipeline = _csv_pipeline(tmp_path)
    assert fixture._unsupported_source_connector_reason(pipeline, strict=False) is None


def test_unsupported_source_connector_reason_refuses_sql_server(tmp_path):
    """sql_server has a live, host/port/database/user/password-shaped
    connection - but pg_throwaway's own throwaway source is Postgres-only
    (seed_source()/_psql() never speak pymssql), so there is no fixture
    adapter for it (M2.4.4 review's own "Connector chưa có fixture adapter
    thì từ chối trước provisioning")."""
    pipeline = _sql_server_pipeline(tmp_path)
    reason = fixture._unsupported_source_connector_reason(pipeline)
    assert reason is not None
    assert "sql_server" in reason


def test_make_validation_clone_refuses_an_unsupported_connector_directly(tmp_path):
    """Defense in depth: make_validation_clone() itself refuses, not just
    preflight_fixture_host() - a caller building a clone directly (bypassing
    run_fixture's own preflight) cannot accidentally build one for a
    connector this harness cannot isolate."""
    pipeline = _sql_server_pipeline(tmp_path)
    with pytest.raises(fixture.ValidationCloneError, match="sql_server"):
        fixture.make_validation_clone(pipeline, tmp_path / "clones")


def test_preflight_fixture_host_refuses_an_unsupported_connector_before_anything_else(
        tmp_path):
    """Zero mutation, same guarantee every other preflight reason already
    has - checked before root/sudo/anything else that touches the host."""
    pipeline = _sql_server_pipeline(tmp_path)
    result = fixture.preflight_fixture_host(pipeline)
    assert result.ok is False
    assert "sql_server" in result.detail


def test_preflight_fixture_host_refuses_csv_too(tmp_path):
    """M2.5-prep review's own explicit ask: refuse --fixture for csv too,
    not just a connector with a live connection - preflight_fixture_host
    is the entry gate for --fixture, and must be strict by default."""
    pipeline = _csv_pipeline(tmp_path)
    result = fixture.preflight_fixture_host(pipeline)
    assert result.ok is False
    assert "csv" in result.detail


def test_run_fixture_refuses_an_unsupported_connector_with_zero_mutation(tmp_path, monkeypatch):
    """Wires the full run_fixture() path: an unsupported connector is
    refused before make_validation_clone, throwaway_database, seed_source,
    or deploy are ever reached."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _sql_server_pipeline(tmp_path)
    for target, name in ((fixture, "make_validation_clone"), (pg_throwaway, "throwaway_database"),
                        (fixture, "seed_source"), (deploy_mod, "deploy")):
        def _boom(*a, _name=name, **k):
            raise AssertionError(f"{_name} must never be called - connector is unsupported")
        monkeypatch.setattr(target, name, _boom)

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="t", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert "sql_server" in report.unavailable_reason
    assert report.clone_name == ""   # make_validation_clone() was never even called
    assert report.seeded is False


def test_make_validation_clone_renames_dbt_model_files_uniquely(tmp_path):
    root = tmp_path / "pipelines"
    d = root / "demo_dbt"
    (d / "models").mkdir(parents=True)
    (d / "models" / "stg_orders.sql").write_text(
        "{{ config(materialized='table', schema='demo_dbt') }}\nselect 1 as id")
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "demo_dbt", "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/o.csv"}},
        "warehouse": {"host": "h", "database": "d", "schema": "demo_dbt"},
        "stages": [
            {"name": "landing", "gates": [{"type": "row_count_bounds", "table": "o", "min": 1}]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing", "models": ["stg_orders"]},
        ],
    }, sort_keys=False))
    pipeline = loader.load("demo_dbt", root)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")

    raw_stage = next(s for s in clone.stages if s.name == "raw")
    assert raw_stage.models == [f"stg_orders__validate_{suffix}"]
    model_file = (tmp_path / "clones" / clone.name / "models"
                 / f"stg_orders__validate_{suffix}.sql")
    assert model_file.exists()
    assert "select 1 as id" in model_file.read_text()
    # The real pipeline's own model file is untouched.
    assert (d / "models" / "stg_orders.sql").exists()


def test_make_validation_clone_pins_the_renamed_models_output_table_back_to_the_original(
        tmp_path):
    """M2.4.3 review: the renamed *file* must not change the *materialized
    table name* dbt actually writes - gates/procedures/expected.yaml all
    still reference the model by its original name."""
    root = tmp_path / "pipelines"
    d = root / "demo_dbt"
    (d / "models").mkdir(parents=True)
    (d / "models" / "stg_orders.sql").write_text(
        "{{ config(materialized='table', schema='demo_dbt') }}\nselect 1 as id")
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "demo_dbt", "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/o.csv"}},
        "warehouse": {"host": "h", "database": "d", "schema": "demo_dbt"},
        "stages": [
            {"name": "landing", "gates": [{"type": "row_count_bounds", "table": "o", "min": 1}]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing", "models": ["stg_orders"]},
        ],
    }, sort_keys=False))
    pipeline = loader.load("demo_dbt", root)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")

    model_file = (tmp_path / "clones" / clone.name / "models"
                 / f"stg_orders__validate_{suffix}.sql")
    content = model_file.read_text()
    assert content.startswith("{{ config(alias='stg_orders') }}\n")
    assert "select 1 as id" in content   # the model's own SQL body, untouched


def test_alias_model_content_only_prepends_never_rewrites_the_body():
    content = fixture._alias_model_content("select 1 as id\n", "stg_orders")
    assert content == "{{ config(alias='stg_orders') }}\nselect 1 as id\n"


def test_make_validation_clone_uses_the_original_pipelines_landing_dataset_name(tmp_path):
    """M2.4.3 review: every real model in this project reads landing by a
    literal, schema-qualified name baked into its own copied SQL - the
    clone's own dlt extract must land data under that same name, not the
    clone's differently-named one, or the model finds nothing."""
    pipeline = _real_pipeline(tmp_path)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    assert clone.landing_dataset_name == f"{pipeline.name}_landing"
    assert clone.landing_dataset_name != f"{clone.name}_landing"

    from dpagent.pipelines import extract as extract_mod
    assert extract_mod.landing_dataset(clone) == clone.landing_dataset_name

    reloaded = loader.load(clone.name, tmp_path / "clones")
    assert reloaded.landing_dataset_name == clone.landing_dataset_name


def test_make_validation_clone_manifest_round_trips_through_loader(tmp_path):
    """What deploy.install_pipeline_files() actually publishes and what a
    DAG task later reloads via loader.load(clone_name) must be this exact
    file - a real round trip, not just an in-memory Pipeline object."""
    pipeline = _real_pipeline(tmp_path)
    clones_dir = tmp_path / "clones"
    clone, suffix = fixture.make_validation_clone(pipeline, clones_dir)
    reloaded = loader.load(clone.name, clones_dir)
    assert reloaded.name == clone.name
    assert reloaded.source.connection == clone.source.connection
    assert reloaded.warehouse.host == clone.warehouse.host
    assert reloaded.maturity == "draft"
    assert reloaded.schedule is None


def test_make_validation_clone_copies_procedure_files_verbatim(tmp_path):
    root = tmp_path / "pipelines"
    d = root / "proc_pipe"
    (d / "procedures").mkdir(parents=True)
    (d / "procedures" / "convert.sql").write_text("CREATE OR REPLACE PROCEDURE convert() ...")
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "proc_pipe", "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/o.csv"}},
        "warehouse": {"host": "h", "database": "d", "schema": "proc_pipe"},
        "stages": [
            {"name": "landing", "gates": [{"type": "row_count_bounds", "table": "o", "min": 1}]},
            {"name": "curated", "engine": "procedure", "depends_on": "landing",
             "procedure": "procedures/convert.sql"},
        ],
    }, sort_keys=False))
    pipeline = loader.load("proc_pipe", root)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    curated = next(s for s in clone.stages if s.name == "curated")
    assert curated.procedure == "procedures/convert.sql"   # not renamed
    copied = (tmp_path / "clones" / clone.name / "procedures" / "convert.sql")
    assert copied.exists()
    assert "CREATE OR REPLACE PROCEDURE convert" in copied.read_text()


# ---------------------------------------------------------------- reporting helpers

def test_hash_file_is_stable_and_content_sensitive(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("a: 1\n")
    h1 = fixture.hash_file(path)
    assert h1.startswith("sha256:")
    assert fixture.hash_file(path) == h1
    path.write_text("a: 2\n")
    assert fixture.hash_file(path) != h1


def test_fixture_report_dict_shape_on_a_full_pass():
    report = fixture.FixtureRunReport(
        clone_name="demo__validate__abc", seeded=True, deployed=True,
        run_ids=[401, 402], run1_status="ok", run2_status="ok",
        comparison_after_run1=fixture.ComparisonResult(True, "1 row matched"),
        comparison_after_run2=fixture.ComparisonResult(True, "1 row matched"),
        cleanup_attempted=True, cleanup_ok=True, cleanup_detail="DAG removed",
        source_db_created=True, source_database_dropped=True, source_role_dropped=True,
        warehouse_db_created=True, warehouse_database_dropped=True, warehouse_role_dropped=True,
    )
    data = fixture.fixture_report_dict(
        report, pipeline_hash="sha256:p", fixture_hash="sha256:f", expected_hash="sha256:e")
    assert data["pipeline_hash"] == "sha256:p"
    assert data["fixture_hash"] == "sha256:f"
    assert data["expected_hash"] == "sha256:e"
    assert data["run_ids"] == [401, 402]
    assert data["comparison"] == {"run_1": "pass", "run_2": "pass", "idempotent": True}
    assert data["cleanup"]["pipeline_artifacts"] == "pass"
    assert data["cleanup"]["source_database"] == "pass"
    assert data["cleanup"]["source_role"] == "pass"
    assert data["cleanup"]["warehouse_database"] == "pass"
    assert data["cleanup"]["warehouse_role"] == "pass"
    assert data["cleanup"]["overall"] == "pass"
    assert data["overall"] == "pass"


def test_fixture_report_dict_marks_unavailable_distinctly_from_fail():
    report = fixture.FixtureRunReport(unavailable_reason="no sudo to postgres")
    data = fixture.fixture_report_dict(report)
    assert data["overall"] == "unavailable"
    assert data["unavailable_reason"] == "no sudo to postgres"


def test_fixture_report_dict_marks_fail_when_cleanup_failed_despite_a_match():
    report = fixture.FixtureRunReport(
        seeded=True, deployed=True, run1_status="ok", run2_status="ok",
        comparison_after_run1=fixture.ComparisonResult(True), comparison_after_run2=fixture.ComparisonResult(True),
        cleanup_attempted=True, cleanup_ok=False, cleanup_detail="undeploy failed: needs root",
    )
    data = fixture.fixture_report_dict(report)
    assert data["cleanup"]["pipeline_artifacts"] == "fail"
    assert data["cleanup"]["overall"] == "fail"
    assert data["overall"] == "fail"   # report.ok is False because cleanup failed


def test_fixture_report_dict_reports_each_throwaway_resource_separately():
    """M2.4.2's own required shape: pipeline_artifacts, source_database,
    source_role, warehouse_database, warehouse_role, each independently -
    not folded into one combined "cleanup passed/failed" boolean."""
    report = fixture.FixtureRunReport(
        seeded=True, deployed=True, run1_status="ok", run2_status="ok",
        comparison_after_run1=fixture.ComparisonResult(True), comparison_after_run2=fixture.ComparisonResult(True),
        cleanup_attempted=True, cleanup_ok=True, cleanup_detail="DAG removed",
        source_db_created=True, source_database_dropped=True, source_role_dropped=True,
        warehouse_db_created=True, warehouse_database_dropped=False,
        warehouse_database_drop_error="permission denied", warehouse_role_dropped=True,
    )
    data = fixture.fixture_report_dict(report)
    assert data["cleanup"]["source_database"] == "pass"
    assert data["cleanup"]["source_role"] == "pass"
    assert "permission denied" in data["cleanup"]["warehouse_database"]
    assert data["cleanup"]["warehouse_role"] == "pass"
    assert data["cleanup"]["overall"] == "fail"   # one of the four still failed
    assert data["overall"] == "fail"


def test_fixture_report_dict_cleanup_overall_fails_on_a_seed_error_with_a_bad_drop(tmp_path):
    """M2.4.4 review's own P1, reproduced: a seed failure means
    `cleanup_attempted` (the *pipeline clone's* own cleanup) stays False -
    deploy() is never even reached - but both throwaway databases are still
    created and torn down regardless. An earlier version computed
    `cleanup.overall` from `cleanup_attempted` alone, so a seed error whose
    throwaway database then genuinely failed to drop still reported
    "overall": "not_attempted" - silently hiding a real, already-known
    failure sitting right next to it in the same dict
    ("source_database": "fail: ...")."""
    report = fixture.FixtureRunReport(
        seeded=False, seed_error="CREATE TABLE orders failed: syntax error",
        source_db_created=True, source_database_dropped=False,
        source_database_drop_error="database is in use", source_role_dropped=True,
        warehouse_db_created=True, warehouse_database_dropped=True, warehouse_role_dropped=True,
    )
    data = fixture.fixture_report_dict(report)
    assert data["cleanup"]["pipeline_artifacts"] == "not_attempted"   # deploy() never reached
    assert "fail" in data["cleanup"]["source_database"]
    assert data["cleanup"]["warehouse_database"] == "pass"
    assert data["cleanup"]["overall"] == "fail"   # not "not_attempted" - a real resource failed


def test_fixture_report_dict_cleanup_overall_is_not_attempted_when_truly_nothing_ran():
    """The other side of the same fix: genuinely nothing touched (e.g. a
    preflight failure, before even the throwaway databases exist) must
    still report "not_attempted", not "fail"."""
    report = fixture.FixtureRunReport(unavailable_reason="preflight failed: not root")
    data = fixture.fixture_report_dict(report)
    assert data["cleanup"]["overall"] == "not_attempted"


def test_fixture_report_dict_cleanup_overall_passes_when_only_throwaway_dbs_were_touched():
    """A seed failure whose throwaway databases *both* tore down cleanly
    must report "pass" for cleanup.overall, even though cleanup_attempted
    (the pipeline clone's own, never reached here) stays False."""
    report = fixture.FixtureRunReport(
        seeded=False, seed_error="bad fixture",
        source_db_created=True, source_database_dropped=True, source_role_dropped=True,
        warehouse_db_created=True, warehouse_database_dropped=True, warehouse_role_dropped=True,
    )
    data = fixture.fixture_report_dict(report)
    assert data["cleanup"]["pipeline_artifacts"] == "not_attempted"
    assert data["cleanup"]["overall"] == "pass"


def test_gate_summary_for_run_reads_the_real_journal(isolated_db):
    from dpagent.engine import state
    run_id = state.start_run("data", "demo__validate__x")
    stage_id = state.start_stage(run_id, "demo__validate__x", "landing")
    state.finish_stage(stage_id, "passed", row_count=3)
    state.record_gate(stage_id, "row_count_bounds", "passed", detail="3 rows, within bounds")
    summary = fixture.gate_summary_for_run(run_id)
    assert summary == {"landing": [
        {"type": "row_count_bounds", "status": "passed", "detail": "3 rows, within bounds",
         "rows_checked": None, "rows_rejected": None},
    ]}


def test_gate_summary_for_run_keeps_two_gates_of_the_same_type_separate(isolated_db):
    """The exact case M2.4.2's own review flagged: a dict keyed by
    gate_type alone would silently overwrite one business_rule gate's
    result with the other's."""
    from dpagent.engine import state
    run_id = state.start_run("data", "demo__validate__x")
    stage_id = state.start_stage(run_id, "demo__validate__x", "curated")
    state.finish_stage(stage_id, "passed", row_count=5)
    state.record_gate(stage_id, "business_rule", "passed", rows_checked=5, rows_rejected=0,
                      detail="rule A")
    state.record_gate(stage_id, "business_rule", "failed", rows_checked=5, rows_rejected=1,
                      detail="rule B")
    summary = fixture.gate_summary_for_run(run_id)
    assert len(summary["curated"]) == 2
    types_and_status = [(g["type"], g["status"], g["detail"]) for g in summary["curated"]]
    assert ("business_rule", "passed", "rule A") in types_and_status
    assert ("business_rule", "failed", "rule B") in types_and_status
