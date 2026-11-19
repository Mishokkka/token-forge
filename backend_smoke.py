"""Exercise installed rembg/PyMatting kernels without model downloads.

The fake session supplies a realistic soft foreground mask; all subsequent
color decontamination and alpha processing run through the actual backend.
This is an integration check, not an inference accuracy or RAM benchmark.
"""
from unittest.mock import patch
from PIL import Image, ImageDraw, ImageFilter
from rembg import bg
from resource_policy import EDGE_MODES
from token_engine import remove_background


class SyntheticSession:
    def __init__(self, mask):
        self.mask = mask
        self.calls = 0

    def predict(self, image, *args, **kwargs):
        assert image.mode == 'RGB' and image.size == self.mask.size
        self.calls += 1
        return [self.mask.copy()]


def run():
    size = (96, 96)
    mask = Image.new('L', size)
    ImageDraw.Draw(mask).ellipse((20, 16, 75, 80), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(2))
    foreground = Image.new('RGB', size, (180, 70, 40))
    background = Image.new('RGB', size, (30, 120, 190))
    artwork = Image.composite(foreground, background, mask).convert('RGBA')

    with patch.object(bg, 'new_session', side_effect=AssertionError('Unexpected model download')):
        for index, mode in enumerate(EDGE_MODES, 1):
            session = SyntheticSession(mask)
            with patch.object(bg, 'decontaminate_cutout', wraps=bg.decontaminate_cutout) as cleanup, \
                 patch.object(bg, 'alpha_matting_cutout', wraps=bg.alpha_matting_cutout) as matting:
                output = remove_background(artwork, session, mode)
            assert session.calls == 1
            assert output.mode == 'RGBA' and output.size == size
            alpha = output.getchannel('A')
            assert alpha.getpixel((48, 48)) >= 245
            assert alpha.getpixel((0, 0)) <= 10
            if mode == 'Alpha matting (медленно)':
                matting.assert_called_once()
                # rembg explicitly falls back to decontamination on a
                # rejected matting solve. Both routes are valid output.
            else:
                matting.assert_not_called()
                assert alpha.tobytes() == mask.tobytes()
                if mode == 'Очистка краёв':
                    cleanup.assert_called_once()
                else:
                    cleanup.assert_not_called()
            print(f'Installed backend mode {index}: OK', flush=True)


if __name__ == '__main__':
    run()
