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


def _fake_root(tmp_path, *, mem_total_kb, cgroup_v1_limit_bytes=None,
                cgroup_v2_limit=None):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc" / "meminfo").write_text(
        f"MemTotal:       {mem_total_kb} kB\nMemFree:        1024 kB\n"
    )
    cgroup = tmp_path / "sys" / "fs" / "cgroup"
    if cgroup_v2_limit is not None:
        cgroup.mkdir(parents=True)
        (cgroup / "memory.max").write_text(f"{cgroup_v2_limit}\n")
    elif cgroup_v1_limit_bytes is not None:
        (cgroup / "memory").mkdir(parents=True)
        (cgroup / "memory" / "memory.limit_in_bytes").write_text(
            f"{cgroup_v1_limit_bytes}\n"
        )
    return tmp_path


def test_memory_mb_ignores_host_total_when_cgroup_v1_limit_is_tighter(tmp_path):
    # This is the exact shape of the bug: a container capped at 1GB by the
    # host, sitting on a 64GB physical machine. /proc/meminfo alone would
    # report the full 64GB and doctor would wave the host through.
    root = _fake_root(
        tmp_path, mem_total_kb=65437896, cgroup_v1_limit_bytes=1073741824
    )
    assert sysinfo.memory_mb(root) == 1024


def test_memory_mb_ignores_unset_cgroup_v1_sentinel(tmp_path):
    # cgroup v1's "no limit" value is the largest page-aligned number below
    # INT64_MAX, not a round figure — must not be mistaken for a real cap.
    root = _fake_root(
        tmp_path, mem_total_kb=65437896, cgroup_v1_limit_bytes=9223372036854771712
    )
    assert sysinfo.memory_mb(root) == 65437896 // 1024


def test_memory_mb_uses_cgroup_v2_limit_when_tighter(tmp_path):
    root = _fake_root(
        tmp_path, mem_total_kb=8000000, cgroup_v2_limit=536870912
    )
    assert sysinfo.memory_mb(root) == 512


def test_memory_mb_ignores_cgroup_v2_max_sentinel(tmp_path):
    root = _fake_root(tmp_path, mem_total_kb=8000000, cgroup_v2_limit="max")
    assert sysinfo.memory_mb(root) == 8000000 // 1024


def test_memory_mb_falls_back_to_host_total_with_no_cgroup_files(tmp_path):
    root = _fake_root(tmp_path, mem_total_kb=8000000)
    assert sysinfo.memory_mb(root) == 8000000 // 1024


def test_memory_mb_does_not_use_a_looser_cgroup_limit_than_host_total(tmp_path):
    # A cgroup limit above the host's real RAM (e.g. an unconstrained
    # container where the limit still reads as a huge-but-finite number)
    # must not inflate the reported figure past what physically exists.
    root = _fake_root(
        tmp_path, mem_total_kb=2000000, cgroup_v1_limit_bytes=999999999999
    )
    assert sysinfo.memory_mb(root) == 2000000 // 1024
