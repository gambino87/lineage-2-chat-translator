"""Exercise the actual Windows PowerShell cleanup against disposable fixtures.

No installed app, registry value, service or application window is changed.
The real loaded-ucrtbase reproduction is also run by the release verifier.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "assets" / "cleanup-program-files.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
VERSION = "1.7.5"
OLD_BYTES = b"unchanged retired program file"
OLD_HASH = hashlib.sha256(OLD_BYTES).hexdigest()


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Windows PowerShell cleanup integration")
class ExternalCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="l2-cleanup-ps-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "installation with spaces"
        self.root.mkdir()
        self.write("Lineage2ChatTranslator.exe", b"fixture launcher; never executed")
        self.write("unins000.exe", b"fixture uninstaller; never executed")
        self.write("_internal/current.dll", b"current required file")
        self.plan_path = self.root / "_internal/assets/obsolete-program-files.json"
        self.result_path = self.base / "result.json"

    def write(self, relative, data=OLD_BYTES):
        path = self.root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def entry(self, path, hashes=None):
        return {"path": path, "hashes": hashes or [OLD_HASH]}

    def plan(self, *entries):
        return {
            "schema_version": 1, "product": "Lineage 2 Chat Translator", "version": VERSION,
            "current_paths": ["Lineage2ChatTranslator.exe", "_internal/current.dll", "_internal/assets/obsolete-program-files.json"],
            "obsolete_files": list(entries),
        }

    def run_cleanup(self, plan=None, *, extra="", body=None, helper=HELPER, root=None):
        if plan is not None:
            self.write("_internal/assets/obsolete-program-files.json", json.dumps(plan).encode("utf-8"))
        wrapper = self.base / "run-fixture.ps1"
        wrapper.write_text(
            "param([string]$HelperPath,[string]$Root,[string]$PlanPath,[string]$ResultPath)\n"
            "$fixtureResult=$ResultPath\n. $HelperPath\n$ResultPath=$fixtureResult\n"
            "$exitCode=0\ntry {\n" + extra + "\n" + (body or
            f"$plan=Read-CleanupPlan -Path $PlanPath -ExpectedVersion '{VERSION}'\n"
            f"$result=Invoke-ProgramFileCleanup -Root $Root -Plan $plan -ExpectedVersion '{VERSION}'\n") +
            "\n} catch { $exitCode=1; $result=[pscustomobject]@{ok=$false;version='" + VERSION + "';removed=@();preserved=@();error=$_.Exception.Message} }\n"
            "Write-CleanupResult -Path $ResultPath -Result $result\nexit $exitCode\n",
            encoding="utf-8-sig",
        )
        completed = subprocess.run(
            [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper), "-HelperPath", str(helper), "-Root", str(root or self.root),
             "-PlanPath", str(self.plan_path), "-ResultPath", str(self.result_path)],
            cwd=self.base, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertTrue(self.result_path.is_file(), (completed.stdout, completed.stderr))
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode == 0, result["ok"], (result, completed.stderr))
        return result

    def test_unchanged_files_removed_modified_and_private_data_preserved(self):
        old = self.write("_internal/retired/sub/old.dll")
        license_file = self.write("licenses/retired/LICENSE.txt")
        modified = self.write("_internal/modified.txt", b"local modification")
        settings = self.write("settings.json", b"private settings")
        model = self.write("models/model.bin", b"private model")
        result = self.run_cleanup(self.plan(self.entry("_internal/retired/sub/old.dll"), self.entry("licenses/retired/LICENSE.txt"), self.entry("_internal/modified.txt")))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["removed_count"], 2)
        self.assertEqual(result["preserved"], ["_internal/modified.txt"])
        self.assertFalse(old.exists())
        self.assertFalse(license_file.exists())
        self.assertFalse((self.root / "_internal/retired").exists())
        self.assertTrue((self.root / "_internal").is_dir())
        self.assertTrue((self.root / "licenses").is_dir())
        self.assertEqual(modified.read_bytes(), b"local modification")
        self.assertEqual(settings.read_bytes(), b"private settings")
        self.assertEqual(model.read_bytes(), b"private model")
        again = self.run_cleanup()
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["removed_count"], 0)

    def test_any_known_release_hash_matches(self):
        old = self.write("_internal/old.dll")
        result = self.run_cleanup(self.plan(self.entry("_internal/old.dll", ["0" * 64, OLD_HASH])))
        self.assertTrue(result["ok"], result)
        self.assertFalse(old.exists())

    def test_modified_retired_binaries_block_cleanup_until_owner_removes_them(self):
        for name in ('_internal/ucrtbase.dll', '_internal/old.PYD'):
            with self.subTest(name=name):
                changed = self.write(name, b'harmless modified fixture; never loaded')
                result = self.run_cleanup(self.plan(self.entry(name)))
                self.assertFalse(result['ok'], result)
                self.assertIn('manually', result['error'])
                self.assertEqual(changed.read_bytes(), b'harmless modified fixture; never loaded')
                changed.unlink()  # The owner retains ordinary file control.
                self.assertTrue(self.run_cleanup()['ok'])

    def test_full_plan_rejected_before_deletion_for_unsafe_targets(self):
        old = self.write("_internal/old.dll")
        unsafe = [
            "../escape.dll", "/absolute.dll", "C:/absolute.dll", "_internal/a/../b.dll",
            "_internal//a.dll", "_internal\\a.dll", "_internal/a.dll:stream", "_internal/*.dll",
            "_internal/a.dll.", "_internal/a.dll ", "_internal/CON.dll", "_internal/LPT².dll",
            "_internal/CONOUT$", "_internal/a\x7f.dll", "settings.json", "_internal/settings.json",
            "_internal/models/data", "_internal/logs/data", "_internal/report.local.json",
            "_internal/Network.log", "_internal/old.exe", "licenses/unins000.dat", "other/file.dll",
            "_internal/CURRENT.dll", "_internal/current.dll/child", "_internal",
        ]
        # One PowerShell process validates all cases without launching an app.
        cases = [self.plan(self.entry("_internal/old.dll"), self.entry(path)) for path in unsafe]
        cases_path = self.base / "cases.json"
        cases_path.write_text(json.dumps(cases), encoding="utf-8")
        body = """
