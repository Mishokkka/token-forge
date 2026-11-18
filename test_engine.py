"""Run with python -m unittest test_engine.py (requires Pillow only)."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import sys
from pathlib import Path
from PIL import Image, ImageDraw
from token_engine import (Adjust, compose, frame_interior, read_image, save_token, remove_background,
                          save_original_webp, list_source_images, token_output_path, export_batch)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.frame = read_image(Path(__file__).parent / 'RingT.png')
        self.mask = frame_interior(self.frame)
        self.figure = Image.new('RGBA', (320, 500))
        draw = ImageDraw.Draw(self.figure)
        draw.ellipse((100, 10, 220, 160), fill=(180, 140, 100, 255))
        draw.polygon([(110, 140), (210, 140), (310, 480), (10, 480)], fill=(50, 90, 130, 255))

    def test_interior_has_no_exterior_pixels(self):
        self.assertEqual(self.mask.getpixel((0, 0)), 0)
        self.assertEqual(self.mask.getpixel((200, 200)), 255)
        self.assertEqual(self.mask.getpixel((200, 399)), 0)

    def test_render_shape_and_alpha(self):
        token = compose(self.figure, self.frame, self.mask, 256)
        self.assertEqual(token.size, (256, 256))
        self.assertEqual(token.getpixel((0, 0))[3], 0)
        self.assertGreater(token.getpixel((128, 128))[3], 0)

    def test_manual_position_changes_pixels(self):
        a = compose(self.figure, self.frame, self.mask, 256, Adjust())
        b = compose(self.figure, self.frame, self.mask, 256, Adjust(zoom=140))
        self.assertNotEqual(a.tobytes(), b.tobytes())

    def test_background_modes_forward_flags(self):
        values = []
        def fake_remove(img, **kwargs):
            values.append((img.mode, kwargs))
            return img.convert('RGBA')
        with patch.dict(sys.modules, {'rembg': SimpleNamespace(remove=fake_remove)}):
            for mode in ('Обычный', 'Очистка краёв', 'Alpha matting (медленно)'):
                remove_background(self.figure, 'fake-session', mode)
        self.assertEqual(values[0], ('RGB', {'session': 'fake-session'}))
        self.assertEqual(values[1][1]['decontaminate'], True)
        self.assertEqual(values[2][1]['alpha_matting'], True)

    def test_webp_retains_alpha(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'Herot.webp'
            save_token(compose(self.figure, self.frame, self.mask, 256), path)
            with Image.open(path) as reread:
                self.assertEqual(reread.size, (256, 256))
                self.assertEqual(reread.mode, 'RGBA')
                self.assertEqual(reread.getpixel((0, 0))[3], 0)

    def test_source_webp_conversion_preserves_original_and_alpha(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            original = folder / 'Demon.png'
            self.figure.save(original)
            before = original.read_bytes()
            self.assertTrue(save_original_webp(original))
            self.assertFalse(save_original_webp(original))
            self.assertEqual(original.read_bytes(), before)
            with Image.open(folder / 'Demon.webp') as converted:
                self.assertEqual(converted.size, self.figure.size)
                self.assertEqual(converted.mode, 'RGBA')
                self.assertEqual(converted.getpixel((0, 0))[3], 0)

    def test_source_scan_ignores_tokens_and_webp_copies(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name in ('Mage.png', 'Mage.webp', 'Maget.webp',
                         'Witch.webp', 'Witcht.webp', 'Artist.jpg', 'RingT.png'):
                image = self.figure.convert('RGB') if name.endswith('.jpg') else self.figure
                image.save(folder / name)
            found = list_source_images(folder, exclude=folder / 'RingT.png')
            self.assertEqual([p.name for p in found], ['Artist.jpg', 'Mage.png', 'Witch.webp'])
            self.assertEqual(token_output_path(found[1]), folder / 'Maget.webp')

    def test_batch_exports_all_sources_and_skips_existing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name in ('A.png', 'B.png', 'C.png'):
                self.figure.save(folder / name)
            files = list_source_images(folder)
            visits = []
            progress = []
            def cutout(path):
                visits.append(path.name)
                return read_image(path)
            result = export_batch(files, cutout, self.frame, self.mask,
                                  {p: Adjust() for p in files},
                                  on_progress=lambda *args: progress.append(args))
            self.assertEqual(result.created, 3)
            self.assertEqual(result.converted, 3)
            self.assertEqual(result.errors, [])
            self.assertEqual(visits, ['A.png', 'B.png', 'C.png'])
            self.assertEqual(len(progress), 6)
            for p in files:
                self.assertTrue(token_output_path(p).exists())
                self.assertTrue(p.with_suffix('.webp').exists())
            self.assertEqual(list_source_images(folder), files)
            repeated = export_batch(list_source_images(folder), cutout,
                                    self.frame, self.mask,
                                    {p: Adjust() for p in files})
            self.assertEqual(repeated.created, 0)
            self.assertEqual(repeated.skipped, 3)
            self.assertEqual(repeated.converted, 0)

    def test_batch_one_failed_image_does_not_abort(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name in ('A.png', 'B.png', 'C.png'):
                self.figure.save(folder / name)
            files = list_source_images(folder)
            def cutout(path):
                if path.name == 'B.png':
                    raise RuntimeError('Test failure')
                return read_image(path)
            result = export_batch(files, cutout, self.frame, self.mask,
                                  {p: Adjust() for p in files})
            self.assertEqual(result.created, 2)
            self.assertEqual(result.converted, 3)
            self.assertEqual(len(result.errors), 1)
            self.assertTrue(token_output_path(folder / 'C.png').exists())


if __name__ == '__main__':
    unittest.main()
