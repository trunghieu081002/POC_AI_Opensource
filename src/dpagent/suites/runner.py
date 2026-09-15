"""Execute an acceptance suite: setup -> checks -> teardown (always).

Rules that make the result trustworthy:

  * **exit 0 means the system behaved correctly**, not merely that a command ran.
    A negative check asserts internally that the bad thing was refused, so there
    is exactly one success condition to reason about.
  * **teardown always runs**, including after a failure or a crash in the middle
    of the checks. A test that leaves fixtures behind poisons the next run.
  * **setup failure is `error`, not `failed`** — the difference between "the
    system is broken" and "we could not find out", which the operator must not
    have to guess at.
  * a `critical` failure stops the remaining checks, because everything after it
    would be reporting on a system already known to be broken.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..engine import executor, params as params_mod, state
from . import loader

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"


@dataclass
class CheckOutcome:
    id: str
    status: str
    description: str = ""
    critical: bool = False
    negative: bool = False
    rc: int | None = None
    duration_ms: int = 0
    output: str = ""


@dataclass
class SuiteOutcome:
    suite: str
    status: str
    checks: list[CheckOutcome] = field(default_factory=list)
    reason: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == FAILED)

    @property
    def ok(self) -> bool:
        return self.status == PASSED


class Reporter:
    """Rendering hooks; the CLI subclasses this."""

    def suite_start(self, suite: loader.Suite) -> None: ...
    def phase(self, name: str) -> None: ...
    def check_done(self, outcome: CheckOutcome) -> None: ...
    def suite_done(self, outcome: SuiteOutcome) -> None: ...


def run_suite(suite: loader.Suite, os_info, resolved_params: dict, run_id: int,
              reporter: Reporter | None = None,
              packs_installed: dict[str, str] | None = None) -> SuiteOutcome:
    reporter = reporter or Reporter()
    reporter.suite_start(suite)

    env = executor.build_env(os_info.as_env(), resolved_params, dry_run=False, extra={
        "DP_SUITE": suite.name,
        "DP_SUITE_ROOT": str(suite.root),
        # A dedicated namespace for fixtures. Checks must confine themselves to
        # it: an acceptance test that writes into a real database is a liability.
        # Suffixed with run_id (unique per invocation) rather than a fixed
        # string - two suite runs against the same pack at once (a genuine
        # install race, or just two operators/CI jobs both running `dpagent
        # test` against the same host) used to collide on the exact same
        # database/dag_id/filename and fail with a confusing "already exists"
        # that had nothing to do with either run's actual health.
        "DP_TEST_NS": f"dpagent_selftest_{run_id}",
    })
    log_run = f"run-{run_id}"

    suite_row = state.start_suite(run_id, suite.name)
    outcome = SuiteOutcome(suite.name, PASSED)

    def execute(check: loader.Check, phase: str) -> CheckOutcome:
        result = executor.run_script(suite.path(check.script), env, log_run,
                                     timeout=check.timeout, cwd=suite.root)
        status = PASSED if result.ok else FAILED
        item = CheckOutcome(
            id=check.id, status=status, description=check.description,
            critical=check.critical, negative=check.negative, rc=result.rc,
            duration_ms=result.duration_ms,
            output=params_mod.redact(result.output),
        )
        state.record_check(suite_row, check.id, check.description, status,
                           critical=check.critical, rc=result.rc,
                           duration_ms=result.duration_ms, phase=phase)
        state.event(f"suite.{phase}", f"{suite.name}/{check.id}: {status}",
                    run_id=run_id, level="info" if result.ok else "error",
                    actor=f"suite:{suite.name}",
                    data={"check": check.id, "rc": result.rc, "phase": phase})
        return item

    try:
        if suite.setup:
            reporter.phase("setup")
            for check in suite.setup:
                item = execute(check, "setup")
                if item.status != PASSED:
                    reporter.check_done(item)
                    outcome.status = ERROR
                    outcome.reason = (
                        f"setup step {check.id!r} failed — the suite could not "
                        f"establish its fixtures, so nothing was proven either way")
                    outcome.checks.append(item)
                    return outcome
                reporter.check_done(item)

        reporter.phase("checks")
        halted = False
        for check in suite.checks:
            if halted:
                item = CheckOutcome(check.id, SKIPPED, check.description,
                                    check.critical, check.negative,
                                    output="not run: a critical check already failed")
                state.record_check(suite_row, check.id, check.description, SKIPPED,
                                   critical=check.critical, phase="check")
                outcome.checks.append(item)
                reporter.check_done(item)
                continue

            item = execute(check, "check")
            outcome.checks.append(item)
            reporter.check_done(item)

            if item.status != PASSED:
                outcome.status = FAILED
                if check.critical:
                    halted = True
                    outcome.reason = (
                        f"critical check {check.id!r} failed; remaining checks "
                        f"skipped because they would only describe a system "
                        f"already known to be broken")

        if outcome.status == FAILED and not outcome.reason:
            outcome.reason = f"{outcome.failed} of {len(outcome.checks)} checks failed"

    finally:
        # Always. A suite that leaves fixtures behind breaks the next run.
        if suite.teardown:
            reporter.phase("teardown")
            for check in suite.teardown:
                item = execute(check, "teardown")
                reporter.check_done(item)
                if item.status != PASSED:
                    state.event("suite.teardown_failed",
                                f"{suite.name}/{check.id} left fixtures behind",
                                run_id=run_id, level="warn",
                                actor=f"suite:{suite.name}")

        state.finish_suite(suite_row, outcome.status, len(outcome.checks),
                           outcome.passed, outcome.failed)

    reporter.suite_done(outcome)
    return outcome
