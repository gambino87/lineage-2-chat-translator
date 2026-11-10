"""Headless language-switch checks preserving real chat/filter behavior."""
import ast
from pathlib import Path
from string import Formatter
import unittest
from unittest.mock import Mock, patch

import chat_overlay
import overlay_tray
from chat_overlay import CHANNEL_FILTERS, ChatOverlay, make_overlay_message, message_parts
from overlay_tray import ChannelTray
from translator_core import ChatRecord
from ui_i18n import get_language, set_language, tr
from ui_strings_overlay import CATALOG, LANGUAGES
from test_overlay_channel_filters import overlay_with
from test_overlay_tray import Value, PALETTE


class OverlayCatalogTests(unittest.TestCase):
    def test_every_overlay_string_has_all_locales_and_identical_placeholders(self):
        fields = lambda text: {name for _, name, _, _ in Formatter().parse(text) if name is not None}
        for source, translations in CATALOG.items():
            self.assertEqual(set(translations), set(LANGUAGES), source)
            for code, translated in translations.items():
                self.assertTrue(translated.strip(), (source, code))
                self.assertEqual(fields(source), fields(translated), (source, code))

    def test_literal_control_captions_and_all_channel_labels_are_in_catalog(self):
        sources = {label for _, label, _ in CHANNEL_FILTERS}
        root = Path(chat_overlay.__file__).parent
        for name in ("chat_overlay.py", "overlay_tray.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                method = getattr(node.func, "attr", "")
                if method in ("_label", "_button", "_checkbutton", "_localized_widget"):
                    for option in node.keywords:
                        if option.arg == "text" and isinstance(option.value, ast.Constant):
                            sources.add(option.value.value)
                if method == "_header_button" and node.args and isinstance(node.args[0], ast.Constant):
                    sources.add(node.args[0].value)
        self.assertEqual(sources - {"", "×", "◥"} - CATALOG.keys(), set())


class OverlayLanguageTests(unittest.TestCase):
    def setUp(self):
        self.previous = get_language()
        self.addCleanup(set_language, self.previous)
        set_language("en")

    def test_switch_updates_open_controls_and_preserves_chat_selection_scroll_and_unread(self):
        overlay = overlay_with(["say", "party"])
        overlay.channels_button = Mock()
        settings_caption, static_caption = Mock(), Mock()
        overlay._localized_widgets = [(settings_caption, "Settings"), (static_caption, "Opacity")]
        overlay._settings_window = Mock()
        overlay._tray = Mock(is_open=True)
        source = ChatRecord("10:00:00", "Notice", "Original text", 0, source_language="English")
        overlay.add_message(source, "Actual translated chat")
        overlay.text.view = (.15, .5)
        overlay._unseen = 3
        before_settings = dict(overlay.settings)
        before_messages = tuple(overlay.history.messages)
        before_text = overlay.text.text
        overlay.on_change.reset_mock()
        overlay.text.focus_set.reset_mock()
        overlay.text.focus_force.reset_mock()

        for code in ("es", "ja", "de", "en"):
            with self.subTest(code=code):
                set_language(code)
                overlay.refresh_language()
                settings_caption.configure.assert_called_with(text=tr("Settings"))
                static_caption.configure.assert_called_with(text=tr("Opacity"))
                overlay.caption.configure.assert_called_with(text=tr("Translations") + " · " + tr("filtered"))
                overlay.channels_button.configure.assert_called_with(text=tr("Channels") + " ▾")
                overlay._settings_window.title.assert_called_with(tr("Chat overlay settings"))
                overlay.latest.configure.assert_called_with(text=tr("{count} new · Latest ↓", count=3))
                self.assertEqual(overlay.text.text, before_text)
                self.assertEqual(tuple(overlay.history.messages), before_messages)
                self.assertEqual(overlay.settings, before_settings)
                self.assertEqual(overlay._unseen, 3)
                self.assertEqual(list(overlay._reply_records.values()), [source])
                overlay.text.yview_moveto.assert_called_with(.15)
        self.assertEqual(overlay._tray.refresh_language.call_count, 4)
        overlay.on_change.assert_not_called()
        overlay.text.focus_set.assert_not_called()
        overlay.text.focus_force.assert_not_called()

    def test_error_label_and_placeholder_sender_change_but_real_names_and_chat_do_not(self):
        record = ChatRecord("10:00:00", "Notice", "Leave this unchanged", 0, source_language="English")
        message = make_overlay_message(record, "backend error detail", error=True)
        no_sender = make_overlay_message(ChatRecord("10:00:00", "", "", 0), "")
        set_language("zh-CN")
        parts = message_parts(message, {"show_original": True})
        self.assertEqual(parts[2], "[Notice]: ")
        self.assertEqual(parts[3], tr("Translation unavailable: ") + "backend error detail")
        self.assertEqual(parts[4], "Leave this unchanged")
        self.assertEqual(message_parts(no_sender, {})[2], "[通知]: ")

    def test_switch_after_destroy_does_not_touch_windows(self):
        overlay = overlay_with()
        overlay._destroyed = True
        overlay.refresh_language()
        overlay.window.title.assert_not_called()

    def test_localized_settings_callbacks_keep_protocol_keys_and_original_sources(self):
        overlay = overlay_with(["say"])
        overlay._tray = None
        overlay.channels_button = Mock()
        widgets = []

        def widget(*args, **options):
            result = Mock()
            result.options = options
            widgets.append(result)
            return result

        factories = {name: Mock(side_effect=widget) for name in
                     ("Toplevel", "Frame", "Label", "Checkbutton", "Scale", "Button")}
        factories.update(BooleanVar=lambda value: Value(value=value), IntVar=lambda value: Value(value=value))
        set_language("ru")
        with patch.multiple(chat_overlay.tk, **factories):
            overlay._settings_text_start = 0
            overlay._build_settings_content(Mock())
        overlay._settings_drawer = Mock(is_open=True)
        by_caption = {widget.options.get("text"): widget for widget in widgets}
        self.assertIn(tr("Show overlay"), by_caption)
        self.assertNotIn("locked", overlay._vars)
        self.assertNotIn(tr("Lock overlay and pass mouse clicks through"), by_caption)
        self.assertIn(tr("Command channel"), by_caption)
        all_button = by_caption[tr("All")]
        set_language("fr")
        overlay.refresh_language()
        all_button.configure.assert_called_with(text=tr("All"))
        all_button.options["command"]()
        self.assertEqual(overlay.settings["channels"], [key for key, _, _ in CHANNEL_FILTERS])
        registered = len(overlay._localized_widgets)
        self.assertGreater(registered, 20)
        overlay._close_settings()
        overlay._settings_drawer.close.assert_called_once()
        self.assertEqual(len(overlay._localized_widgets), registered)
        overlay._destroy_settings()
        self.assertEqual(overlay._localized_widgets, [])


class ChannelTrayLanguageTests(unittest.TestCase):
    def test_language_change_reflows_existing_labels_without_reselecting_channels(self):
        previous = get_language()
        self.addCleanup(set_language, previous)
        set_language("en")
        widgets = []

        def widget(*args, **options):
            result = Mock()
            result.options = dict(options)
            result.winfo_reqwidth.side_effect = lambda: len(result.options.get("text", "")) * 7 + 30
            result.winfo_reqheight.return_value = 24
            result.configure.side_effect = lambda **changes: result.options.update(changes)
            widgets.append(result)
            return result

        window = widget()
        change = Mock()
        options = (("say", "Say"), ("command", "Command channel"), ("other", "Other / notices"))
        with patch.multiple(overlay_tray.tk, Toplevel=Mock(return_value=window),
            Frame=Mock(side_effect=widget), Label=Mock(side_effect=widget),
            Button=Mock(side_effect=widget), Checkbutton=Mock(side_effect=widget), BooleanVar=Value):
            tray = ChannelTray(Mock(), options=options, selected=["command"], on_change=change,
                get_anchor=lambda: (10, 300, 300, 230), get_bounds=lambda: (0, 0, 1920, 1080),
                native_factory=lambda _: Mock(), palette=PALETTE, opacity=.8, on_open_change=Mock())
        tray._shown = True
        tray._progress = .5
        tray._open = True
        tray._layout(300)
        original_grid = tray._choices[2].grid.call_args.kwargs
        selected_before = {key: var.get() for key, var in tray._variables.items()}
        set_language("fr")
        tray.refresh_language()
        self.assertEqual(tray._choices[1].options["text"], "Canal de commandement")
        self.assertNotEqual(tray._choices[2].grid.call_args.kwargs, original_grid)
        self.assertEqual({key: var.get() for key, var in tray._variables.items()}, selected_before)
        self.assertEqual(tray._progress, .5)
        self.assertTrue(tray.is_open)
        change.assert_not_called()
        window.focus_set.assert_not_called()
        window.focus_force.assert_not_called()


if __name__ == "__main__":
    unittest.main()
