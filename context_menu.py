"""Explorer integration via owned SendTo shortcuts, not shared registry branches."""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from process_lock import process_lock

BASE = Path(__file__).resolve().parent
MENU_LOCK = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.config')) / 'TokenForge' / 'context-menu'
EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff')
CLASSES = r'Software\Classes'
ROOT = CLASSES + r'\*\shell'
LEGACY_ROOT = CLASSES + r'\SystemFileAssociations'
ACTION_NAMES = {
    'TokenForge.Create': ('Сделать автоматический токен', '--auto'),
    'TokenForge.Open': ('Открыть в TokenForge', '--open'),
    'TokenForge.CreateItem': ('Сделать арт предмета', '--auto-item'),
    'TokenForge.OpenItem': ('Открыть предмет в TokenForge', '--open-item'),
}
LINK_OWNER = 'TokenForge.SendTo.v1'


def interpreter_and_entry(base: Path = BASE) -> tuple[Path, Path]:
    pythonw = base / '.venv' / 'Scripts' / 'pythonw.exe'
    entry = base / 'sendto_entry.py'
    if not pythonw.is_file():
        raise FileNotFoundError('Не найден .venv\\Scripts\\pythonw.exe. Сначала запусти install.bat.')
    if not entry.is_file() or not (base / 'tokenforge.py').is_file():
        raise FileNotFoundError('Не найдены файлы TokenForge. Обнови программу целиком.')
    return pythonw, entry


def _powershell(script, **variables):
    # Values are passed as data by environment, never interpolated into code.
    prefix = "$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    encoded = base64.b64encode((prefix + script).encode('utf-16-le')).decode('ascii')
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            env={**os.environ, **{f'TF_{k}': str(v) for k, v in variables.items()}},
                            capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise OSError(result.stderr.decode('utf-8', errors='replace').strip() or 'PowerShell shortcut operation failed')
    return result.stdout.decode('utf-8-sig').rstrip('\r\n')


def _sendto_folder():
    folder = _powershell("Write-Output ([Environment]::GetFolderPath([Environment+SpecialFolder]::SendTo))")
    if not folder or not Path(folder).is_dir():
        raise OSError('Не найдена папка Windows SendTo.')
    return Path(folder)


def shortcut_arguments(entry, flag):
    if flag not in {action for _, action in ACTION_NAMES.values()}:
        raise ValueError('Unknown action')
    return f'"{entry}" {flag}'


def _write_shortcut(path, pythonw, entry, flag):
    _powershell(r"""
$s = New-Object -ComObject WScript.Shell
$l = $s.CreateShortcut($env:TF_LINK)
$l.TargetPath = $env:TF_TARGET
$l.Arguments = $env:TF_ARGS
$l.WorkingDirectory = $env:TF_DIR
$l.Description = $env:TF_OWNER
$l.Save()
""", LINK=path, TARGET=pythonw, ARGS=shortcut_arguments(entry, flag),
        DIR=entry.parent, OWNER=LINK_OWNER)


def _shortcut_metadata(path):
    # PowerShell object output, rather than Console.WriteLine overloads,
    # is consistently captured by the host in CI and desktop sessions.
    data = _powershell(r"""
$s = New-Object -ComObject WScript.Shell
$l = $s.CreateShortcut($env:TF_LINK)
[pscustomobject]@{
    description = [string]$l.Description
    target = [string]$l.TargetPath
    arguments = [string]$l.Arguments
} | ConvertTo-Json -Compress
""", LINK=path)
    return json.loads(data)


def _owned_shortcut(path):
    try:
        info = _shortcut_metadata(path)
    except (OSError, ValueError):
        return False
    return info.get('description') == LINK_OWNER


def shortcut_paths(folder):
    return {folder / f'TokenForge - {label}.lnk': flag
            for label, flag in ACTION_NAMES.values()}


def _install_shortcuts(folder, pythonw, entry):
    # Stage all links before replacing any. Existing unrelated shortcuts
    # with these names are a conflict, not permission to overwrite.
    paths = shortcut_paths(folder)
    prior = {}
    for path in paths:
        if path.exists():
            if not _owned_shortcut(path):
                raise FileExistsError(f'Имя ярлыка уже занято: {path}')
            prior[path] = path.read_bytes()
        else:
            prior[path] = None
    committed = []
    with tempfile.TemporaryDirectory(prefix='.tokenforge-', dir=folder) as temporary:
        stage = Path(temporary)
        for number, (path, flag) in enumerate(paths.items()):
            _write_shortcut(stage / f'{number}.lnk', pythonw, entry, flag)
        try:
            for number, path in enumerate(paths):
                staged = stage / f'{number}.lnk'
                content = staged.read_bytes()
                previous = prior[path]
                if previous is None:
                    # Windows rename is no-clobber. The tests also exercise POSIX.
                    if os.name == 'nt':
                        staged.rename(path)
                    else:
                        os.link(staged, path)
                else:
                    if path.read_bytes() != previous:
                        raise FileExistsError(f'Ярлык изменился во время установки: {path}')
                    os.replace(staged, path)
                committed.append((path, content))
        except Exception as original:
            errors = []
            for path, content in reversed(committed):
                try:
                    if path.read_bytes() != content:
                        raise FileExistsError(f'Ярлык изменился: {path}')
                    if prior[path] is None:
                        path.unlink()
                    else:
                        restore = stage / 'restore.lnk'
                        restore.write_bytes(prior[path])
                        os.replace(restore, path)
                except OSError as exc:
                    errors.append(str(exc))
            if errors:
                raise RuntimeError(f'{original}\nRollback was incomplete:\n' + '\n'.join(errors)) from original
            raise


def _registry():
    if os.name != 'nt':
        raise OSError('Контекстное меню доступно только в Windows.')
    import winreg
    return winreg


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


def _cleanup_previous():
    # Migration ONLY: remove the old, explicitly named TokenForge verbs.
    # Never create or delete wildcard/shared parent keys. No registry writes
    # are needed for new SendTo integration.
    reg = _registry()
    errors = []
    roots = [ROOT] + [LEGACY_ROOT + '\\' + ext + r'\shell' for ext in EXTENSIONS]
    for root in roots:
        for name in ACTION_NAMES:
            path = root + '\\' + name
            try:
                _delete_tree(reg, path)
            except OSError as exc:
                errors.append(f'{path}: {exc}')
    return errors


def install(base: Path = BASE) -> list[str]:
    with process_lock(MENU_LOCK, timeout=30):
        pythonw, entry = interpreter_and_entry(base)
        _install_shortcuts(_sendto_folder(), pythonw, entry)
        return _cleanup_previous()


def remove() -> None:
    with process_lock(MENU_LOCK, timeout=30):
        errors = []
        try:
            for path in shortcut_paths(_sendto_folder()):
                try:
                    if path.exists() and _owned_shortcut(path):
                        path.unlink()
                except OSError as exc:
                    errors.append(f'{path}: {exc}')
        except OSError as exc:
            errors.append(str(exc))
        errors.extend(_cleanup_previous())
        if errors:
            raise RuntimeError('Some TokenForge entries could not be removed:\n' + '\n'.join(errors))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='TokenForge Windows SendTo setup')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--install', action='store_true')
    modes.add_argument('--remove', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.install:
            warnings = install()
            print('OK: TokenForge actions installed in Explorer Send to.')
            for warning in warnings:
                print(f'WARNING: Old registration cleanup: {warning}', file=sys.stderr)
            print('Windows 11: Show more options (Shift+F10) > Send to.')
        else:
            remove()
            print('OK: TokenForge entries removed.')
        return 0
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
