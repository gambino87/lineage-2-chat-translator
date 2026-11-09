"""Controlled Windows dependency discovery for the pinned Python 3.12 build.

Windows 10/11 provides UCRT and API-set contracts. We retain the application
runtime and VC runtime libraries, but never package a development tool's copies
of OS compatibility libraries merely because its folder is on PATH.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys

EXPECTED_DLLS = frozenset({
    "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll", "tcl86t.dll",
    "tk86t.dll", "libcrypto-3.dll", "libssl-3.dll", "libffi-8.dll", "zlib1.dll",
})
_OS_RUNTIME = re.compile(r"(?:api-ms-win-(?:core|crt)-[^/\\]+|ucrtbase)\.dll\Z", re.I)
_ENV_DROP = frozenset({"PATH", "TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH", "_MEIPASS2",
                       "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "VIRTUAL_ENV"})


def windows_directories() -> tuple[Path, Path]:
    """Ask Windows instead of trusting a caller-provided SystemRoot variable."""
    if os.name != "nt":
        raise ValueError("Windows build environment required.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    result = []
    for name in ("GetWindowsDirectoryW", "GetSystemDirectoryW"):
        function = getattr(kernel, name)
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        function.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        result.append(Path(buffer.value).resolve(strict=True))
    return tuple(result)


def clean_environment(inherited: dict, *, windows: Path, system: Path,
                      python_root: Path | None = None, interpreter: Path | None = None,
                      cache: Path | None = None) -> dict:
    """Build discovery can use pinned Python; frozen validation uses OS paths only."""
    environment = {key: value for key, value in inherited.items()
                   if key.upper() not in _ENV_DROP
                   and not key.upper().startswith(("PYTHON", "PYINSTALLER_", "_PYI_"))}
    paths = [system, windows]
    if python_root is not None:
        if interpreter is None:
            raise ValueError("The build interpreter is required with a Python root.")
        paths = [interpreter.parent, python_root, python_root / "DLLs", *paths]
    for path in paths:
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Build search paths must be existing absolute directories.")
    environment["PATH"] = os.pathsep.join(dict.fromkeys(str(path.resolve()) for path in paths))
    # These two Windows variables are case-insensitive; discard inherited casing.
    for key in tuple(environment):
        if key.upper() in {"SYSTEMROOT", "WINDIR"}:
            del environment[key]
    environment["SystemRoot"] = str(windows)
    environment["WINDIR"] = str(windows)
    if cache is not None:
        if not cache.is_absolute():
            raise ValueError("The isolated build cache must be absolute.")
        environment["PYINSTALLER_CONFIG_DIR"] = str(cache)
    return environment


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def select_binaries(entries, approved_roots: list[Path]) -> tuple[list, dict]:
    """Reject foreign origins before narrowly omitting OS-supplied libraries."""
    roots = [root.resolve(strict=True) for root in approved_roots]
    kept, records, omitted = [], [], []
    for entry in entries:
        name, filename, kind = entry
        if kind not in {"BINARY", "EXTENSION"}:
            raise ValueError(f"Unexpected native dependency type: {kind}")
        source = Path(filename).resolve(strict=True)
        if not source.is_file() or not any(source.is_relative_to(root) for root in roots):
            raise ValueError(f"Native dependency came from outside the build toolchain: {name}")
        record = {"name": name, "source": str(source), "kind": kind,
                  "bytes": source.stat().st_size, "sha256": _digest(source)}
        if _OS_RUNTIME.fullmatch(Path(name).name):
            omitted.append(record)
        else:
            kept.append(entry)
            records.append(record)
    return kept, {"retained": records, "os_provided": omitted}


def verify_bundled_binaries(app: Path, audit: dict) -> dict:
    """Check exact DLL inventory and byte provenance after PyInstaller collects it."""
    files = list((app / "_internal").rglob("*.dll"))
    names = {path.name.casefold() for path in files}
    if names != EXPECTED_DLLS or len(files) != len(EXPECTED_DLLS):
        raise ValueError(f"Unexpected DLL inventory: {sorted(names ^ EXPECTED_DLLS)}")
    collected = {path.relative_to(app / "_internal").as_posix().casefold()
                 for path in (app / "_internal").rglob("*")
                 if path.is_file() and path.suffix.casefold() in {".dll", ".pyd"}}
    audited = set()
    for record in audit["retained"]:
        name = record["name"].replace("\\", "/")
        if (any(part in {"", ".", ".."} for part in name.split("/"))
                or any(char in name for char in ':*?"<>|')
                or name.casefold() in audited):
            raise ValueError("Invalid or duplicate audited native dependency path.")
        audited.add(name.casefold())
    if collected != audited:
        raise ValueError("Collected native dependencies do not match the complete provenance inventory.")
    for record in audit["retained"]:
        destination = app / "_internal" / record["name"]
        if (not destination.is_file() or destination.stat().st_size != record["bytes"]
                or _digest(destination) != record["sha256"]):
            raise ValueError(f"Collected dependency does not match its approved source: {record['name']}")
    return {"dll_count": len(files), "dll_bytes": sum(path.stat().st_size for path in files),
            "dlls": sorted(names), "native_origins_verified": True,
            "windows_runtime_bundled": False}


def write_spec(path: Path, *, source: Path, version_file: Path, policy_directory: Path,
               approved_roots: list[Path], audit_path: Path) -> None:
    """Use PyInstaller's Analysis list to exclude OS DLLs before collection."""
    text = f'''# Generated build recipe; not installed with the application.
import sys
import json
from pathlib import Path
sys.path.insert(0, {str(policy_directory)!r})
from build_environment import select_binaries
a = Analysis([{str(source / 'installer_main.py')!r}], pathex=[{str(source)!r}],
    binaries=[], datas=[({str(source / 'assets')!r}, 'assets')], hiddenimports=[],
    hookspath=[], hooksconfig={{}}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0)
a.binaries, dependency_audit = select_binaries(a.binaries, [Path(p) for p in {[str(p) for p in approved_roots]!r}])
Path({str(audit_path)!r}).write_text(json.dumps(dependency_audit, indent=2) + '\\n', encoding='utf-8')
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Lineage2ChatTranslator',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, contents_directory='_internal',
    version={str(version_file)!r}, icon=[{str(source / 'assets/L2CT.ico')!r}])
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name='Lineage2ChatTranslator')
'''
    path.write_text(text, encoding="utf-8")
