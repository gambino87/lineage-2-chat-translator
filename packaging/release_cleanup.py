"""Bounded post-publication pruning of disposable, workspace-owned artifacts.

The publication finalizer owns the success gate. This module never discovers
or deletes model locations, settings, source directories, canonical releases,
or files outside the workspace. Windows deletion uses the validated handles.
"""
from contextlib import ExitStack
import ctypes
from datetime import datetime, timezone
import os
import json
from pathlib import Path
import re
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import secure_storage as storage


VERSION = r"(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})"
BUILD_NAME = re.compile(rf"(?P<version>{VERSION})-(?P<stamp>[0-9]{{8}}T[0-9]{{6}}Z)\Z")
RELEASE_BUNDLE = re.compile(rf"Lineage-2-Chat-Translator-(?P<version>{VERSION})-(?:hosting|main-server)\.zip\Z")
SITE_ARCHIVES = (
    re.compile(rf"Lineage-2-Chat-Translator-(?P<version>{VERSION})-sites\.zip\Z"),
    re.compile(rf"site-(?P<version>{VERSION})-[a-z0-9-]+\.tar\.gz\Z"),
    re.compile(rf"sites-l2-(?P<version>{VERSION})\.tar\.gz\Z"),
    re.compile(rf"release-(?P<version>{VERSION})-site\.tar(?:\.gz)?\Z"),
)
LEGACY_SITE_ARCHIVES = frozenset({"sites-github-source-link.tar.gz"})
BACKEND_CACHE_CHILDREN = frozenset({"go", "go.zip", "gocache", "gopath", "llvm", "llvm.zip",
                                   "ollama-windows-amd64.zip", "source", "source.zip", "work"})
MAX_TARGETS = 1000
MAX_ENTRIES = 150000
MAX_DEPTH = 64


class CleanupError(ValueError):
    """No safe cleanup plan can be applied to the current workspace."""


def _version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(VERSION, value):
        raise CleanupError("Invalid finalized release version.")
    return tuple(map(int, value.split(".")))


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise CleanupError("Cleanup requires an absolute workspace path without traversal.")
    result = Path(os.path.abspath(path))
    if result == Path(result.anchor) or result == Path.home():
        raise CleanupError("A drive root or home folder cannot be a cleanup workspace.")
    if os.name == "nt" and (str(result).startswith("\\\\") or ":" in str(result)[2:]):
        raise CleanupError("Cleanup requires a local workspace.")
    return result


