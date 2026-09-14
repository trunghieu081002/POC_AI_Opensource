"""The catalog is what makes install failures converge. Its matching must be
tight: a miss costs a human once, but a false match "fixes" the wrong thing."""
import pytest

from dpagent.engine import errors
from dpagent.library import loader as packs

REAL_FAILURES = [
    # (output the tool actually prints, the entry that should catch it)
    ("E: Could not get lock /var/lib/dpkg/lock-frontend - open (11: Resource temporarily unavailable)",
     "apt-lock-held"),
    ("E: dpkg was interrupted, you must manually run 'dpkg --configure -a'",
     "apt-dpkg-interrupted"),
    # Deliberately not a postgres package: the pack-local pg-version-not-in-repo
    # entry claims those, and pack entries are checked before shared ones.
    ("E: Unable to locate package redis-server", "apt-stale-index"),
    ("E: Unable to locate package postgresql-16", "pg-version-not-in-repo"),
    ("Err:1 https://apt.postgresql.org jammy InRelease\n  The following signatures couldn't be verified: NO_PUBKEY ACCC4CF8",
     "apt-repo-key-missing"),
    ("Error: Unable to find a match: postgresql15-server", "dnf-no-match"),
    ("Temporary failure resolving 'apt.postgresql.org'", "dns-failure"),
    ("curl: (60) SSL certificate problem: unable to get local issuer certificate",
     "tls-cert-verify"),
    ("dpkg: unrecoverable fatal error: unable to write: No space left on device",
     "disk-full"),
    ("mkdir: cannot create directory '/opt/dbt': Read-only file system",
     "read-only-filesystem"),
    ("initdb: error: invalid locale settings; check LANG and LC_* environment variables",
     "pg-initdb-locale-missing"),
    ("initdb: error: directory \"/var/lib/pgsql/15/data\" exists but is not empty",
     "pg-initdb-datadir-not-empty"),
    ("LOG:  could not bind IPv4 address \"127.0.0.1\": Address already in use",
     "pg-port-taken-by-other"),
    ("psql: error: FATAL:  Peer authentication failed for user \"postgres\"",
     "pg-peer-auth-failed"),
    # The realistic shape: runuser's own benign cwd warning (unrelated to the
    # real failure) sits ahead of the actual ERROR line in the captured
    # output. Must not fall through to the shared catalog's generic
    # permission-denied entry, which would misdiagnose this as a root/sudo
    # problem - the process was already root.
    ("could not change directory to \"/root/dpagent-src/packs/postgres\": Permission denied\n"
     "psql:/tmp/dpagent-pg-XXXXXX.sql:15: ERROR:  database \"phantom_db\" does not exist",
     "pg-user-database-not-declared"),
    ("FATAL:  could not map anonymous shared memory: Cannot allocate memory",
     "pg-shared-memory"),
    ("FATAL:  lock file \"postmaster.pid\" already exists",
     "pg-stale-postmaster-pid"),
    ("Job for postgresql-15.service failed because the control process exited with error code.",
     "pg-service-start-failed"),
]


@pytest.fixture(scope="module")
def catalog():
    pack = packs.load("postgres")
    return errors.Catalog.for_pack(pack.root, pack.errors_file)


def test_shared_catalog_loads():
    shared = errors.Catalog.shared()
    assert len(shared) > 10, "shared catalog should cover the common host failures"


def test_pack_catalog_includes_shared(catalog):
    ids = {e.id for e in catalog.entries}
    assert "pg-initdb-locale-missing" in ids     # pack-local
    assert "apt-lock-held" in ids                # shared


def test_pack_entries_are_checked_before_shared(catalog):
    """Specific must win over generic, or a pack could never override."""
    sources = [e.source for e in catalog.entries]
    first_shared = next(i for i, s in enumerate(sources) if "_lib" in s)
    assert all("_lib" not in s for s in sources[:first_shared])


@pytest.mark.parametrize("output,expected_id", REAL_FAILURES)
def test_real_failure_output_matches_the_right_entry(catalog, output, expected_id):
    match = catalog.match(output)
    assert match is not None, f"nothing matched: {output[:60]}"
    assert match.entry.id == expected_id, (
        f"{output[:50]!r} matched {match.entry.id}, expected {expected_id}")


def test_unknown_failure_does_not_match(catalog):
    """A false match is worse than a miss — it would 'repair' the wrong cause."""
    assert catalog.match(
        "ERROR: the flux capacitor reported an unscheduled temporal anomaly"
    ) is None


def test_success_output_does_not_match(catalog):
    assert catalog.match("Setting up postgresql-15 (15.6-1.pgdg22.04+1) ...") is None


def test_autofix_entries_are_marked_automatic(catalog):
    entry = next(e for e in catalog.entries if e.id == "apt-lock-held")
    assert entry.automatic
    assert entry.max_attempts >= 2


def test_entries_without_a_fix_ask_instead(catalog):
    """Where no safe unattended fix exists, the catalog must ask, not guess."""
    for entry_id in ("disk-full", "dns-failure", "pg-stale-postmaster-pid",
                     "pg-port-taken-by-other"):
        entry = next(e for e in catalog.entries if e.id == entry_id)
        assert not entry.autofix, f"{entry_id} should not auto-fix"
        assert entry.ask_user.strip(), f"{entry_id} must explain what the operator does"
        assert not entry.automatic


def test_every_autofix_survives_the_blacklist(catalog):
    from dpagent.engine import safety
    for entry in catalog.entries:
        for command in entry.autofix:
            verdict = safety.check(command)
            assert verdict.allowed, (
                f"catalog entry {entry.id} carries a blacklisted autofix: "
                f"{command} ({verdict.rule_id})")


def test_timeout_matches_by_rc(catalog):
    match = catalog.match("[dpagent] timed out after 600s", rc=124)
    assert match is not None and match.entry.id == "step-timeout"


def test_excerpt_is_bounded(catalog):
    noise = "x" * 5000
    match = catalog.match(noise + "\nNo space left on device\n" + noise)
    assert match is not None
    assert len(match.excerpt) < 600
