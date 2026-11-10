"""Headless checks for tray ownership and top-right resizing; no windows opened."""
import unittest
import ctypes
import os
from contextlib import ExitStack
from unittest.mock import Mock, patch

import chat_overlay
import overlay_tray
from chat_overlay import CHANNEL_FILTERS, resize_from_top_right
from test_overlay_channel_filters import overlay_with, record
from test_overlay_tray import Value


class TopRightResizeTests(unittest.TestCase):
    def test_outward_drag_keeps_bottom_left_anchored(self):
        self.assertEqual(resize_from_top_right((100, 400, 540, 230), 60, -80, (0, 0, 1920, 1080)),
                         (100, 320, 600, 310))

    def test_shrinking_clamps_without_moving_the_anchor(self):
        self.assertEqual(resize_from_top_right((100, 400, 540, 230), -400, 500, (0, 0, 1920, 1080)),
                         (100, 510, 300, 120))

    def test_top_and_right_screen_edges_do_not_push_the_bottom_left(self):
        self.assertEqual(resize_from_top_right((100, 400, 540, 230), 5000, -5000, (0, 0, 1920, 1080)),
                         (100, 0, 1820, 630))

    def test_negative_monitor_coordinates_and_app_size_limits(self):
        self.assertEqual(resize_from_top_right((-1600, -400, 540, 230), 100, -150, (-1920, -1080, 3840, 2160)),
                         (-1600, -550, 640, 380))
        self.assertEqual(resize_from_top_right((100, 3000, 540, 230), 10000, -10000, (0, 0, 5000, 4000)),
                         (100, 1230, 3000, 2000))

    def test_tiny_desktop_and_removed_monitor_still_fit(self):
        for rect in ((0, 0, 200, 100), (-1920, -500, 540, 230)):
            x, y, width, height = resize_from_top_right(rect, 1000, -1000, (0, 0, 240, 100))
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertGreater(width, 0)
            self.assertGreater(height, 0)
            self.assertLessEqual(x + width, 240)
            self.assertLessEqual(y + height, 100)

    def test_resize_handler_uses_pointer_delta_and_obeys_lock(self):
        overlay = overlay_with()
        overlay._rect = Mock(return_value=(100, 400, 540, 230))
        overlay._bounds = Mock(return_value=(0, 0, 1920, 1080))
        overlay._place = Mock()
        overlay._start_resize(Mock(x_root=640, y_root=400))
        overlay._resize(Mock(x_root=700, y_root=320))
        overlay._place.assert_called_once_with(100, 320, 600, 310)
        overlay.settings['locked'] = True
        overlay._resize(Mock(x_root=800, y_root=200))
        self.assertEqual(overlay._place.call_count, 1)


