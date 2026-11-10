"""Verified uninstall of file-update additions uses disposable native fixtures."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

import secure_storage


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "assets/cleanup-program-files.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
VERSION = "99.8.7"


def entry(path, content=b"synthetic packaged file"):
    return {"path": path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Windows verified uninstall integration")
class IncrementalUninstallTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="l2-incremental-uninstall-"))).resolve()
        self.root = self.base / "installation with spaces"
        self.root.mkdir()
        self.executable = self.write("Lineage2ChatTranslator.exe")
        self.write("unins000.exe", b"inert uninstaller; never executed")
        self.plan_path = self.base / "uninstall-files.json"
        self.output = self.base / "result.json"

    def write(self, name, content=b"synthetic packaged file"):
        path = self.root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def plan(self, *entries):
        return {"schema_version": 1, "product": "Lineage 2 Chat Translator", "version": VERSION,
                "files": [entry("Lineage2ChatTranslator.exe"), *entries]}

    def full_plan(self, *entries, legacy=False):
        self.preferences = self.base / "profile" / ("L2Prominence/ChatTranslator" if legacy else "Lineage2ChatTranslator")
        self.preferences.mkdir(parents=True, exist_ok=True)
        self.receipt_data = {"installed-files.json": b"synthetic verified file inventory",
                             "installed-release.json": b"synthetic signed release metadata",
                             "installed-release.sig": b"x" * 384}
        for name, data in self.receipt_data.items():
            (self.preferences / name).write_bytes(data)
        # Match the private ACL retained when updater-stage receipts move into
        # preferences; permissions are changed only in this disposable fixture.
        with secure_storage.guard_private_tree(self.preferences, authorize_existing=lambda _: True,
                                                allow_locked_ancestors=True):
            pass
        document = self.plan(*entries)
        document["preferences"] = str(self.preferences)
        document["receipts"] = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                for name, data in self.receipt_data.items()}
        return document

    def run_cleanup(self, document=None, *, body=None):
        if document is not None:
            self.plan_path.write_bytes(document if isinstance(document, bytes) else json.dumps(document).encode())
        wrapper = self.base / "run-fixture.ps1"
        wrapper.write_text(
            "param([string]$Helper,[string]$Root,[string]$PlanPath,[string]$Output)\n"
            ". $Helper\n$exitCode=0\ntry {\n" +
            (body or "$plan=Read-IncrementalUninstallPlan $PlanPath\n$result=Invoke-IncrementalUninstallCleanup $Root $plan") +
            "\n} catch { $exitCode=1; $result=[pscustomobject]@{ok=$false;error=$_.Exception.Message} }\n"
            "[IO.File]::WriteAllText($Output,($result | ConvertTo-Json -Depth 10 -Compress),[Text.UTF8Encoding]::new($false))\n"
            "exit $exitCode\n", encoding="utf-8-sig")
        environment = os.environ.copy()
        environment["APPDATA"] = str(self.base / "profile")
        completed = subprocess.run(
            [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper), "-Helper", str(HELPER), "-Root", str(self.root),
             "-PlanPath", str(self.plan_path), "-Output", str(self.output)],
            cwd=self.base, env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertTrue(self.output.is_file(), (completed.stdout, completed.stderr))
        result = json.loads(self.output.read_text())
        self.assertEqual(completed.returncode == 0, result["ok"], (result, completed.stderr))
        return result

    def test_removes_verified_files_added_after_initial_install_and_preserves_unknowns(self):
        added = self.write("_internal/new/package/added.dll")
        notice = self.write("licenses/new/LICENSE.txt")
        modified = self.write("_internal/modified.dll", b"owner modified runtime")
        unknown = self.write("_internal/owner-notes.txt", b"owner notes")
        settings = self.write("settings.json", b"private settings")
        model = self.write("models/model.bin", b"private model")
        result = self.run_cleanup(self.plan(entry("_internal/new/package/added.dll"),
                                           entry("licenses/new/LICENSE.txt"), entry("_internal/modified.dll")))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["removed"], ["Lineage2ChatTranslator.exe", "_internal/new/package/added.dll", "licenses/new/LICENSE.txt"])
        self.assertEqual(result["preserved"], ["_internal/modified.dll"])
        self.assertFalse(self.executable.exists())
        self.assertFalse(added.exists())
        self.assertFalse(notice.exists())
        self.assertFalse((self.root / "_internal/new").exists())
        self.assertTrue((self.root / "_internal").is_dir())
        self.assertTrue((self.root / "licenses").is_dir())
        self.assertEqual(modified.read_bytes(), b"owner modified runtime")
        self.assertEqual(unknown.read_bytes(), b"owner notes")
        self.assertEqual(settings.read_bytes(), b"private settings")
        self.assertEqual(model.read_bytes(), b"private model")
        self.assertTrue((self.root / "unins000.exe").is_file())

    def test_missing_files_and_repeated_cleanup_are_idempotent(self):
        document = self.plan(entry("_internal/missing.dll"))
        self.assertTrue(self.run_cleanup(document)["ok"])
        again = self.run_cleanup(document)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["removed"], [])

    def test_readonly_verified_file_is_removed_without_changing_unknown_file(self):
        matched = self.write("_internal/readonly.dll")
        modified = self.write("_internal/changed.dll", b"owner bytes")
        matched.chmod(stat.S_IREAD)
        modified.chmod(stat.S_IREAD)
        self.addCleanup(lambda: modified.chmod(stat.S_IWRITE) if modified.exists() else None)
        self.addCleanup(lambda: matched.chmod(stat.S_IWRITE) if matched.exists() else None)
        result = self.run_cleanup(self.plan(entry("_internal/readonly.dll"), entry("_internal/changed.dll")))
        self.assertTrue(result["ok"], result)
        self.assertFalse(matched.exists())
        self.assertEqual(modified.read_bytes(), b"owner bytes")
        self.assertTrue(modified.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)

    def test_invalid_later_target_blocks_all_deletions(self):
        (self.root / "_internal/directory.dll").mkdir(parents=True)
        result = self.run_cleanup(self.plan(entry("_internal/directory.dll")))
        self.assertFalse(result["ok"], result)
        self.assertTrue(self.executable.exists())

    def test_hardlink_target_is_rejected_before_deleting_other_files(self):
        outside = self.base / "outside.dll"
        outside.write_bytes(b"synthetic packaged file")
        linked = self.root / "_internal/hardlink.dll"
        linked.parent.mkdir()
        os.link(outside, linked)
        result = self.run_cleanup(self.plan(entry("_internal/hardlink.dll")))
        self.assertFalse(result["ok"], result)
        self.assertTrue(self.executable.exists())
        self.assertEqual(outside.read_bytes(), b"synthetic packaged file")

    def test_junction_target_is_rejected_before_deleting_other_files(self):
        import _winapi
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "file.dll").write_bytes(b"synthetic packaged file")
        internal = self.root / "_internal"
        internal.mkdir()
        _winapi.CreateJunction(str(outside), str(internal / "redirect"))
        result = self.run_cleanup(self.plan(entry("_internal/redirect/file.dll")))
        self.assertFalse(result["ok"], result)
        self.assertTrue(self.executable.exists())
        self.assertEqual((outside / "file.dll").read_bytes(), b"synthetic packaged file")

    def test_unsafe_private_uninstaller_and_colliding_paths_are_rejected(self):
        names = ("../outside", "/absolute", "C:/outside", "_internal/../outside", "_internal//file",
                 "_internal\\file", "_internal/a:stream", "_internal/*.dll", "_internal/a.", "_internal/a ",
                 "_internal/NUL.dll", "_internal/LPT².dll", "_internal/CONOUT$", "_internal/a\x7fb",
                 "settings.json", "models/model.bin", "_internal/settings.json", "_internal/runtime.json",
                 "_internal/models/model.bin", "_internal/logs/log.txt", "_internal/private.local.json",
                 "_internal/network.log", "_internal/file.bak", "licenses/unins000.dat", "unins000.exe", "other/file.dll",
                 "_internal/.git/config", "_internal/captures/file", "_internal/review_tools/file")
        cases = [self.plan(entry(name)) for name in names]
        cases.extend([self.plan(entry("_internal/one.dll"), entry("_INTERNAL/ONE.DLL")),
                      self.plan(entry("_internal/parent"), entry("_internal/parent/child.dll"))])
        (self.base / "cases.json").write_text(json.dumps(cases))
        result = self.run_cleanup(body="""
