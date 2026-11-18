"""CLI actions for Windows Explorer. Does not create the main GUI."""
from __future__ import annotations

from cutout_service import load_cutout
from settings_store import read_settings_file, adjustment_for, decode_settings
from local_models import DEFAULT_MODEL, require_local_model, new_local_session
import os
import traceback
from datetime import datetime
from pathlib import Path
from app_paths import preferences_dir

from token_engine import (
    IMAGE_EXTS, Adjust, DiskCutoutCache,
    export_batch, load_frame, list_source_images, read_image,
    remove_background, token_output_path, source_identity, read_export_owner,
    LoadedCutout, excluded_file, extraction_provenance,
)
from item_engine import item_output_path, export_items

BASE = Path(__file__).resolve().parent
PREFS_DIR = preferences_dir()
PREFS_FILE = PREFS_DIR / 'settings.json'
LOG_FILE = PREFS_DIR / 'context_actions.log'


def read_settings() -> dict:
    return read_settings_file(PREFS_FILE, strict=True)


def resolve_context_source(filename: str | Path, *, frame_path: Path | None = None) -> Path:
    """Resolve a selected Explorer file to its source if it is a generated copy.

    A generated .webp copy must map to the original PNG/JPEG where available,
    so its saved placement and cached extraction can be reused.
    """
    # Preserve the logical user-selected directory entry. Resolving a
    # symlink is useful for content identity, not for output naming.
    path = Path(os.path.abspath(Path(filename).expanduser()))
    if not path.is_file():
        raise FileNotFoundError(f'Файл не найден: {path}')
    if path.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f'Неподдерживаемый формат: {path.suffix}')
    # Use configured frame for every entry point, not just GUI discovery.
    if frame_path is None:
        # GUI open must remain usable for recovery from an invalid settings
        # file. Automatic commands pass their strictly read frame explicitly.
        frame_path = Path(decode_settings(read_settings_file(PREFS_FILE),
                                         default_frame=BASE / 'RingT.png')['ring'])
    if excluded_file(path, frame_path):
        raise ValueError('Выбранный файл используется как рамка, а не исходный арт.')
    owner = read_export_owner(path)
    if owner is None:
        return path
    if owner[0] >= 2:
        candidate = path.with_name(owner[2])
        if (candidate.suffix.lower() in IMAGE_EXTS and candidate.is_file()
                and not excluded_file(candidate, path) and read_export_owner(candidate) is None
                and not excluded_file(candidate, frame_path)):
            return candidate
        matches = []
    else:
        candidates = list_source_images(path.parent, exclude=frame_path)
        matches = [p for p in candidates if p.stem.casefold() == owner[2].casefold()]
    if len(matches) != 1:
        raise ValueError(f'Не удалось однозначно найти исходный арт для {path.name}.')
    return matches[0]


def _adjust_for(settings: dict, source: Path) -> Adjust:
    return adjustment_for(settings, source, 'token')


def make_cutout_loader(model, keep_alpha, edge_mode):
    require_local_model(model)
    cache = DiskCutoutCache(PREFS_DIR / 'cutouts')
    memory, sessions = {}, {}
    def get_cutout(path: Path, *, captured=None):
        identity = captured.identity if captured is not None else source_identity(path)
        key = (*identity, model, keep_alpha, edge_mode)
        decode = (lambda path, *, identity: captured.decode()) if captured is not None else read_image
        cutout = load_cutout(path, model, keep_alpha, edge_mode, key=key,
                            disk_cache=cache, memory=memory, sessions=sessions,
                            decode=decode, extract=remove_background, session_factory=new_local_session)
        return LoadedCutout(cutout.image, identity,
                            extraction_provenance(model, keep_alpha, edge_mode, revision=cutout.revision))

    return get_cutout


def automatic_token(filename: str | Path) -> tuple[str, Path]:
    """Create a single token with the saved settings and disk extraction cache.

    Returns (status, token_path), where status is 'created', 'skipped',
    'created_with_warning', or 'skipped_with_warning'. Fatal errors raise
    exceptions. Never loads the Tk GUI; inference is only used when needed.
    """
    settings = decode_settings(read_settings(), default_frame=BASE / 'RingT.png', strict=True, mode='token')
    frame_path = Path(settings['ring'])
    source = resolve_context_source(filename, frame_path=frame_path)


    model = settings.get('model', DEFAULT_MODEL)
    edge_mode = settings.get('edge_mode', 'Очистка краёв')
    keep_alpha = settings['keep_alpha']
    ext = settings['ext']
    size = settings['size']

    get_cutout = make_cutout_loader(model, keep_alpha, edge_mode)
    result = export_batch(
        [source], get_cutout, None, None, {source: _adjust_for(settings, source)},
        frame_loader=lambda: load_frame(frame_path),
        out_size=size, format_name=ext,
        skip_exists=settings['skip_exists'],
        convert_original=settings['convert_original'],
        extraction_recipe=lambda: extraction_provenance(model, keep_alpha, edge_mode),
        captured_loader=lambda capture: get_cutout(capture.path, captured=capture),
    )
    for warning in result.warnings:
        write_log(warning)
    if result.errors:
        for detail in result.error_details:
            write_log(detail)
        if not (result.created or result.skipped):
            raise RuntimeError('\n'.join(result.errors))
    destination = token_output_path(source, ext)
    if result.created:
        return ('created_with_warning' if result.errors or result.warnings else 'created'), destination
    if result.skipped and destination.is_file():
        return ('skipped_with_warning' if result.errors or result.warnings else 'skipped'), destination
    raise RuntimeError('Токен не был создан: неизвестный результат операции.')


