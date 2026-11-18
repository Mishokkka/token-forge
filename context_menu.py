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
from app_paths import preferences_dir

from process_lock import process_lock

BASE = Path(__file__).resolve().parent
MENU_LOCK = preferences_dir() / 'context-menu'
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
    try:
        result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                env={**os.environ, **{f'TF_{k}': str(v) for k, v in variables.items()}},
                                capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OSError('Истекло время ожидания Windows PowerShell.') from exc
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


# Use the documented Unicode Shell Link interface rather than WScript's
# automation wrapper, which can misread links with non-ANSI filenames.
SHELL_LINK_TYPE = r"""
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path,
                 int size, IntPtr findData, uint flags);
    void GetIDList(out IntPtr idList);
    void SetIDList(IntPtr idList);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int size);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int size);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int size);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
    void GetHotkey(out short key);
    void SetHotkey(short key);
    void GetShowCmd(out int command);
    void SetShowCmd(int command);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}
public static class TokenForgeShellLink {
    static object Create() {
        return Activator.CreateInstance(Type.GetTypeFromCLSID(
            new Guid("00021401-0000-0000-C000-000000000046")));
    }
    public static void Write(string file, string target, string args, string dir, string owner) {
        object instance = Create();
        try {
            IShellLinkW link = (IShellLinkW)instance;
            link.SetPath(target);
            link.SetArguments(args);
            link.SetWorkingDirectory(dir);
            link.SetDescription(owner);
            ((IPersistFile)instance).Save(file, true);
        } finally {
            Marshal.FinalReleaseComObject(instance);
        }
    }
    public static string[] Read(string file) {
        object instance = Create();
        try {
            ((IPersistFile)instance).Load(file, 0);
            IShellLinkW link = (IShellLinkW)instance;
            var desc = new StringBuilder(32768);
            var path = new StringBuilder(32768);
            var args = new StringBuilder(32768);
            link.GetDescription(desc, desc.Capacity);
            link.GetPath(path, path.Capacity, IntPtr.Zero, 4);
            link.GetArguments(args, args.Capacity);
            return new string[] {desc.ToString(), path.ToString(), args.ToString()};
        } finally {
            Marshal.FinalReleaseComObject(instance);
        }
    }
}
"""


def _shell_link(script, **variables):
    return _powershell("Add-Type -TypeDefinition @'\n" + SHELL_LINK_TYPE +
                       "\n'@\n" + script, **variables)


def _write_shortcut(path, pythonw, entry, flag):
    _shell_link(r"""
[TokenForgeShellLink]::Write($env:TF_LINK, $env:TF_TARGET, $env:TF_ARGS, $env:TF_DIR, $env:TF_OWNER)
""", LINK=path, TARGET=pythonw, ARGS=shortcut_arguments(entry, flag),
        DIR=entry.parent, OWNER=LINK_OWNER)


def _shortcut_metadata(path):
    data = _shell_link(r"""
$result = [TokenForgeShellLink]::Read($env:TF_LINK)
[pscustomobject]@{
    description = $result[0]
    target = $result[1]
    arguments = $result[2]
} | ConvertTo-Json -Compress
""", LINK=path)
    try:
        info = json.loads(data)
        if not isinstance(info, dict) or not isinstance(info.get('description'), str):
            raise ValueError('Invalid shortcut metadata')
        return info
    except ValueError as exc:
        raise OSError(f'Не удалось прочитать ярлык: {path}') from exc


def _owned_shortcut(path):
    return _shortcut_metadata(path).get('description') == LINK_OWNER


def shortcut_paths(folder):
    return {folder / f'TokenForge - {label}.lnk': flag
            for label, flag in ACTION_NAMES.values()}


def _read_owned_bytes(path, stage):
    """Inspect immutable captured bytes, not a second read of a mutable name."""
    if path.is_symlink():
        raise FileExistsError(f'Ярлык является символической ссылкой: {path}')
    content = path.read_bytes()
    snapshot = stage / 'inspect.lnk'
    snapshot.write_bytes(content)
    return content if _owned_shortcut(snapshot) else None


def _publish_no_clobber(source, destination):
    if os.name == 'nt':
        source.rename(destination)
    else:
        os.link(source, destination, follow_symlinks=False)
        source.unlink()


def _restore_detached(backup, destination):
    try:
        _publish_no_clobber(backup, destination)
    except OSError as exc:
        raise OSError(f'Не удалось восстановить {destination}. Сохранён файл: {backup}: {exc}') from exc