$cases=ConvertFrom-Json ([IO.File]::ReadAllText([IO.Path]::Combine([IO.Path]::GetDirectoryName($ResultPath),'cases.json')))
foreach($case in $cases) {
    $rejected=$false
    try { $null=Invoke-ProgramFileCleanup -Root $Root -Plan $case -ExpectedVersion '1.7.5' }
    catch { $rejected=$true }
    if(-not $rejected) { throw ('Unsafe target was accepted: '+$case.obsolete_files[-1].path) }
    if(-not [IO.File]::Exists([IO.Path]::Combine($Root,'_internal\\old.dll'))) { throw 'A later unsafe target allowed partial deletion.' }
}
$result=[pscustomobject]@{ok=$true;version='1.7.5';removed=@();preserved=@();error=$null}
"""
        result = self.run_cleanup(body=body)
        self.assertTrue(result["ok"], result)
        self.assertTrue(old.exists())

    def test_bad_schema_hashes_and_path_conflicts_reject_entire_plan(self):
        old = self.write("_internal/old.dll")
        cases = []
        for key, value in (("schema_version", True), ("product", "Another app"), ("product", ["Lineage 2 Chat Translator"]), ("version", "1.7.4"), ("version", [VERSION]), ("current_paths", []), ("obsolete_files", {})):
            plan = self.plan(self.entry("_internal/old.dll"))
            plan[key] = value
            cases.append(plan)
        for hashes in (["A" * 64], ["bad"], [], [OLD_HASH] * 129):
            plan = self.plan(self.entry("_internal/old.dll"))
            plan["obsolete_files"][0]["hashes"] = hashes
            cases.append(plan)
        cases.append(self.plan(self.entry("_internal/old.dll"), self.entry("_internal/OLD.dll")))
        cases.append(self.plan(self.entry("_internal/parent"), self.entry("_internal/parent/child.dll")))
        plan = self.plan(self.entry("_internal/old.dll"))
        plan["current_paths"].append("_internal/CURRENT.dll")
        cases.append(plan)
        plan = self.plan(self.entry("_internal/old.dll"))
        plan["current_paths"].append("_internal/current.dll/child")
        cases.append(plan)
        (self.base / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        result = self.run_cleanup(body="""
