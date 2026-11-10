"""Headless tray state/lifecycle tests: no native icon, window or model runs."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_icon import ICON
from tray_controls import TrayControls
import translator_app
from test_overlay_integration import Value, headless_app
from test_service_shutdown import closing_app


class Root:
    def __init__(self):
        self.window_state = "normal"
        self.timers = {}
        self.sequence = 0
        self.withdraw = Mock(side_effect=lambda: setattr(self, "window_state", "withdrawn"))
        self.deiconify = Mock(side_effect=lambda: setattr(self, "window_state", "normal"))
        self.lift = Mock()
        self.bind = Mock()

    def state(self, value=None):
        if value is not None:
            self.window_state = value
        return self.window_state

    def after(self, _milliseconds, callback):
        self.sequence += 1
        self.timers[self.sequence] = callback
        return self.sequence

    def after_idle(self, callback):
        return self.after(0, callback)

    def after_cancel(self, timer):
        self.timers.pop(timer, None)

    def fire(self, timer):
        self.timers.pop(timer)()


class FakeNative:
    def __init__(self, icon, receive):
        self.icon, self.receive = icon, receive
        self.start = Mock()
        self.close = Mock()
        self.is_alive = Mock(return_value=True)
        self.wait_closed = Mock(return_value=True)


class TrayStateTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.enabled = True
        self.instances = []
        def factory(icon, receive):
            native = FakeNative(icon, receive)
            self.instances.append(native)
            return native
        self.on_exit, self.on_error = Mock(), Mock()
        self.tray = TrayControls(self.root, enabled=lambda: self.enabled,
                                on_exit=self.on_exit, on_error=self.on_error, factory=factory)
        self.event = SimpleNamespace(widget=self.root)

    def minimize(self):
        self.root.window_state = "iconic"
        self.tray._unmapped(self.event)
        self.root.fire(self.tray._minimize_timer)
        return self.instances[-1]

    def deliver(self, native, kind, message=None):
        native.receive(kind, message)
        if self.tray._poll_timer is not None:
            self.root.fire(self.tray._poll_timer)
        else:
            self.tray._drain()

    def test_only_enabled_iconic_root_can_start_native_tray(self):
        self.enabled = False
        self.tray._unmapped(self.event)
        self.tray._unmapped(SimpleNamespace(widget=object()))
        self.assertIsNone(self.tray._minimize_timer)
        self.enabled = True
        self.tray._unmapped(self.event)
        self.root.fire(self.tray._minimize_timer)
        self.assertFalse(self.instances)
        self.assertFalse(self.root.withdraw.called)

    def test_never_hides_until_icon_is_ready_and_starts_only_once(self):
        native = self.minimize()
        self.assertEqual(native.icon, ICON)
        native.start.assert_called_once()
        self.root.withdraw.assert_not_called()
        self.tray._minimize()
        self.assertEqual(len(self.instances), 1)
        self.deliver(native, "ready")
        self.root.withdraw.assert_called_once()
        self.assertEqual(self.root.state(), "withdrawn")

    def test_ready_after_manual_restore_does_not_hide_or_keep_icon(self):
        native = self.minimize()
        self.root.window_state = "normal"
        self.deliver(native, "ready")
        self.root.withdraw.assert_not_called()
        native.close.assert_called_once()

    def test_old_callbacks_cannot_hide_restore_or_exit_new_session(self):
        old = self.minimize()
        self.tray.restore()
        current = self.minimize()
        for kind in ("ready", "restore", "exit", "error"):
            old.receive(kind, "old failure")
        self.tray._drain()
        self.on_exit.assert_not_called()
        self.on_error.assert_not_called()
        current.close.assert_not_called()
        self.root.withdraw.assert_not_called()

    def test_restore_preserves_maximized_state_and_closes_same_icon(self):
        self.root.window_state = "zoomed"
        self.tray._configured(self.event)
        native = self.minimize()
        self.deliver(native, "ready")
        self.deliver(native, "restore")
        self.assertEqual(self.root.state(), "zoomed")
        self.root.deiconify.assert_called_once()
        self.root.lift.assert_called_once()
        native.close.assert_called_once()
        self.assertFalse(self.tray._hidden)

    def test_icon_recovery_ready_does_not_remove_already_hidden_icon(self):
        native = self.minimize()
        self.deliver(native, "ready")
        self.deliver(native, "ready")
        native.close.assert_not_called()
        self.root.withdraw.assert_called_once()

    def test_stale_map_does_not_remove_the_hidden_launchers_only_icon(self):
        native = self.minimize()
        self.deliver(native, "ready")
        self.tray._mapped(self.event)
        native.close.assert_not_called()
        self.assertTrue(self.tray._hidden)

    def test_reentrant_map_during_restore_does_not_lose_maximized_state(self):
        self.tray._last_state = "zoomed"
        native = self.minimize()
        self.deliver(native, "ready")
        def deiconify():
            self.root.window_state = "normal"
            self.tray._mapped(self.event)
        self.root.deiconify.side_effect = deiconify
        self.tray.restore()
        self.assertEqual(self.root.state(), "zoomed")

    def test_disabling_preference_restores_hidden_window(self):
        native = self.minimize()
        self.deliver(native, "ready")
        self.enabled = False
        self.tray.preference_changed()
        native.close.assert_called_once()
        self.assertEqual(self.root.state(), "normal")

    def test_native_failure_restores_accessible_root(self):
        native = self.minimize()
        self.deliver(native, "ready")
        self.deliver(native, "error", "Explorer rejected icon")
        self.assertEqual(self.root.state(), "normal")
        self.assertIn("Explorer rejected icon", self.on_error.call_args.args[0])

    def test_exited_native_cleanup_failure_survives_later_minimize_cycles(self):
        old = self.minimize()
        self.tray.restore()
        old.is_alive.return_value = False
        old.wait_closed.return_value = False
        current = self.minimize()
        self.deliver(current, "ready")
        self.tray.restore()
        self.tray.close()
        self.assertIn(old, self.tray._retired)
        self.assertFalse(self.tray.wait_closed(0))
        current.wait_closed.assert_called()

    def test_synchronous_start_failure_does_not_hide_window(self):
        self.tray._factory = Mock(side_effect=OSError("icon missing"))
        self.root.window_state = "iconic"
        self.tray._minimize()
        self.root.withdraw.assert_not_called()
        self.assertEqual(self.root.state(), "normal")
        self.on_error.assert_called_once()

    def test_close_ignores_late_actions_and_waits_retired_thread(self):
        native = self.minimize()
        self.deliver(native, "ready")
        self.tray.close()
        self.tray.close()
        for kind in ("ready", "restore", "exit"):
            native.receive(kind)
        self.tray._drain()
        native.close.assert_called_once()
        self.root.deiconify.assert_not_called()
        self.on_exit.assert_not_called()
        self.assertTrue(self.tray.is_alive())
        self.assertTrue(self.tray.wait_closed())
        native.wait_closed.assert_called_once()
        self.assertFalse(self.root.timers)

    def test_exit_dispatches_once_without_following_restore(self):
        native = self.minimize()
        native.receive("exit")
        native.receive("restore")
        self.tray._drain()
        self.on_exit.assert_called_once()
        self.root.deiconify.assert_not_called()


class TrayAppIntegrationTests(unittest.TestCase):
    def test_settings_are_opt_in_strict_bool_and_round_trip(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(translator_app, "SETTINGS", Path(folder) / "settings.json"):
            for value in (False, None, "true", 1, [], {}):
                translator_app.SETTINGS.write_text(json.dumps({"minimize_to_tray": value}))
                self.assertFalse(translator_app.load_settings()["minimize_to_tray"])
            app = headless_app()
            app.minimize_to_tray = Value(True)
            app.tray = Mock()
            app._tray_setting_changed()
            app.tray.preference_changed.assert_called_once()
            self.assertTrue(translator_app.load_settings()["minimize_to_tray"])
            self.assertNotIn("transcript", json.loads(translator_app.SETTINGS.read_text()))

    def test_overlay_reply_uses_tray_restore_without_starting_new_session(self):
        app = headless_app()
        app.tray = Mock()
        record = object()
        app.select_reply(record)
        app.tray.restore.assert_called_once()
        app.reply.select.assert_called_once_with(record, whisper=False)
        self.assertTrue(app.reading)
        self.assertFalse(app.stop.is_set())

    def test_exit_stops_tray_before_backend_and_waits_before_destroy(self):
        app = closing_app(Mock())
        app.tray = Mock()
        app.tray.wait_closed.return_value = True
        app._shutdown_service.side_effect = lambda: app.tray.close.assert_called_once()
        app.close()
        app._shutdown_thread.join(2)
        app._finish_shutdown()
        app.tray.close.assert_called_once()
        app.tray.wait_closed.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_tray_cleanup_failure_does_not_skip_backend_shutdown(self):
        app = closing_app(Mock())
        app.tray = Mock()
        app.tray.close.side_effect = OSError("tray unavailable")
        app.tray.wait_closed.return_value = True
        with patch.object(translator_app.messagebox, "showwarning") as warning:
            app.close()
            app._shutdown_thread.join(2)
            app._finish_shutdown()
        app._shutdown_service.assert_called_once()
        self.assertIn("tray unavailable", warning.call_args.args[1])

    def test_unstopped_tray_cancels_update_after_backend_stop(self):
        app = closing_app(Mock())
        app.tray = Mock()
        app.tray.wait_closed.return_value = False
        prepared = app._pending_update = object()
        with patch.object(translator_app, "launch_update") as launch, \
                patch.object(translator_app, "discard_update") as discard, \
                patch.object(translator_app.messagebox, "showwarning"):
            app.close()
            app._shutdown_thread.join(2)
            app._finish_shutdown()
        app._shutdown_service.assert_called_once()
        launch.assert_not_called()
        discard.assert_called_once_with(prepared)

    def test_exited_tray_cleanup_failure_does_not_use_immediate_close_path(self):
        app = closing_app(None)
        app.tray = Mock()
        app.tray.is_alive.return_value = False
        app.tray.wait_closed.return_value = False
        with patch.object(translator_app.messagebox, "showwarning") as warning:
            app.close()
            app._shutdown_thread.join(2)
            app._finish_shutdown()
        self.assertIn("system tray cleanup", warning.call_args.args[1])
        app.root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
