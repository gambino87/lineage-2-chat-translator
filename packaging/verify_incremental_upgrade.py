"""Validate real frozen file updates and rollback in disposable private copies.

Only the frozen application's --self-test entry point is executed. This tool
never uses an installed application's registration, starts an installer or GUI,
or opens a runtime service or model. All native harnesses and reports exist only
inside one TemporaryDirectory and are removed before this command returns.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))

from app_updater import _powershell, _signed_release
from build_environment import clean_environment, windows_directories
from incremental_package import blob_path, read_file, verify_package
from incremental_update import changed_files, decode_blob, file_matches, plain_path, validate_descriptor
from secure_storage import guard_private_tree

MAX_REPORT_BYTES = 64 * 1024
MAX_RELEASE_MANIFEST_BYTES = 8 * 1024 * 1024
CHILD_TIMEOUT_SECONDS = 180
EXECUTABLE = "Lineage2ChatTranslator.exe"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with plain_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bounded_read(path, limit):
    path = plain_path(path)
    require(path.is_file() and path.stat().st_size <= limit, "Missing or oversized validation input.")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    require(len(content) <= limit, "Validation input grew while reading.")
    return content


def inventory(folder):
    folder = plain_path(folder)
    require(folder.is_dir(), "A frozen application folder is missing.")
    result = {}
    pending = [folder]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            path = plain_path(path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                continue
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                    "The validation payload contains a special file or hard link.")
            relative = path.relative_to(folder).as_posix()
            require(relative.casefold() not in result, "Frozen application paths collide on Windows.")
            result[relative.casefold()] = {"path": relative, "bytes": info.st_size, "sha256": digest(path)}
    return result


def product_version(executable):
    """Read Windows PE version resources without starting the executable."""
    api = ctypes.WinDLL("version.dll", use_last_error=True, winmode=0x00000800)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    ignored = wintypes.DWORD()
    length = api.GetFileVersionInfoSizeW(str(executable), ctypes.byref(ignored))
    require(length > 0, "The frozen executable has no Windows version resource.")
    data = ctypes.create_string_buffer(length)
    require(api.GetFileVersionInfoW(str(executable), 0, length, data), "Cannot read executable version resource.")
    pointer, size = ctypes.c_void_p(), wintypes.UINT()
    require(api.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(size))
            and size.value >= 4, "The executable version translation is missing.")
    translation = (ctypes.c_ushort * 2).from_address(pointer.value)
    query = f"\\StringFileInfo\\{translation[0]:04x}{translation[1]:04x}\\ProductVersion"
    require(api.VerQueryValueW(data, query, ctypes.byref(pointer), ctypes.byref(size))
            and size.value > 1, "The executable product version is missing.")
    version = ctypes.wstring_at(pointer.value, size.value - 1)
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is not None,
            "The executable product version is invalid.")
    return version


_HARNESS = r'''
$fixture=Read-Record $ConfigPath
$job=$fixture.job
$stage=Assert-PlainPath $fixture.stage
$root=Assert-PlainPath $fixture.root
$pathLocks=New-UpdatePathLocks
$probedVersions=[Collections.Generic.List[string]]::new()
function Test-InstalledProgram([string[]]$AllowedVersions) {
    if($fixture.inject_failure -and $AllowedVersions -ccontains $job.version) {
        throw 'Injected target self-test failure for rollback verification.'
    }
    $executable=Assert-PlainPath $job.executable
    Assert-Condition ([Diagnostics.FileVersionInfo]::GetVersionInfo($executable).ProductVersion -cin $AllowedVersions) 'Unexpected frozen fixture version.'
    $reportPath=Assert-PlainPath ([IO.Path]::Combine($stage,'frozen-self-test.json'))
    if([IO.File]::Exists($reportPath)){[IO.File]::Delete($reportPath)}
    $probe=Start-VerifiedProcess $executable @('--self-test','--output',$reportPath) $true
    try {
        if(-not $probe.WaitForExit(60000)) {
            $probe.Kill()
            $null=$probe.WaitForExit(5000)
            throw 'The owned headless fixture self-test timed out.'
        }
        Assert-Condition ($probe.ExitCode -eq 0) 'The real frozen fixture self-test failed.'
        $report=Read-Record $reportPath
        Assert-Condition ($report.ok -eq $true -and $report.frozen -eq $true -and
            $report.windows_opened -eq $false -and $report.network_requests -eq $false -and
            $AllowedVersions -ccontains $report.version) 'The frozen fixture report failed its headless/version checks.'
        $probedVersions.Add($report.version)
    }finally{$probe.Dispose()}
    return $executable
}
try {
    $pathLocks.HoldDirectoryPath($stage)
    $pathLocks.HoldDirectoryPath($root)
    Assert-PrivateUpdateDirectory $stage
    Assert-PrivateUpdateDirectory $root
    Assert-InstalledVersion $job.executable
    Assert-SignedRelease
    $rejected=$false
    try {$null=Invoke-IncrementalUpdate $root} catch {
        if(-not $fixture.inject_failure){throw}
        Assert-Condition ($_.Exception.Message -match 'Injected target self-test failure') 'Rollback was triggered by an unexpected validation failure.'
        $rejected=$true
    }
    if($fixture.inject_failure){
        Assert-Condition $rejected 'The injected target failure was not rejected.'
        $transaction=Read-Record ([IO.Path]::Combine($stage,'transaction.json')) $maxTransactionBytes
        Assert-Condition ($transaction.state -ceq 'rolled_back' -and $restartPermitted -and -not $preserveStage) 'The real frozen rollback did not complete safely.'
        Assert-InstalledVersion $job.executable
        $null=Test-InstalledProgram @($job.from_version)
    }else{
        $transaction=Read-Record ([IO.Path]::Combine($stage,'transaction.json')) $maxTransactionBytes
        Assert-Condition ($transaction.state -ceq 'committed') 'The real frozen transaction was not committed.'
    }
    @{ok=$true;scenario=$fixture.scenario;state=$transaction.state;frozen_self_tests=$probedVersions.ToArray();
      registry_accessed=$false;installer_executed=$false;windows_opened=$false;services_started=$false;network_requests=$false}|ConvertTo-Json -Compress -Depth 4
}finally{$pathLocks.Dispose()}
'''


def copy_payload(source, destination, records):
    for row in records.values():
        original = plain_path(source / row["path"])
        target = plain_path(destination / row["path"])
        require(target.is_relative_to(destination), "The fixture copy escaped its temporary folder.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        require(target.stat().st_size == row["bytes"] and digest(target) == row["sha256"],
                "Frozen payload changed while copying the private fixture.")


def run_scenario(base, scenario, previous, old_records, manifest, release, metadata,
                 signature, old_version, runner_declarations, environment, repair_paths):
    scenario_root = base / scenario
    scenario_root.mkdir()
    app, stage, preferences = (scenario_root / name for name in ("program", "stage", "preferences"))
    with ExitStack() as held:
        for path in (app, stage, preferences):
            held.enter_context(guard_private_tree(path, create=True, allow_locked_ancestors=True))
        copy_payload(previous, app, old_records)
        sentinel_files = {"user-data/settings.json": b'{"fixture":"preserve preferences"}',
                          "user-data/models/synthetic-model.bin": b"preserve synthetic downloaded model"}
        for relative, content in sentinel_files.items():
            target = scenario_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if scenario == "success":
            (app / repair_paths[0]).unlink()
            (app / repair_paths[1]).write_bytes(b"synthetic corrupted program artifact")
        before = inventory(app)
        changed = changed_files(app, manifest)
        if scenario == "success":
            require(set(repair_paths) <= {row["path"] for row in changed},
                    "Missing and corrupted artifacts were not selected for repair.")
        staged_hashes = set()
        transfer_bytes = 0
        for row in changed:
            if row["sha256"] in staged_hashes:
                continue
            compressed = read_file(release, blob_path(row), row["blob"]["bytes"])
            raw = decode_blob(compressed, row)
            (stage / (row["sha256"] + ".payload")).write_bytes(raw)
            staged_hashes.add(row["sha256"])
            transfer_bytes += len(compressed)
        (stage / "files.json").write_bytes(bounded_read(
            release / f"updates/{manifest['version']}/files.json", 2 * 1024 * 1024))
        (stage / "release.json").write_bytes(metadata)
        (stage / "release.sig").write_bytes(signature)
        public = json.loads(metadata)
        config = {
            "scenario": scenario, "inject_failure": scenario == "rollback", "stage": str(stage), "root": str(app),
            "job": {"app_id": "Lineage2ChatTranslator", "schema_version": 1, "nonce": "a" * 32,
                    "version": manifest["version"], "from_version": old_version,
                    "installer_file": public["installer"]["file"], "installer_bytes": public["installer"]["bytes"],
                    "installer_sha256": public["installer"]["sha256"], "executable": str(app / EXECUTABLE),
                    "preferences": str(preferences), "mode": "incremental"},
        }
        config_path, script = scenario_root / "fixture.json", scenario_root / "fixture.ps1"
        config_path.write_text(json.dumps(config, ensure_ascii=True), encoding="utf-8")
        script.write_text('param([string]$ConfigPath)\n' + runner_declarations + "\n" + _HARNESS,
                          encoding="utf-8-sig")
        child_environment = dict(environment)
        isolated_environment = {"APPDATA": scenario_root / "profile/AppData/Roaming",
                                "LOCALAPPDATA": scenario_root / "profile/AppData/Local",
                                "USERPROFILE": scenario_root / "profile",
                                "TEMP": scenario_root / "temporary", "TMP": scenario_root / "temporary"}
        for key in tuple(child_environment):
            if key.upper() in isolated_environment:
                del child_environment[key]
        for key, path in isolated_environment.items():
            path.mkdir(parents=True, exist_ok=True)
            child_environment[key] = str(path)
        completed = subprocess.run([
            str(_powershell()), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-ConfigPath", str(config_path),
        ], cwd=scenario_root, env=child_environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=CHILD_TIMEOUT_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        require(completed.returncode == 0, "The isolated native update failed: " + completed.stdout + completed.stderr)
        require(len(completed.stdout.encode("utf-8")) <= MAX_REPORT_BYTES, "The native validation report is oversized.")
        report = json.loads(completed.stdout)
        require(report.get("ok") is True, "The native update did not report success.")
        if scenario == "rollback":
            require(inventory(app) == before == old_records, "Rollback did not restore the exact original frozen payload.")
            require(not any(preferences.iterdir()), "Rollback left installed-update receipts behind.")
            require(report["frozen_self_tests"] == [old_version], "The restored old frozen self-test did not run.")
        else:
            expected = {row["path"].casefold(): {key: row[key] for key in ("path", "bytes", "sha256")}
                        for row in manifest["files"]}
            require(inventory(app) == expected, "The successful update differs from the exact signed target payload.")
            require(all(file_matches(app / row["path"], row) for row in manifest["files"]),
                    "A signed target file failed final verification.")
            receipts = {"installed-files.json": (stage / "files.json").read_bytes(),
                        "installed-release.json": metadata, "installed-release.sig": signature}
            require({path.name for path in preferences.iterdir()} == set(receipts),
                    "Installed update receipts are incomplete or unexpected.")
            require(all((preferences / name).read_bytes() == content for name, content in receipts.items()),
                    "Installed update receipts differ from the signed target metadata.")
            require(report["frozen_self_tests"] == [manifest["version"]], "The real new frozen self-test did not run.")
        require(all((scenario_root / relative).read_bytes() == content
                    for relative, content in sentinel_files.items()), "The update changed a settings/model sentinel.")
        require(not any(path.name.endswith(".l2ct-" + "a" * 32) for path in app.rglob("*")),
                "The update left an adjacent temporary program file.")
        return {**report, "selected_files": len(changed), "distinct_file_downloads": len(staged_hashes),
                "compressed_download_bytes": transfer_bytes, "settings_and_models_preserved": True,
                "exact_payload_verified": True, "missing_and_corrupted_files_repaired": scenario == "success"}


def verify(old_app, new_app, release):
    require(os.name == "nt", "Native file-update validation requires Windows.")
    old_app, new_app, release = map(plain_path, (old_app.absolute(), new_app.absolute(), release.absolute()))
    build_root = plain_path(ROOT / ".build")
    require(old_app != new_app and old_app.is_relative_to(build_root) and new_app.is_relative_to(build_root),
            "Use two distinct frozen build outputs inside this workspace's .build folder.")
    old_version, new_version = product_version(old_app / EXECUTABLE), product_version(new_app / EXECUTABLE)
    require(tuple(map(int, old_version.split("."))) < tuple(map(int, new_version.split("."))),
            "The target frozen application must be newer than the baseline.")
    require(release == plain_path(ROOT / "release" / new_version), "Use the canonical new version's release folder.")
    metadata = bounded_read(release / "release.json", 64 * 1024)
    signature = bounded_read(release / "release.sig", 384)
    signed = _signed_release(metadata, signature)
    require(signed.version == new_version, "The signed release differs from the target frozen executable.")
    public = json.loads(metadata)
    descriptor = validate_descriptor(public.get("incremental"), new_version)
    release_manifest = json.loads(bounded_read(release / "release-manifest.json", MAX_RELEASE_MANIFEST_BYTES))
    old_manifest = json.loads(bounded_read(ROOT / "release" / old_version / "release-manifest.json",
                                           MAX_RELEASE_MANIFEST_BYTES))
    require(release_manifest.get("version") == new_version and old_manifest.get("version") == old_version,
            "The canonical release manifest version is invalid.")
    old_records, new_records = inventory(old_app), inventory(new_app)
    require(old_records == {row["path"].casefold(): row for row in old_manifest["payload"]},
            "The old frozen payload differs from its canonical release inventory.")
    require(new_records == {row["path"].casefold(): row for row in release_manifest["payload"]},
            "The new frozen payload differs from its canonical release inventory.")
    manifest, _ = verify_package(release, descriptor, new_version, expected_payload=release_manifest["payload"])
    stable_artifacts = sorted(row["path"] for row in manifest["files"]
                              if 0 < row["bytes"] <= 64 * 1024
                              and Path(row["path"]).suffix.casefold() not in {".exe", ".dll", ".pyd"}
                              and old_records.get(row["path"].casefold(), {}).get("sha256") == row["sha256"])
    require(len(stable_artifacts) >= 2, "The fixture needs two unchanged small artifacts for missing/corrupt repair.")
    runner = bounded_read(new_app / "_internal/assets/apply-update.ps1", 128 * 1024).decode("utf-8-sig")
    require("\ntry {\n    $checkedJob" in runner, "The packaged runner declaration boundary is missing.")
    declarations = runner.split("\ntry {\n    $checkedJob", 1)[0].replace(
        "param([Parameter(Mandatory = $true)][string]$JobPath)", "")
    windows, system = windows_directories()
    environment = clean_environment(os.environ, windows=windows, system=system)
    temp_parent = plain_path(Path(tempfile.gettempdir()))
    with tempfile.TemporaryDirectory(prefix="l2-frozen-incremental-", dir=temp_parent) as temporary:
        base = plain_path(Path(temporary))
        require(base.parent == temp_parent and base.name.startswith("l2-frozen-incremental-"),
                "The native validation temporary folder escaped its scoped root.")
        with guard_private_tree(base, authorize_existing=lambda path: path == base and not any(path.iterdir()),
                                allow_locked_ancestors=True):
            results = [run_scenario(base, scenario, old_app, old_records, manifest, release, metadata, signature,
                                    old_version, declarations, environment, stable_artifacts[:2])
                       for scenario in ("rollback", "success")]
        # Reject a substituted link before TemporaryDirectory performs its owned cleanup.
        inventory(base)
    require(not base.exists(), "Temporary native test diagnostics were not completely removed.")
    require(inventory(old_app) == old_records and inventory(new_app) == new_records,
            "A source frozen build changed during isolated validation.")
    return {"ok": True, "from_version": old_version, "version": new_version,
            "kind": "isolated real frozen incremental update and rollback", "scenarios": results,
            "repair_artifacts": stable_artifacts[:2], "real_installation_touched": False,
            "windows_opened": False, "services_started": False, "network_requests": False,
            "temporary_diagnostics_removed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-app", required=True, type=Path)
    parser.add_argument("--new-app", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.old_app, arguments.new_app, arguments.release), indent=2))


if __name__ == "__main__":
    main()