def automatic_item(filename: str | Path) -> tuple[str, Path]:
    """Create 500x500 equipment art with automatic_token's status contract."""
    settings = decode_settings(read_settings(), default_frame=BASE / 'RingT.png', strict=True, mode='item')
    source = resolve_context_source(filename, frame_path=Path(settings['ring']))
    model = settings.get('model', DEFAULT_MODEL)
    edge_mode = settings.get('edge_mode', 'Очистка краёв')
    keep_alpha = settings['keep_alpha']
    adj = adjustment_for(settings, source, 'item')
    get_cutout = make_cutout_loader(model, keep_alpha, edge_mode)
    result = export_items([source], get_cutout, {source: adj},
                          skip_exists=settings['skip_exists'],
                          convert_original=settings['convert_original'],
                          extraction_recipe=lambda: extraction_provenance(model, keep_alpha, edge_mode),
                          captured_loader=lambda capture: get_cutout(capture.path, captured=capture))
    for warning in result.warnings:
        write_log(warning)
    if result.errors:
        for detail in result.error_details:
            write_log(detail)
        if not (result.created or result.skipped):
            raise RuntimeError('\n'.join(result.errors))
    destination = item_output_path(source)
    if result.created:
        return ('created_with_warning' if result.errors or result.warnings else 'created'), destination
    if result.skipped and destination.is_file():
        return ('skipped_with_warning' if result.errors or result.warnings else 'skipped'), destination
    raise RuntimeError('Предмет не был создан.')


def write_log(message: str, exception: BaseException | None = None) -> None:
    try:
        PREFS_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open('a', encoding='utf-8') as fp:
            fp.write(f'[{datetime.now().isoformat(timespec="seconds")}] {message}\n')
            if exception is not None:
                fp.write(''.join(traceback.format_exception(exception)))
    except OSError:
        pass


def notify_user(title: str, message: str, *, error: bool = False):
    """Brief unobtrusive notification; display a persistent dialog on errors."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        if error:
            messagebox.showerror(title, message, parent=root)
        else:
            toast = tk.Toplevel(root)
            toast.overrideredirect(True)
            toast.attributes('-topmost', True)
            panel = tk.Frame(toast, bg='#242932', padx=17, pady=13,
                             highlightbackground='#6689c7', highlightthickness=1)
            panel.pack(fill='both', expand=True)
            tk.Label(panel, text=title, fg='white', bg='#242932',
                     font=('Segoe UI Semibold', 11)).pack(anchor='w')
            tk.Label(panel, text=message, fg='#e3e7ed', bg='#242932',
                     font=('Segoe UI', 10), wraplength=350, justify='left').pack(anchor='w', pady=(4, 0))
            toast.update_idletasks()
            w, h = toast.winfo_reqwidth(), toast.winfo_reqheight()
            x = max(0, toast.winfo_screenwidth() - w - 24)
            y = max(0, toast.winfo_screenheight() - h - 78)
            toast.geometry(f'{w}x{h}+{x}+{y}')
            toast.after(3200, root.destroy)
            root.mainloop()
        if error:
            root.destroy()
    except Exception:
        # Logging is the fallback if GUI notification cannot be displayed.
        pass


def run_auto(filename: str | Path, *, item: bool = False) -> int:
    from runtime_streams import initialize_missing_streams
    initialize_missing_streams()
    try:
        status, destination = (automatic_item(filename) if item else automatic_token(filename))
        if status.endswith('_with_warning'):
            write_log(f'Результат доступен с предупреждениями: {destination}')
            notify_user('TokenForge: предупреждение',
                        f'Результат: {destination.name}. Обработка завершена с предупреждениями. Подробности: {LOG_FILE}',
                        error=True)
        elif status == 'created':
            write_log(f'Создан: {destination}')
            notify_user('TokenForge', f'Создан {"предмет" if item else "токен"}: {destination.name}')
        else:
            write_log(f'Пропущен (уже существует): {destination}')
            notify_user('TokenForge', f'Файл уже существует: {destination.name}')
        return 0
    except Exception as exc:
        write_log(f'ОШИБКА для {filename}: {exc}', exc)
        notify_user('Ошибка TokenForge', f'{exc}\n\nПодробности: {LOG_FILE}', error=True)
        return 1
