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
        app.approval_pending = False
        app.busy = False
        app.worker = None
        app.preview_generation = 0
        app.preview_cached_only = False
        app.preview_retry = False
        app.scan_running = False
        app.scan_generation = 0
        app.pending_scan = None
        app.active_scan = None
        app.folder = None
        app.frame_path = Path(__file__).parent / 'RingT.png'
        app.preview_pending = False
        app.folder_preference = ''
        app.files = []
        app.selected = None
        app.pending_resets = set()
        app.save_retries = 0
        app.save_retryable = False
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
        with patch('tokenforge.threading.Thread') as launch:
            with patch('tokenforge.source_identity', side_effect=AssertionError('hash on Tk')), \
                 patch('tokenforge.inspect_existing_export', side_effect=AssertionError('read on Tk')):
                app._export_current()
            self.assertTrue(app.events.empty())
            launch.call_args.kwargs['target']()
            action, data = app.events.get_nowait()
            self.assertEqual(action, 'export_preflight')
            app._handle_worker_event(action, data)
            self.assertEqual(launch.call_count, 2)
            launch.call_args.kwargs['target']()
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
        app._run_export.assert_not_called()
        app.busy = False
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
        self.assertIsNone(app._load_saved_preview(self.source, 'token', 'webp'))
        self.assertEqual(images, [])

    def test_saved_preview_cannot_display_a_replaced_foreign_file(self):
        import token_engine
        from token_engine import save_generated
        existing = token_output_path(self.source)
        save_generated(Image.new('RGBA', (256, 256), 'red'), existing, self.source,
                       'token', skip_exists=False)
        app = self.make_mock_app()
        app.ext = SimpleNamespace(get=lambda: 'webp')
        app.work_mode = SimpleNamespace(get=lambda: 'token')
        displays = []
        app._show_token = displays.append
        original = token_engine._fingerprint
        def race(path, snapshot=None):
            result = original(path, snapshot)
            if path == existing and snapshot is not None:
                Image.new('RGBA', (32, 32), 'blue').save(existing)
            return result
        with patch('token_engine._fingerprint', side_effect=race):
            self.assertIsNone(app._load_saved_preview(self.source, 'token', 'webp'))
        self.assertEqual(displays, [])

    def test_saved_preview_io_runs_in_worker_and_stale_mode_is_ignored(self):
        import queue
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.selected = self.source
        app.frame = object()
        app.status, app._show_token = Mock(), Mock()
        app.events = queue.Queue()
        for name, value in {'work_mode': 'token', 'show_cutout': False, 'ext': 'webp',
                            'model': 'birefnet-general', 'keep_alpha': True, 'edge_mode': 'e'}.items():
            setattr(app, name, SimpleNamespace(get=lambda v=value: v))
        saved = Image.new('RGBA', (256, 256), 'red')
        app._get_cutout = Mock(return_value=(None, ('key',)))
        with patch('tokenforge.threading.Thread') as launch, \
             patch.object(app, '_load_saved_preview', return_value=saved) as load:
            app._request_preview(cached_only=True)
            load.assert_not_called()
            launch.call_args.kwargs['target']()
            load.assert_called_once_with(self.source, 'token', 'webp')
            self.assertTrue(app._get_cutout.call_args.kwargs['cached_only'])
        event, data = app.events.get_nowait()
        self.assertEqual(event, 'saved_preview')
        app._apply_worker_event(event, data)
        app._show_token.assert_called_once_with(saved)
        app._show_token.reset_mock()
        app.work_mode = SimpleNamespace(get=lambda: 'item')
        app._apply_worker_event(event, data)
        app._show_token.assert_not_called()

    def test_render_failure_clears_canvas_but_retains_cutout(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.current_cutout = Image.new('RGBA', (50, 50), (255, 0, 0, 7))
        app._show_token, app.status = Mock(), Mock()
        app.show_cutout = SimpleNamespace(get=lambda: False)
        app.work_mode = SimpleNamespace(get=lambda: 'item')
        from item_engine import ItemAdjust
        app._current_item_adjust = lambda: ItemAdjust(clean_noise=True)
        self.assertFalse(app._render())
        app._show_token.assert_called_once_with(None)
        self.assertIsNotNone(app.current_cutout)
        app._current_item_adjust = lambda: ItemAdjust(clean_noise=False)
        self.assertTrue(app._render())

    def test_close_decline_rearms_only_transient_save_failure(self):
        from unittest.mock import Mock
        import threading
        for retryable, answer, expected in ((True, False, True), (False, False, False), (True, True, False)):
            with self.subTest(retryable=retryable, answer=answer):
                app = self.make_mock_app()
                app.root, app.status = Mock(), Mock()
                app.save_timer = 'pending'
                app.cancel_event = threading.Event()
                app.save_retryable = retryable
                app._save = Mock(return_value=False)
                with patch('tokenforge.messagebox.askyesno', return_value=answer):
                    app._on_close()
                app.root.after_cancel.assert_called_once_with('pending')
                self.assertEqual(app.root.after.called, expected)
                self.assertEqual(app.closing, answer)

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

    def test_modal_preview_error_cannot_start_another_worker(self):
        import queue
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.root, app.status = Mock(), Mock()
        app.selected = self.source
        app.events = queue.Queue()
        app.pending_export = object()
        pending = app.pending_export
        app.preview_pending = False
        app.busy = True
        app.model = SimpleNamespace(get=lambda: 'birefnet-general')
        app.keep_alpha = SimpleNamespace(get=lambda: False)
        app.edge_mode = SimpleNamespace(get=lambda: 'e')
        app._show_token = Mock()
        app._request_preview = Mock()
        app._run_export = Mock()
        def nested(*args):
            self.assertTrue(app.busy)
            app._watch_source()
            app._after_preview()
            app._request_preview.assert_not_called()
            app._run_export.assert_not_called()
        app.events.put(('error', (app._preview_request(), 'temporary IO error', True, False)))
        with patch('tokenforge.messagebox.showerror', side_effect=nested) as dialog:
            app._poll()
            dialog.assert_called_once()
        app._run_export.assert_called_once_with(pending)
        self.assertIsNone(app.pending_export)

    def test_approval_pins_source_before_nested_callbacks(self):
        import queue
        import threading
        from unittest.mock import Mock
        from token_engine import Adjust
        app = self.make_mock_app()
        app.folder = self.directory
        app.selected = self.source
        app.running_batch = app.busy = False
        app.pending_export = None
        app.frame = read_image(Path(__file__).parent / 'RingT.png')
        app.interior = frame_interior(app.frame)
        app.status, app.root = Mock(), Mock()
        app.progress, app.events, app.cancel_event = {}, queue.Queue(), threading.Event()
        for name, value in {'work_mode': 'token', 'model': 'birefnet-general', 'keep_alpha': True,
                            'edge_mode': 'Обычный', 'ext': 'webp', 'size': '256',
                            'skip_exists': True, 'convert_original': True}.items():
            setattr(app, name, SimpleNamespace(get=lambda v=value: v))
        app._adjust_for = lambda p: Adjust()
        app.current_key = app._cache_key(self.source, 'birefnet-general', True, 'Обычный')
        app.current_cutout = read_image(self.source)
        dest = token_output_path(self.source)
        Image.new('RGBA', (30, 30), 'blue').save(dest)
        before = dest.read_bytes()
        def nested(*args):
            self.assertTrue(app.approval_pending)
            Image.new('RGBA', (20, 30), 'red').save(self.source)
            app.current_key = app._cache_key(self.source, 'birefnet-general', True, 'Обычный')
            app._start_export([self.source], force_overwrite=True)
            return True
        with patch('tokenforge.messagebox.askyesno', side_effect=nested) as dialog, \
             patch('tokenforge.threading.Thread', side_effect=lambda target, daemon: SimpleNamespace(start=target)):
            app._export_current()
            dialog.assert_not_called()
            action, data = app.events.get_nowait()
            self.assertEqual(action, 'export_preflight')
            app._handle_worker_event(action, data)
            dialog.assert_called_once()
        events = []
        while not app.events.empty():
            events.append(app.events.get_nowait())
        self.assertEqual(events[-1][0], 'done')
        result = events[-1][1]
        self.assertEqual((result.created, result.converted), (0, 0))
        self.assertTrue(result.errors)
        self.assertEqual(dest.read_bytes(), before)
        self.assertFalse(self.source.with_suffix('.webp').exists())

    def test_manual_preflight_rejects_source_changed_while_queued(self):
        import queue
        import threading
        from unittest.mock import Mock
        from token_engine import file_stamp
        app = self.make_mock_app()
        app.events, app.cancel_event, app.status = queue.Queue(), threading.Event(), Mock()
        dest = token_output_path(self.source)
        stamp = file_stamp(self.source)
        request = ([self.source], 'token') + (None,) * 13 + (
            (self.source, dest, stamp, None, app.frame_path),)
        with patch('tokenforge.threading.Thread') as launch:
            app._run_preflight(request)
            Image.new('RGBA', (30, 40), 'blue').save(self.source)
            launch.call_args.kwargs['target']()
        event, message = app.events.get_nowait()
        self.assertEqual(event, 'preflight_failure')
        self.assertIn('изменился', message)
        self.assertFalse(dest.exists())

    def test_cancelled_preflight_never_queues_export(self):
        import threading
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.cancel_event = threading.Event()
        app.cancel_event.set()
        app.pending_export = None
        app._run_export = Mock()
        with patch('tokenforge.messagebox.askyesno', side_effect=AssertionError('dialog after cancel')):
            app._handle_worker_event('export_preflight', (object(), self.source, True))
        self.assertIsNone(app.pending_export)
        app._run_export.assert_not_called()

    def test_worker_start_failure_posts_terminal_event(self):
        import queue
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.events = queue.Queue()
        worker = Mock()
        worker.start.side_effect = RuntimeError('thread unavailable')
        with patch('tokenforge.threading.Thread', return_value=worker):
            app._launch_worker(Mock(), lambda exc: app.events.put(('error', str(exc))))
        self.assertEqual(app.events.get_nowait(), ('error', 'thread unavailable'))

    def test_worker_system_exit_posts_terminal_event(self):
        import queue
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.events = queue.Queue()
        with patch('tokenforge.threading.Thread', side_effect=lambda target, daemon: SimpleNamespace(start=target)):
            app._launch_worker(Mock(side_effect=SystemExit(1)),
                               lambda exc: app.events.put(('error', app._worker_error_text(exc))))
        name, message = app.events.get_nowait()
        self.assertEqual(name, 'error')
        self.assertIn('код 1', message)

    def test_reset_forces_preview_instead_of_saved_token(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.work_mode = SimpleNamespace(get=lambda: 'token')
        app.selected = self.source
        key = str(self.source.resolve())
        app.settings = {'overrides': {key: {'zoom': 150}}}
        app._settings_baseline = {'overrides': {key: {'zoom': 150}}}
        app._select = Mock()
        app._schedule_save = Mock()
        app._reset_current()
        app._select.assert_called_once_with(self.source, force_preview=True)
        self.assertNotIn(key, app.settings['overrides'])

    def test_explicit_selection_preview_does_not_show_old_token(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.settings = {}
        app.preview_label = Mock()
        for name in ('zoom', 'x', 'y', 'gap', 'item_layout', 'item_zoom', 'item_x', 'item_y',
                     'item_rotation', 'item_flip_horizontal', 'item_flip_vertical', 'item_clean_noise'):
            setattr(app, name, Mock())
        from token_engine import Adjust
        from item_engine import ItemAdjust
        app._current_adjust = Mock(return_value=Adjust())
        app._current_item_adjust = Mock(return_value=ItemAdjust())
        app._adjust_for = Mock(return_value=Adjust())
        app._item_adjust_for = Mock(return_value=ItemAdjust())
        app._show_saved_token, app._show_token, app._request_preview = Mock(), Mock(), Mock()
        app._select(self.source, force_preview=True)
        app._show_saved_token.assert_not_called()
        app._show_token.assert_called_once_with(None)
        app._request_preview.assert_called_once_with()

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
        app._apply_folder_scan((2, self.directory, self.source, None, False, False),
                               [self.source], False, None)
        self.assertEqual(app.folder, self.directory)
        self.assertEqual(app.folder_preference, 'saved')
        app._schedule_save.assert_not_called()

    def test_ring_refresh_follows_latest_selection(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.folder = self.directory
        app.scan_generation = 3
        app.folder_label, app.status, app.file_list = Mock(), Mock(), Mock()
        a, b = self.source, self.directory / 'Second.png'
        app.selected = b
        app._select, app._schedule_save, app._clear_selection = Mock(), Mock(), Mock()
        app._apply_folder_scan((3, self.directory, a, None, False, True), [a, b], False, None)
        app.file_list.selection_set.assert_called_with(1)
        app._select.assert_not_called()
        app._apply_folder_scan((3, self.directory, a, None, True, False), [a, b], False, None)
        app._select.assert_called_once_with(a)

    def test_frame_refresh_does_not_scan_folder_on_tk(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.folder = self.directory
        app.selected = self.source
        app.frame_path = self.directory / 'Frame.png'
        app.files = [self.source] + [self.directory / f'Art{i}.png' for i in range(1000)]
        app._queue_scan, app._clear_selection, app._set_file_list = Mock(), Mock(), Mock()
        with patch('tokenforge.excluded_file', return_value=False) as check:
            app._refresh_frame_sources()
            check.assert_called_once_with(self.source, app.frame_path)
        app._clear_selection.assert_not_called()
        app._set_file_list.assert_not_called()
        app._queue_scan.assert_called_once_with(self.directory, persist=False, follow_selection=True)
        app.file_list = Mock()
        with patch('tokenforge.excluded_file', return_value=True) as check:
            app._refresh_frame_sources()
            check.assert_called_once_with(self.source, app.frame_path)
        app._clear_selection.assert_called_once()

    def test_stale_list_cannot_select_new_frame_alias(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.frame_path = self.source
        app.file_list, app.status, app._clear_selection = Mock(), Mock(), Mock()
        app._request_preview = Mock()
        app._select(self.source)
        app._clear_selection.assert_called_once()
        app.file_list.selection_clear.assert_called_once()
        app._request_preview.assert_not_called()

    def test_folder_export_builds_validated_adjustments_only_in_worker(self):
        import queue
        import threading
        from unittest.mock import Mock
        from token_engine import BatchResult
        app = self.make_mock_app()
        app.folder = self.directory
        app.selected = self.source
        app.running_batch = False
        app.pending_export = None
        app.frame = read_image(Path(__file__).parent / 'RingT.png')
        app.interior = frame_interior(app.frame)
        app.events, app.cancel_event, app.progress = queue.Queue(), threading.Event(), {}
        app.status = Mock()
        app.settings = {'overrides': {str(self.source.resolve()): {'zoom': 140, 'x': 'bad'}}}
        for name, value in {'work_mode': 'token', 'model': 'birefnet-general', 'keep_alpha': True,
                            'edge_mode': 'Обычный', 'ext': 'webp', 'size': '256',
                            'skip_exists': True, 'convert_original': False}.items():
            setattr(app, name, SimpleNamespace(get=lambda v=value: v))
        app._adjust_for = Mock(side_effect=AssertionError('Tk resolved every source'))
        with patch('tokenforge.threading.Thread') as launch, \
             patch('tokenforge.list_source_images', return_value=[self.source]), \
             patch('tokenforge.export_batch', return_value=BatchResult(errors=[])) as export:
            with patch.object(Path, 'resolve', side_effect=AssertionError('Tk resolved a path')):
                app._start_export([self.source], scan_folder=self.directory)
            app._adjust_for.assert_not_called()
            export.assert_not_called()
            # The worker uses the captured settings, not subsequent GUI edits.
            app.settings['overrides'][str(self.source.resolve())]['zoom'] = 60
            launch.call_args.kwargs['target']()
            adjustment = export.call_args.args[4][self.source]
            self.assertEqual((adjustment.zoom, adjustment.x), (140, 0))

    def test_malformed_overrides_are_validated_field_by_field(self):
        from item_engine import ITEM_MODES
        app = self.make_mock_app()
        key = str(self.source.resolve())
        app.settings = {'overrides': {key: {'zoom': 130, 'x': 'bad', 'gap': float('nan'), 'y': True}},
                        'item_overrides': {key: {'zoom': 140, 'x': 10**400, 'y': 20, 'rotation': None,
                                                 'layout': [], 'flip_horizontal': 'false',
                                                 'flip_vertical': True, 'clean_noise': False}}}
        app.default_item_layout = SimpleNamespace(get=lambda: ITEM_MODES[2])
        token = app._adjust_for(self.source)
        item = app._item_adjust_for(self.source)
        self.assertEqual((token.zoom, token.x, token.y, token.gap), (130, 0, 0, 11))
        self.assertEqual((item.zoom, item.x, item.y, item.rotation), (100, 0, 20, 0))
        self.assertEqual(item.layout, ITEM_MODES[2])
        self.assertEqual((item.flip_horizontal, item.flip_vertical, item.clean_noise), (False, True, False))
        self.assertEqual(app.settings['item_overrides'][key]['zoom'], 140)

    def test_failed_selection_load_clears_stale_preview_and_unlocks_sliders(self):
        from unittest.mock import Mock
        app = self.make_mock_app()
        app.current_cutout = Image.new('RGBA', (10, 10), 'red')
        app.current_key = ('old',)
        app.preview_label, app.status, app._show_token = Mock(), Mock(), Mock()
        app._adjust_for = Mock(side_effect=OSError('read failed'))
        app._request_preview = Mock()
        app._select(self.source)
        self.assertFalse(app.sliders_updating)
        self.assertIsNone(app.current_key)
        self.assertIsNone(app.current_cutout)
        app._show_token.assert_called_once_with(None)
        app._request_preview.assert_not_called()

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
        self.assertEqual(app._load_saved_preview(self.source, 'token', 'webp').size, (256, 256))
        self.assertEqual(displays, [])


if __name__ == '__main__':
    unittest.main()
