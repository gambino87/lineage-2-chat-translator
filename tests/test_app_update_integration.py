"""Upgrade lifecycle checks with fake downloads/processes/windows only."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import installer_main as setup
import translator_app as app_module
from tests.test_service_shutdown import closing_app


class UpdateShutdownTests(unittest.TestCase):
    def test_verified_handoff_waits_for_service_shutdown_and_keeps_window_until_ready(self):
        app = closing_app(Mock())
        app.updates = Mock()
        prepared = object()
        app._pending_update = prepared
        entered, release = threading.Event(), threading.Event()

        def handoff(job):
            self.assertIs(job, prepared)
            app._shutdown_service.assert_called_once()
            app.updates.close.assert_called_once()
            app.root.destroy.assert_not_called()
            entered.set()
            release.wait(2)

        with patch.object(app_module, 'launch_update', side_effect=handoff), \
             patch.object(app_module, 'discard_update') as discard:
            app.close()
            try:
                self.assertTrue(entered.wait(1))
                app._finish_shutdown()
                app.root.destroy.assert_not_called()
                self.assertFalse(app._shutdown_thread.daemon)
            finally:
                release.set()
                app._shutdown_thread.join(2)
            app._finish_shutdown()
        self.assertTrue(app._update_handed_off)
        app.root.destroy.assert_called_once()
        discard.assert_not_called()

    def test_service_failure_discards_download_and_never_starts_installer(self):
        app = closing_app(Mock(side_effect=RuntimeError('service still active')))
        prepared = app._pending_update = object()
        with patch.object(app_module, 'launch_update') as launch, \
             patch.object(app_module, 'discard_update') as discard, \
             patch.object(app_module.messagebox, 'showwarning') as warning:
            app.close()
            app._shutdown_thread.join(2)
            app._finish_shutdown()
        launch.assert_not_called()
        discard.assert_called_once_with(prepared)
        self.assertIn('not installed', warning.call_args.args[1])
        self.assertIn('service still active', warning.call_args.args[1])

    def test_preferences_preflight_failure_leaves_app_open_and_lets_controls_retry(self):
        app = closing_app(Mock())
        app.close = Mock()
        app.save_settings.return_value = False
        with patch.object(app_module, 'discard_update') as discard:
            with self.assertRaisesRegex(ValueError, 'preferences could not be saved'):
                app._update_ready(object())
        app.close.assert_not_called()
        app.root.destroy.assert_not_called()
        discard.assert_not_called()  # Rejected callback leaves cleanup to controls.

    def test_second_preferences_save_failure_cancels_upgrade_after_service_stop(self):
        app = closing_app(Mock())
        app.save_settings.side_effect = [True, False]
        prepared = object()
        with patch.object(app_module, 'launch_update') as launch, \
             patch.object(app_module, 'discard_update') as discard, \
             patch.object(app_module.messagebox, 'showwarning'):
            app._update_ready(prepared)
            app._shutdown_thread.join(2)
            app._finish_shutdown()
        app._shutdown_service.assert_called_once()
        launch.assert_not_called()
        discard.assert_called_once_with(prepared)

    def test_handoff_error_is_reported_and_owned_download_discarded(self):
        app = closing_app(Mock())
        prepared = app._pending_update = object()
        with patch.object(app_module, 'launch_update', side_effect=RuntimeError('helper did not become ready')), \
             patch.object(app_module, 'discard_update') as discard, \
             patch.object(app_module.messagebox, 'showwarning') as warning:
            app.close()
            app._shutdown_thread.join(2)
            app._finish_shutdown()
        discard.assert_called_once_with(prepared)
        self.assertIn('helper did not become ready', warning.call_args.args[1])
        self.assertFalse(getattr(app, '_update_handed_off', False))

    def test_update_without_managed_service_still_waits_for_handoff(self):
        app = closing_app(None)
        prepared = app._pending_update = object()
        with patch.object(app_module, 'launch_update') as launch:
            app.close()
            app._shutdown_thread.join(2)
            app._finish_shutdown()
        launch.assert_called_once_with(prepared)
        app.root.destroy.assert_called_once()

    def test_late_verified_job_does_not_interrupt_an_existing_close(self):
        app = closing_app(None)
        app._closing = True
        app.close = Mock()
        prepared = object()
        with patch.object(app_module, 'discard_update') as discard:
            app._update_ready(prepared)
        discard.assert_called_once_with(prepared)
        app.close.assert_not_called()

    def test_settings_expansion_checks_but_hiding_does_not(self):
        app = app_module.TranslatorApp.__new__(app_module.TranslatorApp)
        app.settings_panel = Mock()
        app.settings_button = Mock()
        app.updates = Mock()
        app.settings_panel.winfo_ismapped.return_value = False
        app.toggle_settings()
        app.updates.settings_shown.assert_called_once()
        app.settings_panel.winfo_ismapped.return_value = True
        app.toggle_settings()
        app.updates.settings_shown.assert_called_once()


class UpdateEntrypointTests(unittest.TestCase):
    def context(self, update=False, uninstall=False, result=None):
        stack = self.enterContext(ExitStack())
        self.mutex = stack.enter_context(patch.object(setup, 'SingleInstance'))
        self.mutex.return_value.acquire.return_value = True
        stack.enter_context(patch.object(setup, 'update_in_progress', return_value=update))
        stack.enter_context(patch.object(setup, 'uninstall_in_progress', return_value=uninstall))
        self.consume = stack.enter_context(patch.object(setup, 'consume_update_result', return_value=result))
        self.cleanup = stack.enter_context(patch.object(setup, 'cleanup_update_cache'))
        self.root = stack.enter_context(patch.object(setup.tk, 'Tk'))
        stack.enter_context(patch.object(setup, 'apply_window_icon'))
        self.window = stack.enter_context(patch.object(setup, 'SetupWindow'))
        self.window.return_value.launch_requested = False
        self.info = stack.enter_context(patch.object(setup.messagebox, 'showinfo'))
        self.warning = stack.enter_context(patch.object(setup.messagebox, 'showwarning'))
        self.powershell = Path('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
        stack.enter_context(patch.object(setup, '_system_powershell', return_value=self.powershell))
        self.notice_process = stack.enter_context(patch.object(setup.subprocess, 'Popen'))

    def assert_detached_notice(self, text):
        self.notice_process.assert_called_once()
        arguments = self.notice_process.call_args.args[0]
        self.assertEqual(arguments[:-1], [str(self.powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command'])
        script = arguments[-1]
        self.assertIn(text, script)
        self.assertIn("WScript.Shell", script)
        self.assertIn(", 30, 'Lineage 2 Chat Translator', 64)", script)
        options = self.notice_process.call_args.kwargs
        self.assertEqual(options['cwd'], self.powershell.parent)
        self.assertEqual(options['creationflags'], getattr(setup.subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertTrue(options['close_fds'])
        for handle in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(options[handle], setup.subprocess.DEVNULL)
        self.notice_process.return_value.wait.assert_not_called()
        self.notice_process.return_value.communicate.assert_not_called()
        self.root.assert_not_called()
        self.info.assert_not_called()
        self.window.assert_not_called()
        self.consume.assert_not_called()
        self.cleanup.assert_not_called()
        self.mutex.return_value.close.assert_called_once()

    def test_update_guard_prevents_launching_setup_or_services(self):
        self.context(update=True)
        self.notice_process.side_effect = lambda *args, **kwargs: self.mutex.return_value.close.assert_called_once()
        self.assertEqual(setup.main([]), 2)
        self.assert_detached_notice('reopen automatically')

    def test_rejected_second_launch_releases_mutex_and_never_waits_for_its_notice(self):
        self.context()
        self.mutex.return_value.acquire.return_value = False
        self.notice_process.side_effect = lambda *args, **kwargs: self.mutex.return_value.close.assert_called_once()
        self.assertEqual(setup.main([]), 0)
        self.assert_detached_notice('already open or closing')

    def test_uninstall_guard_exits_without_keeping_installed_files_open(self):
        self.context(uninstall=True)
        self.notice_process.side_effect = lambda *args, **kwargs: self.mutex.return_value.close.assert_called_once()
        self.assertEqual(setup.main([]), 2)
        self.assert_detached_notice('Uninstall is in progress')

    def test_notice_failure_never_creates_a_blocking_tk_fallback(self):
        self.context(update=True)
        self.notice_process.side_effect = OSError('system popup unavailable')
        self.assertEqual(setup.main([]), 2)
        self.root.assert_not_called()
        self.info.assert_not_called()
        self.window.assert_not_called()
        self.mutex.return_value.close.assert_called_once()

    def test_notice_rejects_nonconstant_text_before_launching_anything(self):
        self.context()
        setup.detached_launch_notice("'; untrusted command")
        self.notice_process.assert_not_called()
        self.root.assert_not_called()

    def test_previous_failure_reported_once_before_regular_startup(self):
        self.context(result={'status': 'error', 'message': 'Update canceled; retry from Settings.', 'version': '1.7.1'})
        self.assertEqual(setup.main([]), 0)
        self.consume.assert_called_once_with(setup.SETTINGS_PATH.parent)
        self.warning.assert_called_once()
        self.window.assert_called_once()
        self.cleanup.assert_called_once_with(setup.SETTINGS_PATH.parent, strict=False)

    def test_update_guard_stops_uninstall_before_data_cleanup(self):
        self.context(update=True)
        with tempfile.TemporaryDirectory() as folder, patch.object(setup, 'uninstall_data') as uninstall:
            result = Path(folder) / 'result.txt'
            self.assertEqual(setup.main(['--uninstall-cleanup', '--cleanup-result', str(result)]), 1)
            self.assertIn('update is in progress', result.read_text(encoding='utf-8-sig'))
        uninstall.assert_not_called()


if __name__ == '__main__':
    unittest.main()
