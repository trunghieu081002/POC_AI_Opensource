"""The M2.5 acceptance suite's own ledger of real runs - inspired by the
SourceRegistry pattern in phulee9/hgmedia (one batch_id per extract, with a
watermark/checksum recorded alongside it, so a later run can tell "have I
already proven this exact thing, and when" rather than re-deriving it from
scratch or - worse - trusting a stale claim).

Deliberately NOT `dpagent`'s own SQLite journal (`engine/state.py`): that
table is the product's own install/run/gate history, on the target host
dpagent manages. This registry is the opposite thing - a record kept
*about* the test harness's own runs, on whatever disposable host happened
to run them, committed to *this repository* so the evidence survives after
that host is destroyed. Confusing the two would blur "did the pipeline
run successfully" with "did we successfully prove the harness works",
which is exactly the distinction M2 has insisted on from the start
(docs/testing.md: "the installer finished" and "the system works" are
different claims - same idea, one level up).

One JSON object per line (JSONL - append-only, `git diff`-friendly, no
database to migrate). Each line is one *batch*: one scenario, run once,
on one host, at one commit. Fields:

  batch_id       - uuid4, generated here, never reused
  recorded_at    - UTC ISO-8601, when this batch was appended
  commit         - `git rev-parse HEAD` at the time the driver ran
  host           - a free-text label for *what* ran it (never a
                   resolvable address - see `redact_report`), e.g.
                   "docker:dpagent-ubuntu-systemd" or "vm:ubuntu-virtualbox"
  scenario       - the acceptance-matrix row id, e.g. "literal-connection"
  pipeline_hash / fixture_hash / expected_hash - same sha256:<hex> shape
                   `fixture.hash_file()` already produces; this is the
                   "watermark/checksum" half of the pattern - two batches
                   with identical hashes for the same scenario are, by
                   definition, proving the exact same input, which is
                   what `latest_matching` below is for.
  verdict        - "pass" | "fail" | "unavailable" | "timeout"
  cleanup_ok     - bool | None (None = cleanup never attempted)
  report_path    - path to the redacted report this batch's own evidence
                   lives in, relative to the repo root (see redact_report)

Nothing here is a promise about the *current* state of any host - a batch
is a historical fact ("this combination was run, on this date, with this
result"), read the same way `docs/deploy-log.md`'s own dated entries are:
true when written, not re-verified on every read.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY_PATH = Path(__file__).parent / "registry.jsonl"


@dataclass
class Batch:
    scenario: str
    commit: str
    host: str
    pipeline_hash: str = ""
    fixture_hash: str = ""
    expected_hash: str = ""
    verdict: str = ""          # "pass" | "fail" | "unavailable" | "timeout"
    cleanup_ok: bool | None = None
    report_path: str = ""
    batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())


def append_batch(batch: Batch, *, registry_path: Path = DEFAULT_REGISTRY_PATH) -> None:
    """Appends one batch - never rewrites a previous line. A batch, once
    recorded, is history; correcting a mistaken entry means appending a
    new one, same as `engine/state.py`'s own audit table never updates or
    deletes."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with registry_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(batch), sort_keys=True))
        f.write("\n")


def read_all(*, registry_path: Path = DEFAULT_REGISTRY_PATH) -> list[dict]:
    if not registry_path.exists():
        return []
    batches = []
    with registry_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                batches.append(json.loads(line))
    return batches


def latest_matching(scenario: str, *, pipeline_hash: str, fixture_hash: str,
                    expected_hash: str,
                    registry_path: Path = DEFAULT_REGISTRY_PATH) -> dict | None:
    """The most recent batch that proves *this exact* scenario against
    *this exact* pipeline/fixture/expected combination still holds - the
    idempotent-check half of the pattern. `None` means either this
    scenario was never run against these hashes, or (just as likely) one
    of the three files changed since the last run that did match - either
    way, the caller's answer is the same: this has not been proven against
    what exists right now, go run it rather than trust an old entry whose
    inputs have since moved. This is also what Step 4's planned promote()
    gate would call, to decide whether a validation report is still fresh
    enough to promote against, instead of re-deriving that from
    `.synth-validation.yaml`'s mtime alone."""
    match = None
    for row in read_all(registry_path=registry_path):
        if (row.get("scenario") == scenario
                and row.get("pipeline_hash") == pipeline_hash
                and row.get("fixture_hash") == fixture_hash
                and row.get("expected_hash") == expected_hash):
            if match is None or row["recorded_at"] > match["recorded_at"]:
                match = row
    return match


# ------------------------------------------------------------ redaction

# Matches credential-shaped values dpagent itself already treats as
# `secret: true` params, plus the generic "a password/token appeared in
# an error string" case subprocess stderr sometimes produces (e.g. a
# dbt/psql connection-refused message that echoes its own DSN). Over-redact
# rather than under-redact - a report with one too many [REDACTED] markers
# is still useful evidence; one real password committed to git history is
# not recoverable by deleting the file afterward.
_SECRET_PATTERNS = [
    re.compile(r"(password['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+", re.IGNORECASE),
    re.compile(r"(PASSWORD['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"),
    re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)"),   # user:password@host DSNs
]


def _redact_text(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]" + (m.group(2) if m.lastindex and m.lastindex >= 2 else ""), text)
    return text


def redact_report(data: Any) -> Any:
    """Walks a `fixture_report_dict()`-shaped structure (or any JSON-ish
    dict/list/str) and redacts anything password-shaped, recursively. Does
    NOT redact host/database/user names or table/column contents - those
    are exactly what a reviewer needs to confirm isolation (the M2.5
    "control database unchanged" check), and this project's own convention
    (params.py's `secret: true`) has always been about credentials
    specifically, never about connection topology."""
    if isinstance(data, str):
        return _redact_text(data)
    if isinstance(data, list):
        return [redact_report(item) for item in data]
    if isinstance(data, dict):
        return {k: redact_report(v) for k, v in data.items()}
    return data
