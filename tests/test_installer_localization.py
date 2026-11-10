"""Installer locale handoff, persistence and offline resource contracts."""
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import installer_main as setup
from installer_runtime import DEFAULT_MODEL, MODEL_CATALOG
import ui_i18n as ui
from ui_strings_installer import CATALOG, INNO_MESSAGES, SETUP_INFORMATION


ROOT = Path(__file__).resolve().parents[1]
INNO_NAMES = {"pt-BR": "ptbr", "zh-CN": "zhcn"}


class InstallerLocalizationTests(unittest.TestCase):
    def setUp(self):
        ui.set_language("en")
        self.addCleanup(ui.set_language, "en")
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Path(self.folder) / "settings.json"
        self.enterContext(patch.object(setup, "SETTINGS_PATH", self.settings))
        self.old = {"target_language": "Portuguese", "model": DEFAULT_MODEL,
                    "log_path": "D:/Game/system/Network.log", "overlay": {"locked": True}}
        setup.write_json(self.settings, self.old)

    def test_language_choice_changes_only_interface_preference(self):
        for code in ui.LANGUAGE_NAMES:
            self.assertEqual(setup.save_ui_language(code), code)
            saved = setup.read_json(self.settings)
            self.assertEqual(saved.pop("ui_language"), code)
            self.assertEqual(saved, self.old)

    def test_corrupt_preferences_are_never_replaced_to_save_language(self):
        self.settings.write_text("{corrupt", encoding="utf-8")
        with self.assertRaises(ValueError):
            setup.save_ui_language("ja")
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{corrupt")

    def test_installer_language_is_saved_before_model_setup_window(self):
        with patch.object(setup, "SingleInstance") as lock, \
             patch.object(setup, "uninstall_in_progress", return_value=False), \
             patch.object(setup, "update_in_progress", return_value=False), \
             patch.object(setup, "cleanup_update_cache"), \
             patch.object(setup, "consume_update_result", return_value=None), \
             patch.object(setup, "apply_window_icon"), \
             patch.object(setup.tk, "Tk"), patch.object(setup, "SetupWindow") as window:
            lock.return_value.acquire.return_value = True
            def configured(*args, **kwargs):
                self.assertEqual(ui.get_language(), "ja")
                self.assertEqual(setup.read_json(self.settings)["ui_language"], "ja")
                return Mock(setup_completed=True)
            window.side_effect = configured
            self.assertEqual(setup.main(["--install-model", "--ui-language", "ja"]), 0)
        saved = setup.read_json(self.settings)
        self.assertEqual(saved["target_language"], self.old["target_language"])
        self.assertEqual(saved["overlay"], self.old["overlay"])

    def test_switching_setup_language_keeps_model_license_consent_and_inputs(self):
        interpreter = tk.Tcl()
        window = setup.SetupWindow.__new__(setup.SetupWindow)
        window.root = Mock()
        window.settings = dict(self.old)
        window.model_tags = list(MODEL_CATALOG)
        window.labels = [MODEL_CATALOG[tag]["label"] for tag in window.model_tags]
        window.model = tk.StringVar(master=interpreter, value=window.labels[0])
        window.model_box = Mock()
        window.ui_language = tk.StringVar(master=interpreter, value=ui.LANGUAGE_NAMES["ja"])
        window.accepted = tk.BooleanVar(master=interpreter, value=True)
        window.detail = ui.LocalizedStringVar(master=interpreter)
        window.model_note = ui.LocalizedStringVar(master=interpreter)
        window.status = ui.LocalizedStringVar(master=interpreter, value="Checking your local translation service…")
        window._language_variables = [window.detail, window.model_note, window.status]
        with patch.object(setup, "localize_tree"):
            window._ui_language_changed()
        self.assertEqual(window.selected_tag(), window.model_tags[0])
        self.assertTrue(window.accepted.get())
        self.assertEqual(window.settings["target_language"], "Portuguese")
        self.assertEqual(window.status.get(), ui.tr("Checking your local translation service…"))
        self.assertNotEqual(window.model.get(), MODEL_CATALOG[window.model_tags[0]]["label"])
        self.assertEqual(setup.read_json(self.settings)["ui_language"], "ja")

    def test_failed_language_save_preserves_current_selection_and_language(self):
        window = setup.SetupWindow.__new__(setup.SetupWindow)
        window.root = Mock()
        window.ui_language = Mock(get=Mock(return_value=ui.LANGUAGE_NAMES["ja"]))
        window.selected_tag = Mock(return_value=DEFAULT_MODEL)
        window.settings = dict(self.old)
        with patch.object(setup, "save_ui_language", side_effect=PermissionError("read only")), \
             patch.object(setup.messagebox, "showerror") as error:
            window._ui_language_changed()
        self.assertEqual(ui.get_language(), "en")
        window.ui_language.set.assert_called_once_with("English")
        self.assertNotIn("ui_language", window.settings)
        error.assert_called_once()

    def test_worker_progress_retains_template_for_switching_after_delivery(self):
        window = setup.SetupWindow.__new__(setup.SetupWindow)
        window.events = Mock()
        message = ui.tr("Removed {tag}.", tag="test-model")
        window._report(message)
        delivered = window.events.put.call_args.args[0][1]
        self.assertIs(delivered, message)
        interpreter = tk.Tcl()
        variable = ui.LocalizedStringVar(master=interpreter, value=delivered)
        ui.set_language("ja")
        variable.refresh_language()
        self.assertEqual(variable.get(), ui.tr("Removed {tag}.", tag="test-model"))

    def test_inno_includes_every_locale_and_hands_off_canonical_language(self):
        script = (ROOT / "packaging/installer.iss").read_text(encoding="utf-8-sig")
        section = script.split("[Languages]", 1)[1].split("[Tasks]", 1)[0]
        self.assertIn("ShowLanguageDialog=yes", script)
        for code in ui.LANGUAGE_NAMES:
            self.assertIn(f'Name: "{INNO_NAMES.get(code, code)}";', section)
            self.assertIn(f'InfoBeforeFile: "languages\\info-{code}.txt"', section)
        self.assertIn("'--install-model --ui-language ' + CustomMessage('UiLanguageCode')", script)
        self.assertIn("'--uninstall-cleanup --ui-language ' + CustomMessage('UiLanguageCode')", script)
        # Launching later must honor the current saved choice, not an old shortcut argument.
        icons = script.split("[Icons]", 1)[1].split("[Code]", 1)[0]
        self.assertNotIn("--ui-language", icons)

    def test_compiled_locale_inputs_match_reviewed_catalog(self):
        resource = (ROOT / "packaging/languages/CustomMessages.isl").read_text(encoding="utf-8-sig")
        for code in ui.LANGUAGE_NAMES:
            prefix = INNO_NAMES.get(code, code)
            self.assertIn(f"{prefix}.UiLanguageCode={code}\n", resource)
            for key, source in INNO_MESSAGES.items():
                translated = source if code == "en" else CATALOG[source][code]
                self.assertIn(f"{prefix}.{key}=" + translated.replace("\n", "%n") + "\n", resource)
            info = (ROOT / f"packaging/languages/info-{code}.txt").read_text(encoding="utf-8-sig")
            expected = SETUP_INFORMATION if code == "en" else CATALOG[SETUP_INFORMATION][code]
            self.assertEqual(info, "LINEAGE 2 CHAT TRANSLATOR\n\n" + expected + "\n")
        chinese = (ROOT / "packaging/languages/ChineseSimplified.isl").read_text(encoding="utf-8-sig")
        self.assertIn("LanguageName=简体中文", chinese)
        self.assertIn("LanguageID=$0804", chinese)


if __name__ == "__main__":
    unittest.main()
