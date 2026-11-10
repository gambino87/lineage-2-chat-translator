"""Track and remove only this application's downloaded, per-user data.

The caller must hold the setup/translator single-instance lock. This module
never starts Ollama and never deletes an arbitrary selected parent directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import base64
from datetime import datetime, timezone
from contextlib import ExitStack, nullcontext

from installer_runtime import OLLAMA_VERSION, RuntimeManager, ServiceOwnershipMismatch, SetupError
from secure_storage import (StorageError, guard_private_tree, locked_directory, locked_file,
                            path_identity, private_directory, safe_remove_tree, seal_record, verify_record)


APP_ID = "Lineage2ChatTranslator"
REGISTRY = "installations.json"
MARKER = ".l2-translator-install.json"
CHILDREN = ("ollama", "models", "logs", "service-home")
OWNERSHIP_SCHEMA_VERSION = 2
UNCONFIRMED_RECORDS = "unconfirmed-storage-records.json"
REVIEW_STORAGE = "These older storage ownership records need your review. Open Configure, review the exact selected data folder, and confirm adoption before continuing. No data was deleted."
_CONFIRMATION_UNSET = object()
SERVICE_IDENTIFICATION_SECONDS = 5
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_CONTROL_RECORD_BYTES = 256 * 1024
MAX_HISTORY_BYTES = 4 * 1024 * 1024
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
        with path.open("rb") as source:
            content = source.read(MAX_HISTORY_BYTES + 1)
        if len(content) > MAX_HISTORY_BYTES:
            raise SetupError("The historical storage record is too large; preserve it for manual review.")
        value = json.loads(content.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"Cannot read the cleanup record: {path}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Invalid cleanup record: {path}")
    return value


def _read_control_json(path: Path) -> dict:
    """Read an owned private control record without promoting legacy pointers.

    Holding the record and every ancestor prevents a write/rename while its
    provenance is checked. Runtime bytes cannot prove ownership of sibling data.
    """
    path = _plain_path(path)
    if not path.exists():
        return {}
    if os.name != "nt":
        return _read_json(path)
    import msvcrt
    from secure_storage import _Windows
    try:
        with locked_file(path) as source:
            api = _Windows()
            private = api.private(api.security(msvcrt.get_osfhandle(source.fileno())), False)
            content = source.read(MAX_CONTROL_RECORD_BYTES + 1)
            if len(content) > MAX_CONTROL_RECORD_BYTES:
                raise SetupError("The cleanup ownership record is unexpectedly large.")
            value = json.loads(content.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise SetupError(f"Invalid cleanup record: {path}")
            if not private or not private_directory(path.parent):
                raise SetupError(REVIEW_STORAGE)
            return value
    except (OSError, UnicodeError, ValueError) as exc:
        raise SetupError(f"Cannot safely read the cleanup ownership record: {path}") from exc


def _write_json(path: Path, value: dict) -> None:
    _plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name in (MARKER, REGISTRY) and not verify_record(value):
        value = seal_record(value)
    content = json.dumps(value, indent=2) + "\n"
    limit = MAX_HISTORY_BYTES if path.name == UNCONFIRMED_RECORDS else MAX_CONTROL_RECORD_BYTES
    if len(content.replace("\n", os.linesep).encode("utf-8")) > limit:
        raise SetupError("The storage record is too large to save safely; existing records were preserved.")
    fd, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
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
    data = _read_control_json(preferences / REGISTRY)
    if not (preferences / REGISTRY).exists():
        return []
    values = data.get("data_roots")
    if data.get("schema_version") != OWNERSHIP_SCHEMA_VERSION or not verify_record(data):
        raise SetupError(REVIEW_STORAGE)
    if data.get("app_id") != APP_ID or not isinstance(values, list):
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
    data = _read_control_json(root / MARKER)
    if not (root / MARKER).exists():
        return {}
    if (data.get("schema_version") != OWNERSHIP_SCHEMA_VERSION or not verify_record(data)
            or data.get("root_identity") != path_identity(root)):
        raise SetupError("The model-data ownership record changed or needs review. " + REVIEW_STORAGE)
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
    value = _read_control_json(preferences / "runtime.json").get("data_root")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SetupError("The saved model-data location is invalid.")
    root = _data_root(Path(value), preferences)
    if root.exists() and root not in _registry(preferences):
        raise SetupError(REVIEW_STORAGE)
    return root


def _unconfirmed_preferences(preferences: Path) -> set[Path]:
    """Only a sealed owner-approved registry may exclude historical pointers."""
    value = _read_control_json(preferences / REGISTRY)
    if not value or not verify_record(value):
        return set()
    names = value.get("unconfirmed_preferences", [])
    family = set(_preference_family(preferences)) - {preferences}
    return {Path(name) for name in names if isinstance(name, str) and Path(name) in family} if isinstance(names, list) else set()


def authorize_data_root(data_root: Path, preferences_dir: Path | None = None) -> bool:
    """Validate sealed ownership before repairing an already adopted app tree.

    Called by the storage guard while all existing paths are pinned. Unrelated
    siblings must retain their ACLs, so mixed trees are never permission-repaired.
    This record grants cleanup scope; executable authenticity is checked against
    the bundled runtime manifest separately, never inferred from this marker.
    """
    if preferences_dir is None:
        from app_paths import preferences_directory
        preferences_dir = preferences_directory()
    preferences = _preferences(preferences_dir)
    root = _data_root(data_root, preferences)
    marker = _marker(root, preferences)
    if not marker:
        return False
    # A prior release may have registered the same owned store under the legacy
    # preferences directory. Both exact app directories were accepted by _marker.
    recorded = any(root in _registry(folder) for folder in
                   dict.fromkeys((preferences, *_preference_family(preferences))))
    if not recorded:
        return False
    allowed = {MARKER, *(target.name for target in _targets(root, marker))}
    return all(entry.name in allowed for entry in root.iterdir())


def _authorize_preferences(root: Path) -> bool:
    """Repair only known app preferences whose control records are trustworthy."""
    allowed = {"settings.json", "runtime.json", REGISTRY, "update-result.json", "updates", UNCONFIRMED_RECORDS,
               "installed-files.json", "installed-release.json", "installed-release.sig"}
    if any(entry.name not in allowed and not _PREF_TEMP.fullmatch(entry.name) for entry in root.iterdir()):
        return False
    _registry(root)
    return True


def storage_requires_confirmation(data_root: Path, preferences_dir: Path) -> bool:
    """Read-only: decide whether this exact chosen folder needs owner adoption."""
    preferences = _preferences(preferences_dir)
    root = _data_root(data_root, preferences)
    registered = []
    try:
        ignored = _unconfirmed_preferences(preferences)
    except SetupError:
        return True
    for folder in dict.fromkeys((preferences, *_preference_family(preferences))):
        if folder in ignored:
            continue
        try:
            registered.extend(_registry(folder))
        except SetupError:
            return True
    if not root.exists() or not any(root.iterdir()):
        return False
    try:
        return not (_marker(root, preferences) and root in registered)
    except SetupError:
        return True


def storage_confirmation_identity(data_root: Path, preferences_dir: Path) -> list[int] | None:
    """Capture before the prompt; worker checks this identity before changing ACLs."""
    root = _data_root(data_root, _preferences(preferences_dir))
    if not root.exists():
        return None
    return path_identity(root)


def _selected_names_only(root: Path) -> bool:
    for entry in root.iterdir():
        name = entry.name
        if (name not in (*CHILDREN, MARKER) and not _STAGE.fullmatch(name)
                and not _MARKER_TEMP.fullmatch(name) and not _ARCHIVE.fullmatch(name)
                and not (name.endswith(".part") and _ARCHIVE.fullmatch(name[:-5]))):
            return False
    return True


def _snapshot_records(preferences: Path, root: Path) -> list[dict]:
    records = []
    paths = [root / MARKER]
    for folder in dict.fromkeys((preferences, *_preference_family(preferences))):
        paths.extend((folder / REGISTRY, folder / "runtime.json"))
    for path in paths:
        if path.exists():
            _plain_path(path)
            try:
                reader = locked_file(path) if os.name == "nt" else path.open("rb")
                with reader as source:
                    value = source.read(MAX_CONTROL_RECORD_BYTES + 1)
            except OSError:
                records.append({"path": str(path), "unreadable": "Not safely readable; original file retained and not cleanup-authorized."})
                continue
            if len(value) > MAX_CONTROL_RECORD_BYTES:
                raise SetupError("An old storage record is too large to archive safely; no data was adopted.")
            records.append({"path": str(path), "bytes_base64": base64.b64encode(value).decode("ascii")})
    return records


def _adopt_selected_root(root: Path, preferences: Path, expected_identity) -> None:
    """Explicit owner confirmation authorizes only this displayed dedicated tree."""
    trusted = []
    unconfirmed_preferences = []
    for folder in dict.fromkeys((preferences, *_preference_family(preferences))):
        try:
            for candidate in _registry(folder):
                if candidate == root or (candidate.exists() and _marker(candidate, folder)):
                    trusted.append(candidate)
        except (SetupError, OSError):
            # Historical pointers remain informational in the archive only.
            if folder != preferences:
                unconfirmed_preferences.append(str(folder))
            continue
    trusted = list(dict.fromkeys([*trusted, root]))
    _disjoint_roots(trusted)
    preferences.parent.mkdir(parents=True, exist_ok=True)
    root.parent.mkdir(parents=True, exist_ok=True)
    allowed_preferences = {"settings.json", "runtime.json", REGISTRY, "update-result.json", "updates", UNCONFIRMED_RECORDS,
                           "installed-files.json", "installed-release.json", "installed-release.sig"}
    def authorize_preferences(path):
        return all(entry.name in allowed_preferences or _PREF_TEMP.fullmatch(entry.name) for entry in path.iterdir())
    with ExitStack() as held:
        if os.name == "nt":
            held.enter_context(guard_private_tree(preferences, create=True, authorize_existing=authorize_preferences,
                                                  allow_locked_ancestors=True))
            held.enter_context(guard_private_tree(root, create=True, authorize_existing=_selected_names_only,
                                                  allow_locked_ancestors=True, expected_identity=expected_identity))
        else:
            preferences.mkdir(parents=True, exist_ok=True)
            root.mkdir(parents=True, exist_ok=True)
        if not _selected_names_only(root):
            raise SetupError("Choose a dedicated data folder containing only the application's runtime, models, logs and service-home. Personal files were preserved.")
        records = _snapshot_records(preferences, root)
        archive_path = preferences / UNCONFIRMED_RECORDS
        archive = _read_json(archive_path) if archive_path.exists() else {}
        history = archive.get("adoptions", []) if archive.get("app_id") == APP_ID else []
        if not isinstance(history, list):
            raise SetupError("The historical storage archive is invalid; preserve it and review Configure before retrying.")
        history.append({"confirmed_root": str(root), "at_utc": datetime.now(timezone.utc).isoformat(), "records": records})
        _write_json(archive_path, {"app_id": APP_ID, "notice": "Historical records only. These paths do not authorize cleanup; remove unwanted old stores manually after review.", "adoptions": history})
        archives = {f"ollama-windows-amd64-{OLLAMA_VERSION}.zip"}
        for entry in root.iterdir():
            name = entry.name.removesuffix(".part")
            if _ARCHIVE.fullmatch(name):
                archives.add(name)
        marker = {"app_id": APP_ID, "schema_version": OWNERSHIP_SCHEMA_VERSION, "data_root": str(root),
                  "root_identity": path_identity(root), "preferences_dir": str(preferences), "archives": sorted(archives)}
        try:
            _write_json(root / MARKER, marker)
            _write_json(preferences / REGISTRY, {"app_id": APP_ID, "schema_version": OWNERSHIP_SCHEMA_VERSION,
                                                "data_roots": [str(item) for item in trusted],
                                                "unconfirmed_preferences": unconfirmed_preferences})
            _write_json(preferences / "runtime.json", {"data_root": str(root)})
        except OSError as exc:
            # The raw originals are already archived. Do not re-authorize any
            # legacy pointer by restoring it as a fresh sealed ownership record.
            raise SetupError("The selected folder could not be fully registered. Original control records were preserved in the private historical archive; review Configure and retry. No model data was deleted.") from exc


def register_data_root(data_root: Path, preferences_dir: Path, *, confirmed_root: Path | None = None,
                       confirmed_identity=_CONFIRMATION_UNSET) -> None:
    """Write new cleanup records only inside protected application preferences."""
    preferences = _preferences(preferences_dir)
    # Validate the selected store before creating any preferences directories.
    root = _data_root(data_root, preferences)
    if confirmed_root is not None:
        if _data_root(confirmed_root, preferences) != root:
            raise SetupError("The confirmed folder does not match the selected folder. Review the exact folder in Configure.")
        if confirmed_identity is _CONFIRMATION_UNSET:
            raise SetupError("The selected folder's confirmation identity is missing. Review it in Configure again.")
        _adopt_selected_root(root, preferences, confirmed_identity)
        return
    if storage_requires_confirmation(root, preferences):
        raise SetupError("The untracked storage needs review. Choose a dedicated data folder. " + REVIEW_STORAGE)
    existed = preferences.exists()
    preferences.parent.mkdir(parents=True, exist_ok=True)
    guard = (guard_private_tree(preferences, create=True, authorize_existing=_authorize_preferences,
                                allow_locked_ancestors=True) if os.name == "nt" else nullcontext(preferences))
    try:
        with guard:
            _register_private_data_root(root, preferences)
    except (OSError, SetupError) as exc:
        if not existed:
            _remove_empty_folder(preferences)
        if isinstance(exc, StorageError):
            raise SetupError(f"The application's preferences could not be protected safely. {exc}") from exc
        raise


def _register_private_data_root(data_root: Path, preferences_dir: Path) -> None:
    """Reserve a private, dedicated app store before writing ownership records."""
    preferences = _preferences(preferences_dir)
    root = _data_root(data_root, preferences)
    existed = root.exists()
    marker = _marker(root, preferences)
    if existed and not marker:
        # Existing contents need the explicit identity-bound adoption path.
        example_marker = {"archives": [f"ollama-windows-amd64-{OLLAMA_VERSION}.zip"]}
        owned = _targets(root, example_marker)
        if any(path.exists() or path.is_symlink() for path in owned):
            raise SetupError("The selected folder already contains untracked runtime, model or log files. Choose an empty dedicated data folder.")

    def authorize(path):
        if not any(path.iterdir()):
            return True
        if marker:
            return authorize_data_root(path, preferences)
        return False

    # A selected custom parent may not exist yet. Create it, never change its
    # permissions; the Windows guard then verifies and pins each component.
    root.parent.mkdir(parents=True, exist_ok=True)
    guard = (guard_private_tree(root, create=True, authorize_existing=authorize, allow_locked_ancestors=True)
             if os.name == "nt" else nullcontext(root))
    try:
        with guard:
            _register_data_root(root, preferences)
    except (OSError, SetupError) as exc:
        if not existed:
            _remove_empty_folder(root)
        if isinstance(exc, StorageError):
            raise SetupError(f"Choose an empty dedicated data folder or repair this application's owned storage. {exc}") from exc
        raise


def _register_data_root(data_root: Path, preferences_dir: Path) -> None:
    """Reserve known app data names before any runtime/model download starts."""
    preferences = _preferences(preferences_dir)
    root = _data_root(data_root, preferences)
    roots = _registry(preferences)
    related_roots = list(roots)
    for related in _preference_family(preferences):
        if related in _unconfirmed_preferences(preferences):
            continue
        if related.exists():
            related = _preferences(related)
            related_roots.extend(_registry(related))
    _disjoint_roots(list(dict.fromkeys([*related_roots, root])))
    marker = _marker(root, preferences)
    previous_marker = json.loads(json.dumps(marker)) if marker else None
    root_existed = root.exists()
    preferences_existed = preferences.exists()
    archive = f"ollama-windows-amd64-{OLLAMA_VERSION}.zip"
    if not marker:
        # This path reserves an empty store; adoption has a separate consent gate.
        marker = {"app_id": APP_ID, "schema_version": OWNERSHIP_SCHEMA_VERSION, "data_root": str(root),
                  "root_identity": path_identity(root), "preferences_dir": str(preferences), "archives": [archive]}
        existing = [path for path in _targets(root, marker) if path.exists() or path.is_symlink()]
        if existing:
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
                                           "data_roots": [str(path) for path in roots],
                                           "unconfirmed_preferences": [str(path) for path in _unconfirmed_preferences(preferences)]})
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
    # Authenticated file-update receipts survive until the external program
    # cleanup succeeds, so an interrupted uninstall can export its plan again.
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
    with ExitStack() as held_roots:
        report = _uninstall_data(preferences_dir, held_roots)
    # Release identity pins before removing the empty root directories themselves.
    # rmdir never recurses; a concurrent new sibling is preserved, not swept.
    try:
        for root in [*report["data_roots"], *report["preference_folders"]]:
            _remove_empty_folder(Path(root))
    except OSError as exc:
        raise SetupError("Some empty application folders could not be removed; retry uninstall.") from exc
    return report


def _uninstall_data(preferences_dir: Path, held_roots: ExitStack) -> dict:
    from app_updater import cleanup_update_cache, update_in_progress
    if update_in_progress():
        raise SetupError("An update is in progress. Wait for it to finish before uninstalling.")
    preferences = _preferences(preferences_dir)
    preference_folders = [preferences]
    ignored_preferences = _unconfirmed_preferences(preferences)
    for related in _preference_family(preferences):
        if related != preferences and related not in ignored_preferences and related.exists():
            preference_folders.append(_preferences(related))
    for folder in preference_folders:
        if folder.exists():
            guard = (guard_private_tree(folder, authorize_existing=_authorize_preferences,
                                         allow_locked_ancestors=True) if os.name == "nt" else locked_directory(folder))
            held_roots.enter_context(guard)
    roots = []
    for folder in preference_folders:
        recorded = _registry(folder)
        current = _current_root(folder)
        if current is not None and current not in recorded:
            if not current.exists():
                recorded.append(current)
            else:
                raise SetupError(REVIEW_STORAGE)
        roots.extend(recorded)
    roots = list(dict.fromkeys(roots))
    _disjoint_roots(roots)
    plans = []
    for root in roots:
        if not root.exists():
            continue
        # Ownership records are meaningful only for this exact directory. Hold
        # its identity from record validation through the final payload delete.
        held_roots.enter_context(locked_directory(root))
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
                # The preflight is only a scope check. Removal itself holds
                # ancestor/child handles and deletes by handle on Windows, so a
                # concurrent junction swap cannot redirect recursive cleanup.
                if safe_remove_tree(target):
                    removed.append(str(target))
        for root, _ in plans:
            _tree_check(root / MARKER, root)
        for folder, target in preference_targets:
            _tree_check(target, folder)
        # Keep the registry if any earlier payload operation failed.
        for _, target in preference_targets:
            safe_remove_tree(target)
        for root, _ in plans:
            safe_remove_tree(root / MARKER)
        for folder in preference_folders:
            safe_remove_tree(folder / REGISTRY)
    except OSError as exc:
        raise SetupError(f"Some application data could not be removed. Close files in use and retry uninstall. {exc}") from exc
    preserved = [str(folder / UNCONFIRMED_RECORDS) for folder in preference_folders if (folder / UNCONFIRMED_RECORDS).exists()]
    return {"ok": True, "service_stopped": stopped, "data_roots": [str(root) for root in roots],
            "removed_paths": removed, "preferences_removed": True,
            "unconfirmed_records_preserved": preserved,
            "unconfirmed_preferences_preserved": [str(folder) for folder in ignored_preferences],
            "preference_folders": [str(folder) for folder in preference_folders]}
