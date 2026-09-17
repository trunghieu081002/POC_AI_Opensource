"""bootstrap.sh's source resolution, run for real through real bash - not
read for plausibility.

Found for real (P0 audit, docs/e2e-hardening.md): `DPAGENT_REPO` defaulted
to `https://github.com/CHANGEME/dpagent.git`, and `scripts/setup.sh` never
told `bootstrap.sh` to use the checkout it was itself just extracted from -
running the documented "tar xzf ... && sudo bash scripts/setup.sh" flow
tried to git-clone a placeholder URL instead of installing the tarball's own
source. Fixed with an explicit `DPAGENT_SOURCE_DIR` priority (local dir >
tarball URL > git URL, no default), auto-detected when unset so bootstrap.sh
used standalone (its own documented "scp scripts/bootstrap.sh" use case)
still works.

Root is unavoidable for the *whole* script (it ends by writing
/var/lib/dpagent, /usr/local/bin - real system paths, not parameterised).
These tests stub apt-get/dnf/yum/id/update-ca-trust via a PATH-prepended
fake bin/ (the only OS-level calls the fetch stage makes) and set
DPAGENT_BOOTSTRAP_STOP_AFTER_FETCH so the real script exits cleanly right
after fetch - covering exactly the regression, in well under a second,
without needing real root or a real (possibly slow) pip/network round trip
for venv creation, which is not what these tests are about.
"""
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
BASH = shutil.which("bash")

requires_bash = pytest.mark.skipif(BASH is None, reason="bash is not installed here")


def _fake_bin(tmp_path: Path) -> Path:
    """A PATH-prepended directory whose apt-get/dnf/yum/id/update-ca-trust
    override the real ones - id -u lies that we are root (bootstrap.sh's own
    root check is the very first line of real logic; the *actual* privilege
    enforcement further down, on /var/lib, still fires as our real,
    non-root, user - that is what stops these tests short, not this stub),
    the package managers are harmless no-ops. Everything else (git, curl,
    tar, python3, pip) is the real system tool."""
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "apt-get").write_text("#!/bin/sh\nexit 0\n")
    (fake / "dnf").write_text("#!/bin/sh\nexit 0\n")
    (fake / "yum").write_text("#!/bin/sh\nexit 0\n")
    (fake / "update-ca-trust").write_text("#!/bin/sh\nexit 0\n")
    (fake / "update-ca-certificates").write_text("#!/bin/sh\nexit 0\n")
    (fake / "id").write_text(textwrap.dedent("""\
        #!/bin/sh
        if [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi
        """))
    for f in fake.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return fake


def _modern_python() -> str:
    """A real 3.10+ interpreter on this host, if one exists beyond the
    system python3 - this project's own dev/test hosts run RHEL/OL8, whose
    system python3 is 3.6, which would otherwise fail bootstrap.sh's own
    version gate before ever reaching the fetch logic under test here.
    Skips these tests rather than guessing when none is found."""
    for candidate in ("python3.13", "python3.12", "python3.11", "python3.10"):
        found = shutil.which(candidate)
        if found:
            return found
    return sys.executable if sys.version_info >= (3, 10) else ""


MODERN_PYTHON = _modern_python()
requires_modern_python = pytest.mark.skipif(
    not MODERN_PYTHON,
    reason="no python 3.10+ interpreter found on PATH to satisfy bootstrap.sh's own gate")


