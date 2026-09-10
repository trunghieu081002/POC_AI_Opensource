"""Acceptance suites — proving the installed system actually works.

A pack's `verify.sh` answers a narrow question: is this component alive? That is
necessary and nowhere near sufficient. A server can be active, listening, and
answering `SELECT 1` while authentication is disabled, the data directory is on
tmpfs, or the port the operator asked for was never applied. Every one of those
passes verify and fails in production.

A suite answers the real question: does the system do its job? It writes and
reads actual data, restarts services to prove persistence, and asserts that
things which *should* fail actually do — the negative checks are where
false-passes get caught.

Suites run automatically at the end of an install. `installs.tested` records the
result, so `dpagent status` can never report a component as good on the strength
of verify alone.
"""