$cases=ConvertFrom-Json ([IO.File]::ReadAllText([IO.Path]::Combine([IO.Path]::GetDirectoryName($ResultPath),'cases.json')))
foreach($case in $cases) {
    $rejected=$false
    try { $null=Invoke-ProgramFileCleanup -Root $Root -Plan $case -ExpectedVersion '1.7.5' } catch {$rejected=$true}
    if(-not $rejected) {throw 'Invalid plan was accepted.'}
}
$result=[pscustomobject]@{ok=$true;version='1.7.5';removed=@();preserved=@();error=$null}
""")
        self.assertTrue(result["ok"], result)
        self.assertTrue(old.exists())

    def test_json_duplicate_fields_and_invalid_utf8_rejected(self):
        payloads = [
            json.dumps(self.plan()).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1').encode(),
            json.dumps(self.plan()).replace('"version": "1.7.5"', '"version": "1.7.5", "\\u0076ersion": "1.7.5"').encode(),
            b'{"schema_version":1,"bad":"\xff"}',
        ]
        for payload in payloads:
            with self.subTest(payload=payload[:80]):
                self.write("_internal/assets/obsolete-program-files.json", payload)
                result = self.run_cleanup()
                self.assertFalse(result["ok"], result)

    def test_bounded_plan_read_and_hash_size(self):
        self.write("_internal/assets/obsolete-program-files.json", b" " * (2 * 1024 * 1024 + 1))
        result = self.run_cleanup()
        self.assertFalse(result["ok"], result)
        self.assertIn("too large", result["error"])
        old = self.write("_internal/large.dll")
        result = self.run_cleanup(self.plan(self.entry("_internal/large.dll")), extra="$script:CleanupFileBytes=4")
        self.assertFalse(result["ok"], result)
        self.assertIn('manually', result['error'])
        self.assertTrue(old.exists())

    def test_directory_target_preflight_prevents_partial_deletion(self):
        old = self.write("_internal/old.dll")
        (self.root / "_internal/not-a-file.dll").mkdir()
        result = self.run_cleanup(self.plan(self.entry("_internal/old.dll"), self.entry("_internal/not-a-file.dll")))
        self.assertFalse(result["ok"], result)
        self.assertTrue(old.exists())

    def test_junction_ancestor_and_junction_root_rejected(self):
        import _winapi
        external = self.base / "outside"
        external.mkdir()
        external_file = external / "old.dll"
        external_file.write_bytes(OLD_BYTES)
        junction = self.root / "_internal/redirect"
        _winapi.CreateJunction(str(external), str(junction))
        self.addCleanup(junction.rmdir)  # Delete the junction itself, never its target.
        old = self.write("_internal/old.dll")
        result = self.run_cleanup(self.plan(self.entry("_internal/old.dll"), self.entry("_internal/redirect/old.dll")))
        self.assertFalse(result["ok"], result)
        self.assertTrue(old.exists())
        self.assertEqual(external_file.read_bytes(), OLD_BYTES)
        root_link = self.base / "linked-installation"
        _winapi.CreateJunction(str(self.root), str(root_link))
        self.addCleanup(root_link.rmdir)
        result = self.run_cleanup(self.plan(self.entry("_internal/old.dll")), root=root_link)
        self.assertFalse(result["ok"], result)
        self.assertTrue(old.exists())

    def test_real_file_lock_fails_then_release_allows_retry(self):
        old = self.write("_internal/ucrtbase.dll")
        result = self.run_cleanup(self.plan(self.entry("_internal/ucrtbase.dll")), body="""
