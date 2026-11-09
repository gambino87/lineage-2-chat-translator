"""Remove only unchanged, retired program files after a successful install.

The build supplies file names and hashes from earlier releases, never their
contents. This module has no access to model/settings registries and never
recursively deletes a directory. The installed entrypoint binds the root to its
own executable and holds the application's instance guard before calling it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

CLEANUP_PLAN_NAME = "obsolete-program-files.json"
PRODUCT = "Lineage 2 Chat Translator"
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_PLAN_ENTRIES = 10000
MAX_HASHES_PER_FILE = 128
MAX_OBSOLETE_FILE_BYTES = 128 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
MAX_RELATIVE_PATH_CHARS = 1024
MAX_COMPONENT_CHARS = 255
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.I)
_PRIVATE_COMPONENTS = frozenset({"models", "logs", "settings.json", "runtime.json", "network.log"})
_ROOT_DOCUMENTS = frozenset({"user-readme.txt", "third-party-notices.txt"})


class CleanupError(ValueError):
    """An unsafe or inconsistent cleanup plan was rejected without guessing."""


def _require(condition, message):
    if not condition:
        raise CleanupError(message)


def safe_relative_path(value) -> str:
    """Accept a canonical POSIX path that has an unambiguous Windows meaning."""
    _require(isinstance(value, str) and 0 < len(value) <= MAX_RELATIVE_PATH_CHARS,
             "Invalid program file path.")
    _require(not any(ord(char) < 32 or ord(char) == 127 or char in '\\:*?"<>|' for char in value),
             "Program file paths cannot contain Windows path syntax or wildcards.")
    for part in value.split("/"):
        _require(part not in ("", ".", "..") and len(part) <= MAX_COMPONENT_CHARS
                 and part == part.strip() and not part.endswith("."),
                 "Program file paths must be canonical relative names.")
        _require(not _DEVICE.match(part), "Windows device names cannot be program file paths.")
    return value


def _ancestors(value: str) -> set[str]:
    parts = value.split("/")
    return {"/".join(parts[:index]) for index in range(1, len(parts))}


def _unique_paths(values: list) -> tuple[list[str], set[str], set[str]]:
    paths, keys, ancestors = [], set(), set()
    for value in values:
        path = safe_relative_path(value)
        key = path.casefold()
        parents = _ancestors(key)
        _require(key not in keys, "Program file paths collide on Windows.")
        _require(key not in ancestors and not (parents & keys),
                 "A program file path conflicts with another file's directory.")
        paths.append(path)
        keys.add(key)
        ancestors.update(parents)
    return paths, keys, ancestors


def _obsolete_path(value: str) -> str:
    path = safe_relative_path(value)
    parts = path.casefold().split("/")
    _require((len(parts) > 1 and parts[0] in {"_internal", "licenses"})
             or (len(parts) == 1 and parts[0] in _ROOT_DOCUMENTS),
             "Cleanup is restricted to packaged libraries, assets, licenses and documentation.")
    _require(not any(part in _PRIVATE_COMPONENTS or part.endswith(".local.json") for part in parts),
             "Models, settings and logs cannot be cleanup targets.")
    _require(not parts[-1].endswith(".exe") and not parts[-1].startswith("unins"),
             "Cleanup cannot remove executables or uninstaller files.")
    return path


def validate_plan(document, version: str) -> dict:
    """Validate every entry and every current-file collision before deletion."""
    _require(isinstance(version, str) and _VERSION.fullmatch(version), "Invalid cleanup version.")
    _require(isinstance(document, dict) and set(document) == {
        "schema_version", "product", "version", "current_paths", "obsolete_files"},
        "Invalid program cleanup plan.")
    _require(type(document["schema_version"]) is int and document["schema_version"] == 1
             and document["product"] == PRODUCT and document["version"] == version,
             "The program cleanup plan belongs to a different release.")
    current, obsolete = document["current_paths"], document["obsolete_files"]
    _require(isinstance(current, list) and 0 < len(current) <= MAX_PLAN_ENTRIES
             and isinstance(obsolete, list) and len(obsolete) <= MAX_PLAN_ENTRIES,
             "The program cleanup plan has too many or missing file entries.")
    current_paths, current_keys, current_ancestors = _unique_paths(current)
    obsolete_paths = []
    normalized = []
    for entry in obsolete:
        _require(isinstance(entry, dict) and set(entry) == {"path", "hashes"},
                 "Invalid obsolete program file entry.")
        path = _obsolete_path(entry["path"])
        key = path.casefold()
        _require(key not in current_keys and key not in current_ancestors
                 and not (_ancestors(key) & current_keys),
                 "Cleanup cannot remove or overlap current program files.")
        hashes = entry["hashes"]
        _require(isinstance(hashes, list) and 0 < len(hashes) <= MAX_HASHES_PER_FILE
                 and all(isinstance(value, str) and _HASH.fullmatch(value) for value in hashes),
                 "Obsolete program file hashes are invalid.")
        obsolete_paths.append(path)
        normalized.append({"path": path, "hashes": sorted(set(hashes))})
    _unique_paths(obsolete_paths)
    return {"schema_version": 1, "product": PRODUCT, "version": version,
            "current_paths": current_paths, "obsolete_files": normalized}


def _plain_path(path: Path) -> Path:
    """Reject links/junctions even in ancestors of the installation directory."""
    path = Path(path)
    _require(path.is_absolute(), "The installation path must be absolute.")
    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode)
                 and not (getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT),
                 "Program cleanup will not follow linked files or folders.")
    _require(absolute.resolve() == absolute, "The installation path changed while checking it.")
    return absolute


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "The cleanup plan contains duplicate fields.")
        result[key] = value
    return result


def read_cleanup_plan(path: Path, version: str) -> dict:
    """Bound the bytes actually read, including if a file grows during reading."""
    path = _plain_path(path)
    _require(stat.S_ISREG(path.lstat().st_mode), "The cleanup plan is not a regular file.")
    with path.open("rb") as source:
        data = source.read(MAX_PLAN_BYTES + 1)
    _require(len(data) <= MAX_PLAN_BYTES, "The program cleanup plan is too large.")
    try:
        document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_json)
    except (UnicodeError, ValueError) as exc:
        raise CleanupError("The program cleanup plan is unreadable.") from exc
    return validate_plan(document, version)


def _candidate(root: Path, relative: str) -> Path:
    path = _plain_path(root.joinpath(*relative.split("/")))
    _require(path != root and path.is_relative_to(root), "Cleanup path escaped the installation.")
    return path


def _file_state(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _require(stat.S_ISREG(info.st_mode), "An obsolete program file was replaced with a directory or special file.")
    return info


def _same_file(left, right) -> bool:
    return ((left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns)
            == (right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns))


def _remove_empty_parents(root: Path, path: Path) -> list[str]:
    removed = []
    parent = path.parent
    # These shared top-level folders remain part of the package layout.
    while parent != root and parent.parent != root:
        parent = _plain_path(parent)
        _require(parent.is_relative_to(root), "Cleanup directory escaped the installation.")
        if not parent.exists() or any(parent.iterdir()):
            break
        # rmdir is deliberately nonrecursive: a newly arrived file prevents it.
        parent.rmdir()
        removed.append(parent.relative_to(root).as_posix())
        parent = parent.parent
    return removed


def _safe_to_preserve(relative: str):
    # Unknown bytes are never silently deleted. A leftover loadable binary can
    # affect the newly installed runtime, so require explicit owner cleanup.
    _require(Path(relative).suffix.casefold() not in {'.dll', '.pyd', '.exe'},
             f'Retired program file {relative} differs from known releases and was left untouched. '
             'Move or remove that file manually, then run this installer again before opening the translator.')


def cleanup_program_files(install_root: Path, plan: dict, version: str) -> dict:
    """Delete retired files only when their complete bytes match a past release.

    Modified data files are preserved; modified loadable binaries block success.
    Failures remain visible to the installer so a
    later install can retry; an already removed file is harmless on that retry.
    """
    plan = validate_plan(plan, version)
    root = _plain_path(install_root)
    _require(root.is_dir() and root != Path(root.anchor), "Invalid installation directory.")
    candidates = [(entry, _candidate(root, entry["path"])) for entry in plan["obsolete_files"]]
    # A bad later path must not cause partial cleanup of earlier valid paths.
    for _, path in candidates:
        _file_state(path)
    result = {"removed": [], "preserved": [], "removed_directories": []}
    for entry, path in candidates:
        path = _candidate(root, entry["path"])
        before = _file_state(path)
        if before is None:
            continue
        if before.st_size > MAX_OBSOLETE_FILE_BYTES:
            _safe_to_preserve(entry["path"])
            result["preserved"].append(entry["path"])
            continue
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            _require(_same_file(before, opened), "An obsolete file changed while opening it.")
            hasher = hashlib.sha256()
            total = 0
            while True:
                block = source.read(min(HASH_CHUNK_BYTES, MAX_OBSOLETE_FILE_BYTES + 1 - total))
                if not block:
                    break
                total += len(block)
                _require(total <= MAX_OBSOLETE_FILE_BYTES, "An obsolete file grew during verification.")
                hasher.update(block)
            digest = hasher.hexdigest()
        path = _candidate(root, entry["path"])
        after = _file_state(path)
        if after is None:
            continue
        _require(_same_file(before, after), "An obsolete file changed during verification.")
        if digest not in entry["hashes"]:
            _safe_to_preserve(entry["path"])
            result["preserved"].append(entry["path"])
            continue
        path.unlink()
        result["removed"].append(entry["path"])
        result["removed_directories"].extend(_remove_empty_parents(root, path))
    return result
