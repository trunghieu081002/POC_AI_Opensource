"""Test helper: a pipeline approved the way every approval was before the
evidence gate (A2) - hash recorded, maturity reviewed, no evidence. Tests that
only need "an approved pipeline" (deploy mechanics, synth's overwrite guard)
use this; the gate itself is tested in test_pipelines_evidence.py."""
from dpagent.pipelines import approval


def approve_legacy(pipeline, approved_by="tester"):
    result = approval._write_approval(pipeline, approved_by)
    approval.set_maturity_reviewed(pipeline)
    return result
