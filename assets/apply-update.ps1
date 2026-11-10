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
$maxFileManifestBytes = 2 * 1024 * 1024
$maxPayloadBytes = 128 * 1024 * 1024
$maxPayloadTotalBytes = 512 * 1024 * 1024
$maxPayloadFiles = 4096
$maxTransactionBytes = 16 * 1024 * 1024
$stagePrefix = 'update-'
$allowedFiles = @('owner.json', 'job.json', 'installer.part', 'installer.exe', 'apply-update.ps1',
                  'release.json', 'release.sig',
                  'ready.json', 'commit.json', 'commit.pending', 'cancel', 'self-test.json', 'installer.log',
                  'files.json', 'transaction.json', 'transaction.pending', 'receipt.pending')
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
$preserveStage = $false
$signedRelease = $null
$fileManifest = $null
$progressWindow = $null
$status = 'error'
$message = 'The update did not complete. Saved models and settings have not been removed.'

function Assert-Condition([bool]$Condition, [string]$Description) {
    if (-not $Condition) { throw $Description }
}

function New-UpdateProgressWindow {
    if ($null -eq ('L2Update.ProgressWindow' -as [type])) {
        Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;
namespace L2Update {
    // Only this thread owns the controls. File verification, copying and the
    // installed self-test cannot block its message loop or progress animation.
    public sealed class ProgressWindow : IDisposable {
        private readonly object gate = new object();
        private readonly ManualResetEvent ready = new ManualResetEvent(false);
        private readonly Thread worker;
        private string text = "Preparing update...";
        private int percent = -1;
        private bool stopping;
        private Exception startupError;
        public ProgressWindow() {
            worker = new Thread(Run);
            worker.IsBackground = true;
            worker.SetApartmentState(ApartmentState.STA);
            worker.Start();
            if (!ready.WaitOne(10000)) {
                Dispose();
                throw new InvalidOperationException("The update progress window could not open. Please retry the update.");
            }
            if (startupError != null) {
                Dispose();
                throw new InvalidOperationException("The update progress window could not open. Please retry the update.", startupError);
            }
        }
        public void Report(string value, int completed) {
            lock (gate) { text = value; percent = Math.Max(-1, Math.Min(100, completed)); }
        }
        public void Dispose() {
            lock (gate) { stopping = true; }
            worker.Join(5000);
        }
        private void Run() {
            try {
                using (var form = new Form())
                using (var timer = new System.Windows.Forms.Timer()) {
                    form.Text = "Lineage 2 Chat Translator - Update";
                    form.ClientSize = new Size(480, 158);
                    form.AutoScaleMode = AutoScaleMode.Dpi;
                    form.Font = new Font("Segoe UI", 10F);
                    form.FormBorderStyle = FormBorderStyle.FixedDialog;
                    form.StartPosition = FormStartPosition.CenterScreen;
                    form.MaximizeBox = false;
                    form.MinimizeBox = false;
                    form.ControlBox = false;
                    var label = new Label { Left = 24, Top = 22, Width = 432, Height = 28, Text = text };
                    var bar = new ProgressBar { Left = 24, Top = 62, Width = 432, Height = 22,
                        Style = ProgressBarStyle.Marquee, MarqueeAnimationSpeed = 25 };
                    var note = new Label { Left = 24, Top = 102, Width = 432, Height = 40,
                        Text = "Please wait. The translator will reopen when finished." };
                    form.Controls.AddRange(new Control[] { label, bar, note });
                    // Alt+F4/session close cannot interrupt a file transaction.
                    form.FormClosing += delegate(object sender, FormClosingEventArgs e) {
                        lock (gate) { e.Cancel = !stopping; }
                    };
                    form.Shown += delegate { ready.Set(); };
                    timer.Interval = 100;
                    timer.Tick += delegate {
                        lock (gate) {
                            if (stopping) { form.Close(); return; }
                            label.Text = text;
                            bar.Style = percent < 0 ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
                            if (percent >= 0) bar.Value = percent;
                        }
                    };
                    timer.Start();
                    Application.Run(form);
                }
            } catch (Exception error) { startupError = error; }
            finally { ready.Set(); }
        }
    }
}
'@
    }
    return [L2Update.ProgressWindow]::new()
}

function Set-UpdateProgress([string]$Text, [int]$Percent = -1) {
    if ($null -ne $progressWindow) { $progressWindow.Report($Text, $Percent) }
}

