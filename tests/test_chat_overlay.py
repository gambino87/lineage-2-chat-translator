from collections import deque
import unittest
from unittest.mock import Mock

from chat_overlay import (ChatOverlay, DEFAULT_OVERLAY, OverlayHistory,
    WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TRANSPARENT, clamp_placement,
    make_overlay_message, message_parts, normalize_overlay_settings, overlay_extended_style)
from translator_core import ChatRecord


class OverlaySettingsTests(unittest.TestCase):
    def test_unknown_and_invalid_settings_do_not_escape_bounds(self):
        result = normalize_overlay_settings({"opacity": float("nan"), "font_size": 500,
            "width": -9, "height": "bad", "x": "-1200", "y": float("inf"),
            "enabled": "false", "locked": 1, "show_original": True, "arbitrary": "ignored",
            "show_timestamps": False, "show_channels": True})
        self.assertEqual(result["opacity"], .85)
        self.assertEqual(result["font_size"], 22)
        self.assertEqual(result["width"], 300)
        self.assertEqual(result["height"], 230)
        self.assertEqual(result["x"], -1200)
        self.assertIsNone(result["y"])
        self.assertFalse(result["enabled"])
        self.assertFalse(result["locked"])
        self.assertTrue(result["show_original"])
        self.assertNotIn("arbitrary", result)
        self.assertNotIn("show_timestamps", result)
        self.assertNotIn("show_channels", result)

    def test_numeric_booleans_and_nonmapping_use_defaults(self):
        result = normalize_overlay_settings({"opacity": False, "font_size": True, "x": False})
        self.assertEqual(result["opacity"], .85)
        self.assertEqual(result["font_size"], 11)
        self.assertIsNone(result["x"])
        self.assertEqual(normalize_overlay_settings(None), DEFAULT_OVERLAY)

    def test_saved_negative_monitor_coordinates_survive(self):
        self.assertEqual(clamp_placement({"x": -1600, "y": 50, "width": 540, "height": 230},
                         (-1920, 0, 3840, 1080)), (-1600, 50, 540, 230))

    def test_disconnected_monitor_location_is_recovered(self):
        x, y, width, height = clamp_placement({"x": -3000, "y": 5000, "width": 600, "height": 250},
                                            (0, 0, 1920, 1080))
        self.assertEqual((x, y, width, height), (0, 830, 600, 250))

    def test_tiny_desktop_still_keeps_window_on_screen(self):
        self.assertEqual(clamp_placement({"x": 999, "y": 999}, (0, 0, 240, 100)), (0, 0, 240, 100))


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.record = ChatRecord("18:18:29", "Player", "Привет", 25, source_language="Russian")

    def test_compact_translation_and_optional_original(self):
        message = make_overlay_message(self.record, "Hello")
        self.assertEqual(message_parts(message, DEFAULT_OVERLAY),
                         ("[18:18:29]", "[RU]", "[Player]: ", "Hello", ""))
        settings = dict(DEFAULT_OVERLAY, show_timestamps=False, show_channels=True, show_original=True)
        self.assertEqual(message_parts(message, settings),
                         ("[18:18:29]", "[RU]", "[Player]: ", "Hello", "Привет"))

    def test_language_codes_represent_source_language_and_unknown_is_explicit(self):
        for language, code in (("English", "EN"), ("Chinese", "CN"), ("Russian", "RU"),
                               ("Universal trade", "--"), ("Unknown", "??"), (None, "??")):
            with self.subTest(language=language):
                record = ChatRecord("19:51:50", "Name", "Original text", 25, source_language=language)
                parts = message_parts(make_overlay_message(record, "Translated message"), DEFAULT_OVERLAY)
                self.assertEqual("".join(parts[:4]), f"[19:51:50][{code}][Name]: Translated message")

    def test_no_duplicate_original_when_translation_unchanged(self):
        message = make_overlay_message(self.record, "Привет")
        self.assertEqual(message_parts(message, {"show_original": True})[4], "")

    def test_newlines_cannot_impersonate_additional_chat_rows(self):
        record = ChatRecord("18:18:29", "Name\nFake", "hello\nworld", 0)
        message = make_overlay_message(record, "hello\nOther: forged\tline")
        self.assertEqual(message.sender, "Name Fake")
        self.assertEqual(message.translated, "hello Other: forged line")

    def test_errors_are_bounded_and_labelled(self):
        message = make_overlay_message(self.record, "x" * 5000, error=True)
        parts = message_parts(message, DEFAULT_OVERLAY)
        self.assertTrue(parts[3].startswith("Translation unavailable: "))
        self.assertEqual(len(message.translated), 180)

    def test_history_evicts_oldest_without_losing_identical_new_events(self):
        history = OverlayHistory(limit=2)
        first = make_overlay_message(self.record, "Hello")
        last = make_overlay_message(self.record, "Last")
        self.assertFalse(history.append(first))
        self.assertFalse(history.append(first))
        self.assertTrue(history.append(last))
        self.assertEqual(list(history.messages), [first, last])
        history.clear()
        self.assertEqual(len(history.messages), 0)


