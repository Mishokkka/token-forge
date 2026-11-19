"""Real Tk integration test, isolated from a user's settings and model store.

Run with a Windows desktop or with a display (e.g. Xvfb on Linux).
No model/network download: synthetic RGBA input exercises both export modes.
"""
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from PIL import Image, ImageDraw
import tokenforge
from token_engine import read_export_owner, token_output_path
from item_engine import item_output_path


def run():
    with tempfile.TemporaryDirectory(prefix='TokenForge GUI ') as directory:
        base = Path(directory)
        arts = base / 'Arts'
        arts.mkdir()
        prefs = base / 'Prefs'
        prefs.mkdir()
        source = arts / 'Hero.png'
        img = Image.new('RGBA', (320, 500))
        draw = ImageDraw.Draw(img)
        draw.ellipse((100, 10, 220, 160), fill=(180, 140, 100, 255))
        draw.polygon([(110, 140), (210, 140), (310, 480), (10, 480)], fill=(50, 90, 130, 255))
        img.save(source)
        pref_file = prefs / 'settings.json'
        pref_file.write_text(json.dumps({'keep_alpha': True, 'convert_original': False,
                                         'ring': str(tokenforge.BASE / 'RingT.png')}),
                             encoding='utf-8')
        def unexpected_dialog(*args, **kwargs):
            raise AssertionError(f'Unexpected dialog: {args}')
        displayed = []
        apply_event = tokenforge.TokenForge._apply_worker_event

        def observe_event(app, action, data):
            current = action == 'rendered' and app._render_is_current(data[0])
            apply_event(app, action, data)
            if current:
                job, image, error = data
                if error is None and image is not None:
                    displayed.append(job)

        with patch.object(tokenforge.TokenForge, '_apply_worker_event', observe_event), \
             patch.object(tokenforge, 'PREFS_DIR', prefs), \
             patch.object(tokenforge, 'PREFS_FILE', pref_file), \
             patch.object(tokenforge, 'new_local_session', side_effect=AssertionError('Unexpected AI session')), \
             patch.object(tokenforge.messagebox, 'showerror', side_effect=unexpected_dialog), \
             patch.object(tokenforge.messagebox, 'askyesno', side_effect=unexpected_dialog):
            app = tokenforge.TokenForge(startup_file=source, startup_mode='token')
            errors = []
            app.root.report_callback_exception = lambda typ, exc, tb: errors.append(exc)
            def update_until(condition, detail, timeout=25):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    app.root.update()
                    if errors:
                        raise AssertionError('Tk callback failed') from errors[0]
                    if condition():
                        return
                    time.sleep(.01)
                raise AssertionError(f'{detail}: {app.status.get()}')

            def idle():
                return (not app.busy and not app.preview_pending and app.pending_render is None
                        and app.pending_export is None and app.pending_scan is None
                        and app.pending_frame is None)

            def find_button(container, text):
                for widget in container.winfo_children():
                    if isinstance(widget, tokenforge.ttk.Button) and widget.cget('text') == text:
                        return widget
                    found = find_button(widget, text)
                    if found is not None:
                        return found
                return None

            try:
                update_until(lambda: idle() and app.current_cutout is not None,
                             'initial asynchronous frame, scan and preview')
                assert app.selected.samefile(source)
                assert app.display_photo is not None
                assert displayed and app._render_is_current(displayed[-1]), app.status.get()
                initial_serial = displayed[-1].serial
                print('GUI startup, frame, list, preview: OK', flush=True)
                for zoom in (110, 125, 140):
                    app.zoom.set(zoom)
                    app._on_adjust('zoom')
                update_until(idle, 'coalesced slider rendering')
                assert displayed and app._render_is_current(displayed[-1]), app.status.get()
                assert displayed[-1].serial > initial_serial
                assert displayed[-1].adjustment.zoom == 140
                save = find_button(app.root, 'Сохранить выбранный')
                assert save is not None
                save.invoke()
                token = token_output_path(source)
                update_until(lambda: idle() and token.is_file(), 'manual token export')
                assert read_export_owner(token)[:3] == (3, 'token', source.name)
                with Image.open(token) as result:
                    assert result.size == (256, 256)
                    result.load()
                app.work_mode.set('item')
                app._on_work_mode_change()
                update_until(lambda: idle() and app.current_cutout is not None, 'switch mode')
                save.invoke()
                item = item_output_path(source)
                update_until(lambda: idle() and item.is_file(), 'manual item export')
                assert read_export_owner(item)[:3] == (3, 'item', source.name)
                with Image.open(item) as result:
                    assert result.size == (500, 500)
                    result.load()
                assert not source.with_suffix('.webp').exists()
                app._on_close()
                assert app.closing
                saved = json.loads(pref_file.read_text(encoding='utf-8'))
                assert saved['overrides'][str(source.resolve())]['zoom'] == 140
                print('GUI token/item export and preferences: OK', flush=True)
            finally:
                if not app.closing:
                    app.closing = True
                    app.cancel_event.set()
                    if app.worker is not None and app.worker.is_alive():
                        app.worker.join(30)
                    app.root.destroy()


if __name__ == '__main__':
    run()
