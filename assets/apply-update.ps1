# Trusted updater runner copied from the installed application, never downloaded.
# This process waits for clean launcher exit before replacing program files.
param([Parameter(Mandatory = $true)][string]$JobPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$appIdentity = 'Lineage2ChatTranslator'
$productName = 'Lineage 2 Chat Translator'
# This public key is part of the installed helper, never read from the host/job.
# Keep it identical to update_trust.PUBLIC_KEY_BLOB_B64 when rotating trust.
$releasePublicKeyBlob = 'UlNBMQAMAAADAAAAgAEAAAAAAAAAAAAAAQABqH1NyNtVCMd/9YZYLdUITlH/T8qIwWUEbZZmLRCYBeo2rC1il+aTdicUZ+YvJvn3zSuGOfil71uhGzQRlx79amfzzJIQdssaGE1PKy82d5HA1vsQ17CcZwmJYcuRYnEsqyIrhKoTTOeLtgp6Y6+hzgT+DUXwFzG1w0IIp+iI/qeXgl5UhcO10QOsfl3+bsirG39xAWu/9aD+i9lZHNJ4Q7RTzuKKJNBcR1StiKuPsh8rrLVtRZlD/ZqDMjmalumrB4ccWXPyhnxhyGHynX4Ct6f9ItKjd0G6V/yzcrig/W0C9TvtelaElJ4nX/U/bKKo6zqwSsRuWTJUTkHmJ2oqL9940eegnsZ0QfxPsJBE/SWWkMqHcPLTZcMNhWO6oBFzU2YxIoo4iOtZfWl6MLHrNYtxNt0nD7yU/qzjtIf0qPtNIkAl/a41RJGrFnz2ijLvud//ZV0MQjNCD/MVFpwzBPpKTqSU/xKi1s2eTnJG94e2B63/B70rgyxzcSptxntp'
$releaseSignatureBytes = 384  # RSA-3072; PSS SHA-256 uses a 32-byte salt.
$registryPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{1D8CE3C7-53A6-4C67-AC9F-1C6F0D6B1920}_is1'
# These bounds mirror app_updater.py; they are the handoff protocol, not model settings.
$maxRecordBytes = 64 * 1024
$maxSmallRecordBytes = 4 * 1024
$minInstallerBytes = 1024
$maxInstallerBytes = 128 * 1024 * 1024
$selfTestTimeoutMs = 30000
$selfTestStopTimeoutMs = 5000
$handoffConfirmationSeconds = 30
$handoffPollMs = 50
$parentExitTimeoutMs = 180000
$restartRequiredExitCode = 3010  # Inno's configured reboot-required result.
$maxResultMessageChars = 7800  # Leaves room for a bounded failure explanation.
$maxInstallerLogBytes = 2 * 1024 * 1024
$stagePrefix = 'update-'
$allowedFiles = @('owner.json', 'job.json', 'installer.part', 'installer.exe', 'apply-update.ps1',
                  'release.json', 'release.sig',
                  'ready.json', 'commit.json', 'commit.pending', 'cancel', 'self-test.json', 'installer.log')
$stage = $null
$job = $null
$safeStage = $false
$committed = $false
$parentExited = $false
$helperMutex = $null
$mutexHeld = $false
$originalProcess = $null
$pathLocks = $null
$restartExecutable = $null
$restartPermitted = $true
$status = 'error'
$message = 'The update did not complete. Saved models and settings have not been removed.'

function Assert-Condition([bool]$Condition, [string]$Description) {
    if (-not $Condition) { throw $Description }
}

function New-UpdatePathLocks {
    # Transfer path stability from the parent before writing ready.json. Handles
    # stay alive after the parent exits, through installation and staged cleanup.
    if ($null -eq ('L2Update.PathLocks' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace L2Update {
    public sealed class PathLocks : IDisposable {
        [StructLayout(LayoutKind.Sequential)]
        private struct Info {
            public uint Attributes, C0, C1, A0, A1, W0, W1, Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out Info info);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder value, uint length, uint flags);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool SetFileInformationByHandle(SafeFileHandle handle, int kind, ref int value, uint size);
        private readonly Dictionary<string, SafeFileHandle> held = new Dictionary<string, SafeFileHandle>(StringComparer.OrdinalIgnoreCase);
        private readonly Dictionary<string, Info> released = new Dictionary<string, Info>(StringComparer.OrdinalIgnoreCase);
        private static string Key(string path) { return Path.GetFullPath(path).TrimEnd('\\'); }
        private static Info Inspect(SafeFileHandle handle, string path) {
            Info info;
            if (!GetFileInformationByHandle(handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
            var actual = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandleW(handle, actual, (uint)actual.Capacity, 0);
            if (length == 0 || length >= actual.Capacity) throw new IOException("Cannot resolve an update directory safely.");
            string final = actual.ToString();
            if (final.StartsWith(@"\\?\")) final = final.Substring(4);
            if (!String.Equals(Key(final), Key(path), StringComparison.OrdinalIgnoreCase) ||
                (info.Attributes & 0x400) != 0 || (info.Attributes & 0x10) == 0)
                throw new IOException("The updater will not follow linked or redirected folders.");
            return info;
        }
        private static SafeFileHandle Open(string path, uint access) {
            var handle = CreateFileW(path, access, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
            if (handle.IsInvalid) { handle.Dispose(); throw new Win32Exception(Marshal.GetLastWin32Error()); }
            try { Inspect(handle, path); return handle; } catch { handle.Dispose(); throw; }
        }
        public void HoldDirectoryPath(string path) {
            var pending = new Stack<string>();
            for (var cursor = new DirectoryInfo(Path.GetFullPath(path)); cursor != null; cursor = cursor.Parent)
                pending.Push(cursor.FullName);
            while (pending.Count > 0) {
                string next = pending.Pop(), key = Key(next);
                if (!held.ContainsKey(key)) {
                    var handle = Open(next, 0x20001);
                    try {
                        Info expected;
                        if (released.TryGetValue(key, out expected)) {
                            Info current = Inspect(handle, next);
                            if (expected.Volume != current.Volume || expected.IndexHigh != current.IndexHigh || expected.IndexLow != current.IndexLow)
                                throw new IOException("The installation directory changed during setup. The translator was left closed.");
                        }
                        held.Add(key, handle);
                        handle = null;
                        released.Remove(key);
                    } finally { if (handle != null) handle.Dispose(); }
                }
            }
        }
        public void ReleaseForInstaller(string path) {
            string key = Key(path);
            SafeFileHandle handle;
            if (!held.TryGetValue(key, out handle)) throw new IOException("The installation directory was not held.");
            released.Add(key, Inspect(handle, path));
            held.Remove(key);
            handle.Dispose();
        }
        public bool IsHeld(string path) { return held.ContainsKey(Key(path)); }
        public void RemoveEmptyDirectory(string path) {
            string key = Key(path);
            SafeFileHandle original;
            if (!held.TryGetValue(key, out original)) throw new IOException("Cleanup directory was not held.");
            Info expected = Inspect(original, path);
            // A delete-capable handle cannot coexist with the no-delete guard.
            // Retain all parents, then reopen and compare the exact object ID
            // before setting disposition. A raced replacement is never deleted.
            held.Remove(key);
            original.Dispose();
            var replacement = Open(path, 0x30001);
            try {
                Info current = Inspect(replacement, path);
                if (expected.Volume != current.Volume || expected.IndexHigh != current.IndexHigh || expected.IndexLow != current.IndexLow)
                    throw new IOException("Cleanup directory changed; it was left untouched.");
                held.Add(key, replacement);
                replacement = null;
                int remove = 1;
                if (!SetFileInformationByHandle(held[key], 4, ref remove, 4)) throw new Win32Exception(Marshal.GetLastWin32Error());
                held[key].Dispose();
                held.Remove(key);
            } finally { if (replacement != null) replacement.Dispose(); }
        }
        public void Dispose() {
            foreach (var handle in held.Values) handle.Dispose();
            held.Clear();
        }
    }
}
'@
    }
    return [L2Update.PathLocks]::new()
}

function Assert-PrivateUpdateDirectory([string]$Path) {
    Assert-Condition ($null -ne $pathLocks -and $pathLocks.IsHeld($Path)) 'Update directory is not held safely.'
    $acl = [IO.Directory]::GetAccessControl($Path)
    $sidType = [Security.Principal.SecurityIdentifier]
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Assert-Condition ($acl.GetOwner($sidType).Value -ceq $current) 'Update directory is owned by another Windows account.'
    $trusted = @($current, 'S-1-5-18', 'S-1-5-32-544')
    $rules = @($acl.GetAccessRules($true, $true, $sidType))
    Assert-Condition ($acl.AreAccessRulesProtected -and $rules.Count -eq $trusted.Count) 'Update directory permissions need repair. Use the website installer.'
    $seen = @{}
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        Assert-Condition ($trusted -contains $sid -and -not $seen.ContainsKey($sid) -and
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $rule.FileSystemRights -eq [Security.AccessControl.FileSystemRights]::FullControl) 'Update directory permissions allow unexpected access.'
        $seen[$sid] = $true
    }
}

function Assert-PlainPath([string]$Value) {
    # Re-check every ancestor before a read, write, process start or deletion.
    Assert-Condition ([IO.Path]::IsPathRooted($Value)) 'The update path must be absolute.'
    $full = [IO.Path]::GetFullPath($Value)
    $part = $full
    while ($part) {
        if ([IO.File]::Exists($part) -or [IO.Directory]::Exists($part)) {
            $attributes = [IO.File]::GetAttributes($part)
            Assert-Condition (($attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'The updater will not follow linked folders or files.'
        }
        $parentPart = [IO.Path]::GetDirectoryName($part)
        if ($parentPart -eq $part) { break }
        $part = $parentPart
    }
    return $full
}

function Read-Record([string]$Path, [int]$Maximum = $maxRecordBytes) {
    $content = Read-BoundedBytes $Path $Maximum
    $text = [Text.UTF8Encoding]::new($false, $true).GetString($content).TrimStart([char]0xFEFF)
    return ($text | ConvertFrom-Json)
}

function Read-BoundedBytes([string]$Path, [int]$Maximum = $maxRecordBytes) {
    $checked = Assert-PlainPath $Path
    Assert-Condition ([IO.File]::Exists($checked)) 'An update record is missing.'
    Assert-Condition ((Get-Item -LiteralPath $checked).Length -le $Maximum) 'An update record is too large.'
    # A record can grow between the size check and read. Bound bytes actually
    # read, with one extra byte to distinguish an oversized file from EOF.
    $buffer = [byte[]]::new($Maximum + 1)
    $stream = [IO.File]::Open($checked, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $count = 0
    try {
        while ($count -lt $buffer.Length) {
            $received = $stream.Read($buffer, $count, $buffer.Length - $count)
            if ($received -eq 0) { break }
            $count += $received
        }
    } finally { $stream.Dispose() }
    Assert-Condition ($count -le $Maximum) 'An update record is too large.'
    $content = [byte[]]::new($count)
    [Array]::Copy($buffer, $content, $count)
    return ,$content
}

function Get-InstallerCleanupExplanation {
    # Recover display-only details before deleting the temporary log. Exit code
    # and signature checks govern behavior; wording never decides whether to run.
    try {
        $bytes = Read-BoundedBytes ([IO.Path]::Combine($stage, 'installer.log')) $maxInstallerLogBytes
        $memory = [IO.MemoryStream]::new($bytes, $false)
        $reader = [IO.StreamReader]::new($memory, [Text.Encoding]::UTF8, $true)
        try { $text = $reader.ReadToEnd() } finally { $reader.Dispose(); $memory.Dispose() }
        foreach ($line in ($text -split '[\r\n]+')) {
            if ($line -match 'Program file cleanup:\s*(\{.+\})\s*$') {
                $report = $Matches[1] | ConvertFrom-Json
                if ($report.ok -is [bool] -and -not $report.ok -and $report.version -ceq $job.version -and
                    $report.error -is [string] -and $report.error.Length -gt 0 -and $report.error.Length -le 2048) {
                    return ' ' + ($report.error -replace '[\x00-\x1f\x7f]', ' ')
                }
            }
        }
    } catch { }  # Missing/oversized logs cannot weaken the failed-install guard.
    return ''
}

function Assert-SignedRelease {
    # Authenticate the exact bytes before parsing; the host cannot substitute
    # its own public key, checksum, filename, version, or signing algorithm.
    $metadata = Read-BoundedBytes ([IO.Path]::Combine($stage, 'release.json')) $maxRecordBytes
    $signature = Read-BoundedBytes ([IO.Path]::Combine($stage, 'release.sig')) $releaseSignatureBytes
    Assert-Condition (($metadata.Length -gt 0) -and ($signature.Length -eq $releaseSignatureBytes)) 'The update signature is missing or invalid.'
    $key = $null
    $rsa = $null
    try {
        $key = [Security.Cryptography.CngKey]::Import([Convert]::FromBase64String($releasePublicKeyBlob), [Security.Cryptography.CngKeyBlobFormat]::GenericPublicBlob)
        $rsa = [Security.Cryptography.RSACng]::new($key)
        Assert-Condition ($rsa.KeySize -eq 3072) 'The update signing key is invalid.'
        Assert-Condition ($rsa.VerifyData($metadata, $signature, [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pss)) 'The update signature could not be verified. No installer was run.'
    } finally {
        if ($rsa) { $rsa.Dispose() }
        if ($key) { $key.Dispose() }
    }
    $document = ([Text.UTF8Encoding]::new($false, $true).GetString($metadata).TrimStart([char]0xFEFF) | ConvertFrom-Json)
    Assert-Condition (($document.product -ceq $productName) -and ($document.platform -ceq 'windows-x64') -and
        ($document.validation.packaged_self_test_passed -is [bool]) -and $document.validation.packaged_self_test_passed) 'The signed release does not belong to this application or failed its checks.'
    Assert-Condition (($document.version -is [string]) -and ($document.version -ceq $job.version) -and
        ([Version]$document.version -gt [Version]$job.from_version)) 'The signed release version does not match this update.'
    Assert-Condition (($document.installer.file -is [string]) -and ($document.installer.file -ceq $job.installer_file) -and
        ($document.installer.bytes -is [int] -or $document.installer.bytes -is [long]) -and
        ($document.installer.bytes -eq $job.installer_bytes) -and ($document.installer.sha256 -is [string]) -and
        ($document.installer.sha256 -ceq $job.installer_sha256)) 'The installer does not match its signed release.'
}

function Assert-InstalledVersion([string]$Executable) {
    # The job's claimed old version is not an independent downgrade guard.
    $actualVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo($Executable).ProductVersion
    Assert-Condition ($actualVersion -ceq $job.from_version) 'The installed version changed. Check for updates again.'
}

function Open-VerifiedInstaller([string]$Installer) {
    # Hold a deny-write/deny-delete handle through CreateProcess and execution.
    # A pre-existing writer also makes this open fail rather than racing a hash.
    $checked = Assert-PlainPath $Installer
    $stream = [IO.File]::Open($checked, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        Assert-Condition ($stream.Length -eq $job.installer_bytes) 'The installer size changed.'
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $hash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
        Assert-Condition ($hash -ceq $job.installer_sha256) 'The installer checksum changed; it was not run.'
        return $stream
    } catch {
        $stream.Dispose()
        throw
    }
}

function Write-Record([string]$Path, $Value) {
    $checked = Assert-PlainPath $Path
    [IO.File]::WriteAllText($checked, ($Value | ConvertTo-Json -Compress -Depth 6), [Text.UTF8Encoding]::new($false))
}

function Quote-Argument([string]$Value) {
    # Windows CommandLineToArgvW escaping; no cmd.exe, Invoke-Expression or eval.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Start-VerifiedProcess([string]$Executable, [string[]]$Arguments, [bool]$Hidden) {
    # Start only an already validated absolute executable; never invoke a shell.
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = (Assert-PlainPath $Executable)
    $info.WorkingDirectory = [IO.Path]::GetDirectoryName($info.FileName)
    $info.UseShellExecute = $false
    $info.Arguments = (($Arguments | ForEach-Object { Quote-Argument $_ }) -join ' ')
    $info.CreateNoWindow = $Hidden
    if ($Hidden) { $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden }
    return [Diagnostics.Process]::Start($info)
}

function Assert-Installation([string]$Executable) {
    $checked = Assert-PlainPath $Executable
    Assert-Condition ([IO.Path]::GetFileName($checked) -ieq 'Lineage2ChatTranslator.exe') 'The launcher filename is invalid.'
    Assert-Condition ([IO.File]::Exists($checked)) 'The installed launcher is missing.'
    $registration = Get-ItemProperty -LiteralPath $registryPath
    Assert-Condition (($registration.DisplayName -eq $productName) -or ($registration.DisplayName.StartsWith($productName + ' '))) 'The installed application identity does not match.'
    $location = (Assert-PlainPath $registration.InstallLocation).TrimEnd('\')
    $directory = [IO.Path]::GetDirectoryName($checked).TrimEnd('\')
    Assert-Condition ($location -ieq $directory) 'The registered installation folder changed.'
    $uninstaller = Assert-PlainPath ([IO.Path]::Combine($directory, 'unins000.exe'))
    Assert-Condition ([IO.File]::Exists($uninstaller)) 'The registered uninstaller is missing.'
    return $checked
}

function Test-InstalledProgram([string[]]$AllowedVersions) {
    # Validate the installed version without starting its GUI, Ollama or models.
    $executable = Assert-Installation $job.executable
    $reportPath = [IO.Path]::Combine($stage, 'self-test.json')
    if ([IO.File]::Exists($reportPath)) { Remove-Item -LiteralPath (Assert-PlainPath $reportPath) }
    $probe = Start-VerifiedProcess $executable @('--self-test', '--output', $reportPath) $true
    try {
        if (-not $probe.WaitForExit($selfTestTimeoutMs)) {
            # Only this exact, updater-created headless child is stopped.
            $probe.Kill()
            $probe.WaitForExit($selfTestStopTimeoutMs) | Out-Null
            throw 'The installed program did not finish its self-test.'
        }
        Assert-Condition ($probe.ExitCode -eq 0) 'The installed program failed its self-test.'
        $report = Read-Record $reportPath
        Assert-Condition (($report.ok -eq $true) -and ($report.frozen -eq $true) -and ($AllowedVersions -contains $report.version)) 'The installed program did not report the expected version.'
    } finally { $probe.Dispose() }
    return $executable
}

function Remove-OwnedStage {
    # Delete only this job's allowlisted direct files, never a recursive tree.
    if (-not $safeStage -or -not [IO.Directory]::Exists($stage)) { return }
    Assert-PlainPath $stage | Out-Null
    $owner = Read-Record ([IO.Path]::Combine($stage, 'owner.json')) $maxSmallRecordBytes
    Assert-Condition (($owner.app_id -eq $appIdentity) -and ($owner.nonce -eq $job.nonce)) 'Temporary update ownership changed; files were kept.'
    $files = @(Get-ChildItem -LiteralPath $stage -Force)
    foreach ($file in $files) {
        Assert-Condition (($allowedFiles -contains $file.Name) -and -not $file.PSIsContainer) 'Unrecognized temporary update files were kept.'
        Assert-PlainPath $file.FullName | Out-Null
    }
    foreach ($file in $files) {
        if ($file.Name -ne 'owner.json') { Remove-Item -LiteralPath $file.FullName -Force }
    }
    Remove-Item -LiteralPath ([IO.Path]::Combine($stage, 'owner.json')) -Force
    try {
        $pathLocks.RemoveEmptyDirectory($stage)  # Exact held directory; never recursive.
    } catch {
        # Leave an ownership record if a transient directory handle prevents
        # removing the now-empty stage, so the next startup can retry cleanup.
        if ($pathLocks.IsHeld($stage) -and [IO.Directory]::Exists($stage)) {
            Assert-PlainPath $stage | Out-Null
            Write-Record ([IO.Path]::Combine($stage, 'owner.json')) @{
                app_id=$appIdentity; schema_version=1; nonce=$job.nonce
            }
        }
        throw
    }
    $updatesDirectory = [IO.Path]::GetDirectoryName($stage)
    if (@(Get-ChildItem -LiteralPath $updatesDirectory -Force).Count -eq 0) {
        $pathLocks.RemoveEmptyDirectory($updatesDirectory)
    }
}

try {
    $checkedJob = Assert-PlainPath $JobPath
    Assert-Condition ([IO.Path]::GetFileName($checkedJob) -ceq 'job.json') 'Invalid update job filename.'
    $stage = [IO.Path]::GetDirectoryName($checkedJob)
    $stageName = [IO.Path]::GetFileName($stage)
    Assert-Condition ($stageName -cmatch '^update-[a-f0-9]{32}$') 'Invalid update stage name.'
    $pathLocks = New-UpdatePathLocks
    $pathLocks.HoldDirectoryPath($stage)
    Assert-PrivateUpdateDirectory $stage
    Assert-PrivateUpdateDirectory ([IO.Path]::GetDirectoryName($stage))
    $job = Read-Record $checkedJob
    $owner = Read-Record ([IO.Path]::Combine($stage, 'owner.json')) $maxSmallRecordBytes
    Assert-Condition (($job.app_id -eq $appIdentity) -and ($job.schema_version -eq 1) -and
        ($owner.app_id -eq $appIdentity) -and ($owner.schema_version -eq 1) -and
        ($job.nonce -ceq $stageName.Substring($stagePrefix.Length)) -and ($owner.nonce -ceq $job.nonce)) 'Invalid update ownership.'
    $preferences = Assert-PlainPath $job.preferences
    $allowedPreferences = @(
        [IO.Path]::GetFullPath([IO.Path]::Combine($env:APPDATA, 'Lineage2ChatTranslator')),
        [IO.Path]::GetFullPath([IO.Path]::Combine($env:APPDATA, 'L2Prominence\ChatTranslator'))
    )
    Assert-Condition ($allowedPreferences -contains $preferences) 'Unexpected preferences folder.'
    Assert-Condition ([IO.Path]::GetDirectoryName($stage) -ieq [IO.Path]::Combine($preferences, 'updates')) 'Unexpected temporary folder.'
    Assert-Condition (($job.version -cmatch '^(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})$') -and
        ($job.from_version -cmatch '^(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})$') -and
        ([Version]$job.version -gt [Version]$job.from_version)) 'The update version is not newer.'
    Assert-Condition ($job.installer_file -ceq ('Lineage-2-Chat-Translator-' + $job.version + '-Setup.exe')) 'Invalid installer filename.'
    Assert-Condition (($job.installer_bytes -ge $minInstallerBytes) -and ($job.installer_bytes -le $maxInstallerBytes) -and
        ($job.installer_sha256 -cmatch '^[a-f0-9]{64}$')) 'Invalid installer identity.'
    $executable = Assert-Installation $job.executable
    $pathLocks.HoldDirectoryPath([IO.Path]::GetDirectoryName($executable))
    Assert-PrivateUpdateDirectory ([IO.Path]::GetDirectoryName($executable))
    $safeStage = $true
    Assert-InstalledVersion $executable
    Assert-SignedRelease

    $created = $false
    $helperMutex = [Threading.Mutex]::new($false, 'Local\Lineage2ChatTranslatorUpdate', [ref]$created)
    Assert-Condition $created 'Another update is already in progress.'
    $mutexHeld = $helperMutex.WaitOne(0)
    Assert-Condition $mutexHeld 'Another update holds the installation lock.'
    $originalProcess = [Diagnostics.Process]::GetProcessById([int]$job.parent_pid)
    $originalProcess.Handle | Out-Null  # Keep the exact process handle, not a reusable PID.
    Assert-Condition ($originalProcess.MainModule.FileName -ieq $executable) 'The requesting process is not the installed translator.'
    Write-Record ([IO.Path]::Combine($stage, 'ready.json')) @{nonce=$job.nonce; pid=$PID}

    $deadline = [DateTime]::UtcNow.AddSeconds($handoffConfirmationSeconds)
    $commitPath = [IO.Path]::Combine($stage, 'commit.json')
    $cancelPath = [IO.Path]::Combine($stage, 'cancel')
    while (-not [IO.File]::Exists($commitPath)) {
        Assert-Condition (-not [IO.File]::Exists($cancelPath)) 'The update was canceled before installation.'
        Assert-Condition ([DateTime]::UtcNow -lt $deadline) 'The launcher did not confirm the update handoff.'
        Assert-Condition (-not $originalProcess.HasExited) 'The launcher exited before confirming the update.'
        Start-Sleep -Milliseconds $handoffPollMs
    }
    Assert-Condition (-not [IO.File]::Exists($cancelPath)) 'The update was canceled.'
    $commit = Read-Record $commitPath $maxSmallRecordBytes
    Assert-Condition ($commit.nonce -ceq $job.nonce) 'The update handoff confirmation is invalid.'
    $committed = $true
    Assert-Condition ($originalProcess.WaitForExit($parentExitTimeoutMs)) 'The translator did not finish closing. No installer was run.'
    $parentExited = $true
    Assert-Condition (-not [IO.File]::Exists($cancelPath)) 'The update was canceled.'
    Assert-Installation $executable | Out-Null
    Assert-InstalledVersion $executable
    Assert-SignedRelease
    $installer = Assert-PlainPath ([IO.Path]::Combine($stage, 'installer.exe'))
    $arguments = @('/SP-', '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NORESTARTAPPLICATIONS',
                   '/NOCLOSEAPPLICATIONS', ('/RESTARTEXITCODE=' + $restartRequiredExitCode), '/AUTOUPDATE=1',
                   ('/DIR=' + [IO.Path]::GetDirectoryName($executable)),
                   ('/LOG=' + [IO.Path]::Combine($stage, 'installer.log')))
    $installerLock = Open-VerifiedInstaller $installer
    $installerProcess = $null
    try {
        # Once installation starts, a failed install/cleanup may leave loadable
        # retired files. Do not run even the installed self-test until Inno has
        # confirmed its integrity/cleanup gates succeeded.
        $restartPermitted = $false
        # Inno's trusted permission helper needs a delete-capable handle. Keep
        # parents pinned and bind the root's object ID across this handoff.
        $pathLocks.ReleaseForInstaller([IO.Path]::GetDirectoryName($executable))
        $installerProcess = Start-VerifiedProcess $installer $arguments $false
        # Inno shows its normal progress window; it never closes unrelated apps.
        $installerProcess.WaitForExit()
        $exitCode = $installerProcess.ExitCode
        if ($exitCode -eq $restartRequiredExitCode) {
            $restartPermitted = $false
            throw 'The installer requested a Windows restart. Restart Windows before opening the translator again.'
        }
        Assert-Condition ($exitCode -eq 0) ('The installer did not complete (code ' + $exitCode + '). The translator was left closed.' +
            (Get-InstallerCleanupExplanation) + ' Rerun the website installer before opening it.')
        $pathLocks.HoldDirectoryPath([IO.Path]::GetDirectoryName($executable))
        Assert-PrivateUpdateDirectory ([IO.Path]::GetDirectoryName($executable))
        $restartPermitted = $true
    } finally {
        if ($installerProcess) { $installerProcess.Dispose() }
        $installerLock.Dispose()
    }
    $restartExecutable = Test-InstalledProgram @($job.version)
    $status = 'success'
    $message = 'Updated to version ' + $job.version + '. Temporary installer files were removed.'
} catch {
    $message = $_.Exception.Message
    if ($committed -and $parentExited -and $restartPermitted) {
        try {
            $restartExecutable = Test-InstalledProgram @($job.from_version, $job.version)
            $message += ' The installed translator will reopen; you can retry the update from Settings.'
        } catch {
            $restartExecutable = $null
            $message += ' The installed program could not be verified for automatic reopening. Use the website installer to repair it; saved models and settings have not been removed.'
        }
    }
} finally {
    if ($originalProcess) { $originalProcess.Dispose() }
    if ($safeStage) {
        try { Remove-OwnedStage } catch {
            $status = 'error'
            $message += ' Temporary update cleanup could not finish: ' + $_.Exception.Message
        }
    }
    if ($committed -and $job) {
        try {
            if ($message.Length -gt $maxResultMessageChars) { $message = $message.Substring(0, $maxResultMessageChars) }
            Write-Record ([IO.Path]::Combine($job.preferences, 'update-result.json')) @{
                app_id=$appIdentity; status=$status; version=$job.version; message=$message
            }
        } catch { $message += ' The update result could not be saved.' }
    }
    if ($helperMutex) {
        if ($mutexHeld) { $helperMutex.ReleaseMutex() }
        $helperMutex.Dispose()
    }
}

if ($restartExecutable) {
    try {
        $restarted = Start-VerifiedProcess $restartExecutable @() $false
        $restarted.Dispose()
    } catch {
        $status = 'error'
        $message += ' The translator could not reopen automatically. Open it from the Start menu.'
        $restartExecutable = $null
        try {
            Write-Record ([IO.Path]::Combine($job.preferences, 'update-result.json')) @{
                app_id=$appIdentity; status=$status; version=$job.version; message=$message.Substring(0, [Math]::Min($maxResultMessageChars, $message.Length))
            }
        } catch { }
    }
}
if ($pathLocks) { $pathLocks.Dispose() }
if ($committed -and $parentExited -and -not $restartExecutable) {
    # Only an actual failed update needs a final visible handoff to the user.
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show($message, 'Translator update', 'OK', 'Warning') | Out-Null
    } catch { }
}
