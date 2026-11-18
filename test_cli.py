"""Offline integration checks for v1.6. Run with: python -m unittest -v test_cli.py"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

import context_menu
import token_cli
import tokenforge


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='TokenForge test кириллица ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.art_folder = self.root / 'My Portraits' / 'Персонажи'
        self.art_folder.mkdir(parents=True)
        self.base = self.root / 'Program Files' / 'TokenForge'
        self.base.mkdir(parents=True)
        self.ring = Path(__file__).parent / 'RingT.png'
        self.pref = self.root / 'AppData Local' / 'TokenForge'
        self.pref.mkdir(parents=True)
        self.patchers = [
            patch.object(token_cli, 'PREFS_DIR', self.pref),
            patch.object(token_cli, 'PREFS_FILE', self.pref / 'settings.json'),
            patch.object(token_cli, 'BASE', self.base),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.photo = self.art_folder / 'Великий дракон 01.png'
        image = Image.new('RGBA', (320, 520), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((115, 40, 245, 180), fill=(50, 100, 140, 255))
        draw.rectangle((72, 170, 280, 510), fill=(80, 40, 120, 255))
        image.save(self.photo)
        self.settings = {
            'ring': str(self.ring), 'model': 'birefnet-portrait',
            'edge_mode': 'Очистка краёв', 'keep_alpha': True,
            'convert_original': True, 'skip_exists': True,
            'size': 256, 'ext': 'webp',
            'overrides': {str(self.photo.resolve()): {'zoom': 130, 'x': 12, 'y': -4, 'gap': 8}},
        }
        self.save_settings()

    def save_settings(self):
        (self.pref / 'settings.json').write_text(json.dumps(self.settings, ensure_ascii=False), encoding='utf-8')

    def test_context_auto_creates_art_and_token(self):
        with patch.object(token_cli, 'remove_background', side_effect=AssertionError('AI invoked')):
            state, token = token_cli.automatic_token(self.photo)
        self.assertEqual(state, 'created')
        self.assertEqual(token.name, 'Великий дракон 01t.webp')
        self.assertTrue(token.is_file())
        self.assertTrue(self.photo.with_suffix('.webp').exists())
        with Image.open(token) as image:
            self.assertEqual(image.size, (256, 256))
            self.assertEqual(image.mode, 'RGBA')
            self.assertEqual(image.getpixel((0,0))[3], 0)
        self.assertTrue(list((self.pref/'cutouts').glob('*.png')))
        state2, token2 = token_cli.automatic_token(self.photo)
        self.assertEqual(state2, 'skipped')
        self.assertEqual(token2, token)

    def test_existing_copy_and_token_are_resolved_to_original(self):
        token_cli.automatic_token(self.photo)
        webp = self.photo.with_suffix('.webp')
        token = self.photo.with_name(f'{self.photo.stem}t.webp')
        self.assertEqual(token_cli.resolve_context_source(webp), self.photo)
        self.assertEqual(token_cli.resolve_context_source(token), self.photo)
        self.assertEqual(token_cli.automatic_token(webp)[0], 'skipped')
        self.assertFalse((self.art_folder / (self.photo.stem + 'tt.webp')).exists())

    def test_item_cli_creates_500px_image_and_reuses_cache(self):
        with patch.object(token_cli, 'remove_background', side_effect=AssertionError('AI invoked')):
            status, output = token_cli.automatic_item(self.photo)
        self.assertEqual(status, 'created')
        self.assertEqual(output.name, self.photo.stem + '_item.webp')
        with Image.open(output) as artwork:
            self.assertEqual(artwork.size, (500,500))
            self.assertEqual(artwork.mode, 'RGBA')
            self.assertEqual(artwork.getpixel((0,0))[3], 0)
        with patch.object(token_cli, 'remove_background', side_effect=AssertionError('AI invoked')):
            self.assertEqual(token_cli.automatic_item(output)[0], 'skipped')
        self.assertEqual(token_cli.resolve_context_source(output), self.photo)
        self.assertFalse((self.art_folder / (self.photo.stem + '_item_item.webp')).exists())

    def test_gui_open_item_selects_user_target(self):
        instance = SimpleNamespace(run=lambda: None)
        with patch.object(tokenforge, 'TokenForge', return_value=instance) as gui:
            self.assertEqual(tokenforge.main(['--open-item',str(self.photo)]),0)
            self.assertEqual(gui.call_args.kwargs['startup_file'],self.photo)
            self.assertEqual(gui.call_args.kwargs['startup_mode'],'item')

    def test_auto_item_routes_without_starting_gui(self):
        with patch('token_cli.run_auto',return_value=0) as auto,patch.object(tokenforge,'TokenForge') as gui:
            self.assertEqual(tokenforge.main(['--auto-item',str(self.photo)]),0)
            auto.assert_called_once_with(str(self.photo),item=True)
            gui.assert_not_called()

    def test_auto_overwrites_if_skip_disabled(self):
        status, token = token_cli.automatic_token(self.photo)
        self.assertEqual(status, 'created')
        self.settings['skip_exists'] = False
        self.save_settings()
        with patch.object(token_cli, 'remove_background', side_effect=AssertionError('AI invoked')):
            state, dest = token_cli.automatic_token(self.photo)
        self.assertEqual(state, 'created')
        self.assertEqual(dest, token)

    def test_context_command_quotes_file_paths_and_mode(self):
        exe = self.base / '.venv' / 'Scripts' / 'pythonw.exe'
        script = self.base / 'tokenforge.py'
        cmd = context_menu.command_for(exe, script, '--open')
        self.assertEqual(cmd, f'"{exe}" "{script}" --open "%1"')
        self.assertNotIn('cmd.exe', cmd)
        with self.assertRaises(ValueError):
            context_menu.command_for(exe, script, '--erase')

    def test_gui_open_selects_user_target(self):
        instance = SimpleNamespace(run=lambda: None)
        with patch.object(tokenforge, 'TokenForge', return_value=instance) as mock:
            self.assertEqual(tokenforge.main(['--open', str(self.photo)]), 0)
            self.assertEqual(mock.call_args.kwargs['startup_file'], self.photo)

    def test_auto_routes_without_starting_gui(self):
        with patch('token_cli.run_auto', return_value=0) as auto, patch.object(tokenforge, 'TokenForge') as gui:
            self.assertEqual(tokenforge.main(['--auto', str(self.photo)]), 0)
            auto.assert_called_once_with(str(self.photo), item=False)
            gui.assert_not_called()

    def test_invalid_file_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            token_cli.automatic_token(self.art_folder / 'нет.png')
        text = self.art_folder / 'bad.txt'
        text.write_text('hello')
        with self.assertRaises(ValueError):
            token_cli.automatic_token(text)


class RegistryTests(unittest.TestCase):
    def test_install_and_remove_per_user_extensions(self):
        class Registry:
            HKEY_CURRENT_USER = 1234
            KEY_WRITE = 1
            REG_SZ = 1
            def __init__(self):
                self.created = {}
                self.deleted = []
            def CreateKeyEx(self, hive, path, reserved, flags):
                assert hive == self.HKEY_CURRENT_USER
                self.created.setdefault(path, {})
                class Context:
                    def __enter__(inner): return path
                    def __exit__(inner, *args): return False
                return Context()
            def SetValueEx(self, key, name, reserved, kind, value):
                self.created[key][name] = value
            def DeleteKey(self, hive, path):
                self.deleted.append(path)

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            exe = base / '.venv' / 'Scripts' / 'pythonw.exe'
            exe.parent.mkdir(parents=True)
            exe.touch()
            (base / 'tokenforge.py').touch()
            reg = Registry()
            with patch.object(context_menu, '_registry', return_value=reg):
                context_menu.install(base)
                context_menu.remove()
            self.assertEqual(len(reg.created), len(context_menu.EXTENSIONS) * len(context_menu.ACTION_NAMES) * 2)
            self.assertEqual(len(reg.deleted), len(reg.created))
            allkeys = list(reg.created.keys())
            self.assertTrue(all(k.startswith(context_menu.ROOT) for k in allkeys))
            for ext in context_menu.EXTENSIONS:
                self.assertTrue(any(k.startswith(context_menu.ROOT + '\\' + ext + '\\shell\\') for k in allkeys))
            cmd = [v.get('') for k, v in reg.created.items() if k.endswith('\\command')]
            self.assertEqual(len(cmd), 28)
            self.assertTrue(all('"%1"' in s for s in cmd))
            self.assertTrue(all('--auto' in s or '--open' in s for s in cmd))


if __name__ == '__main__':
    unittest.main()
