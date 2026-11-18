"""Atomic preferences updates shared by multiple GUI processes."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import time

from token_engine import atomic_write


def read_settings_file(path: Path, *, strict=False) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    if isinstance(data, dict):
        return data
    if strict:
        raise ValueError('Файл настроек должен содержать JSON-объект.')
    return {}


@contextmanager
def settings_lock(path: Path, timeout=3.0):
    """Stable lock inode; OS releases lock when its owning process exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.lock')
    with lock_path.open('a+b') as lock:
        # Windows byte-range locks may extend past EOF; never delete the
        # lock file, since another process may already hold an open handle.
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Файл настроек занят другим экземпляром TokenForge.')
                time.sleep(.05)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


_MISSING = object()


def merge_delta(latest: dict, baseline: dict, current: dict) -> dict:
    """Apply only local edits, recursively for per-source settings."""
    result = deepcopy(latest)
    for key in baseline.keys() | current.keys():
        old, new = baseline.get(key, _MISSING), current.get(key, _MISSING)
        if old == new:
            continue
        if new is _MISSING:
            result.pop(key, None)
        elif isinstance(new, dict) and (isinstance(old, dict) or old is _MISSING):
            remote = result.get(key)
            result[key] = merge_delta(remote if isinstance(remote, dict) else {},
                                      old if isinstance(old, dict) else {}, new)
        else:
            result[key] = deepcopy(new)
    return result


def save_settings_delta(path: Path, baseline: dict, current: dict) -> bool:
    if baseline == current:
        return False
    with settings_lock(path):
        latest = read_settings_file(path, strict=True)
        updated = merge_delta(latest, baseline, current)
        if updated == latest:
            return False
        payload = json.dumps(updated, ensure_ascii=False, indent=2).encode('utf-8')
        atomic_write(path, lambda stream: stream.write(payload))
    return True
