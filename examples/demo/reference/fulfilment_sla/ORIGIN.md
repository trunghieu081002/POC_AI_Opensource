# Hand-written reference - NOT drafted by AI

`examples/demo/demo.sh synth` normally lets the model draft `pipelines/fulfilment_sla/` from the BRD
(`dpagent pipeline synth ... --fixture ... --expected ...`). This directory is what the demo uses **instead** when
`DEMO_DRAFT=reference` (no model credential, or a rehearsal of the layers 1/2 and BI): a person wrote it from the same
BRD. Every step after it (validate, approval by a person, deploy, run, dashboard) is the same product path. A demo run
that used this directory says so in its evidence (`draft-origin.txt`); it is never reported as the model's work.
