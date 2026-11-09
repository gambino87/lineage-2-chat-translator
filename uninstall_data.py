"""Track and remove only this application's downloaded, per-user data.

The caller must hold the setup/translator single-instance lock. This module
never starts Ollama and never deletes an arbitrary selected parent directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

from installer_runtime import OLLAMA_VERSION, RuntimeManager, ServiceOwnershipMismatch, SetupError


APP_ID = "Lineage2ChatTranslator"
REGISTRY = "installations.json"
MARKER = ".l2-translator-install.json"
CHILDREN = ("ollama", "models", "logs")
OWNERSHIP_SCHEMA_VERSION = 1
SERVICE_IDENTIFICATION_SECONDS = 5
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# tempfile.mkdtemp/mkstemp append eight random characters. Only these owned
# naming patterns may be swept; arbitrary sibling folders remain untouched.
_STAGE = re.compile(r"\.ollama-stage-[a-z0-9_]{8}\Z")
_ARCHIVE = re.compile(r"ollama-windows-amd64-[0-9]+\.[0-9]+\.[0-9]+\.zip\Z")
_MARKER_TEMP = re.compile(r"\.l2-translator-install-[a-z0-9_]{8}\.tmp\Z")
_PREF_TEMP = re.compile(r"(?:settings|runtime|installations)-[a-z0-9_]{8}\.tmp\Z")


def _plain_path(path: str | Path) -> Path:
    """Resolve an absolute cleanup path only after rejecting linked ancestors."""
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise SetupError("The saved cleanup location must be an absolute path.")
    value = Path(os.path.abspath(value))
    # Check lexical ancestors before resolve() can hide a junction or symlink.
    for part in reversed((value, *value.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
            raise SetupError(f"Cleanup will not follow a linked or redirected folder: {part}")
    return value.resolve()


def _read_json(path: Path) -> dict:
    path = _plain_path(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"Cannot read the cleanup record: {path}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Invalid cleanup record: {path}")
    return value


def _write_json(path: Path, value: dict) -> None:
    _plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _preferences(path: Path) -> Path:
    result = _plain_path(path)
    if not (result.name.casefold() == "lineage2chattranslator"
            or (result.name.casefold() == "chattranslator" and result.parent.name.casefold() == "l2prominence")):
        raise SetupError("The cleanup preferences folder is not this application's folder.")
    if result == Path(result.anchor) or result == Path.home().resolve():
        raise SetupError("The cleanup preferences location is unsafe.")
    if result.exists() and not result.is_dir():
        raise SetupError("The cleanup preferences location is not a folder.")
    return result


def _preference_family(preferences: Path) -> tuple[Path, Path]:
    base = preferences.parent if preferences.name.casefold() == "lineage2chattranslator" else preferences.parent.parent
    return (base / "Lineage2ChatTranslator", base / "L2Prominence" / "ChatTranslator")


def _disjoint_roots(roots: list[Path]) -> None:
    for index, root in enumerate(roots):
        for other in roots[index + 1:]:
            if root != other and (root.is_relative_to(other) or other.is_relative_to(root)):
                raise SetupError("Recorded model-data folders overlap. Choose separate data folders before continuing.")


def _data_root(path: Path, preferences: Path) -> Path:
    root = _plain_path(path)
    home = Path.home().resolve()
    if (root == Path(root.anchor) or root == home or root in home.parents
            or any(root.is_relative_to(pref) or pref.is_relative_to(root) for pref in _preference_family(preferences))):
        raise SetupError("Choose a dedicated model-data folder for this application.")
    forbidden = [Path(os.environ[key]).resolve() for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)") if os.environ.get(key)]
    if getattr(sys, "frozen", False):
        forbidden.append(Path(sys.executable).resolve().parent)
    for location in forbidden:
        if root.is_relative_to(location) or location.is_relative_to(root):
            raise SetupError("The model-data location overlaps a system or application folder.")
    if root.exists() and not root.is_dir():
        raise SetupError("The model-data location is not a folder.")
    for candidate in (root, root / "system"):
        if any((candidate / name).is_file() for name in ("L2.exe", "L2.bin", "L2.ini", "Engine.dll")):
            raise SetupError("Choose a model-data folder separate from the Lineage II game files.")
    return root


def _tree_check(target: Path, parent: Path) -> None:
    """Preflight one direct owned child and reject links anywhere below it."""
    absolute = _plain_path(target)
    if absolute.parent != parent or not absolute.is_relative_to(parent):
        raise SetupError("A cleanup target escapes its recorded data folder.")
    if absolute.is_dir():
        for folder, directories, files in os.walk(absolute, followlinks=False):
            for name in directories + files:
                child = _plain_path(Path(folder) / name)
                if not child.is_relative_to(absolute):
                    raise SetupError("A cleanup target escapes its recorded folder.")


def _registry(preferences: Path) -> list[Path]:
    """Load registered stores without permitting system, game or overlapping roots."""
    data = _read_json(preferences / REGISTRY)
    if not (preferences / REGISTRY).exists():
        return []
    values = data.get("data_roots")
    if data.get("app_id") != APP_ID or data.get("schema_version") != OWNERSHIP_SCHEMA_VERSION or not isinstance(values, list):
        raise SetupError("The model-data cleanup registry is invalid.")
    roots = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SetupError("The model-data cleanup registry contains an invalid folder.")
        root = _data_root(Path(value), preferences)
        if root not in roots:
            roots.append(root)
    _disjoint_roots(roots)
    return roots


def _marker(root: Path, preferences: Path) -> dict:
    """Require matching ownership before any registered store can be removed."""
    data = _read_json(root / MARKER)
    if not (root / MARKER).exists():
        return {}
    archives = data.get("archives")
    if (data.get("app_id") != APP_ID or data.get("schema_version") != OWNERSHIP_SCHEMA_VERSION
            or data.get("data_root") != str(root)
            or data.get("preferences_dir") not in [str(path) for path in _preference_family(preferences)]
            or not isinstance(archives, list) or not all(isinstance(name, str) and _ARCHIVE.fullmatch(name) for name in archives)):
        raise SetupError(f"The model-data ownership record is invalid: {root}")
    return data


def _targets(root: Path, marker: dict) -> list[Path]:
    names = set(CHILDREN)
    for archive in marker["archives"]:
        names.update((archive, archive + ".part"))
    if root.exists():
        names.update(entry.name for entry in root.iterdir()
                     if _STAGE.fullmatch(entry.name) or _MARKER_TEMP.fullmatch(entry.name))
    return [root / name for name in sorted(names)]


def _remaining_payload(root: Path) -> bool:
    """Recognize an interrupted cleanup after its marker was removed."""
    for entry in root.iterdir():
        name = entry.name
        if (name in CHILDREN or _STAGE.fullmatch(name) or _MARKER_TEMP.fullmatch(name)
                or _ARCHIVE.fullmatch(name) or (name.endswith(".part") and _ARCHIVE.fullmatch(name[:-5]))):
            return True
    return False


def _remove_empty_folder(path: Path) -> None:
    if path.exists() and not any(path.iterdir()):
        path.rmdir()


def _current_root(preferences: Path) -> Path | None:
    value = _read_json(preferences / "runtime.json").get("data_root")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SetupError("The saved model-data location is invalid.")
    return _data_root(Path(value), preferences)


def register_data_root(data_root: Path, preferences_dir: Path) -> None:
    """Reserve known app data names before any runtime/model download starts."""
    preferences = _preferences(preferences_dir)
    root = _data_root(data_root, preferences)
    roots = _registry(preferences)
    related_roots = list(roots)
    for related in _preference_family(preferences):
        if related.exists():
            related = _preferences(related)
            related_roots.extend(_registry(related))
            legacy_root = _current_root(related)
            if legacy_root is not None:
                related_roots.append(legacy_root)
    _disjoint_roots(list(dict.fromkeys([*related_roots, root])))
    marker = _marker(root, preferences)
    previous_marker = json.loads(json.dumps(marker)) if marker else None
    root_existed = root.exists()
    preferences_existed = preferences.exists()
    archive = f"ollama-windows-amd64-{OLLAMA_VERSION}.zip"
    if not marker:
        # Earlier releases already used this saved dedicated store. A verified
        # executable receipt is required before adopting its existing children.
        legacy = _current_root(preferences) == root and RuntimeManager(root)._verified_runtime()
        marker = {"app_id": APP_ID, "schema_version": OWNERSHIP_SCHEMA_VERSION, "data_root": str(root),
                  "preferences_dir": str(preferences), "archives": [archive]}
        existing = [path for path in _targets(root, marker) if path.exists() or path.is_symlink()]
        if existing and not legacy:
            raise SetupError("The selected folder already contains untracked runtime, model or log files. Choose an empty dedicated data folder.")
    elif archive not in marker["archives"]:
        marker["archives"].append(archive)
    for target in _targets(root, marker):
        _tree_check(target, root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        _write_json(root / MARKER, marker)
        if root not in roots:
            roots.append(root)
        _write_json(preferences / REGISTRY, {"app_id": APP_ID, "schema_version": OWNERSHIP_SCHEMA_VERSION,
                                           "data_roots": [str(path) for path in roots]})
    except OSError as exc:
        try:
            if previous_marker is not None:
                _write_json(root / MARKER, previous_marker)
            else:
                (root / MARKER).unlink(missing_ok=True)
            if not root_existed:
                _remove_empty_folder(root)
            if not preferences_existed:
                _remove_empty_folder(preferences)
        except OSError as rollback_error:
            raise SetupError(f"Could not save or roll back the model-data cleanup record at {root}. No downloads were started.") from rollback_error
        raise SetupError("Could not save the model-data cleanup record. No downloads were started.") from exc


def _preference_targets(preferences: Path) -> list[Path]:
    names = {"settings.json", "runtime.json", "update-result.json"}
    if preferences.exists():
        names.update(path.name for path in preferences.iterdir() if _PREF_TEMP.fullmatch(path.name))
    targets = [preferences / name for name in sorted(names)]
    for target in targets + [preferences / REGISTRY]:
        _tree_check(target, preferences)
        if target.exists() and not target.is_file():
            raise SetupError("A saved preferences file has become a folder; cleanup stopped.")
    return targets


def _stop_owned_service(roots: list[Path]) -> bool:
    """Stop a positively identified owned server; uncertain ownership blocks cleanup."""
    if not roots or not RuntimeManager(roots[0])._port_open():
        return False
    errors = []
    for root in roots:
        manager = RuntimeManager(root)
        try:
            manager._verify_owner(timeout=SERVICE_IDENTIFICATION_SECONDS)
        except ServiceOwnershipMismatch:
            # This root does not own the identified listener; another registered
            # root may. Display wording must never decide cleanup behavior.
            continue
        except SetupError as exc:
            errors.append(exc)
            continue
        manager.stop_service()
        return True
    # A verified executable mismatch is safe to leave running. Failed Windows
    # verification is not evidence of an unrelated service, so fail closed.
    if errors:
        raise SetupError("Could not identify the running local service. Close it and retry uninstall; no data was deleted.") from errors[0]
    return False


def uninstall_data(preferences_dir: Path) -> dict:
    """Stop the app's service and remove its registered data, preserving siblings."""
    from app_updater import cleanup_update_cache, update_in_progress
    if update_in_progress():
        raise SetupError("An update is in progress. Wait for it to finish before uninstalling.")
    preferences = _preferences(preferences_dir)
    preference_folders = [preferences]
    for related in _preference_family(preferences):
        if related != preferences and related.exists():
            preference_folders.append(_preferences(related))
    roots = []
    for folder in preference_folders:
        recorded = _registry(folder)
        current = _current_root(folder)
        if current is not None and current not in recorded:
            if not current.exists():
                recorded.append(current)
            else:
                # Persist legacy ownership before removal so interrupted cleanup
                # is repeatable after its executable receipt has been removed.
                register_data_root(current, folder)
                recorded = _registry(folder)
        roots.extend(recorded)
    roots = list(dict.fromkeys(roots))
    _disjoint_roots(roots)
    plans = []
    for root in roots:
        if not root.exists():
            continue
        marker = _marker(root, preferences)
        if not marker:
            if _remaining_payload(root):
                raise SetupError(f"The saved model-data folder has no ownership record: {root}")
            # A previous attempt may have removed the marker before a final
            # preference write failed. No reserved data remains to delete.
            plans.append((root, []))
            continue
        targets = _targets(root, marker)
        for target in targets:
            _tree_check(target, root)
        _tree_check(root / MARKER, root)
        plans.append((root, targets))
    preference_targets = [(folder, target) for folder in preference_folders for target in _preference_targets(folder)]
    stopped = _stop_owned_service(roots)
    removed = []
    try:
        for folder in preference_folders:
            cleanup_update_cache(folder, strict=True)
        for root, targets in plans:
            # Recheck after service exit, then remove only the already-scoped
            # names. Marker/registry remain until all payload removal succeeds.
            for target in targets:
                _tree_check(target, root)
                if target.is_dir():
                    shutil.rmtree(target)
                    removed.append(str(target))
                elif target.exists():
                    target.unlink()
                    removed.append(str(target))
        for root, _ in plans:
            _tree_check(root / MARKER, root)
        for folder, target in preference_targets:
            _tree_check(target, folder)
        # Keep the registry if any earlier payload operation failed.
        for _, target in preference_targets:
            target.unlink(missing_ok=True)
        for root, _ in plans:
            (root / MARKER).unlink(missing_ok=True)
            _remove_empty_folder(root)  # Unrelated files are never removed.
        for folder in preference_folders:
            (folder / REGISTRY).unlink(missing_ok=True)
            _remove_empty_folder(folder)
    except OSError as exc:
        raise SetupError(f"Some application data could not be removed. Close files in use and retry uninstall. {exc}") from exc
    return {"ok": True, "service_stopped": stopped, "data_roots": [str(root) for root in roots],
            "removed_paths": removed, "preferences_removed": True,
            "preference_folders": [str(folder) for folder in preference_folders]}
