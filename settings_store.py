"""Atomic preferences updates shared by multiple GUI processes."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from token_engine import atomic_write
from process_lock import process_lock as settings_lock


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
