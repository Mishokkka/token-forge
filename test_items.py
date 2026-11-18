"""Item generator regression tests, no neural model required."""
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw
from item_engine import (ITEM_MODES, ItemAdjust, compose_item,
                         export_items, item_output_path, _major_axis_angle)
from token_engine import list_source_images, read_image


class ItemTests(unittest.TestCase):
    def setUp(self):
        self.sword = Image.new('RGBA', (160, 600))
        draw = ImageDraw.Draw(self.sword)
        draw.polygon([(75, 20), (96, 70), (96, 460), (75, 490), (55, 460), (55, 70)],
                     fill=(180, 180, 198, 255))
        draw.rectangle((30, 490, 120, 512), fill=(105, 50, 23, 255))
        draw.rectangle((67, 515, 85, 570), fill=(80, 39, 25, 255))
        self.potion = Image.new('RGBA', (440, 550))
        d = ImageDraw.Draw(self.potion)
        d.ellipse((85, 160, 370, 490), fill=(230, 30, 35, 255))
        d.rectangle((190, 40, 275, 180), fill=(150, 70, 15, 255))

    def test_center_canvas_transparency_and_margin(self):
        rendered = compose_item(self.potion, ItemAdjust(layout=ITEM_MODES[0]))
        self.assertEqual(rendered.size, (500, 500))
        self.assertEqual(rendered.getpixel((0, 0))[3], 0)
        bbox = rendered.getchannel('A').getbbox()
        self.assertGreaterEqual(min(bbox[0], bbox[1], 500-bbox[2], 500-bbox[3]), 23)

    def test_small_center_canvas_meets_5_percent_margin(self):
        rendered = compose_item(Image.new('RGBA', (100, 100), 'red'), size=49)
        bbox = rendered.getchannel('A').getbbox()
        self.assertGreaterEqual(min(bbox[0], bbox[1], 49-bbox[2], 49-bbox[3]), 3)

    def test_sparse_alpha_cannot_export_invisible_item(self):
        from unittest.mock import Mock
        sparse = Image.new('RGBA', (2000, 2000))
        sparse.putpixel((0, 0), (255, 0, 0, 1))
        sparse.putpixel((1999, 1999), (255, 0, 0, 1))
        adjust = ItemAdjust(clean_noise=False)
        with self.assertRaisesRegex(ValueError, 'полностью прозрачен'):
            compose_item(sparse, adjust)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'Sparse.png'
            sparse.save(path)
            result = export_items([path], Mock(return_value=sparse), {path: adjust}, convert_original=False)
            self.assertEqual((result.created, result.skipped), (0, 0))
            self.assertTrue(result.errors)
            self.assertFalse(item_output_path(path).exists())

    def test_diagonal_axis(self):
        ascending = compose_item(self.sword, ItemAdjust(layout=ITEM_MODES[1]))
        descending = compose_item(self.sword, ItemAdjust(layout=ITEM_MODES[2]))
        angle_up = _major_axis_angle(ascending)
        angle_down = _major_axis_angle(descending)
        self.assertLess(abs(angle_up - 45), 6, angle_up)
        self.assertLess(abs(angle_down + 45), 6, angle_down)
        self.assertNotEqual(ascending.tobytes(), descending.tobytes())
        for obj in (ascending, descending):
            self.assertEqual(obj.getpixel((0,0))[3], 0)
            self.assertIsNotNone(obj.getchannel('A').getbbox())

    def test_outline_is_black_outside_silhouette(self):
        obj = Image.new('RGBA', (100, 100))
        ImageDraw.Draw(obj).rectangle((10, 10, 90, 90), fill='red')
        full = compose_item(obj, ItemAdjust(), 500, 3)
        row = 250
        pixels = [full.getpixel((x, row)) for x in range(500)]
        # A distinct opaque black edge appears before the opaque red body.
        black_x = [x for x, px in enumerate(pixels) if px == (0, 0, 0, 255)]
        red_x = [x for x, px in enumerate(pixels)
                 if px[0] > 0 and px[3] > 0]
        self.assertTrue(black_x and red_x)
        self.assertLess(min(black_x), min(red_x))
        self.assertEqual(min(red_x) - min(black_x), 3)

    def test_zoom_control_independently_shrinks_subject(self):
        a = compose_item(self.sword, ItemAdjust(zoom=100)).getchannel('A').getbbox()
        b = compose_item(self.sword, ItemAdjust(zoom=50)).getchannel('A').getbbox()
        self.assertLess(b[2] - b[0], a[2] - a[0])
        self.assertLess(b[3] - b[1], a[3] - a[1])

    def test_position_controls_move_independent_axes(self):
        baseline = compose_item(self.sword, ItemAdjust(zoom=50)).getchannel('A').getbbox()
        def center(box, axis):
            return (box[axis] + box[axis + 2]) / 2
        for axis, adjust in ((0, ItemAdjust(zoom=50, x=30)),
                             (1, ItemAdjust(zoom=50, y=-30))):
            moved = compose_item(self.sword, adjust).getchannel('A').getbbox()
            direction = 1 if axis == 0 else -1
            self.assertGreater((center(moved, axis) - center(baseline, axis)) * direction, 20)
            self.assertAlmostEqual(center(moved, 1-axis), center(baseline, 1-axis), delta=3)

    def test_rotation_control_changes_axis(self):
        a = compose_item(self.sword, ItemAdjust(rotation=0), stroke_px=0)
        b = compose_item(self.sword, ItemAdjust(rotation=24), stroke_px=0)
        delta = abs((_major_axis_angle(b) - _major_axis_angle(a) + 90) % 180 - 90)
        self.assertAlmostEqual(delta, 24, delta=5)

    def test_batch_and_no_reingestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            for name in ('Sword.png','Potion.png'):
                self.sword.save(folder/name)
            sources=list_source_images(folder)
            calls=[]
            def get_cutout(source):
                calls.append(source.name)
                return read_image(source)
            overrides={source:ItemAdjust() for source in sources}
            result=export_items(sources,get_cutout,overrides)
            self.assertEqual(result.created,2)
            self.assertEqual(result.converted,2)
            self.assertEqual(result.errors,[])
            for src in sources:
                output=item_output_path(src)
                self.assertTrue(output.exists())
                with Image.open(output) as image:
                    self.assertEqual(image.size,(500,500))
                    self.assertEqual(image.mode,'RGBA')
            self.assertEqual(list_source_images(folder),sources)
            again=export_items(sources,get_cutout,overrides)
            self.assertEqual(again.skipped,2)
            self.assertEqual(again.created,0)
            self.assertEqual(len(calls),2)

    def test_unknown_existing_item_is_not_overwritten(self):
        from token_engine import source_identity
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'Sword.png'
            self.sword.save(source)
            destination = item_output_path(source)
            self.potion.save(destination, 'WEBP')
            before = destination.read_bytes()
            args = ([source], read_image, {source: ItemAdjust()})
            result = export_items(*args, skip_exists=False, convert_original=False)
            self.assertEqual(result.created, 0)
            self.assertTrue(result.errors)
            self.assertEqual(destination.read_bytes(), before)
            result = export_items(*args, skip_exists=False, convert_original=False,
                                  approved_outputs={destination: source_identity(destination)})
            self.assertEqual(result.created, 1)
            result = export_items(*args, skip_exists=False, convert_original=False)
            self.assertEqual(result.created, 1)

    def test_outline_covers_mixed_opacity_regions(self):
        item = Image.new('RGBA', (100, 50), (200, 50, 30, 100))
        ImageDraw.Draw(item).rectangle((0, 0, 49, 49), fill=(200, 50, 30, 255))
        rendered = compose_item(item, ItemAdjust())
        box = rendered.getchannel('A').getbbox()
        y = 250
        self.assertEqual(rendered.getpixel((box[2]-1, y)), (0, 0, 0, 255))
        # Pillow premultiplied interpolation can round RGB by a few units.
        # Directly check exact alpha/color preservation at the outline boundary below.
        from item_engine import outline_subject
        original = Image.new('RGBA', (50, 50))
        ImageDraw.Draw(original).rectangle((10, 10, 20, 30), fill=(200, 50, 30, 255))
        ImageDraw.Draw(original).rectangle((21, 10, 35, 30), fill=(200, 50, 30, 100))
        outlined = outline_subject(original, 3)
        for py in range(50):
            for px in range(50):
                if original.getpixel((px, py))[3]:
                    self.assertEqual(outlined.getpixel((px, py)), original.getpixel((px, py)))
        self.assertEqual(outlined.getpixel((38, 20)), (0, 0, 0, 255))
        self.assertEqual(outlined.getpixel((39, 20))[3], 0)

    def test_translucent_item_diagonal_and_outline(self):
        item = Image.new('RGBA', (60, 300))
        ImageDraw.Draw(item).rectangle((25, 10, 34, 289), fill=(220, 50, 20, 50))
        for mode, angle in ((ITEM_MODES[1], 45), (ITEM_MODES[2], -45)):
            image = compose_item(item, ItemAdjust(layout=mode), stroke_px=0)
            self.assertLess(abs(_major_axis_angle(image) - angle), 7)
        outlined = compose_item(item, ItemAdjust())
        raw = compose_item(item, ItemAdjust(), stroke_px=0)
        # Use an unscaled interior sample well away from the outline.
        self.assertEqual(outlined.getpixel((250, 250)), raw.getpixel((250, 250)))
        self.assertGreater(outlined.getchannel('A').getextrema()[1], 128)

    def test_invalid_layout_and_blank_alpha(self):
        with self.assertRaises(ValueError):
            compose_item(self.sword,ItemAdjust(layout='not-valid'))
        with self.assertRaises(ValueError):
            compose_item(Image.new('RGBA',(200,200)),ItemAdjust())