def _run_bootstrap(tmp_path: Path, env_extra: dict, cwd: Path | None = None):
    fake = _fake_bin(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{fake}:{env['PATH']}"
    env["DPAGENT_PYTHON"] = MODERN_PYTHON
    # These tests are about source resolution only - stop before venv/pip,
    # which do real (possibly slow) network work unrelated to what is under
    # test here. See bootstrap.sh's own comment on this variable.
    env["DPAGENT_BOOTSTRAP_STOP_AFTER_FETCH"] = "1"
    env.update(env_extra)
    return subprocess.run(
        [BASH, str(BOOTSTRAP)], capture_output=True, text=True,
        env=env, timeout=30, cwd=str(cwd) if cwd else None)


def _fake_extracted_tarball(tmp_path: Path, name: str = "dpagent-test") -> Path:
    """A directory shaped like a real release tarball once extracted -
    real source (a real, if trivial, pyproject.toml + package), not the
    actual dpagent checkout, so a wrong fetch (e.g. quietly falling through
    to a real git clone) shows up as a content mismatch, not a coincidence."""
    root = tmp_path / name
    (root / "src" / "dpagent_fake").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "dpagent-fake"\nversion = "0.0.0-test"\n')
    (root / "MARKER.txt").write_text("this exact file proves which source was used\n")
    return root


@requires_bash
@requires_modern_python
def test_fresh_tarball_extract_installs_its_own_source_not_a_placeholder_clone(tmp_path):
    """The P0 regression, end to end: run exactly as if bootstrap.sh were
    sitting inside a just-extracted tarball (DPAGENT_SOURCE_DIR pointing at
    that directory, as scripts/setup.sh now sets it) - PREFIX must end up
    holding *that* source, and no git clone of any URL - CHANGEME or
    otherwise - may be attempted."""
    source = _fake_extracted_tarball(tmp_path)
    prefix = tmp_path / "prefix"

    result = _run_bootstrap(tmp_path, {
        "DPAGENT_SOURCE_DIR": str(source),
        "DPAGENT_PREFIX": str(prefix),
    })

    assert "CHANGEME" not in result.stdout + result.stderr
    assert "cloning" not in (result.stdout + result.stderr).lower()
    assert (prefix / "MARKER.txt").exists(), (
        f"PREFIX does not hold the local source's own file - stdout/stderr:\n"
        f"{result.stdout}\n{result.stderr}")
    assert (prefix / "MARKER.txt").read_text() == "this exact file proves which source was used\n"
    assert not (prefix / ".git").exists(), "local-source install must not be a git checkout"
    assert result.returncode == 0, result.stdout + result.stderr


@requires_bash
@requires_modern_python
def test_explicit_source_dir_wins_even_when_bootstrap_sh_is_not_inside_it(tmp_path):
    """Auto-detection (bootstrap.sh's own parent has a pyproject.toml) must
    not be the only way DPAGENT_SOURCE_DIR gets used - an operator naming a
    directory explicitly, with bootstrap.sh copied somewhere else entirely
    (its own single-file "scp scripts/bootstrap.sh" use case), must still
    work."""
    source = _fake_extracted_tarball(tmp_path, name="elsewhere")
    prefix = tmp_path / "prefix"
    isolated_cwd = tmp_path / "isolated"
    isolated_cwd.mkdir()

    result = _run_bootstrap(tmp_path, {
        "DPAGENT_SOURCE_DIR": str(source),
        "DPAGENT_PREFIX": str(prefix),
    }, cwd=isolated_cwd)

    assert (prefix / "MARKER.txt").exists(), result.stdout + result.stderr


@requires_bash
@requires_modern_python
def test_auto_detected_source_dir_does_not_override_an_explicit_tarball_or_repo(tmp_path):
    """An operator who set DPAGENT_TARBALL or DPAGENT_REPO on purpose must
    not have it silently overridden just because bootstrap.sh happens to be
    sitting next to a real checkout (this repo's own scripts/ directory)."""
    result = _run_bootstrap(tmp_path, {
        "DPAGENT_TARBALL": "https://example.invalid/does-not-exist.tar.gz",
        "DPAGENT_PREFIX": str(tmp_path / "prefix"),
    })
    combined = result.stdout + result.stderr
    assert "example.invalid" in combined or "fetching the release tarball" in combined
    assert "installing from the local source" not in combined


@requires_bash
@requires_modern_python
def test_no_source_configured_fails_loudly_not_a_placeholder_clone(tmp_path):
    """Bootstrap.sh copied alone (its own documented single-file use case)
    into a directory with nothing else, no env vars set: must refuse
    clearly, never fall back to a CHANGEME URL."""
    isolated = tmp_path / "alone"
    isolated.mkdir()
    shutil.copy(BOOTSTRAP, isolated / "bootstrap.sh")

    result = subprocess.run(
        [BASH, str(isolated / "bootstrap.sh")],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{_fake_bin(tmp_path)}:{os.environ['PATH']}",
            "DPAGENT_PYTHON": MODERN_PYTHON,
            "DPAGENT_PREFIX": str(tmp_path / "prefix")})

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "CHANGEME" not in combined
    assert "no source to install from" in combined


@requires_bash
@requires_modern_python
def test_rerunning_with_the_same_source_is_idempotent(tmp_path):
    """Second bootstrap run must not error out differently or duplicate
    anything - re-syncing identical source into an already-populated PREFIX
    is a no-op beyond re-copying the same bytes."""
    source = _fake_extracted_tarball(tmp_path)
    prefix = tmp_path / "prefix"
    env_extra = {"DPAGENT_SOURCE_DIR": str(source), "DPAGENT_PREFIX": str(prefix)}

    first = _run_bootstrap(tmp_path, env_extra)
    assert (prefix / "MARKER.txt").exists(), first.stdout + first.stderr

    second = _run_bootstrap(tmp_path, env_extra)
    assert (prefix / "MARKER.txt").exists(), second.stdout + second.stderr
    # Both runs reach the same real-root boundary the same way - the second
    # run's own output must not show a source-resolution error the first
    # run didn't have.
    assert ("no source to install from" not in second.stdout + second.stderr)
