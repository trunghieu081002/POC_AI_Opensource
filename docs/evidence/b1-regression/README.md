# B1 closure and regression of #39

Final source revision: `cbac310741ea82ab11e2c3031222a4c64a1f1322`, tree `e53b41c894995080b4781c43c99119e1f3039b77`. B1, label and artist were tested on `fc18c74eb4341772c171718a6483dcf0969d994f`, tree `a037dcf9c88e56575be4b1e2819719ba71fe969d`. The only change from fc18c74 to cbac310 is the bronze driver's DROP-failure fault injection: an idle connection held until teardown, with a readiness check, replaces the expiring 120-second sleep. Runtime, A3, prompt, template and generated SQL are identical. Full units passed on cbac310; attempts to run full bronze at that revision were blocked during bootstrap. Every test uses a clean Linux checkout of its recorded revision; per-run `status-before.txt` and `run-info.txt` record cleanliness. Evidence is committed separately after the tested source.

## Contract

`expected.yaml` can require `on_violation: fail` on a gate. Matching stage/type/table/columns is then insufficient: any quarantine on that stage is refused, at every rejection threshold, by the shared checker used by the harness and A2. Requirements without this field retain their previous behavior. Prompt, dbt template and existing SQL are unchanged from main/#39.

## Results

**Status: B1 mechanism proved; full bronze regression remains blocked.** No final bronze run of cbac310 has reached the matrix. Final source passes syntax and full unit validation; the durable DROP-failure holder still needs real-host acceptance. A stable Docker/Linux test host is needed to close regression; Git CLI has no credential and the connected GitHub integration returned 403 on blob creation, so no remote branch or PR was created.

| Run | Result | Evidence |
|---|---|---|
| Clean Linux unit suite (fc18c74) | 1254 passed, 25 skipped | `unit/04-final-clean-linux-full-suite.log`, `unit/final-provenance.json` |
| Final clean Linux unit suite (cbac310) | 1254 passed, 25 skipped, exit 0 | `unit/05-harness-fixed-clean-linux-full-suite.log`, `unit/05-provenance.json` |
| Label scripted | 8/8 matched, exit 0, leaks=0 | `fc18c74/label-attempt-1/` |
| Bronze profile attempt 1 | 30/31 matched, exit 1, leaks=0; dbt Hub DNS failed in final case | `fc18c74/bronze-attempt-1/` |
| Bronze profile attempt 2 | Exit 1 at bootstrap; DNS failures in pip, no matrix ran | `fc18c74/bronze-attempt-2/` |
| Bronze profile attempt 3 | 19 reports; DROP-failure mismatched; stopped with exit 130, leaks=0 | `fc18c74/bronze-attempt-3/` |
| Bronze profile, durable holder attempt 1 | Exit 1 at dlt bootstrap (pip DNS), no matrix ran | `cbac310/bronze-attempt-1/` |
| Bronze profile, durable holder attempt 2 | Exit 1 at PostgreSQL PGDG network preflight; no matrix ran | `cbac310/bronze-attempt-2/` |
| Artist attempt 1 | Exit 1 before scenarios; Docker Hub DNS failed at image build | `fc18c74/artist-attempt-1/` |
| Artist scripted attempt 2 | 8/8 matched, exit 0, leaks=0 | `fc18c74/artist-attempt-2/` |

### B1 results on real services, with scripted replies

- L7: two certificates for order 5001 fail the landing unique gate (2/7 rejected). The journal contains only landing/failed, no silver transform start. Promote refuses, approval unchanged, cleanup passes, no recovered leftovers.
- L8: the correct gate plus landing quarantine at 100%, on the unchanged clean fixture. Both real runs are ok, both comparisons pass and are idempotent, both dbt-test runs pass 2/2. The harness returns policy-fail; A2 refuses solely because `on_violation: fail` forbids stage quarantine. Approval unchanged, cleanup passes, no recovered leftovers.
- L1 remains valid through validation twice, A2 promote, real deploy/run, published-file byte checks and approval invalidation checks.

These are mechanism tests using hand-written provider replies, not evidence of new model performance. No real model calls or new benchmark were run.

## Retained unsuccessful attempts