def _detach_expected(path, content, recovery):
    """Inspect the file actually displaced, and never delete a foreign replacement."""
    import uuid
    backup = recovery / (uuid.uuid4().hex + '.lnk')
    path.rename(backup)
    try:
        matches = not backup.is_symlink() and backup.is_file() and backup.read_bytes() == content
    except OSError as exc:
        _restore_detached(backup, path)
        raise OSError(f'Не удалось проверить перемещённый ярлык: {path}') from exc
    if not matches:
        _restore_detached(backup, path)
        raise FileExistsError(f'Ярлык изменился во время операции и восстановлен: {path}')
    return backup


def _discard_detached(backup):
    # Only private, verified displaced copies reach this function.
    if backup is not None:
        backup.unlink()


def _recovery_dir(folder):
    # Sibling of SendTo, not inside its visible menu. Preserve any nonempty
    # directory on error instead of deleting unknown displaced data.
    return Path(tempfile.mkdtemp(prefix='.tokenforge-recovery-', dir=folder.parent))


def _remove_empty_recovery(recovery):
    try:
        recovery.rmdir()
    except OSError:
        pass


def _install_shortcuts(folder, pythonw, entry):
    paths = shortcut_paths(folder)
    with tempfile.TemporaryDirectory(prefix='tokenforge-stage-') as temporary:
        stage = Path(temporary)
        prior = {}
        # Snapshot ownership of all destinations before touching any of them.
        for path in paths:
            if os.path.lexists(path):
                content = _read_owned_bytes(path, stage)
                if content is None:
                    raise FileExistsError(f'Имя ярлыка уже занято: {path}')
                prior[path] = content
            else:
                prior[path] = None
        recovery = _recovery_dir(folder)
        committed = []
        try:
            # Stage on the destination volume for no-clobber rename.
            staged_links = {}
            for number, (path, flag) in enumerate(paths.items()):
                staged = recovery / f'new-{number}.lnk'
                _write_shortcut(staged, pythonw, entry, flag)
                staged_links[path] = staged
            for path, staged in staged_links.items():
                content = staged.read_bytes()
                previous = prior[path]
                backup = _detach_expected(path, previous, recovery) if previous is not None else None
                try:
                    _publish_no_clobber(staged, path)
                except OSError:
                    if backup is not None:
                        _restore_detached(backup, path)
                    raise
                committed.append((path, content, backup))
        except Exception as original:
            errors = []
            for path, content, backup in reversed(committed):
                try:
                    installed = _detach_expected(path, content, recovery)
                    try:
                        if backup is not None:
                            _restore_detached(backup, path)
                    finally:
                        _discard_detached(installed)
                except OSError as exc:
                    errors.append(str(exc))
            if errors:
                raise RuntimeError(f'{original}\nRollback was incomplete:\n' +
                                   '\n'.join(errors) + f'\nRecovery: {recovery}') from original
            # Keep any displaced backup that could not be restored.
            raise
        else:
            warnings = []
            for _, _, backup in committed:
                try:
                    _discard_detached(backup)
                except OSError as exc:
                    warnings.append(f'Не удалось удалить резервный ярлык {backup}: {exc}')
            return warnings
        finally:
            # Only known staged new files are disposable, never remaining backups.
            for staged in recovery.glob('new-*.lnk'):
                try:
                    staged.unlink()
                except OSError:
                    pass
            _remove_empty_recovery(recovery)


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
        warnings = _install_shortcuts(_sendto_folder(), pythonw, entry)
        return warnings + _cleanup_previous()


def remove() -> None:
    with process_lock(MENU_LOCK, timeout=30):
        errors = []
        try:
            folder = _sendto_folder()
            with tempfile.TemporaryDirectory(prefix='tokenforge-inspect-') as temporary:
                stage = Path(temporary)
                recovery = _recovery_dir(folder)
                try:
                    for path in shortcut_paths(folder):
                        try:
                            if not os.path.lexists(path):
                                continue
                            content = _read_owned_bytes(path, stage)
                            if content is not None:
                                _discard_detached(_detach_expected(path, content, recovery))
                        except OSError as exc:
                            errors.append(f'{path}: {exc}')
                finally:
                    _remove_empty_recovery(recovery)
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
                print(f'WARNING: {warning}', file=sys.stderr)
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
