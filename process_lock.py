"""Dependency-free interprocess lock for settings and Windows setup."""
from contextlib import contextmanager
import os
import errno
from pathlib import Path
import time
from operation_control import check_cancel


@contextmanager
def process_lock(path: Path, timeout=3.0, *, cancel_event=None):
    """Stable lock inode; OS releases lock when its owning process exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.lock')
    with lock_path.open('a+b') as lock:
        # Windows byte-range locks may extend past EOF; never delete the
        # lock file, since another process may already hold an open handle.
        deadline = time.monotonic() + timeout
        while True:
            check_cancel(cancel_event)
            try:
                if os.name == 'nt':
                    import msvcrt
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                contention = ((errno.EACCES, errno.EAGAIN) if os.name == 'nt'
                              else (errno.EAGAIN, errno.EWOULDBLOCK))
                if exc.errno not in contention:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError('Ресурс занят другим экземпляром TokenForge.')
                if cancel_event is None:
                    time.sleep(.05)
                else:
                    cancel_event.wait(.05)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)

