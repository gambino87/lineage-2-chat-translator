"""Verify the DLL reduction using disposable copies of frozen build outputs.

This is a build-time check, not an installer or a repair command. It never reads
the real install registry or user settings, and never launches the translator
window, Ollama, or a model. Only the frozen application's headless self-test runs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / ".build"
sys.path.insert(0, str(ROOT))
from installation_cleanup import CLEANUP_PLAN_NAME, CleanupError, cleanup_program_files, read_cleanup_plan

EXPECTED_OLD_DLLS = 52
EXPECTED_CURRENT_DLLS = 9
EXPECTED_RETIRED_DLLS = 43
SELF_TEST_TIMEOUT_SECONDS = 45
MAX_SELF_TEST_BYTES = 64 * 1024
REPARSE_POINT = 0x400


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _plain_path(path: Path) -> Path:
    path = Path(path)
    _require(path.is_absolute(), "Upgrade validation paths must be absolute.")
    full = Path(os.path.abspath(path))
    for part in reversed((full, *full.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode)
                 and not getattr(info, "st_file_attributes", 0) & REPARSE_POINT,
                 "Upgrade validation will not follow links or junctions.")
    _require(full.resolve() == full, "Upgrade validation path changed during checking.")
    return full


def _build_path(path: Path) -> Path:
    full = _plain_path(path)
    base = _plain_path(BUILD_ROOT)
    _require(full != base and full.is_relative_to(base),
             "Upgrade validation is restricted to child folders of .build.")
    return full


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _inventory(folder: Path) -> dict[str, dict]:
    """Inspect actual tree entries without following a redirected directory."""
    folder = _plain_path(folder)
    _require(folder.is_dir(), "A frozen build folder is missing.")
    records = {}
    pending = [folder]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            _plain_path(path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                continue
            _require(stat.S_ISREG(info.st_mode), "Unexpected build entry type.")
            relative = path.relative_to(folder).as_posix()
            key = relative.casefold()
            _require(key not in records, "Build paths collide on Windows.")
            records[key] = {"path": relative, "bytes": info.st_size, "sha256": _digest(path)}
    return records


def _copy_payload(source: Path, destination: Path, records: dict[str, dict]) -> None:
    destination = _build_path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for row in records.values():
        original = _plain_path(source / row["path"])
        target = _plain_path(destination / row["path"])
        _require(target.is_relative_to(destination), "Copy target escaped the fixture.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        _require(target.stat().st_size == row["bytes"] and _digest(target) == row["sha256"],
                 "Payload changed while copying the upgrade fixture.")


def _run_self_test(app: Path, output: Path, version: str, child_env: dict) -> dict:
    executable = _build_path(app / "Lineage2ChatTranslator.exe")
    output = _build_path(output)
    subprocess.run(
        [str(executable), "--self-test", "--output", str(output)],
        cwd=output.parent, env=dict(child_env), check=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=SELF_TEST_TIMEOUT_SECONDS,
    )
    _plain_path(output)
    with output.open("rb") as source:
        data = source.read(MAX_SELF_TEST_BYTES + 1)
    _require(len(data) <= MAX_SELF_TEST_BYTES, "The headless report is too large.")
    report = json.loads(data.decode("utf-8-sig"))
    _require(report.get("ok") is True and report.get("frozen") is True
             and report.get("version") == version and report.get("windows_opened") is False
             and report.get("network_requests") is False,
             "The upgraded frozen application failed its headless checks.")
    return {key: value for key, value in report.items() if not key.endswith("_path")}


def _remove_fixture(fixture: Path, work_root: Path) -> None:
    """Remove only this invocation's checked tree; reject new redirects first."""
    fixture = _build_path(fixture)
    work_root = _build_path(work_root)
    _require(fixture.parent == work_root and fixture.name.startswith("dll-upgrade-"),
             "Unexpected upgrade fixture cleanup target.")
    # Full preflight prevents following a substituted junction during removal.
    _inventory(fixture)
    shutil.rmtree(fixture)


