"""Headless channel tray checks: no Tk root, desktop window or native call."""
from contextlib import ExitStack
import unittest
from unittest.mock import Mock, call, patch

import overlay_tray
from overlay_tray import ChannelTray, choice_columns, tray_geometry


OPTIONS = (("say", "Say"), ("party", "Party"), ("trade", "Trade"),
           ("command", "Command channel"), ("world", "World / global"))
PALETTE = dict(background="#10100f", header="#201f1a", border="#625841",
               foreground="#d4c59a", muted="#a59e8b")


class Value:
    def __init__(self, *, master=None, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class TrayGeometryTests(unittest.TestCase):
    def test_opens_upward_with_fixed_lower_edge_and_negative_monitor_position(self):
        anchor = (-700, 300, 540, 230)
        bounds = (-1920, -200, 3840, 1280)
        for progress, height in ((0, 1), (.25, 40), (.5, 80), (1, 160)):
            self.assertEqual(tray_geometry(anchor, bounds, 160, progress),
                             (-700, 300 - height, 540, height))

    def test_screen_top_fallback_and_side_edges_keep_entire_tray_on_screen(self):
        self.assertEqual(tray_geometry((1800, 20, 540, 230), (0, 0, 1920, 1080), 180, 1),
                         (1380, 0, 540, 180))
        self.assertEqual(tray_geometry((-2300, -180, 540, 230), (-1920, -200, 1920, 1080), 180, 1),
                         (-1920, -200, 540, 180))

    def test_tiny_desktop_caps_size_and_invalid_progress_does_not_escape_bounds(self):
        for progress in (-1, 0, .5, 1, 9):
            x, y, width, height = tray_geometry((999, 999, 540, 230), (0, 0, 240, 100), 180, progress)
            self.assertEqual((x, width), (0, 240))
            self.assertTrue(0 <= y and y + height <= 100)

    def test_columns_follow_measured_labels_at_narrow_wide_and_high_dpi_sizes(self):
        self.assertEqual(choice_columns(300, 130), 2)
        self.assertEqual(choice_columns(540, 130), 3)
        self.assertEqual(choice_columns(300, 180), 1)
        self.assertEqual(choice_columns(1000, 100), 3)


class TrayBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = 10.0
        self.pending = {}
        self.timer_number = 0
        self.widgets = []
        self.anchor = (-700, 300, 540, 230)
        self.bounds = (-1920, 0, 3840, 1080)

        def widget(parent=None, **kwargs):
            result = Mock()
            result.options = kwargs
            result.parent = parent
            result.winfo_reqwidth.return_value = len(kwargs.get("text", "")) * 7 + 30
            result.winfo_reqheight.return_value = 24
            self.widgets.append(result)
            return result

        self.window = widget()
        self.window.after.side_effect = self.after
        self.window.after_cancel.side_effect = lambda timer: self.pending.pop(timer, None)
        self.native = Mock()
        self.owner = Mock()
        self.change = Mock()
        self.open_change = Mock()
        self.stack.enter_context(patch.object(overlay_tray.time, "monotonic", side_effect=lambda: self.now))
        self.stack.enter_context(patch.multiple(overlay_tray.tk,
            Toplevel=Mock(return_value=self.window), Frame=Mock(side_effect=widget),
            Label=Mock(side_effect=widget), Button=Mock(side_effect=widget),
            Checkbutton=Mock(side_effect=widget), BooleanVar=Value))
        self.tray = ChannelTray(self.owner, options=OPTIONS, selected=["say", "party"],
            on_change=self.change, get_anchor=lambda: self.anchor, get_bounds=lambda: self.bounds,
            native_factory=lambda window: self.native, palette=PALETTE, opacity=.8,
            on_open_change=self.open_change)

    def after(self, delay, callback):
        self.timer_number += 1
        token = f"timer-{self.timer_number}"
        self.pending[token] = (delay, callback)
        return token

    def next_frame(self):
        token, (delay, callback) = next(iter(self.pending.items()))
        del self.pending[token]
        self.now += delay / 1000
        callback()

    def finish_animation(self):
        for _ in range(30):
            if not self.pending:
                return
            token, (delay, callback) = next(iter(self.pending.items()))
            del self.pending[token]
            self.now += delay / 1000
            callback()
        self.fail("Animation did not finish within a bounded number of frames")

    def test_constructor_is_withdrawn_and_selection_sync_is_not_a_user_change(self):
        self.window.withdraw.assert_called_once()
        self.window.deiconify.assert_not_called()
        self.assertFalse(self.tray.is_open)
        self.assertTrue(self.tray._variables["say"].get())
        self.assertFalse(self.tray._variables["trade"].get())
        self.tray.sync(["trade"], .6)
        self.assertTrue(self.tray._variables["trade"].get())
        self.assertFalse(self.tray._variables["say"].get())
        self.window.attributes.assert_any_call("-alpha", .6)
        self.change.assert_not_called()

    def test_open_and_close_animation_preserve_owner_and_finish_without_focus_calls(self):
        self.tray.toggle()
        self.assertTrue(self.tray.is_open)
        self.open_change.assert_called_once_with(True)
        self.native.style.assert_has_calls([call(True, .8), call(False, .8)])
        self.finish_animation()
        x, y, width, height = self.native.place.call_args.args
        self.assertEqual((x, width, y + height), (-700, 540, 300))
        self.assertEqual(self.tray._progress, 1)
        self.assertFalse(self.pending)
        self.owner.geometry.assert_not_called()
        self.window.focus_force.assert_not_called()
        self.window.focus_set.assert_not_called()
        self.window.lift.assert_not_called()
        self.tray.toggle()
        self.assertFalse(self.tray.is_open)
        self.finish_animation()
        self.assertEqual(self.tray._progress, 0)
        self.assertFalse(self.tray._shown)
        self.assertFalse(self.pending)
        self.assertEqual(self.open_change.call_args_list, [call(True), call(False)])

    def test_rapid_toggle_reverses_current_progress_and_stale_tick_cannot_run(self):
        self.tray.toggle()
        stale = next(iter(self.pending.values()))[1]
        self.now += .06
        self.tray.toggle()
        progress = self.tray._progress
        self.assertTrue(0 < progress < 1)
        self.assertFalse(self.tray.is_open)
        self.native.place.reset_mock()
        stale()
        self.native.place.assert_not_called()
        self.now += .02
        self.tray.toggle()
        self.assertTrue(self.tray.is_open)
        self.assertLess(self.tray._start_progress, progress)
        self.finish_animation()
        self.assertEqual(self.tray._progress, 1)
        self.assertFalse(self.pending)

    def test_frames_use_one_native_placement_without_relayout_or_idle_flushes(self):
        self.tray.toggle()
        self.window.geometry.reset_mock()
        self.window.update_idletasks.reset_mock()
        self.tray.content.place.reset_mock()
        self.native.place.reset_mock()
        self.tray.get_anchor = Mock(side_effect=AssertionError("Anchor queried during frame"))
        self.tray.get_bounds = Mock(side_effect=AssertionError("Monitor queried during frame"))
        self.finish_animation()
        frames = [item.args for item in self.native.place.call_args_list]
        self.assertGreater(len(frames), 8)
        self.assertEqual([height for _, _, _, height in frames],
                         sorted(height for _, _, _, height in frames))
        self.assertTrue(all(x == -700 and width == 540 and y + height == 300
                            for x, y, width, height in frames))
        self.window.geometry.assert_not_called()
        self.window.update_idletasks.assert_not_called()
        self.tray.content.place.assert_not_called()

    def test_render_cost_is_in_frame_budget_and_missed_frames_are_skipped(self):
        self.tray.toggle()
        self.native.place.side_effect = lambda *_: setattr(self, "now", self.now + .006)
        self.next_frame()
        delay, _ = next(iter(self.pending.values()))
        self.assertLessEqual(delay, 11)
        self.assertGreater(delay, 0)
        self.now += .075
        self.next_frame()
        self.assertEqual(len(self.pending), 1)
        self.assertLessEqual(next(iter(self.pending.values()))[0], 17)
        self.assertGreater(self.tray._progress, .4)
        self.finish_animation()
        self.assertEqual(self.tray._progress, 1)

    def test_slow_first_map_does_not_consume_opening_animation_and_reopen_settles(self):
        self.window.deiconify.side_effect = lambda: setattr(self, "now", self.now + .12)
        self.window.update_idletasks.side_effect = lambda: setattr(self, "now", self.now + .025)
        self.tray.toggle()
        self.assertEqual(self.tray._progress, 0)
        self.assertEqual(self.tray._started_at, self.now)
        self.native.place.assert_called_with(-700, 299, 540, 1)
        self.finish_animation()
        self.tray.toggle()
        self.finish_animation()
        self.window.update_idletasks.reset_mock()
        self.tray.toggle()
        self.assertEqual(self.tray._progress, 0)
        self.assertEqual(self.window.update_idletasks.call_count, 3)
        self.native.place.assert_called_with(-700, 299, 540, 1)
        self.finish_animation()

    def test_easing_starts_and_finishes_gently(self):
        self.tray.toggle()
        self.now = self.tray._started_at + self.tray._duration * .1
        self.tray._advance()
        self.assertAlmostEqual(self.tray._progress, .028)
        self.now = self.tray._started_at + self.tray._duration * .9
        self.tray._advance()
        self.assertAlmostEqual(self.tray._progress, .972)

    def test_non_native_fallback_animates_without_flushing_idles_per_frame(self):
        self.tray._native = None
        self.tray.toggle()
        self.window.update_idletasks.reset_mock()
        self.finish_animation()
        self.window.geometry.assert_called_with(
            f"540x{self.tray._wanted_height}-700+{300 - self.tray._wanted_height}")
        self.window.update_idletasks.assert_not_called()

    def test_hide_and_destroy_cancel_callbacks_and_never_reopen(self):
        self.tray.toggle()
        stale = next(iter(self.pending.values()))[1]
        self.tray.hide()
        self.assertFalse(self.tray.is_open)
        self.assertFalse(self.tray._shown)
        self.assertFalse(self.pending)
        self.assertEqual(self.tray._progress, 0)
        self.native.place.reset_mock()
        stale()
        self.native.place.assert_not_called()
        self.tray.toggle()
        self.tray.destroy()
        self.tray.destroy()
        self.assertFalse(self.pending)
        self.window.destroy.assert_called_once()
        self.window.deiconify.reset_mock()
        self.tray.toggle()
        self.tray.reposition()
        self.tray.sync(["say"], .4)
        self.window.deiconify.assert_not_called()

    def test_escape_collapses_instead_of_destroying_controls(self):
        self.tray.toggle()
        self.finish_animation()
        self.assertEqual(self.tray._escape(), "break")
        self.finish_animation()
        self.assertFalse(self.tray.is_open)
        self.window.destroy.assert_not_called()

    def test_all_none_and_checkbox_callbacks_emit_exact_selections(self):
        by_text = {widget.options.get("text"): widget for widget in self.widgets}
        by_text["None"].options["command"]()
        self.change.assert_called_with([])
        by_text["Say"].options["variable"].set(True)
        by_text["Say"].options["command"]()
        self.change.assert_called_with(["say"])
        by_text["All"].options["command"]()
        self.change.assert_called_with([key for key, _label in OPTIONS])
        self.change.reset_mock()
        self.tray.sync(["party"], .8)
        self.change.assert_not_called()

    def test_reposition_follows_owner_and_reflows_narrow_width_without_changing_selection(self):
        self.tray.reposition()
        self.native.place.assert_not_called()
        self.tray.toggle()
        self.finish_animation()
        self.anchor = (-1000, 30, 300, 230)
        self.tray.reposition()
        x, y, width, height = self.native.place.call_args.args
        self.assertEqual((x, y, width), (-1000, 0, 300))
        self.assertTrue(height > 30)
        self.assertEqual(self.tray._choices[2].grid.call_args.kwargs["row"], 1)
        self.assertEqual(self.tray._choices[2].grid.call_args.kwargs["column"], 0)
        self.assertTrue(self.tray._variables["say"].get())
        self.change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
