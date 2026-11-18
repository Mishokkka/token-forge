"""Persistent caching and targeted re-export regression tests (CPU/GUI not required)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from PIL import Image

from token_engine import DiskCutoutCache, frame_interior, read_image, token_output_path
from tokenforge import TokenForge


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / 'Portrait.png'
        Image.new('RGB', (170, 220), (60, 50, 40)).save(self.source)
        self.cache = DiskCutoutCache(self.directory / 'cache')

    def test_disk_cache_persists_full_rgba_unchanged(self):
        image = Image.new('RGBA', (170, 220), (11, 22, 33, 0))
        image.putpixel((20, 30), (71, 92, 154, 157))
        self.assertIsNone(self.cache.load(self.source, 'birefnet-general', False, 'e'))
        self.cache.save(self.source, 'birefnet-general', False, 'e', image)
        stored = DiskCutoutCache(self.directory / 'cache').load(self.source, 'birefnet-general', False, 'e')
        self.assertIsNotNone(stored)
        self.assertEqual(stored.tobytes(), image.tobytes())

    def test_cache_key_invalidation(self):
        baseline = self.cache.path_for(self.source, 'birefnet-general', False, 'e')
        self.assertNotEqual(baseline, self.cache.path_for(self.source, 'birefnet-portrait', False, 'e'))
        self.assertNotEqual(baseline, self.cache.path_for(self.source, 'birefnet-general', False, 'different'))
        self.assertNotEqual(baseline, self.cache.path_for(self.source, 'birefnet-general', True, 'e'))
        Image.new('RGB', (171, 220), (60, 50, 40)).save(self.source)
        self.assertNotEqual(baseline, self.cache.path_for(self.source, 'birefnet-general', False, 'e'))

    def test_same_size_same_mtime_replacement_invalidates_content_cache(self):
        import os
        from token_engine import source_identity
        bmp = self.directory / 'source.bmp'
        Image.new('RGB', (40, 20), 'red').save(bmp)
        info = bmp.stat()
        identity = source_identity(bmp)
        self.cache.save(bmp, 'birefnet-general', False, 'e', Image.new('RGBA', (40, 20), 'red'))
        Image.new('RGB', (40, 20), 'blue').save(bmp)
        os.utime(bmp, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(bmp.stat().st_size, info.st_size)
        self.assertEqual(bmp.stat().st_mtime_ns, info.st_mtime_ns)
        self.assertNotEqual(source_identity(bmp), identity)
        self.assertIsNone(self.cache.load(bmp, 'birefnet-general', False, 'e'))

    def test_corrupt_cache_is_discarded(self):
        path = self.cache.path_for(self.source, 'birefnet-general', False, 'e')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'not an image')
        self.assertIsNone(self.cache.load(self.source, 'birefnet-general', False, 'e'))
        self.assertTrue(path.exists())  # do not delete a potentially refreshed entry

    def test_unreadable_cache_cleanup_is_best_effort(self):
        path = self.cache.path_for(self.source, 'birefnet-general', False, 'e')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'corrupt')
        with patch.object(Path, 'unlink', side_effect=PermissionError('read-only')):
            self.assertIsNone(self.cache.load(self.source, 'birefnet-general', False, 'e'))

    def test_source_replaced_during_inference_does_not_poison_cache(self):
        app = self.make_mock_app()
        def replace_source(*args):
            Image.new('RGB', (171, 220), 'red').save(self.source)
            return Image.new('RGBA', (170, 220), 'blue')
        key = app._cache_key(self.source, 'birefnet-general', False, 'e')
        original_disk = self.cache.path_for(self.source, 'birefnet-general', False, 'e', identity=key[:4])
        with patch('tokenforge.remove_background', side_effect=replace_source) as extracted, \
             patch.dict('sys.modules', {'rembg': SimpleNamespace(new_session=lambda name: object())}):
            with self.assertRaises(OSError):
                app._get_cutout(self.source, 'birefnet-general', False, 'e')
        extracted.assert_called_once()
        self.assertNotIn(key, app.cache)
        self.assertFalse(original_disk.exists())
        self.assertIsNone(self.cache.load(self.source, 'birefnet-general', False, 'e'))

    def test_snapshot_decodes_captured_bytes_even_if_path_is_replaced(self):
        from token_engine import source_identity, source_snapshot, read_image
        identity = source_identity(self.source)
        before = read_image(self.source).tobytes()
        with source_snapshot(self.source, identity) as captured:
            Image.new('RGBA', (50, 50), 'blue').save(self.source)
            with Image.open(captured) as stored:
                self.assertEqual(stored.convert('RGBA').tobytes(), before)
        with self.assertRaisesRegex(OSError, 'изменился'):
            read_image(self.source, identity=identity)

    def make_mock_app(self):
        app = TokenForge.__new__(TokenForge)
        app.disk_cache = self.cache
        app.cache = {}
        app.sessions = {}
        app.current_key = None
        app.current_cutout = None
        app.closing = False
        app.worker = None
        app.preview_generation = 0
        app.preview_cached_only = False
        app.preview_retry = False
        app.scan_running = False
        app.scan_generation = 0
        app.pending_scan = None
        app.active_scan = None
        app.folder = None
        app.folder_preference = ''
        app.files = []
        app.selected = None
        app.pending_resets = set()
        app.save_retries = 0
        app.widget_values = {}
        return app

    def test_reuses_disk_without_neural_model_after_restart(self):
        cutout = Image.new('RGBA', (170, 220), (24, 25, 26, 233))
        count = []
        def fake_cut(img, session, edge_mode):
            count.append(1)
            return cutout.copy()
        with patch('tokenforge.remove_background', side_effect=fake_cut), \
             patch.dict('sys.modules', {'rembg': SimpleNamespace(new_session=lambda name: object())}):
            first = self.make_mock_app()
            a, _ = first._get_cutout(self.source, 'birefnet-general', False, 'e')
            self.assertEqual(a.tobytes(), cutout.tobytes())
            second = self.make_mock_app()
            with patch('tokenforge.new_local_session', side_effect=AssertionError('model started on disk hit')) as init:
                b, _ = second._get_cutout(self.source, 'birefnet-general', False, 'e')
                init.assert_not_called()
            self.assertEqual(b.tobytes(), cutout.tobytes())
            self.assertEqual(len(count), 1)
            second._get_cutout(self.source, 'birefnet-general', False, 'different')
            self.assertEqual(len(count), 2)

    def test_single_export_overwrites_even_when_skip_enabled(self):
        frame = read_image(Path(__file__).parent / 'RingT.png')
        interior = frame_interior(frame)
        app = self.make_mock_app()
        app.folder = self.directory
        app.selected = self.source
        app.running_batch = False
        app.busy = False
        app.pending_export = None
        app.frame = frame
        app.interior = interior
        app.model = SimpleNamespace(get=lambda: 'birefnet-general')
        app.keep_alpha = SimpleNamespace(get=lambda: True)
        app.edge_mode = SimpleNamespace(get=lambda: 'Очистка краёв')
        app.ext = SimpleNamespace(get=lambda: 'webp')
        app.size = SimpleNamespace(get=lambda: '256')
        app.skip_exists = SimpleNamespace(get=lambda: True)
        app.convert_original = SimpleNamespace(get=lambda: False)
        app._adjust_for = lambda p: __import__('token_engine').Adjust()
        # Force overwrite is forwarded to batch flags; inspect instead of
        # starting a worker thread through the async GUI.
        captured = []
        app._start_export = lambda paths, force_overwrite=False: captured.append(force_overwrite)
        app._export_current()
        self.assertEqual(captured, [True])

    def test_real_selected_export_replaces_owned_file_with_global_skip_enabled(self):
        import queue
        import threading
        from unittest.mock import Mock
        from token_engine import Adjust, save_generated, source_identity
        app = self.make_mock_app()
        app.folder = self.directory
        app.selected = self.source
        app.running_batch = app.busy = False
        app.pending_export = None
        app.frame = read_image(Path(__file__).parent / 'RingT.png')
        app.interior = frame_interior(app.frame)
        app.status = Mock()
        app.progress = {}
        app.events = queue.Queue()
        app.cancel_event = threading.Event()
        def control(value):
            return SimpleNamespace(get=lambda: value)
        for name, value in {'work_mode': 'token', 'model': 'birefnet-general', 'keep_alpha': True,
                            'edge_mode': 'Обычный', 'ext': 'webp', 'size': '512',
                            'skip_exists': True, 'convert_original': False}.items():
            setattr(app, name, control(value))
        app._adjust_for = lambda path: Adjust()
        app._get_cutout = lambda path, *args: (read_image(path), source_identity(path) + args)
        destination = token_output_path(self.source)
        save_generated(Image.new('RGBA', (256, 256), 'red'), destination, self.source,
                       'token', skip_exists=False)
        with patch('tokenforge.threading.Thread', side_effect=lambda target, daemon: SimpleNamespace(start=target)):
            app._export_current()
        events = []
        while not app.events.empty():
            events.append(app.events.get_nowait())
        self.assertEqual(events[-1][0], 'done')
        self.assertEqual(events[-1][1].created, 1)
        with Image.open(destination) as result:
            self.assertEqual(result.size, (512, 512))

    def test_item_layout_default_is_independent_of_selected_override(self):
        from item_engine import ITEM_MODES
        app = self.make_mock_app()
        app.settings = {'item_overrides': {str(self.source.resolve()): {'layout': ITEM_MODES[1]}}}
        app.default_item_layout = SimpleNamespace(get=lambda: ITEM_MODES[0])
        app.item_layout = SimpleNamespace(get=lambda: ITEM_MODES[1])
        self.assertEqual(app._item_adjust_for(self.source).layout, ITEM_MODES[1])
        self.assertEqual(app._item_adjust_for(self.directory / 'Another.png').layout, ITEM_MODES[0])

    def test_queued_export_keeps_settings_and_pending_preview(self):
        from token_engine import Adjust
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.folder = self.directory
        app.running_batch = False
        app.busy = True
        app.preview_pending = True
        app.pending_export = None
        app.frame = read_image(Path(__file__).parent / 'RingT.png')
        app.interior = frame_interior(app.frame)
        def control(value):
            return SimpleNamespace(get=lambda: value)
        app.work_mode = control('token')
        app.model = control('model')
        app.keep_alpha = control(True)
        app.edge_mode = control('Обычный')
        app.ext = control('webp')
        app.size = control('256')
        app.skip_exists = control(True)
        app.convert_original = control(False)
        app.status = Mock()
        app._adjust_for = lambda path: Adjust(zoom=70)
        app._run_export = Mock()
        app._start_export([self.source])
        original_request = app.pending_export
        app._start_export([self.source], force_overwrite=True)
        self.assertIs(app.pending_export, original_request)
        app.ext, app.size = control('png'), control('1024')
        app._adjust_for = lambda path: Adjust(zoom=160)
        app._after_preview()
        request = app._run_export.call_args.args[0]
        self.assertEqual(request[5:7], ('webp', 256))
        self.assertEqual(request[11][self.source].zoom, 70)
        self.assertTrue(app.preview_pending)
        self.assertIsNone(app.pending_export)
        app._request_preview = Mock()
        app._after_preview()
        app._request_preview.assert_called_once_with(cached_only=False)

    def test_deleted_preview_source_does_not_stop_event_polling(self):
        import queue
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.events = queue.Queue()
        app.root = Mock()
        app.selected = self.source
        app.status = Mock()
        app.pending_export = None
        app.preview_pending = False
        app.busy = True
        app.model = SimpleNamespace(get=lambda: 'birefnet-general')
        app.keep_alpha = SimpleNamespace(get=lambda: False)
        app.edge_mode = SimpleNamespace(get=lambda: 'e')
        key = app._cache_key(self.source, 'birefnet-general', False, 'e')
        self.source.unlink()
        request = app._preview_request()
        app.preview_generation += 1
        app.selected = None
        app.events.put(('preview', (request, Image.new('RGBA', (1, 1)), key)))
        with patch.object(app, '_cache_key', side_effect=AssertionError('GUI hashed source')):
            app._poll()
        self.assertFalse(app.busy)
        self.assertFalse(app.preview_pending)
        app.root.after.assert_called_once_with(120, app._poll)

    def test_unmarked_saved_result_is_not_shown(self):
        existing = token_output_path(self.source)
        Image.new('RGBA', (256, 256), 'blue').save(existing)
        app = self.make_mock_app()
        app.ext = SimpleNamespace(get=lambda: 'webp')
        app.work_mode = SimpleNamespace(get=lambda: 'token')
        images = []
        app._show_token = images.append
        self.assertFalse(app._show_saved_token(self.source))
        self.assertEqual(images, [None])

    def test_raw_preview_handles_extremely_thin_art(self):
        img = Image.new('RGBA', (1200, 1), 'red')
        result = TokenForge._make_cutout_preview(img, 512)
        self.assertIsNotNone(result.getbbox())
        self.assertEqual(result.getbbox()[3] - result.getbbox()[1], 1)

    def test_stale_preview_error_does_not_open_dialog(self):
        import queue
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.root, app.status = Mock(), Mock()
        app.selected = self.directory / 'New.png'
        app.events = queue.Queue()
        app.pending_export = None
        app.preview_pending = True
        app.busy = True
        app.model = SimpleNamespace(get=lambda: 'birefnet-general')
        app.keep_alpha = SimpleNamespace(get=lambda: False)
        app.edge_mode = SimpleNamespace(get=lambda: 'e')
        app._request_preview = Mock()
        app.events.put(('error', ((0, self.source, 'birefnet-general', False, 'e'), 'old failure', True, False)))
        with patch('tokenforge.messagebox.showerror') as error:
            app._poll()
            error.assert_not_called()
        app._request_preview.assert_called_once_with(cached_only=False)

    def test_clamped_widget_does_not_overwrite_unedited_setting(self):
        from item_engine import ItemAdjust
        app = self.make_mock_app()
        name = str(self.source.resolve())
        app.settings = {'item_overrides': {name: {'zoom': 140}}}
        app.widget_values = {'item_overrides': vars(ItemAdjust(zoom=100))}
        self.assertTrue(app._record_adjustment('item_overrides', self.source,
                                             ItemAdjust(zoom=100, x=20), 'x'))
        self.assertEqual(app.settings['item_overrides'][name], {'zoom': 140, 'x': 20})
        self.assertTrue(app._record_adjustment('item_overrides', self.source,
                                             ItemAdjust(zoom=90, x=20), 'zoom'))
        self.assertEqual(app.settings['item_overrides'][name]['zoom'], 90)

    def test_preview_verification_runs_only_in_worker(self):
        import queue
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.events = queue.Queue()
        app.status = Mock()
        app.selected = self.source
        app.frame = object()
        app.busy = False
        app.preview_pending = False
        for name, value in {'work_mode': 'token', 'show_cutout': False, 'model': 'birefnet-general',
                            'keep_alpha': True, 'edge_mode': 'e'}.items():
            setattr(app, name, SimpleNamespace(get=lambda v=value: v))
        app.current_cutout = read_image(self.source)
        app.current_key = app._cache_key(self.source, 'birefnet-general', True, 'e')
        Image.new('RGBA', (171, 220), (255, 0, 0, 200)).save(self.source)
        worker = Mock()
        with patch('tokenforge.threading.Thread', return_value=worker) as launch, \
             patch.object(app, '_cache_key', wraps=app._cache_key) as hash_source:
            app._request_preview(verify=True)
            hash_source.assert_not_called()
            launch.call_args.kwargs['target']()
            hash_source.assert_called_once()
        self.assertEqual(app.events.get_nowait()[0], 'preview_invalidated')
        event = app.events.get_nowait()
        self.assertEqual(event[0], 'preview')
        self.assertNotEqual(event[1][2], app.current_key)

    def test_close_waits_for_export_worker_and_discards_queue(self):
        import queue
        import threading
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.root, app.status, app.worker = Mock(), Mock(), Mock()
        app.worker.is_alive.side_effect = [True, True, False]
        app.events = queue.Queue()
        app.save_timer = None
        app.cancel_event = threading.Event()
        app.pending_export = object()
        app.preview_pending = True
        app._save = Mock(return_value=True)
        app._on_close()
        self.assertTrue(app.closing)
        self.assertTrue(app.cancel_event.is_set())
        self.assertIsNone(app.pending_export)
        app.root.destroy.assert_not_called()
        app._poll()
        app.root.destroy.assert_not_called()
        app._poll()
        app.root.destroy.assert_called_once()

    def test_folder_scan_runs_in_worker_and_rejects_stale_result(self):
        import queue
        import threading
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.root, app.status, app.file_list, app.folder_label = Mock(), Mock(), Mock(), Mock()
        app._clear_selection = Mock()
        app.events = queue.Queue()
        app.cancel_event = threading.Event()
        app.running_batch = app.busy = False
        app.pending_export = None
        app.frame_path = self.directory / 'Ring.png'
        worker = Mock()
        with patch('tokenforge.threading.Thread', return_value=worker) as launch, \
             patch('tokenforge.list_source_images', return_value=[self.source]) as scan:
            app._set_folder(self.directory)
            scan.assert_not_called()
            launch.call_args.kwargs['target']()
            scan.assert_called_once()
        _, data = app.events.get_nowait()
        app.scan_generation += 1
        app._set_file_list = Mock()
        app._apply_folder_scan(*data)
        app._set_file_list.assert_not_called()

    def test_cancelled_scan_keeps_committed_folder_and_preference(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.folder = self.directory
        app.folder_preference = str(self.directory)
        app.files = [self.source]
        app.selected = self.source
        app.frame_path = self.directory / 'Ring.png'
        app.folder_label, app.status, app.cancel_event = Mock(), Mock(), Mock()
        app.running_batch = False
        app.scan_running = app.busy = True
        app.pending_export = None
        app._set_file_list = Mock()
        app._set_folder(self.directory / 'new')
        request = app.pending_scan
        app._cancel()
        app._apply_folder_scan(request, [], True, None)
        self.assertEqual(app.folder, self.directory)
        self.assertEqual(app.folder_preference, str(self.directory))
        self.assertEqual(app.files, [self.source])
        self.assertEqual(app.selected, self.source)
        app.folder_label.set.assert_called_with(str(self.directory))
        app.status.set.assert_called_with('Поиск остановлен.')

    def test_restored_scan_is_not_a_settings_edit(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.folder_preference = 'saved'
        app.scan_generation = 2
        app.folder_label, app.status, app.file_list = Mock(), Mock(), Mock()
        app._select, app._schedule_save, app._clear_selection = Mock(), Mock(), Mock()
        app._apply_folder_scan((2, self.directory, self.source, None, False),
                               [self.source], False, None)
        self.assertEqual(app.folder, self.directory)
        self.assertEqual(app.folder_preference, 'saved')
        app._schedule_save.assert_not_called()

    def test_exact_row_is_not_windows_path_equality(self):
        from pathlib import PureWindowsPath
        paths = [PureWindowsPath('Mage.png'), PureWindowsPath('mage.png')]
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(TokenForge._exact_row(paths, paths[1]), 1)

    def test_preview_verification_retries_without_cutout(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.root = Mock()
        app.preview_retry = True
        app.busy = False
        app._request_preview = Mock()
        app._watch_source()
        app._request_preview.assert_called_once_with(verify=True)

    def test_old_token_display_has_no_neural_load(self):
        existing = token_output_path(self.source)
        from token_engine import save_generated
        save_generated(Image.new('RGBA', (256, 256), (31, 41, 51, 255)),
                       existing, self.source, 'token', skip_exists=False)
        app = self.make_mock_app()
        app.ext = SimpleNamespace(get=lambda: 'webp')
        app.work_mode = SimpleNamespace(get=lambda: 'token')
        displays = []
        app._show_token = lambda img: displays.append(img)
        self.assertTrue(app._show_saved_token(self.source))
        self.assertEqual(displays[0].size, (256, 256))


if __name__ == '__main__':
    unittest.main()
