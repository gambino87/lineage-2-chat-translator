"""Headless checks for offline language switching and shipped translations."""
import ast
from pathlib import Path
import tkinter as tk
import unittest

import ui_i18n as ui


class Widget:
    def __init__(self, text="", children=(), variable=""):
        self.options = {"text": text, "textvariable": variable}
        self.children = children

    def keys(self):
        return self.options.keys()

    def cget(self, option):
        return self.options[option]

    def configure(self, **options):
        self.options.update(options)

    def winfo_children(self):
        return self.children


class InterfaceLanguageTests(unittest.TestCase):
    def setUp(self):
        ui.set_language("en")
        self.addCleanup(ui.set_language, "en")

    def test_all_languages_use_native_names_and_accept_windows_locale_codes(self):
        self.assertEqual(len(ui.LANGUAGE_NAMES), 11)
        for code, label in ui.LANGUAGE_NAMES.items():
            self.assertEqual(ui.language_code(label), code)
            self.assertEqual(ui.normalize_language(code), code)
        self.assertEqual(ui.normalize_language("pt_BR"), "pt-BR")
        self.assertEqual(ui.normalize_language("es_MX"), "es")
        self.assertEqual(ui.normalize_language("zh_Hans_CN"), "zh-CN")
        self.assertEqual(ui.normalize_language("zh_TW"), "en")
        self.assertEqual(ui.normalize_language(None), "en")
        self.assertEqual(ui.normalize_language("unknown"), "en")

    def test_every_catalog_entry_has_every_language_and_matching_placeholders(self):
        self.assertGreater(len(ui.CATALOG), 50)
        self.assertEqual(ui.catalog_issues(), [])
        for source, translations in ui.CATALOG.items():
            for code in ui.LANGUAGE_NAMES:
                self.assertEqual(ui.tr(source, language=code),
                                 source if code == "en" else translations[code])

    def test_literal_translation_calls_are_in_catalog(self):
        missing = []
        root = Path(__file__).resolve().parents[1]
        for name in ("installer_main.py", "installer_runtime.py", "translator_app.py", "reply_composer.py",
                     "chat_overlay.py", "overlay_tray.py", "update_controls.py", "tray_controls.py", "windows_tray.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                index = 1 if node.func.id == "set_message" else 0
                if node.func.id not in ("tr", "set_message") or len(node.args) <= index:
                    continue
                value = node.args[index]
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                    if value.value == "Lineage 2 Chat Translator":
                        continue  # Product identity is the same in every locale.
                    if value.value not in ui.CATALOG:
                        missing.append(f"{name}:{node.lineno}: {value.value}")
        self.assertEqual(missing, [])

    def test_unknown_strings_and_game_content_remain_unchanged(self):
        for code in ui.LANGUAGE_NAMES:
            ui.set_language(code)
            self.assertEqual(ui.tr("SamplePlayer WTS [Sword] 10kk"), "SamplePlayer WTS [Sword] 10kk")
            self.assertEqual(ui.tr("Unknown {name}", name="Player"), "Unknown Player")

    def test_switching_static_widgets_preserves_source_and_dynamic_state(self):
        variable_label = Widget("Player-authored text", variable="reply_draft")
        button = Widget("Settings")
        parent = Widget(children=(button, variable_label))
        for code in ("ja", "ru", "de", "en", "es", "en"):
            ui.set_language(code)
            ui.localize_tree(parent)
            self.assertEqual(button.cget("text"), ui.tr("Settings"))
            self.assertEqual(variable_label.cget("text"), "Player-authored text")
        button.configure(text="Done")
        ui.set_language("fr")
        ui.localize_tree(parent)
        self.assertEqual(button.cget("text"), ui.tr("Done"))
        ui.set_language("en")
        ui.localize_tree(parent)
        self.assertEqual(button.cget("text"), "Done")

    def test_message_variable_switches_templates_without_creating_windows(self):
        interpreter = tk.Tcl()
        variable = ui.LocalizedStringVar(master=interpreter, value="Settings")
        for code in ("es", "ko", "en"):
            ui.set_language(code)
            ui.localize_tree(Widget())
            self.assertEqual(variable.get(), ui.tr("Settings"))
        ui.set_message(variable, "Player {name}: {count}", name="Sample", count=3)
        ui.set_language("de")
        ui.localize_tree(Widget())
        self.assertEqual(variable.get(), "Player Sample: 3")
        variable.set(ui.tr("{count} new · Latest ↓", count=7))
        ui.set_language("ja")
        ui.localize_tree(Widget())
        self.assertEqual(variable.get(), ui.tr("{count} new · Latest ↓", count=7))
        variable.set_message("Nested {caption}", caption=ui.tr("Settings"))
        ui.set_language("es")
        ui.localize_tree(Widget())
        self.assertEqual(variable.get(), "Nested " + ui.tr("Settings"))


if __name__ == "__main__":
    unittest.main()