$cases=ConvertFrom-Json ([IO.File]::ReadAllText([IO.Path]::Combine([IO.Path]::GetDirectoryName($Output),'cases.json')))
foreach ($case in $cases) {
    $rejected=$false
    try {$null=Invoke-IncrementalUninstallCleanup $Root $case} catch {$rejected=$true}
    if(-not $rejected) {throw ('Unsafe uninstall inventory accepted: '+$case.files[-1].path)}
    if(-not [IO.File]::Exists([IO.Path]::Combine($Root,'Lineage2ChatTranslator.exe'))) {throw 'Unsafe later target allowed earlier deletion.'}
}
$result=[pscustomobject]@{ok=$true;error=$null}
""")
        self.assertTrue(result["ok"], result)

    def test_schema_hash_sizes_and_missing_executable_are_rejected(self):
        cases = []
        for key, value in (("schema_version", True), ("product", "Other app"), ("product", ["Lineage 2 Chat Translator"]),
                           ("version", [VERSION]), ("version", "01.2.3"), ("version", "current"), ("files", [])):
            document = self.plan()
            document[key] = value
            cases.append(document)
        for key, value in (("bytes", True), ("bytes", -1), ("bytes", 128 * 1024 * 1024 + 1),
                           ("sha256", "A" * 64), ("sha256", "bad"), ("extra", True)):
            document = self.plan()
            document["files"][0][key] = value
            cases.append(document)
        document = self.plan()
        document["files"] = [entry("_internal/no-executable.dll")]
        cases.append(document)
        (self.base / "cases.json").write_text(json.dumps(cases))
        result = self.run_cleanup(body="""
