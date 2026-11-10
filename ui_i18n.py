"""Offline interface translations, independent of the chat translation model.

English source strings are stable catalog keys. Locale changes never send text
to a translation service or change the language requested from the local model.
"""
from __future__ import annotations

import ctypes
import locale
import os
import tkinter as tk
import weakref
from string import Formatter

from ui_strings_installer import CATALOG as INSTALLER_CATALOG
from ui_strings_client import CATALOG as CLIENT_CATALOG
from ui_strings_overlay import CATALOG as OVERLAY_CATALOG
from ui_strings_runtime import CATALOG as RUNTIME_CATALOG

LANGUAGE_NAMES = {
    "en": "English",
    "es": "Español",
    "pt-BR": "Português (Brasil)",
    "ru": "Русский",
    "pl": "Polski",
    "de": "Deutsch",
    "fr": "Français",
    "tr": "Türkçe",
    "ko": "한국어",
    "ja": "日本語",
    "zh-CN": "简体中文",
}
CATALOG: dict[str, dict[str, str]] = {}
for _surface in (INSTALLER_CATALOG, CLIENT_CATALOG, OVERLAY_CATALOG, RUNTIME_CATALOG):
    for _source, _translations in _surface.items():
        CATALOG.setdefault(_source, {}).update(_translations)

_language = "en"
_variables: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_reverse = {translated: source for source, translations in CATALOG.items()
            for translated in translations.values() if translated != source}


def normalize_language(value) -> str:
    """Accept supported BCP 47/Windows codes and native names; fail to English."""
    if not isinstance(value, str):
        return "en"
    if value in LANGUAGE_NAMES.values():
        return next(code for code, name in LANGUAGE_NAMES.items() if name == value)
    code = value.strip().replace("_", "-").split(".", 1)[0].lower()
    # Traditional Chinese is not advertised as a supported interface locale.
    if code in ("zh-tw", "zh-hk", "zh-mo", "zh-hant") or code.startswith("zh-hant-"):
        return "en"
    base = code.split("-", 1)[0]
    if base == "pt":
        return "pt-BR"
    if base == "zh":
        return "zh-CN"
    return base if base in LANGUAGE_NAMES else "en"


def system_language() -> str:
    """Use the Windows interface locale, without opening a window or service."""
    if os.name == "nt":
        try:
            language_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return normalize_language(locale.windows_locale.get(language_id, "en"))
        except (AttributeError, OSError):
            pass
    try:
        return normalize_language(locale.getlocale()[0])
    except (ValueError, TypeError):
        return "en"


def language_choices() -> tuple[str, ...]:
    return tuple(LANGUAGE_NAMES.values())


def language_code(label: str) -> str:
    return normalize_language(label)


def get_language() -> str:
    return _language


def set_language(code: str) -> str:
    global _language
    _language = normalize_language(code)
    return _language


def source_text(text: str) -> str:
    """Recover a static source label when a widget starts in another language."""
    if text in CATALOG:
        return text
    return _reverse.get(text, text)


class LocalizedText(str):
    """Carry a template through worker queues until a status variable receives it."""
    def __new__(cls, rendered, source, values):
        instance = super().__new__(cls, rendered)
        instance.message_source = source
        instance.message_values = dict(values)
        return instance


def tr(text: str, language: str | None = None, **values) -> str:
    code = normalize_language(language) if language is not None else _language
    if isinstance(text, LocalizedText):
        source = text.message_source
        values = dict(text.message_values, **values)
    else:
        source = str(text)
    translated = CATALOG.get(source, {}).get(code, source) if code != "en" else source
    if values:
        rendered_values = {
            key: tr(value.message_source, language=code, **value.message_values)
            if isinstance(value, LocalizedText) else value
            for key, value in values.items()
        }
        try:
            rendered = translated.format(**rendered_values)
        except (KeyError, ValueError, IndexError):
            # A damaged translation must not break startup or an error dialog.
            rendered = source.format(**rendered_values)
    else:
        rendered = translated
    return LocalizedText(rendered, source, values)


class LocalizedStringVar(tk.StringVar):
    """Remember a message template/arguments for immediate language switching."""
    def __init__(self, master=None, value=None, name=None):
        self._message_source = ""
        self._message_values = {}
        super().__init__(master=master, value="", name=name)
        _variables[id(self)] = self
        if value is not None:
            self.set(value)

    def set(self, value):
        if isinstance(value, LocalizedText):
            self.set_message(value.message_source, **value.message_values)
        else:
            self.set_message(source_text(str(value)))

    def set_message(self, text, **values):
        self._message_source = text
        self._message_values = dict(values)
        tk.StringVar.set(self, tr(text, **values))

    def refresh_language(self, language=None):
        tk.StringVar.set(self, tr(self._message_source, language=language, **self._message_values))


def set_message(variable, text: str, **values) -> None:
    """Also support simple test doubles and caller-owned ordinary StringVars."""
    if isinstance(variable, LocalizedStringVar):
        variable.set_message(text, **values)
    else:
        variable.set(tr(text, **values))


def localize_tree(widget, language: str | None = None) -> None:
    """Relabel static Tk controls in place, preserving state, bindings and input.

    Only literal captions are touched. Entries, combobox choices, Text widgets,
    and caller-owned textvariables may contain user/game text and stay intact.
    Captions changed by the application since the previous pass acquire a new
    English source; captions unchanged since that pass reuse their source.
    """
    code = get_language() if language is None else normalize_language(language)
    for variable in list(_variables.values()):
        try:
            variable.refresh_language(language=code)
        except tk.TclError:
            _variables.pop(id(variable), None)

    def visit(current):
        try:
            options = current.keys()
            if "text" in options and not ("textvariable" in options and current.cget("textvariable")):
                caption = str(current.cget("text"))
                previous = getattr(current, "_i18n_caption", None)
                source = previous[0] if previous and caption == previous[1] else source_text(caption)
                translated = tr(source, language=code)
                current.configure(text=translated)
                current._i18n_caption = (source, translated)
            for child in current.winfo_children():
                visit(child)
        except tk.TclError:
            # A transient dialog can close between child enumeration and visit.
            return

    visit(widget)


def catalog_issues() -> list[str]:
    """Check locale completeness and formatting contracts without a display."""
    formatter = Formatter()
    issues = []
    for source, translations in CATALOG.items():
        expected = {field for _, field, _, _ in formatter.parse(source) if field is not None}
        for code in LANGUAGE_NAMES:
            if code == "en":
                continue
            text = translations.get(code)
            if not isinstance(text, str) or not text.strip():
                issues.append(f"{code}: missing {source!r}")
                continue
            try:
                actual = {field for _, field, _, _ in formatter.parse(text) if field is not None}
            except ValueError:
                issues.append(f"{code}: invalid format {source!r}")
                continue
            if actual != expected:
                issues.append(f"{code}: placeholders differ for {source!r}")
    return issues
