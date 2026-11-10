"""Exercise the shipped runner's guards without its process/install entry point."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import app_updater
import update_trust


class UpdateRunnerTests(unittest.TestCase):
    def test_progress_window_lifecycle_follows_verified_handoff(self):
        source = app_updater.RUNNER_PATH.read_text(encoding='utf-8')
        main = source.split('\ntry {\n    $checkedJob', 1)[1]
        opened = main.index('$progressWindow = New-UpdateProgressWindow')
        self.assertLess(main.index('$parentExited = $true'), opened)
        self.assertLess(main.index('Assert-SignedRelease'), opened)
        self.assertLess(opened, main.index('$restartExecutable = Invoke-IncrementalUpdate'))
        installer = main.index('$installerProcess = Start-VerifiedProcess')
        self.assertLess(main.index('Close-UpdateProgressWindow'), installer)
        final = main.split("    Set-UpdateProgress 'Finishing update...'", 1)[1]
        self.assertLess(final.index('Close-UpdateProgressWindow'),
                        final.index('$restarted = Start-VerifiedProcess'))
        self.assertLess(final.index('Close-UpdateProgressWindow'),
                        final.index('[Windows.Forms.MessageBox]::Show'))

    @unittest.skipUnless(os.name == 'nt', 'Windows Forms compile-only validation')
    def test_progress_window_compiles_without_creating_a_window(self):
        # Compile the shipped C# and inspect its API. Never call its constructor,
        # start the UI thread or enter the updater's install/process entry point.
        declarations = app_updater.RUNNER_PATH.read_text(encoding='utf-8').split(
            '\ntry {\n    $checkedJob', 1)[0].replace(
                'param([Parameter(Mandatory = $true)][string]$JobPath)', '')
        declarations = declarations.replace('return [L2Update.ProgressWindow]::new()',
                                            'return [L2Update.ProgressWindow]')
        harness = r'''
$type = New-UpdateProgressWindow
if (-not [IDisposable].IsAssignableFrom($type)) { throw 'Progress window cannot be disposed.' }
if ($null -eq $type.GetMethod('Report', [type[]]@([string], [int]))) { throw 'Progress reporting API is missing.' }
'progress-window-compiled-without-opening'
'''
        with tempfile.TemporaryDirectory(prefix='l2-update-progress-') as folder:
            script = Path(folder) / 'compile.ps1'
            script.write_text(declarations + '\n' + harness, encoding='utf-8-sig')
            result = subprocess.run([
                str(app_updater._powershell()), '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(script),
            ], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('progress-window-compiled-without-opening', result.stdout)

    def test_both_verifiers_pin_the_same_key(self):
        source = app_updater.RUNNER_PATH.read_text(encoding='utf-8')
        key = re.search(r"\$releasePublicKeyBlob = '([^']+)'", source).group(1)
        self.assertEqual(key, update_trust.PUBLIC_KEY_BLOB_B64)

    def test_signed_checks_gate_readiness_and_installer_execution(self):
        source = app_updater.RUNNER_PATH.read_text(encoding='utf-8')
        main = source.split('\ntry {\n    $checkedJob', 1)[1]
        ready = main.index("'ready.json'")
        exited = main.index('$originalProcess.WaitForExit')
        started = main.index('$installerProcess = Start-VerifiedProcess')
        self.assertLess(main.index('Assert-SignedRelease'), ready)
        self.assertLess(exited, main.rindex('Assert-SignedRelease'))
        self.assertLess(main.rindex('Assert-SignedRelease'), started)
        self.assertLess(main.rindex('Assert-InstalledVersion'), started)
        self.assertLess(main.index('$installerLock = Open-VerifiedInstaller'), started)
        self.assertGreater(main.index('$installerLock.Dispose()'), started)

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell integration')
    def test_failed_installer_cleanup_cannot_launch_installed_program(self):
        source = app_updater.RUNNER_PATH.read_text(encoding='utf-8')
        declarations = source.split('\ntry {\n    $checkedJob', 1)[0].replace(
            'param([Parameter(Mandatory = $true)][string]$JobPath)', '')
        # Exercise the actual installer-result branch with inert process doubles.
        # No installer, installed EXE, registry, service or GUI is touched.
        branch = source.split('    $installerProcess = $null\n', 1)[1].split(
            '    $restartExecutable = Test-InstalledProgram @($job.version)\n', 1)[0]
        branch += '    $restartExecutable = Test-InstalledProgram @($job.version)\n'
        harness = r'''
function Start-VerifiedProcess {
    $fake=[pscustomobject]@{ExitCode=$script:fixtureExit}
    $fake | Add-Member ScriptMethod WaitForExit {}
    $fake | Add-Member ScriptMethod Dispose {}
    return $fake
}
function Test-InstalledProgram { $script:probes++; return 'fixture-never-launched.exe' }
function Assert-PrivateUpdateDirectory {}
$pathLocks=[pscustomobject]@{}
$pathLocks | Add-Member ScriptMethod ReleaseDescendantsForInstaller {}
$pathLocks | Add-Member ScriptMethod ReleaseForInstaller {}
$pathLocks | Add-Member ScriptMethod HoldDirectoryPath {}
$executable='C:\fixture\launcher.exe'
$job=[pscustomobject]@{version='99.8.7'}
$installer='fixture-never-executed.exe'
$arguments=@()
foreach($code in @(4,1,3010,0)) {
    $script:fixtureExit=$code
    $script:probes=0
    $restartPermitted=$true
    $installerProcess=$null
    $installerLock=[pscustomobject]@{}
    $installerLock | Add-Member ScriptMethod Dispose {}
    try {
__BRANCH__
    } catch {
        # This is the shipped recovery condition, with a confirmed handoff and
        # exited parent. False must prevent both its self-test and GUI restart.
        if($restartPermitted) { $null=Test-InstalledProgram @('1.7.12','99.8.7') }
    }
    if($code -eq 0) {
        if(-not $restartPermitted -or $script:probes -ne 1) {throw 'Successful installer did not permit one probe.'}
    } elseif($restartPermitted -or $script:probes -ne 0) {throw 'Failed installer allowed installed code execution.'}
}
'installer-restart-guards-passed'
'''.replace('__BRANCH__', branch)
        with tempfile.TemporaryDirectory(prefix='l2-runner-result-') as folder:
            script = Path(folder) / 'guard.ps1'
            script.write_text(declarations + '\n' + harness, encoding='utf-8-sig')
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            result = subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
                                     '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                    capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertIn(b'installer-restart-guards-passed', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Windows native path-lock integration')
    def test_runner_keeps_paths_stable_through_cleanup_and_rejects_root_substitution(self):
        declarations = app_updater.RUNNER_PATH.read_text(encoding='utf-8').split(
            '\ntry {\n    $checkedJob', 1)[0].replace(
                'param([Parameter(Mandatory = $true)][string]$JobPath)', '')
        harness = r'''
function Protect-Fixture([string]$Path) {
    $current=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $acl=[Security.AccessControl.DirectorySecurity]::new()
    $acl.SetSecurityDescriptorSddlForm(('D:P(A;OICI;FA;;;{0})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)' -f $current),[Security.AccessControl.AccessControlSections]::Access)
    [IO.Directory]::SetAccessControl($Path,$acl)
}
$updates=[IO.Path]::Combine($TestDirectory,'updates')
$stage=[IO.Path]::Combine($updates,'update-'+('a'*32))
$install=[IO.Path]::Combine($TestDirectory,'installation')
$null=[IO.Directory]::CreateDirectory($stage)
$null=[IO.Directory]::CreateDirectory($install)
Protect-Fixture $updates
Protect-Fixture $stage
Protect-Fixture $install
$pathLocks=New-UpdatePathLocks
try {
    $pathLocks.HoldDirectoryPath($stage)
    Assert-PrivateUpdateDirectory $updates
    Assert-PrivateUpdateDirectory $stage
    $blocked=$false
    try {[IO.Directory]::Move($stage,$stage+'-moved')} catch {$blocked=$true}
    if(-not $blocked) {throw 'Guard allowed stage replacement.'}
    [IO.File]::WriteAllText([IO.Path]::Combine($stage,'installer.part'),'inert fixture')
    [IO.File]::Move([IO.Path]::Combine($stage,'installer.part'),[IO.Path]::Combine($stage,'installer.exe'))
    $pathLocks.HoldDirectoryPath($install)
    Assert-PrivateUpdateDirectory $install
    $pathLocks.ReleaseForInstaller($install)
    [IO.Directory]::Move($install,$install+'-original')
    $null=[IO.Directory]::CreateDirectory($install)
    Protect-Fixture $install
    $blocked=$false
    try {$pathLocks.HoldDirectoryPath($install)} catch {$blocked=$true}
    if(-not $blocked) {throw 'Replaced installation root was accepted.'}
    [IO.Directory]::Delete($install)
    [IO.Directory]::Move($install+'-original',$install)
    $pathLocks.HoldDirectoryPath($install)
    $job=[pscustomobject]@{nonce=('a'*32);version='99.8.7'}
    $log=[IO.Path]::Combine($stage,'installer.log')
    $report=@{ok=$false;version='99.8.7';error='Retired program file _internal/ucrtbase.dll differs. Remove it manually.'}
    [IO.File]::WriteAllText($log,'2026-01-01 Program file cleanup: '+($report | ConvertTo-Json -Compress),[Text.Encoding]::Unicode)
    if((Get-InstallerCleanupExplanation) -notmatch 'ucrtbase.dll.*manually') {throw 'Specific cleanup explanation was lost.'}
    $maxInstallerLogBytes=8
    if((Get-InstallerCleanupExplanation) -ne '') {throw 'Oversized diagnostic log was accepted.'}
    $safeStage=$true
    Write-Record ([IO.Path]::Combine($stage,'owner.json')) @{app_id=$appIdentity;nonce=$job.nonce}
    Remove-OwnedStage
    if([IO.Directory]::Exists($stage) -or [IO.Directory]::Exists($updates)) {throw 'Owned empty stage/cache remained.'}
} finally {$pathLocks.Dispose()}
[IO.File]::WriteAllText([IO.Path]::Combine($install,'owner-access.txt'),'owner still has full control')
[IO.File]::Delete([IO.Path]::Combine($install,'owner-access.txt'))
[IO.Directory]::Delete($install)
'runner-path-locks-passed'
'''
        with tempfile.TemporaryDirectory(prefix='l2-runner-paths-') as folder:
            script = Path(folder) / 'guard.ps1'
            script.write_text('param([string]$TestDirectory)\n' + declarations + '\n' + harness, encoding='utf-8-sig')
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            result = subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
                                     '-ExecutionPolicy', 'Bypass', '-File', str(script), '-TestDirectory', folder],
                                    capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertIn(b'runner-path-locks-passed', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Windows CNG/PowerShell integration')
    def test_real_runner_signature_binding_and_installer_locks(self):
        # Evaluate only declarations/functions. Never execute the runner's main
        # body, registry inspection, process launch, GUI, or cleanup/restart flow.
        declarations = app_updater.RUNNER_PATH.read_text(encoding='utf-8').split(
            '\ntry {\n    $checkedJob', 1)[0]
        declarations = declarations.replace(
            'param([Parameter(Mandatory = $true)][string]$JobPath)', '')
        harness = r'''
$stage = $TestDirectory
$checkedCases = [Collections.Generic.List[string]]::new()
$testKey = [Security.Cryptography.RSACng]::new(3072)
$wrongKey = [Security.Cryptography.RSACng]::new(3072)
try {
    # Ephemeral test key: the shipped pinned key is never changed on disk.
    $releasePublicKeyBlob = [Convert]::ToBase64String($testKey.Key.Export([Security.Cryptography.CngKeyBlobFormat]::GenericPublicBlob))
    $installer = [IO.Path]::Combine($stage, 'installer.exe')
    $fixture = [byte[]]::new(2048)
    $fixture[0] = 77; $fixture[1] = 90
    [IO.File]::WriteAllBytes($installer, $fixture)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash($fixture)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    $original = @{
        product='Lineage 2 Chat Translator'; platform='windows-x64'; version='99.8.7'
        validation=@{packaged_self_test_passed=$true}
        installer=@{file='Lineage-2-Chat-Translator-99.8.7-Setup.exe'; bytes=2048; sha256=$hash}
    } | ConvertTo-Json -Depth 6 -Compress
    function Write-Signed($Document) {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Document | ConvertTo-Json -Depth 6 -Compress))
        [IO.File]::WriteAllBytes([IO.Path]::Combine($stage, 'release.json'), $bytes)
        $sig = $testKey.SignData($bytes, [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pss)
        [IO.File]::WriteAllBytes([IO.Path]::Combine($stage, 'release.sig'), $sig)
    }
    function Reset-Valid {
        $script:job = [pscustomobject]@{
            version='99.8.7'; from_version='1.7.12'
            installer_file='Lineage-2-Chat-Translator-99.8.7-Setup.exe'
            installer_bytes=2048; installer_sha256=$hash
        }
        Write-Signed ($original | ConvertFrom-Json)
    }
    function Expect-Rejected([string]$Name, [scriptblock]$Action) {
        $rejected = $false
        try { & $Action } catch { $rejected = $true }
        if (-not $rejected) { throw ('Guard accepted: ' + $Name) }
        $checkedCases.Add($Name)
    }
    Reset-Valid
    Assert-SignedRelease
    $checkedCases.Add('valid exact-byte RSA-PSS signature')
    $metadataPath = [IO.Path]::Combine($stage, 'release.json')
    $signaturePath = [IO.Path]::Combine($stage, 'release.sig')
    [IO.File]::AppendAllText($metadataPath, ' ')
    Expect-Rejected 'changed metadata' { Assert-SignedRelease }
    Reset-Valid
    $bad = [IO.File]::ReadAllBytes($signaturePath); $bad[0] = $bad[0] -bxor 1
    [IO.File]::WriteAllBytes($signaturePath, $bad)
    Expect-Rejected 'changed signature' { Assert-SignedRelease }
    Reset-Valid
    $bad = $wrongKey.SignData([IO.File]::ReadAllBytes($metadataPath), [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pss)
    [IO.File]::WriteAllBytes($signaturePath, $bad)
    Expect-Rejected 'different signing key' { Assert-SignedRelease }
    Reset-Valid
    [IO.File]::Delete($signaturePath)
    Expect-Rejected 'missing signature' { Assert-SignedRelease }
    Reset-Valid
    [IO.File]::WriteAllBytes($signaturePath, [byte[]]::new(385))
    Expect-Rejected 'oversized signature' { Assert-SignedRelease }
    Reset-Valid
    [IO.File]::WriteAllBytes($metadataPath, [byte[]]::new(65537))
    Expect-Rejected 'oversized metadata' { Assert-SignedRelease }
    foreach ($field in @('product', 'platform', 'version', 'file', 'bytes', 'sha256', 'validation')) {
        Reset-Valid
        $document = $original | ConvertFrom-Json
        switch ($field) {
            'product' { $document.product = 'Different App' }
            'platform' { $document.platform = 'linux-x64' }
            'version' { $document.version = '99.8.8' }
            'file' { $document.installer.file = 'other.exe' }
            'bytes' { $document.installer.bytes = 2049 }
            'sha256' { $document.installer.sha256 = 'a' * 64 }
            'validation' { $document.validation.packaged_self_test_passed = $false }
        }
        Write-Signed $document
        Expect-Rejected ('signed mismatch: ' + $field) { Assert-SignedRelease }
    }
    Reset-Valid
    $job.from_version = '99.8.7'
    Expect-Rejected 'equal version replay' { Assert-SignedRelease }
    $job.from_version = '99.8.8'
    Expect-Rejected 'older version replay' { Assert-SignedRelease }
    Reset-Valid
    Expect-Rejected 'false installed version' { Assert-InstalledVersion (Join-Path $PSHOME 'powershell.exe') }
    $job.from_version = [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $PSHOME 'powershell.exe')).ProductVersion
    Assert-InstalledVersion (Join-Path $PSHOME 'powershell.exe')
    $checkedCases.Add('real installed version comparison')
    Reset-Valid
    $lock = Open-VerifiedInstaller $installer
    try {
        Expect-Rejected 'installer replacement while verified' { [IO.File]::WriteAllBytes($installer, $fixture) }
        Expect-Rejected 'installer deletion while verified' { [IO.File]::Delete($installer) }
    } finally { $lock.Dispose() }
    $writer = [IO.File]::Open($installer, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    try { Expect-Rejected 'pre-existing installer writer' { Open-VerifiedInstaller $installer } }
    finally { $writer.Dispose() }
    $fixture[10] = 1
    [IO.File]::WriteAllBytes($installer, $fixture)
    Expect-Rejected 'tampered installer checksum' { Open-VerifiedInstaller $installer }
    @{cases=@($checkedCases); installer_executed=$false} | ConvertTo-Json -Compress
} finally {
    $testKey.Dispose()
    $wrongKey.Dispose()
}
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'guard-tests.ps1'
            path.write_text('param([string]$TestDirectory)\n' + declarations + harness, encoding='utf-8')
            result = subprocess.run([str(app_updater._powershell()), '-NoLogo', '-NoProfile',
                                     '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(path),
                                     '-TestDirectory', folder], capture_output=True, text=True, timeout=40,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(result.stdout)
            self.assertFalse(evidence['installer_executed'])
            self.assertEqual(len(evidence['cases']), 22)


if __name__ == '__main__':
    unittest.main()
