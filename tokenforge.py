"""TokenForge: local Windows-friendly GUI for batch RPG token creation."""
from __future__ import annotations

from runtime_streams import initialize_missing_streams

# Explorer launches this entry point via pythonw.exe, without console streams.
if __name__ == '__main__':
    initialize_missing_streams()

import argparse
from copy import deepcopy
import os
import queue
import threading
import traceback
from pathlib import Path
from app_paths import preferences_dir
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageDraw, ImageTk

from token_engine import (
    Adjust, DiskCutoutCache, compose, frame_interior,
    export_batch, list_source_images, read_image, remove_background, token_output_path,
    source_identity, assert_source_identity, is_owned_export, LoadedCutout, excluded_file, read_owned_export_image,
    file_stamp, EXPECTED_ABSENT,
)
from item_engine import ItemAdjust, ITEM_MODES, compose_item, export_items, item_output_path
from settings_store import (read_settings_file, save_settings_delta, adjustment_for,
                            override_for, output_format, valid_layout, TOKEN_RANGES, ITEM_RANGES)
from local_models import LOCAL_MODELS, DEFAULT_MODEL, new_local_session
from cutout_service import load_cutout

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    RootClass = TkinterDnD.Tk
except ImportError:
    DND_FILES = None
    RootClass = tk.Tk

BASE = Path(__file__).resolve().parent
PREFS_DIR = preferences_dir()
PREFS_FILE = PREFS_DIR / 'settings.json'

BG = '#17191e'
PANEL = '#22252b'
INPUT = '#2a2e35'
FG = '#e9ecf1'
MUTED = '#a0a6b1'
BLUE = '#789fff'
MODELS = LOCAL_MODELS
EDGE_MODES = ['Очистка краёв', 'Обычный', 'Alpha matting (медленно)']


def settings_read():
    return read_settings_file(PREFS_FILE)


def checkerboard(size: int) -> Image.Image:
    im = Image.new('RGB', (size, size), '#d9dce2')
    draw = ImageDraw.Draw(im)
    cell = 18
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if (x // cell + y // cell) % 2:
                draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill='#b7bdc7')
    return im.convert('RGBA')