function Close-UpdateProgressWindow {
    if ($null -ne $progressWindow) {
        try { $progressWindow.Dispose() } finally { $script:progressWindow = $null }
    }
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
        public static void AssertSingleLinkFile(SafeFileHandle handle) {
            Info info;
            if (!GetFileInformationByHandle(handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (info.Links != 1 || (info.Attributes & 0x410) != 0)
                throw new IOException("Updates require ordinary program files without links.");
        }
        public static string FileObjectId(SafeFileHandle handle) {
            AssertSingleLinkFile(handle);
            Info info;
            if (!GetFileInformationByHandle(handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
            return info.Volume + ":" + info.IndexHigh + ":" + info.IndexLow;
        }
        public static void ReplaceFile(string source, string destination) {
            File.Replace(source, destination, null);
        }
        public void ReleaseDescendantsForInstaller(string path) {
            string prefix = Key(path) + "\\";
            var remove = new List<string>();
            foreach (string key in held.Keys)
                if (key.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) remove.Add(key);
            foreach (string key in remove) { held[key].Dispose(); held.Remove(key); }
        }
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
    $script:signedRelease = $document
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

function Assert-ExactFields($Value, [string[]]$Names) {
    Assert-Condition ($null -ne $Value) 'An incremental update record is missing.'
    $actual = @($Value.PSObject.Properties.Name)
    Assert-Condition ($actual.Count -eq $Names.Count) 'An incremental update record has unexpected fields.'
    foreach ($name in $Names) {
        Assert-Condition ($actual -ccontains $name) 'An incremental update record has missing fields.'
    }
}

function Assert-Integer($Value, [long]$Minimum, [long]$Maximum) {
    Assert-Condition (($Value -is [int] -or $Value -is [long]) -and
        $Value -ge $Minimum -and $Value -le $Maximum) 'An incremental update size or count is invalid.'
}

function Assert-ProgramRelativePath($Value, [bool]$Obsolete = $false) {
    Assert-Condition ($Value -is [string] -and $Value.Length -gt 0 -and $Value.Length -le 1024 -and
        $Value -notmatch '[\x00-\x1f\x7f\\:*?"<>|]') 'An incremental program path is invalid.'
    $parts = $Value.Split('/')
    foreach ($part in $parts) {
        Assert-Condition ($part.Length -gt 0 -and $part.Length -le 255 -and $part -cne '.' -and $part -cne '..' -and
            $part -ceq $part.Trim() -and -not $part.EndsWith('.') -and
            $part -notmatch '^(con|prn|aux|nul|conin\$|conout\$|com[1-9\u00b9\u00b2\u00b3]|lpt[1-9\u00b9\u00b2\u00b3])(\.|$)' -and
            $part -notmatch '^(models|logs|captures|\.git|\.build|\.build-tools|release|review_tools|settings\.json|runtime\.json|network\.log)$' -and
            -not $part.EndsWith('.local.json', [StringComparison]::OrdinalIgnoreCase) -and
            $part -notmatch '\.(log|bak|old)$' -and
            -not $part.StartsWith('unins', [StringComparison]::OrdinalIgnoreCase)) 'An incremental program path is unsafe.'
    }
    $rootFiles = @('Lineage2ChatTranslator.exe', 'USER-README.txt', 'THIRD-PARTY-NOTICES.txt')
    Assert-Condition (($parts.Length -gt 1 -and @('_internal', 'licenses') -ccontains $parts[0]) -or
        ($parts.Length -eq 1 -and $rootFiles -ccontains $Value)) 'Incremental updates are restricted to program files.'
    if ($Obsolete) {
        Assert-Condition (-not $Value.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) 'Cleanup cannot remove executables.'
    }
    return $Value
}

function Assert-PathSet([object[]]$Paths) {
    $files = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $parents = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($path in $Paths) {
        Assert-Condition (-not $parents.Contains($path) -and $files.Add($path)) 'Incremental paths collide on Windows.'
        $parent = $path
        while ($parent.Contains('/')) {
            $parent = $parent.Substring(0, $parent.LastIndexOf('/'))
            Assert-Condition (-not $files.Contains($parent)) 'An incremental file conflicts with a directory.'
            $null = $parents.Add($parent)
        }
    }
}

function Get-ProgramFile([string]$Root, [string]$Relative) {
    $relative = Assert-ProgramRelativePath $Relative
    $checkedRoot = (Assert-PlainPath $Root).TrimEnd('\')
    $path = Assert-PlainPath ([IO.Path]::Combine($checkedRoot, $relative.Replace('/', '\')))
    Assert-Condition ($path.StartsWith($checkedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) 'The update escaped its installation folder.'
    return $path
}

function Get-ProgramTemporary([string]$Root, $Entry) {
    Assert-Condition ($Entry.temporary -ceq ($Entry.path + '.l2ct-' + $job.nonce)) 'An update temporary path is invalid.'
    return Assert-PlainPath ((Get-ProgramFile $Root $Entry.path) + '.l2ct-' + $job.nonce)
}

function Get-FileIdentity([string]$Path) {
    $checked = Assert-PlainPath $Path
    Assert-Condition (-not [IO.Directory]::Exists($checked)) 'A program file was replaced with a directory.'
    if (-not [IO.File]::Exists($checked)) { return $null }
    $stream = [IO.File]::Open($checked, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        [L2Update.PathLocks]::AssertSingleLinkFile($stream.SafeFileHandle)
        Assert-Condition ($stream.Length -le $maxPayloadBytes) 'A program file exceeds the update size limit.'
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $hash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
        return [pscustomobject]@{bytes=$stream.Length; sha256=$hash; object_id=[L2Update.PathLocks]::FileObjectId($stream.SafeFileHandle)}
    } finally { $stream.Dispose() }
}

function Assert-FileMatches([string]$Path, $Record) {
    $identity = Get-FileIdentity $Path
    Assert-Condition ($null -ne $identity -and $identity.bytes -eq $Record.bytes -and
        $identity.sha256 -ceq $Record.sha256) 'A program file failed its signed size/checksum verification.'
}

function Assert-FileManifest {
    Assert-Condition ($null -ne $signedRelease -and
        @($signedRelease.PSObject.Properties.Name) -ccontains 'incremental') 'This release has no incremental update.'
    $incremental = $signedRelease.incremental
    Assert-ExactFields $incremental @('schema_version', 'manifest')
    Assert-Integer $incremental.schema_version 1 1
    $descriptor = $incremental.manifest
    Assert-ExactFields $descriptor @('file', 'bytes', 'sha256')
    Assert-Condition ($descriptor.file -ceq ('updates/' + $job.version + '/files.json') -and
        $descriptor.sha256 -is [string] -and $descriptor.sha256 -cmatch '^[a-f0-9]{64}$') 'The signed file manifest identity is invalid.'
    Assert-Integer $descriptor.bytes 1 $maxFileManifestBytes
    $manifestPath = Assert-PlainPath ([IO.Path]::Combine($stage, 'files.json'))
    Assert-FileMatches $manifestPath $descriptor
    $manifest = Read-Record $manifestPath $maxFileManifestBytes
    Assert-ExactFields $manifest @('schema_version', 'product', 'platform', 'version', 'files', 'cleanup')
    Assert-Integer $manifest.schema_version 1 1
    Assert-Condition ($manifest.product -ceq $productName -and $manifest.platform -ceq 'windows-x64' -and
        $manifest.version -ceq $job.version -and $manifest.files -is [array]) 'The file manifest does not belong to this release.'
    Assert-Integer $manifest.files.Count 1 $maxPayloadFiles
    [long]$total = 0; [long]$compressed = 0
    $paths = [Collections.Generic.List[string]]::new()
    foreach ($file in $manifest.files) {
        Assert-ExactFields $file @('path', 'bytes', 'sha256', 'blob')
        $null = $paths.Add((Assert-ProgramRelativePath $file.path))
        Assert-Integer $file.bytes 0 $maxPayloadBytes
        Assert-Condition ($file.sha256 -is [string] -and $file.sha256 -cmatch '^[a-f0-9]{64}$') 'A program checksum is invalid.'
        Assert-ExactFields $file.blob @('bytes', 'sha256')
        Assert-Integer $file.blob.bytes 1 $maxPayloadBytes
        Assert-Condition ($file.blob.sha256 -is [string] -and $file.blob.sha256 -cmatch '^[a-f0-9]{64}$') 'A compressed program checksum is invalid.'
        $total += $file.bytes; $compressed += $file.blob.bytes
    }
    Assert-Condition ($paths -ccontains 'Lineage2ChatTranslator.exe' -and $total -le $maxPayloadTotalBytes -and
        $compressed -le $maxPayloadTotalBytes) 'The file manifest is incomplete or exceeds its size limit.'
    Assert-PathSet @($paths)
    $cleanup = $manifest.cleanup
    Assert-ExactFields $cleanup @('schema_version', 'product', 'version', 'current_paths', 'obsolete_files')
    Assert-Integer $cleanup.schema_version 1 1
    Assert-Condition ($cleanup.product -ceq $productName -and $cleanup.version -ceq $job.version -and
        $cleanup.current_paths -is [array] -and $cleanup.obsolete_files -is [array] -and
        $cleanup.current_paths.Count -eq $paths.Count -and $cleanup.obsolete_files.Count -le 10000) 'The cleanup plan does not match the file manifest.'
    Assert-PathSet @($cleanup.current_paths)
    foreach ($path in $cleanup.current_paths) {
        Assert-Condition ($paths -ccontains $path) 'The cleanup plan does not describe the current files.'
    }
    $obsoletePaths = [Collections.Generic.List[string]]::new()
    foreach ($old in $cleanup.obsolete_files) {
        Assert-ExactFields $old @('path', 'hashes')
        $null = $obsoletePaths.Add((Assert-ProgramRelativePath $old.path $true))
        Assert-Condition ($old.hashes -is [array] -and $old.hashes.Count -gt 0 -and $old.hashes.Count -le 128) 'An obsolete checksum list is invalid.'
        foreach ($hash in $old.hashes) {
            Assert-Condition ($hash -is [string] -and $hash -cmatch '^[a-f0-9]{64}$') 'An obsolete checksum is invalid.'
        }
    }
    Assert-PathSet @($obsoletePaths)
    Assert-PathSet @(@($paths) + @($obsoletePaths))
    $script:fileManifest = $manifest
}

function Write-Transaction($Value) {
    $path = Assert-PlainPath ([IO.Path]::Combine($stage, 'transaction.json'))
    $pending = Assert-PlainPath ([IO.Path]::Combine($stage, 'transaction.pending'))
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Compress -Depth 10))
    Assert-Condition ($bytes.Length -le $maxTransactionBytes) 'The update recovery journal exceeded its size limit.'
    $stream = [IO.File]::Open($pending, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
    if ([IO.File]::Exists($path)) { [L2Update.PathLocks]::ReplaceFile($pending, $path) }
    else { [IO.File]::Move($pending, $path) }
}

function Hold-ProgramParent([string]$Path) {
    $parent = Assert-PlainPath ([IO.Path]::GetDirectoryName($Path))
    $missing = [Collections.Generic.Stack[string]]::new()
    $cursor = $parent
    while (-not [IO.Directory]::Exists($cursor)) {
        Assert-Condition (-not [IO.File]::Exists($cursor)) 'An update directory was replaced by a file.'
        $missing.Push($cursor)
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    $pathLocks.HoldDirectoryPath($cursor)
    while ($missing.Count -gt 0) {
        $next = $missing.Pop()
        $null = [IO.Directory]::CreateDirectory((Assert-PlainPath $next))
        $pathLocks.HoldDirectoryPath($next)
    }
    $pathLocks.HoldDirectoryPath($parent)
}

function Copy-FlushedFile([string]$Source, [string]$Destination, [bool]$Overwrite) {
    $sourcePath = Assert-PlainPath $Source
    $destinationPath = Assert-PlainPath $Destination
    $sourceFile = [IO.File]::Open($sourcePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        [L2Update.PathLocks]::AssertSingleLinkFile($sourceFile.SafeFileHandle)
        $mode = [IO.FileMode]::CreateNew
        if ($Overwrite) {
            if ([IO.File]::Exists($destinationPath)) { $null = Get-FileIdentity $destinationPath }
            $mode = [IO.FileMode]::Create
        }
        $output = [IO.File]::Open($destinationPath, $mode, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $sourceFile.CopyTo($output); $output.Flush($true) } finally { $output.Dispose() }
    } finally { $sourceFile.Dispose() }
}

function New-InstalledReceipts([int]$BackupStart) {
    $receipts = [Collections.Generic.List[object]]::new()
    $sources = @('files.json', 'release.json', 'release.sig')
    $names = @('installed-files.json', 'installed-release.json', 'installed-release.sig')
    for ($index = 0; $index -lt $names.Length; $index++) {
        $destination = Assert-PlainPath ([IO.Path]::Combine($job.preferences, $names[$index]))
        $source = Assert-PlainPath ([IO.Path]::Combine($stage, $sources[$index]))
        $target = Get-FileIdentity $source
        $original = Get-FileIdentity $destination
        $receipt = [pscustomobject]@{name=$names[$index]; source=$sources[$index]; existed=($null -ne $original);
            bytes=0; sha256=''; backup=''; target_sha256=$target.sha256}
        if ($null -ne $original) {
            $receipt.bytes=$original.bytes; $receipt.sha256=$original.sha256
            $receipt.backup='rollback-'+($BackupStart+$index).ToString('00000')+'.bak'
            $backup=Assert-PlainPath ([IO.Path]::Combine($stage,$receipt.backup))
            Copy-FlushedFile $destination $backup $false
            Assert-FileMatches $backup $receipt
        }
        $receipts.Add($receipt)
    }
    return ,$receipts.ToArray()
}

function Save-InstalledReceipts($Transaction) {
    foreach ($receipt in $Transaction.receipts) {
        $source = Assert-PlainPath ([IO.Path]::Combine($stage, $receipt.source))
        $destination = Assert-PlainPath ([IO.Path]::Combine($job.preferences, $receipt.name))
        $pending = Assert-PlainPath ([IO.Path]::Combine($stage, 'receipt.pending'))
        Copy-FlushedFile $source $pending $false
        Assert-Condition ((Get-FileIdentity $pending).sha256 -ceq $receipt.target_sha256) 'Installed update metadata changed while saving.'
        if ([IO.File]::Exists($destination)) {
            $null = Get-FileIdentity $destination
            [L2Update.PathLocks]::ReplaceFile($pending, $destination)
        } else { [IO.File]::Move($pending, $destination) }
    }
}

function Restore-IncrementalTransaction($Transaction, [string]$Root) {
    Set-UpdateProgress 'Restoring the previous version...'
    # Every backup was verified and flushed before the journal became active.
    # Restore all planned paths: this also covers a crash between mutation/journal writes.
    foreach ($entry in $Transaction.entries) {
        $destination = Get-ProgramFile $Root $entry.path
        Hold-ProgramParent $destination
        $temporary = Get-ProgramTemporary $Root $entry
        if ([IO.File]::Exists($temporary)) {
            Assert-Condition ((Get-FileIdentity $temporary).object_id -ceq $entry.temporary_id) 'An update temporary file changed identity and was kept.'
            [IO.File]::Delete($temporary)
        }
        if ($entry.existed) {
            $backup = Assert-PlainPath ([IO.Path]::Combine($stage, $entry.backup))
            Assert-FileMatches $backup $entry
            $source = [IO.File]::Open($backup, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            try {
                [L2Update.PathLocks]::AssertSingleLinkFile($source.SafeFileHandle)
                $output = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try {
                    $entry.temporary_id = [L2Update.PathLocks]::FileObjectId($output.SafeFileHandle)
                    Write-Transaction $Transaction
                    $source.CopyTo($output); $output.Flush($true)
                } finally { $output.Dispose() }
            } finally { $source.Dispose() }
            Assert-FileMatches $temporary $entry
            if ([IO.File]::Exists($destination)) { [L2Update.PathLocks]::ReplaceFile($temporary, $destination) }
            else { [IO.File]::Move($temporary, $destination) }
            Assert-FileMatches $destination $entry
        } elseif ([IO.File]::Exists($destination)) {
            # A raced unknown file is never deleted on rollback.
            Assert-Condition ((Get-FileIdentity $destination).sha256 -ceq $entry.target_sha256) 'A newly added file changed during rollback; it was kept.'
            [IO.File]::Delete($destination)
            Assert-Condition (-not [IO.File]::Exists($destination)) 'A newly added file could not be removed during rollback.'
        }
    }
    foreach ($receipt in $Transaction.receipts) {
        Assert-Condition (@('installed-files.json', 'installed-release.json', 'installed-release.sig') -ccontains $receipt.name) 'An installed metadata recovery name is invalid.'
        $destination = Assert-PlainPath ([IO.Path]::Combine($job.preferences, $receipt.name))
        if ($receipt.existed) {
            $backup=Assert-PlainPath ([IO.Path]::Combine($stage,$receipt.backup))
            Assert-FileMatches $backup $receipt
            Copy-FlushedFile $backup $destination $true
            Assert-FileMatches $destination $receipt
        } elseif ([IO.File]::Exists($destination)) {
            Assert-Condition ((Get-FileIdentity $destination).sha256 -ceq $receipt.target_sha256) 'An installed metadata recovery file changed and was kept.'
            [IO.File]::Delete($destination)
        }
    }
    $Transaction.state = 'rolled_back'
    Write-Transaction $Transaction
}

function Invoke-IncrementalUpdate([string]$Root) {
    Set-UpdateProgress 'Checking installed files...' 0
    Assert-FileManifest
    $entries = [Collections.Generic.List[object]]::new()
    $checkedFiles = 0
    foreach ($file in $fileManifest.files) {
        $destination = Get-ProgramFile $Root $file.path
        $identity = Get-FileIdentity $destination
        $checkedFiles++
        Set-UpdateProgress 'Checking installed files...' ([int](100 * $checkedFiles / $fileManifest.files.Count))
        if ($null -ne $identity -and $identity.bytes -eq $file.bytes -and $identity.sha256 -ceq $file.sha256) { continue }
        $payload = Assert-PlainPath ([IO.Path]::Combine($stage, $file.sha256 + '.payload'))
        Assert-FileMatches $payload $file
        $entries.Add([pscustomobject]@{path=$file.path; existed=($null -ne $identity); bytes=0; sha256='';
            backup=''; target_sha256=$file.sha256; remove=$false; temporary=($file.path+'.l2ct-'+$job.nonce); temporary_id=''})
    }
    foreach ($old in $fileManifest.cleanup.obsolete_files) {
        $destination = Get-ProgramFile $Root $old.path
        $identity = Get-FileIdentity $destination
        if ($null -eq $identity) { continue }
        if ($old.hashes -cnotcontains $identity.sha256) {
            Assert-Condition ([IO.Path]::GetExtension($old.path) -notin @('.dll', '.pyd', '.exe')) ('Retired program file ' + $old.path + ' differs from known releases. Use the website installer to repair it.')
            continue
        }
        $entries.Add([pscustomobject]@{path=$old.path; existed=$true; bytes=0; sha256='';
            backup=''; target_sha256=''; remove=$true; temporary=($old.path+'.l2ct-'+$job.nonce); temporary_id=''})
    }
    # Snapshot every original before allowing a single installation change.
    Set-UpdateProgress 'Backing up application files...' 0
    for ($index = 0; $index -lt $entries.Count; $index++) {
        $entry = $entries[$index]
        $destination = Get-ProgramFile $Root $entry.path
        Hold-ProgramParent $destination
        if ($entry.existed) {
            $identity = Get-FileIdentity $destination
            Assert-Condition ($null -ne $identity) 'A program file disappeared before its backup.'
            $entry.bytes = $identity.bytes; $entry.sha256 = $identity.sha256
            $entry.backup = 'rollback-' + $index.ToString('00000') + '.bak'
            $backup = Assert-PlainPath ([IO.Path]::Combine($stage, $entry.backup))
            Assert-Condition (-not [IO.File]::Exists($backup)) 'An update backup already exists.'
            [IO.File]::Copy($destination, $backup, $false)
            Assert-FileMatches $backup $entry
            $stream = [IO.File]::Open($backup, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $stream.Flush($true) } finally { $stream.Dispose() }
        } else {
            Assert-Condition (-not [IO.File]::Exists($destination)) 'A new program path changed before installation.'
        }
        Set-UpdateProgress 'Backing up application files...' ([int](100 * ($index + 1) / $entries.Count))
    }
    Set-UpdateProgress 'Backing up application files...' 100
    $transaction = [pscustomobject]@{app_id=$appIdentity; schema_version=1; nonce=$job.nonce;
        state='applying'; version=$job.version; from_version=$job.from_version; entries=$entries.ToArray();
        receipts=(New-InstalledReceipts $entries.Count)}
    Write-Transaction $transaction
    $script:restartPermitted = $false
    try {
        Set-UpdateProgress 'Installing application files...' 0
        $appliedFiles = 0
        foreach ($entry in $entries) {
            Write-Transaction $transaction
            $destination = Get-ProgramFile $Root $entry.path
            if ($entry.existed) { Assert-FileMatches $destination $entry }
            else { Assert-Condition (-not [IO.File]::Exists($destination)) 'A new program file appeared during installation.' }
            if ($entry.remove) { [IO.File]::Delete($destination) }
            else {
                $payload = Assert-PlainPath ([IO.Path]::Combine($stage, $entry.target_sha256 + '.payload'))
                $source = [IO.File]::Open($payload, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
                try {
                    [L2Update.PathLocks]::AssertSingleLinkFile($source.SafeFileHandle)
                    $sha = [Security.Cryptography.SHA256]::Create()
                    try { $hash = [BitConverter]::ToString($sha.ComputeHash($source)).Replace('-', '').ToLowerInvariant() }
                    finally { $sha.Dispose() }
                    Assert-Condition ($hash -ceq $entry.target_sha256) 'A staged update file changed.'
                    $source.Position = 0
                    $temporary = Get-ProgramTemporary $Root $entry
                    $output = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                    try {
                        $entry.temporary_id = [L2Update.PathLocks]::FileObjectId($output.SafeFileHandle)
                        Write-Transaction $transaction
                        $source.CopyTo($output); $output.Flush($true)
                    } finally { $output.Dispose() }
                } finally { $source.Dispose() }
                Assert-Condition ((Get-FileIdentity $temporary).sha256 -ceq $entry.target_sha256) 'An update temporary file failed verification.'
                if ($entry.existed) { [L2Update.PathLocks]::ReplaceFile($temporary, $destination) }
                else { [IO.File]::Move($temporary, $destination) }
            }
            $appliedFiles++
            Set-UpdateProgress 'Installing application files...' ([int](100 * $appliedFiles / $entries.Count))
        }
        Set-UpdateProgress 'Installing application files...' 100
        Set-UpdateProgress 'Verifying installed files...' 0
        $verifiedFiles = 0
        foreach ($file in $fileManifest.files) {
            Assert-FileMatches (Get-ProgramFile $Root $file.path) $file
            $verifiedFiles++
            Set-UpdateProgress 'Verifying installed files...' ([int](100 * $verifiedFiles / $fileManifest.files.Count))
        }
        Set-UpdateProgress 'Checking the updated translator...'
        $verified = Test-InstalledProgram @($job.version)
        Set-UpdateProgress 'Saving update information...'
        Save-InstalledReceipts $transaction
        $transaction.state = 'committed'
        Write-Transaction $transaction
        $script:restartPermitted = $true
        return $verified
    } catch {
        $failure = $_.Exception.Message
        try {
            Restore-IncrementalTransaction $transaction $Root
            $script:restartPermitted = $true
        } catch {
            $script:preserveStage = $true
            $transaction.state = 'rollback_failed'
            try { Write-Transaction $transaction } catch { }
            throw ('The file update failed and rollback could not finish. Recovery files were kept at ' + $stage + '. Use the website installer to repair the translator. ' + $_.Exception.Message)
        }
        throw ('The file update was rolled back. ' + $failure)
    }
}

function New-FullInstallerRequest([string]$Url) {
    Assert-Condition ($Url -ceq ('https://40.160.140.227/downloads/' + $job.installer_file)) 'Unexpected installer fallback URL.'
    $request = [Net.HttpWebRequest]::Create($Url)
    $request.AllowAutoRedirect = $false
    $request.Proxy = $null
    $request.Timeout = 20000
    $request.ReadWriteTimeout = 20000
    $request.UserAgent = 'Lineage2ChatTranslator/' + $job.from_version
    return $request
}

function Download-FullInstaller {
    Set-UpdateProgress 'Downloading the full installer...' 0
    Assert-SignedRelease
    $url = 'https://40.160.140.227/downloads/' + $job.installer_file
    $request = New-FullInstallerRequest $url
    $response = $null; $source = $null; $output = $null
    $part = Assert-PlainPath ([IO.Path]::Combine($stage, 'installer.part'))
    $target = Assert-PlainPath ([IO.Path]::Combine($stage, 'installer.exe'))
    Assert-Condition (-not [IO.File]::Exists($part) -and -not [IO.File]::Exists($target)) 'The installer fallback was already attempted.'
    try {
        $response = $request.GetResponse()
        Assert-Condition ($response.StatusCode -eq [Net.HttpStatusCode]::OK -and $response.ResponseUri.AbsoluteUri -ceq $url -and
            ($response.ContentLength -eq -1 -or $response.ContentLength -eq $job.installer_bytes)) 'The installer server returned an unexpected response.'
        $source = $response.GetResponseStream()
        $output = [IO.File]::Open($part, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $buffer = [byte[]]::new(262144); [long]$received = 0
        $deadline = [DateTime]::UtcNow.AddSeconds(300)
        while ($true) {
            Assert-Condition ([DateTime]::UtcNow -lt $deadline) 'The installer fallback download timed out.'
            $count = $source.Read($buffer, 0, $buffer.Length)
            if ($count -eq 0) { break }
            $received += $count
            Assert-Condition ($received -le $job.installer_bytes) 'The installer fallback exceeded its signed size.'
            $output.Write($buffer, 0, $count)
            Set-UpdateProgress 'Downloading the full installer...' ([int](100 * $received / $job.installer_bytes))
        }
        $output.Flush($true); $output.Dispose(); $output = $null
        Assert-Condition ($received -eq $job.installer_bytes) 'The installer fallback download was incomplete.'
        Set-UpdateProgress 'Verifying the installer...'
        $lock = Open-VerifiedInstaller $part
        try {
            $lock.Position = 0
            Assert-Condition ($lock.ReadByte() -eq 77 -and $lock.ReadByte() -eq 90) 'The fallback is not a Windows installer.'
        } finally { $lock.Dispose() }
        [IO.File]::Move($part, $target)
    } finally {
        if ($output) { $output.Dispose() }
        if ($source) { $source.Dispose() }
        if ($response) { $response.Dispose() }
        $request.Abort()
    }
}

function Remove-OwnedStage {
    # Delete only this job's allowlisted direct files, never a recursive tree.
    if ($preserveStage -or -not $safeStage -or -not [IO.Directory]::Exists($stage)) { return }
    Assert-Condition (-not [IO.File]::Exists((Assert-PlainPath ([IO.Path]::Combine($stage, 'transaction.pending'))))) 'An unfinished journal has recovery files which were kept.'
    $journal = [IO.Path]::Combine($stage, 'transaction.json')
    if ([IO.File]::Exists($journal)) {
        $transaction = Read-Record $journal $maxTransactionBytes
        Assert-Condition (@('committed', 'rolled_back') -ccontains $transaction.state) 'An unfinished update has recovery files which were kept.'
    }
    Assert-PlainPath $stage | Out-Null
    $owner = Read-Record ([IO.Path]::Combine($stage, 'owner.json')) $maxSmallRecordBytes
    Assert-Condition (($owner.app_id -eq $appIdentity) -and ($owner.nonce -eq $job.nonce)) 'Temporary update ownership changed; files were kept.'
    $files = @(Get-ChildItem -LiteralPath $stage -Force)
    foreach ($file in $files) {
        Assert-Condition ((($allowedFiles -contains $file.Name) -or $file.Name -cmatch '^[a-f0-9]{64}\.payload$' -or
            $file.Name -cmatch '^rollback-[0-9]{5}\.bak$') -and -not $file.PSIsContainer) 'Unrecognized temporary update files were kept.'
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
    $incrementalMode = @($job.PSObject.Properties.Name) -ccontains 'mode' -and $job.mode -ceq 'incremental'
    if (@($job.PSObject.Properties.Name) -ccontains 'mode') {
        Assert-Condition (@('incremental', 'installer') -ccontains $job.mode) 'Unknown update mode.'
    }
    if ($incrementalMode) { Assert-FileManifest }

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
    if ($incrementalMode) {
        $progressWindow = New-UpdateProgressWindow
        Set-UpdateProgress 'Checking installed files...'
    }
    Assert-Condition (-not [IO.File]::Exists($cancelPath)) 'The update was canceled.'
    Assert-Installation $executable | Out-Null
    Assert-InstalledVersion $executable
    Assert-SignedRelease
    $incrementalFinished = $false
    if ($incrementalMode) {
        try {
            $restartExecutable = Invoke-IncrementalUpdate ([IO.Path]::GetDirectoryName($executable))
            $incrementalFinished = $true
        } catch {
            Assert-Condition (-not $preserveStage -and $restartPermitted) $_.Exception.Message
            # Any changed files have already been restored before fallback.
            # The complete installer has the same pinned signature/checksum gates.
            Download-FullInstaller
        }
    }
    if (-not $incrementalFinished) {
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
        $pathLocks.ReleaseDescendantsForInstaller([IO.Path]::GetDirectoryName($executable))
        $pathLocks.ReleaseForInstaller([IO.Path]::GetDirectoryName($executable))
        Close-UpdateProgressWindow
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
    if ($incrementalMode) { $progressWindow = New-UpdateProgressWindow }
    Set-UpdateProgress 'Checking the updated translator...'
    $restartExecutable = Test-InstalledProgram @($job.version)
    }
    # The uninstaller registration remains the same; Settings and Windows now
    # report the successfully verified file update's application version.
    Set-ItemProperty -LiteralPath $registryPath -Name DisplayVersion -Value $job.version
    $status = 'success'
    $message = 'Updated to version ' + $job.version + '. Temporary update files were removed.'
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
    Set-UpdateProgress 'Finishing update...'
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
    Set-UpdateProgress 'Reopening the translator...'
    Close-UpdateProgressWindow
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