class TrayIntegrationTests(unittest.TestCase):
    def make_overlay(self):
        overlay = overlay_with(['say', 'party'])
        overlay._tray = None
        overlay.channels_button = Mock()
        overlay._geometry_timer = None
        overlay._drag_origin = overlay._resize_origin = None
        return overlay

    def test_tray_is_created_once_and_edits_same_saved_filter_preferences(self):
        overlay = self.make_overlay()
        overlay.add_message(record(1, 0), 'Say message')
        overlay.add_message(record(2, 8), 'Trade message')
        with patch.object(chat_overlay, 'ChannelTray') as factory:
            tray = factory.return_value
            tray.is_open = False
            tray.open.side_effect = lambda: setattr(tray, 'is_open', True)
            overlay._toggle_channel_tray()
            overlay._toggle_channel_tray()
            factory.assert_called_once()
            self.assertIs(factory.call_args.args[0], overlay.window)
            self.assertEqual(factory.call_args.kwargs['options'], [(key, label) for key, label, _ in CHANNEL_FILTERS])
            tray.open.assert_called_once()
            tray.close.assert_called_once()
            factory.call_args.kwargs['on_change'](['trade'])
            self.assertEqual(overlay.settings['channels'], ['trade'])
            self.assertIn('Trade message', overlay.text.text)
            self.assertNotIn('Say message', overlay.text.text)
            factory.return_value.sync.assert_called_with(['trade'], .85)
            overlay.on_change.assert_called_once()
            factory.call_args.kwargs['on_open_change'](True)
            overlay.channels_button.configure.assert_called_with(text='Channels ▾')

    def test_locked_or_destroyed_overlay_cannot_open_tray(self):
        overlay = self.make_overlay()
        with patch.object(chat_overlay, 'ChannelTray') as factory:
            overlay.settings['locked'] = True
            overlay._toggle_channel_tray()
            overlay.settings['locked'] = False
            overlay._destroyed = True
            overlay._toggle_channel_tray()
            factory.assert_not_called()

    def test_hiding_and_locking_close_tray_and_shutdown_destroys_it(self):
        overlay = self.make_overlay()
        overlay._tray = Mock()
        overlay._remember_geometry = Mock()
        overlay.hide()
        overlay._tray.hide.assert_called_once()
        overlay.window.withdraw.assert_called_once()
        overlay._native = Mock()
        overlay._change('locked', True)
        self.assertEqual(overlay._tray.hide.call_count, 2)
        overlay.destroy()
        overlay._tray.destroy.assert_called_once()
        overlay.window.destroy.assert_called_once()

    def test_placement_repositions_tray_without_changing_saved_dimensions(self):
        overlay = self.make_overlay()
        overlay._tray = Mock()
        overlay._native = Mock()
        before = dict(overlay.settings)
        overlay._place(-800, 200, 700, 300)
        overlay._native.place.assert_called_once_with(-800, 200, 700, 300)
        overlay._tray.reposition.assert_called_once()
        self.assertEqual(overlay.settings, before)
        overlay._configured(Mock(widget=overlay.window))
        self.assertEqual(overlay._tray.reposition.call_count, 2)

    def test_tray_opacity_and_selections_follow_settings(self):
        overlay = self.make_overlay()
        overlay._tray = Mock()
        overlay._change('opacity', .65)
        overlay._tray.sync.assert_called_with(['say', 'party'], .65)
        overlay._change('channels', [])
        overlay._tray.sync.assert_called_with([], .65)

    def test_tray_uses_nearest_monitor_work_area_and_has_disconnection_fallback(self):
        overlay = self.make_overlay()
        overlay._native = Mock()
        overlay._native.work_area.return_value = (-1920, 120, 1920, 1000)
        self.assertEqual(overlay._tray_bounds(), (-1920, 120, 1920, 1000))
        overlay._native.bounds.assert_not_called()
        overlay._native.work_area.side_effect = OSError('monitor removed')
        overlay._native.bounds.return_value = (0, 0, 1920, 1080)
        self.assertEqual(overlay._tray_bounds(), (0, 0, 1920, 1080))

    def test_native_monitor_query_uses_only_owned_overlay_handle(self):
        api = Mock()
        api.GetAncestor.return_value = 123
        api.MonitorFromWindow.return_value = 456
        api.GetWindowThreadProcessId.side_effect = lambda _handle, owner: setattr(owner._obj, 'value', os.getpid())

        def fill_monitor(_monitor, info):
            self.assertEqual(info._obj.cbSize, ctypes.sizeof(info._obj))
            info._obj.rcWork.left, info._obj.rcWork.top = -1920, 120
            info._obj.rcWork.right, info._obj.rcWork.bottom = 0, 1120
            return True

        api.GetMonitorInfoW.side_effect = fill_monitor
        with patch.object(ctypes, 'WinDLL', return_value=api, create=True):
            native = chat_overlay._WindowsOverlay(Mock())
        self.assertEqual(native.work_area(), (-1920, 120, 1920, 1000))
        api.MonitorFromWindow.assert_called_once_with(123, chat_overlay.MONITOR_DEFAULTTONEAREST)
        api.GetWindowThreadProcessId.side_effect = lambda _handle, owner: setattr(owner._obj, 'value', os.getpid() + 1)
        with self.assertRaisesRegex(OSError, 'does not belong'):
            native.work_area()
        self.assertEqual(api.MonitorFromWindow.call_count, 1)

    def test_resize_grip_is_first_right_packed_header_control(self):
        overlay = self.make_overlay()
        overlay.model = 'test-model'
        widgets = []

        def widget(parent, **options):
            result = Mock()
            result.parent, result.options = parent, options
            widgets.append(result)
            return result

        with patch.multiple(chat_overlay.tk, **{name: Mock(side_effect=widget) for name in
                                               ('Frame', 'Label', 'Button', 'Text', 'Scrollbar')}):
            overlay._build()
        header_widgets = [w for w in widgets if w.parent is overlay.header]
        self.assertIs(header_widgets[0], overlay.grip)
        self.assertEqual(overlay.grip.options['cursor'], 'size_ne_sw')
        overlay.grip.pack.assert_called_once_with(side='right')
        overlay.grip.bind.assert_any_call('<B1-Motion>', overlay._resize)
        self.assertIsNot(overlay.grip.parent, overlay.bottom)
        self.assertEqual(overlay.channels_button.options['command'], overlay._toggle_channel_tray)
        self.assertEqual(overlay.settings_button.options['command'], overlay._toggle_settings_drawer)


