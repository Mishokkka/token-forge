"""Atomic preferences updates shared by multiple GUI processes."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from token_engine import atomic_write, Adjust, source_identity, source_snapshot, assert_source_identity
from local_models import LOCAL_MODELS, DEFAULT_MODEL, require_local_model
from item_engine import ItemAdjust, ITEM_MODES
from process_lock import process_lock as settings_lock
from resource_policy import EDGE_MODES, DEFAULT_EDGE_MODE, TOKEN_OUTPUT_SIZES


def read_settings_file(path: Path, *, strict=False) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        if strict:
            raise
        return {}
    if isinstance(data, dict):
        return data
    if strict:
        raise ValueError('Файл настроек должен содержать JSON-объект.')
    return {}




BOOL_DEFAULTS = {'keep_alpha': True, 'skip_exists': True, 'convert_original': True}


def decode_settings(raw, *, default_frame, strict=False, mode=None):
    """One global schema for GUI and CLI. Accept only unambiguous legacy values.

    Strict automated actions reject invalid relevant values before writing.
    The GUI uses safe defaults and allows the user to correct them.
    Unknown fields and per-art overrides survive local edits.
    """
    result = deepcopy(raw)
    def invalid(name, default):
        if strict:
            raise ValueError(f'Некорректная настройка: {name}. Открой TokenForge и исправь её.')
        return default

    for name, default in BOOL_DEFAULTS.items():
        value = raw.get(name, default)
        if isinstance(value, bool):
            pass
        elif isinstance(value, (int, str)) and str(value).strip().lower() in ('1', 'true', 'yes', 'on'):
            value = True
        elif isinstance(value, (int, str)) and str(value).strip().lower() in ('0', 'false', 'no', 'off'):
            value = False
        else:
            value = invalid(name, default)
        result[name] = value
    for name, allowed, default in (('model', LOCAL_MODELS, DEFAULT_MODEL),
                                   ('edge_mode', EDGE_MODES, DEFAULT_EDGE_MODE)):
        value = raw.get(name, default)
        if name == 'model' and strict:
            require_local_model(value)
        result[name] = value if isinstance(value, str) and value in allowed else invalid(name, default)
    # Token-only options must not prevent an item export.
    if mode != 'item':
        result['ext'] = output_format(raw.get('ext', 'webp'), strict=strict)
        size = raw.get('size', 256)
        if isinstance(size, str) and size in tuple(map(str, TOKEN_OUTPUT_SIZES)):
            size = int(size)
        result['size'] = size if type(size) is int and size in TOKEN_OUTPUT_SIZES else invalid('size', 256)
    ring = raw.get('ring', str(default_frame))
    if not isinstance(ring, str) or '\0' in ring:
        ring = invalid('ring', str(default_frame))
    result['ring'] = ring or str(default_frame)
    folder = raw.get('folder', '')
    result['folder'] = folder if isinstance(folder, str) and '\0' not in folder else ''
    result['work_mode'] = raw.get('work_mode') if raw.get('work_mode') in ('token', 'item') else 'token'
    result['item_layout'] = valid_layout(raw.get('item_layout'))
    for name in ('overrides', 'item_overrides'):
        if not isinstance(raw.get(name), dict):
            result[name] = {}
    return result


def recover_settings_file(path: Path):
    """Explicit recovery: back up exact invalid bytes before replacing them.

    Recheck under the lock so a concurrently repaired document wins.
    IO/permission errors are not interpreted as corrupt JSON.
    """
    import uuid
    with settings_lock(path, timeout=0):
        try:
            return read_settings_file(path, strict=True), None
        except (ValueError, RecursionError):
            identity = source_identity(path)
            with source_snapshot(path, identity) as captured:
                data = captured.read()
            # The file may have changed while waiting for the user's consent.
            try:
                parsed = json.loads(data.decode('utf-8'))
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed, None
            backup = path.with_name(f'{path.name}.corrupt-{uuid.uuid4().hex}.bak')
            if not atomic_write(backup, lambda fp: fp.write(data), replace=False):
                raise FileExistsError(backup)
            publication = atomic_write(path, lambda fp: fp.write(b'{}\n'),
                                       before_publish=lambda: assert_source_identity(path, identity),
                                       expected_destination=identity)
            if publication.warnings:
                raise OSError('Восстановление с предупреждениями: ' + '; '.join(publication.warnings)
                              + f'. Резервная копия: {backup}')
            return {}, backup


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


class BatchAdjustments:
    """Lazily resolve placement from the captured settings, once per file."""
    def __init__(self, settings, mode, default_layout=None):
        self.settings = settings
        self.mode = mode
        self.default_layout = default_layout
        self._values = {}

    def __getitem__(self, source):
        if source not in self._values:
            self._values[source] = adjustment_for(self.settings, source, self.mode, self.default_layout)
        return self._values[source]


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
        elif isinstance(new, dict):
            remote = result.get(key)
            result[key] = merge_delta(remote if isinstance(remote, dict) else {},
                                      old if isinstance(old, dict) else {}, new)
        else:
            result[key] = deepcopy(new)
    return result


def save_settings_delta(path: Path, baseline: dict, current: dict, *, resets=(), lock_timeout=3.0) -> bool:
    if baseline == current and not resets:
        return False
    with settings_lock(path, timeout=lock_timeout):
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
