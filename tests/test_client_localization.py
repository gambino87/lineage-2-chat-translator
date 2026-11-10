"""Interface changes with a Tcl interpreter only: no windows, workers or models."""
import json
from pathlib import Path
import queue
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import translator_app as client
from reply_composer import REPLY_LANGUAGES
from tests.test_reply_composer import composer, Value
from ui_i18n import LANGUAGE_NAMES, LocalizedStringVar, get_language, set_language, tr


class ClientLanguageTests(unittest.TestCase):
    def setUp(self):
        self.previous_language = get_language()
        set_language("en")
        self.interpreter = tk.Tcl()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "preferences" / "settings.json"

    def tearDown(self):
        set_language(self.previous_language)

    def saved(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def app(self):
        app = client.TranslatorApp.__new__(client.TranslatorApp)
        app.root = Mock()
        app.config = {"ui_language": "en", "target_language": "Russian"}
        app.ui_language = tk.StringVar(master=self.interpreter, value="English")
        app.target = tk.StringVar(master=self.interpreter, value="Russian")
        app.target_display = tk.StringVar(master=self.interpreter, value="Russian")
        app.target_languages = ("English", "Russian", "Spanish", "Portuguese")
        app.target_box = Mock()
        app.settings_panel = Mock()
        app.settings_panel.winfo_ismapped.return_value = False
        app.settings_button = Mock()
        app.read_button = Mock()
        app.overlay_enabled = Mock()
        app.overlay_button = Mock()
        app.status = LocalizedStringVar(master=self.interpreter, value="Watching for new chat…")
        app.counter = LocalizedStringVar(master=self.interpreter)
        app.counter.set_message("{count} translated · {seconds:.1f}s last", count=7, seconds=1.2)
        app.connection = LocalizedStringVar(master=self.interpreter, value="LOCAL · READY")
        app.reading = app.connected = True
        app.overlay = Mock()
        app.overlay.settings = {"enabled": True}
        app.reply = Mock()
        app.updates = Mock()
        app._rows = [("existing", object())]
        app._render_rows = Mock()
        app.jobs = queue.Queue()
        app.jobs.put(object())
        return app

    def test_language_only_save_preserves_unknown_settings_and_existing_chat_target(self):
        original = {"target_language": "Russian", "model": "custom-model", "overlay": {"opacity": 0.6},
                    "future_setting": {"retain": True}}
        self.saved(original)
        client.persist_ui_language("pt_BR", self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {**original, "ui_language": "pt-BR"})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_first_choice_creates_preferences_without_unrelated_defaults(self):
        client.persist_ui_language("ja", self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"ui_language": "ja"})

    def test_broken_preferences_are_not_overwritten_by_language_choice(self):
        self.path.parent.mkdir(parents=True)
        for original in ("{broken", "[1, 2, 3]"):
            with self.subTest(original=original):
                self.path.write_text(original, encoding="utf-8")
                with self.assertRaises(ValueError):
                    client.persist_ui_language("es", self.path)
                self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_change_updates_visible_surfaces_and_preserves_chat_work_and_target(self):
        self.saved({"target_language": "German", "log_path": "saved.log"})
        app = self.app()
        retained = list(app._rows)
        queued = app.jobs.queue[0]
        app.ui_language.set("Español")
        with patch.object(client, "SETTINGS", self.path), patch.object(client, "localize_tree"):
            app._ui_language_changed()
        self.assertEqual(get_language(), "es")
        self.assertEqual(app.config["ui_language"], "es")
        self.assertEqual(app.target.get(), "Russian")
        self.assertEqual(app.target_display.get(), "Ruso")
        self.assertEqual(app.status.get(), tr("Watching for new chat…"))
        self.assertIn("7", app.counter.get())
        self.assertIs(app.jobs.queue[0], queued)
        self.assertEqual(app._rows, retained)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"target_language": "German", "log_path": "saved.log", "ui_language": "es"})
        app.overlay.refresh_language.assert_called_once()
        app.reply.refresh_language.assert_called_once()
        app.updates.refresh_language.assert_called_once()
        app._render_rows.assert_called_once()

    def test_failed_save_keeps_language_usable_and_displays_localized_error(self):
        app = self.app()
        app.ui_language.set("Deutsch")
        with patch.object(client, "persist_ui_language", side_effect=OSError("read only")), \
             patch.object(client, "localize_tree"):
            app._ui_language_changed()
        self.assertEqual(get_language(), "de")
        self.assertEqual(app.status.get(), tr("Could not save preferences. Check that the project folder is writable."))
        self.assertNotEqual(app.status.get(), "Could not save preferences. Check that the project folder is writable.")

    def test_localized_target_picker_retains_canonical_model_language(self):
        app = self.app()
        for code in LANGUAGE_NAMES:
            with self.subTest(code=code):
                set_language(code)
                app.target_display.set(tr("Portuguese"))
                app._target_changed()
                self.assertEqual(app.target.get(), "Portuguese")

    def test_reply_picker_retains_canonical_language_and_draft(self):
        panel = composer()
        panel.language_display = Value()
        panel.draft.set("Can I join?")
        for code in LANGUAGE_NAMES:
            with self.subTest(code=code):
                set_language(code)
                panel.language_display.set(tr("Spanish"))
                panel._language_changed()
                self.assertEqual(panel.language.get(), "Spanish")
                self.assertEqual(panel.draft.get(), "Can I join?")
                self.assertIn(tr("Spanish"), panel.title.get())
                self.assertTrue(panel._jobs.empty())

    def test_saved_language_is_loaded_independently_of_chat_target(self):
        self.saved({"ui_language": "ko", "target_language": "Portuguese"})
        with patch.object(client, "SETTINGS", self.path):
            loaded = client.load_settings()
        self.assertEqual(loaded["ui_language"], "ko")
        self.assertEqual(loaded["target_language"], "Portuguese")


if __name__ == "__main__":
    unittest.main()
