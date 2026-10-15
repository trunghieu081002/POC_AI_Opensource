# Final shared-source regression

Code source `4e754bf2dd872ee1aedf5ebe3f4194de5ac0a591`, tree
`391a67fa2d3b42d8604364f5be94f3fea2a7ba11`:

- Full unit **1,326 passed / 26 skipped**, exit 0. Skip count includes the existing
  25 skips and the new runtime pack's unsupported RPM dry-run case on Ubuntu.
- Targeted usage **3/3**, exit 3 (intentional requested-subset status), leaks 0.
- Full current bronze **34/34**, exit 0, leaks 0. Includes all conflicting-ID,
  no-gold-start, unchanged-approval, independent comparisons and deploy/run gates.
- Final journal and verified owned-container removal are in `final-audit`.
  Existing company-container baseline: **11 IDs present, missing 0**.

Failures are preserved. An earlier source's full unit found a missing Superset
parameter map in a dry-run fixture (fixed at 4e754bf). Initial stack setup on
4e754bf passed installation but the PostgreSQL suite restart timed out with the
process and cgroup empty, while systemd remained deactivating on this legacy
cgroup-v1 host. No usage test ran in that attempt. A single daemon-reexec inside
that verified disposable container recovered its manager; the PostgreSQL suite
then passed 6/6 (real restart about six seconds). The same container/source was
reused without bootstrap for the complete usage and bronze matrix above. No
host systemd, Docker daemon, DNS or company service was modified.

This observed recovery is not a general fix or proof of production stability for
legacy cgroup configurations. The initial failure and recovery logs remain.
The final host audit collector first used system Python 3.6 and failed parsing
its baseline-only script after container removal; the collector was rerun with
the dedicated interpreter and independently confirmed removal and 11/11 baseline
IDs. No source/assertion/expected was changed to obtain these results.

Documentation/evidence commits after 4e754bf do not change tested implementation.
Runtime acceptance and rollback/recovery appear under `../superset-pack/`.
Publication copies redact secrets/private endpoint where necessary; original
records remain in the private host evidence directories.
