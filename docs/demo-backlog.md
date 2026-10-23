# After the demo: separate milestones

Each item below is a milestone of its own, with its own acceptance. None of them is part of `v0.4.0-demo-rc1`, and
none may delay it.

## 1. Native Oracle Linux 8

**Goal.** The same demo on a real OL8.10 VM (not a container), with SELinux enforcing and firewalld running.

**Accepted when:**
- `bootstrap.sh`, `demo.sh install`/`lock` and the full flow (`--demo artifact`) pass on the VM, with SELinux
  `Enforcing` and no policy turned off.
- Every port the stack opens is listed. Only the documented ports are allowed through firewalld.
- The system Python is unchanged; `rpm -V` on it is clean.
- A reboot leaves every service running, and a second run of the pipeline still equals the expected result.
- The evidence records the VM's OS release, kernel, `getenforce`, firewalld rules, and every AVC denial (none
  unexplained).

## 2. HG representative load

**Goal.** HG's own data shape and volume through the same engine, against an SLA.

**Accepted when:**
- HG names the dataset (tables, rows, growth), the SLA (finish time, freshness) and the host size. The test data
  is either HG's own data or a generator HG signs off as representative.
- Three consecutive scheduled runs finish within the SLA, and their gates and outputs are checked independently.
- One run with a failure injected at each layer (source unavailable, bad rows, a warehouse restart) fails where
  it should and recovers without manual data repair.
- Resource use (CPU, memory, disk, run time per stage) is recorded, and the host has documented headroom.

## 3. Production operations

**Goal.** Something a team can run unattended.

**Accepted when:**
- Backup and restore of the warehouse, the Airflow and Superset metadata, and DPAgent's journal are demonstrated
  by restoring onto a new host and running the pipeline.
- Monitoring and alerts work: a failed run, a stopped service and a full disk each raise an alert someone receives.
- An upgrade path is shown: one pack version moves forward on a running host; it can be rolled back, and nothing
  breaks.
- Secrets come from the team's secret store, not from files on the host. Users and roles are reviewed.
- Each of the failure modes above has a runbook.

## 4. Fully locked environments (follow-up to the demo locks)

**Goal.** No dependency resolved at install time.

**Accepted when:**
- Every venv, Superset included, installs from a lock with hashes (`--require-hashes`).
- PostgreSQL and the OS packages are pinned to exact versions.
- A rebuild a month later produces the same `dependency-versions.txt`.
