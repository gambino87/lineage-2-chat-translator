"""Headless checks for tray ownership and top-right resizing; no windows opened."""
import unittest
import ctypes
import os
from unittest.mock import Mock, patch

import chat_overlay
from chat_overlay import CHANNEL_FILTERS, resize_from_top_right
from test_overlay_channel_filters import overlay_with, record


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
            overlay._toggle_channel_tray()
            overlay._toggle_channel_tray()
            factory.assert_called_once()
            self.assertIs(factory.call_args.args[0], overlay.window)
            self.assertEqual(factory.call_args.kwargs['options'], [(key, label) for key, label, _ in CHANNEL_FILTERS])
            factory.return_value.toggle.assert_has_calls([unittest.mock.call(), unittest.mock.call()])
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


if __name__ == '__main__':
    unittest.main()
