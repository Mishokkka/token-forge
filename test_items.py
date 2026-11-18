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
                 if px[0] >= 245 and px[1] <= 10 and px[3] >= 250]
        self.assertTrue(black_x and red_x)
        self.assertLess(min(black_x), min(red_x))
        self.assertLessEqual(min(red_x) - min(black_x), 9)

    def test_position_zoom_rotate_adjust(self):
        a = compose_item(self.sword, ItemAdjust(layout=ITEM_MODES[0]))
        b = compose_item(self.sword, ItemAdjust(layout=ITEM_MODES[0], x=15, y=-20, zoom=130, rotation=24))
        self.assertNotEqual(a.tobytes(), b.tobytes())

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