# v1.7.1 safety checks: manual controls must never crop any item pixels.
class NoClipTests(unittest.TestCase):
    def setUp(self):
        self.item = Image.new('RGBA', (960, 220), (0, 0, 0, 0))
        dr = ImageDraw.Draw(self.item)
        dr.polygon([(12, 103), (108, 65), (895, 80), (949, 112),
                    (870, 140), (108, 156)], fill=(230, 145, 30, 255))
        dr.line([(32, 102), (940, 113)], fill=(255, 255, 255, 230), width=4)

    def test_extreme_zoom_caps_at_full_fitting_size(self):
        for mode in ITEM_MODES:
            with self.subTest(mode=mode):
                normal = compose_item(self.item, ItemAdjust(layout=mode, zoom=100))
                too_big = compose_item(self.item, ItemAdjust(layout=mode, zoom=180))
                self.assertEqual(normal.tobytes(), too_big.tobytes())

    def test_extreme_offset_does_not_lose_pixels_or_outline(self):
        for mode in ITEM_MODES:
            for rotation in (-180, -115, -50, 0, 75, 179):
                for zoom, x, y in ((20, -100, 100), (45, 100, -100),
                                    (100, -100, 100), (180, 100, -100)):
                    with self.subTest(mode=mode, rotation=rotation,
                                      zoom=zoom, x=x, y=y):
                        margin = round(500 * (.05 if mode == ITEM_MODES[0] else .018))
                        available = 500 - 2 * (margin + 3)
                        # Both axes are shifted by 160 px. The permitted span
                        # loses 320 px relative to the centered composition.
                        reference_zoom = min(zoom, 100) * (available - 320) / available
                        centered = compose_item(self.item, ItemAdjust(
                            layout=mode, zoom=reference_zoom, rotation=rotation))
                        shifted = compose_item(self.item, ItemAdjust(
                            layout=mode, zoom=zoom, x=x, y=y, rotation=rotation))
                        ref_alpha = centered.getchannel('A')
                        moved_alpha = shifted.getchannel('A')
                        ref_box, moved_box = ref_alpha.getbbox(), moved_alpha.getbbox()
                        self.assertIsNotNone(moved_box)
                        # Identical fitted geometry except for one-pixel rounding.
                        for axis in (0, 1):
                            self.assertLessEqual(abs((ref_box[axis+2] - ref_box[axis]) -
                                                     (moved_box[axis+2] - moved_box[axis])), 1)
                        for axis, direction in enumerate((x, y)):
                            delta = (moved_box[axis]+moved_box[axis+2]
                                     -ref_box[axis]-ref_box[axis+2]) / 2
                            self.assertGreater(delta * (1 if direction > 0 else -1), 100)
                        ref_area = sum(a > 0 for a in ref_alpha.tobytes())
                        moved_area = sum(a > 0 for a in moved_alpha.tobytes())
                        self.assertAlmostEqual(moved_area, ref_area, delta=max(10, ref_area * .08))
                        for rendered in (centered, shifted):
                            left, top, right, bottom = rendered.getchannel('A').getbbox()
                            self.assertGreaterEqual(left, margin - 1)
                            self.assertGreaterEqual(top, margin - 1)
                            self.assertLessEqual(right, 500 - margin + 1)
                            self.assertLessEqual(bottom, 500 - margin + 1)

    def test_translucent_whiskers_not_cropped_by_bbox(self):
        from item_engine import _subject_bbox
        img = Image.new('RGBA', (100, 100))
        img.putpixel((1, 40), (200, 100, 60, 1))
        img.putpixel((99, 60), (200, 100, 60, 2))
        self.assertEqual(_subject_bbox(img, clean_noise=False), (0, 38, 100, 63))
        rotated = compose_item(img, ItemAdjust(rotation=90, clean_noise=False))
        self.assertIsNotNone(rotated.getchannel('A').getbbox())


if __name__=='__main__':
    unittest.main()
