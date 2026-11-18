"""TokenForge: local Windows-friendly GUI for batch RPG token creation."""
from __future__ import annotations

from runtime_streams import initialize_missing_streams

# Explorer launches this entry point via pythonw.exe, without console streams.
if __name__ == '__main__':
    initialize_missing_streams()

import argparse
import json
import os
import queue
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageDraw, ImageTk

from token_engine import (
    Adjust, DiskCutoutCache, already_transparent, compose, frame_interior,
    export_batch, list_source_images, read_image, remove_background, token_output_path,
    atomic_write, source_identity, assert_source_identity, is_owned_export,
)
from item_engine import ItemAdjust, ITEM_MODES, compose_item, export_items, item_output_path

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    RootClass = TkinterDnD.Tk
except ImportError:
    DND_FILES = None
    RootClass = tk.Tk

BASE = Path(__file__).resolve().parent
PREFS_DIR = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.config')) / 'TokenForge'
PREFS_FILE = PREFS_DIR / 'settings.json'

BG = '#17191e'
PANEL = '#22252b'
INPUT = '#2a2e35'
FG = '#e9ecf1'
MUTED = '#a0a6b1'
BLUE = '#789fff'
MODELS = ['birefnet-general', 'birefnet-portrait', 'bria-rmbg', 'birefnet-general-lite', 'isnet-general-use', 'isnet-anime', 'u2net']
EDGE_MODES = ['Очистка краёв', 'Обычный', 'Alpha matting (медленно)']


