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

    def test_small_zoom_does_not_crop_outside_source(self):
        from unittest.mock import patch
        image = Image.new('RGBA', (2500, 2500), (20, 30, 40, 255))
        crop = Image.Image.crop
        def guarded(img, box=None):
            if box is not None:
                self.assertGreaterEqual(min(box[:2]), 0)
                self.assertLessEqual(box[2], img.width)
                self.assertLessEqual(box[3], img.height)
            return crop(img, box)
        with patch.object(Image.Image, 'crop', guarded):
            self.assertEqual(compose(image, self.frame, self.mask, 512, Adjust(zoom=5)).size, (512, 512))

    def test_open_thick_ring_rejected(self):
        frame = Image.new('RGBA', (100, 100))
        draw = ImageDraw.Draw(frame)
        draw.rectangle((0, 0, 99, 99), fill='black')
        draw.rectangle((30, 30, 70, 70), fill=(0, 0, 0, 0))
        draw.rectangle((45, 0, 55, 30), fill=(0, 0, 0, 0))
        with self.assertRaises(ValueError):
            frame_interior(frame)

    def test_atomic_write_preserves_previous_file(self):
        from token_engine import atomic_write
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'output.webp'
            dest.write_bytes(b'old')
            def failed(fp):
                fp.write(b'partial')
                raise OSError('encoder failed')
            with self.assertRaises(OSError):
                atomic_write(dest, failed)
            self.assertEqual(dest.read_bytes(), b'old')
            self.assertFalse(atomic_write(dest, lambda fp: fp.write(b'new'), replace=False))
            self.assertEqual(dest.read_bytes(), b'old')
            self.assertEqual(list(Path(directory).iterdir()), [dest])

    def test_square_dilation_matches_pillow(self):
        from token_engine import square_dilate
        from PIL import ImageFilter
        import random
        rng = random.Random(7)
        mask = Image.frombytes('L', (29, 23), bytes(rng.randrange(256) for _ in range(29*23)))
        for radius in (1, 2, 3, 5, 12):
            self.assertEqual(square_dilate(mask, radius).tobytes(),
                             mask.filter(ImageFilter.MaxFilter(radius*2+1)).tobytes())

    def test_replacement_preserves_file_that_races_publication(self):
        from token_engine import atomic_write, source_identity
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'output.webp'
            dest.write_bytes(b'old')
            prior = source_identity(dest)
            def race():
                dest.write_bytes(b'external editor')
            result = atomic_write(dest, lambda fp: fp.write(b'new'),
                                  before_publish=race, expected_destination=prior)
            self.assertTrue(result)
            self.assertTrue(any('сохранена' in warning for warning in result.warnings))
            self.assertEqual(dest.read_bytes(), b'new')
            recovered = list((dest.parent / '.tokenforge-recovery').iterdir())
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].read_bytes(), b'external editor')

    def test_both_exporters_reject_changed_input_during_composition(self):
        from item_engine import export_items, ItemAdjust, item_output_path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'Hero.png'
            for mode in ('token', 'item'):
                self.figure.save(source)
                def race(*args):
                    Image.new('RGBA', (32, 32), 'red').save(source)
                    return Image.new('RGBA', (256, 256), 'blue')
                if mode == 'token':
                    with patch('token_engine.compose', side_effect=race):
                        result = export_batch([source], read_image, self.frame, self.mask,
                            {source: Adjust()}, convert_original=False)
                    dest = token_output_path(source)
                else:
                    with patch('item_engine.compose_item', side_effect=race):
                        result = export_items([source], read_image, {source: ItemAdjust()},
                                              convert_original=False)
                    dest = item_output_path(source)
                self.assertEqual(result.created, 0)
                self.assertTrue(result.errors)
                self.assertIn('изменился', result.errors[0])
                self.assertFalse(dest.exists())

    def test_late_source_change_is_reported_as_published_warning(self):
        from token_engine import save_generated, source_identity
        from unittest.mock import patch
        import token_engine
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'A.png'
            self.figure.save(source)
            output = token_output_path(source)
            save_generated(self.figure, output, source, 'token', skip_exists=False)
            identity = source_identity(source)
            replace = token_engine._replace_preserving_displaced
            def race(*args):
                Image.new('RGBA', (30, 30), 'red').save(source)
                return replace(*args)
            with patch('token_engine._replace_preserving_displaced', side_effect=race):
                result = save_generated(self.figure, output, source, 'token', skip_exists=False,
                                        input_identity=identity)
            self.assertTrue(result)
            self.assertTrue(any('исходник изменился' in w for w in result.warnings))
            self.assertTrue(output.is_file())

    def test_source_edit_during_destination_validation_rejected(self):
        from token_engine import save_generated, source_identity, assert_source_identity
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'A.png'
            self.figure.save(source)
            output = token_output_path(source)
            save_generated(self.figure, output, source, 'token', skip_exists=False)
            identity = source_identity(source)
            original = output.read_bytes()
            def race(path, expected):
                assert_source_identity(path, expected)
                if path == output:
                    Image.new('RGBA', (30, 30), 'red').save(source)
            with patch('token_engine.assert_source_identity', side_effect=race):
                with self.assertRaisesRegex(OSError, 'изменился'):
                    save_generated(self.figure, output, source, 'token', skip_exists=False,
                                   input_identity=identity)
            self.assertEqual(output.read_bytes(), original)

    def test_oversized_source_is_discovered_without_disabling_limit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            path = folder / 'A.png'
            Image.new('RGB', (60, 60)).save(path)
            with patch.object(Image, 'MAX_IMAGE_PIXELS', 100):
                self.assertEqual(list_source_images(folder), [path])
                result = export_batch([path], read_image, self.frame, self.mask,
                                      {path: Adjust()}, convert_original=False)
                self.assertTrue(result.errors)
                self.assertEqual(result.created, 0)

    def test_frame_mask_feather_does_not_escape_thin_ring(self):
        from token_engine import _enclosed_shape
        from PIL import ImageChops
        frame = Image.new('RGBA', (120, 120))
        draw = ImageDraw.Draw(frame)
        draw.rectangle((10, 10, 109, 109), outline='black', width=1)
        for rect in ((10, 48, 25, 71), (94, 48, 109, 71),
                     (48, 10, 71, 25), (48, 94, 71, 109)):
            draw.rectangle(rect, fill='black')
        mask = frame_interior(frame)
        shape = _enclosed_shape(frame.getchannel('A').point(lambda a: 128 if a else 0))
        self.assertIsNone(ImageChops.subtract(mask, shape).getbbox())
        for size in (63, 256):
            # Use a fully opaque artwork to exercise the composition.
            result = compose(Image.new('RGBA', (120, 120), 'red'), frame, mask, size,
                             Adjust(zoom=320, gap=-20))
            support = mask.point(lambda a: 255 if a else 0).resize((size, size), Image.Resampling.NEAREST)
            frame_alpha = frame.resize((size, size), Image.Resampling.LANCZOS).getchannel('A')
            exterior = ImageChops.multiply(result.getchannel('A'), support.point(lambda a: 255-a))
            allowed = ImageChops.multiply(frame_alpha, support.point(lambda a: 255-a))
            self.assertEqual(exterior.tobytes(), allowed.tobytes())

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows replacement API')
    def test_partial_windows_replace_restores_previous_output(self):
        from token_engine import atomic_write, source_identity
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / 'output.webp'
            dest.write_bytes(b'old')
            identity = source_identity(dest)
            def fail(temporary, destination, backup):
                destination.rename(backup)
                exc = OSError('partial replacement')
                exc.winerror = 1177
                raise exc
            with patch('token_engine._windows_replace', side_effect=fail):
                with self.assertRaisesRegex(OSError, 'восстановлен'):
                    atomic_write(dest, lambda fp: fp.write(b'new'), expected_destination=identity)
            self.assertEqual(dest.read_bytes(), b'old')
            self.assertFalse(list(dest.parent.glob('.tokenforge-*.tmp')))

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
            from token_engine import save_generated
            for name in ('Mage.png', 'Witch.webp', 'Artist.jpg', 'RingT.png'):
                image = self.figure.convert('RGB') if name.endswith('.jpg') else self.figure
                image.save(folder / name)
            save_original_webp(folder / 'Mage.png')
            for stem, suffix in (('Mage', '.png'), ('Witch', '.webp')):
                save_generated(self.figure, folder / (stem + 't.webp'),
                               folder / (stem + suffix), 'token', skip_exists=False)
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
