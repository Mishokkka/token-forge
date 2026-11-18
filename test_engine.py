"""Run with python -m unittest test_engine.py (requires Pillow only)."""
import unittest
import tempfile
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

    def test_batch_prepares_frame_once(self):
        from token_engine import prepare_token_frame
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            sources = [Path(directory) / name for name in ('One.png', 'Two.png')]
            for source in sources:
                self.figure.save(source)
            with patch('token_engine.prepare_token_frame', wraps=prepare_token_frame) as prepare:
                result = export_batch(sources, read_image, self.frame, self.mask,
                                      {source: Adjust() for source in sources}, convert_original=False)
            self.assertEqual(result.created, 2, result.errors)
            prepare.assert_called_once()

    def test_both_exporters_reject_changed_input_during_composition(self):
        from item_engine import export_items, ItemAdjust, item_output_path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'Hero.png'
            for mode in ('token', 'item'):
                self.figure.save(source)
                def race(*args, **kwargs):
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

    def test_tagged_art_is_normalized_to_srgb_and_copy_keeps_profile(self):
        from PIL import ImageCms
        import io
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Tagged.png'
            # Keep the sRGB primaries and white point, but use a linear
            # transfer curve. The transformed middle gray is not the input.
            import struct
            profile = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
            count = struct.unpack_from('>I', profile, 128)[0]
            for i in range(count):
                pos = 132 + 12 * i
                tag, offset, length = struct.unpack_from('>4sII', profile, pos)
                if tag in (b'rTRC', b'gTRC', b'bTRC'):
                    self.assertGreaterEqual(length, 16)
                    profile[offset:offset+16] = struct.pack('>4s4xIH2x', b'curv', 1, 256)
                    struct.pack_into('>I', profile, pos+8, 14)
            figure = self.figure.copy()
            figure.putpixel((0, 0), (64, 64, 64, 123))
            figure.save(source, icc_profile=bytes(profile))
            decoded = read_image(source)
            self.assertEqual(decoded.getpixel((0, 0)), (137, 137, 137, 123))
            self.assertEqual(decoded.getchannel('A').tobytes(), figure.getchannel('A').tobytes())
            self.assertTrue(decoded.info.get('icc_profile'))
            self.assertTrue(save_original_webp(source))
            with Image.open(source.with_suffix('.webp')) as converted:
                self.assertTrue(converted.info.get('icc_profile'))
                parsed = ImageCms.ImageCmsProfile(io.BytesIO(converted.info['icc_profile']))
                self.assertIn('sRGB', ImageCms.getProfileDescription(parsed))

    def test_resource_limits_and_decoder_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            disguised = Path(d) / 'not-png.png'
            Image.new('RGB', (10, 10)).save(disguised, format='GIF')
            with self.assertRaises(OSError):
                read_image(disguised)
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            with patch('token_engine.MAX_IMAGE_PIXELS', 100):
                with self.assertRaises(ValueError):
                    read_image(source)
            with patch('token_engine.MAX_ENCODED_BYTES', 10):
                from token_engine import source_identity
                with self.assertRaises(OSError):
                    source_identity(source)

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

    def test_16bit_gray_preserves_tonal_range(self):
        from token_engine import decode_artwork
        gray = Image.new('I;16', (4, 1))
        gray.putdata([0, 16384, 32768, 65535])
        self.assertEqual(list(decode_artwork(gray).getdata()),
                         [(0, 0, 0), (64, 64, 64), (128, 128, 128), (255, 255, 255)])
        gray.info['transparency'] = 16384
        self.assertEqual(list(decode_artwork(gray).getchannel('A').getdata()), [255, 0, 255, 255])

    def test_tagged_16bit_gray_uses_compatible_color_transform(self):
        import io
        import struct
        from PIL import ImageCms
        from token_engine import decode_artwork
        # Minimal ICC v2 display profile: GRAY input, XYZ PCS, D50, gamma 2.2.
        # Build it in the test so no OS-installed profile is required.
        fixed = lambda value: round(value * 65536)
        white = struct.pack('>4s4xiii', b'XYZ ', fixed(.9642), fixed(1), fixed(.8249))
        curve = struct.pack('>4s4xIH2x', b'curv', 1, round(2.2 * 256))
        header = bytearray(128)
        header[8:12] = bytes((2, 16, 0, 0))
        header[12:24] = b'mntrGRAYXYZ '
        header[36:40] = b'acsp'
        header[68:80] = struct.pack('>iii', fixed(.9642), fixed(1), fixed(.8249))
        tags = struct.pack('>I4sII4sII', 2, b'wtpt', 156, len(white),
                           b'kTRC', 156 + len(white), len(curve))
        data = header + tags + white + curve
        data[:4] = struct.pack('>I', len(data))
        gray = Image.new('I;16', (4, 1))
        gray.putdata([0, 16384, 32768, 65535])
        gray.info['icc_profile'] = bytes(data)
        gray.info['transparency'] = 16384
        decoded = decode_artwork(gray)
        self.assertEqual(decoded.mode, 'RGBA')
        self.assertEqual(list(decoded.getchannel('A').getdata()), [255, 0, 255, 255])
        pixels = list(decoded.getchannel('R').getdata())
        self.assertTrue(pixels[0] < pixels[1] < pixels[2] < pixels[3])
        profile = ImageCms.ImageCmsProfile(io.BytesIO(decoded.info['icc_profile']))
        self.assertIn('sRGB', ImageCms.getProfileDescription(profile))

        # A real grayscale+alpha 16-bit PNG uses the RGBA decoder path.
        import zlib
        def chunk(kind, payload):
            return (struct.pack('>I', len(payload)) + kind + payload
                    + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))
        png = (b'\x89PNG\r\n\x1a\n'
               + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 1, 16, 4, 0, 0, 0))
               + chunk(b'iCCP', b'Gray\0\0' + zlib.compress(bytes(data)))
               + chunk(b'IDAT', zlib.compress(b'\0' + struct.pack('>HHHH', 16384, 32768, 65535, 16384)))
               + chunk(b'IEND', b''))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'GrayAlpha.png'
            path.write_bytes(png)
            decoded = read_image(path)
            self.assertEqual(list(decoded.getchannel('A').getdata()), [128, 64])
            self.assertLess(decoded.getpixel((0, 0))[0], decoded.getpixel((1, 0))[0])
            # A truecolor PNG carrying the same incompatible profile should
            # still be rejected, not silently reduced to grayscale.
            Image.new('RGBA', (2, 1), 'red').save(path, icc_profile=bytes(data))
            with self.assertRaisesRegex(ValueError, 'ICC'):
                read_image(path)

    def test_legacy_webp_discovery_does_not_open_decoder_or_allow_oversized_skip(self):
        from token_engine import read_export_owner, validate_skip, read_owned_export_image
        from token_cli import resolve_context_source
        from PIL import WebPImagePlugin
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            source, copy = folder / 'Hero.png', folder / 'Hero.webp'
            self.figure.save(source)
            exif = Image.Exif()
            exif[270] = 'TokenForge/2:["copy","Hero.png"]'
            self.figure.save(copy, 'WEBP', exif=exif)
            with patch('token_engine.MAX_IMAGE_PIXELS', 100), \
                 patch.object(WebPImagePlugin.WebPImageFile, '_open',
                              side_effect=AssertionError('unexpected decoder allocation')):
                self.assertEqual(read_export_owner(copy), (2, 'copy', 'Hero.png'))
                self.assertEqual(list_source_images(folder), [source])
                self.assertEqual(resolve_context_source(copy, frame_path=folder / 'Ring.png'), source)
            with patch('token_engine.MAX_IMAGE_PIXELS', 100):
                with self.assertRaises(OSError):
                    validate_skip(copy, source, 'copy')
                with self.assertRaises(ValueError):
                    read_owned_export_image(copy, source, 'copy')
            # Truncated RIFF must not be accepted as ownership evidence.
            copy.write_bytes(copy.read_bytes()[:-1])
            self.assertIsNone(read_export_owner(copy))

    def test_oversized_ring_and_discovery_reject_before_pixel_decode(self):
        from token_engine import load_frame, read_export_owner
        from PIL import PngImagePlugin
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'ring.png'
            Image.new('L', (4097, 4097)).save(path)
            with patch.object(PngImagePlugin.PngImageFile, 'load',
                              side_effect=AssertionError('unexpected pixel decode')):
                with self.assertRaisesRegex(ValueError, '4096'):
                    load_frame(path)
                with patch('token_engine.MAX_IMAGE_PIXELS', 100):
                    self.assertIsNone(read_export_owner(path))

    def test_frame_retry_records_actual_recipe_after_success(self):
        from token_engine import read_export_owner, token_recipe
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            adjust = Adjust()
            def export(loader, skip=True):
                return export_batch([source], read_image, None, None, {source: adjust},
                                    frame_loader=loader, convert_original=False, skip_exists=skip)
            loader = unittest.mock.Mock(side_effect=[OSError('not ready'), (self.frame, self.mask)])
            result = export(loader)
            self.assertEqual(result.created, 1, result.errors)
            token = token_output_path(source)
            self.assertEqual(read_export_owner(token)[4], token_recipe(self.frame, adjust, 256, 'webp', None))
            result = export(unittest.mock.Mock(side_effect=OSError('not ready')))
            self.assertEqual(result.skipped, 1)
            self.assertTrue(any('Не удалось проверить актуальность' in w for w in result.warnings))

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

    def test_folder_index_reuses_scan_and_detects_new_conflict(self):
        from token_engine import FolderIndex, ensure_no_source_collision
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            source = folder / 'Ca.png'
            self.figure.save(source)
            index = FolderIndex()
            with patch('token_engine.os.scandir', wraps=os.scandir) as scan:
                for _ in range(10):
                    ensure_no_source_collision(token_output_path(source), source, 'token', index)
                self.assertEqual(scan.call_count, 1)
                conflict = folder / 'Cat.jpg'
                self.figure.convert('RGB').save(conflict)
                # Force a distinct observable directory timestamp on filesystems
                # with low timestamp resolution.
                stat = folder.stat()
                os.utime(folder, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2000000000))
                with self.assertRaises(FileExistsError):
                    ensure_no_source_collision(token_output_path(source), source, 'token', index, live=True)
                self.assertEqual(scan.call_count, 1 if os.name == 'nt' else 2)

    def test_invalid_metadata_is_not_a_discovery_failure(self):
        from token_engine import _parse_owner
        self.assertIsNone(_parse_owner('other:' + '[' * 2000))
        self.assertIsNone(_parse_owner('TokenForge/2:' + '[' * 1500 + ']' * 1500))
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'A.png'
            exif = Image.Exif()
            exif[270] = 'TokenForge/2:' + '[' * 1500 + ']' * 1500
            self.figure.save(source, exif=exif)
            self.assertEqual(list_source_images(source.parent), [source])

    def test_copy_and_token_share_one_identity(self):
        import token_engine
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            old = token_engine.save_original_webp
            def change(*args, **kwargs):
                result = old(*args, **kwargs)
                Image.new('RGBA', (32, 32), 'red').save(source)
                return result
            with patch('token_engine.save_original_webp', side_effect=change):
                result = export_batch([source], read_image, self.frame, self.mask, {source: Adjust()})
            self.assertEqual((result.converted, result.created), (1, 0))
            self.assertTrue(result.errors)
            self.assertFalse(token_output_path(source).exists())

    def test_preview_identity_checked_before_copy(self):
        from token_engine import source_identity
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            identity = source_identity(source)
            Image.new('RGBA', (32, 32), 'red').save(source)
            result = export_batch([source], read_image, self.frame, self.mask, {source: Adjust()},
                                  expected_inputs={str(source): identity})
            self.assertEqual((result.converted, result.created), (0, 0))
            self.assertTrue(result.errors)
            self.assertFalse(source.with_suffix('.webp').exists())

    def test_ownership_is_bound_to_fingerprint(self):
        from token_engine import save_generated
        import token_engine
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            dest = token_output_path(source)
            self.figure.save(dest)
            before = dest.read_bytes()
            import os
            original = token_engine.source_identity
            stat = dest.stat()
            identity_a = original(dest)
            calls = []
            replacement = Image.new('RGBA', (17, 19), 'red')
            def race(path):
                if path != dest:
                    return original(path)
                calls.append(path)
                if len(calls) == 1:
                    exif = Image.Exif()
                    exif[270] = token_engine.export_owner(source, 'token')
                    replacement.save(dest, exif=exif)
                    return identity_a
                # A non-snapshot implementation sees B, then receives A
                # again at the final fingerprint guard (ABA).
                dest.write_bytes(before)
                os.utime(dest, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                self.assertEqual(original(dest), identity_a)
                return identity_a
            with patch('token_engine.source_identity', side_effect=race):
                with self.assertRaises(OSError):
                    save_generated(self.figure, dest, source, 'token', skip_exists=False)
            self.assertEqual(calls, [dest])
            self.assertNotEqual(dest.read_bytes(), before)
            self.assertEqual(token_engine.read_export_owner(dest), (2, 'token', 'Hero.png'))
            self.assertEqual(read_image(dest).size, replacement.size)

    def test_corrupt_owned_png_is_not_counted_as_skip(self):
        from token_engine import save_generated, read_export_owner
        import struct
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            dest = token_output_path(source, 'png')
            save_generated(self.figure, dest, source, 'token', skip_exists=False)
            contents = dest.read_bytes()
            pos = 8
            while contents[pos+4:pos+8] != b'IDAT':
                pos += 12 + struct.unpack('>I', contents[pos:pos+4])[0]
            dest.write_bytes(contents[:pos+9])
            self.assertIsNotNone(read_export_owner(dest))
            result = export_batch([source], read_image, self.frame, self.mask, {source: Adjust()},
                                  format_name='png', convert_original=False)
            self.assertEqual((result.created, result.skipped), (0, 0))
            self.assertTrue(result.errors)
            result = export_batch([source], read_image, self.frame, self.mask, {source: Adjust()},
                                  format_name='png', convert_original=False, skip_exists=False)
            self.assertEqual(result.created, 1)

    def test_manual_approval_cannot_authorize_another_file_version(self):
        from token_engine import save_generated, source_identity
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            dest = token_output_path(source)
            Image.new('RGBA', (30, 30), 'blue').save(dest)
            identity = source_identity(dest)
            # The replacement is marked as owned, but it isn't the file
            # the user actually approved for overwrite.
            exif = Image.Exif()
            from token_engine import export_owner
            exif[270] = export_owner(source, 'token')
            Image.new('RGBA', (20, 20), 'red').save(dest, exif=exif)
            before = dest.read_bytes()
            with self.assertRaises(FileExistsError):
                save_generated(self.figure, dest, source, 'token',
                               skip_exists=False, approved_identity=identity)
            self.assertEqual(dest.read_bytes(), before)
            result = export_batch([source], read_image, self.frame, self.mask,
                                  {source: Adjust()}, skip_exists=False,
                                  approved_outputs={dest: identity})
            self.assertEqual((result.created, result.converted), (0, 0))
            self.assertTrue(result.errors)
            self.assertEqual(dest.read_bytes(), before)
            self.assertFalse(source.with_suffix('.webp').exists())
            dest.unlink()
            with self.assertRaises(FileExistsError):
                save_generated(self.figure, dest, source, 'token',
                               skip_exists=False, approved_identity=identity)
            self.assertFalse(dest.exists())

    def test_approval_rechecked_after_encoding_copy(self):
        import token_engine
        from token_engine import source_identity
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            dest = token_output_path(source)
            Image.new('RGBA', (20, 20), 'blue').save(dest)
            approved = source_identity(dest)
            replacement = Image.new('RGBA', (21, 21), 'red')
            original_write = token_engine.atomic_write
            def intervening_write(path, writer, **kwargs):
                def encode(fp):
                    writer(fp)
                    if path == source.with_suffix('.webp'):
                        replacement.save(dest)
                return original_write(path, encode, **kwargs)
            loader = Mock(side_effect=AssertionError('stale approval must stop rendering'))
            with patch('token_engine.atomic_write', side_effect=intervening_write):
                result = export_batch([source], loader, self.frame, self.mask, {source: Adjust()},
                                      skip_exists=False, approved_outputs={dest: approved})
            loader.assert_not_called()
            self.assertEqual((result.created, result.converted), (0, 0))
            self.assertEqual(len(result.errors), 1)
            self.assertIn('Подтверди заново', result.errors[0])
            self.assertFalse(source.with_suffix('.webp').exists())
            self.assertEqual(read_image(dest).size, replacement.size)

    def test_invalid_output_extension_rejected_before_encoding(self):
        from token_engine import save_generated
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            with self.assertRaises(ValueError):
                token_output_path(source, 'jpg')
            dest = source.with_name('Herot.jpg')
            with self.assertRaises(ValueError):
                save_generated(self.figure, dest, source, 'token', skip_exists=False)
            self.assertFalse(dest.exists())

    def test_export_inspection_fingerprints_and_checks_owner_in_one_read(self):
        from token_engine import save_generated, inspect_existing_export, source_identity
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            dest = token_output_path(source)
            save_generated(self.figure, dest, source, 'token', skip_exists=False)
            expected = source_identity(dest)
            # Windows temp paths may use 8.3 aliases (e.g. RUNNER~1).
            # Count opens of the underlying file, not of one spelling of its path.
            opened = []
            original = Path.open
            def track(path, *args, **kwargs):
                if path.samefile(dest):
                    opened.append(path)
                return original(path, *args, **kwargs)
            with patch.object(Path, 'open', track):
                identity, owned = inspect_existing_export(dest, source, 'token')
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].samefile(dest))
            self.assertTrue(owned)
            self.assertEqual(identity, expected)

    def test_manual_click_expected_absence_rejects_new_destination(self):
        from token_engine import EXPECTED_ABSENT, save_generated
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            dest = token_output_path(source)
            save_generated(self.figure, dest, source, 'token', skip_exists=False)
            before = dest.read_bytes()
            result = export_batch([source], read_image, self.frame, self.mask,
                                  {source: Adjust()}, skip_exists=False,
                                  approved_outputs={dest: EXPECTED_ABSENT})
            self.assertEqual((result.created, result.converted), (0, 0))
            self.assertTrue(result.errors)
            self.assertEqual(dest.read_bytes(), before)
            self.assertFalse(source.with_suffix('.webp').exists())

    def test_late_owned_export_collision_is_not_successful_skip(self):
        import token_engine
        from token_engine import save_generated, FolderIndex
        for late_in_publication in (False, True):
            with self.subTest(late_in_publication=late_in_publication), tempfile.TemporaryDirectory() as d:
                source = Path(d) / 'Hero.png'
                self.figure.save(source)
                dest = token_output_path(source)
                index = FolderIndex()
                list(index.candidates(source.parent, {source.stem}))
                def other_export():
                    exif = Image.Exif()
                    exif[270] = token_engine.export_owner(source, 'token')
                    self.figure.save(dest, exif=exif)
                    self.figure.convert('RGB').save(source.with_suffix('.jpg'))
                if late_in_publication:
                    def late(temporary, destination):
                        other_export()
                        return False
                    with patch.object(token_engine, '_publish_new', side_effect=late):
                        with self.assertRaises(FileExistsError):
                            save_generated(self.figure, dest, source, 'token', skip_exists=True, collision_index=index)
                else:
                    other_export()
                    with self.assertRaises(FileExistsError):
                        save_generated(self.figure, dest, source, 'token', skip_exists=True, collision_index=index)
                self.assertTrue(dest.is_file())
                source.with_suffix('.jpg').unlink()
                self.assertFalse(save_generated(self.figure, dest, source, 'token',
                                                skip_exists=True, collision_index=index))

    def test_posix_exchange_unsupported_preserves_destination(self):
        from token_engine import _replace_posix
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old, new, backup = root / 'old', root / 'new', root / 'backup'
            old.write_bytes(b'original')
            new.write_bytes(b'encoded')
            with patch('token_engine._posix_exchange', side_effect=OSError('swap unsupported')):
                with self.assertRaisesRegex(OSError, 'swap unsupported'):
                    _replace_posix(new, old, backup)
            self.assertEqual(old.read_bytes(), b'original')
            self.assertFalse(backup.exists())

    def test_late_corrupt_export_is_not_a_successful_skip(self):
        from token_engine import save_generated, export_owner, InvalidExportError
        import token_engine
        import struct
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            dest = token_output_path(source, 'png')
            def late_writer(temporary, destination):
                exif = Image.Exif()
                exif[270] = export_owner(source, 'token')
                self.figure.save(destination, exif=exif)
                data = destination.read_bytes()
                pos = 8
                while data[pos+4:pos+8] != b'IDAT':
                    pos += 12 + struct.unpack('>I', data[pos:pos+4])[0]
                destination.write_bytes(data[:pos+9])
                return False
            with patch.object(token_engine, '_publish_new', side_effect=late_writer):
                with self.assertRaisesRegex(InvalidExportError, 'Отключи пропуск'):
                    save_generated(self.figure, dest, source, 'token', skip_exists=True)
            self.assertTrue(dest.is_file())

    def test_copy_corruption_has_correct_recovery_step(self):
        from token_engine import InvalidExportError, validate_skip
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            dest = source.with_suffix('.webp')
            save_original_webp(source)
            with patch('token_engine.read_export_owner', side_effect=InvalidExportError('Готовое изображение повреждено.')):
                with self.assertRaisesRegex(InvalidExportError, 'Перемести или удали повреждённую WebP-копию'):
                    validate_skip(dest, source, 'copy')

    def test_zoom_matches_preview_at_very_small_scale(self):
        cutout = Image.new('RGBA', (4000, 400), 'red')
        # Transparent frame and opaque interior isolate the scaling geometry.
        frame = Image.new('RGBA', (512, 512))
        interior = Image.new('L', (512, 512), 255)
        a = compose(cutout, frame, interior, 256, Adjust(zoom=5)).getchannel('A').getbbox()
        b = compose(cutout, frame, interior, 512, Adjust(zoom=5)).getchannel('A').getbbox()
        self.assertGreater(b[2] - b[0], a[2] - a[0])
        self.assertAlmostEqual((b[2]-b[0]) / 2, a[2]-a[0], delta=1)
        for zoom in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                compose(cutout, frame, interior, 256, Adjust(zoom=zoom))

    def test_ring_exclusion_handles_alias_and_distinct_same_name(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            ring = folder / 'Ring.png'
            alias = folder / 'FrameAlias.png'
            unrelated_folder = folder / 'other'
            unrelated_folder.mkdir()
            self.figure.save(ring)
            self.figure.save(unrelated_folder / ring.name)
            # Hardlinks exercise same-file semantics without Windows admin.
            os.link(ring, alias)
            self.assertEqual(list_source_images(folder, exclude=alias), [])
            self.assertEqual(list_source_images(unrelated_folder, exclude=alias),
                             [unrelated_folder / ring.name])
            try:
                symbolic = folder / 'Symbolic.png'
                symbolic.symlink_to(ring)
            except OSError:
                return  # hardlink coverage already ran
            self.assertEqual(list_source_images(folder, exclude=symbolic), [])

    def test_cleanup_failure_keeps_publication_error(self):
        from token_engine import atomic_write
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / 'output.webp'
            def fail():
                raise OSError('recovery at important path')
            with patch.object(Path, 'unlink', side_effect=PermissionError('cleanup denied')):
                with self.assertRaisesRegex(OSError, 'important path') as caught:
                    atomic_write(dest, lambda fp: fp.write(b'x'), before_publish=fail)
            self.assertIn('cleanup denied', caught.exception.__notes__[0])

    def test_render_shape_and_alpha(self):
        token = compose(self.figure, self.frame, self.mask, 256)
        self.assertEqual(token.size, (256, 256))
        self.assertEqual(token.getpixel((0, 0))[3], 0)
        self.assertGreater(token.getpixel((128, 128))[3], 0)

    def test_copy_and_extraction_share_one_lazy_snapshot(self):
        import token_engine
        from token_engine import LoadedCutout
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            original_snapshot = token_engine.source_snapshot
            opened = []
            def count(path, identity):
                if path.samefile(source):
                    opened.append(path)
                return original_snapshot(path, identity)
            def extract(capture):
                first = capture.decode()
                second = capture.decode()
                self.assertEqual(first.tobytes(), second.tobytes())
                return LoadedCutout(first, capture.identity)
            with patch('token_engine.source_snapshot', side_effect=count):
                result = export_batch([source], lambda p: self.fail('legacy loader invoked'),
                                      self.frame, self.mask, {source: Adjust()},
                                      captured_loader=extract)
            self.assertEqual((result.created, result.converted), (1, 1))
            self.assertFalse(result.errors)
            self.assertEqual(len(opened), 1)

    def test_stale_source_and_recipe_warn_without_overwriting(self):
        from token_engine import read_export_owner
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            def run(zoom=100, skip=True):
                return export_batch([source], read_image, self.frame, self.mask,
                                    {source: Adjust(zoom=zoom)}, skip_exists=skip)
            self.assertEqual(run().created, 1)
            token, copy = token_output_path(source), source.with_suffix('.webp')
            marker = read_export_owner(token)
            self.assertEqual(marker[:3], (3, 'token', 'Hero.png'))
            self.assertEqual(len(marker[3]), 64)
            self.assertEqual(len(marker[4]), 64)
            self.assertFalse(run().warnings)
            original_token, original_copy = token.read_bytes(), copy.read_bytes()
            result = run(zoom=125)
            self.assertEqual(result.skipped, 1)
            self.assertTrue(any('Параметры генерации' in w for w in result.warnings))
            Image.new('RGBA', (320, 500), 'green').save(source)
            result = run()
            self.assertEqual(result.skipped, 1)
            self.assertTrue(any(token.name in w and 'Исходник изменился' in w for w in result.warnings))
            self.assertTrue(any(copy.name + ':' in w and 'Исходник изменился' in w for w in result.warnings))
            self.assertEqual(token.read_bytes(), original_token)
            self.assertEqual(copy.read_bytes(), original_copy)
            result = run(skip=False)
            self.assertEqual(result.created, 1)
            self.assertNotEqual(token.read_bytes(), original_token)
            self.assertEqual(copy.read_bytes(), original_copy)
            self.assertTrue(result.warnings)

    def test_both_exporters_record_actual_extraction_revision(self):
        from token_engine import LoadedCutout, read_export_owner, source_identity, token_recipe, extraction_provenance
        from item_engine import export_items, item_output_path, ItemAdjust, item_recipe
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            args = ('birefnet-general', False, 'Обычный')
            old = extraction_provenance(*args, revision=('before-download',))
            actual = extraction_provenance(*args, revision=('actual',))
            changed = extraction_provenance(*args, revision=('changed',))
            loader = Mock(return_value=LoadedCutout(self.figure, source_identity(source), actual))
            current = Mock(return_value=old)
            token_adjust, item_adjust = Adjust(), ItemAdjust()
            def token():
                return export_batch([source], loader, self.frame, self.mask,
                                    {source: token_adjust}, convert_original=False,
                                    extraction_recipe=current)
            def item():
                return export_items([source], loader, {source: item_adjust},
                                    convert_original=False, extraction_recipe=current)
            for run, destination, recipe in (
                (token, token_output_path(source), token_recipe(self.frame, token_adjust, 256, 'webp', actual)),
                (item, item_output_path(source), item_recipe(item_adjust, actual)),
            ):
                with self.subTest(destination=destination.name):
                    current.return_value = old
                    self.assertEqual(run().created, 1)
                    self.assertEqual(read_export_owner(destination)[4], recipe)
                    current.return_value = actual
                    self.assertFalse(run().warnings)
                    current.return_value = changed
                    loader.reset_mock()
                    report = run()
                    self.assertEqual(report.skipped, 1)
                    self.assertTrue(any('Параметры генерации' in w for w in report.warnings))
                    loader.assert_not_called()

    def test_late_skip_compares_winner_against_current_extraction_recipe(self):
        from token_engine import LoadedCutout, source_identity, save_generated, token_recipe, extraction_provenance
        from item_engine import export_items, item_output_path, ItemAdjust, item_recipe
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            identity = source_identity(source)
            old = extraction_provenance('birefnet-general', True, 'e', revision=('old',))
            current = extraction_provenance('birefnet-general', True, 'e', revision=('current',))
            revision = Mock(return_value=old)
            for kind, destination, winner_recipe, run in (
                ('token', token_output_path(source), token_recipe(self.frame, Adjust(), 256, 'webp', current),
                 lambda loader: export_batch([source], loader, self.frame, self.mask, {source: Adjust()},
                                             convert_original=False, extraction_recipe=revision)),
                ('item', item_output_path(source), item_recipe(ItemAdjust(), current),
                 lambda loader: export_items([source], loader, {source: ItemAdjust()},
                                              convert_original=False, extraction_recipe=revision)),
            ):
                with self.subTest(kind=kind):
                    revision.return_value = old
                    def loader(path):
                        revision.return_value = current
                        save_generated(self.figure, destination, source, kind, skip_exists=False,
                                       input_identity=identity, recipe=winner_recipe)
                        return LoadedCutout(self.figure, identity, old)
                    report = run(loader)
                    self.assertEqual(report.skipped, 1, report.errors)
                    self.assertFalse(report.warnings)

    def test_token_without_visible_subject_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'вне видимой области'):
            compose(self.figure, self.frame, self.mask, 256, Adjust(x=120))
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'Hero.png'
            self.figure.save(source)
            result = export_batch([source], read_image, self.frame, self.mask,
                                  {source: Adjust(x=120)}, convert_original=False)
            self.assertEqual(result.created, 0)
            self.assertTrue(result.errors)
            self.assertFalse(token_output_path(source).exists())

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
