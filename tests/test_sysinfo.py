"""sysinfo backs `dpagent doctor` and has to work before `base` is installed —
these checks use only the standard library, no shelling out to `ss`/`df`."""
import socket

from dpagent.engine import sysinfo


def test_port_free_is_true_for_an_unused_high_port():
    # Ask the OS for a free port, then release it and check again immediately -
    # about as close to a guaranteed-free port as a test can get.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert sysinfo.port_free(port, host="127.0.0.1")


def test_port_free_is_false_while_something_listens():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert not sysinfo.port_free(port, host="127.0.0.1")


def test_disk_free_mb_returns_a_positive_number_for_an_existing_path(tmp_path):
    free = sysinfo.disk_free_mb(str(tmp_path))
    assert free is not None
    assert free > 0


def test_disk_free_mb_walks_up_to_an_existing_ancestor(tmp_path):
    missing = tmp_path / "does" / "not" / "exist"
    assert sysinfo.disk_free_mb(str(missing)) is not None


def test_command_exists_finds_something_universally_present():
    # Whatever launched this test run, one of these common names should
    # resolve on PATH — a portable stand-in for "some command that exists"
    # without asserting a specific one (which shim is on PATH varies by OS).
    assert any(sysinfo.command_exists(name) for name in ("python3", "python", "py"))


def test_command_exists_is_false_for_a_name_that_cannot_exist():
    assert not sysinfo.command_exists("dpagent-nonexistent-command-xyz")


def test_memory_mb_is_none_or_positive():
    # /proc/meminfo doesn't exist off Linux; must degrade to None, not raise.
    result = sysinfo.memory_mb()
    assert result is None or result > 0