class TokenForge:
    def __init__(self, startup_file: Path | None = None, startup_mode: str | None = None):
        self.root = RootClass()
        self.root.title('TokenForge 1.7.3 | Tokens & Items')
        self.root.geometry('1070x785')
        self.root.minsize(945, 730)
        self.root.configure(bg=BG)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.style = ttk.Style(self.root)
        self.style.theme_use('clam')
        self.style.configure('TButton', background=INPUT, foreground=FG, padding=(13, 8), borderwidth=0)
        self.style.map('TButton', background=[('active', '#3c4350')])
        self.style.configure('Accent.TButton', background=BLUE, foreground='#11141b', padding=(16, 9))
        self.style.map('Accent.TButton', background=[('active', '#9bb8ff')])
        self.style.configure('TCombobox', fieldbackground=INPUT, background=INPUT, foreground=FG)
        self.style.map('TCombobox', fieldbackground=[('readonly', INPUT)], foreground=[('readonly', FG)],
                       selectbackground=[('readonly', INPUT)], selectforeground=[('readonly', FG)])
        self.style.configure('Horizontal.TProgressbar', troughcolor=INPUT, background=BLUE)

        self.settings = settings_read()
        for name in ('overrides', 'item_overrides'):
            if not isinstance(self.settings.get(name), dict):
                self.settings[name] = {}
        self.folder = None  # last successfully scanned folder
        self.folder_preference = self.settings.get('folder') or ''
        self.frame_path = Path(self.settings.get('ring') or BASE / 'RingT.png')
        self.frame = None
        self.interior = None
        self.files = []
        self.selected = None
        self.current_cutout = None
        self.current_key = None
        self.cache = {}
        self.disk_cache = DiskCutoutCache(PREFS_DIR / 'cutouts')
        self.sessions = {}
        self.busy = False
        self.approval_pending = False
        self.running_batch = False
        self.preview_pending = False
        self.pending_export = None
        self.preview_generation = 0
        self.preview_cached_only = False
        self.preview_retry = False
        self.scan_running = False
        self.scan_generation = 0
        self.pending_scan = None
        self.active_scan = None
        self.pending_resets = set()
        self.save_retries = 0
        self.save_retryable = False
        self.worker = None
        self.closing = False
        self.widget_values = {}
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.sliders_updating = False
        self.save_timer = None

        saved_model = self.settings.get('model', DEFAULT_MODEL)
        self.model = tk.StringVar(value=saved_model if saved_model in MODELS else DEFAULT_MODEL)
        self.edge_mode = tk.StringVar(value=self.settings.get('edge_mode', EDGE_MODES[0]))
        self.show_cutout = tk.BooleanVar(value=False)
        saved_size = str(self.settings.get('size', 256))
        self.size = tk.StringVar(value=saved_size if saved_size in ('256', '512', '1024') else '256')
        self.ext = tk.StringVar(value=output_format(self.settings.get('ext', 'webp')))
        self.keep_alpha = tk.BooleanVar(value=self.settings.get('keep_alpha', True))
        self.skip_exists = tk.BooleanVar(value=self.settings.get('skip_exists', True))
        self.convert_original = tk.BooleanVar(value=self.settings.get('convert_original', True))
        self.work_mode = tk.StringVar(value=startup_mode or self.settings.get('work_mode', 'token'))
        if self.work_mode.get() not in ('token', 'item'):
            self.work_mode.set('token')
        default_layout = self.settings.get('item_layout', ITEM_MODES[0])
        if default_layout not in ITEM_MODES:
            default_layout = ITEM_MODES[0]
        self.default_item_layout = tk.StringVar(value=default_layout)
        self.item_layout = tk.StringVar(value=default_layout)
        self.item_zoom = tk.DoubleVar(value=100)
        self.item_x = tk.DoubleVar(value=0)
        self.item_y = tk.DoubleVar(value=0)
        self.item_rotation = tk.DoubleVar(value=0)
        self.item_flip_horizontal = tk.BooleanVar(value=False)
        self.item_flip_vertical = tk.BooleanVar(value=False)
        self.item_clean_noise = tk.BooleanVar(value=True)
        self.zoom = tk.DoubleVar(value=100)
        self.x = tk.DoubleVar(value=0)
        self.y = tk.DoubleVar(value=0)
        self.gap = tk.DoubleVar(value=11)
        self.status = tk.StringVar(value='Выбери папку с артами.')
        self.folder_label = tk.StringVar(value='Папка не выбрана')
        self.ring_label = tk.StringVar(value='')
        self.preview_label = tk.StringVar(value='Выбери изображение слева')

        self._build()
        self._on_work_mode_change(initial=True)
        try:
            self._set_frame(self.frame_path)
        except Exception as exc:
            self.status.set(f'Не удалось открыть рамку: {exc}')
        self._settings_baseline = self._settings_snapshot()
        if startup_file is not None:
            self._set_folder(startup_file.parent, select_path=startup_file)
        else:
            last_folder = self.folder_preference
            if last_folder and Path(last_folder).is_dir():
                self._set_folder(Path(last_folder), persist=False)
        self.root.after(120, self._poll)
        self.root.after(2500, self._watch_source)
        if DND_FILES is not None:
            for widget in (self.root, self.file_list):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self._on_drop)

    def _label(self, parent, text, font=None, color=FG, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color, font=font, **kwargs)

    def _build(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill='x', padx=24, pady=(19, 15))
        self._label(header, 'TOKENFORGE', font=('Segoe UI Semibold', 22)).pack(side='left')
        self._label(header, 'Арты для Foundry VTT', font=('Segoe UI', 10), color=MUTED).pack(side='left', padx=18, pady=(10, 0))
        mode_panel = tk.Frame(header, bg=BG)
        mode_panel.pack(side='right', pady=(4, 0))
        for label, value in [('Токены', 'token'), ('Предметы', 'item')]:
            tk.Radiobutton(mode_panel, text=label, value=value, variable=self.work_mode,
                           command=self._on_work_mode_change, bg=BG, fg=FG,
                           activebackground=BG, activeforeground=BLUE,
                           selectcolor=INPUT, font=('Segoe UI Semibold', 11),
                           padx=8).pack(side='left')

        filebar = tk.Frame(self.root, bg=BG)
        filebar.pack(fill='x', padx=24, pady=(0, 12))
        ttk.Button(filebar, text='Папка с артами', command=self._choose_folder).pack(side='left')
        self.ring_button = ttk.Button(filebar, text='Выбрать кольцо', command=self._choose_ring)
        self.ring_button.pack(side='left', padx=8)
        self.folder_path_label = tk.Label(filebar, textvariable=self.folder_label, bg=BG, fg=MUTED,
                 font=('Segoe UI', 9), anchor='w')
        self.folder_path_label.pack(side='left', fill='x', expand=True, padx=8)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill='both', expand=True, padx=24)
        sidebar = tk.Frame(body, bg=PANEL, width=270)
        sidebar.pack(side='left', fill='y', padx=(0, 16))
        sidebar.pack_propagate(False)
        self._label(sidebar, 'ИЗОБРАЖЕНИЯ', ('Segoe UI Semibold', 10)).pack(anchor='w', padx=14, pady=(16, 10))
        list_holder = tk.Frame(sidebar, bg=PANEL)
        list_holder.pack(fill='both', expand=True, padx=12)
        scrollbar = tk.Scrollbar(list_holder)
        scrollbar.pack(side='right', fill='y')
        self.file_list = tk.Listbox(list_holder, bg=INPUT, fg=FG,
                                   selectbackground='#3a5b9e', selectforeground='white',
                                   borderwidth=0, highlightthickness=0, font=('Segoe UI', 10),
                                   activestyle='none', yscrollcommand=scrollbar.set, exportselection=False)
        self.file_list.pack(side='left', fill='both', expand=True)
        scrollbar.config(command=self.file_list.yview)
        self.file_list.bind('<<ListboxSelect>>', self._on_select)
        self._label(sidebar, 'Изменения сохраняются для каждого арта.',
                    font=('Segoe UI', 9), color=MUTED, wraplength=235,
                    justify='left').pack(anchor='w', padx=14, pady=(10, 5))
        ttk.Button(sidebar, text='Сбросить настройки арта',
                   command=self._reset_current).pack(fill='x', padx=12, pady=(0, 14))

        content = tk.Frame(body, bg=PANEL)
        content.pack(side='left', fill='both', expand=True)
        self._label(content, 'ПРЕДПРОСМОТР', ('Segoe UI Semibold', 10)).pack(anchor='w', padx=18, pady=(14, 4))
        self._label(content, text='', color=MUTED, font=('Segoe UI', 9),
                    textvariable=self.preview_label).pack(anchor='w', padx=18)
        self.cutout_check = tk.Checkbutton(content, text='Показать вырезку без рамки', variable=self.show_cutout,
                       command=self._on_show_cutout, bg=PANEL, fg=MUTED, activebackground=PANEL,
                       activeforeground=FG, selectcolor=INPUT, highlightthickness=0,
                       font=('Segoe UI', 9))
        self.cutout_check.pack(anchor='w', padx=18, pady=(5, 0))
        middle = tk.Frame(content, bg=PANEL)
        middle.pack(fill='both', expand=True, padx=15, pady=(13, 10))
        self.display_size = 268
        self.canvas = tk.Canvas(middle, width=self.display_size, height=self.display_size,
                                borderwidth=0, highlightthickness=0, bg=INPUT)
        self.canvas.pack(side='left', anchor='n', padx=(0, 16))
        self.checker = checkerboard(self.display_size)
        self._show_token(None)

        settings_col = tk.Frame(middle, bg=PANEL)
        settings_col.pack(side='left', fill='both', expand=True)
        self.token_controls = tk.Frame(settings_col, bg=PANEL)
        self.token_controls.pack(fill='x')
        fields = [
            ('zoom', 'Масштаб, %', self.zoom),
            ('x', 'Горизонталь, %', self.x),
            ('y', 'Вертикаль, %', self.y),
            ('gap', 'Отступ над головой, %', self.gap),
        ]
        for name, title, var in fields:
            low, high = TOKEN_RANGES[name]
            field = tk.Frame(self.token_controls, bg=PANEL)
            field.pack(fill='x', pady=(0, 1))
            tk.Label(field, text=title, bg=PANEL, fg=FG, font=('Segoe UI', 9),
                     anchor='w', width=20).pack(side='left')
            slider = tk.Scale(field, from_=low, to=high, orient='horizontal', variable=var,
                              resolution=1, showvalue=True, length=108,
                              bg=PANEL, fg=FG, troughcolor=INPUT, highlightthickness=0,
                              activebackground=BLUE, borderwidth=0,
                              command=lambda value, field=name: self._on_adjust(field))
            slider.pack(side='left', fill='x', expand=True)

        options = tk.Frame(self.token_controls, bg=PANEL)
        options.pack(fill='x', pady=(8, 5))
        self._label(options, 'Размер:', font=('Segoe UI', 9)).grid(row=0, column=0, sticky='w', pady=3)
        size_box = ttk.Combobox(options, values=['256', '512', '1024'], textvariable=self.size,
                                 state='readonly', width=8)
        size_box.grid(row=0, column=1, sticky='w', padx=(8, 0), pady=3)
        size_box.bind('<<ComboboxSelected>>', lambda e: self._schedule_save())
        self._label(options, 'Формат:', font=('Segoe UI', 9)).grid(row=1, column=0, sticky='w', pady=3)
        ext_box = ttk.Combobox(options, values=['webp', 'png'], textvariable=self.ext,
                                state='readonly', width=8)
        ext_box.grid(row=1, column=1, sticky='w', padx=(8, 0), pady=3)
        ext_box.bind('<<ComboboxSelected>>', lambda e: self._on_format_change())
        options.columnconfigure(1, weight=1)

        self.item_controls = tk.Frame(settings_col, bg=PANEL)
        self._label(self.item_controls, '500 × 500  |  WebP без потерь  |  Обводка 3 px',
                    font=('Segoe UI', 9), color=MUTED).pack(anchor='w', pady=(0, 9))
        layout_row = tk.Frame(self.item_controls, bg=PANEL)
        layout_row.pack(fill='x', pady=(0, 9))
        self._label(layout_row, 'Расположение:', font=('Segoe UI', 9)).pack(side='left')
        layout_box = ttk.Combobox(layout_row, values=list(ITEM_MODES), textvariable=self.item_layout,
                                  state='readonly', width=18)
        layout_box.pack(side='left', fill='x', expand=True, padx=(8, 0))
        layout_box.bind('<<ComboboxSelected>>', lambda e: self._on_item_adjust('layout'))
        default_row = tk.Frame(self.item_controls, bg=PANEL)
        default_row.pack(fill='x', pady=(0, 5))
        self._label(default_row, 'Для новых артов:', font=('Segoe UI', 9)).pack(side='left')
        default_box = ttk.Combobox(default_row, values=list(ITEM_MODES),
                                   textvariable=self.default_item_layout, state='readonly', width=16)
        default_box.pack(side='left', fill='x', expand=True, padx=(8, 0))
        default_box.bind('<<ComboboxSelected>>', lambda e: self._on_default_item_layout_change())
        for name, title, var in [
            ('zoom', 'Масштаб, %', self.item_zoom),
            ('x', 'Горизонталь, %', self.item_x),
            ('y', 'Вертикаль, %', self.item_y),
            ('rotation', 'Поворот, °', self.item_rotation),
        ]:
            low, high = ITEM_RANGES[name]
            row = tk.Frame(self.item_controls, bg=PANEL)
            row.pack(fill='x', pady=(0, 1))
            tk.Label(row, text=title, bg=PANEL, fg=FG, font=('Segoe UI', 9),
                     width=20, anchor='w').pack(side='left')
            tk.Scale(row, from_=low, to=high, orient='horizontal', variable=var,
                     resolution=1, showvalue=False, length=108, width=10, sliderlength=17,
                     bg=PANEL, fg=FG, troughcolor=INPUT, highlightthickness=0,
                     activebackground=BLUE, borderwidth=0,
                     command=lambda value, field=name: self._on_item_adjust(field)).pack(side='left', fill='x', expand=True)
            tk.Label(row, textvariable=var, bg=PANEL, fg=FG, width=5,
                     font=('Segoe UI', 9)).pack(side='right')
        for name, title, value in [
            ('flip_horizontal', 'Отразить по горизонтали', self.item_flip_horizontal),
            ('flip_vertical', 'Отразить по вертикали', self.item_flip_vertical),
            ('clean_noise', 'Игнорировать почти прозрачный мусор', self.item_clean_noise),
        ]:
            tk.Checkbutton(self.item_controls, text=title, variable=value,
                           command=lambda field=name: self._on_item_adjust(field), bg=PANEL, fg=FG,
                           activebackground=PANEL, activeforeground=FG,
                           selectcolor=INPUT, highlightthickness=0,
                           font=('Segoe UI', 9)).pack(anchor='w')
        self._label(self.item_controls,
                    '100% = максимум без обрезки. Смещение изменяет центр и при необходимости уменьшает предмет.',
                    color=MUTED, font=('Segoe UI', 9), wraplength=350,
                    justify='left').pack(anchor='w', pady=(7, 3))

        common_options = tk.Frame(settings_col, bg=PANEL)
        common_options.pack(fill='x', pady=(8, 5))
        self.common_options = common_options
        self._label(common_options, 'Модель:', font=('Segoe UI', 9)).grid(row=0, column=0, sticky='w', pady=3)
        model_box = ttk.Combobox(common_options, values=MODELS, textvariable=self.model,
                                  state='readonly', width=19)
        model_box.grid(row=0, column=1, sticky='ew', padx=(8, 0), pady=3)
        model_box.bind('<<ComboboxSelected>>', lambda e: self._on_model_change())
        self._label(common_options, 'Края:', font=('Segoe UI', 9)).grid(row=1, column=0, sticky='w', pady=3)
        edges_box = ttk.Combobox(common_options, values=EDGE_MODES, textvariable=self.edge_mode,
                                 state='readonly', width=19)
        edges_box.grid(row=1, column=1, sticky='ew', padx=(8, 0), pady=3)
        edges_box.bind('<<ComboboxSelected>>', lambda e: self._on_model_change())
        common_options.columnconfigure(1, weight=1)

        checks = tk.Frame(settings_col, bg=PANEL)
        checks.pack(fill='x')
        self.common_checks = checks
        for label, var, cmd in [
            ('Не вырезать фон PNG/WebP с альфой', self.keep_alpha, self._on_model_change),
            ('Пропускать готовые изображения', self.skip_exists, self._schedule_save),
            ('Конвертировать исходные арты в WebP', self.convert_original, self._schedule_save),
        ]:
            tk.Checkbutton(checks, text=label, variable=var, command=cmd,
                           bg=PANEL, fg=FG, activebackground=PANEL, activeforeground=FG,
                           selectcolor=INPUT, highlightthickness=0,
                           font=('Segoe UI', 9)).pack(anchor='w')

        actions = tk.Frame(self.root, bg=BG)
        actions.pack(fill='x', padx=24, pady=(15, 6))
        ttk.Button(actions, text='Сохранить выбранный', command=self._export_current).pack(side='left')
        ttk.Button(actions, text='Обработать всю папку', style='Accent.TButton',
                   command=self._export_all).pack(side='left', padx=9)
        ttk.Button(actions, text='Остановить', command=self._cancel).pack(side='right')
        self.progress = ttk.Progressbar(self.root, mode='determinate', maximum=100)
        self.progress.pack(fill='x', padx=24, pady=(4, 6))
        tk.Label(self.root, textvariable=self.status, bg=BG, fg=MUTED,
                 font=('Segoe UI', 9), anchor='w').pack(fill='x', padx=24, pady=(0, 13))

    def _on_work_mode_change(self, initial=False):
        is_item = self.work_mode.get() == 'item'
        self.token_controls.pack_forget()
        self.item_controls.pack_forget()
        target = self.item_controls if is_item else self.token_controls
        target.pack(fill='x', before=self.common_options)
        if is_item:
            self.ring_button.pack_forget()
            self.cutout_check.configure(text='Показать вырезку без обработки')
        else:
            self.ring_button.pack(side='left', padx=8, before=self.folder_path_label)
            self.cutout_check.configure(text='Показать вырезку без рамки')
        self._schedule_save()
        if self.selected and not initial:
            self._select(self.selected)

    def _show_token(self, image):
        base = self.checker.copy()
        if image is not None:
            token = image.resize((self.display_size, self.display_size), Image.Resampling.LANCZOS)
            base.alpha_composite(token)
        self.display_photo = ImageTk.PhotoImage(base)
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, image=self.display_photo, anchor='nw')

    def _choose_folder(self):
        chosen = filedialog.askdirectory(title='Папка с исходными артами',
                                          initialdir=str(self.folder or Path.home()))
        if chosen:
            self._set_folder(Path(chosen))

    def _set_folder(self, folder: Path, select_path: Path | None = None, *, persist=True):
        if self.closing:
            return
        if self.approval_pending or self.running_batch or self.pending_export is not None:
            self.status.set('Сначала заверши или останови экспорт.')
            return
        self._queue_scan(folder, select_path, persist=persist)

    def _queue_scan(self, folder, select_path=None, *, persist=False, follow_selection=False):
        # A request is not a committed folder. Keep the previous list and
        # preferences intact until this particular scan succeeds.
        self.scan_generation += 1
        self.folder_label.set(f'Поиск: {folder}')
        self.status.set(f'Поиск исходных артов: {folder.name}')
        request = (self.scan_generation, folder, select_path, self.frame_path, persist, follow_selection)
        if self.busy or self.approval_pending:
            self.pending_scan = request
            if self.scan_running:
                self.cancel_event.set()
        else:
            self._run_scan(request)

    def _run_scan(self, request):
        generation, folder, select_path, frame_path, persist, follow_selection = request
        self.busy = self.scan_running = True
        self.active_scan = request
        self.cancel_event.clear()

        def work():
            files = list_source_images(folder, exclude=frame_path, cancel_event=self.cancel_event)
            self.events.put(('folder_scan', (request, files, self.cancel_event.is_set(), None)))

        self._launch_worker(work, lambda exc: self.events.put(
            ('folder_scan', (request, [], False, self._worker_error_text(exc)))))

    def _restore_folder_label(self):
        self.folder_label.set(str(self.folder) if self.folder else 'Папка не выбрана')

    @staticmethod
    def _exact_row(files, selected):
        return next((i for i, p in enumerate(files) if p.name == selected.name), None) if selected else None

    def _set_file_list(self, files):
        self.files = files
        self.file_list.delete(0, tk.END)
        for p in files:
            self.file_list.insert(tk.END, p.name)

    def _apply_folder_scan(self, request, files, cancelled, error):
        generation, folder, select_path, _, persist, follow_selection = request
        if generation != self.scan_generation:
            return
        if cancelled or error:
            self._restore_folder_label()
            self.status.set('Поиск остановлен.' if cancelled else f'Ошибка поиска: {error}')
            return
        # An automatic refresh updates the list, not the user's selection.
        # Explicit folder/Explorer requests still honor their captured target.
        if follow_selection:
            select_path = self.selected
        self.folder = folder
        if persist:
            self.folder_preference = str(folder)
        self._restore_folder_label()
        self._set_file_list(files)
        self.status.set(f'Исходных артов: {len(files)}. Результаты сохраняются рядом с оригиналами.')
        index = self._exact_row(files, select_path)
        if select_path is not None and index is None and not follow_selection:
            self._clear_selection()
            self.status.set(f'Не удалось открыть {select_path.name}: файл недоступен или исключён как рамка.')
        elif files:
            index = index if index is not None else 0
            self.file_list.selection_set(index)
            self.file_list.see(index)
            if not follow_selection or self.selected is None or str(self.selected) != str(files[index]):
                self._select(files[index])
        else:
            self._clear_selection()
            self.preview_label.set('Нет изображений в папке')
        if persist:
            self._schedule_save()

    def _refresh_frame_sources(self):
        # Checking every alias in a large or remote folder is filesystem
        # work, so leave the last committed list visible until discovery ends.
        # Immediately invalidate the selected art only if it is the new frame.
        if self.selected is not None and excluded_file(self.selected, self.frame_path):
            self.file_list.selection_clear(0, tk.END)
            self._clear_selection()
        request = self.pending_scan or self.active_scan
        if request is not None:
            _, folder, target, _, persist, follow_selection = request
            self._queue_scan(folder, target, persist=persist, follow_selection=follow_selection)
        elif self.folder is not None:
            self._queue_scan(self.folder, persist=False, follow_selection=True)

    def _choose_ring(self):
        filename = filedialog.askopenfilename(title='Выбери PNG-кольцо',
                                               filetypes=[('PNG images', '*.png'), ('Все файлы', '*.*')])
        if filename:
            try:
                self._set_frame(Path(filename))
                self._refresh_frame_sources()
                if self.selected and self.current_cutout is not None:
                    self._render()
                elif self.selected:
                    self._request_preview()
                self._schedule_save()
            except Exception as exc:
                messagebox.showerror('Ошибка рамки', str(exc))

    def _set_frame(self, path):
        frame = read_image(path)
        if frame.width != frame.height:
            raise ValueError('Сейчас поддерживаются квадратные PNG-кольца.')
        mask = frame_interior(frame)
        self.frame = frame
        self.interior = mask
        self.frame_path = path
        self.ring_label.set(path.name)

    def _on_drop(self, event):
        # tk understands Windows file names with spaces and {braced paths}.
        paths = self.root.tk.splitlist(event.data)
        if paths:
            p = Path(paths[0])
            folder = p if p.is_dir() else p.parent
            if folder.is_dir():
                self._set_folder(folder)

    def _on_select(self, _event):
        indexes = self.file_list.curselection()
        if indexes:
            self._select(self.files[indexes[0]])

    def _item_adjust_for(self, path):
        return adjustment_for(self.settings, path, 'item', self.default_item_layout.get())

    def _on_default_item_layout_change(self):
        # A new default applies to uncustomized items, not to explicit per-item
        # overrides. Keep the preview and the next batch in agreement.
        stored = override_for(self.settings, 'item_overrides', self.selected) if self.selected else {}
        if self.selected is None or valid_layout(stored.get('layout'), None) is None:
            self.item_layout.set(self.default_item_layout.get())
            if 'item_overrides' in self.widget_values:
                self.widget_values['item_overrides']['layout'] = self.item_layout.get()
            if self.selected is not None:
                if self.current_cutout is not None:
                    self._render()
                else:
                    self._request_preview()
        self._schedule_save()

    def _current_item_adjust(self):
        return ItemAdjust(layout=self.item_layout.get(), zoom=self.item_zoom.get(),
                          x=self.item_x.get(), y=self.item_y.get(),
                          rotation=self.item_rotation.get(),
                          flip_horizontal=self.item_flip_horizontal.get(),
                          flip_vertical=self.item_flip_vertical.get(),
                          clean_noise=self.item_clean_noise.get())

    def _on_item_adjust(self, field):
        if self.closing or self.sliders_updating or self.selected is None:
            return
        if self._record_adjustment('item_overrides', self.selected, self._current_item_adjust(), field):
            self._schedule_save()
            if self.current_cutout is not None:
                self._render()
            else:
                self._request_preview()

    def _adjust_for(self, path):
        return adjustment_for(self.settings, path, 'token')

    def _record_adjustment(self, name, path, adjusted, field):
        # Tk can clamp old values while loading a selection. Treat only the
        # edited widget as a local setting change, never other clamped values.
        value = vars(adjusted)[field]
        snapshot = self.widget_values.setdefault(name, {})
        if field in snapshot and snapshot[field] == value:
            return False
        snapshot[field] = value
        mapping = self.settings.setdefault(name, {})
        path_key = str(path.resolve())
        stored = mapping.get(path_key)
        mapping[path_key] = {**(stored if isinstance(stored, dict) else {}), field: value}
        return True

    def _clear_selection(self):
        self.preview_generation += 1
        self.preview_retry = False
        self.selected = None
        self.current_cutout = None
        self.current_key = None
        self.preview_pending = False
        self.preview_label.set('Нет выбранного изображения')
        self._show_token(None)

    def _select(self, path, *, force_preview=False):
        if self.closing:
            return
        # The old list can remain visible during an automatic frame refresh,
        # after cancellation or if a remote folder is temporarily unavailable.
        if excluded_file(path, self.frame_path):
            self.file_list.selection_clear(0, tk.END)
            self._clear_selection()
            self.status.set('Выбранный файл используется как рамка, а не исходный арт.')
            return
        self.preview_generation += 1
        self.preview_retry = False
        self.preview_pending = False
        self.selected = path
        self.preview_label.set(path.name)
        self.current_cutout = None
        self.current_key = None
        self.widget_values = {}
        self._show_token(None)
        self.sliders_updating = True
        try:
            adj = self._adjust_for(path)
            self.zoom.set(adj.zoom)
            self.x.set(adj.x)
            self.y.set(adj.y)
            self.gap.set(adj.gap)
            item = self._item_adjust_for(path)
            self.item_layout.set(item.layout)
            self.item_zoom.set(item.zoom)
            self.item_x.set(item.x)
            self.item_y.set(item.y)
            self.item_rotation.set(item.rotation)
            self.item_flip_horizontal.set(item.flip_horizontal)
            self.item_flip_vertical.set(item.flip_vertical)
            self.item_clean_noise.set(item.clean_noise)
            self.widget_values = {'overrides': vars(self._current_adjust()),
                                  'item_overrides': vars(self._current_item_adjust())}
        except (OSError, ValueError, TypeError, RuntimeError, tk.TclError) as exc:
            self.status.set(f'Не удалось загрузить параметры {path.name}: {exc}')
            return
        finally:
            self.sliders_updating = False
        # Ordinary selection can show the saved result immediately. Explicit
        # Reset must produce a fresh preview even when the cutout is uncached.
        if force_preview:
            self._request_preview()
        else:
            self._request_preview(cached_only=not self.show_cutout.get())

    @staticmethod
    def _load_saved_preview(source: Path, mode, ext):
        """Worker-only I/O. No Tk controls or canvas access."""
        if mode == 'item':
            candidates = [item_output_path(source)]
        else:
            candidates = [token_output_path(source, fmt)
                          for fmt in dict.fromkeys([ext, 'webp', 'png'])]
        for token in candidates:
            if token.is_file():
                try:
                    existing = read_owned_export_image(token, source, mode)
                    if existing is not None:
                        return existing
                except (OSError, ValueError, SyntaxError, TypeError, RecursionError, Image.DecompressionBombError):
                    pass
        return None

    def _on_format_change(self):
        self._schedule_save()
        if self.selected is not None and self.work_mode.get() == 'token' and self.current_cutout is None:
            self.preview_generation += 1
            self._show_token(None)
            self._request_preview(cached_only=not self.show_cutout.get())

    def _current_adjust(self):
        return Adjust(self.zoom.get(), self.x.get(), self.y.get(), self.gap.get())

    def _on_adjust(self, field):
        if self.closing or self.sliders_updating or self.selected is None:
            return
        if self._record_adjustment('overrides', self.selected, self._current_adjust(), field):
            self._schedule_save()
            if self.current_cutout is not None:
                self._render()
            else:
                self._request_preview()

    def _on_show_cutout(self):
        if self.current_cutout is not None:
            self._render()
        elif self.selected is not None:
            self.preview_generation += 1
            self._show_token(None)
            self._request_preview(cached_only=not self.show_cutout.get())
        else:
            self._show_token(None)

    def _reset_current(self):
        if self.selected is None:
            return
        key = 'item_overrides' if self.work_mode.get() == 'item' else 'overrides'
        path_key = str(self.selected.resolve())
        self.pending_resets.add((key, path_key))
        self.settings.setdefault(key, {}).pop(path_key, None)
        # A later edit after Reset is intentional even if it happens to
        # match the value before Reset. Compare those edits with absence.
        baseline = self._settings_baseline.get(key)
        if isinstance(baseline, dict):
            baseline.pop(path_key, None)
        self._select(self.selected, force_preview=True)
        self._schedule_save()

    def _on_model_change(self):
        self._schedule_save()
        if self.selected:
            self.preview_generation += 1
            self.current_cutout = None
            self.current_key = None
            self._request_preview()

    def _cache_key(self, path, model, keep_alpha, edge_mode):
        return (*source_identity(path), model, keep_alpha, edge_mode)

    def _get_cutout(self, path, model, keep_alpha, edge_mode, *, key=None, cached_only=False):
        key = key if key is not None else self._cache_key(path, model, keep_alpha, edge_mode)
        cutout = load_cutout(path, model, keep_alpha, edge_mode, key=key,
                            disk_cache=self.disk_cache, memory=self.cache, sessions=self.sessions,
                            decode=read_image, extract=remove_background, session_factory=new_local_session,
                            cached_only=cached_only)
        return cutout, key

    def _preview_request(self):
        return (self.preview_generation, self.selected, self.model.get(),
                self.keep_alpha.get(), self.edge_mode.get())

    def _request_preview(self, *, cached_only=False, verify=False):
        if self.closing or not self.selected:
            return
        can_render = self.work_mode.get() == 'item' or self.frame is not None or self.show_cutout.get()
        if not can_render and not cached_only:
            return
        if self.busy or self.approval_pending:
            if not verify:
                # A requested full extraction must supersede a cache-only probe.
                self.preview_cached_only = cached_only if not self.preview_pending else (
                    self.preview_cached_only and cached_only)
                self.preview_pending = True
            return
        request = self._preview_request()
        _, path, model, keep_alpha, edge_mode = request
        previous = self.current_key if verify else None
        saved_mode = self.work_mode.get()
        saved_ext = self.ext.get() if cached_only else None
        prefer_saved = cached_only and not self.show_cutout.get() and not verify
        self.busy = True
        if not verify:
            self.status.set(f'Подготовка: {path.name}. При первом запуске модель может скачиваться.')

        def work():
            saved = self._load_saved_preview(path, saved_mode, saved_ext) if prefer_saved else None
            if saved is not None:
                self.events.put(('saved_preview', (request, saved_mode, saved_ext, saved)))
            if not can_render:
                self.events.put(('preview_no_frame', request))
                return
            key = self._cache_key(path, model, keep_alpha, edge_mode)
            if verify and key == previous:
                self.events.put(('preview_unchanged', request))
                return
            if verify:
                self.events.put(('preview_invalidated', request))
            img, key = self._get_cutout(path, model, keep_alpha, edge_mode,
                                        key=key, cached_only=saved is not None)
            self.events.put(('preview', (request, img, key)))

        self._launch_worker(work, lambda exc: self.events.put(
            ('error', (request, f'{path.name}: {self._worker_error_text(exc)}',
                       isinstance(exc, OSError), verify))))

    def _watch_source(self):
        # Hash periodically in the existing worker, not on every slider event.
        # This also detects same-size, same-timestamp edits on a network share.
        if self.closing:
            return
        if (self.current_cutout is not None or self.preview_retry) and not (self.busy or self.approval_pending):
            self._request_preview(verify=True)
        self.root.after(2500, self._watch_source)

    def _render(self):
        if self.current_cutout is None:
            return False
        try:
            if self.show_cutout.get():
                image = self._make_cutout_preview(self.current_cutout, 512)
            elif self.work_mode.get() == 'item':
                image = compose_item(self.current_cutout, self._current_item_adjust())
            else:
                if self.frame is None:
                    return False
                image = compose(self.current_cutout, self.frame, self.interior, 512,
                                self._current_adjust())
            self._show_token(image)
            return True
        except Exception as exc:
            self._show_token(None)
            self.status.set(f'Ошибка компоновки: {exc}')
            return False

    @staticmethod
    def _make_cutout_preview(img: Image.Image, size: int) -> Image.Image:
        # This view shows the model's raw extraction, before ring clipping.
        bbox = img.getchannel('A').point(lambda p: 255 if p >= 16 else 0).getbbox()
        target = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        if not bbox:
            return target
        cropped = img.crop(bbox)
        fit = (size - 28) / max(cropped.size)
        dimensions = tuple(max(1, round(length * fit)) for length in cropped.size)
        shrunk = cropped.resize(dimensions, Image.Resampling.LANCZOS)
        target.alpha_composite(shrunk, ((size - shrunk.width) // 2, (size - shrunk.height) // 2))
        return target

    def _export_current(self):
        if self.selected:
            self._start_export([self.selected], force_overwrite=True)

    def _export_all(self):
        if not self.folder:
            messagebox.showinfo('Нет изображений', 'Сначала выбери папку с артами.')
            return
        self._start_export(self.files, scan_folder=self.folder)

    def _start_export(self, image_files, force_overwrite=False, requested_mode=None, scan_folder=None):
        if self.closing or self.approval_pending:
            return
        if self.scan_running or self.pending_scan is not None:
            self.status.set('Дождись завершения поиска исходных артов.')
            return
        if not self.folder or (not image_files and scan_folder is None):
            messagebox.showinfo('Нет изображений', 'Сначала выбери папку с артами.')
            return
        if self.running_batch:
            self.status.set('Пакетная обработка уже выполняется.')
            return
        if self.pending_export is not None:
            self.status.set('Экспорт уже в очереди. Нажми «Остановить», чтобы отменить его, затем запусти новый.')
            return
        files = list(image_files) if scan_folder is None else []
        mode = requested_mode or self.work_mode.get()
        if mode == 'token' and self.frame is None:
            messagebox.showerror('Нет рамки', 'Выбери корректное PNG-кольцо.')
            return
        model, keep, edge_mode = self.model.get(), self.keep_alpha.get(), self.edge_mode.get()
        ext, size = self.ext.get(), int(self.size.get())
        skip, convert = self.skip_exists.get() and not force_overwrite, self.convert_original.get()
        frame = self.frame.copy() if self.frame is not None else None
        interior = self.interior.copy() if self.interior is not None else None
        adjustments = {p: (self._item_adjust_for(p) if mode == 'item'
                            else self._adjust_for(p)) for p in files}
        approved_outputs = {}
        # Tk continues to dispatch timers while a modal message box is open.
        # Capture every mutable input before entering that nested event loop.
        expected_input = None
        scan = (scan_folder, self.frame_path, deepcopy(self.settings),
                self.default_item_layout.get() if mode == 'item' else None) if scan_folder else None
        preflight = None
        if force_overwrite and len(files) == 1:
            source = files[0]
            destination = item_output_path(source) if mode == 'item' else token_output_path(source, ext)
            try:
                source_stamp = file_stamp(source)
                if source_stamp is None:
                    raise FileNotFoundError(f'Файл не найден: {source.name}')
                destination_stamp = file_stamp(destination)
            except (OSError, ValueError) as exc:
                messagebox.showerror('Ошибка файла', str(exc))
                return
            if self.current_key is not None and str(source) == str(self.selected):
                expected_input = self.current_key[:4]
            preflight = (source, destination, source_stamp, destination_stamp, self.frame_path)
        request = (files, mode, model, keep, edge_mode, ext, size, skip,
                   convert, frame, interior, adjustments, approved_outputs, expected_input, scan)
        if preflight is not None:
            request += (preflight,)
        # Only the dispatcher consumes queued requests.
        self.pending_export = request
        if self.busy:
            self.status.set('Обработка папки в очереди.' if scan else
                            f'В очереди: {len(files)} артов. Начнётся после текущего предпросмотра.')
        self._after_preview()

    @staticmethod
    def _worker_error_text(exc):
        if isinstance(exc, SystemExit):
            return f'Внешняя библиотека аварийно завершила операцию (код {exc.code}). Проверь зависимости.'
        return str(exc)

    def _launch_worker(self, work, on_error):
        """Every reserved worker emits a terminal outcome, even on a foreign
        SystemExit or failure to start a thread."""
        def guarded():
            try:
                work()
            except (Exception, SystemExit) as exc:
                on_error(exc)

        try:
            self.worker = threading.Thread(target=guarded, daemon=False)
            self.worker.start()
        except (Exception, SystemExit) as exc:
            on_error(exc)

    def _run_preflight(self, request):
        source, destination, source_stamp, destination_stamp, frame_path = request[-1]
        expected_input = request[13]
        mode = request[1]
        self.busy = self.running_batch = True
        self.cancel_event.clear()
        self.status.set(f'Проверка файлов перед сохранением: {source.name}')

        def check_stamps():
            if file_stamp(source) != source_stamp or file_stamp(destination) != destination_stamp:
                raise OSError('Исходник или результат изменился после нажатия «Сохранить». Повтори сохранение.')

        def work():
            check_stamps()
            if excluded_file(source, frame_path):
                raise ValueError('Выбранный файл используется как рамка.')
            identity = source_identity(source)
            if expected_input is not None and expected_input != identity:
                raise OSError(f'Исходный файл изменился: {source.name}')
            prior = source_identity(destination) if destination_stamp is not None else EXPECTED_ABSENT
            owned = prior is not EXPECTED_ABSENT and is_owned_export(destination, source, mode, identity=prior)
            check_stamps()
            ready = request[:12] + ({destination: prior}, identity, request[14])
            self.events.put(('export_preflight', (ready, destination, prior is not EXPECTED_ABSENT and not owned)))

        self._launch_worker(work, lambda exc: self.events.put(
            ('preflight_failure', self._worker_error_text(exc))))

    def _run_export(self, request):
        if len(request) == 16:
            self._run_preflight(request)
            return
        (files, mode, model, keep, edge_mode, ext, size, skip,
         convert, frame, interior, adjustments, approved_outputs, expected_input, scan) = request
        self.busy = True
        self.running_batch = True
        self.cancel_event.clear()
        self.progress['value'] = 0
        self.status.set('Поиск исходных артов…' if scan else f'Начата пакетная обработка: {len(files)} артов.')

        def work():
            selected_files, captured = files, adjustments
            if scan is not None:
                folder, ring_path, settings, default_layout = scan
                selected_files = list_source_images(folder, exclude=ring_path, cancel_event=self.cancel_event)
                if self.cancel_event.is_set():
                    from token_engine import BatchResult
                    self.events.put(('done', BatchResult(errors=[], cancelled=True)))
                    return
                self.events.put(('batch_scan', (folder, selected_files, ring_path)))
                captured = {}
                for path in selected_files:
                    if self.cancel_event.is_set():
                        from token_engine import BatchResult
                        self.events.put(('done', BatchResult(errors=[], cancelled=True, total=len(selected_files))))
                        return
                    captured[path] = adjustment_for(settings, path, mode, default_layout)
            def get_cutout(path):
                if expected_input is not None:
                    assert_source_identity(path, expected_input)
                image, key = self._get_cutout(path, model, keep, edge_mode)
                if expected_input is not None and key[:4] != expected_input:
                    raise OSError(f'Исходный файл изменился во время обработки: {path.name}')
                return LoadedCutout(image, key[:4])
            progress = lambda i, n, msg: self.events.put(('progress', (i, n, msg)))
            if mode == 'item':
                result = export_items(selected_files, get_cutout, captured,
                                      skip_exists=skip, convert_original=convert,
                                      cancel_event=self.cancel_event, on_progress=progress,
                                      approved_outputs=approved_outputs,
                                      expected_inputs={str(files[0]): expected_input} if expected_input is not None else None)
            else:
                result = export_batch(selected_files, get_cutout, frame, interior,
                                      captured, out_size=size, format_name=ext,
                                      skip_exists=skip, convert_original=convert,
                                      cancel_event=self.cancel_event, on_progress=progress,
                                      approved_outputs=approved_outputs,
                                      expected_inputs={str(files[0]): expected_input} if expected_input is not None else None)
            self.events.put(('done', result))
        self._launch_worker(work, lambda exc: self.events.put(('batch_failure', traceback.format_exc())))

    def _after_preview(self):
        """Dispatch only after the active worker completion has been handled."""
        if self.closing or self.busy or self.approval_pending:
            return
        if self.pending_export is not None:
            request = self.pending_export
            self.pending_export = None
            self._run_export(request)
        elif self.pending_scan is not None:
            request = self.pending_scan
            self.pending_scan = None
            self._run_scan(request)
        elif self.preview_pending:
            self.preview_pending = False
            cached_only = self.preview_cached_only
            self.preview_cached_only = False
            self._request_preview(cached_only=cached_only)

    def _cancel(self):
        if self.running_batch:
            self.cancel_event.set()
            self.status.set('Остановка после текущего изображения…')
        elif self.scan_running or self.pending_scan is not None:
            self.scan_generation += 1
            self.pending_scan = None
            if self.scan_running:
                self.cancel_event.set()
            self._restore_folder_label()
            self.status.set('Поиск остановлен.')
        elif self.pending_export is not None:
            self.pending_export = None
            self.status.set('Запланированная пакетная обработка отменена.')

    def _poll(self):
        try:
            while not self.closing:
                action, data = self.events.get_nowait()
                self._handle_worker_event(action, data)
        except queue.Empty:
            pass
        finally:
            if self.closing and (self.worker is None or not self.worker.is_alive()):
                self.root.destroy()
            else:
                self.root.after(120, self._poll)

    def _handle_worker_event(self, action, data):
        terminal = action in ('folder_scan', 'preview_unchanged', 'preview', 'error',
                              'preview_no_frame', 'export_preflight', 'preflight_failure',
                              'done', 'batch_failure')
        # Keep the reservation during completion handling, including modal
        # Tk loops; retries and queued exports cannot steal the worker slot.
        try:
            self._apply_worker_event(action, data)
        finally:
            if terminal:
                if action in ('export_preflight', 'preflight_failure', 'done', 'batch_failure'):
                    self.running_batch = False
                self.busy = False
                self._after_preview()

    def _apply_worker_event(self, action, data):
        if action == 'folder_scan':
            self.scan_running = False
            self.active_scan = None
            self._apply_folder_scan(*data)
        elif action == 'batch_scan':
            folder, files, scanned_frame = data
            if folder == self.folder and str(scanned_frame) == str(self.frame_path):
                self._set_file_list(files)
                index = self._exact_row(files, self.selected)
                if index is not None:
                    self.file_list.selection_set(index)
                else:
                    self._clear_selection()
        elif action == 'saved_preview':
            request, mode, ext, image = data
            if (request == self._preview_request() and mode == self.work_mode.get()
                    and ext == self.ext.get() and not self.show_cutout.get()
                    and not (self.preview_pending and not self.preview_cached_only)):
                self._show_token(image)
        elif action == 'preview_no_frame':
            if data == self._preview_request():
                self.status.set('Выбери корректное PNG-кольцо для редактирования.')
        elif action == 'export_preflight':
            ready, destination, needs_approval = data
            if self.cancel_event.is_set() or self.closing:
                return
            accepted = True
            if needs_approval:
                self.approval_pending = True
                try:
                    accepted = messagebox.askyesno(
                        'Подтверждение замены',
                        f'{destination.name} не помечен как результат TokenForge. '
                        'Это может быть старый токен или отдельный арт. Заменить его?')
                finally:
                    self.approval_pending = False
            if accepted and not self.closing and not self.cancel_event.is_set():
                self.pending_export = ready
        elif action == 'preflight_failure':
            if not self.cancel_event.is_set():
                self.status.set('Сохранение остановлено.')
                messagebox.showerror('Ошибка файла', data)
        elif action == 'preview_invalidated':
            if data == self._preview_request():
                self.current_cutout = None
                self.current_key = None
                self._show_token(None)
                self.status.set('Исходник изменился. Обновляю предпросмотр…')
        elif action == 'preview_unchanged':
            pass
        elif action == 'preview':
            request, img, key = data
            if request == self._preview_request():
                self.preview_retry = False
                if img is not None:
                    self.current_cutout = img
                    self.current_key = key
                    # This cutout satisfies a pending full extraction too.
                    self.preview_pending = False
                    if self._render():
                        self.status.set(f'Готово: {self.selected.name}. Отрегулируй положение или экспортируй.')
                else:
                    self.status.set(f'Готовый результат: {self.selected.name}. Для редактирования измени ползунок.')
        elif action == 'error':
            request, error, retry, verifying = data
            if request == self._preview_request():
                self.preview_retry = retry
                self.current_cutout = None
                self.current_key = None
                self._show_token(None)
                self.status.set('Ошибка: ' + error)
                if not self.preview_pending and not verifying:
                    messagebox.showerror('Ошибка обработки', error)
        elif action == 'progress':
            n, total, text = data
            self.progress['value'] = n / max(1, total) * 100
            self.status.set(text)
        elif action == 'done':
            report = (f'Создано: {data.created}. Пропущено: {data.skipped}. '
                      f'Артов в WebP: {data.converted}. Ошибок: {len(data.errors)}. '
                      f'Предупреждений: {len(data.warnings)}. '
                      f'Всего исходников: {data.total}.')
            if data.cancelled:
                report = 'Остановлено. ' + report
            self.status.set(report)
            if (self.selected and self.current_cutout is None and not self.show_cutout.get()):
                expected = ([item_output_path(self.selected)] if self.work_mode.get() == 'item' else
                            [token_output_path(self.selected, ext) for ext in ('webp', 'png')])
                if any(path in expected for path in data.outputs):
                    self._request_preview(cached_only=True)
            messages = data.errors + data.warnings
            if messages:
                # Preserve every message (especially recovery paths)
                # in a scrollable dialog instead of truncating them.
                self._show_batch_report(report, messages)
        elif action == 'batch_failure':
            self.status.set('Критическая ошибка пакетной обработки.')
            messagebox.showerror('Ошибка пакетной обработки', str(data))

    def _show_batch_report(self, report, messages):
        dialog = tk.Toplevel(self.root)
        dialog.title('Отчёт обработки')
        dialog.geometry('760x480')
        ttk.Label(dialog, text=report, wraplength=720).pack(fill='x', padx=12, pady=8)
        body = tk.Frame(dialog)
        body.pack(fill='both', expand=True, padx=12)
        scrollbar = ttk.Scrollbar(body)
        scrollbar.pack(side='right', fill='y')
        content = tk.Text(body, wrap='word', yscrollcommand=scrollbar.set)
        scrollbar.configure(command=content.yview)
        content.pack(fill='both', expand=True)
        content.insert('1.0', '\n\n'.join(messages))
        content.configure(state='disabled')
        ttk.Button(dialog, text='Закрыть', command=dialog.destroy).pack(pady=8)

    def _schedule_save(self):
        if self.closing:
            return
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        self.save_retries = 0
        self.save_timer = self.root.after(500, self._save)

    def _settings_snapshot(self):
        current = deepcopy(self.settings)
        current.update({
            'folder': self.folder_preference,
            'ring': str(self.frame_path),
            'model': self.model.get(),
            'edge_mode': self.edge_mode.get(),
            'size': int(self.size.get()),
            'ext': self.ext.get(),
            'keep_alpha': self.keep_alpha.get(),
            'skip_exists': self.skip_exists.get(),
            'convert_original': self.convert_original.get(),
            'work_mode': self.work_mode.get(),
            'item_layout': self.default_item_layout.get(),
        })
        return current

    def _save(self, allow_retry=True):
        self.save_timer = None
        current = self._settings_snapshot()
        self.save_retryable = False
        try:
            save_settings_delta(PREFS_FILE, self._settings_baseline, current, resets=self.pending_resets)
            self.pending_resets.clear()
            self.save_retries = 0
            # Keep the baseline local: remote edits have not changed our controls.
            self._settings_baseline = deepcopy(current)
            return True
        except (OSError, ValueError) as exc:
            # A reader holding settings.json can briefly deny replacement
            # on Windows. Retry the whole locked merge, not a stale write.
            self.save_retryable = isinstance(exc, OSError) and getattr(exc, 'winerror', None) in (5, 32, 33)
            if allow_retry:
                self._resume_save_retry()
            self.status.set(f'Не удалось сохранить настройки: {exc}')
            return False

    def _resume_save_retry(self):
        if self.save_retryable and self.save_retries < 5 and not self.closing and not self.save_timer:
            self.save_retries += 1
            self.save_timer = self.root.after(200 * self.save_retries, self._save)

    def _on_close(self):
        if self.closing:
            return
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
            self.save_timer = None
        if not self._save(allow_retry=False):
            if not messagebox.askyesno('Настройки не сохранены', 'Закрыть программу без сохранения изменений?'):
                self._resume_save_retry()
                return
        self.closing = True
        self.pending_scan = None
        self.pending_export = None
        self.preview_pending = False
        self.cancel_event.set()
        if not self.busy and not self.approval_pending and (self.worker is None or not self.worker.is_alive()):
            self.root.destroy()
        else:
            self.status.set('Завершение текущей операции и очистка временных файлов…')

    def run(self):
        self.root.mainloop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='TokenForge: RPG tokens & items')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--auto', metavar='IMAGE', help='Create one token silently')
    mode.add_argument('--open', dest='open_file', metavar='IMAGE', help='Open one character image')
    mode.add_argument('--auto-item', metavar='IMAGE', help='Create one 500px item')
    mode.add_argument('--open-item', metavar='IMAGE', help='Open one image in item editor')
    args = parser.parse_args(argv)
    if args.auto or args.auto_item:
        from token_cli import run_auto
        return run_auto(args.auto or args.auto_item, item=bool(args.auto_item))
    try:
        startup = None
        if args.open_file or args.open_item:
            from token_cli import resolve_context_source
            startup = resolve_context_source(args.open_file or args.open_item)
        TokenForge(startup_file=startup,
                   startup_mode=('item' if args.open_item else
                                 'token' if args.open_file else None)).run()
        return 0
    except Exception as exc:
        traceback.print_exc()
        from token_cli import write_log, notify_user, LOG_FILE
        write_log(f'GUI error: {exc}', exc)
        notify_user('Ошибка TokenForge', f'{exc}\n\nПодробности: {LOG_FILE}', error=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
