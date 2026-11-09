"""Exercise retirement of an actually loaded DLL in disposable Windows fixtures.

This is a headless build check, not an interactive installer test. The holder
loads a copied historical UCRT with the Windows loader; no real installation,
registry, game, model or application window is touched.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
from build_environment import windows_directories

# Reuse the stricter fixture boundaries and payload comparisons from the normal
# upgrade check. Keep this separate so a mocked deletion cannot stand in for the
# Windows loader lock exercised here.
_spec = importlib.util.spec_from_file_location("_l2_upgrade_fixture", ROOT / "packaging/verify_dll_upgrade.py")
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
_build_path = _fixture._build_path
_plain_path = _fixture._plain_path
_require = _fixture._require
_inventory = _fixture._inventory
_copy_payload = _fixture._copy_payload
_remove_fixture = _fixture._remove_fixture
_run_self_test = _fixture._run_self_test
CLEANUP_PLAN_NAME = _fixture.CLEANUP_PLAN_NAME
read_cleanup_plan = _fixture.read_cleanup_plan

HOLDER_START_TIMEOUT_SECONDS = 10
HOLDER_EXIT_TIMEOUT_SECONDS = 10
HELPER_TIMEOUT_SECONDS = 45
MAX_HELPER_REPORT_BYTES = 1024 * 1024
POLL_SECONDS = 0.05
UCRT_PATH = "_internal/ucrtbase.dll"
EXPECTED_OLD_DLLS = _fixture.EXPECTED_OLD_DLLS
EXPECTED_CURRENT_DLLS = _fixture.EXPECTED_CURRENT_DLLS
EXPECTED_RETIRED_DLLS = _fixture.EXPECTED_RETIRED_DLLS

_HOLDER = r'''
import ctypes
import json
import os
from pathlib import Path
import sys

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint]
kernel.LoadLibraryExW.restype = ctypes.c_void_p
kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
kernel.GetModuleFileNameW.restype = ctypes.c_uint
kernel.FreeLibrary.argtypes = [ctypes.c_void_p]
kernel.FreeLibrary.restype = ctypes.c_int
dll, ready = map(Path, sys.argv[1:])
# Restrict dependencies to this DLL's directory and System32. An absolute name
# also prevents an already loaded system UCRT from satisfying this request.
handle = kernel.LoadLibraryExW(str(dll), None, 0x00000100 | 0x00000800)
if not handle:
    raise ctypes.WinError(ctypes.get_last_error())
try:
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetModuleFileNameW(handle, buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    actual = Path(buffer.value).resolve(strict=True)
    if actual != dll.resolve(strict=True):
        raise RuntimeError("Windows loaded a different DLL than the fixture")
    marker = ready.with_suffix(".writing")
    marker.write_text(json.dumps({"pid": os.getpid(), "loaded_path": str(actual)}), encoding="utf-8")
    marker.replace(ready)
    if sys.stdin.readline().strip() != "release":
        raise RuntimeError("The DLL holder did not receive its normal release signal")
finally:
    if not kernel.FreeLibrary(handle):
        raise ctypes.WinError(ctypes.get_last_error())
print("released", flush=True)
'''

_CLEANUP_LAUNCHER = r'''
import ctypes
import json
from pathlib import Path
import subprocess
import sys

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
kernel.SetDllDirectoryW.restype = ctypes.c_int
kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p]
kernel.GetDllDirectoryW.restype = ctypes.c_uint
directory, job_path = map(Path, sys.argv[1:])
job = json.loads(job_path.read_text(encoding="utf-8"))
# Reproduce the search directory inherited from a frozen app. Mirror Inno's
# reset before spawning the external cleanup, rather than relying on cwd/PATH.
if not kernel.SetDllDirectoryW(str(directory)):
    raise ctypes.WinError(ctypes.get_last_error())
buffer = ctypes.create_unicode_buffer(32768)
if not kernel.GetDllDirectoryW(len(buffer), buffer) or Path(buffer.value) != directory:
    raise RuntimeError("Could not establish the inherited frozen DLL directory")
if not kernel.SetDllDirectoryW(""):
    raise ctypes.WinError(ctypes.get_last_error())
if kernel.GetDllDirectoryW(len(buffer), buffer) != 0:
    raise RuntimeError("The inherited DLL search directory was not cleared")
completed = subprocess.run(job["arguments"], cwd=job["cwd"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=job["timeout"])
sys.exit(completed.returncode)
'''


@contextmanager
def hold_loaded_dll(dll: Path, ready: Path, child_env: dict):
    """Hold only a copied DLL in one owned, hidden child, then release it normally."""
    _require(os.name == "nt", "The real DLL lock check requires Windows.")
    dll, ready = _build_path(dll), _build_path(ready)
    _require(dll.is_file() and not ready.exists(), "Invalid DLL holder fixture.")
    # A Windows venv executable may be a redirector that creates a second PID.
    # Invoke this interpreter's base executable so the owned PID is the holder.
    executable = _plain_path(Path(getattr(sys, "_base_executable", sys.executable)).absolute())
    process = subprocess.Popen(
        [str(executable), "-I", "-c", _HOLDER, str(dll), str(ready)],
        cwd=ready.parent, env=dict(child_env), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    release = {"normal_exit": False}
    try:
        deadline = time.monotonic() + HOLDER_START_TIMEOUT_SECONDS
        while not ready.exists():
            _require(process.poll() is None, "The owned DLL holder failed before becoming ready.")
            _require(time.monotonic() < deadline, "The DLL holder did not become ready in time.")
            time.sleep(POLL_SECONDS)
        _plain_path(ready)
        marker = json.loads(ready.read_text(encoding="utf-8"))
        _require(marker.get("pid") == process.pid
                 and _plain_path(Path(marker["loaded_path"])) == dll,
                 "The owned child did not load the exact fixture DLL.")
        yield release
    finally:
        try:
            stdout, stderr = process.communicate(input="release\n", timeout=HOLDER_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # This PID is exclusively owned by the verifier; never find or stop
            # other application/service processes to make cleanup succeed.
            process.kill()
            process.communicate(timeout=HOLDER_EXIT_TIMEOUT_SECONDS)
            raise ValueError("The owned DLL holder failed to exit after release.")
        release["normal_exit"] = process.returncode == 0 and stdout.strip() == "released"
        _require(release["normal_exit"], "The owned DLL holder failed: " + stderr[-2000:])


def require_os_delete_lock(dll: Path) -> int:
    """Prove the Windows deletion failure instead of manufacturing an exception."""
    dll = _build_path(dll)
    try:
        dll.unlink()
    except PermissionError as exc:
        _require(getattr(exc, "winerror", None) in {5, 32, 33} and dll.is_file(),
                 "The fixture did not fail deletion with a Windows sharing/access error.")
        return exc.winerror
    raise ValueError("Windows allowed deletion of the supposedly loaded fixture DLL.")


def _run_helper(script: Path, app: Path, output: Path, version: str, child_env: dict) -> dict:
    """Call the shipped cleanup core from system PowerShell, outside the payload."""
    script, app, output = _plain_path(script), _build_path(app), _build_path(output)
    _, system = windows_directories()
    powershell = _plain_path(system / "WindowsPowerShell/v1.0/powershell.exe")
    wrapper = _build_path(output.with_suffix(".ps1"))
    wrapper.write_text(r'''param(
    [string]$FixtureScript, [string]$FixtureRoot, [string]$FixtureVersion, [string]$FixtureReport
)
$ErrorActionPreference = 'Stop'
. $FixtureScript
function Get-FixtureModuleEvidence {
    $modules = @([Diagnostics.Process]::GetCurrentProcess().Modules)
    $prefix = [IO.Path]::GetFullPath($FixtureRoot).TrimEnd('\') + '\'
    return @{
        helper_ucrt_modules = @($modules | Where-Object { $_.ModuleName -ieq 'ucrtbase.dll' } | ForEach-Object { $_.FileName })
        helper_modules_from_install = @($modules | Where-Object { $_.FileName.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { $_.ModuleName })
    }
}
try {
    $plan = Read-CleanupPlan -Path (Join-Path $FixtureRoot '_internal\assets\obsolete-program-files.json') -ExpectedVersion $FixtureVersion
    $result = Invoke-ProgramFileCleanup -Root $FixtureRoot -Plan $plan -ExpectedVersion $FixtureVersion
    $moduleEvidence = Get-FixtureModuleEvidence
    $result | Add-Member -NotePropertyName helper_ucrt_modules -NotePropertyValue $moduleEvidence.helper_ucrt_modules
    $result | Add-Member -NotePropertyName helper_modules_from_install -NotePropertyValue $moduleEvidence.helper_modules_from_install
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $FixtureReport -Encoding UTF8
    if (-not $result.ok) { exit 1 }
    exit 0
} catch {
    $failure = @{ok=$false;error=$_.Exception.Message}
    $moduleEvidence = Get-FixtureModuleEvidence
    $failure.helper_ucrt_modules = $moduleEvidence.helper_ucrt_modules
    $failure.helper_modules_from_install = $moduleEvidence.helper_modules_from_install
    $failure | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $FixtureReport -Encoding UTF8
    exit 1
}
''', encoding="utf-8")
    arguments = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper), "-FixtureScript", str(script), "-FixtureRoot", str(app),
         "-FixtureVersion", version, "-FixtureReport", str(output)]
    job = _build_path(output.with_suffix(".job.json"))
    job.write_text(json.dumps({"arguments": arguments, "cwd": str(output.parent),
                               "timeout": HELPER_TIMEOUT_SECONDS - HOLDER_EXIT_TIMEOUT_SECONDS}), encoding="utf-8")
    interpreter = _plain_path(Path(getattr(sys, "_base_executable", sys.executable)).absolute())
    completed = subprocess.run(
        [str(interpreter), "-I", "-c", _CLEANUP_LAUNCHER, str(app / "_internal"), str(job)],
        cwd=output.parent, env=dict(child_env), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=HELPER_TIMEOUT_SECONDS,
    )
    _plain_path(output)
    with output.open("rb") as source:
        data = source.read(MAX_HELPER_REPORT_BYTES + 1)
    _require(len(data) <= MAX_HELPER_REPORT_BYTES, "The external cleanup report is too large.")
    result = json.loads(data.decode("utf-8-sig"))
    _require(completed.returncode in {0, 1} and isinstance(result, dict)
             and isinstance(result.get("ok"), bool)
             and result["ok"] == (completed.returncode == 0),
             "External cleanup produced an invalid report.")
    _require(result.get("helper_modules_from_install") == [],
             "External cleanup loaded a native library from the installed payload.")
    result["inherited_dll_directory_cleared"] = True
    result["exit_code"] = completed.returncode
    return result


def _run_dirty_self_test(app: Path, output: Path, version: str, child_env: dict) -> dict:
    """A dirty frozen upgrade must fail its headless check before cleanup."""
    executable, output = _build_path(app / "Lineage2ChatTranslator.exe"), _build_path(output)
    completed = subprocess.run(
        [str(executable), "--self-test", "--output", str(output)],
        cwd=output.parent, env=dict(child_env), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=_fixture.SELF_TEST_TIMEOUT_SECONDS,
    )
    _plain_path(output)
    with output.open("rb") as source:
        data = source.read(_fixture.MAX_SELF_TEST_BYTES + 1)
    _require(len(data) <= _fixture.MAX_SELF_TEST_BYTES, "The dirty self-test report is too large.")
    report = json.loads(data.decode("utf-8-sig"))
    _require(completed.returncode == 1 and report.get("ok") is False
             and report.get("version") == version
             and "obsolete app-local Windows runtime" in report.get("error", ""),
             "The dirty frozen self-test did not detect the leftover app-local UCRT.")
    return report


def verify_locked_upgrade(previous_app: Path, current_app: Path, work_root: Path,
                          version: str, *, child_env: dict) -> dict:
    """Validate a historical payload overlay while an actual UCRT mapping is held."""
    previous_app, current_app, work_root = map(_build_path, (previous_app, current_app, work_root))
    _require(previous_app != current_app, "Locked upgrade validation needs distinct builds.")
    _require(isinstance(child_env, dict) and child_env.get("PATH"), "A clean child environment is required.")
    before, current = _inventory(previous_app), _inventory(current_app)
    plan = read_cleanup_plan(current_app / "_internal/assets" / CLEANUP_PLAN_NAME, version)
    _require({p.casefold() for p in plan["current_paths"]} == set(current), "Current payload inventory mismatch.")
    old_dlls = {key for key in before if key.endswith(".dll")}
    current_dlls = {key for key in current if key.endswith(".dll")}
    retired = old_dlls - current_dlls
    _require(len(old_dlls) == EXPECTED_OLD_DLLS and len(current_dlls) == EXPECTED_CURRENT_DLLS
             and len(retired) == EXPECTED_RETIRED_DLLS and UCRT_PATH in retired,
             "The real lock check requires the 52-to-9 historical DLL reduction.")
    obsolete = {row["path"].casefold(): row for row in plan["obsolete_files"]}
    _require(retired <= set(obsolete) and all(before[key]["sha256"] in obsolete[key]["hashes"] for key in retired),
             "The cleanup plan does not recognize every retired historical DLL.")
    _require(set(before) - set(current) == retired, "Unexpected historical non-DLL retirement in lock fixture.")
    script = _plain_path(current_app / "_internal/assets/cleanup-program-files.ps1")
    _require(script.is_file(), "The current package has no external cleanup helper.")
    work_root.mkdir(parents=True, exist_ok=True)
    fixture = _build_path(Path(tempfile.mkdtemp(prefix="dll-upgrade-locked-", dir=work_root)))
    try:
        upgraded = fixture / "upgraded"
        _copy_payload(previous_app, upgraded, before)
        _copy_payload(current_app, upgraded, current)
        dll = upgraded / UCRT_PATH
        dirty_self_test = _run_dirty_self_test(upgraded, fixture / "dirty-self-test.json", version, child_env)
        with hold_loaded_dll(dll, fixture / "holder-ready.json", child_env) as holder:
            os_error = require_os_delete_lock(dll)
            locked = _run_helper(script, upgraded, fixture / "locked.json", version, child_env)
            _require(locked["exit_code"] == 1 and dll.is_file(), "The external helper hid a locked-DLL failure.")
            locked_inventory = _inventory(upgraded)
            remaining = set(locked_inventory) - set(current)
            _require(UCRT_PATH in remaining and remaining <= retired
                     and all(locked_inventory.get(key) == value for key, value in current.items()),
                     "Locked cleanup changed current files or unexpectedly removed the loaded UCRT.")
        after = _run_helper(script, upgraded, fixture / "released.json", version, child_env)
        _require(holder["normal_exit"] and after["exit_code"] == 0 and not dll.exists()
                 and {p.casefold() for p in after["removed"]} == remaining,
                 "The external helper did not delete UCRT after the owned holder released it: "
                 + str(after.get("error", "unexpected remaining files")))
        _require(_inventory(upgraded) == current, "Released cleanup differs from the clean current payload.")
        again = _run_helper(script, upgraded, fixture / "repeated.json", version, child_env)
        _require(again["exit_code"] == 0 and not again["removed"] and not again["preserved"],
                 "Repeated external cleanup was not idempotent.")
        self_test = _run_self_test(upgraded, fixture / "self-test.json", version, child_env)
        _require(_inventory(upgraded) == current, "The upgraded self-test changed program files.")

        # The user's partial v1.7.4 upgrade has only UCRT left. Exercise this
        # separate starting state so success does not depend on other DLLs.
        partial = fixture / "partial-upgrade"
        _copy_payload(current_app, partial, current)
        _copy_payload(previous_app, partial, {UCRT_PATH: before[UCRT_PATH]})
        with hold_loaded_dll(partial / UCRT_PATH, fixture / "partial-holder-ready.json", child_env) as partial_holder:
            partial_os_error = require_os_delete_lock(partial / UCRT_PATH)
            partial_locked = _run_helper(script, partial, fixture / "partial-locked.json", version, child_env)
            _require(partial_locked["exit_code"] == 1 and (partial / UCRT_PATH).exists(),
                     "Partial upgrade did not report the loaded UCRT cleanup failure.")
        partial_after = _run_helper(script, partial, fixture / "partial-released.json", version, child_env)
        _require(partial_holder["normal_exit"] and partial_after["exit_code"] == 0
                 and {p.casefold() for p in partial_after["removed"]} == {UCRT_PATH}
                 and _inventory(partial) == current,
                 "Partial upgrade did not reach the exact nine-DLL payload after release.")
        partial_self_test = _run_self_test(partial, fixture / "partial-self-test.json", version, child_env)
        _require(_inventory(partial) == current, "The partial upgraded self-test changed program files.")

        protected = fixture / "protected"
        _copy_payload(previous_app, protected, before)
        _copy_payload(current_app, protected, current)
        changed_key = sorted(retired - {UCRT_PATH})[0]
        sentinels = {
            "settings.json": b'{"fixture":"keep settings"}',
            "models/fixture-model.bin": b"keep model",
            "logs/fixture.log": b"keep log",
            "game/system/Engine.dll": b"keep game file",
            "_internal/unrecognized-library.dll": b"keep unknown library",
            before[changed_key]["path"]: b"modified historical library; preserve",
        }
        _require(not {p.casefold() for p in sentinels} & set(current), "Sentinel collided with current payload.")
        for relative, content in sentinels.items():
            path = _plain_path(protected / relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        preservation = _run_helper(script, protected, fixture / "protected.json", version, child_env)
        _require(preservation["exit_code"] == 0
                 and {p.casefold() for p in preservation["removed"]} == retired - {changed_key}
                 and {p.casefold() for p in preservation["preserved"]} == {changed_key},
                 "External cleanup failed to preserve the modified historical DLL.")
        for relative, content in sentinels.items():
            _require((protected / relative).read_bytes() == content, "Cleanup modified a protected synthetic file.")
        protected_inventory = _inventory(protected)
        _require(all(protected_inventory.get(key) == value for key, value in current.items()),
                 "Protection check modified required current payload files.")
        return {
            "ok": True, "version": version,
            "kind": "isolated real Windows loaded-DLL upgrade check; not interactive Inno execution",
            "old_dll_count": len(old_dlls), "current_dll_count": len(current_dlls),
            "retired_dll_count": len(retired), "actual_loaded_dll": UCRT_PATH,
            "os_deletion_error_while_loaded": os_error,
            "helper_reports_locked_failure": True, "holder_exited_normally": holder["normal_exit"],
            "inherited_dll_directory_cleared": after["inherited_dll_directory_cleared"],
            "helper_modules_from_install": after["helper_modules_from_install"],
            "removed_before_release": sorted(retired - remaining),
            "removed_after_release": after["removed"], "exact_current_payload_after_cleanup": True,
            "cleanup_idempotent": True, "modified_retired_dll_preserved": True,
            "synthetic_protected_files_preserved": sorted(sentinels),
            "dirty_frozen_self_test": dirty_self_test,
            "upgraded_frozen_self_test": self_test,
            "partial_upgrade": {
                "starting_retired_dlls": [UCRT_PATH], "os_deletion_error_while_loaded": partial_os_error,
                "helper_reports_locked_failure": True, "holder_exited_normally": partial_holder["normal_exit"],
                "removed_after_release": partial_after["removed"], "exact_current_payload_after_cleanup": True,
                "frozen_self_test": partial_self_test,
            },
            "real_installation_touched": False, "windows_opened": False,
        }
    finally:
        _remove_fixture(fixture, work_root)