$plan=Read-CleanupPlan -Path $PlanPath -ExpectedVersion '1.7.5'
$locked=[IO.File]::Open([IO.Path]::Combine($Root,'_internal\\ucrtbase.dll'),[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
try {
    $rejected=$false
    try {$null=Invoke-ProgramFileCleanup -Root $Root -Plan $plan -ExpectedVersion '1.7.5'}
    catch {$rejected=$true; if($_.Exception.Message -notmatch 'retry') {throw}}
    if(-not $rejected) {throw 'Cleanup ignored an active Windows file lock.'}
    if(-not [IO.File]::Exists([IO.Path]::Combine($Root,'_internal\\ucrtbase.dll'))) {throw 'Locked file disappeared.'}
} finally {$locked.Dispose()}
$result=Invoke-ProgramFileCleanup -Root $Root -Plan $plan -ExpectedVersion '1.7.5'
""")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["removed"], ["_internal/ucrtbase.dll"])
        self.assertFalse(old.exists())

    def test_cloud_tags_only_allow_known_non_redirecting_family(self):
        result = self.run_cleanup(body="""
$null=Test-CleanupCloudPlaceholder $Root
foreach($hex in @('9000001A','9000101A','9000F01A')) {
    if(-not [L2Cleanup.FileTags]::IsCloudTag([Convert]::ToUInt32($hex,16))) {throw 'Known cloud tag rejected.'}
}
foreach($hex in @('00000000','A000000C','A0000003','80000021','9001001A','B000001A')) {
    if([L2Cleanup.FileTags]::IsCloudTag([Convert]::ToUInt32($hex,16))) {throw 'Unknown or redirecting reparse tag accepted.'}
}
# A read-only check also exercises real hydrated OneDrive ancestors on hosts
# where the source workspace lives there; it writes nothing to that folder.
$null=Assert-CleanupPlainPath $HelperPath
$result=[pscustomobject]@{ok=$true;version='1.7.5';removed=@();preserved=@();error=$null}
""")
        self.assertTrue(result["ok"], result)

    def test_registered_root_binding_with_test_scope_registry_stub(self):
        extra = """
function Get-ItemProperty([string]$LiteralPath) {
    if($LiteralPath -ne $script:CleanupRegistryKey) {throw 'Unexpected registry path.'}
    [pscustomobject]@{DisplayName='Lineage 2 Chat Translator 1.7.5';DisplayVersion='1.7.5';InstallLocation=$Root}
}
"""
        result = self.run_cleanup(extra=extra, body="""
$null=Assert-RegisteredInstallation -Root $Root -Version '1.7.5'
foreach($bad in @([IO.Path]::Combine($Root,'elsewhere'),[IO.Path]::GetDirectoryName($Root))) {
    $rejected=$false
    try {$null=Assert-RegisteredInstallation -Root $bad -Version '1.7.5'} catch {$rejected=$true}
    if(-not $rejected) {throw 'Mismatched registered root was accepted.'}
}
$rejected=$false
try {$null=Assert-RegisteredInstallation -Root $Root -Version '1.7.4'} catch {$rejected=$true}
if(-not $rejected) {throw 'Mismatched registered version was accepted.'}
$result=[pscustomobject]@{ok=$true;version='1.7.5';removed=@();preserved=@();error=$null}
""")
        self.assertTrue(result["ok"], result)

    def test_mutex_guards_in_isolated_namespace_and_updater_allowed(self):
        suffix = uuid.uuid4().hex
        text = HELPER.read_text(encoding="utf-8")
        app_guard = "Local\\L2ProminenceChatTranslator"
        uninstall_guard = "Local\\Lineage2ChatTranslatorUninstall"
        self.assertIn(app_guard, text)
        self.assertIn(uninstall_guard, text)
        helper = self.base / "isolated-guards.ps1"
        # Only the mutex namespace changes in this private copy. Real translator
        # and uninstall guards are never created/opened by this test.
        helper.write_text(text.replace(app_guard, app_guard + suffix).replace(uninstall_guard, uninstall_guard + suffix), encoding="utf-8-sig")
        extra = "function Assert-RegisteredInstallation([string]$Root,[string]$Version) {return $Root}\n"
        body = f"""
foreach($name in @('{app_guard + suffix}','{uninstall_guard + suffix}')) {{
    $holder=[Threading.Mutex]::new($false,$name)
    try {{
        $rejected=$false
        try {{$null=Invoke-RegisteredCleanup -Root $Root -Version '1.7.5'}} catch {{$rejected=$true}}
        if(-not $rejected) {{throw 'Existing app/uninstall guard was ignored.'}}
    }} finally {{$holder.Dispose()}}
}}
$updater=[Threading.Mutex]::new($false,'Local\\Lineage2ChatTranslatorUpdate{suffix}')
try {{$result=Invoke-RegisteredCleanup -Root $Root -Version '1.7.5'}} finally {{$updater.Dispose()}}
"""
        result = self.run_cleanup(self.plan(), extra=extra, body=body, helper=helper)
        self.assertTrue(result["ok"], result)

    def test_result_log_is_bounded_but_counts_remain_complete(self):
        result = self.run_cleanup(body="""
$paths=@(1..10000 | ForEach-Object {('_internal/'+('x'*900)+'/'+$_+'.dll')})
$result=[pscustomobject]@{ok=$true;version='1.7.5';removed=$paths;preserved=@();error=$null}
""")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["removed_count"], 10000)
        self.assertEqual(len(result["removed"]), 12)
        self.assertLess(self.result_path.stat().st_size, 65536)


if __name__ == "__main__":
    unittest.main()
