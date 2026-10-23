# Native Oracle Linux 8 acceptance (runbook)

Row 1 of [release-criteria.md](release-criteria.md). An OL8.10 **container** run (`quickstart-acceptance-ci.sh --os ol8`)
proves the EL8 userland only. This runbook covers a real host or VM with SELinux enforcing and firewalld running.
**Status: not run yet - waiting for a VM.** The runner below has not touched a real VM.

## The machine

| | Required |
|---|---|
| OS | Oracle Linux 8.10, a fresh "Server" or minimal install with the defaults: SELinux `Enforcing` (targeted), firewalld enabled |
| Size | ≥ 4 vCPU, ≥ 16 GB RAM, ≥ 60 GB disk (≥ 30 GB free on `/` at start) |
| Access | SSH from the machine that runs the acceptance; a user with passwordless `sudo` (`sudo -n true` works) |
| Network | outbound HTTPS to the OL8 / EPEL / PGDG mirrors, PyPI and GitHub (Airflow constraints) |
| Snapshot | one snapshot taken **right after** the clean install and before anything else; reverted before every run |

Nothing in the runner turns SELinux or firewalld off, and none of its checks pass without them. If a step fails
because of them, the fix goes into the shared packs/scripts; the policy is never weakened for a test.

## Runs

From a checkout of the commit under test (a clean tree), on a machine with `ssh`, `git` and `docker`. Docker is
needed only for the browser check.

```bash
# 1. revert the VM to the clean snapshot, then:
bash scripts/native-acceptance.sh --ssh admin@ol8-vm --suite demo --label vm1
# 2. revert to the clean snapshot again, then:
bash scripts/native-acceptance.sh --ssh admin@ol8-vm --suite bronze --label vm1
# 3. reproducibility: revert once more and repeat 1 (label vm1-rebuild); the two demo runs must agree
```

| Suite | What it does on the host | PASS needs |
|---|---|---|
| preflight (both) | **refuses** a container, a non-EL8 OS, SELinux not Enforcing, firewalld not running, < 30 GB free, an existing `/opt/dpagent`; records firewall rules, ports and the system python before anything is installed | all true |
| `demo` | bootstrap from `git archive`; `demo.sh install` + `lock`; the accepted AI draft imported (no model, no key); deploy refused before approval; approval stand-in; deploy/run = expected; schedule; dashboard + viewer (API, and a real browser through an SSH tunnel) | every check of `inside.sh first` |
| | **reboot**: every service running before is running again; SELinux still enforcing; firewalld active; DPAgent journal, Superset dashboards and Airflow runs intact; the viewer still gets the expected rows; a new run = expected | all |
| | bad data blocked at the landing gate, recovery, undeploy audit; then **reinstall** (`install` + `lock` again) succeeds without change | every check of `inside.sh second`; reinstall rc=0 |
| `bronze` | bootstrap; the bronze stack (`examples/layer2-bronze-stack.yaml`, SeaweedFS pack); the bronze matrix (`run_matrix.py --profile bronze`) | all scenarios matched |
| OS audit (both) | AVC/USER_AVC/SELINUX_ERR records since the start; firewall rules before/after; ports listening off loopback; system python and `rpm -V python3` before/after | 0 unexplained denials (exit 6 otherwise), firewall unchanged, system python unchanged |

Evidence: `docs/evidence/native/<sha>/<utc>-<suite>-<label>/` (`steps.log` first). Commit it, including failed runs.

## What it will not show

- RHEL 8 or other EL8 rebuilds: the preflight accepts them, but only the OS that was run counts as supported.
- EL9.
- Opening the services to other machines. Superset stays on loopback by design, and the runner checks that the
  firewall was **not** changed. Exposing a service is a separate decision that needs its own firewalld rule and
  review.
- Load. That is row 5.