$cases=ConvertFrom-Json ([IO.File]::ReadAllText([IO.Path]::Combine([IO.Path]::GetDirectoryName($Output),'cases.json')))
foreach($case in $cases) {
    $rejected=$false
    try {$null=Invoke-IncrementalUninstallCleanup $Root $case} catch {$rejected=$true}
    if(-not $rejected) {throw 'Malformed uninstall inventory accepted.'}
}
$result=[pscustomobject]@{ok=$true;error=$null}
""")
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.executable.exists())

    def test_duplicate_json_fields_and_unreadable_or_oversized_plan_are_rejected(self):
        valid = json.dumps(self.plan()).encode()
        payloads = (valid.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
                    valid.replace(b'"bytes":', b'"bytes": 1, "bytes":', 1), b"{broken", b"\xff",
                    b" " * (2 * 1024 * 1024 + 1))
        for payload in payloads:
            with self.subTest(size=len(payload)):
                result = self.run_cleanup(payload)
                self.assertFalse(result["ok"], result)
                self.assertTrue(self.executable.exists())

    def test_native_file_and_parent_handles_block_replacement_until_delete(self):
        target = self.write("_internal/current.dll")
        document = self.plan(entry("_internal/current.dll"))
        result = self.run_cleanup(document, body="""
$plan=Read-IncrementalUninstallPlan $PlanPath
$file=[IO.Path]::Combine($Root,'_internal\\current.dll')
$guard=New-IncrementalUninstallGuard
try {
    $guard.HoldDirectories($Root)
    $state=$guard.Preflight($file,$plan.files[1].bytes,$plan.files[1].sha256)
    if($state -cne 'matched') {throw 'Synthetic file did not verify.'}
    $blocked=$false
    try {[IO.File]::WriteAllText($file,'changed')} catch {$blocked=$true}
    if(-not $blocked) {throw 'Verified file bytes were replaceable.'}
    $blocked=$false
    try {[IO.File]::Move($file,$file+'.moved')} catch {$blocked=$true}
    if(-not $blocked) {throw 'Verified file path was replaceable.'}
    $blocked=$false
    try {[IO.Directory]::Move([IO.Path]::GetDirectoryName($file),[IO.Path]::GetDirectoryName($file)+'-moved')} catch {$blocked=$true}
    if(-not $blocked) {throw 'Verified parent was replaceable.'}
    $guard.DeleteVerified($file)
} finally {$guard.Dispose()}
$result=[pscustomobject]@{ok=$true;error=$null}
""")
        self.assertTrue(result["ok"], result)
        self.assertFalse(target.exists())
        self.assertTrue(self.executable.exists())

    def run_with_receipts(self, document):
        return self.run_cleanup(document, body="$plan=Read-IncrementalUninstallPlan $PlanPath\n$result=Invoke-IncrementalUninstallWithReceipts $Root $plan")

    def assert_receipts_retained(self):
        for name, content in self.receipt_data.items():
            self.assertEqual((self.preferences / name).read_bytes(), content)

    def test_success_removes_receipts_only_after_program_cleanup_and_empty_preferences(self):
        document = self.full_plan()
        result = self.run_with_receipts(document)
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.executable.exists())
        self.assertFalse(self.preferences.exists())

    def test_failed_program_cleanup_preserves_receipts_and_allows_retry(self):
        (self.root / "_internal/not-a-file.dll").mkdir(parents=True)
        document = self.full_plan(entry("_internal/not-a-file.dll"))
        result = self.run_with_receipts(document)
        self.assertFalse(result["ok"], result)
        self.assertTrue(self.executable.exists())
        self.assert_receipts_retained()
        (self.root / "_internal/not-a-file.dll").rmdir()
        self.write("_internal/not-a-file.dll")
        again = self.run_with_receipts(document)
        self.assertTrue(again["ok"], again)
        self.assertFalse(self.preferences.exists())

    def test_mutated_receipt_blocks_program_deletion_and_preserves_all_receipts(self):
        document = self.full_plan()
        changed = self.preferences / "installed-release.json"
        changed.write_bytes(b"owner changed metadata")
        result = self.run_with_receipts(document)
        self.assertFalse(result["ok"], result)
        self.assertTrue(self.executable.exists())
        self.assertEqual(changed.read_bytes(), b"owner changed metadata")
        for name in ("installed-files.json", "installed-release.sig"):
            self.assertEqual((self.preferences / name).read_bytes(), self.receipt_data[name])

    def test_unknown_preference_files_are_preserved_and_directory_remains(self):
        document = self.full_plan()
        unknown = self.preferences / "owner-notes.txt"
        unknown.write_bytes(b"owner notes")
        result = self.run_with_receipts(document)
        self.assertTrue(result["ok"], result)
        self.assertEqual(unknown.read_bytes(), b"owner notes")
        for name in self.receipt_data:
            self.assertFalse((self.preferences / name).exists())

    def test_legacy_preferences_path_uses_same_verified_receipt_cleanup(self):
        document = self.full_plan(legacy=True)
        result = self.run_with_receipts(document)
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.preferences.exists())

    def test_preferences_injection_or_extra_receipt_name_cannot_delete_private_data(self):
        document = self.full_plan()
        outside = self.base / "outside-preferences"
        outside.mkdir()
        (outside / "settings.json").write_bytes(b"private settings")
        document["preferences"] = str(outside)
        result = self.run_with_receipts(document)
        self.assertFalse(result["ok"], result)
        self.assertTrue(self.executable.exists())
        self.assertEqual((outside / "settings.json").read_bytes(), b"private settings")
        document["preferences"] = str(self.preferences)
        document["receipts"]["settings.json"] = {"bytes": 1, "sha256": "a" * 64}
        result = self.run_with_receipts(document)
        self.assertFalse(result["ok"], result)
        self.assertTrue(self.executable.exists())
        self.assert_receipts_retained()

    def test_private_receipt_handles_remain_pinned_during_program_cleanup(self):
        document = self.full_plan()
        result = self.run_cleanup(document, body="""