class DrawerSwitchTests(unittest.TestCase):
    """Drive both real drawers with a controlled clock and headless Tk widgets."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = 10.0
        self.pending = {}
        self.timer_number = 0
        self.events = []
        self.widgets = []
        self.overlay = overlay_with(['say', 'party'])
        self.overlay.channels_button = Mock()
        self.overlay.settings_button = Mock()
        self.overlay._geometry_timer = None
        self.overlay._remember_geometry = Mock()
        self.overlay._native = Mock()
        self.overlay._rect = Mock(return_value=(-700, 800, 300, 230))
        self.overlay._tray_bounds = Mock(return_value=(-1920, 0, 3840, 1080))

        def widget(parent=None, **options):
            result = Mock()
            result.options = dict(options)
            result.winfo_reqwidth.return_value = min(200, len(options.get('text', '')) * 7 + 30)
            result.winfo_reqheight.return_value = 700 if parent is not None else 24
            result.after.side_effect = self.after
            result.after_cancel.side_effect = lambda token: self.pending.pop(token, None)
            result.deiconify.side_effect = lambda: self.events.append(('show', result))
            result.withdraw.side_effect = lambda: self.events.append(('hide', result))
            self.widgets.append(result)
            return result

        self.stack.enter_context(patch.object(overlay_tray.time, 'monotonic', side_effect=lambda: self.now))
        self.stack.enter_context(patch.object(chat_overlay, '_WindowsOverlay', side_effect=lambda _window: Mock()))
        factories = {name: Mock(side_effect=widget) for name in
                     ('Toplevel', 'Frame', 'Label', 'Button', 'Checkbutton', 'Scale', 'Canvas', 'Scrollbar')}
        factories.update(BooleanVar=Value, IntVar=Value)
        self.stack.enter_context(patch.multiple(chat_overlay.tk, **factories))

    def after(self, delay, callback):
        self.timer_number += 1
        token = f'timer-{self.timer_number}'
        self.pending[token] = (delay, callback)
        return token

    def next_frame(self):
        token, (delay, callback) = next(iter(self.pending.items()))
        del self.pending[token]
        self.now += delay / 1000
        callback()
        drawers = (self.overlay._tray, self.overlay._settings_drawer)
        self.assertLessEqual(sum(drawer.is_shown for drawer in drawers if drawer is not None), 1)

    def finish(self):
        for _ in range(100):
            if not self.pending:
                return
            self.next_frame()
        self.fail('Drawer animations did not finish')

    def test_switch_in_both_directions_waits_until_old_window_withdraws(self):
        overlay = self.overlay
        overlay._toggle_channel_tray()
        self.finish()
        channels = overlay._tray
        self.events.clear()
        overlay.open_settings()
        self.assertFalse(channels.is_open)
        self.assertTrue(channels.is_shown)
        self.assertIsNone(overlay._settings_drawer)
        self.finish()
        settings = overlay._settings_drawer
        self.assertTrue(settings.is_open)
        self.assertLess(self.events.index(('hide', channels.window)),
                        self.events.index(('show', settings.window)))
        self.events.clear()
        overlay._toggle_channel_tray()
        self.assertTrue(settings.is_shown)
        self.assertFalse(channels.is_shown)
        self.finish()
        self.assertLess(self.events.index(('hide', settings.window)),
                        self.events.index(('show', channels.window)))
        self.assertEqual(overlay.settings['channels'], ['say', 'party'])
        self.assertEqual((overlay.settings['width'], overlay.settings['height']), (540, 230))

    def test_switch_while_opening_closes_from_current_progress(self):
        overlay = self.overlay
        overlay._toggle_channel_tray()
        self.next_frame()
        overlay._toggle_settings_drawer()
        self.assertGreater(overlay._tray._start_progress, 0)
        self.assertLess(overlay._tray._start_progress, 1)
        self.assertIsNone(overlay._settings_drawer)
        self.finish()
        self.assertTrue(overlay._settings_drawer.is_open)
        self.assertFalse(overlay._tray.is_shown)

    def test_switch_while_already_closing_does_not_restart_close(self):
        overlay = self.overlay
        overlay._toggle_channel_tray()
        self.finish()
        overlay._toggle_channel_tray()
        started_at = overlay._tray._started_at
        self.next_frame()
        overlay.open_settings()
        self.assertEqual(overlay._tray._started_at, started_at)
        self.finish()
        self.assertTrue(overlay._settings_drawer.is_open)

    def test_repeated_pending_toggle_cancels_and_original_toggle_can_reverse(self):
        overlay = self.overlay
        overlay._toggle_channel_tray()
        self.finish()
        overlay._toggle_settings_drawer()
        self.next_frame()
        overlay._toggle_settings_drawer()
        self.finish()
        self.assertIsNone(overlay._settings_drawer)
        self.assertFalse(overlay._tray.is_shown)
        overlay._toggle_channel_tray()
        self.finish()
        overlay._toggle_settings_drawer()
        self.next_frame()
        overlay._toggle_channel_tray()
        self.finish()
        self.assertTrue(overlay._tray.is_open)
        self.assertIsNone(overlay._settings_drawer)

    def test_owner_hide_or_destroy_cancels_pending_open_and_stale_frames(self):
        for action in ('hide', 'destroy'):
            with self.subTest(action=action):
                overlay = self.overlay
                overlay._toggle_channel_tray()
                self.finish()
                overlay.open_settings()
                stale = next(iter(self.pending.values()))[1]
                getattr(overlay, action)()
                stale()
                self.finish()
                self.assertIsNone(overlay._settings_drawer)
                self.assertIsNone(overlay._pending_drawer)
                self.assertFalse(overlay._tray.is_shown)

    def test_launcher_settings_remains_usable_when_hidden_or_locked_and_opens_once(self):
        overlay = self.overlay
        overlay.settings.update(enabled=False, locked=True)
        overlay.open_settings()
        drawer = overlay._settings_drawer
        overlay.open_settings()
        self.finish()
        drawer.window.deiconify.assert_called_once()
        self.assertTrue(drawer.is_open)
        self.assertFalse(overlay.settings['enabled'])
        overlay.window.deiconify.assert_not_called()
        overlay._toggle_channel_tray()
        self.assertIsNone(overlay._tray)
        overlay._change('locked', False)
        overlay._toggle_channel_tray()
        self.finish()
        self.assertTrue(overlay._tray.is_open)

    def test_done_escape_and_settings_header_close_with_animation(self):
        overlay = self.overlay
        for close in (overlay._close_settings, overlay._toggle_settings_drawer,
                      lambda: overlay._settings_drawer._escape()):
            overlay.open_settings()
            self.finish()
            close()
            self.assertFalse(overlay._settings_drawer.is_open)
            self.assertTrue(overlay._settings_drawer.is_shown)
            self.finish()
            self.assertFalse(overlay._settings_drawer.is_shown)
        overlay._settings_drawer.window.destroy.assert_not_called()

    def test_movement_opacity_and_lock_preserve_interactive_settings(self):
        overlay = self.overlay
        overlay.open_settings()
        self.finish()
        settings = overlay._settings_drawer
        settings._native.place.reset_mock()
        overlay._rect.return_value = (-1000, 900, 540, 230)
        overlay._reposition_tray()
        self.assertEqual(settings._native.place.call_args.args[0], -1000)
        self.assertEqual(settings._native.place.call_args.args[2], 540)
        overlay._change('opacity', .6)
        settings.window.attributes.assert_any_call('-alpha', .6)
        settings._native.style.assert_called_with(False, .6)
        overlay._change('locked', True)
        self.assertTrue(settings.is_open)
        self.assertIsNone(overlay._pending_drawer)
        self.assertEqual(overlay.settings['channels'], ['say', 'party'])

    def test_settings_controls_reflow_and_heading_wraps_around_measured_actions(self):
        overlay = self.overlay
        overlay.open_settings()
        self.finish()
        self.assertEqual(overlay._settings_choices[1].grid.call_args.kwargs['column'], 0)
        self.assertEqual(overlay._settings_choices[1].grid.call_args.kwargs['row'], 1)
        overlay._settings_actions[0].winfo_reqwidth.return_value = 240
        overlay._settings_actions[1].winfo_reqwidth.return_value = 100
        overlay._settings_drawer._layout_width = None
        overlay._reposition_tray()
        self.assertEqual(overlay._settings_actions[0].pack.call_args.kwargs['side'], 'top')
        body_width = 300 - 2 - 12 - 16
        buttons_width = sum(widget.winfo_reqwidth() + 5 for widget in overlay._settings_channel_actions)
        overlay._settings_channel_heading.configure.assert_called_with(
            wraplength=body_width - buttons_width)
        overlay._rect.return_value = (-700, 800, 700, 230)
        overlay._reposition_tray()
        self.assertEqual(overlay._settings_choices[1].grid.call_args.kwargs['column'], 1)
        self.assertEqual(overlay._settings_choices[1].grid.call_args.kwargs['row'], 0)
        self.assertEqual(overlay._settings_actions[0].pack.call_args.kwargs['side'], 'left')
        self.assertEqual(overlay.settings['channels'], ['say', 'party'])


if __name__ == '__main__':
    unittest.main()