- `unit/01-collection-failed.log`: default `/var/lib/dpagent` journal inaccessible to the unprivileged WSL user; collection failed.
- `unit/02-full-suite.log`: Windows checkout used from WSL, with CRLF shell files and different file hashes; 45 failed / 1209 passed / 25 skipped. It is retained, not presented as a successful Linux run.
- `unit/03-clean-linux-full-suite.log`: clean Linux checkout of the first code commit `66d1c7a`, 1254 passed / 25 skipped. fc18c74 and cbac310 also have independent clean unit runs 04 and 05.
- `66d1c7a/label-attempt-1/`: systemd degraded before bootstrap/test driver, exit 1.
- `66d1c7a/bronze-attempt-1/`: PostgreSQL service PID ownership failed with private cgroup namespaces on this Docker/WSL host; bootstrap config timed out, exit 1, no matrix ran.
- `66d1c7a/artist-attempt-1/`: explicitly stopped during bootstrap after diagnosing the incompatible cgroup configuration; `interrupted.txt` records this. No A3 scenarios ran.
- `fc18c74/bronze-attempt-1/`: 30/31 matched, exit 1, leaks=0. Only `fact-promote-deploy-run` mismatched: `dbt deps` could not resolve `hub.getdbt.com`. Its fail verdict and unchanged approval are retained.
- `fc18c74/bronze-attempt-2/`: bootstrap pip failed to resolve package hosts and could not install wheel, exit 1; no matrix ran.
- `fc18c74/bronze-attempt-3/`: DROP-failure returned pass instead of expected fail. The old `pg_sleep(120)` holder could expire before real validation ended. Stopped with SIGINT after 19 completed reports, exit 130, leaks=0; incomplete remaining scenarios are not claimed. `interrupted.txt` records this.
- `cbac310/bronze-attempt-1/`: pip could not resolve package hosts during dlt bootstrap, exit 1; no matrix ran. The container-only 1.1.1.1 workaround was insufficient.
- `cbac310/bronze-attempt-2/`: dbt/dlt installed, then PostgreSQL network preflight could not reach `https://apt.postgresql.org`, exit 1; no matrix ran. Per-container DNS was 8.8.8.8/8.8.4.4 with `single-request-reopen`, `timeout:2`, `attempts:3` (same DNS servers as the Windows Wi-Fi adapter); it was insufficient to finish bootstrap. Later direct GET probes from Windows/WSL succeeded, so the failure is retained rather than treated as proof of a permanently unavailable PGDG service.
- `fc18c74/artist-attempt-1/`: Docker Hub DNS lookup failed while building the image, exit 1; no A3 scenarios ran.
- `cgroup-probe.txt`: diagnostic observations (not an acceptance verdict). A non-root forking service worked with host cgroup namespaces; an unrelated tty1 getty failed in the headless container. The runner now supports optional `M25_CGROUPNS`, and the disposable image masks getty@tty1. Product packs are unchanged.

## Reproduce

Use a clean Linux checkout. Each command creates, installs and tests a new disposable systemd host. Use a new absolute output directory for each attempt to retain failures. On this Docker Desktop/WSL host, set `M25_CGROUPNS=host`; the runner default remains unchanged for other hosts.

```bash
M25_CGROUPNS=host M25_RESULTS_DIR=/absolute/new/label bash scripts/m25-acceptance-ci.sh --a3 scripted --a3-suite label
M25_CGROUPNS=host M25_RESULTS_DIR=/absolute/new/bronze bash scripts/m25-acceptance-ci.sh --profile bronze
M25_CGROUPNS=host M25_RESULTS_DIR=/absolute/new/artist bash scripts/m25-acceptance-ci.sh --a3 scripted --a3-suite artist
```

For bronze attempt 3, `fc18c74/bronze-attempt-3/docker-wrapper.sh` forwards only `docker run` with `--dns 1.1.1.1`; set `B1_REAL_DOCKER` to the real Docker executable and put the wrapper (named `docker`) first on PATH to reproduce. Other Docker commands are forwarded unchanged. `host-options.txt` records this per-container workaround. No host/global DNS settings were changed.

The same wrapper approach is recorded separately in each cbac310 attempt directory. Bootstrap failures occur before leak-audit collection and have no fabricated leaks=0 verdict. All disposable regression containers were removed by the runner, with their outer attempt logs retained. No production host was deployed to.

## Remaining work

1. Run the complete bronze profile on a stable fresh host at cbac310, retaining source SHA/tree, full 31-case summary and final leak audit. Confirm DROP-failure now reports the expected failure while the idle holder is alive, and leaves no resources after recovery.
2. Confirm 31/31 expectations, exit 0 and leaks=0; retain all failed attempts above.
3. Authenticate Git CLI or grant repository write access to the GitHub integration, push `fix/b1-fail-required-gate` and open the small PR against main. `PR-description.md` is a prepared draft and explicitly names the pending regression.
