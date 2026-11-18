"""Register/unregister TokenForge context actions in HKCU (no admin needed)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff')
ROOT = r'Software\Classes\SystemFileAssociations'
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


def install(base: Path = BASE) -> None:
    reg = _registry()
    pythonw, entry = interpreter_and_entry(base)
    for suffix in EXTENSIONS:
        for name, (label, action) in ACTION_NAMES.items():
            item = f'{ROOT}\\{suffix}\\shell\\{name}'
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, item, 0, reg.KEY_WRITE) as key:
                reg.SetValueEx(key, '', 0, reg.REG_SZ, label)
                reg.SetValueEx(key, 'MultiSelectModel', 0, reg.REG_SZ, 'Single')
                reg.SetValueEx(key, 'Icon', 0, reg.REG_SZ, str(pythonw))
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, item + r'\command', 0, reg.KEY_WRITE) as key:
                reg.SetValueEx(key, '', 0, reg.REG_SZ, command_for(pythonw, entry, action))


def remove() -> None:
    reg = _registry()
    for suffix in EXTENSIONS:
        for name in ACTION_NAMES:
            item = f'{ROOT}\\{suffix}\\shell\\{name}'
            for path in (item + r'\command', item):
                try:
                    reg.DeleteKey(reg.HKEY_CURRENT_USER, path)
                except FileNotFoundError:
                    pass


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
