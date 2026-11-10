"""Channel-filter regression checks using production handlers without Tk windows."""
from collections import deque
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import chat_overlay
from chat_overlay import (ALL_CHANNEL_FILTERS, ChatOverlay, DEFAULT_OVERLAY,
                          OverlayHistory, channel_is_visible, normalize_overlay_settings)
import translator_app
from translator_core import ChatRecord
from test_overlay_integration import Transcript, Value, headless_app, ORIGINS_LOG


class OverlayText(Transcript):
    """Track displayed text and marks, and spy on view/focus operations."""
    def __init__(self):
        super().__init__()
        self.see = Mock()
        self.focus_set = Mock()
        self.focus_force = Mock()
        self.tag_delete = Mock()
        self.tag_configure = Mock()
        self.yview_moveto = Mock()
        self.yview = Mock(wraps=super().yview)

    def insert(self, position, text, tag=()):
        super().insert(position, text, tag)


def overlay_with(channels=None, limit=250):
    overlay = ChatOverlay.__new__(ChatOverlay)
    overlay.settings = normalize_overlay_settings({} if channels is None else {"channels": channels})
    overlay.history = OverlayHistory(limit)
    overlay.text = OverlayText()
    overlay.root = Mock()
    overlay.window = Mock()
    overlay.caption = Mock()
    overlay.latest = Mock()
    overlay.on_reply = Mock()
    overlay.on_change = Mock()
    overlay._marks = deque()
    overlay._reply_records = {}
    overlay._sequence = 0
    overlay._unseen = 0
    overlay._destroyed = False
    overlay._layout_timer = None
    overlay._settings_window = None
    overlay._settings_drawer = None
    overlay._pending_drawer = None
    overlay._tray = None
    overlay._localized_widgets = []
    overlay._native = None
    overlay._vars = {}
    overlay._channel_vars = {}
    return overlay


def record(number, channel):
    return ChatRecord(f"10:00:{number:02}", f"Player{number}", f"Source{number}", channel,
                      source_language="English")


def line(number):
    return f"[10:00:{number:02}][EN][Player{number}]: Text{number}"


class FilterNormalizationTests(unittest.TestCase):
    def test_missing_preferences_preserve_all_channels_including_unknown_ids(self):
        prefs = normalize_overlay_settings({"font_size": 14})
        self.assertEqual(prefs["channels"], list(ALL_CHANNEL_FILTERS))
        self.assertEqual(prefs["font_size"], 14)
        for channel in (0, 3, 25, 10, -1, 9999):
            self.assertTrue(channel_is_visible(channel, prefs))

    def test_invalid_preferences_fall_back_to_all_but_explicit_empty_stays_empty(self):
        for malformed in (None, True, 3, "party", {"say": True}, [None, 3, False], ["bogus"]):
            with self.subTest(malformed=malformed):
                self.assertEqual(normalize_overlay_settings({"channels": malformed})["channels"],
                                 list(ALL_CHANNEL_FILTERS))
        empty = normalize_overlay_settings({"channels": []})
        self.assertEqual(empty["channels"], [])
        self.assertFalse(channel_is_visible(0, empty))
        self.assertFalse(channel_is_visible(9999, empty))

    def test_mixed_preferences_keep_valid_choices_once_without_coercing_other_values(self):
        prefs = normalize_overlay_settings({"channels": ["party", "say", "party", True, 0, {}, "invalid"]})
        self.assertEqual(prefs["channels"], ["say", "party"])

    def test_normalized_lists_are_independent_of_input_defaults_and_other_windows(self):
        supplied = ["say", "party"]
        first = normalize_overlay_settings({"channels": supplied})
        second = normalize_overlay_settings({})
        third = normalize_overlay_settings({})
        first["channels"].clear()
        second["channels"].clear()
        self.assertEqual(supplied, ["say", "party"])
        self.assertEqual(third["channels"], list(ALL_CHANNEL_FILTERS))
        self.assertEqual(DEFAULT_OVERLAY["channels"], list(ALL_CHANNEL_FILTERS))


