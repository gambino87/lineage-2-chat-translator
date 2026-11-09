# Runs from the installer's private temporary folder using Windows PowerShell.
# In particular, this helper must never load the installed Python runtime: that
# runtime may still load a retired DLL which this cleanup needs to remove.
[CmdletBinding()]
param([string]$InstallRoot, [string]$Version, [string]$ResultPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:CleanupProduct = 'Lineage 2 Chat Translator'
$script:CleanupPlanBytes = 2 * 1024 * 1024
$script:CleanupFileBytes = 128 * 1024 * 1024
$script:CleanupRegistryKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{1D8CE3C7-53A6-4C67-AC9F-1C6F0D6B1920}_is1'

function Assert-CleanupCondition($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Test-CleanupCloudPlaceholder([string]$Path) {
    # .NET Framework exposes hydrated OneDrive ancestor folders as reparse
    # points. Inspect the tag without following it; allow only Microsoft's
    # CLOUD/CLOUD_1..F tags, never a name surrogate, junction or unknown filter.
    # Tags: https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/c8e77b37-3909-4fe6-a4ea-2b9d423b1ee4
    if ($null -eq ('L2Cleanup.FileTags' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace L2Cleanup {
    public static class FileTags {
        [StructLayout(LayoutKind.Sequential)]
        private struct AttributeTag { public uint Attributes; public uint Tag; }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern SafeFileHandle CreateFileW(string name, uint access,
            uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool GetFileInformationByHandleEx(SafeFileHandle file,
            int infoClass, out AttributeTag info, uint size);
        public static bool IsCloudTag(uint tag) {
            return (tag & 0x20000000U) == 0 && (tag & 0xFFFF0FFFU) == 0x9000001AU;
        }
        public static bool IsCloudPlaceholder(string path) {
            // OPEN_REPARSE_POINT | BACKUP_SEMANTICS opens the directory itself.
            using (SafeFileHandle file=CreateFileW(path, 0, 7, IntPtr.Zero, 3,
                    0x02200000U, IntPtr.Zero)) {
                if (file.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                AttributeTag info;
                if (!GetFileInformationByHandleEx(file, 9, out info, 8))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                return (info.Attributes & 0x400U) != 0 && IsCloudTag(info.Tag);
            }
        }
    }
}
'@
    }
    return [L2Cleanup.FileTags]::IsCloudPlaceholder($Path)
}

function Assert-CleanupPlainPath([string]$Path) {
    Assert-CleanupCondition ($Path -match '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)') 'Cleanup requires an absolute path.'
    $full = [IO.Path]::GetFullPath($Path)
    $cursor = $full
    while ($cursor) {
        try { $attributes = [IO.File]::GetAttributes($cursor) }
        catch [IO.FileNotFoundException] { $attributes = $null }
        catch [IO.DirectoryNotFoundException] { $attributes = $null }
        if ($null -ne $attributes) {
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Assert-CleanupCondition (Test-CleanupCloudPlaceholder $cursor) 'Cleanup will not follow linked files or folders.'
            }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
    return $full
}

function Assert-CleanupRelativePath($Value) {
    Assert-CleanupCondition ($Value -is [string] -and $Value.Length -gt 0 -and $Value.Length -le 1024) 'Invalid program file path.'
    Assert-CleanupCondition ($Value -notmatch '[\x00-\x1f\x7f\\:*?"<>|]') 'Program file paths cannot contain Windows path syntax or wildcards.'
    foreach ($part in $Value.Split('/')) {
        Assert-CleanupCondition ($part.Length -gt 0 -and $part.Length -le 255 -and $part -ne '.' -and $part -ne '..' -and $part -ceq $part.Trim() -and -not $part.EndsWith('.')) 'Program file paths must be canonical relative names.'
        Assert-CleanupCondition ($part -notmatch '^(?:con|prn|aux|nul|conin\$|conout\$|com[1-9\u00b9\u00b2\u00b3]|lpt[1-9\u00b9\u00b2\u00b3])(?:\.|$)') 'Windows device names cannot be program file paths.'
    }
    return $Value
}

function Assert-CleanupFields($Value, [string[]]$Fields) {
    Assert-CleanupCondition ($Value -is [pscustomobject]) 'Invalid program cleanup object.'
    $actual = @($Value.PSObject.Properties.Name)
    Assert-CleanupCondition ($actual.Count -eq $Fields.Count) 'Invalid program cleanup fields.'
    foreach ($name in $actual) {
        Assert-CleanupCondition ($Fields -ccontains $name) 'Invalid program cleanup fields.'
    }
}

function New-CleanupPathIndex {
    return @{Files = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase); Parents = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)}
}

function Get-CleanupParents([string]$Path) {
    $position = $Path.IndexOf('/')
    while ($position -ge 0) {
        $Path.Substring(0, $position)
        $position = $Path.IndexOf('/', $position + 1)
    }
}

function Add-CleanupIndexedPath($Index, [string]$Path) {
    Assert-CleanupCondition (-not $Index.Files.Contains($Path) -and -not $Index.Parents.Contains($Path)) 'Program file paths collide on Windows.'
    foreach ($parent in @(Get-CleanupParents $Path)) {
        Assert-CleanupCondition (-not $Index.Files.Contains($parent)) 'A program file path conflicts with another file directory.'
        [void]$Index.Parents.Add($parent)
    }
    [void]$Index.Files.Add($Path)
}

function Test-CleanupPlan($Plan, [string]$ExpectedVersion) {
    Assert-CleanupCondition ($ExpectedVersion -cmatch '^[0-9]+\.[0-9]+\.[0-9]+$') 'Invalid cleanup version.'
    Assert-CleanupFields $Plan @('schema_version', 'product', 'version', 'current_paths', 'obsolete_files')
    Assert-CleanupCondition (($Plan.schema_version -is [int] -or $Plan.schema_version -is [long]) -and $Plan.schema_version -eq 1 -and $Plan.product -is [string] -and $Plan.product -ceq $script:CleanupProduct -and $Plan.version -is [string] -and $Plan.version -ceq $ExpectedVersion) 'The program cleanup plan belongs to a different release.'
    Assert-CleanupCondition ($Plan.current_paths -is [Array] -and $Plan.current_paths.Count -gt 0 -and $Plan.current_paths.Count -le 10000 -and $Plan.obsolete_files -is [Array] -and $Plan.obsolete_files.Count -le 10000) 'The program cleanup plan has too many or missing file entries.'
    $current = New-CleanupPathIndex
    foreach ($value in $Plan.current_paths) { Add-CleanupIndexedPath $current (Assert-CleanupRelativePath $value) }
    $obsolete = New-CleanupPathIndex
    foreach ($entry in $Plan.obsolete_files) {
        Assert-CleanupFields $entry @('path', 'hashes')
        $path = Assert-CleanupRelativePath $entry.path
        $parts = $path.Split('/')
        Assert-CleanupCondition (($parts.Count -gt 1 -and $parts[0] -in @('_internal', 'licenses')) -or ($parts.Count -eq 1 -and $parts[0] -in @('USER-README.txt', 'THIRD-PARTY-NOTICES.txt'))) 'Cleanup is restricted to packaged libraries, assets, licenses and documentation.'
        foreach ($part in $parts) {
            Assert-CleanupCondition ($part -notin @('models', 'logs', 'settings.json', 'runtime.json', 'Network.log') -and -not $part.EndsWith('.local.json', [StringComparison]::OrdinalIgnoreCase)) 'Models, settings and logs cannot be cleanup targets.'
        }
        $name = $parts[-1]
        Assert-CleanupCondition (-not $name.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -and -not $name.StartsWith('unins', [StringComparison]::OrdinalIgnoreCase)) 'Cleanup cannot remove executables or uninstaller files.'
        Assert-CleanupCondition (-not $current.Files.Contains($path) -and -not $current.Parents.Contains($path)) 'Cleanup cannot remove or overlap current program files.'
        foreach ($parent in @(Get-CleanupParents $path)) {
            Assert-CleanupCondition (-not $current.Files.Contains($parent)) 'Cleanup cannot remove or overlap current program files.'
        }
        Add-CleanupIndexedPath $obsolete $path
        Assert-CleanupCondition ($entry.hashes -is [Array] -and $entry.hashes.Count -gt 0 -and $entry.hashes.Count -le 128) 'Obsolete program file hashes are invalid.'
        foreach ($hash in $entry.hashes) {
            Assert-CleanupCondition ($hash -is [string] -and $hash -cmatch '^[a-f0-9]{64}$') 'Obsolete program file hashes are invalid.'
        }
    }
    return $Plan
}

function Assert-CleanupJsonFields([byte[]]$Bytes) {
    # ConvertFrom-Json alone silently accepts duplicate keys in Windows
    # PowerShell. The framework reader exposes every key before conversion.
    Add-Type -AssemblyName System.Runtime.Serialization
    $quotas = [Xml.XmlDictionaryReaderQuotas]::new()
    $quotas.MaxDepth = 12
    $quotas.MaxStringContentLength = $script:CleanupPlanBytes
    $quotas.MaxArrayLength = 20000
    $quotas.MaxNameTableCharCount = $script:CleanupPlanBytes
    $reader = [Runtime.Serialization.Json.JsonReaderWriterFactory]::CreateJsonReader($Bytes, $quotas)
    $objects = @{}
    try {
        while ($reader.Read()) {
            if ($reader.NodeType -eq [Xml.XmlNodeType]::Element) {
                if ($objects.ContainsKey($reader.Depth - 1)) {
                    $key = $reader.LocalName
                    if ($reader.NamespaceURI -eq 'item') { $key = $reader.GetAttribute('item') }
                    Assert-CleanupCondition ($objects[$reader.Depth - 1].Add($key)) 'The cleanup plan contains duplicate fields.'
                }
                if ($reader.GetAttribute('type') -eq 'object') {
                    $objects[$reader.Depth] = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
                }
            } elseif ($reader.NodeType -eq [Xml.XmlNodeType]::EndElement) {
                $objects.Remove($reader.Depth)
            }
        }
    } finally { $reader.Close() }
}

function Read-CleanupPlan([string]$Path, [string]$ExpectedVersion) {
    $path = Assert-CleanupPlainPath $Path
    Assert-CleanupCondition ([IO.File]::Exists($path)) 'The program cleanup plan is missing or not a regular file.'
    $buffer = [byte[]]::new($script:CleanupPlanBytes + 1)
    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $count = 0
        while ($count -lt $buffer.Length) {
            $read = $stream.Read($buffer, $count, $buffer.Length - $count)
            if ($read -eq 0) { break }
            $count += $read
        }
    } finally { $stream.Dispose() }
    Assert-CleanupCondition ($count -le $script:CleanupPlanBytes) 'The program cleanup plan is too large.'
    $text = [Text.UTF8Encoding]::new($false, $true).GetString($buffer, 0, $count).TrimStart([char]0xfeff)
    Assert-CleanupJsonFields ([Text.Encoding]::UTF8.GetBytes($text))
    $plan = ConvertFrom-Json -InputObject $text
    return Test-CleanupPlan $plan $ExpectedVersion
}

function Get-CleanupCandidate([string]$Root, [string]$Relative) {
    $path = Assert-CleanupPlainPath ([IO.Path]::Combine($Root, $Relative.Replace('/', '\')))
    Assert-CleanupCondition ($path.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) 'Cleanup path escaped the installation.'
    return $path
}

function Get-CleanupFileState([string]$Path) {
    try { $attributes = [IO.File]::GetAttributes($Path) }
    catch [IO.FileNotFoundException] { return $null }
    catch [IO.DirectoryNotFoundException] { return $null }
    Assert-CleanupCondition (($attributes -band ([IO.FileAttributes]::Directory -bor [IO.FileAttributes]::Device)) -eq 0) 'An obsolete program file was replaced with a directory or special file.'
    if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Assert-CleanupCondition (Test-CleanupCloudPlaceholder $Path) 'An obsolete program file became a linked file.'
    }
    $file = [IO.FileInfo]::new($Path)
    return @{Length = $file.Length; Modified = $file.LastWriteTimeUtc.Ticks; Created = $file.CreationTimeUtc.Ticks}
}

function Remove-CleanupEmptyParents([string]$Root, [string]$Path) {
    $parent = [IO.Directory]::GetParent($Path)
    # Keep the installation root and shared _internal/licenses directories.
    while ($null -ne $parent -and $parent.FullName -ine $Root -and $parent.Parent.FullName -ine $Root) {
        $checked = Assert-CleanupPlainPath $parent.FullName
        Assert-CleanupCondition ($checked.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) 'Cleanup directory escaped the installation.'
        if (-not [IO.Directory]::Exists($checked) -or @([IO.Directory]::EnumerateFileSystemEntries($checked)).Count -gt 0) { break }
        [IO.Directory]::Delete($checked, $false)
        $checked.Substring($Root.TrimEnd('\').Length + 1).Replace('\', '/')
        $parent = $parent.Parent
    }
}

function Invoke-ProgramFileCleanup([string]$Root, $Plan, [string]$ExpectedVersion) {
    $plan = Test-CleanupPlan $Plan $ExpectedVersion
    $root = (Assert-CleanupPlainPath $Root).TrimEnd('\')
    Assert-CleanupCondition ([IO.Directory]::Exists($root) -and $root -ine [IO.Path]::GetPathRoot($root).TrimEnd('\')) 'Invalid installation directory.'
    # Preflight the whole plan before deleting its first file.
    foreach ($entry in $plan.obsolete_files) {
        $path = Get-CleanupCandidate $root $entry.path
        $null = Get-CleanupFileState $path
    }
    $removed = [Collections.Generic.List[string]]::new()
    $preserved = [Collections.Generic.List[string]]::new()
    $directories = [Collections.Generic.List[string]]::new()
    foreach ($entry in $plan.obsolete_files) {
        $path = Get-CleanupCandidate $root $entry.path
        $before = Get-CleanupFileState $path
        if ($null -eq $before) { continue }
        if ($before.Length -gt $script:CleanupFileBytes) {
            Assert-RetiredFileSafeToPreserve $entry.path
            $preserved.Add($entry.path); continue
        }
        $stream = $null
        $hasher = [Security.Cryptography.SHA256]::Create()
        try {
            # Deny changes/renames while hashing; close this handle before Delete.
            $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            Assert-CleanupCondition ($stream.Length -eq $before.Length) 'An obsolete file changed while opening it.'
            $buffer = [byte[]]::new(1024 * 1024)
            $total = 0L
            while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $total += $read
                Assert-CleanupCondition ($total -le $script:CleanupFileBytes) 'An obsolete file grew during verification.'
                $null = $hasher.TransformBlock($buffer, 0, $read, $buffer, 0)
            }
            $null = $hasher.TransformFinalBlock([byte[]]::new(0), 0, 0)
            $digest = [BitConverter]::ToString($hasher.Hash).Replace('-', '').ToLowerInvariant()
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
            $hasher.Dispose()
        }
        $path = Get-CleanupCandidate $root $entry.path
        $after = Get-CleanupFileState $path
        if ($null -eq $after) { continue }
        Assert-CleanupCondition ($before.Length -eq $after.Length -and $before.Modified -eq $after.Modified -and $before.Created -eq $after.Created) 'An obsolete file changed during verification.'
        if ($entry.hashes -cnotcontains $digest) {
            Assert-RetiredFileSafeToPreserve $entry.path
            $preserved.Add($entry.path); continue
        }
        try { [IO.File]::Delete($path) }
        catch { throw ('Could not remove retired file {0}. Close the translator and retry the installation. {1}' -f $entry.path, $_.Exception.Message) }
        $removed.Add($entry.path)
        foreach ($directory in @(Remove-CleanupEmptyParents $root $path)) { $directories.Add($directory) }
    }
    return [pscustomobject]@{ok = $true; version = $ExpectedVersion; removed = $removed.ToArray(); preserved = $preserved.ToArray(); removed_directories = $directories.ToArray(); error = $null}
}

function Assert-RetiredFileSafeToPreserve([string]$RelativePath) {
    # Unknown bytes remain the owner's property. A retired binary may still be
    # found by Windows/Python's loader, so keeping it cannot count as success.
    if ([IO.Path]::GetExtension($RelativePath) -in @('.dll', '.pyd', '.exe')) {
        throw ('Retired program file {0} differs from known releases and was left untouched. Move or remove that file manually, then run this installer again before opening the translator.' -f $RelativePath)
    }
}

function Assert-RegisteredInstallation([string]$Root, [string]$Version) {
    $root = (Assert-CleanupPlainPath $Root).TrimEnd('\')
    $registration = Get-ItemProperty -LiteralPath $script:CleanupRegistryKey
    Assert-CleanupCondition ($registration.DisplayName -is [string] -and ($registration.DisplayName -ceq $script:CleanupProduct -or $registration.DisplayName.StartsWith($script:CleanupProduct + ' ', [StringComparison]::Ordinal))) 'The registered installation belongs to another product.'
    Assert-CleanupCondition ($registration.InstallLocation -is [string] -and $registration.DisplayVersion -is [string]) 'The registered installation metadata is invalid.'
    $registered = (Assert-CleanupPlainPath ([string]$registration.InstallLocation)).TrimEnd('\')
    Assert-CleanupCondition ($registered -ieq $root) 'Cleanup root does not match the registered installation.'
    Assert-CleanupCondition ($registration.DisplayVersion -ceq $Version) 'Cleanup version does not match the registered installation.'
    foreach ($name in @('Lineage2ChatTranslator.exe', 'unins000.exe')) {
        $file = Assert-CleanupPlainPath ([IO.Path]::Combine($root, $name))
        Assert-CleanupCondition ([IO.File]::Exists($file)) 'The registered translator installation is incomplete.'
    }
    return $root
}

function Invoke-RegisteredCleanup([string]$Root, [string]$Version) {
    $created = $false
    $guard = [Threading.Mutex]::new($false, 'Local\L2ProminenceChatTranslator', [ref]$created)
    try {
        Assert-CleanupCondition $created 'The translator or setup is still open. Close it and retry the installation.'
        $uninstall = $null
        try { $uninstall = [Threading.Mutex]::OpenExisting('Local\Lineage2ChatTranslatorUninstall') }
        catch [Threading.WaitHandleCannotBeOpenedException] { }
        if ($null -ne $uninstall) { $uninstall.Dispose(); throw 'Uninstall is in progress. Wait for it to finish before cleanup.' }
        # The updater mutex is intentionally permitted: this helper is part of it.
        $root = Assert-RegisteredInstallation $Root $Version
        $plan = Read-CleanupPlan ([IO.Path]::Combine($root, '_internal\assets\obsolete-program-files.json')) $Version
        return Invoke-ProgramFileCleanup $root $plan $Version
    } finally { $guard.Dispose() }
}

function Write-CleanupResult([string]$Path, $Result) {
    $path = Assert-CleanupPlainPath $Path
    # Retain counts, but cap path examples and error text so install logs stay small.
    $report = [ordered]@{ok = [bool]$Result.ok; version = [string]$Result.version; removed_count = @($Result.removed).Count; preserved_count = @($Result.preserved).Count; removed = @($Result.removed | Select-Object -First 12); preserved = @($Result.preserved | Select-Object -First 12); error = $Result.error}
    if ($null -ne $report.error -and $report.error.Length -gt 2048) { $report.error = $report.error.Substring(0, 2048) }
    $json = ConvertTo-Json -InputObject $report -Depth 5 -Compress
    while ([Text.Encoding]::UTF8.GetByteCount($json) -gt 65536 -and ($report.removed.Count -gt 0 -or $report.preserved.Count -gt 0)) {
        if ($report.removed.Count -ge $report.preserved.Count) {
            $report.removed = @($report.removed | Select-Object -Skip 1)
        } else { $report.preserved = @($report.preserved | Select-Object -Skip 1) }
        $json = ConvertTo-Json -InputObject $report -Depth 5 -Compress
    }
    Assert-CleanupCondition ([Text.Encoding]::UTF8.GetByteCount($json) -le 65536) 'Cleanup result is too large.'
    [IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))
}

# Dot-sourcing exposes independently testable functions, never a command-line
# bypass for registration or the application/uninstall guards.
if ($MyInvocation.InvocationName -ne '.') {
    $exitCode = 0
    try {
        Assert-CleanupCondition ($InstallRoot -and $Version -and $ResultPath) 'InstallRoot, Version and ResultPath are required.'
        $result = Invoke-RegisteredCleanup $InstallRoot $Version
    } catch {
        $exitCode = 1
        $result = [pscustomobject]@{ok = $false; version = $Version; removed = @(); preserved = @(); error = $_.Exception.Message}
    }
    try { Write-CleanupResult $ResultPath $result }
    catch { [Console]::Error.WriteLine('Could not write cleanup result: ' + $_.Exception.Message); $exitCode = 1 }
    exit $exitCode
}
