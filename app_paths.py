"""Writable user preferences, kept separate from installed program files."""
from pathlib import Path
import os
import sys


def preferences_directory() -> Path:
    roaming = os.environ.get("APPDATA")
    base = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
    current = base / "Lineage2ChatTranslator"
    legacy = base / "L2Prominence" / "ChatTranslator"
    # Reuse preferences from 1.4.0 without moving user files or losing models.
    if not any((current / name).is_file() for name in ("settings.json", "runtime.json")):
        if any((legacy / name).is_file() for name in ("settings.json", "runtime.json")):
            return legacy
    return current


def settings_file() -> Path:
    if getattr(sys, "frozen", False):
        return preferences_directory() / "settings.json"
    return Path(__file__).resolve().parent / "settings.json"


def runtime_file() -> Path:
    return preferences_directory() / "runtime.json"


def default_data_directory() -> Path:
    return Path.home() / "Lineage2ChatTranslator"