class FilterDisplayTests(unittest.TestCase):
    def test_say_and_party_filter_keeps_full_history_and_uses_channel_not_message_words(self):
        overlay = overlay_with(["say", "party"])
        records = [record(1, 0), record(2, 8), record(3, 3), record(4, 9999)]
        for number, item in enumerate(records, 1):
            overlay.add_message(item, f"Text{number}")
        self.assertEqual(overlay.text.text, line(1) + "\n" + line(3))
        self.assertEqual([message.record for message in overlay.history.messages], records)
        self.assertEqual(list(overlay._reply_records.values()), [records[0], records[2]])
        # A party advertisement in Say still belongs to Say.
        overlay._change("channels", ["party"])
        overlay.add_message(record(5, 0), "Looking for a party")
        self.assertEqual(overlay.text.text, line(3))

    def test_other_restores_unknown_ids_and_notices_without_including_known_channels(self):
        overlay = overlay_with(["say"])
        unknown, notice, party = record(1, -123), record(2, 10), record(3, 3)
        for number, item in enumerate((unknown, notice, party), 1):
            overlay.add_message(item, f"Text{number}")
        self.assertEqual(overlay.text.text, "")
        overlay._change("channels", ["other"])
        self.assertEqual(overlay.text.text, line(1) + "\n" + line(2))
        self.assertEqual(list(overlay._reply_records.values()), [unknown, notice])

    def test_hidden_arrival_does_not_change_display_unread_focus_or_scroll(self):
        overlay = overlay_with(["say"])
        overlay.add_message(record(1, 0), "Text1")
        overlay.text.view = (.1, .5)
        overlay._unseen = 2
        overlay.text.see.reset_mock()
        overlay.text.yview.reset_mock()
        overlay.latest.reset_mock()
        overlay.add_message(record(2, 3), "Text2")
        self.assertEqual(overlay.text.text, line(1))
        self.assertEqual(overlay._unseen, 2)
        self.assertTrue(all(not call.args for call in overlay.text.yview.call_args_list))
        overlay.text.see.assert_not_called()
        overlay.text.yview_moveto.assert_not_called()
        overlay.text.focus_set.assert_not_called()
        overlay.text.focus_force.assert_not_called()
        overlay.latest.configure.assert_not_called()

    def test_visible_arrival_while_scrolled_up_marks_only_visible_message_unread(self):
        overlay = overlay_with(["say"])
        overlay.add_message(record(1, 0), "Text1")
        overlay.text.view = (.1, .5)
        overlay.text.see.reset_mock()
        overlay.add_message(record(2, 8), "Text2")
        overlay.add_message(record(3, 0), "Text3")
        self.assertEqual(overlay._unseen, 1)
        overlay.text.yview.assert_called_with("overlay_view")
        overlay.text.see.assert_not_called()

    def test_limit_one_handles_every_visible_hidden_eviction_transition(self):
        for first, second in ((0, 0), (0, 8), (8, 0), (8, 8)):
            with self.subTest(first=first, second=second):
                overlay = overlay_with(["say"], limit=1)
                old, new = record(1, first), record(2, second)
                overlay.add_message(old, "Text1")
                old_tags = set(overlay._reply_records)
                overlay.add_message(new, "Text2")
                self.assertEqual([item.record for item in overlay.history.messages], [new])
                self.assertEqual(overlay.text.text, line(2) if second == 0 else "")
                self.assertEqual(list(overlay._reply_records.values()), [new] if second == 0 else [])
                self.assertEqual(len(overlay._marks), int(second == 0))
                self.assertTrue(old_tags.isdisjoint(overlay._reply_records))
                for tag in old_tags:
                    overlay.text.tag_delete.assert_any_call(tag)

    def test_mixed_eviction_keeps_visible_order_separators_and_reply_targets_bounded(self):
        overlay = overlay_with(["say", "party"], limit=3)
        recent = deque(maxlen=3)
        for number, channel in enumerate((0, 8, 3, 8, 8, 0, 3, 0, 8, 3), 1):
            item = record(number, channel)
            recent.append((number, item))
            overlay.add_message(item, f"Text{number}")
            visible = [(number, item) for number, item in recent if item.channel in (0, 3)]
            self.assertEqual(overlay.text.text, "\n".join(line(number) for number, _ in visible))
            self.assertEqual(list(overlay._reply_records.values()), [item for _, item in visible])
            self.assertEqual(len(overlay._marks), len(visible))
            self.assertEqual(len(overlay.history.messages), len(recent))

    def test_hidden_arrival_evicting_visible_row_preserves_scrolled_view_and_unread(self):
        overlay = overlay_with(["say"], limit=2)
        overlay.add_message(record(1, 0), "Text1")
        overlay.add_message(record(2, 0), "Text2")
        overlay.text.view = (.1, .5)
        overlay._unseen = 1
        overlay.text.see.reset_mock()
        overlay.add_message(record(3, 8), "Text3")
        self.assertEqual(overlay.text.text, line(2))
        self.assertEqual(overlay._unseen, 1)
        overlay.text.see.assert_not_called()
        overlay.text.yview.assert_called_with("overlay_view")

    def test_filter_change_rebuilds_exact_reply_tags_and_resets_unread(self):
        overlay = overlay_with()
        say, party = record(1, 0), record(2, 3)
        overlay.add_message(say, "Text1")
        overlay.add_message(party, "Text2")
        old_tags = set(overlay._reply_records)
        overlay._unseen = 5
        overlay._change("channels", ["party"])
        self.assertEqual(overlay.text.text, line(2))
        self.assertEqual(overlay._unseen, 0)
        self.assertTrue(old_tags.isdisjoint(overlay._reply_records))
        for tag in old_tags:
            overlay.text.tag_delete.assert_any_call(tag)
        party_tag = next(iter(overlay._reply_records))
        self.assertIs(overlay._reply_records[party_tag], party)
        overlay.text.click_tags = ("reply", party_tag)
        overlay._reply_clicked(Mock(x=1, y=1))
        overlay.on_reply.assert_called_once_with(party)
        overlay.on_reply.reset_mock()
        overlay._change("channels", ["say"])
        overlay.text.click_tags = ("reply", party_tag)
        overlay._reply_clicked(Mock(x=1, y=1))
        overlay.on_reply.assert_not_called()
        self.assertEqual(list(overlay._reply_records.values()), [say])
        overlay._change("channels", [])
        self.assertEqual(overlay.text.text, "")
        self.assertFalse(overlay._reply_records)
        self.assertFalse(overlay._marks)
        self.assertEqual(len(overlay.history.messages), 2)

    def test_channels_are_overlay_only_and_main_transcript_keeps_all_messages(self):
        app = headless_app()
        app.overlay = overlay_with(["say", "party"])
        say, trade, party = record(1, 0), record(2, 8), record(3, 3)
        for number, item in enumerate((say, trade, party), 1):
            app._append(item, f"Text{number}", "English", "Live", 0)
        self.assertEqual(app.overlay.text.text, line(1) + "\n" + line(3))
        for number in (1, 2, 3):
            self.assertIn(f"Player{number}: Text{number}", app.transcript.text)
        self.assertEqual(app.completed, 3)
        self.assertEqual(list(app._reply_records.values()), [say, trade, party])


