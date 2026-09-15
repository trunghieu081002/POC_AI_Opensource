"""hostlock is the only thing standing between two dpagent processes and the
race this project actually hit: two concurrent `dpagent install postgres
--force` runs both passed preflight, both proceeded, and one's acceptance
suite got "FATAL: the database system is shutting down" from the other's
step restarting the same service mid-check. These tests exercise the real
flock, not a mock - the whole point is being sure two processes genuinely
serialize."""
import multiprocessing
import time

import pytest

from dpagent.engine import hostlock


@pytest.fixture(autouse=True)
def _isolated_lock_path(tmp_path, monkeypatch):
    monkeypatch.setattr(hostlock, "LOCK_PATH", tmp_path / "dpagent.lock")


def test_lock_is_reentrant_free_sequential_use():
    """Acquiring and releasing twice in a row (the common case: one
    invocation, real work, done) must never deadlock against itself."""
    with hostlock.host_lock(timeout=5):
        pass
    with hostlock.host_lock(timeout=5):
        pass


def test_second_acquire_blocks_until_the_first_releases():
    """The actual guarantee this module exists for: a second caller waiting
    on the lock does not proceed until the first one is done - checked by
    timing, not by inspecting internal state, so this fails if the lock
    is ever accidentally non-blocking."""
    order = []

    def holder():
        with hostlock.host_lock(timeout=10):
            order.append("holder-start")
            time.sleep(1.5)
            order.append("holder-end")

    def waiter():
        time.sleep(0.3)  # let holder acquire first
        with hostlock.host_lock(timeout=10):
            order.append("waiter-start")

    import threading
    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert order == ["holder-start", "holder-end", "waiter-start"], order


def test_on_wait_callback_fires_only_when_actually_blocked():
    calls = []

    with hostlock.host_lock(timeout=5, on_wait=lambda: calls.append(1)):
        pass
    assert calls == [], "on_wait must not fire when the lock was free"


def test_lock_times_out_with_a_clear_message_instead_of_hanging_forever():
    """A stuck lock (something still genuinely running, or - in principle -
    a crashed holder that somehow left it, though flock itself is released
    by the kernel on process exit) must never hang a caller with no way
    out. Uses a real second process, not a thread, so the lock is actually
    held by a separate OS process the way two dpagent invocations would be."""
    lock_path = hostlock.LOCK_PATH

    def hold_forever(path):
        import fcntl
        fh = open(path, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        time.sleep(5)

    proc = multiprocessing.Process(target=hold_forever, args=(lock_path,))
    proc.start()
    time.sleep(0.5)  # let the child actually acquire it first

    try:
        with pytest.raises(hostlock.HostLockTimeout, match="another dpagent"):
            with hostlock.host_lock(timeout=1):
                pass
    finally:
        proc.terminate()
        proc.join()


def test_waiter_proceeds_once_the_holder_process_exits():
    """flock releases automatically when the holding process exits (even
    without an explicit unlock) - a genuinely crashed dpagent process must
    not leave the lock stuck forever for the next real run."""
    lock_path = hostlock.LOCK_PATH

    def hold_briefly(path):
        import fcntl
        fh = open(path, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        time.sleep(1)
        # process exit releases the flock even with no explicit unlock

    proc = multiprocessing.Process(target=hold_briefly, args=(lock_path,))
    proc.start()
    time.sleep(0.3)

    with hostlock.host_lock(timeout=10):
        pass  # must not raise - proves waiting past the holder's exit works

    proc.join()
