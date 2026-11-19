"""Cross-format pixel contracts; no model, OS profile or external fixtures."""
import io
import struct
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageCms
from token_engine import read_image, save_original_webp
from image_normalization import decode_artwork


def gray_profile():
    fixed = lambda value: round(value * 65536)
    white = struct.pack('>4s4xiii', b'XYZ ', fixed(.9642), fixed(1), fixed(.8249))
    curve = struct.pack('>4s4xIH2x', b'curv', 1, 256)
    header = bytearray(128)
    header[8:12] = bytes((2, 16, 0, 0))
    header[12:24] = b'mntrGRAYXYZ '
    header[36:40] = b'acsp'
    header[68:80] = struct.pack('>iii', fixed(.9642), fixed(1), fixed(.8249))
    tags = struct.pack('>I4sII4sII', 2, b'wtpt', 156, len(white),
                       b'kTRC', 156 + len(white), len(curve))
    data = header + tags + white + curve
    data[:4] = struct.pack('>I', len(data))
    return bytes(data)


def packed_gray_tiff(bits, profile=None, *, sample_format=1):
    """Four little-endian grayscale TIFF samples, packed in TIFF bit order.

    SampleFormat can be omitted (unsigned by default) or set explicitly.
    For signed 8-bit samples these same bytes include negative values.
    """
    values = (0, 1 << (bits - 2), 1 << (bits - 1), (1 << bits) - 1)
    if bits == 8:
        pixels = bytes(values)
    elif bits == 12:
        pixels = bytes.fromhex('000400800fff')
    elif bits == 16:
        pixels = struct.pack('<4H', *values)
    else:
        raise ValueError(bits)
    tags = [(256, 4, 1, 4), (257, 4, 1, 1), (258, 3, 1, bits),
            (259, 3, 1, 1), (262, 3, 1, 1), (273, 4, 1, 0), (277, 3, 1, 1),
            (278, 4, 1, 1), (279, 4, 1, len(pixels))]
    if sample_format is not None:
        tags.append((339, 3, 1, sample_format))
    if profile is not None:
        tags.append((34675, 7, len(profile), 0))
    offset = 8 + 2 + 12 * len(tags) + 4
    tags[5] = (273, 4, 1, offset)
    if profile is not None:
        tags[-1] = (34675, 7, len(profile), offset + len(pixels))
    return (b'II' + struct.pack('<HIH', 42, 8, len(tags)) +
            b''.join(struct.pack('<HHII', *tag) for tag in tags) + bytes(4) + pixels + (profile or b''))


