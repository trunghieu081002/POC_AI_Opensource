"""Execute a resolved plan: preflight -> steps -> verify, with catalog-driven repair.

The step lifecycle is the whole design in one function (`_run_step`):

    when?  -> not applicable, skip
    guard? -> already satisfied, skip
    run    -> ok, checkpoint
           -> fail -> catalog hit + autofix -> repair, retry   (deterministic, free)
                   -> catalog hit + ask     -> stop, ask the operator
                   -> catalog miss          -> stop, queue an entry for review
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import errors as errors_mod
from . import executor, params as params_mod, safety, state
from ..library import loader as packs

OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"
BLOCKED = "blocked"
NEEDS_USER = "needs_user"
NEEDS_REVIEW = "needs_review"


@dataclass
class StepOutcome:
    step_id: str
    status: str
    rc: int | None = None
    duration_ms: int = 0
    reason: str = ""
    fixes_applied: list[str] = field(default_factory=list)
    match: errors_mod.Match | None = None
    output: str = ""


@dataclass
class PackOutcome:
    pack: str
    status: str
    steps: list[StepOutcome] = field(default_factory=list)
    verify_ok: bool | None = None
    reason: str = ""


@dataclass
class RunOutcome:
    run_id: int
    status: str
    packs: list[PackOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK


class Reporter:
    """Rendering hooks. The CLI subclasses this; the engine stays print-free."""

    def pack_start(self, pack: packs.Pack, resolved: dict) -> None: ...
    def pack_done(self, outcome: PackOutcome) -> None: ...
    def pack_skipped(self, pack: packs.Pack, reason: str) -> None: ...
    def preflight(self, pack: packs.Pack, result: executor.Result) -> None: ...
    def step_start(self, pack: packs.Pack, step: packs.Step, index: int, total: int) -> None: ...
    def step_done(self, outcome: StepOutcome) -> None: ...
    def fixing(self, match: errors_mod.Match, attempt: int) -> None: ...
    def fix_blocked(self, command: str, verdict: safety.Verdict) -> None: ...
    def unmatched(self, pack: packs.Pack, step: packs.Step, result: executor.Result) -> None: ...
    def verify(self, pack: packs.Pack, result: executor.Result) -> None: ...


class Engine:
    def __init__(self, os_info, run_id: int, *, dry_run: bool = False,
                 force: bool = False, auto_fix: bool = True,
                 reporter: Reporter | None = None,
                 diagnose: Callable[..., dict | None] | None = None):
        self.os_info = os_info
        self.run_id = run_id
        self.dry_run = dry_run
        self.force = force
        self.auto_fix = auto_fix
        self.reporter = reporter or Reporter()
        self.diagnose = diagnose          # llm hook, only called on a catalog miss
        self.log_run_id = f"run-{run_id}"

    # ------------------------------------------------------------ helpers

    def _env(self, resolved: dict, extra: dict | None = None) -> dict:
        return executor.build_env(self.os_info.as_env(), resolved, self.dry_run, extra)

    def _shell_check(self, snippet: str, env: dict, cwd: Path) -> bool:
        """Run a guard/when snippet. True == exited 0."""
        if not snippet:
            return False
        result = executor.run_command(snippet, env, self.log_run_id, timeout=60,
                                      dry_run=False)
        return result.ok

    # ------------------------------------------------------------ steps

    def _run_step(self, pack: packs.Pack, step: packs.Step, index: int, total: int,
                  env: dict, catalog: errors_mod.Catalog, done_ids: set[str],
                  phash: str) -> StepOutcome:
        self.reporter.step_start(pack, step, index, total)

        if step.id in done_ids and not self.force:
            outcome = StepOutcome(step.id, SKIPPED, reason="checkpointed on a previous run")
            state.event("step.skip", f"{pack.name}/{step.id}: already checkpointed",
                        run_id=self.run_id, data={"pack": pack.name, "step": step.id})
            self.reporter.step_done(outcome)
            return outcome

        if step.when and not self._shell_check(step.when, env, pack.root):
            outcome = StepOutcome(step.id, SKIPPED, reason="`when` condition not met")
            state.event("step.skip", f"{pack.name}/{step.id}: when-condition false",
                        run_id=self.run_id, data={"pack": pack.name, "step": step.id})
            self.reporter.step_done(outcome)
            return outcome

        if step.guard and not self.force and self._shell_check(step.guard, env, pack.root):
            outcome = StepOutcome(step.id, SKIPPED, reason="guard says already applied")
            state.event("guard.skip", f"{pack.name}/{step.id}: guard satisfied",
                        run_id=self.run_id, data={"pack": pack.name, "step": step.id})
            self.reporter.step_done(outcome)
            return outcome

        script = pack.path(step.script)
        attempt = 1
        fixes_applied: list[str] = []

        while True:
            step_row = state.start_step(self.run_id, pack.name, step.id, step.script,
                                        params_hash_=phash, attempt=attempt)
            state.event("step.start", f"{pack.name}/{step.id} (attempt {attempt})",
                        run_id=self.run_id,
                        data={"pack": pack.name, "step": step.id, "attempt": attempt})

            result = executor.run_script(script, env, self.log_run_id,
                                         timeout=step.timeout, cwd=pack.root)

            if result.ok:
                state.finish_step(step_row, OK, rc=0, duration_ms=result.duration_ms,
                                  log_path=result.log_path)
                state.event("step.ok", f"{pack.name}/{step.id} ok in {result.duration_ms}ms",
                            run_id=self.run_id,
                            data={"pack": pack.name, "step": step.id,
                                  "duration_ms": result.duration_ms,
                                  "fixes_applied": fixes_applied})
                outcome = StepOutcome(step.id, OK, rc=0,
                                      duration_ms=result.duration_ms,
                                      fixes_applied=fixes_applied,
                                      output=result.output)
                self.reporter.step_done(outcome)
                return outcome

            state.finish_step(step_row, FAILED, rc=result.rc,
                              duration_ms=result.duration_ms, log_path=result.log_path)
            state.event("step.fail", f"{pack.name}/{step.id} rc={result.rc}",
                        run_id=self.run_id, level="error",
                        data={"pack": pack.name, "step": step.id, "rc": result.rc,
                              "stderr": params_mod.redact(result.stderr[-2000:])})

            match = catalog.match(result.output, rc=result.rc, step_id=step.id)

            if match is None:
                state.record_error_hit(pack.name, self.run_id, step_row, None, None, False)
                self.reporter.unmatched(pack, step, result)
                proposal = None
                if self.diagnose is not None:
                    proposal = self.diagnose(pack=pack, step=step, result=result,
                                             os_info=self.os_info)
                reason = ("drafted a new catalog entry for review" if proposal
                          else "no catalog entry matched this failure")
                return StepOutcome(step.id, NEEDS_REVIEW, rc=result.rc,
                                   duration_ms=result.duration_ms,
                                   fixes_applied=fixes_applied, reason=reason,
                                   output=result.output)

            entry = match.entry
            state.record_error_hit(pack.name, self.run_id, step_row, entry.id,
                                   entry.autofix, resolved=False)
            state.event("error.matched",
                        f"{pack.name}/{step.id} matched {entry.id}: {entry.cause}",
                        run_id=self.run_id, level="warn",
                        data={"pack": pack.name, "step": step.id, "error_id": entry.id,
                              "source": entry.source, "excerpt": match.excerpt})

            if not entry.automatic or not self.auto_fix:
                reason = entry.ask_user or entry.cause or "needs an operator decision"
                return StepOutcome(step.id, NEEDS_USER, rc=result.rc,
                                   duration_ms=result.duration_ms,
                                   fixes_applied=fixes_applied, reason=reason,
                                   match=match, output=result.output)

            if attempt >= entry.max_attempts:
                return StepOutcome(step.id, FAILED, rc=result.rc,
                                   duration_ms=result.duration_ms,
                                   fixes_applied=fixes_applied,
                                   reason=f"{entry.id}: still failing after "
                                          f"{attempt} attempts",
                                   match=match, output=result.output)

            self.reporter.fixing(match, attempt)
            for command in entry.autofix:
                verdict = safety.check(command)
                if not verdict.allowed:
                    state.event("fix.blocked", f"{entry.id}: {verdict.reason}",
                                run_id=self.run_id, level="error",
                                data={"cmd": command, "rule": verdict.rule_id})
                    self.reporter.fix_blocked(command, verdict)
                    return StepOutcome(step.id, BLOCKED, rc=result.rc,
                                       fixes_applied=fixes_applied,
                                       reason=f"autofix blocked by safety rule "
                                              f"{verdict.rule_id}",
                                       match=match, output=result.output)
                state.event("fix.run", command, run_id=self.run_id,
                            data={"error_id": entry.id})
                executor.run_command(command, env, self.log_run_id,
                                     dry_run=self.dry_run)
                fixes_applied.append(command)

            attempt += 1

    # ------------------------------------------------------------ packs

    def install_pack(self, pack: packs.Pack, supplied: dict) -> PackOutcome:
        resolved = params_mod.resolve(pack.param_schema, supplied, pack=pack.name)
        audit_params = params_mod.for_audit(pack.param_schema, resolved)
        phash = state.params_hash(pack.version, audit_params)

        existing = state.get_install(pack.name)
        if (existing and existing["status"] == "installed"
                and existing["params_hash"] == phash and not self.force
                and not self.dry_run):
            self.reporter.pack_skipped(
                pack, f"already installed at v{existing['pack_version']} "
                      f"with identical params")
            state.event("pack.noop", f"{pack.name} already at the requested config",
                        run_id=self.run_id, data={"pack": pack.name, "hash": phash})
            return PackOutcome(pack.name, SKIPPED, reason="already installed")

        self.reporter.pack_start(pack, audit_params)
        state.event("pack.start", f"installing {pack.name} v{pack.version}",
                    run_id=self.run_id,
                    data={"pack": pack.name, "version": pack.version,
                          "maturity": pack.maturity, "params": audit_params})

        env = self._env(resolved, {"DP_PACK": pack.name, "DP_PACK_ROOT": str(pack.root)})
        catalog = errors_mod.Catalog.for_pack(pack.root, pack.errors_file)
        outcome = PackOutcome(pack.name, OK)

        if pack.preflight:
            result = executor.run_script(pack.path(pack.preflight), env,
                                         self.log_run_id, timeout=180, cwd=pack.root)
            self.reporter.preflight(pack, result)
            state.event("preflight", f"{pack.name} preflight rc={result.rc}",
                        run_id=self.run_id,
                        level="info" if result.ok else "error",
                        data={"pack": pack.name, "rc": result.rc,
                              "output": params_mod.redact(result.output[-4000:])})
            if not result.ok:
                outcome.status = FAILED
                outcome.reason = "preflight failed — fix the host, then re-run"
                state.record_install(pack.name, pack.version, audit_params, phash,
                                     self.os_info.family, "failed", self.run_id)
                self.reporter.pack_done(outcome)
                return outcome

        done_ids = set() if self.force else state.completed_steps(pack.name, phash)

        for index, step in enumerate(pack.steps, start=1):
            step_outcome = self._run_step(pack, step, index, len(pack.steps),
                                          env, catalog, done_ids, phash)
            outcome.steps.append(step_outcome)

            if step_outcome.status in (OK, SKIPPED):
                continue
            if step.optional:
                state.event("step.optional_fail",
                            f"{pack.name}/{step.id} failed but is optional",
                            run_id=self.run_id, level="warn")
                continue

            outcome.status = step_outcome.status
            outcome.reason = step_outcome.reason
            state.record_install(pack.name, pack.version, audit_params, phash,
                                 self.os_info.family, "failed", self.run_id)
            self.reporter.pack_done(outcome)
            return outcome

        if pack.verify and not self.dry_run:
            result = executor.run_script(pack.path(pack.verify), env, self.log_run_id,
                                         timeout=180, cwd=pack.root)
            outcome.verify_ok = result.ok
            self.reporter.verify(pack, result)
            state.event("verify", f"{pack.name} verify rc={result.rc}",
                        run_id=self.run_id, level="info" if result.ok else "error",
                        data={"pack": pack.name, "rc": result.rc,
                              "output": params_mod.redact(result.output[-4000:])})
            if not result.ok:
                outcome.status = FAILED
                outcome.reason = "steps completed but verify failed"
                state.record_install(pack.name, pack.version, audit_params, phash,
                                     self.os_info.family, "failed", self.run_id)
                self.reporter.pack_done(outcome)
                return outcome

        if not self.dry_run:
            state.record_install(pack.name, pack.version, audit_params, phash,
                                 self.os_info.family, "installed", self.run_id)
        state.event("pack.ok", f"{pack.name} installed", run_id=self.run_id,
                    data={"pack": pack.name, "version": pack.version})
        self.reporter.pack_done(outcome)
        return outcome

    def install(self, resolution) -> RunOutcome:
        run = RunOutcome(self.run_id, OK)
        for pack in resolution.order:
            supplied = resolution.requests[pack.name].params
            outcome = self.install_pack(pack, supplied)
            run.packs.append(outcome)
            if outcome.status not in (OK, SKIPPED):
                run.status = outcome.status
                break
        state.finish_run(self.run_id, run.status)
        return run
