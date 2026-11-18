"""Register/unregister TokenForge context actions in HKCU (no admin needed)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
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


def _restore_tree(reg, path, tree):
    for relative, values in tree.items():
        full = path + '\\' + relative if relative else path
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, full, 0, reg.KEY_WRITE) as key:
            for name, (value, kind) in values.items():
                reg.SetValueEx(key, name, 0, kind, value)


def _prune_empty(reg, path):
    """Leave any ancestor containing other registrations or values untouched."""
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_READ) as key:
            children, values, _ = reg.QueryInfoKey(key)
        if children == 0 and values == 0:
            reg.DeleteKey(reg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def _remove_legacy(reg):
    for suffix in EXTENSIONS:
        ext = LEGACY_ROOT + '\\' + suffix
        shell = ext + r'\shell'
        for name in ACTION_NAMES:
            _delete_tree(reg, shell + '\\' + name)
        _prune_empty(reg, shell)
        _prune_empty(reg, ext)
    _prune_empty(reg, LEGACY_ROOT)


def _foreign_shell(reg):
    # Verify the actual merged view instead of assuming a merge rule.
    return _snapshot_tree(reg, reg.HKEY_CLASSES_ROOT, r'*\shell',
                          exclude=ACTION_NAMES)


def install(base: Path = BASE) -> None:
    reg = _registry()
    pythonw, entry = interpreter_and_entry(base)
    foreign_before = _foreign_shell(reg)
    previous = {name: _snapshot_tree(reg, reg.HKEY_CURRENT_USER, ROOT + '\\' + name)
                for name in ACTION_NAMES}
    had_shell = bool(_snapshot_tree(reg, reg.HKEY_CURRENT_USER, ROOT))
    star = CLASSES + r'\*'
    had_star = bool(_snapshot_tree(reg, reg.HKEY_CURRENT_USER, star))

    try:
        for name, (label, action) in ACTION_NAMES.items():
            item = ROOT + '\\' + name
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, item, 0, reg.KEY_WRITE) as key:
                reg.SetValueEx(key, '', 0, reg.REG_SZ, label)
                reg.SetValueEx(key, 'MultiSelectModel', 0, reg.REG_SZ, 'Single')
                reg.SetValueEx(key, 'Icon', 0, reg.REG_SZ, str(pythonw))
                reg.SetValueEx(key, 'AppliesTo', 0, reg.REG_SZ, APPLIES_TO)
                reg.SetValueEx(key, 'NeverDefault', 0, reg.REG_SZ, '')
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, item + r'\command', 0, reg.KEY_WRITE) as key:
                reg.SetValueEx(key, '', 0, reg.REG_SZ, command_for(pythonw, entry, action))

        after = _foreign_shell(reg)
        if any(relative not in after or after[relative] != values
               for relative, values in foreign_before.items()):
            raise RegistryConflictError(
                'Windows would hide existing context-menu commands. '
                'Registration was rolled back; existing menu entries were preserved.')
    except Exception:
        for name, tree in previous.items():
            item = ROOT + '\\' + name
            _delete_tree(reg, item)
            _restore_tree(reg, item, tree)
        if not had_shell:
            _prune_empty(reg, ROOT)
        if not had_star:
            _prune_empty(reg, star)
        _notify_shell()
        raise

    # Only migrate old registrations once the safer new registration succeeds.
    _remove_legacy(reg)
    _notify_shell()


def remove() -> None:
    reg = _registry()
    for name in ACTION_NAMES:
        _delete_tree(reg, ROOT + '\\' + name)
    _prune_empty(reg, ROOT)
    _prune_empty(reg, CLASSES + r'\*')
    _remove_legacy(reg)
    _notify_shell()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='TokenForge Windows context menu setup')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--install', action='store_true')
    modes.add_argument('--remove', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.install:
            install()
            print('OK: Context menu installed for current Windows user.')
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
