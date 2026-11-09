"""Discord setup checks with fake API responses and temporary credentials."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

SERVER_TOOLS = Path(__file__).resolve().parents[1] / 'packaging/main-server'
sys.path.insert(0, str(SERVER_TOOLS))
try:
    spec = importlib.util.spec_from_file_location('l2_discord_setup', SERVER_TOOLS / 'configure_discord.py')
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
finally:
    sys.path.remove(str(SERVER_TOOLS))

APP_ID = '1569092047321829001'
CHANNEL_ID = '1569092047321829002'


class DiscordSetupTests(unittest.TestCase):
    def test_invite_is_guild_locked_and_requests_only_posting_permissions(self):
        url = urlparse(setup.invite_url(APP_ID))
        self.assertEqual((url.scheme, url.netloc, url.path), ('https', 'discord.com', '/oauth2/authorize'))
        query = parse_qs(url.query)
        self.assertEqual(query['scope'], ['bot'])
        self.assertEqual(query['permissions'], ['19456'])
        self.assertEqual(query['guild_id'], [setup.GUILD_ID])
        self.assertEqual(query['disable_guild_select'], ['true'])

    def test_setup_resolves_exact_channel_and_does_not_return_secret(self):
        client = Mock()
        client.get.side_effect = [
            {'id': APP_ID},
            [{'id': '1569092047321829003', 'name': 'general', 'type': 0},
             {'id': CHANNEL_ID, 'name': setup.CHANNEL_NAME, 'guild_id': setup.GUILD_ID, 'type': 5}],
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private' / 'config.json'
            result = setup.configure('synthetic-secret', config_path=path, client_factory=Mock(return_value=client))
            self.assertNotIn('synthetic-secret', str(result))
            self.assertEqual(result['channel_id'], CHANNEL_ID)
            self.assertEqual(json.loads(path.read_text())['bot_token'], 'synthetic-secret')
            self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertEqual([call.args[0] for call in client.get.call_args_list],
                         ['/oauth2/applications/@me', f'/guilds/{setup.GUILD_ID}/channels'])

    def test_missing_ambiguous_wrong_guild_or_voice_channel_preserves_existing_config(self):
        good = {'id': CHANNEL_ID, 'name': setup.CHANNEL_NAME, 'type': 0}
        for channels in ([], [good, good], [dict(good, guild_id='1569092047321829999')], [dict(good, type=2)]):
            with self.subTest(channels=channels), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'config.json'
                path.write_text('previous config', encoding='utf-8')
                client = Mock()
                client.get.side_effect = [{'id': APP_ID}, channels]
                with self.assertRaises(ValueError):
                    setup.configure('synthetic-secret', config_path=path, client_factory=Mock(return_value=client))
                self.assertEqual(path.read_text(), 'previous config')

    def test_ids_cannot_inject_query_parameters(self):
        for bad in ('123&permissions=8', '', None, '99999999999999999999'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                setup.invite_url(bad)

    def test_hidden_prompt_fails_closed_before_echoed_fallback(self):
        def unavailable_prompt(_prompt):
            warnings.warn('Cannot control echo on the terminal.', setup.getpass.GetPassWarning)
            self.fail('Echoed fallback must never be reached.')

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text('previous config', encoding='utf-8')
            with patch.object(setup, 'os', SimpleNamespace(name='posix', geteuid=lambda: 0)), \
                 patch.object(sys, 'argv', ['configure_discord.py']), \
                 patch.object(setup.getpass, 'getpass', side_effect=unavailable_prompt), \
                 patch.object(setup, 'configure') as configure, \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(setup.main(), 1)
                configure.assert_not_called()
                self.assertIn('interactive terminal', output.getvalue())
            self.assertEqual(path.read_text(), 'previous config')


if __name__ == '__main__':
    unittest.main()