class NormalizationTests(unittest.TestCase):
    def test_gray_storage_depths_follow_same_tonal_contract(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for bits in (12, 16):
                for tagged in (False, True):
                    with self.subTest(bits=bits, tagged=tagged):
                        path = folder / f'Gray{bits}-{tagged}.tiff'
                        path.write_bytes(packed_gray_tiff(bits, gray_profile() if tagged else None))
                        image = read_image(path)
                        self.assertEqual(image.mode, 'RGBA')
                        reds = list(image.getchannel('R').getdata())
                        self.assertEqual(reds[0], 0)
                        self.assertEqual(reds[-1], 255)
                        self.assertLess(reds[1], reds[2])
                        if tagged:
                            self.assertTrue(130 <= reds[1] <= 145)
                            self.assertTrue(180 <= reds[2] <= 195)
                        else:
                            self.assertEqual(reds, [0, 64, 128, 255])
                        self.assertEqual(list(image.getchannel('A').getdata()), [255] * 4)
                        self.assertTrue(save_original_webp(path))
                        with Image.open(path.with_suffix('.webp')) as copy:
                            self.assertEqual(copy.size, (4, 1))
                            self.assertGreater(copy.convert('RGB').getpixel((3, 0))[0], 230)

    def test_tiff_sample_format_is_checked_even_when_pillow_exposes_mode_l(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for tagged in (False, True):
                profile = gray_profile() if tagged else None
                for sample_format in (None, 1, 2):
                    with self.subTest(tagged=tagged, sample_format=sample_format):
                        path = folder / f'gray8-{tagged}-{sample_format}.tiff'
                        path.write_bytes(packed_gray_tiff(8, profile, sample_format=sample_format))
                        with Image.open(path) as raw:
                            self.assertEqual(raw.mode, 'L')
                            if sample_format == 2:
                                with patch.object(raw, 'load', side_effect=AssertionError('unexpected pixel decode')):
                                    with self.assertRaisesRegex(ValueError, 'числовой диапазон TIFF'):
                                        decode_artwork(raw, force_alpha=True)
                        if sample_format == 2:
                            with self.assertRaisesRegex(ValueError, 'числовой диапазон TIFF'):
                                read_image(path)
                            with self.assertRaisesRegex(ValueError, 'числовой диапазон TIFF'):
                                save_original_webp(path)
                            self.assertFalse(path.with_suffix('.webp').exists())
                        else:
                            result = read_image(path)
                            self.assertEqual(result.mode, 'RGBA')
                            self.assertEqual(result.getchannel('A').getextrema(), (255, 255))
                            reds = list(result.getchannel('R').getdata())
                            self.assertEqual((reds[0], reds[-1]), (0, 255))
                            self.assertLess(reds[1], reds[2])
                            if not tagged:
                                self.assertEqual(reds, [0, 64, 128, 255])
                            self.assertTrue(save_original_webp(path))
                            self.assertTrue(path.with_suffix('.webp').is_file())

    def test_common_containers_and_alpha_layouts(self):
        palette = Image.new('P', (3, 2))
        palette.putpalette([255, 0, 0] + [0, 0, 0] * 255)
        cases = (
            ('PNG', Image.new('RGBA', (3, 2), (70, 110, 150, 64)), {}, 64),
            ('PNG', Image.new('LA', (3, 2), (100, 128)), {'icc_profile': gray_profile()}, 128),
            ('PNG', Image.new('1', (3, 2), 1), {'icc_profile': gray_profile()}, 255),
            ('PNG', palette, {'transparency': 0}, 0),
            ('JPEG', Image.new('RGB', (3, 2), (70, 110, 150)), {}, 255),
            ('JPEG', Image.new('CMYK', (3, 2), (0, 80, 160, 0)), {}, 255),
            ('BMP', Image.new('RGB', (3, 2), (70, 110, 150)), {}, 255),
            ('TIFF', Image.new('L', (3, 2), 100), {'icc_profile': gray_profile()}, 255),
            ('TIFF', Image.new('RGBA', (3, 2), (70, 110, 150, 64)), {}, 64),
            ('WEBP', Image.new('RGBA', (3, 2), (70, 110, 150, 64)), {'lossless': True}, 64),
        )
        for fmt, original, options, expected_alpha in cases:
            with self.subTest(fmt=fmt, mode=original.mode):
                stream = io.BytesIO()
                original.save(stream, fmt, **options)
                stream.seek(0)
                with Image.open(stream) as raw:
                    result = decode_artwork(raw, force_alpha=True)
                self.assertEqual(result.mode, 'RGBA')
                self.assertEqual(result.size, original.size)
                self.assertEqual(result.getpixel((0, 0))[3], expected_alpha)
                if options.get('icc_profile'):
                    profile = ImageCms.ImageCmsProfile(io.BytesIO(result.info['icc_profile']))
                    self.assertIn('sRGB', ImageCms.getProfileDescription(profile))

    def test_color_space_alpha_is_not_inferred_from_channel_name(self):
        lab = Image.new('LAB', (2, 1), (128, 128, 128))
        lab.info['icc_profile'] = ImageCms.ImageCmsProfile(ImageCms.createProfile('LAB')).tobytes()
        self.assertEqual(decode_artwork(lab).mode, 'RGB')
        self.assertEqual(decode_artwork(lab, force_alpha=True).getchannel('A').getextrema(), (255, 255))
        for mode, pixel in (('RGBa', (50, 25, 10, 128)), ('La', (50, 128))):
            with self.subTest(mode=mode):
                image = decode_artwork(Image.new(mode, (2, 1), pixel))
                self.assertEqual(image.mode, 'RGBA')
                self.assertEqual(image.getchannel('A').getextrema(), (128, 128))
                self.assertTrue(98 <= image.getpixel((0, 0))[0] <= 100)

    def test_out_of_range_numeric_modes_rejected_explicitly(self):
        for mode in ('I', 'F'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'нормализации диапазона'):
                decode_artwork(Image.new(mode, (1, 1)))

    def test_exif_orientation_is_applied_before_composition(self):
        source = Image.new('RGB', (3, 2), 'red')
        exif = Image.Exif()
        exif[274] = 6
        stream = io.BytesIO()
        source.save(stream, 'JPEG', exif=exif)
        stream.seek(0)
        with Image.open(stream) as raw:
            output = decode_artwork(raw, force_alpha=True)
        self.assertEqual(output.size, (2, 3))
        self.assertNotIn(274, output.getexif())


if __name__ == '__main__':
    unittest.main()
