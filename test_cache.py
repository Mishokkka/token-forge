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
        self.assertIsNone(self.cache.load(self.source, 'm', False, 'e'))
        self.cache.save(self.source, 'm', False, 'e', image)
        stored = DiskCutoutCache(self.directory / 'cache').load(self.source, 'm', False, 'e')
        self.assertIsNotNone(stored)
        self.assertEqual(stored.tobytes(), image.tobytes())

    def test_cache_key_invalidation(self):
        baseline = self.cache.path_for(self.source, 'm', False, 'e')
        self.assertNotEqual(baseline, self.cache.path_for(self.source, 'm2', False, 'e'))
        self.assertNotEqual(baseline, self.cache.path_for(self.source, 'm', False, 'different'))
        self.assertNotEqual(baseline, self.cache.path_for(self.source, 'm', True, 'e'))
        Image.new('RGB', (171, 220), (60, 50, 40)).save(self.source)
        self.assertNotEqual(baseline, self.cache.path_for(self.source, 'm', False, 'e'))

    def test_corrupt_cache_is_discarded(self):
        path = self.cache.path_for(self.source, 'm', False, 'e')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'not an image')
        self.assertIsNone(self.cache.load(self.source, 'm', False, 'e'))
        self.assertFalse(path.exists())

    def make_mock_app(self):
        app = TokenForge.__new__(TokenForge)
        app.disk_cache = self.cache
        app.cache = {}
        app.sessions = {}
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
            a, _ = first._get_cutout(self.source, 'm', False, 'e')
            self.assertEqual(a.tobytes(), cutout.tobytes())
            second = self.make_mock_app()
            b, _ = second._get_cutout(self.source, 'm', False, 'e')
            self.assertEqual(b.tobytes(), cutout.tobytes())
            self.assertEqual(len(count), 1)
            second._get_cutout(self.source, 'm', False, 'different')
            self.assertEqual(len(count), 2)

    def test_single_export_overwrites_even_when_skip_enabled(self):
        frame = read_image(Path(__file__).parent / 'RingT.png')
        interior = frame_interior(frame)
        app = self.make_mock_app()
        app.folder = self.directory
        app.selected = self.source
        app.running_batch = False
        app.busy = False
        app.frame = frame
        app.interior = interior
        app.model = SimpleNamespace(get=lambda: 'm')
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

    def test_old_token_display_has_no_neural_load(self):
        existing = token_output_path(self.source)
        Image.new('RGBA', (256, 256), (31, 41, 51, 255)).save(existing, 'WEBP', lossless=True)
        app = self.make_mock_app()
        app.ext = SimpleNamespace(get=lambda: 'webp')
        app.work_mode = SimpleNamespace(get=lambda: 'token')
        displays = []
        app._show_token = lambda img: displays.append(img)
        self.assertTrue(app._show_saved_token(self.source))
        self.assertEqual(displays[0].size, (256, 256))


if __name__ == '__main__':
    unittest.main()
