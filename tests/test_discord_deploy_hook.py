"""Publishing succeeds independently of the optional Discord notification."""
import unittest
from unittest.mock import Mock, patch

from test_main_server_deploy import deploy


class DiscordDeployHookTests(unittest.TestCase):
    def test_notification_runs_after_verified_publication(self):
        manifest = {'version': '1.7.10'}
        calls = []

        def publish(*_args, **_kwargs):
            calls.append('publish')
            return {'version': '1.7.10', 'current': '/verified/release'}

        def announce(received, *, root):
            self.assertIs(received, manifest)
            self.assertEqual(root, '/server')
            self.assertEqual(calls, ['publish'])
            calls.append('announce')
            return {'status': 'posted', 'message_id': '123456789012345678'}

        with patch.object(deploy, 'publish', side_effect=publish):
            result = deploy.publish_and_announce('archive', manifest, root='/server', announcer=announce)
        self.assertEqual(calls, ['publish', 'announce'])
        self.assertEqual(result['current'], '/verified/release')
        self.assertEqual(result['discord']['status'], 'posted')

    def test_failed_publication_never_announces(self):
        announce = Mock()
        with patch.object(deploy, 'publish', side_effect=ValueError('invalid archive')):
            with self.assertRaisesRegex(ValueError, 'invalid archive'):
                deploy.publish_and_announce('archive', {}, announcer=announce)
        announce.assert_not_called()

    def test_notification_error_preserves_release_and_does_not_print_credentials(self):
        with patch.object(deploy, 'publish', return_value={'version': '1.7.10', 'current': '/verified/release'}):
            result = deploy.publish_and_announce('archive', {}, announcer=Mock(side_effect=RuntimeError('SECRET TOKEN')))
        self.assertEqual(result['current'], '/verified/release')
        self.assertEqual(result['discord']['status'], 'error')
        self.assertNotIn('SECRET', str(result))

    def test_missing_configuration_is_reported_without_hiding_publication(self):
        with patch.object(deploy, 'publish', return_value={'version': '1.7.10'}):
            result = deploy.publish_and_announce('archive', {}, announcer=Mock(return_value={'status': 'unconfigured'}))
        self.assertEqual(result, {'version': '1.7.10', 'discord': {'status': 'unconfigured'}})


if __name__ == '__main__':
    unittest.main()
