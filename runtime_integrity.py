"""Trust runtime bytes from the signed application's manifest, never a receipt.

The downloaded vendor archive supplies GPU libraries. Only the small frontend
is replaced by this project's documented authenticated build. Model weights are
separate and never bundled with the application.
"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys

from secure_storage import StorageError, locked_file


MANIFEST_PATH = Path(__file__).parent / "assets" / "secure-ollama-manifest.json"
BACKEND_VERSION = "0.40.14-l2ct.2"
PREDECESSOR_FRONTENDS = {
    "fd03fdbb47a91b247e26f63769d694c761ee617e063a01ec69e388270ca040c0": 40338944,
}
LOADABLE_SUFFIXES = {".exe", ".dll", ".pyd", ".com"}
MAX_MANIFEST_BYTES = 256 * 1024


def load_manifest():
    content = MANIFEST_PATH.read_bytes()
    if len(content) > MAX_MANIFEST_BYTES:
        raise StorageError("The bundled runtime inventory is too large.")
    data = json.loads(content)
    if (data.get("version") != BACKEND_VERSION or data.get("upstream_version") != "0.40.14"
            or not re.fullmatch(r"[a-f0-9]{64}", data.get("archive_sha256", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", data.get("upstream_executable_sha256", ""))):
        raise StorageError("The bundled runtime identity is invalid.")
    seen = set()
    files = data.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 1000:
        raise StorageError("The bundled runtime inventory is invalid.")
    for row in files:
        name = row.get("path", "")
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or str(path) != name or ".." in path.parts
                or any(char in name for char in '\\:*?"<>|') or name.casefold() in seen
                or type(row.get("bytes")) is not int or not 0 <= row["bytes"] <= 8 * 1024**3
                or not re.fullmatch(r"[a-f0-9]{64}", row.get("sha256", ""))):
            raise StorageError("The bundled runtime contains an unsafe inventory entry.")
        seen.add(name.casefold())
    if "ollama.exe" not in seen:
        raise StorageError("The bundled runtime has no frontend identity.")
    return data


def bundled_frontend():
    if getattr(sys, "frozen", False):
        return Path(__file__).parent / "secure-runtime" / "ollama.exe"
    return Path(__file__).parent / ".build-tools" / "secure-ollama" / "output" / "ollama.exe"


def pin_runtime(root, *, legacy=False, check=lambda: None):
    """Return held read-only handles after validating all distributed members.

    The caller holds the private-root guard for execution. Holding these files
    also prevents accidental writes or deletion until its service stops. Owner
    control returns normally as soon as those ordinary Windows locks close.
    """
    manifest = load_manifest()
    expected = {row["path"].casefold() for row in manifest["files"]}
    root = Path(root)
    stack = ExitStack()
    try:
        # Reject extra loadable files rather than silently ignoring DLLs placed
        # next to a verified executable. Never delete an owner's unknown files.
        pending = [root]
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                check()
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise StorageError("Runtime files cannot contain linked or redirected paths.")
                if path.is_dir():
                    pending.append(path)
                elif path.suffix.casefold() in LOADABLE_SUFFIXES and path.relative_to(root).as_posix().casefold() not in expected:
                    raise StorageError("The runtime contains an unexpected executable or library. Its files were preserved.")
        for row in manifest["files"]:
            check()
            source = stack.enter_context(locked_file(root / row["path"]))
            legacy_frontend = legacy and row["path"].casefold() == "ollama.exe"
            digest = hashlib.sha256()
            count = 0
            while block := source.read(1024 * 1024):
                check()
                digest.update(block)
                count += len(block)
            actual_hash = digest.hexdigest()
            if legacy_frontend:
                matches = (actual_hash == manifest["upstream_executable_sha256"]
                           or PREDECESSOR_FRONTENDS.get(actual_hash) == count)
            else:
                matches = actual_hash == row["sha256"] and count == row["bytes"]
            if not matches:
                raise StorageError("The runtime does not match the trusted application inventory. Its files were preserved.")
        return stack
    except Exception:
        stack.close()
        raise


def verified_runtime(root, *, legacy=False, check=lambda: None):
    try:
        with pin_runtime(root, legacy=legacy, check=check):
            return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def verify_bundled_frontend():
    row = next(row for row in load_manifest()["files"] if row["path"] == "ollama.exe")
    path = bundled_frontend()
    with path.open("rb") as stream:
        if path.stat().st_size != row["bytes"] or hashlib.file_digest(stream, "sha256").hexdigest() != row["sha256"]:
            raise StorageError("The bundled protected runtime failed verification. Reinstall the translator.")
    return path