$plan=Read-IncrementalUninstallPlan $PlanPath
$original=(Get-Command Invoke-IncrementalUninstallCleanup).ScriptBlock
function Invoke-IncrementalUninstallCleanup([string]$Root,$Plan) {
    $receipt=[IO.Path]::Combine($Plan.preferences,'installed-files.json')
    $blocked=$false
    try {[IO.File]::WriteAllText($receipt,'changed')} catch {$blocked=$true}
    if(-not $blocked) {throw 'Uninstall receipt was mutable during program cleanup.'}
    $blocked=$false
    try {[IO.Directory]::Move($Plan.preferences,$Plan.preferences+'-moved')} catch {$blocked=$true}
    if(-not $blocked) {throw 'Preferences directory was replaceable during cleanup.'}
    return & $original $Root $Plan
}
$result=Invoke-IncrementalUninstallWithReceipts $Root $plan
""")
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.preferences.exists())

    def test_nonprivate_receipt_acl_is_rejected_before_program_deletion(self):
        document = self.full_plan()
        result = self.run_cleanup(document, body="""
$plan=Read-IncrementalUninstallPlan $PlanPath
$receipt=[IO.Path]::Combine($plan.preferences,'installed-files.json')
$security=[IO.File]::GetAccessControl($receipt)
$rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-1-0'),[Security.AccessControl.FileSystemRights]::Read,[Security.AccessControl.AccessControlType]::Allow)
$security.AddAccessRule($rule)
[IO.File]::SetAccessControl($receipt,$security)
$result=Invoke-IncrementalUninstallWithReceipts $Root $plan
""")
        self.assertFalse(result["ok"], result)
        self.assertIn("private", result["error"])
        self.assertTrue(self.executable.exists())
        self.assert_receipts_retained()

    def test_minimal_internal_plan_is_rejected_by_receipt_wrapper(self):
        result = self.run_with_receipts(self.plan())
        self.assertFalse(result["ok"], result)
        self.assertTrue(self.executable.exists())


if __name__ == "__main__":
    unittest.main()
