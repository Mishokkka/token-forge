"""Atomic preferences updates shared by multiple GUI processes."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from token_engine import atomic_write, Adjust
from item_engine import ItemAdjust, ITEM_MODES
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




TOKEN_RANGES = {'zoom': (5, 320), 'x': (-120, 120),
                'y': (-120, 120), 'gap': (-20, 50)}
ITEM_RANGES = {'zoom': (10, 100), 'x': (-100, 100),
               'y': (-100, 100), 'rotation': (-180, 180)}


def valid_layout(value, default=ITEM_MODES[0]):
    return value if isinstance(value, str) and value in ITEM_MODES else default


def output_format(value, *, strict=False):
    if isinstance(value, str) and value.lower() in ('png', 'webp'):
        return value.lower()
    if strict:
        raise ValueError('Неподдерживаемый формат токена в настройках.')
    return 'webp'


def override_for(settings, group, source):
    mapping = settings.get(group)
    stored = mapping.get(str(source.resolve()), {}) if isinstance(mapping, dict) else {}
    return stored if isinstance(stored, dict) else {}


def decode_adjustment(stored, mode, default_layout=ITEM_MODES[0]):
    """Use one field-wise schema for GUI controls and both export entry points.

    Valid legacy numeric values outside the current slider range are clamped
    for rendering only; never silently rewrite unrelated stored fields.
    """
    item = mode == 'item'
    defaults = ItemAdjust(layout=valid_layout(default_layout)) if item else Adjust()
    ranges = ITEM_RANGES if item else TOKEN_RANGES
    stored = stored if isinstance(stored, dict) else {}
    for name, (low, high) in ranges.items():
        value = stored.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        # Huge JSON integers can overflow conversion to float.
        try:
            number = float(value)
        except (ValueError, OverflowError):
            continue
        if not math.isfinite(number) or (name == 'zoom' and number <= 0):
            continue
        setattr(defaults, name, min(high, max(low, round(number))))
    if item:
        defaults.layout = valid_layout(stored.get('layout'), defaults.layout)
        for name in ('flip_horizontal', 'flip_vertical', 'clean_noise'):
            value = stored.get(name)
            if isinstance(value, bool):
                setattr(defaults, name, value)
    return defaults


def adjustment_for(settings, source, mode, default_layout=None):
    group = 'item_overrides' if mode == 'item' else 'overrides'
    if default_layout is None:
        default_layout = settings.get('item_layout', ITEM_MODES[0])
    return decode_adjustment(override_for(settings, group, source), mode, default_layout)


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


def save_settings_delta(path: Path, baseline: dict, current: dict, *, resets=()) -> bool:
    if baseline == current and not resets:
        return False
    with settings_lock(path):
        latest = read_settings_file(path, strict=True)
        # A reset is an explicit operation, even when the local snapshot
        # never contained that override. Apply it to the latest document.
        reset_latest = deepcopy(latest)
        for group, key in resets:
            mapping = reset_latest.get(group)
            if isinstance(mapping, dict):
                mapping.pop(key, None)
        updated = merge_delta(reset_latest, baseline, current)
        if updated == latest:
            return False
        payload = json.dumps(updated, ensure_ascii=True, indent=2).encode('utf-8')
        atomic_write(path, lambda stream: stream.write(payload))
    return True