def _plain(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise CleanupError("Cleanup refuses a redirected generated path: " + str(path))


def _site_version(path: Path, known_versions: set[str]) -> str | None:
    for pattern in SITE_ARCHIVES:
        match = pattern.fullmatch(path.name)
        if match:
            return match["version"]
    # Earlier local publishing scripts omitted dots. Resolve only an unambiguous
    # token corresponding to a retained canonical release, never guess a version.
    old = re.fullmatch(r"release-([0-9]+)-site\.tar(?:\.gz)?", path.name)
    if old:
        matches = [v for v in known_versions if v.replace(".", "") == old[1]]
        if len(matches) == 1:
            return matches[0]
    return None


def plan_cleanup(root: Path, version: str, latest_build: Path) -> list[dict]:
    """Select known generated artifacts, retaining latest and unpublished work.

    Upload packages newer than the latest completed build are deliberately kept
    until a later release. That includes the most recent publication package.
    """
    root, latest = _absolute(root), _absolute(latest_build)
    finalized = _version(version)
    build_root, releases = root / ".build", root / "release"
    for folder in (root, build_root, releases):
        _plain(folder)
        if not folder.is_dir():
            raise CleanupError("Missing release workspace directory: " + str(folder))
    match = BUILD_NAME.fullmatch(latest.name)
    if latest.parent != build_root or not match or match["version"] != version:
        raise CleanupError("Latest build does not belong to the finalized release.")
    _plain(latest)
    if not latest.is_dir():
        raise CleanupError("Latest verified build is missing.")
    try:
        cutoff = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise CleanupError("Invalid latest-build timestamp.") from exc
    known_versions = set()
    for directory in releases.iterdir():
        if not re.fullmatch(VERSION, directory.name):
            continue
        _plain(directory)
        if not directory.is_dir():
            continue
        manifest = directory / "release-manifest.json"
        installer = directory / f"Lineage-2-Chat-Translator-{directory.name}-Setup.exe"
        if manifest.is_file() and installer.is_file():
            _plain(manifest)
            _plain(installer)
            known_versions.add(directory.name)
    candidates = []

    def add(path: Path, category: str):
        _plain(path)
        if not path.resolve().is_relative_to(root.resolve()):
            raise CleanupError("Cleanup target escapes its workspace.")
        candidates.append({"path": path.relative_to(root).as_posix(), "category": category})

    for path in sorted(build_root.iterdir()):
        old = BUILD_NAME.fullmatch(path.name)
        if old and path != latest and old["version"] in known_versions and _version(old["version"]) <= finalized:
            try:
                when = datetime.strptime(old["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise CleanupError("Invalid generated build timestamp.") from exc
            if when < cutoff:
                if not path.is_dir():
                    raise CleanupError("A generated build path has become a file.")
                add(path, "old_build")
            continue
        old_version = _site_version(path, known_versions)
        if (old_version in known_versions and _version(old_version) <= finalized) or path.name in LEGACY_SITE_ARCHIVES:
            if not path.is_file():
                raise CleanupError("A generated archive path has become a directory.")
            if path.stat().st_mtime <= cutoff.timestamp():
                add(path, "site_upload_archive")
    for path in sorted(releases.iterdir()):
        bundle = RELEASE_BUNDLE.fullmatch(path.name)
        if bundle and bundle["version"] in known_versions and _version(bundle["version"]) <= finalized:
            if not path.is_file():
                raise CleanupError("A generated publication bundle has become a directory.")
            if path.stat().st_mtime <= cutoff.timestamp():
                add(path, "publication_bundle")
    backend = root / ".build-tools" / "secure-ollama"
    if backend.exists():
        for part in (root / ".build-tools", backend):
            _plain(part)
        output = backend / "output/ollama.exe"
        if output.is_file() and output.stat().st_mtime <= cutoff.timestamp():
            _plain(output.parent)
            _plain(output)
            for name in sorted(BACKEND_CACHE_CHILDREN):
                path = backend / name
                if path.exists() or path.is_symlink():
                    add(path, "backend_build_cache")
    if len(candidates) > MAX_TARGETS:
        raise CleanupError("Too many cleanup candidates; review the workspace first.")
    return candidates


def _check_latest_pointer(root: Path, latest: Path) -> None:
    record = root / ".build/latest-build.json"
    _plain(record)
    with record.open("rb") as source:
        content = source.read(65537)
    if len(content) > 65536:
        raise CleanupError("Latest-build record is unexpectedly large.")
    try:
        value = json.loads(content.decode("utf-8-sig"))
        selected = _absolute(Path(value["build"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise CleanupError("Latest-build record is invalid.") from exc
    if selected != _absolute(latest):
        raise CleanupError("Latest build changed before cleanup; preserve all artifacts and retry.")


def _write_progress(root: Path, version: str, report: dict) -> None:
    """Keep retry evidence outside candidates; atomically replace only this record."""
    destination = root / ".build" / f"release-cleanup-{version}.local.json"
    if destination.exists() or destination.is_symlink():
        _plain(destination)
        if not destination.is_file():
            raise CleanupError("Cleanup progress record is not a regular file.")
    descriptor, temporary = tempfile.mkstemp(prefix="release-cleanup-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


class _TagInfo(ctypes.Structure):
    _fields_ = [("attributes", storage.w.DWORD), ("tag", storage.w.DWORD)]


class _BasicInfo(ctypes.Structure):
    _fields_ = [("creation", ctypes.c_longlong), ("access", ctypes.c_longlong),
                ("write", ctypes.c_longlong), ("change", ctypes.c_longlong), ("attributes", storage.w.DWORD)]


class _CleanupWindows(storage._Windows):
    """Build-only cloud-ancestor support without weakening installed app storage."""
    def __init__(self, root):
        super().__init__()
        self.cloud_ancestors = {root, *root.parents}
        self._declare(self.k, "GetFileInformationByHandleEx", storage.w.BOOL,
                      storage.w.HANDLE, ctypes.c_int, ctypes.c_void_p, storage.w.DWORD)

    def open(self, path, *, ancestor=False, delete=False, **unused):
        access = 0x20081 | (0x10100 if delete else 0)  # READ_CONTROL/read data/attributes; optional DELETE/write attributes.
        handle = self.k.CreateFileW(str(path), access, 3 if ancestor else 1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            self.fail("Generated files are in use or cannot be pinned safely")
        return handle

    def inspect(self, handle, path):
        info = storage._Info()
        if not self.k.GetFileInformationByHandle(handle, ctypes.byref(info)):
            self.fail("Cannot inspect generated storage")
        buffer = ctypes.create_unicode_buffer(32768)
        length = self.k.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            self.fail("Cannot resolve generated storage")
        actual = buffer.value.removeprefix("\\\\?\\")
        if os.path.normcase(actual.rstrip("\\")) != os.path.normcase(str(path).rstrip("\\")):
            raise CleanupError("Generated storage resolves outside its checked path.")
        if info.attributes & storage._REPARSE:
            tag = _TagInfo()
            if not self.k.GetFileInformationByHandleEx(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
                self.fail("Cannot inspect a generated-storage ancestor")
            # IO_REPARSE_TAG_CLOUD and CLOUD_1..F are non-name-surrogate tags.
            # Only workspace ancestors may use them; candidate trees never may.
            cloud = tag.tag & 0xFFFF0FFF == 0x9000001A and not tag.tag & 0x20000000
            if path not in self.cloud_ancestors or not info.attributes & storage._DIRECTORY or not cloud:
                raise CleanupError("Cleanup will not follow redirected files or directories.")
        if not info.attributes & storage._DIRECTORY and info.links != 1:
            raise CleanupError("Cleanup refuses hard-linked generated files.")
        return info

    def make_deletable(self, handle):
        basic = _BasicInfo()
        if not self.k.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            self.fail("Cannot inspect generated file attributes")
        if basic.attributes & 1:
            basic.attributes &= ~1  # Clear only READONLY on the exact deletion target.
            if not self.k.SetFileInformationByHandle(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                self.fail("Cannot remove a read-only generated file")


def cleanup_release(root: Path, version: str, latest_build: Path) -> dict:
    """Delete only an internally generated plan after the caller verifies release.

    Every target and descendant is pinned before any deletion starts. The caller
    holds release_lock throughout publication verification and this operation.
    """
    root = _absolute(root)
    if os.name != "nt":
        raise CleanupError("Native post-release cleanup requires Windows.")
    plan = plan_cleanup(root, version, latest_build)
    api = _CleanupWindows(root)
    removed = []
    report = {"version": version, "status": "preflight", "candidates": plan,
              "removed": removed, "removed_bytes": 0, "removed_targets": 0,
              "preserved_latest_build": str(latest_build), "models_touched": False}
    entries = []
    root_handles = {}
    with ExitStack() as held:
        def pin_ancestor(path):
            if path in root_handles:
                return
            handle, info = storage._open_checked(api, held, path, ancestor=True)
            if not info.attributes & storage._DIRECTORY:
                raise CleanupError("A cleanup ancestor is not a directory.")
            api.verify_owner(api.security(handle), ancestor=True)
            root_handles[path] = handle

        for path in reversed((root, *root.parents)):
            pin_ancestor(path)
        for candidate in plan:
            target = root / candidate["path"]
            for parent in reversed(target.parents):
                pin_ancestor(parent)
        # The pointer cannot be rewritten during deletion, including by a
        # noncooperating tool. The shared workflow lock also protects caches.
        pointer, _ = storage._open_checked(api, held, root / ".build/latest-build.json")
        api.verify_owner(api.security(pointer))
        _check_latest_pointer(root, latest_build)

        # Target handles have separate ownership so child handles close before
        # deleting their parent while all remaining objects stay pinned.
        try:
            def collect(path, depth, group):
                if depth > MAX_DEPTH or len(entries) >= MAX_ENTRIES:
                    raise CleanupError("Generated tree is too large or deeply nested for automatic cleanup.")
                handle = api.open(path, delete=True)
                try:
                    info = api.inspect(handle, path)
                    api.verify_owner(api.security(handle))
                    directory = bool(info.attributes & storage._DIRECTORY)
                    looks_like_models = directory and path.name.casefold() == "models" and (
                        plan[group]["category"] == "old_build"
                        or (path / "blobs").exists() or (path / "manifests").exists())
                    if path.name.casefold() == ".l2-translator-install.json" or looks_like_models:
                        raise CleanupError("A cleanup candidate contains model storage; preserve it for manual review.")
                    entry = {"path": path, "handle": handle, "directory": directory, "group": group,
                             "bytes": 0 if directory else (info.size_high << 32) | info.size_low}
                    entries.append(entry)
                except BaseException:
                    api.k.CloseHandle(handle)
                    raise
                if directory:
                    for name in sorted(os.listdir(path)):
                        collect(path / name, depth + 1, group)
            for index, candidate in enumerate(plan):
                collect(root / candidate["path"], 0, index)
            # A newly inserted child or a changed latest pointer must stop the
            # whole batch before READONLY attributes or file contents change.
            expected = {}
            for entry in entries:
                if entry["directory"]:
                    expected[entry["path"]] = set()
            for entry in entries:
                if entry["path"].parent in expected:
                    expected[entry["path"].parent].add(entry["path"].name)
            for directory, children in expected.items():
                if set(os.listdir(directory)) != children:
                    raise CleanupError("Generated storage changed during cleanup preflight.")
            _check_latest_pointer(root, latest_build)
            if plan_cleanup(root, version, latest_build) != plan:
                raise CleanupError("Cleanup candidates changed during preflight; retry safely.")
            report["status"] = "deleting"
            _write_progress(root, version, report)
            for index, candidate in enumerate(plan):
                group = [entry for entry in entries if entry["group"] == index]
                for entry in reversed(group):
                    api.make_deletable(entry["handle"])
                    api.delete(entry["handle"])
                    api.k.CloseHandle(entry["handle"])
                    entry["handle"] = None
                removed.append({**candidate, "bytes": sum(entry["bytes"] for entry in group)})
                report["removed_targets"] = len(removed)
                report["removed_bytes"] = sum(item["bytes"] for item in removed)
                _write_progress(root, version, report)
            report["status"] = "complete"
            _write_progress(root, version, report)
        except BaseException as exc:
            report["status"] = "partial_failure" if removed else "not_started_or_first_target_incomplete"
            report["error"] = str(exc)[:2000]
            # Evidence failure must not mask the original cleanup error.
            try:
                _write_progress(root, version, report)
            except (OSError, CleanupError):
                pass
            raise
        finally:
            for entry in entries:
                if entry["handle"] is not None:
                    api.k.CloseHandle(entry["handle"])
    return report
