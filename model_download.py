"""Verified, streamed downloads for the seven pinned rembg 2.0.87 models.

Only official pinned assets are downloaded. The worker writes a parent-owned
staging file next to the destination; cancellation cannot expose partial ONNX.
"""
import hashlib
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

from local_models import model_candidates, require_local_model, selected_model_file

RELEASE = 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/'
MODEL_ASSETS = {
    'birefnet-general': ('BiRefNet-general-epoch_244.onnx', 'md5:7a35a0141cbbc80de11d9c9a28f52697'),
    'birefnet-portrait': ('BiRefNet-portrait-epoch_150.onnx', 'md5:c3a64a6abf20250d090cd055f12a3b67'),
    'bria-rmbg': ('bria-rmbg-2.0.onnx', 'sha256:5b486f08200f513f460da46dd701db5fbb47d79b4be4b708a19444bcd4e79958'),
    'birefnet-general-lite': ('BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx', 'md5:4fab47adc4ff364be1713e97b7e66334'),
    'isnet-general-use': ('isnet-general-use.onnx', 'md5:fc16ebd8b0c10d971d3513d564d01e29'),
    'isnet-anime': ('isnet-anime.onnx', 'md5:6f184e756bb3bd901c8849220a83e38e'),
    'u2net': ('u2net.onnx', 'md5:60024c5c889badc19c04ad937298a77b'),
}
# Refuse an unbounded response even if Content-Length is absent.
MAX_DOWNLOAD_BYTES = 2 * 1024**3


def download_model(name, staging, emit, *, opener=urlopen):
    require_local_model(name)
    if selected_model_file(name) is not None:
        return
    destination = model_candidates(name)[0]
    staging = Path(staging)
    asset, expected = MODEL_ASSETS[name]
    algorithm, digest = expected.split(':', 1)
    checksum = hashlib.new(algorithm)
    started = time.monotonic()
    count, total, last = 0, 0, started
    emit('download', 0, 0, 0.0)
    with opener(Request(RELEASE + asset, headers={'User-Agent': 'TokenForge/1.8',
                                                'Accept-Encoding': 'identity'}), timeout=30) as response:
        try:
            total = int(response.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            total = 0
        if total < 0 or total > MAX_DOWNLOAD_BYTES:
            raise OSError('Некорректный размер модели.')
        emit('download', 0, total, 0.0)
        with staging.open('wb') as output:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_DOWNLOAD_BYTES or (total and count > total):
                    raise OSError('Размер скачанной модели превышает заявленный.')
                output.write(chunk)
                checksum.update(chunk)
                now = time.monotonic()
                if now - last >= .25:
                    emit('download', count, total, count / max(now - started, .001))
                    last = now
            output.flush()
            os.fsync(output.fileno())
    emit('verify', count, total or count, 0.0)
    if not count or (total and count != total) or checksum.hexdigest() != digest:
        raise OSError('Загрузка модели неполная или контрольная сумма не совпала.')
    # Never overwrite pre-existing weights, even if an independent tool
    # published them while we downloaded. Windows rename is create-only.
    if selected_model_file(name) is None:
        try:
            if os.name == 'nt':
                os.rename(staging, destination)
            else:
                os.link(staging, destination)
        except FileExistsError:
            pass