class FilterSettingsTests(unittest.TestCase):
    def test_filter_changes_round_trip_without_changing_log_model_or_saving_chat(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(translator_app, "SETTINGS", Path(folder) / "settings.json"):
            app = headless_app()
            app.config["model"] = "another-local-model:7b"
            app.overlay = overlay_with()
            app.overlay.on_change = app._overlay_changed
            app.overlay.add_message(record(1, 0), "Private message")
            for selection in (["say", "party"], [], list(ALL_CHANNEL_FILTERS)):
                with self.subTest(selection=selection):
                    app.overlay._change("channels", selection)
                    saved = json.loads(translator_app.SETTINGS.read_text(encoding="utf-8"))
                    restored = translator_app.load_settings()
                    self.assertEqual(saved["overlay"]["channels"], selection)
                    self.assertEqual(restored["overlay"]["channels"], selection)
                    self.assertEqual(restored["log_path"], ORIGINS_LOG)
                    self.assertEqual(restored["model"], "another-local-model:7b")
                    self.assertNotIn("Private message", translator_app.SETTINGS.read_text(encoding="utf-8"))

    def test_settings_checkbox_all_none_commands_change_and_sync_actual_preferences(self):
        overlay = overlay_with(["say", "party"])
        widgets = []
        def widget(*_args, **kwargs):
            result = Mock()
            result.options = kwargs
            widgets.append(result)
            return result
        patches = {name: Mock(side_effect=widget) for name in
                   ("Toplevel", "Frame", "Label", "Checkbutton", "Scale", "Button")}
        patches.update(BooleanVar=lambda value: Value(value), IntVar=lambda value: Value(value))
        with patch.multiple(chat_overlay.tk, **patches):
            overlay._build_settings_content(Mock())
        by_label = {item.options.get("text"): item for item in widgets}
        self.assertTrue(by_label["Say"].options["variable"].get())
        self.assertTrue(by_label["Party"].options["variable"].get())
        self.assertFalse(by_label["Trade"].options["variable"].get())
        by_label["None"].options["command"]()
        self.assertEqual(overlay.settings["channels"], [])
        self.assertTrue(all(not variable.get() for variable in overlay._channel_vars.values()))
        by_label["Say"].options["variable"].set(True)
        by_label["Say"].options["command"]()
        by_label["Party"].options["variable"].set(True)
        by_label["Party"].options["command"]()
        self.assertEqual(overlay.settings["channels"], ["say", "party"])
        by_label["All"].options["command"]()
        self.assertEqual(overlay.settings["channels"], list(ALL_CHANNEL_FILTERS))
        self.assertTrue(all(variable.get() for variable in overlay._channel_vars.values()))
        self.assertEqual(overlay.on_change.call_count, 4)


if __name__ == "__main__":
    unittest.main()
