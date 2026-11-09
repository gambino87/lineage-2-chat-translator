"""Exact current-release payloads and hash-scoped retirement metadata.

Historical installers are read only to verify provenance. None of their bytes,
source snapshots, settings, or reports are copied into the new application.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import stat

from installation_cleanup import CLEANUP_PLAN_NAME, safe_relative_path, validate_plan

LICENSE_FILES = ("PYTHON.txt", "PYINSTALLER.txt", "TCL.txt", "TK.txt", "INNO-SETUP.txt", "L2J-ITEM-DATA.txt")
ROOT_FILES = frozenset({"Lineage2ChatTranslator.exe", "USER-README.txt", "THIRD-PARTY-NOTICES.txt"})
PRIVATE_NAMES = frozenset({"settings.json", "runtime.json", "runtime.local.json", "network.log"})
MAX_HISTORY_MANIFEST_BYTES = 8 * 1024 * 1024
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\Z")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_licenses(source: Path, destination: Path) -> None:
    """Copy only required notices, never backups or historical license trees."""
    for name in LICENSE_FILES:
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or linked license: {name}")
    destination.mkdir()
    for name in LICENSE_FILES:
        shutil.copy2(source / name, destination / name)


def payload_records(app: Path, assets: tuple[str, ...]) -> list[dict]:
    """Reject unexpected layout/private data before compiling an exact file list."""
    allowed_assets = {"_internal/" + name for name in assets}
    allowed_assets.add("_internal/assets/" + CLEANUP_PLAN_NAME)
    records = []
    seen = set()
    for path in sorted(app.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked files or folders cannot enter the installer payload.")
        relative = safe_relative_path(path.relative_to(app).as_posix())
        parts = relative.split("/")
        if parts[0] not in {"_internal", "licenses"} and relative not in ROOT_FILES:
            raise ValueError(f"Unexpected payload entry: {relative}")
        if any(p.casefold() in {".build", ".build-tools", "release", "review_tools", "models", "logs", "captures"} for p in parts):
            raise ValueError(f"Development or user data cannot enter the installer: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Unexpected payload file type: {relative}")
        if path.name.casefold() in PRIVATE_NAMES or path.name.casefold().endswith((".local.json", ".bak", ".old", ".log")):
            raise ValueError(f"Private or historical file cannot enter the installer: {relative}")
        if parts[0] == "licenses" and relative not in {"licenses/" + name for name in LICENSE_FILES}:
            raise ValueError(f"Unexpected license payload: {relative}")
        if relative.startswith("_internal/assets/") and relative not in allowed_assets:
            raise ValueError(f"Unexpected application asset: {relative}")
        if relative.casefold() in seen:
            raise ValueError(f"Case-colliding payload file: {relative}")
        seen.add(relative.casefold())
        records.append({"path": relative, "bytes": info.st_size, "sha256": digest(path)})
    return records


def retirement_plan(records: list[dict], releases: Path, version: str) -> dict:
    """Carry hashes of retired files across skipped versions, never old payloads."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Invalid release version.")
    current = {row["path"].casefold(): row["path"] for row in records}
    plan_path = "_internal/assets/" + CLEANUP_PLAN_NAME
    current[plan_path.casefold()] = plan_path
    retired: dict[str, dict] = {}
    for directory in sorted(releases.iterdir()) if releases.exists() else ():
        if not directory.is_dir() or not VERSION_PATTERN.fullmatch(directory.name):
            continue
        if tuple(map(int, directory.name.split("."))) >= tuple(map(int, version.split("."))):
            continue
        manifest_path = directory / "release-manifest.json"
        if not manifest_path.is_file():
            continue
        with manifest_path.open("rb") as stream:
            content = stream.read(MAX_HISTORY_MANIFEST_BYTES + 1)
        if len(content) > MAX_HISTORY_MANIFEST_BYTES:
            raise ValueError("Historical release manifest is too large.")
        old = json.loads(content.decode("utf-8-sig"))
        if old.get("product") != "Lineage 2 Chat Translator" or old.get("platform") != "windows-x64" or old.get("version") != directory.name:
            raise ValueError("Historical release identity mismatch.")
        artifact = old.get("installer")
        if artifact is None:
            continue  # A headless build without an installer was never distributed.
        filename = f"Lineage-2-Chat-Translator-{directory.name}-Setup.exe"
        installer = directory / filename
        if (artifact.get("file") != filename or not installer.is_file()
                or installer.stat().st_size != artifact.get("bytes")
                or digest(installer) != artifact.get("sha256")):
            raise ValueError(f"Historical installer failed verification: {directory.name}")
        seen = set()
        for row in old["payload"]:
            relative = safe_relative_path(row["path"])
            key = relative.casefold()
            if key in seen or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"]):
                raise ValueError("Invalid historical file record.")
            seen.add(key)
            # A current file, or one replaced by a directory (and vice versa),
            # is never a post-install deletion candidate.
            if key in current or any(key.startswith(p + "/") or p.startswith(key + "/") for p in current):
                continue
            record = retired.setdefault(key, {"path": relative, "hashes": set()})
            record["hashes"].add(row["sha256"])
    document = {"schema_version": 1, "product": "Lineage 2 Chat Translator", "version": version,
                "current_paths": sorted(current.values()),
                "obsolete_files": [{"path": row["path"], "hashes": sorted(row["hashes"])} for _, row in sorted(retired.items())]}
    return validate_plan(document, version)


def write_installer_file_list(path: Path, records: list[dict]) -> None:
    """The compiler gets exact files, not a recursive wildcard into a build tree."""
    lines = ["; Generated from the verified current payload. No recursive wildcards."]
    for row in records:
        relative = safe_relative_path(row["path"]).replace("/", "\\")
        parent = relative.rpartition("\\")[0]
        destination = "{app}" + ("\\" + parent if parent else "")
        lines.append(f'Source: "{{#AppSource}}\\{relative}"; DestDir: "{destination}"; Flags: ignoreversion')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
