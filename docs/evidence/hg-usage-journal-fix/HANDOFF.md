# HG usage journal fix ? completed Linux-host verification

Current source accepted: **cefb458b8131e97b7b1df9649edc43a6926e5759**.
Company-host targeted usage: **3/3 matched**, exit **3** (partial profile).
Full bronze: **34/34 matched**, exit **0**. Final full unit: **1,310 passed /
25 skipped**, exit **0**. Independent leak audits: **0**. The owned company
runner was removed after journal capture and baseline-container preservation
checks. Host was Oracle Linux 8.10; the real stack ran inside the repository's
Ubuntu 22.04 systemd disposable Docker host, not natively on Oracle Linux.

See [company evidence and reproduction](../../evidence/hg-usage-company/README.md)
and [machine-readable summary](../../evidence/hg-usage-company/summary.json).
Each successful and failed company attempt has its own directory. Full local VM
history is preserved under `../hg-usage-vm/`. Its final attempt was interrupted
(exit 130), leak audit 0; its runner was stopped and retained for diagnosis.

The original journal fix requires matching silver transform.start and the silver
`unique_fact_editing_fact_editing_sk` failure in the exact conflicting run, no
gold transform.start, refused promote and unchanged approval. Missing journal
fails acceptance. These conditions passed on the real company disposable host.
Positive cases also prove independent silver/gold expected, idempotency, seed
5,787, CamelCase Bronze, measured Etc/UTC dbt timezone, actual timestamp types,
source-down LOAD, valid promote/deploy/run and published bytes. HG business SQL,
seed and independent expected remain unchanged.

Follow-up source fixes handle scenario argument splitting, verified disposable
reuse/evidence collection, multiline dbt audit JSON and typed timestamp fixture
equivalence without assuming UTC for naive timestamptz expected values. Original
1,269/25 evidence at merge b1a5b6e and 1,281/25 on b2587b3 remain intact; those
old results are not substituted for final-source unit evidence.

PR: https://github.com/trunghieu081002/POC_AI_Opensource/pull/42.
No automatic merge. Superset pack, BI artifacts, second project, operations and
representative HG load remain separately reviewed work.