def verify_upgrade(previous_app: Path, current_app: Path, work_root: Path,
                   version: str, *, child_env: dict) -> dict:
    """Overlay the new package on a real prior payload and verify retirement.

    Source paths must be frozen build outputs inside this workspace's .build.
    The caller verifies the baseline against its immutable release manifest and
    supplies the same clean environment used for the candidate build/self-test.
    """
    previous_app = _build_path(previous_app)
    current_app = _build_path(current_app)
    work_root = _build_path(work_root)
    _require(previous_app != current_app, "Upgrade validation needs distinct old and new builds.")
    _require(isinstance(child_env, dict) and child_env.get("PATH"),
             "A clean child environment is required for the frozen self-test.")
    before = _inventory(previous_app)
    current = _inventory(current_app)
    plan = read_cleanup_plan(current_app / "_internal/assets" / CLEANUP_PLAN_NAME, version)
    _require({path.casefold() for path in plan["current_paths"]} == set(current),
             "The new cleanup inventory does not match the current payload.")
    old_dlls = {key for key in before if key.endswith(".dll")}
    current_dlls = {key for key in current if key.endswith(".dll")}
    retired = old_dlls - current_dlls
    _require(len(old_dlls) == EXPECTED_OLD_DLLS and len(current_dlls) == EXPECTED_CURRENT_DLLS
             and len(retired) == EXPECTED_RETIRED_DLLS,
             "The expected 52-to-9 DLL reduction was not produced.")
    _require(all(Path(key).name.startswith("api-ms-win-") or Path(key).name == "ucrtbase.dll"
                 for key in retired), "An unexpected core DLL was retired.")
    obsolete = {row["path"].casefold(): row for row in plan["obsolete_files"]}
    _require(retired <= set(obsolete), "Retired DLLs are missing from the upgrade cleanup plan.")
    _require(all(before[key]["sha256"] in obsolete[key]["hashes"] for key in retired),
             "The cleanup plan does not recognize the prior shipped DLL bytes.")
    _require(set(before) - set(current) == retired,
             "This validation expects only compatibility DLLs to be removed from the old payload.")
    work_root.mkdir(parents=True, exist_ok=True)
    fixture = _build_path(Path(tempfile.mkdtemp(prefix="dll-upgrade-", dir=work_root)))
    _require(fixture.parent == work_root, "Temporary upgrade fixture escaped its work folder.")
    try:
        upgraded = fixture / "upgraded"
        _copy_payload(previous_app, upgraded, before)
        _copy_payload(current_app, upgraded, current)
        result = cleanup_program_files(upgraded, plan, version)
        _require({p.casefold() for p in result["removed"]} == retired and not result["preserved"],
                 "Upgrade cleanup did not remove exactly the old compatibility DLLs.")
        _require(_inventory(upgraded) == current, "The cleaned upgrade differs from a clean current install.")
        again = cleanup_program_files(upgraded, plan, version)
        _require(not again["removed"] and not again["preserved"], "Repeated cleanup was not idempotent.")
        self_test = _run_self_test(upgraded, fixture / "self-test.json", version, child_env)
        _require(_inventory(upgraded) == current, "The headless self-test changed program files.")

        # A separate fixture verifies preservation AND a failed cleanup gate.
        # Never execute this fixture: its retired library has unknown bytes.
        protected = fixture / "protected"
        _copy_payload(previous_app, protected, before)
        _copy_payload(current_app, protected, current)
        changed_key = sorted(retired)[0]
        changed_path = before[changed_key]["path"]
        sentinels = {
            "settings.json": b'{"fixture":"keep settings"}',
            "models/fixture-model.bin": b"keep downloaded model",
            "logs/fixture.log": b"keep user log",
            "game/system/Engine.dll": b"keep game file",
            "_internal/unrecognized-library.dll": b"keep unknown program file",
            changed_path: b"user modified historical DLL; preserve",
        }
        _require(not (set(name.casefold() for name in sentinels) & set(current)),
                 "Preservation fixture collided with the new payload.")
        for relative, contents in sentinels.items():
            path = _plain_path(protected / relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        for _ in range(2):
            try:
                cleanup_program_files(protected, plan, version)
            except CleanupError as exc:
                _require(changed_path in str(exc) and "manually" in str(exc),
                         "Modified retired DLL did not produce an actionable cleanup failure.")
            else:
                raise ValueError("Modified retired DLL was incorrectly accepted as a successful cleanup.")
        for relative, contents in sentinels.items():
            _require((protected / relative).read_bytes() == contents,
                     "Upgrade cleanup changed a protected synthetic file.")
        preserved_inventory = _inventory(protected)
        _require(all(preserved_inventory.get(key) == row for key, row in current.items()),
                 "Preservation test changed current program files.")
        return {
            "ok": True, "version": version, "kind": "isolated frozen-payload upgrade simulation",
            "old_dll_count": len(old_dlls), "current_dll_count": len(current_dlls),
            "retired_dll_count": len(retired), "removed_dlls": sorted(result["removed"]),
            "removed_dll_bytes": sum(before[key]["bytes"] for key in retired),
            "exact_current_payload_after_cleanup": True, "current_payload_files": len(current),
            "cleanup_idempotent": True, "modified_retired_dll_preserved": True,
            "modified_retired_dll_blocks_cleanup": True, "modified_fixture_never_executed": True,
            "synthetic_protected_files_preserved": sorted(sentinels),
            "upgraded_frozen_self_test": self_test,
            "real_installation_touched": False, "windows_opened": False,
        }
    finally:
        _remove_fixture(fixture, work_root)
