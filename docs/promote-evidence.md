# Promote requires validation evidence (A2)

`dpagent pipeline promote` used to hash the files and flip `maturity: reviewed`
on a human's say-so. Since A2 it is a gate: **a pipeline can only be approved on
the strength of a real fixture validation of exactly that content**, and the
check lives in the shared function `approval.promote()` — Python callers and the
CLI go through the same door, and no other public function in the product writes
`.approved.yaml` (the internal writer is private and `promote` is its only caller).

```
dpagent pipeline validate NAME --fixture F --expected E     # runs for real, seals evidence
dpagent pipeline promote  NAME --fixture F --expected E     # checks it; --evidence ID to pick one
approval.promote(pipeline, who, fixture_path=F, expected_path=E, evidence_id=None)
```

`fixture_path` and `expected_path` are required arguments: which fixture and which
expected result the approval rests on is stated, not discovered.

## Why a `pass` in `.synth-validation.yaml` is not enough

That file is a report for humans: anyone can edit it or type it from scratch, and
a hash in it only says what it claims to be about. Evidence therefore has three
independent legs, all of which `evidence.check_for_promote` checks:

1. **Seal — it came out of the controlled path.** `evidence.run_and_seal` (what
   `validate --fixture` and the acceptance matrix call) writes the fixture section
   into the journal table `validation_evidence` with an HMAC made by a host-local
   key (`evidence.key` next to the journal, mode 0600, created on first use; a key
   readable by group/other is not trusted). No row, or a row whose MAC does not
   verify → refused. The report file is never read by promote.
2. **Binding — it is about exactly this content.** The sealed record carries the
   pipeline hash (`approval.content_hash`: manifest, procedures, models, and for an
   owned dbt project every authored file — seeds, macros, packages, lock), the
   fixture file hash and the expected file hash. Promote recomputes all three from
   disk now. Editing a model, a seed, the fixture or the expected file after
   validating → refused.
3. **The runs really happened.** The record names the two dpagent run ids; promote
   asks the journal again: both exist, are `data` runs of the validation clone,
   finished `ok`, and their gate verdicts in the journal are equal to the ones the
   report states; every stage of the pipeline that declares gates has verdicts, all
   `passed`.

## The policy (`evidence.POLICY_VERSION = 1`), evaluated from raw fields

Never from the report's own `overall` string; every failing reason is listed, not
the first one.

| Requirement | Refused when |
|---|---|
| supported shape | `report_version` not in `{2}` |
| step 3 | manifest did not load, or dbt / procedures / dbt-dependencies check not `pass` |
| two runs | either run status is not `ok` (a `timeout` is named as such); not exactly 2 run ids |
| comparison | run 1 or run 2 comparison not `pass`; not idempotent |
| unavailable / deploy | `unavailable_reason` or `deploy_error` set |
| cleanup, per resource | `pipeline_artifacts`, `source_database`, `source_role`, `warehouse_database`, `warehouse_role` (and `s3_objects` for bronze) must each be `pass` — `not_attempted` is not accepted |
| bronze: source removed | no source-down proof, or `ok`, `source_dropped`, `source_unreachable`, `loaded`, `landing_matches_fixture` not all true |
| bronze: S3 | objects remaining after purge ≠ 0, or an S3 error |
| bronze: batches | fewer than 3 (2 runs + the source-down LOAD), or any not `loaded` |
| consistency | evidence and pipeline disagree on `bronze_staging` / `dbt_project` |
| journal | any item of leg 3 above |

A timeout is refused twice over: run status is not `ok`, and cleanup is `fail` by
[the timeout policy](m25-timeout-policy.md) (the worker is not confirmed stopped).

## A refusal changes nothing

Everything is checked before the first write. On success the approval is written
and then `maturity: reviewed`; if the second write fails the first is rolled back,
so an approval without the label (or the label without an approval) is never left.
`dpagent pipeline promote` prints every reason and says nothing was changed.

## What the approval records

`.approved.yaml` gains an `evidence:` block — id, sealed record sha256, policy and
report versions, validation time, clone name, run ids, pipeline hash, the fixture
and expected paths with their hashes, validator versions (dpagent, PostgreSQL, dbt)
and whether it was bronze. It is git-tracked with the approval, so a reviewer sees
which validation the approval stands on.

## Approvals that predate this gate

They are not revoked and not relabelled:

- `deploy()` still accepts them as long as their content hash matches (nothing
  that ran yesterday stops today);
- they report **`unverified`** — `approval.verification()` returns
  `verified | unverified | stale | none`, and `dpagent pipeline list` shows
  `reviewed (no evidence)` for them versus `reviewed + verified`;
- `promote` on such a pipeline is **not** a no-op: it requires evidence and, if
  accepted, replaces the approval with one that carries it. Without evidence it is
  refused and the old approval stays exactly as it was.

The checked-in `demo`, `quickstart` and `quickstart_dbt` approvals are in this
group; none was edited.

## Limits, said plainly

- The key sits on the same host as the journal: someone with root there can forge
  a row. This stops hand-edited or stale reports, evidence for other content or a
  different pipeline, failed / timed-out / uncleaned validations — not a hostile
  root.
- Evidence is host-local. Promote on the host that validated; the approval file
  (which travels in git) records what was accepted, but a second host cannot
  re-verify the sealed record and will not have one to promote from.
- The newest sealed evidence for the pipeline is used unless `--evidence` names one,
  so a later failing validation blocks promoting on an earlier passing one until
  the operator names the good record deliberately.
- It does not make the *expected* file correct — that is still the independently
  hand-calculated input a human wrote; it only fixes which expected file the
  validation used.
- Not in this change: incremental models, more connectors, the rest of HG, platform
  upgrades. `packs/seaweedfs` is still `draft`.

## Verified for real

`bash scripts/m25-acceptance-ci.sh --profile bronze` (a host built from packs) runs
the scenario **`a2-promote-deploy-run`**: promote before any validation is refused;
validate through `run_and_seal`; promote; **real deploy of the promoted, non-draft
pipeline** (distinct name `hg_a2_promote`, its own S3 prefix) and a real Airflow run
whose output is compared with the hand-calculated expected and whose gates are read
from the journal; undeploy and S3 purge verified; then a seed edit, a model edit, a
fixture edit and an expected edit each in turn — the first two lose the approval and
`deploy` refuses, all four make `promote` refuse with the approval bytes unchanged —
and restoring the bytes restores the approval. The older `hg-*` scenarios also try to
promote on their own evidence: `hg-correct-twice` must be accepted and `verified`;
wrong-expected, selector-typo, timeout and purge-failure must be refused. The final
host audit also looks for leftovers of the A2 pipeline.