class WindowStyleTests(unittest.TestCase):
    def test_lock_sets_layered_clickthrough_and_noactivate_only(self):
        unrelated = 0x80 | 0x8 | 0x200
        locked = overlay_extended_style(unrelated, True)
        self.assertEqual(locked, unrelated | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        self.assertEqual(overlay_extended_style(locked, False), unrelated | WS_EX_LAYERED)


class OverlayStateTests(unittest.TestCase):
    def make_overlay(self, view):
        overlay = ChatOverlay.__new__(ChatOverlay)
        overlay.settings = DEFAULT_OVERLAY.copy()
        overlay.on_reply = None
        overlay._reply_records = {}
        overlay.history = OverlayHistory()
        overlay.root = Mock()
        overlay.viewport = Mock()
        overlay.text = Mock()
        overlay.text.yview.return_value = view
        overlay.text.index.return_value = "3.0"
        overlay.latest = Mock()
        overlay._marks = deque()
        overlay._sequence = 0
        overlay._unseen = 0
        overlay._destroyed = False
        overlay._layout_timer = None
        record = ChatRecord("18:18:29", "Player", "Привет", 25, source_language="Russian")
        overlay.history.append(make_overlay_message(record, "Hello"))
        return overlay, record

    def test_new_message_preserves_scrolled_position_and_marks_unread(self):
        overlay, record = self.make_overlay((.1, .5))
        overlay.add_message(record, "New message")
        overlay.text.yview.assert_called_with("overlay_view")
        overlay.text.see.assert_not_called()
        self.assertEqual(overlay._unseen, 1)
        overlay.latest.configure.assert_called_with(text="1 new · Latest ↓")

    def test_at_bottom_new_messages_follow_without_focus_changes(self):
        overlay, record = self.make_overlay((.1, 1.0))
        overlay.add_message(record, "New message")
        overlay.text.see.assert_called_with("end")
        overlay.text.focus_set.assert_not_called()
        overlay.text.focus_force.assert_not_called()
        self.assertEqual(overlay._unseen, 0)

    def test_short_chat_uses_its_content_height_and_follows_after_layout(self):
        overlay, _ = self.make_overlay((0, 1))
        overlay.text.count.return_value = (36,)
        overlay.text.cget.return_value = 3
        overlay.text.winfo_height.return_value = 200
        overlay.viewport.winfo_height.return_value = 200
        overlay._align_bottom()
        overlay.text.place_configure.assert_called_once_with(height=42)
        overlay.root.after_idle.assert_called_once_with(overlay._finish_bottom_layout)
        overlay._finish_bottom_layout()
        overlay.text.see.assert_called_once_with("end")

    def test_long_chat_fills_viewport_without_jumping_from_older_messages(self):
        overlay, _ = self.make_overlay((.1, .5))
        overlay.text.count.return_value = 900  # Tk also returns a scalar count.
        overlay.text.cget.return_value = 3
        overlay.text.winfo_height.return_value = 300
        overlay.viewport.winfo_height.return_value = 180
        overlay._align_bottom()
        overlay.text.place_configure.assert_called_once_with(height=180)
        overlay.root.after_idle.assert_not_called()
        overlay.text.see.assert_not_called()

    def test_rows_have_separators_without_blank_line_below_latest(self):
        overlay, record = self.make_overlay((0, 1))
        overlay._write_message(make_overlay_message(record, "First"))
        overlay._write_message(make_overlay_message(record, "Second"))
        rendered = "".join(call.args[1] for call in overlay.text.insert.call_args_list)
        self.assertEqual(rendered, "[18:18:29][RU][Player]: First\n[18:18:29][RU][Player]: Second")
        self.assertFalse(overlay._reply_records)  # No callback means plain labels.

    def test_reply_link_dispatches_exact_source_record_in_same_row(self):
        overlay, _ = self.make_overlay((0, 1))
        overlay.history.clear()
        overlay.on_reply = Mock()
        record = ChatRecord("18:18:29", "Name", "Привет\nмир", 25, ("[Sword]",), "Russian")
        other = ChatRecord("18:18:30", "Other", "Hola", 25, source_language="Spanish")
        overlay.add_message(record, "Hello world")
        first_tag = next(iter(overlay._reply_records))
        overlay.add_message(other, "Hello")
        rendered = "".join(call.args[1] for call in overlay.text.insert.call_args_list)
        self.assertEqual(rendered, "[18:18:29][RU][Name]: Hello world\n[18:18:30][ES][Other]: Hello")
        reply_runs = [call.args[1] for call in overlay.text.insert.call_args_list
                      if len(call.args) > 2 and isinstance(call.args[2], tuple) and "reply" in call.args[2]]
        self.assertEqual(reply_runs, ["[RU]", "[ES]"])
        overlay.text.tag_names.return_value = ("reply", first_tag)
        self.assertEqual(overlay._reply_clicked(Mock(x=12, y=34)), "break")
        overlay.on_reply.assert_called_once_with(record)
        self.assertIs(overlay.on_reply.call_args.args[0], record)
        overlay.text.tag_names.assert_called_once_with("@12,34")

    def test_reply_links_are_inactive_while_locked_or_destroyed(self):
        overlay, record = self.make_overlay((0, 1))
        overlay.on_reply = Mock()
        overlay.add_message(record, "Hello")
        overlay.text.tag_names.return_value = (next(iter(overlay._reply_records)),)
        overlay.settings["locked"] = True
        overlay._reply_clicked(Mock(x=0, y=0))
        overlay.on_reply.assert_not_called()
        overlay.settings["locked"] = False
        overlay._destroyed = True
        overlay._reply_clicked(Mock(x=0, y=0))
        overlay.on_reply.assert_not_called()

    def test_name_link_selects_exact_sender_and_keeps_channel_color(self):
        overlay, _ = self.make_overlay((0, 1))
        overlay.on_reply = Mock()
        record = ChatRecord("20:00:00", "ExamplePlayer", "Hola", 25, source_language="Spanish")
        overlay.add_message(record, "Hello")
        tag = next(iter(overlay._reply_records))
        name_run = next(call for call in overlay.text.insert.call_args_list if call.args[1] == "[ExamplePlayer]: ")
        self.assertEqual(name_run.args[2], ("channel25", "sender", "whisper", tag))
        overlay.text.tag_names.return_value = name_run.args[2]
        self.assertEqual(overlay._whisper_clicked(Mock(x=5, y=6)), "break")
        overlay.on_reply.assert_called_once_with(record, whisper=True)

    def test_name_links_are_inactive_while_locked_or_destroyed(self):
        overlay, record = self.make_overlay((0, 1))
        overlay.on_reply = Mock()
        overlay.add_message(record, "Hello")
        overlay.text.tag_names.return_value = ("whisper", next(iter(overlay._reply_records)))
        overlay.settings["locked"] = True
        overlay._whisper_clicked(Mock(x=0, y=0))
        overlay.on_reply.assert_not_called()
        overlay.settings["locked"] = False
        overlay._destroyed = True
        overlay._whisper_clicked(Mock(x=0, y=0))
        overlay.on_reply.assert_not_called()

    def test_missing_or_invalid_name_stays_plain_but_language_link_remains(self):
        for sender in ("", 'Bad"Name', "Two Names", "Bad\nName"):
            with self.subTest(sender=sender):
                overlay, _ = self.make_overlay((0, 1))
                overlay.on_reply = Mock()
                record = ChatRecord("20:00:00", sender, "Hello", 10, source_language="English")
                overlay.add_message(record, "Hello")
                name_run = next(call for call in overlay.text.insert.call_args_list
                                if len(call.args) > 2 and "sender" in call.args[2])
                self.assertNotIn("whisper", name_run.args[2])
                tag = next(iter(overlay._reply_records))
                overlay.text.tag_names.return_value = ("reply", tag)
                overlay._reply_clicked(Mock(x=0, y=0))
                overlay.on_reply.assert_called_once_with(record)
    def test_reply_lookup_stays_bounded_and_removed_tags_are_deleted(self):
        overlay, record = self.make_overlay((0, 1))
        overlay.history = OverlayHistory(limit=2)
        overlay.on_reply = Mock()
        overlay.add_message(record, "First")
        first_tag = next(iter(overlay._reply_records))
        overlay.add_message(record, "Second")
        overlay.add_message(record, "Third")
        self.assertEqual(len(overlay._reply_records), 2)
        self.assertNotIn(first_tag, overlay._reply_records)
        overlay.text.tag_delete.assert_called_once_with(first_tag)

    def test_rerender_preserves_reply_sources_and_clear_reclaims_tags(self):
        overlay, record = self.make_overlay((0, 1))
        overlay.history.clear()
        overlay.on_reply = Mock()
        overlay.add_message(record, "Hello")
        old_tag = next(iter(overlay._reply_records))
        overlay.settings["show_original"] = True
        overlay.text.insert.reset_mock()
        overlay._render_all()
        self.assertNotIn(old_tag, overlay._reply_records)
        self.assertEqual(list(overlay._reply_records.values()), [record])
        rendered = "".join(call.args[1] for call in overlay.text.insert.call_args_list)
        self.assertEqual(rendered, "[18:18:29][RU][Player]: Hello\n  Привет")
        overlay.text.tag_delete.assert_called_once_with(old_tag)
        new_tag = next(iter(overlay._reply_records))
        overlay.clear()
        self.assertEqual(overlay._reply_records, {})
        overlay.text.tag_delete.assert_called_with(new_tag)
        self.assertFalse(overlay._marks)

    def test_destroy_keeps_enabled_preference_for_next_launch(self):
        overlay, _ = self.make_overlay((0, 1))
        overlay.settings["enabled"] = True
        overlay.root = Mock()
        overlay.window = Mock()
        overlay._geometry_timer = "pending-timer"
        overlay._remember_geometry = Mock()
        overlay._close_settings = Mock()
        overlay.destroy()
        self.assertTrue(overlay.settings["enabled"])
        self.assertTrue(overlay._destroyed)
        overlay.window.destroy.assert_called_once()
        overlay.root.after_cancel.assert_called_once_with("pending-timer")


if __name__ == "__main__":
    unittest.main()
