"""Pure preview jobs: immutable inputs, one worker-owned preparation cache."""
from dataclasses import dataclass
from weakref import ref
from PIL import Image
from token_engine import compose, silhouette_geometry
from item_engine import compose_item, _orient


@dataclass(frozen=True)
class PreviewJob:
    selection: int
    serial: int
    cutout: Image.Image
    mode: str
    raw: bool
    frame: Image.Image | None
    interior: Image.Image | None
    adjustment: object
    size: int = 512


class PreviewRenderer:
    def __init__(self):
        self._source = None
        self._geometry = None
        self._oriented = None
        self._orientation_key = None
        self._raw = None

    def render(self, job, raw_renderer):
        # A weak reference plus object identity avoids id reuse and does not
        # pin an extra original-sized image in the preparation cache.
        if self._source is None or self._source() is not job.cutout:
            self._source = ref(job.cutout)
            self._geometry = None
            self._oriented = None
            self._orientation_key = None
            self._raw = None
        if job.raw:
            if self._raw is None:
                self._raw = raw_renderer(job.cutout, 512)
            return self._raw
        if job.mode == 'item':
            adjust = job.adjustment
            key = (adjust.layout, adjust.rotation, adjust.flip_horizontal,
                   adjust.flip_vertical, adjust.clean_noise)
            if self._oriented is None or key != self._orientation_key:
                oriented = _orient(job.cutout, adjust)
                self._oriented, self._orientation_key = oriented, key
            return compose_item(job.cutout, adjust, prepared=self._oriented)
        if self._geometry is None:
            self._geometry = silhouette_geometry(job.cutout)
        return compose(job.cutout, job.frame, job.interior, job.size,
                       job.adjustment, geometry=self._geometry)
