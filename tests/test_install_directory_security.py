"""Real NTFS ACL checks in disposable trees; no installed program or registry edits.

The machine's profile ancestry is outside test ownership, so the test driver
replaces ancestor enumeration with a synthetic boundary. ACLs and handles inside
that boundary are real; production has no boundary-override command-line option.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / "assets/protect-install-directory.ps1"
POWERSHELL = shutil.which("powershell.exe")

PRELUDE = r'''
param([string]$Helper, [string]$Fixture)
$ErrorActionPreference = 'Stop'
. $Helper
$script:Boundary = $Fixture
function Get-InstallAncestorPaths([string]$Root) { return @($script:Boundary) }
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$trust = @($user, 'S-1-5-18', 'S-1-5-32-544')
function Read-Sddl([string]$Path) {
    $h = [L2InstallSecurity.Native]::Open($Path, $false, [IO.Directory]::Exists($Path))
    try { return [L2InstallSecurity.Native]::Security($h).GetSddlForm([Security.AccessControl.AccessControlSections]::All) }
    finally { $h.Dispose() }
}
function Set-Private([string]$Path) {
    $directory = [IO.Directory]::Exists($Path)
    $h = [L2InstallSecurity.Native]::Open($Path, $true, $directory)
    try { [L2InstallSecurity.Native]::Protect($h, $trust, $directory) }
    finally { $h.Dispose() }
}
function Add-Access([string]$Path, [Security.AccessControl.FileSystemRights]$Rights, [bool]$InheritOnly=$false) {
    $directory = [IO.Directory]::Exists($Path)
    $acl = if($directory) {[IO.Directory]::GetAccessControl($Path)} else {[IO.File]::GetAccessControl($Path)}
    $inherit = if($directory) {[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'} else {[Security.AccessControl.InheritanceFlags]::None}
    $propagate = if($InheritOnly) {[Security.AccessControl.PropagationFlags]::InheritOnly} else {[Security.AccessControl.PropagationFlags]::None}
    $ace = [Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new('S-1-5-11'), $Rights, $inherit, $propagate,
        [Security.AccessControl.AccessControlType]::Allow)
    $acl.AddAccessRule($ace)
    if($directory) {[IO.Directory]::SetAccessControl($Path,$acl)} else {[IO.File]::SetAccessControl($Path,$acl)}
}
function Expect-Failure([scriptblock]$Action, [string]$Reason) {
    $failed = $false
    try { & $Action | Out-Null } catch { $failed = $true; if($Reason -and $_.Exception.Message -notlike ('*'+$Reason+'*')) {throw} }
    if(-not $failed) {throw 'Unsafe installation was accepted.'}
}
function New-RegisteredTree {
    [IO.Directory]::CreateDirectory($root) | Out-Null
    Add-Access $root ([Security.AccessControl.FileSystemRights]::Modify)
    [IO.File]::WriteAllText((Join-Path $root 'Lineage2ChatTranslator.exe'), 'synthetic executable bytes')
    [IO.File]::WriteAllText((Join-Path $root 'unins000.exe'), 'synthetic uninstaller bytes')
    [IO.Directory]::CreateDirectory((Join-Path $root '_internal')) | Out-Null
    [IO.File]::WriteAllText((Join-Path $root '_internal\library.dll'), 'synthetic library bytes')
}
Set-Private $Fixture
$root = Join-Path $Fixture 'application'
'''


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell and NTFS are required.")
class InstallationDirectorySecurityTests(unittest.TestCase):
    def run_script(self, body):
        with tempfile.TemporaryDirectory(prefix="l2ct-acl-") as temporary:
            base = Path(temporary)
            fixture = base / "owned-fixture"
            fixture.mkdir()
            script = base / "check.ps1"
            script.write_text(PRELUDE + "\n" + body + "\n", encoding="utf-8-sig")
            result = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), "-Helper", str(HELPER), "-Fixture", str(fixture)],
                capture_output=True, text=True, timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 77:
                self.skipTest("Windows did not permit the isolated link fixture.")
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_repairs_inherited_and_explicit_rights_without_changing_bytes_or_parent(self):
        self.run_script(r'''
New-RegisteredTree
$dll = Join-Path $root '_internal\library.dll'
Add-Access $dll ([Security.AccessControl.FileSystemRights]::FullControl)
$bytes = [IO.File]::ReadAllText($dll)
$parentAcl = Read-Sddl $Fixture
$result = Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root
if(-not $result.ok -or $result.entries -ne 5) {throw 'Unexpected protection result.'}
$verified = Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root -VerifyOnly
if(-not $verified.ok -or -not $verified.verified_only) {throw 'Verification did not pass.'}
$again = Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root
if(-not $again.ok) {throw 'Idempotent repair failed.'}
if((Read-Sddl $Fixture) -cne $parentAcl -or [IO.File]::ReadAllText($dll) -cne $bytes) {throw 'Non-permission state changed.'}
''')

    def test_creates_new_private_directory_and_verifies_it(self):
        self.run_script(r'''
$result = Invoke-InstallDirectoryProtection -Root $root
if(-not $result.ok -or -not $result.created -or $result.entries -ne 1) {throw 'New folder failed.'}
Invoke-InstallDirectoryProtection -Root $root -VerifyOnly | Out-Null
''')

    def test_verify_only_never_repairs_unsafe_permissions(self):
        self.run_script(r'''
New-RegisteredTree
$before = Read-Sddl $root
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root -VerifyOnly} ''
if((Read-Sddl $root) -cne $before) {throw 'Verify-only changed permissions.'}
''')

    def test_verify_only_accepts_new_payload_inheriting_private_root(self):
        self.run_script(r'''
Invoke-InstallDirectoryProtection -Root $root | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $root '_internal\nested')) | Out-Null
foreach($file in @('Lineage2ChatTranslator.exe','unins000.exe','_internal\nested\library.dll')) {
    [IO.File]::WriteAllText((Join-Path $root $file), 'new installer payload')
}
$result = Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root -VerifyOnly
if(-not $result.ok) {throw 'Secure inherited payload was rejected.'}
$dll = Join-Path $root '_internal\nested\library.dll'
Add-Access $dll ([Security.AccessControl.FileSystemRights]::Modify)
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root -VerifyOnly} 'unexpected access rules'
''')

    def test_nonempty_unregistered_and_unrelated_registered_targets_are_unchanged(self):
        self.run_script(r'''
New-RegisteredTree
$before = Read-Sddl $root
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root} 'nonempty unregistered'
[IO.File]::WriteAllText((Join-Path $root 'personal.txt'), 'unrelated data')
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root} 'unrelated files'
if((Read-Sddl $root) -cne $before) {throw 'Rejected target ACL changed.'}
''')

    def test_relocation_and_reserved_destinations_are_rejected(self):
        self.run_script(r'''
New-RegisteredTree
$before = Read-Sddl $root
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot (Join-Path $Fixture 'elsewhere')} 'registered folder'
Expect-Failure {Invoke-InstallDirectoryProtection -Root ([IO.Path]::GetPathRoot($root))} 'drive root'
Expect-Failure {Invoke-InstallDirectoryProtection -Root ([Environment]::GetFolderPath('UserProfile'))} 'separate folder'
if((Read-Sddl $root) -cne $before) {throw 'Rejected relocation changed permissions.'}
''')

    def test_effective_parent_delete_child_stops_before_creation(self):
        self.run_script(r'''
Add-Access $Fixture ([Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles)
$before = Read-Sddl $Fixture
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root} 'replace an installation parent'
if([IO.Directory]::Exists($root) -or (Read-Sddl $Fixture) -cne $before) {throw 'Unsafe parent was changed.'}
''')

    def test_inherit_only_parent_write_rule_is_not_effective_on_parent(self):
        self.run_script(r'''
Add-Access $Fixture ([Security.AccessControl.FileSystemRights]::Modify) $true
$result = Invoke-InstallDirectoryProtection -Root $root
if(-not $result.ok) {throw 'Inherit-only parent fixture was rejected.'}
Invoke-InstallDirectoryProtection -Root $root -VerifyOnly | Out-Null
''')

    def test_foreign_owner_and_null_dacl_policy_fail(self):
        self.run_script(r'''
$foreign = [Security.AccessControl.RawSecurityDescriptor]::new('O:S-1-5-21-111-222-333-444D:(A;;FA;;;SY)')
Expect-Failure {Assert-InstallOwner $foreign $trust} 'owned by another account'
$nullAcl = [Security.AccessControl.RawSecurityDescriptor]::new('O:' + $user + 'D:NO_ACCESS_CONTROL')
Expect-Failure {Assert-InstallOwner $nullAcl $trust} 'unrestricted permissions'
''')

    def test_hard_link_is_rejected_without_modifying_external_file_acl(self):
        self.run_script(r'''
New-RegisteredTree
$outside = Join-Path $Fixture 'outside.txt'
[IO.File]::WriteAllText($outside, 'external fixture')
$before = Read-Sddl $outside
try {New-Item -ItemType HardLink -Path (Join-Path $root '_internal\linked.dll') -Target $outside | Out-Null} catch {exit 77}
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root} 'Hard-linked'
if((Read-Sddl $outside) -cne $before -or [IO.File]::ReadAllText($outside) -cne 'external fixture') {throw 'External hard-link target changed.'}
''')

    def test_junction_is_rejected_without_touching_its_target(self):
        self.run_script(r'''
New-RegisteredTree
$outside = Join-Path $Fixture 'outside'
[IO.Directory]::CreateDirectory($outside) | Out-Null
$before = Read-Sddl $outside
try {New-Item -ItemType Junction -Path (Join-Path $root '_internal\linked') -Target $outside | Out-Null} catch {exit 77}
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root} ''
if((Read-Sddl $outside) -cne $before) {throw 'External junction target changed.'}
''')

    def test_existing_write_handle_blocks_before_acl_mutation(self):
        self.run_script(r'''
New-RegisteredTree
$before = Read-Sddl $root
$open = [IO.File]::Open((Join-Path $root '_internal\library.dll'), [IO.FileMode]::Open,
    [IO.FileAccess]::ReadWrite, [IO.FileShare]'ReadWrite,Delete')
try {Expect-Failure {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root} ''}
finally {$open.Dispose()}
if((Read-Sddl $root) -cne $before) {throw 'A blocked file caused partial ACL repair.'}
''')

    def test_protect_changes_only_the_opened_directory_not_existing_children(self):
        self.run_script(r'''
New-RegisteredTree
$dll = Join-Path $root '_internal\library.dll'
$before = Read-Sddl $dll
$parent = Join-Path $root '_internal'
Set-Private $parent
if((Read-Sddl $dll) -cne $before) {throw 'Protection propagated to an unchecked child.'}
''')

    def test_post_preflight_hardlink_attempt_cannot_change_external_acl(self):
        self.run_script(r'''
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class AclRaceFixture {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern bool CreateHardLinkW(string name, string existing, IntPtr security);
    public static void Link(string name, string existing) {
        if (!CreateHardLinkW(name, existing, IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error());
    }
}
'@
New-RegisteredTree
$outsideDir = Join-Path $Fixture 'outside'
[IO.Directory]::CreateDirectory($outsideDir) | Out-Null
Add-Access $outsideDir ([Security.AccessControl.FileSystemRights]::Modify)
$outside = Join-Path $outsideDir 'document.txt'
[IO.File]::WriteAllText($outside, 'external fixture')
$before = Read-Sddl $outside
$script:OriginalAssertOwner = (Get-Command Assert-InstallOwner).ScriptBlock
$script:OwnerChecks = 0
$script:Injected = $false
$script:BlockedBySharing = $false
function Assert-InstallOwner($Security, [string[]]$Trusted) {
    & $script:OriginalAssertOwner $Security $Trusted
    $script:OwnerChecks++
    # One private fixture ancestor plus the five original app entries have now
    # passed owner preflight. Inject a new directory entry before ACL repair.
    if($script:OwnerChecks -eq 6) {
        try {
            [AclRaceFixture]::Link((Join-Path $root 'unins009.dat'), $outside)
            $script:Injected = $true
        } catch {
            # Current Windows blocks CreateHardLink's parent access while the
            # directory handle is retained. Only that exact failure is allowed.
            if($_.Exception.InnerException.NativeErrorCode -ne 32) {throw}
            $script:BlockedBySharing = $true
        }
    }
}
$failed = $false
try {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root | Out-Null}
catch {
    if($_.Exception.Message -notlike '*changed during permission checks*') {throw}
    $failed = $true
}
if(-not $script:Injected -and -not $script:BlockedBySharing) {throw 'The post-preflight injection did not run.'}
if($script:Injected -and -not $failed) {throw 'The added hardlink escaped the final inventory.'}
if((Read-Sddl $outside) -cne $before -or [IO.File]::ReadAllText($outside) -cne 'external fixture') {throw 'An unchecked hardlink target changed.'}
''')

    def test_added_file_after_preflight_is_rejected_before_success(self):
        self.run_script(r'''
New-RegisteredTree
$script:OriginalAssertOwner = (Get-Command Assert-InstallOwner).ScriptBlock
$script:OwnerChecks = 0
$script:Injected = $false
$script:InjectedAcl = ''
function Assert-InstallOwner($Security, [string[]]$Trusted) {
    & $script:OriginalAssertOwner $Security $Trusted
    $script:OwnerChecks++
    if($script:OwnerChecks -eq 6) {
        [IO.File]::WriteAllText((Join-Path $root 'unins009.dat'), 'injected after preflight')
        $script:InjectedAcl = Read-Sddl (Join-Path $root 'unins009.dat')
        $script:Injected = $true
    }
}
Expect-Failure {Invoke-InstallDirectoryProtection -Root $root -RegisteredRoot $root} 'changed during permission checks'
if(-not $script:Injected) {throw 'The post-preflight injection did not run.'}
if((Read-Sddl (Join-Path $root 'unins009.dat')) -cne $script:InjectedAcl) {throw 'An unchecked added file received ACL changes.'}
''')


if __name__ == "__main__":
    unittest.main()
