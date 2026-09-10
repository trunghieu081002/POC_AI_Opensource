"""Execution machinery: detect the host, resolve params, run scripts, repair, record.

Nothing in here talks to a model. This is the part that must be deterministic,
auditable, and runnable offline.
"""
