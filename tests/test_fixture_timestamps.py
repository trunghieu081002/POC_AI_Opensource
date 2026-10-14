"""Timestamp semantic comparison must not mask actual value/offset changes."""
import json
import subprocess
import pytest
from dpagent.pipelines import fixture


def compare(monkeypatch, kind, expected, actual):
    monkeypatch.setattr(fixture, "_column_types", lambda *a: {"stamp": kind})
    monkeypatch.setattr(fixture, "_psql", lambda *a: subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"stamp": actual}) + "\n", stderr=""))
    exp = fixture.ExpectedResult(table="example", rows=[{"stamp": expected}])
    return fixture.compare_curated(exp, None, "example")


@pytest.mark.parametrize("expected,actual", [
    ("2026-01-10 08:00:00+00", "2026-01-10T08:00:00+00:00"),
    ("2026-01-10T15:00:00+07:00", "2026-01-10T08:00:00Z"),
    ("2026-01-10T08:00:00.000000+00:00", "2026-01-10T08:00:00+00:00")])
def test_equivalent_explicit_timestamptz_instants_match(monkeypatch, expected, actual):
    assert compare(monkeypatch, "timestamp with time zone", expected, actual).ok


def test_same_wall_time_different_offset_is_not_the_same_instant(monkeypatch):
    assert not compare(monkeypatch, "timestamp with time zone",
                       "2026-01-10T08:00:00+07:00", "2026-01-10T08:00:00+00:00").ok


def test_naive_timestamptz_does_not_silently_assume_utc(monkeypatch):
    result = compare(monkeypatch, "timestamp with time zone",
                     "2026-01-10T08:00:00", "2026-01-10T08:00:00+00:00")
    assert not result.ok and "explicit timezone offset" in result.detail


def test_text_date_like_identifiers_remain_exact(monkeypatch):
    assert not compare(monkeypatch, "text", "2026-01-10 08:00:00+00",
                       "2026-01-10T08:00:00+00:00").ok


def test_timestamp_without_zone_compares_wall_clock_fields(monkeypatch):
    assert compare(monkeypatch, "timestamp without time zone",
                   "2026-01-10 08:00:00.000000", "2026-01-10T08:00:00").ok
    assert not compare(monkeypatch, "timestamp without time zone",
                       "2026-01-10 08:00:00", "2026-01-10T09:00:00").ok


def test_excess_nonzero_fractional_precision_cannot_be_truncated_into_a_match(monkeypatch):
    result = compare(monkeypatch, "timestamp with time zone",
                     "2026-01-10T08:00:00.1234567Z", "2026-01-10T08:00:00.123456Z")
    assert not result.ok and "precision" in result.detail


def test_nullable_timestamp_is_preserved(monkeypatch):
    assert compare(monkeypatch, "timestamp with time zone", None, None).ok
    assert not compare(monkeypatch, "timestamp with time zone", "", None).ok


def test_date_only_wall_clock_is_not_mistaken_for_a_timezone(monkeypatch):
    assert compare(monkeypatch, "timestamp without time zone",
                   "2026-01-10", "2026-01-10T00:00:00").ok


def test_compact_explicit_offset_is_supported_on_python_310(monkeypatch):
    assert compare(monkeypatch, "timestamp with time zone",
                   "2026-01-10T15:00:00+0700", "2026-01-10T08:00:00+00:00").ok
