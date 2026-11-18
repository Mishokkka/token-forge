"""Register/unregister TokenForge context actions in HKCU (no admin needed)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from process_lock import process_lock

BASE = Path(__file__).resolve().parent
MENU_LOCK = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.config')) / 'TokenForge' / 'context-menu'
EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff')
CLASSES = r'Software\Classes'
ROOT = CLASSES + r'\*\shell'
LEGACY_ROOT = CLASSES + r'\SystemFileAssociations'
APPLIES_TO = ' OR '.join('System.FileExtension:=' + ext for ext in EXTENSIONS)
ACTION_NAMES = {
    'TokenForge.Create': ('Сделать автоматический токен', '--auto'),
    'TokenForge.Open': ('Открыть в TokenForge', '--open'),
    'TokenForge.CreateItem': ('Сделать арт предмета', '--auto-item'),
    'TokenForge.OpenItem': ('Открыть предмет в TokenForge', '--open-item'),
}


def interpreter_and_entry(base: Path = BASE) -> tuple[Path, Path]:
    pythonw = base / '.venv' / 'Scripts' / 'pythonw.exe'
    entry = base / 'tokenforge.py'
    if not pythonw.is_file():
        raise FileNotFoundError('Не найден .venv\\Scripts\\pythonw.exe. Сначала запусти install.bat.')
    if not entry.is_file():
        raise FileNotFoundError('Не найден tokenforge.py в папке программы.')
    return pythonw, entry


def command_for(pythonw: Path, script: Path, action: str) -> str:
    if action not in ('--auto', '--open', '--auto-item', '--open-item'):
        raise ValueError('Unknown action')
    return f'"{pythonw}" "{script}" {action} "%1"'


def _registry():
    if os.name != 'nt':
        raise OSError('Контекстное меню можно установить только в Windows.')
    import winreg
    return winreg


def _notify_shell() -> None:
    if os.name == 'nt':
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


class RegistryConflictError(RuntimeError):
    """The per-user registration would hide a foreign system verb."""


def _snapshot_tree(reg, hive, path, *, exclude=()):
    """Read the key/value tree (including empty keys); absent root is {}."""
    result = {}
    excluded = {name.casefold() for name in exclude}

    def visit(full, relative):
        try:
            key = reg.OpenKey(hive, full, 0, reg.KEY_READ)
        except FileNotFoundError:
            return
        with key:
            children, values, _ = reg.QueryInfoKey(key)
            result[relative] = {name: (value, kind) for name, value, kind in
                                (reg.EnumValue(key, i) for i in range(values))}
            names = [reg.EnumKey(key, i) for i in range(children)]
        for name in names:
            if not relative and name.casefold() in excluded:
                continue
            child = relative + '\\' + name if relative else name
            visit(full + '\\' + name, child)

    visit(path, '')
    return result


def _delete_tree(reg, path):
    # Only called for the four owned TokenForge verb subtrees.
    tree = _snapshot_tree(reg, reg.HKEY_CURRENT_USER, path)
    for relative in sorted(tree, key=lambda key: key.count('\\') + bool(key), reverse=True):
        try:
            full = path + '\\' + relative if relative else path
            reg.DeleteKey(reg.HKEY_CURRENT_USER, full)
        except FileNotFoundError:
            pass


def _key_values(reg, path):
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_READ) as key:
            count = reg.QueryInfoKey(key)[1]
            return {name: (value, kind) for name, value, kind in
                    (reg.EnumValue(key, i) for i in range(count))}
    except FileNotFoundError:
        return None


_MISSING = object()


def _query_value(reg, path, name):
    # QueryValueEx follows Windows' case-insensitive value-name semantics.
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_READ) as key:
            return reg.QueryValueEx(key, name)
    except FileNotFoundError:
        return _MISSING


class RegistryEdits:
    """Journal successful writes to our private verb keys, not shared parents."""
    def __init__(self, reg):
        self.reg = reg
        self.created = []
        self.changed = []

    def write(self, path, values):
        reg = self.reg
        before = _key_values(reg, path)
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_WRITE) as key:
            if before is None:
                self.created.append(path)
                before = {}
            for name, value in values.items():
                old = _query_value(reg, path, name)
                new = (value, reg.REG_SZ)
                if old != new:
                    reg.SetValueEx(key, name, 0, reg.REG_SZ, value)
                    self.changed.append((path, name, old, new))

    def rollback(self):
        errors = []
        reg = self.reg
        for path, name, old, new in reversed(self.changed):
            try:
                if _query_value(reg, path, name) != new:
                    raise RegistryConflictError('Value changed during installation')
                with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_WRITE) as key:
                    if old is _MISSING:
                        reg.DeleteValue(key, name)
                    else:
                        reg.SetValueEx(key, name, 0, old[1], old[0])
            except Exception as exc:
                errors.append(f'{path} [{name}]: {exc}')
        for path in reversed(self.created):
            try:
                # Never remove an existing key or a shared shell/extension key.
                with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_READ) as key:
                    children, values, _ = reg.QueryInfoKey(key)
                if children or values:
                    raise RegistryConflictError('New verb key contains unexpected entries')
                reg.DeleteKey(reg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                errors.append(f'{path}: {exc}')
        return errors


def _remove_verbs(reg, root):
    errors = []
    for name in ACTION_NAMES:
        path = root + '\\' + name
        try:
            _delete_tree(reg, path)
        except Exception as exc:
            errors.append(f'{path}: {exc}')
    return errors


def _remove_legacy(reg):
    errors = []
    for suffix in EXTENSIONS:
        errors.extend(_remove_verbs(reg, LEGACY_ROOT + '\\' + suffix + r'\shell'))
    return errors


def _foreign_shell(reg):
    # Verify the actual merged view instead of assuming a merge rule.
    return _snapshot_tree(reg, reg.HKEY_CLASSES_ROOT, r'*\shell',
                          exclude=ACTION_NAMES)


def _check_safe_registration(reg):
    # Microsoft documents that a per-user *\shell can shadow its machine
    # counterpart. Do not create that shared parent if foreign machine data
    # could be hidden. There is no atomic "delete only if empty" operation
    # for shared registry parents we could safely use to undo its creation.
    before = _foreign_shell(reg)
    if _key_values(reg, ROOT) is None:
        machine = _snapshot_tree(reg, reg.HKEY_LOCAL_MACHINE, ROOT)
        if any(relative or values for relative, values in machine.items()):
            raise RegistryConflictError(
                'Per-user registration could hide existing machine-wide shell commands. '
                'No registry changes were made. Existing menu entries remain.')
    return before


def install(base: Path = BASE) -> list[str]:
    with process_lock(MENU_LOCK, timeout=15):
        return _install(base)


def _install(base: Path) -> list[str]:
    reg = _registry()
    pythonw, entry = interpreter_and_entry(base)
    foreign_before = _check_safe_registration(reg)
    edits = RegistryEdits(reg)
    try:
        for name, (label, action) in ACTION_NAMES.items():
            item = ROOT + '\\' + name
            edits.write(item, {'': label, 'MultiSelectModel': 'Single',
                               'Icon': str(pythonw), 'AppliesTo': APPLIES_TO,
                               'NeverDefault': ''})
            edits.write(item + r'\command', {'': command_for(pythonw, entry, action)})
        after = _foreign_shell(reg)
        # A newly created empty shared shell key is not itself a foreign verb.
        if any(after.get(relative) != values for relative, values in foreign_before.items()):
            raise RegistryConflictError('Windows would hide or change existing context-menu commands.')
    except Exception as original:
        errors = edits.rollback()
        _notify_shell()
        if errors:
            raise RuntimeError(f'{original}\nRollback was incomplete:\n' +
                               '\n'.join(errors)) from original
        raise

    # Registration is already committed. A legacy cleanup failure is a
    # warning, not an install failure; retrying installation retries cleanup.
    try:
        return _remove_legacy(reg)
    finally:
        _notify_shell()


def remove() -> None:
    with process_lock(MENU_LOCK, timeout=15):
        _remove()


def _remove() -> None:
    reg = _registry()
    try:
        errors = _remove_verbs(reg, ROOT) + _remove_legacy(reg)
        if errors:
            raise RuntimeError('Some TokenForge entries could not be removed:\n' +
                               '\n'.join(errors))
        # Shared parents may belong to Windows or another app, including
        # empty keys. Never prune them based on a non-atomic count.
    finally:
        _notify_shell()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='TokenForge Windows context menu setup')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--install', action='store_true')
    modes.add_argument('--remove', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.install:
            warnings = install()
            print('OK: Context menu installed for current Windows user.')
            if warnings:
                print('WARNING: Some old TokenForge entries could not be removed. '
                      'Rerun setup to retry cleanup.', file=sys.stderr)
                for warning in warnings:
                    print(warning, file=sys.stderr)
            print('Windows 11: items may be under "Show more options" (Shift+F10).')
        else:
            remove()
            print('OK: TokenForge context-menu entries removed.')
        return 0
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