def settings_read():
    try:
        return json.loads(PREFS_FILE.read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, ValueError):
        return {}


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
        self.folder = None
        self.frame_path = Path(self.settings.get('ring', str(BASE / 'RingT.png')))
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
        self.running_batch = False
        self.preview_pending = False
        self.pending_export = None
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.sliders_updating = False
        self.save_timer = None

        self.model = tk.StringVar(value=self.settings.get('model', MODELS[0]))
        self.edge_mode = tk.StringVar(value=self.settings.get('edge_mode', EDGE_MODES[0]))
        self.show_cutout = tk.BooleanVar(value=False)
        self.size = tk.StringVar(value=str(self.settings.get('size', 256)))
        self.ext = tk.StringVar(value=self.settings.get('ext', 'webp'))
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
        if startup_file is not None:
            self._set_folder(startup_file.parent, select_path=startup_file)
        else:
            last_folder = self.settings.get('folder')
            if last_folder and Path(last_folder).is_dir():
                self._set_folder(Path(last_folder))
        self.root.after(120, self._poll)
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
            ('Масштаб, %', self.zoom, 5, 320),
            ('Горизонталь, %', self.x, -120, 120),
            ('Вертикаль, %', self.y, -120, 120),
            ('Отступ над головой, %', self.gap, -20, 50),
        ]
        for title, var, low, high in fields:
            field = tk.Frame(self.token_controls, bg=PANEL)
            field.pack(fill='x', pady=(0, 1))
            tk.Label(field, text=title, bg=PANEL, fg=FG, font=('Segoe UI', 9),
                     anchor='w', width=20).pack(side='left')
            slider = tk.Scale(field, from_=low, to=high, orient='horizontal', variable=var,
                              resolution=1, showvalue=True, length=108,
                              bg=PANEL, fg=FG, troughcolor=INPUT, highlightthickness=0,
                              activebackground=BLUE, borderwidth=0, command=self._on_adjust)
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
        ext_box.bind('<<ComboboxSelected>>', lambda e: self._schedule_save())
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
        layout_box.bind('<<ComboboxSelected>>', lambda e: self._on_item_adjust())
        default_row = tk.Frame(self.item_controls, bg=PANEL)
        default_row.pack(fill='x', pady=(0, 5))
        self._label(default_row, 'Для новых артов:', font=('Segoe UI', 9)).pack(side='left')
        default_box = ttk.Combobox(default_row, values=list(ITEM_MODES),
                                   textvariable=self.default_item_layout, state='readonly', width=16)
        default_box.pack(side='left', fill='x', expand=True, padx=(8, 0))
        default_box.bind('<<ComboboxSelected>>', lambda e: self._on_default_item_layout_change())
        for title, var, low, high in [
            ('Масштаб, %', self.item_zoom, 10, 100),
            ('Горизонталь, %', self.item_x, -100, 100),
            ('Вертикаль, %', self.item_y, -100, 100),
            ('Поворот, °', self.item_rotation, -180, 180),
        ]:
            row = tk.Frame(self.item_controls, bg=PANEL)
            row.pack(fill='x', pady=(0, 1))
            tk.Label(row, text=title, bg=PANEL, fg=FG, font=('Segoe UI', 9),
                     width=20, anchor='w').pack(side='left')
            tk.Scale(row, from_=low, to=high, orient='horizontal', variable=var,
                     resolution=1, showvalue=False, length=108, width=10, sliderlength=17,
                     bg=PANEL, fg=FG, troughcolor=INPUT, highlightthickness=0,
                     activebackground=BLUE, borderwidth=0,
                     command=self._on_item_adjust).pack(side='left', fill='x', expand=True)
            tk.Label(row, textvariable=var, bg=PANEL, fg=FG, width=5,
                     font=('Segoe UI', 9)).pack(side='right')
        for title, value in [
            ('Отразить по горизонтали', self.item_flip_horizontal),
            ('Отразить по вертикали', self.item_flip_vertical),
            ('Игнорировать почти прозрачный мусор', self.item_clean_noise),
        ]:
            tk.Checkbutton(self.item_controls, text=title, variable=value,
                           command=self._on_item_adjust, bg=PANEL, fg=FG,
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

    def _set_folder(self, folder: Path, select_path: Path | None = None):
        if self.busy:
            self.status.set('Операция выполняется. Сначала дождись её завершения или останови.')
            return
        self.folder = folder
        self.folder_label.set(str(folder))
        self.files = list_source_images(folder, exclude=self.frame_path)
        self.file_list.delete(0, tk.END)
        for p in self.files:
            self.file_list.insert(tk.END, p.name)
        self.status.set(f'Исходных артов: {len(self.files)}. Результаты сохраняются рядом с оригиналами.')
        if self.files:
            selected_path = select_path if select_path in self.files else self.files[0]
            index = self.files.index(selected_path)
            self.file_list.selection_set(index)
            self.file_list.see(index)
            self._select(selected_path)
        else:
            self.selected = None
            self._show_token(None)
            self.preview_label.set('Нет изображений в папке')
        self._schedule_save()

    def _choose_ring(self):
        filename = filedialog.askopenfilename(title='Выбери PNG-кольцо',
                                               filetypes=[('PNG images', '*.png'), ('Все файлы', '*.*')])
        if filename:
            try:
                self._set_frame(Path(filename))
                if self.selected and self.current_cutout is not None:
                    self._render()
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
        stored = self.settings.get('item_overrides', {}).get(str(path.resolve()), {})
        if not isinstance(stored, dict):
            stored = {}
        defaults = {'layout': self.default_item_layout.get()}
        defaults.update({key: stored[key] for key in ('layout', 'zoom', 'x', 'y', 'rotation', 'flip_horizontal', 'flip_vertical', 'clean_noise')
                         if key in stored})
        return ItemAdjust(**defaults)

    def _on_default_item_layout_change(self):
        # A new default applies to uncustomized items, not to explicit per-item
        # overrides. Keep the preview and the next batch in agreement.
        if self.selected is None or str(self.selected.resolve()) not in self.settings.get('item_overrides', {}):
            self.item_layout.set(self.default_item_layout.get())
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

    def _on_item_adjust(self, _value=None):
        if self.sliders_updating:
            return
        if self.selected is not None:
            adj = self._current_item_adjust()
            self.settings.setdefault('item_overrides', {})[str(self.selected.resolve())] = vars(adj)
            if self.current_cutout is not None:
                self._render()
            else:
                self._request_preview()
        self._schedule_save()

    def _adjust_for(self, path):
        stored = self.settings.get('overrides', {}).get(str(path.resolve()), {})
        return Adjust(**{key: stored[key] for key in ('zoom', 'x', 'y', 'gap') if key in stored})

    def _select(self, path):
        self.selected = path
        self.preview_label.set(path.name)
        self.sliders_updating = True
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
        self.sliders_updating = False
        self.current_cutout = None
        self.current_key = None
        has_token = self._show_saved_token(path)
        model, edge = self.model.get(), self.edge_mode.get()
        keep = self.keep_alpha.get()
        try:
            key = self._cache_key(path, model, keep, edge)
            has_cached_cutout = (key in self.cache or
                                 self.disk_cache.path_for(path, model, keep, edge, identity=key[:4]).is_file())
        except OSError:
            self.status.set(f'Исходный файл недоступен: {path.name}')
            return
        # Navigating existing tokens should not waste time running the AI.
        # Extract only when the user begins editing or requests the raw cutout.
        if not has_token or has_cached_cutout:
            self._request_preview()
        else:
            self.status.set(f'Готовый токен: {path.name}. Для редактирования измени ползунок.')

    def _show_saved_token(self, source: Path):
        # Show the finished token immediately; full editable cutout is loaded
        # from disk or extracted asynchronously afterwards.
        if self.work_mode.get() == 'item':
            candidates = [item_output_path(source)]
        else:
            candidates = [token_output_path(source, ext)
                          for ext in dict.fromkeys([self.ext.get(), 'webp', 'png'])]
        for token in candidates:
            if token.is_file():
                try:
                    with Image.open(token) as existing:
                        existing.load()
                        self._show_token(existing.convert('RGBA'))
                    return True
                except (OSError, ValueError):
                    pass
        self._show_token(None)
        return False

    def _current_adjust(self):
        return Adjust(self.zoom.get(), self.x.get(), self.y.get(), self.gap.get())

    def _on_adjust(self, _value=None):
        if self.sliders_updating or self.selected is None:
            return
        adj = self._current_adjust()
        self.settings.setdefault('overrides', {})[str(self.selected.resolve())] = vars(adj)
        self._schedule_save()
        if self.current_cutout is not None:
            self._render()
        else:
            self._request_preview()

    def _on_show_cutout(self):
        if self.current_cutout is not None:
            self._render()
        elif self.show_cutout.get():
            self._request_preview()
        else:
            self._show_saved_token(self.selected) if self.selected else self._show_token(None)

    def _reset_current(self):
        if self.selected is None:
            return
        key = 'item_overrides' if self.work_mode.get() == 'item' else 'overrides'
        self.settings.setdefault(key, {}).pop(str(self.selected.resolve()), None)
        self._select(self.selected)
        self._schedule_save()

    def _on_model_change(self):
        self._schedule_save()
        if self.selected:
            self.current_cutout = None
            self.current_key = None
            self._request_preview()

    def _cache_key(self, path, model, keep_alpha, edge_mode):
        return (*source_identity(path), model, keep_alpha, edge_mode)

    def _get_cutout(self, path, model, keep_alpha, edge_mode):
        key = self._cache_key(path, model, keep_alpha, edge_mode)
        identity = key[:4]
        if key in self.cache:
            assert_source_identity(path, identity)
            return self.cache[key], key
        # Disk cache is checked before reading original and loading AI model.
        cutout = self.disk_cache.load(path, model, keep_alpha, edge_mode, identity=identity)
        if cutout is None:
            original = read_image(path)
            assert_source_identity(path, identity)
            if keep_alpha and already_transparent(original):
                cutout = original
            else:
                if model not in self.sessions:
                    from rembg import new_session
                    self.sessions[model] = new_session(model)
                cutout = remove_background(original, self.sessions[model], edge_mode)
            try:
                self.disk_cache.save(path, model, keep_alpha, edge_mode, cutout, identity=identity)
            except OSError:
                pass  # Unwritable cache must not prevent creating tokens.
        assert_source_identity(path, identity)
        self.cache[key] = cutout
        if len(self.cache) > 6:
            oldest = next(iter(self.cache))
            if oldest != key:
                self.cache.pop(oldest)
        return cutout, key

    def _request_preview(self):
        if not self.selected or (self.work_mode.get() == 'token' and self.frame is None):
            return
        if self.busy:
            self.preview_pending = True
            return
        path, model, keep_alpha, edge_mode = self.selected, self.model.get(), self.keep_alpha.get(), self.edge_mode.get()
        self.busy = True
        self.status.set(f'Подготовка: {path.name}. При первом запуске модель может скачиваться.')

        def work():
            try:
                img, key = self._get_cutout(path, model, keep_alpha, edge_mode)
                self.events.put(('preview', (path, img, key)))
            except Exception as exc:
                self.events.put(('error', f'{path.name}: {exc}'))
        threading.Thread(target=work, daemon=True).start()

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
            self.status.set(f'Ошибка компоновки: {exc}')
            return False

    @staticmethod
    def _make_cutout_preview(img: Image.Image, size: int) -> Image.Image:
        # This view shows the model's raw extraction, before ring clipping.
        from PIL import ImageOps
        bbox = img.getchannel('A').point(lambda p: 255 if p >= 16 else 0).getbbox()
        target = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        if not bbox:
            return target
        cropped = img.crop(bbox)
        shrunk = ImageOps.contain(cropped, (size - 28, size - 28), Image.Resampling.LANCZOS)
        target.alpha_composite(shrunk, ((size - shrunk.width) // 2, (size - shrunk.height) // 2))
        return target

    def _export_current(self):
        if self.selected:
            self._start_export([self.selected], force_overwrite=True)

    def _export_all(self):
        if not self.folder:
            messagebox.showinfo('Нет изображений', 'Сначала выбери папку с артами.')
            return
        # Re-scan to include files added while the program was open, but not
        # tokens or WebP copies produced by an earlier batch.
        fresh_files = list_source_images(self.folder, exclude=self.frame_path)
        if fresh_files != self.files:
            self.files = fresh_files
            self.file_list.delete(0, tk.END)
            for path in self.files:
                self.file_list.insert(tk.END, path.name)
            if self.selected in self.files:
                self.file_list.selection_set(self.files.index(self.selected))
        self._start_export(fresh_files)

    def _start_export(self, image_files, force_overwrite=False, requested_mode=None):
        if not self.folder or not image_files:
            messagebox.showinfo('Нет изображений', 'Сначала выбери папку с артами.')
            return
        if self.running_batch:
            self.status.set('Пакетная обработка уже выполняется.')
            return
        files = list(image_files)
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
        if force_overwrite and len(files) == 1:
            source = files[0]
            destination = item_output_path(source) if mode == 'item' else token_output_path(source, ext)
            if destination.exists() and not is_owned_export(destination, source, mode):
                try:
                    identity = source_identity(destination)
                except OSError as exc:
                    messagebox.showerror('Ошибка файла', str(exc))
                    return
                if not messagebox.askyesno(
                        'Подтверждение замены',
                        f'{destination.name} не помечен как результат TokenForge. '
                        'Это может быть старый токен или отдельный арт. Заменить его?'):
                    return
                approved_outputs[destination] = identity
        request = (files, mode, model, keep, edge_mode, ext, size, skip,
                   convert, frame, interior, adjustments, approved_outputs)
        if self.busy:
            self.pending_export = request
            self.status.set(f'В очереди: {len(files)} артов. Начнётся после текущего предпросмотра.')
            return
        self._run_export(request)

    def _run_export(self, request):
        (files, mode, model, keep, edge_mode, ext, size, skip,
         convert, frame, interior, adjustments, approved_outputs) = request
        self.busy = True
        self.running_batch = True
        self.cancel_event.clear()
        self.progress['value'] = 0
        self.status.set(f'Начата пакетная обработка: {len(files)} артов.')

        def work():
            try:
                get_cutout = lambda p: self._get_cutout(p, model, keep, edge_mode)[0]
                progress = lambda i, n, msg: self.events.put(('progress', (i, n, msg)))
                if mode == 'item':
                    result = export_items(files, get_cutout, adjustments,
                                          skip_exists=skip, convert_original=convert,
                                          cancel_event=self.cancel_event, on_progress=progress,
                                          approved_outputs=approved_outputs)
                else:
                    result = export_batch(files, get_cutout, frame, interior,
                                          adjustments, out_size=size, format_name=ext,
                                          skip_exists=skip, convert_original=convert,
                                          cancel_event=self.cancel_event, on_progress=progress,
                                          approved_outputs=approved_outputs)
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('batch_failure', traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def _after_preview(self):
        """Prioritize queued exports over another potentially slow preview."""
        if self.pending_export is not None:
            request = self.pending_export
            self.pending_export = None
            self._run_export(request)
        elif self.preview_pending:
            self.preview_pending = False
            self._request_preview()

    def _cancel(self):
        if self.running_batch:
            self.cancel_event.set()
            self.status.set('Остановка после текущего изображения…')
        elif self.pending_export is not None:
            self.pending_export = None
            self.status.set('Запланированная пакетная обработка отменена.')

    def _poll(self):
        try:
            while True:
                action, data = self.events.get_nowait()
                if action == 'preview':
                    path, img, key = data
                    self.busy = False
                    try:
                        matches = path == self.selected and key == self._cache_key(
                            path, self.model.get(), self.keep_alpha.get(), self.edge_mode.get())
                    except OSError:
                        matches = False
                    if matches:
                        self.current_cutout = img
                        self.current_key = key
                        self.preview_pending = False
                        if self._render():
                            self.status.set(f'Готово: {path.name}. Отрегулируй положение или экспортируй.')
                    else:
                        self.preview_pending = bool(self.selected and self.selected.is_file())
                        if not self.preview_pending:
                            self.status.set(f'Исходный файл недоступен: {path.name}')
                    self._after_preview()
                elif action == 'error':
                    self.busy = False
                    self.status.set('Ошибка: ' + str(data))
                    messagebox.showerror('Ошибка обработки', str(data))
                    self._after_preview()
                elif action == 'progress':
                    n, total, text = data
                    self.progress['value'] = n / max(1, total) * 100
                    self.status.set(text)
                elif action == 'done':
                    self.busy = False
                    self.running_batch = False
                    report = (f'Создано: {data.created}. Пропущено: {data.skipped}. '
                              f'Артов в WebP: {data.converted}. Ошибок: {len(data.errors)}. '
                              f'Всего исходников: {data.total}.')
                    if data.cancelled:
                        report = 'Остановлено. ' + report
                    self.status.set(report)
                    if data.errors:
                        messagebox.showwarning('Отчёт об ошибках', report + '\n\n' + '\n'.join(data.errors[:15]))
                    self._after_preview()
                elif action == 'batch_failure':
                    self.busy = False
                    self.running_batch = False
                    self.status.set('Критическая ошибка пакетной обработки.')
                    messagebox.showerror('Ошибка пакетной обработки', str(data))
                    self._after_preview()
        except queue.Empty:
            pass
        finally:
            self.root.after(120, self._poll)

    def _schedule_save(self):
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        self.save_timer = self.root.after(500, self._save)

    def _save(self):
        self.save_timer = None
        self.settings.update({
            'folder': str(self.folder) if self.folder else '',
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
        try:
            PREFS_DIR.mkdir(parents=True, exist_ok=True)
            content = json.dumps(self.settings, ensure_ascii=False, indent=2).encode('utf-8')
            atomic_write(PREFS_FILE, lambda fp: fp.write(content))
        except OSError as exc:
            self.status.set(f'Не удалось сохранить настройки: {exc}')

    def _on_close(self):
        self.cancel_event.set()
        self._save()
        self.root.destroy()

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
