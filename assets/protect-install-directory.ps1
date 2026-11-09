param(
    [string]$InstallRoot,
    [string]$ResultPath,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
# Only application-owned paths are changed. Ancestors are checked and held open,
# never repaired. A failed preflight must not run any installed executable.
$script:InstallSecurityRegistry = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{1D8CE3C7-53A6-4C67-AC9F-1C6F0D6B1920}_is1'
$script:InstallSecurityProduct = 'Lineage 2 Chat Translator'
$script:InstallSecurityMaxEntries = 20000  # Bounds a malformed or unrelated target tree.
$script:InstallSecurityMaxDepth = 48
$script:InstallTrustedInstallerSid = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'

if (-not ('L2InstallSecurity.Native' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace L2InstallSecurity {
    public static class Native {
        [StructLayout(LayoutKind.Sequential)] public struct Info {
            public uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh,
                WriteLow, WriteHigh, VolumeSerial, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        [StructLayout(LayoutKind.Sequential)] private struct SecurityAttributes {
            public int Length; public IntPtr Descriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool Inherit;
        }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out Info info);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder path, uint size, uint flags);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern bool CreateDirectoryW(string path, ref SecurityAttributes security);
        [DllImport("advapi32.dll")] private static extern uint GetSecurityInfo(SafeFileHandle handle,
            uint type, uint flags, out IntPtr owner, out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
        [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern bool ConvertSecurityDescriptorToStringSecurityDescriptorW(IntPtr descriptor,
            uint revision, uint flags, out IntPtr text, out uint length);
        [DllImport("advapi32.dll")] private static extern uint SetSecurityInfo(SafeFileHandle handle,
            uint type, uint flags, IntPtr owner, IntPtr group, byte[] dacl, IntPtr sacl);
        [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);

        private static SafeFileHandle OpenCore(string path, bool writeDacl, uint share) {
            // FILE_READ_DATA/FILE_LIST_DIRECTORY + READ_CONTROL (+ WRITE_DAC
            // for repair), OPEN_REPARSE_POINT and
            // BACKUP_SEMANTICS. No share-delete, so checked components cannot
            // be replaced while their ACL is being inspected or changed.
            // A metadata-only handle does not enforce file share-write checks.
            // MAXIMUM_ALLOWED suppresses SetSecurityInfo's automatic child-ACL
            // propagation. Only the entries preflighted through their own held
            // handles may be changed. Explicit required rights retain fail-fast
            // access checks and FILE_READ_DATA retains writer exclusion.
            // https://learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo
            var h = CreateFileW(path, 0x20001U | (writeDacl ? 0x02040000U : 0U),
                share, IntPtr.Zero, 3U, 0x02200000U, IntPtr.Zero);
            if (h.IsInvalid) { h.Dispose(); throw new Win32Exception(Marshal.GetLastWin32Error(), "An installation path could not be opened safely."); }
            return h;
        }
        public static SafeFileHandle Open(string path, bool writeDacl, bool directory) {
            return OpenCore(path, writeDacl, 1U);
        }
        public static SafeFileHandle OpenAncestor(string path) {
            // Checking C:\ or the user's profile must not deny unrelated writers.
            // Ancestor ACLs are inspected, not changed; retain no-share-delete.
            return OpenCore(path, false, 3U);
        }
        public static Info Inspect(SafeFileHandle handle, string expected) {
            Info info;
            if (!GetFileInformationByHandle(handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
            var path = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandleW(handle, path, (uint)path.Capacity, 0);
            if (length == 0 || length >= path.Capacity) throw new InvalidOperationException("Cannot resolve an installation handle.");
            string final = path.ToString();
            if (final.StartsWith(@"\\?\")) final = final.Substring(4);
            if (!String.Equals(final.TrimEnd('\\'), expected.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("An installation path resolves to another location.");
            if ((info.Attributes & 0x400U) != 0) throw new InvalidOperationException("Linked installation paths are not supported.");
            if ((info.Attributes & 0x10U) == 0 && info.Links != 1)
                throw new InvalidOperationException("Hard-linked program files are not supported.");
            return info;
        }
        public static RawSecurityDescriptor Security(SafeFileHandle handle) {
            IntPtr owner, group, dacl, sacl, descriptor;
            uint error = GetSecurityInfo(handle, 1U, 5U, out owner, out group, out dacl, out sacl, out descriptor);
            if (error != 0) throw new Win32Exception((int)error, "Cannot inspect installation permissions.");
            IntPtr text = IntPtr.Zero;
            try {
                uint length;
                if (!ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1U, 5U, out text, out length))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                return new RawSecurityDescriptor(Marshal.PtrToStringUni(text));
            } finally { if (text != IntPtr.Zero) LocalFree(text); LocalFree(descriptor); }
        }
        private static RawSecurityDescriptor PrivateDescriptor(string[] trustees, bool directory) {
            var text = new StringBuilder("D:P");
            foreach (string sid in trustees) text.Append("(A;").Append(directory ? "OICI" : "").Append(";FA;;;").Append(sid).Append(")");
            return new RawSecurityDescriptor(text.ToString());
        }
        public static void Protect(SafeFileHandle handle, string[] trustees, bool directory) {
            var acl = PrivateDescriptor(trustees, directory).DiscretionaryAcl;
            var bytes = new byte[acl.BinaryLength]; acl.GetBinaryForm(bytes, 0);
            // DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
            uint error = SetSecurityInfo(handle, 1U, 0x80000004U, IntPtr.Zero, IntPtr.Zero, bytes, IntPtr.Zero);
            if (error != 0) throw new Win32Exception((int)error, "Cannot protect installation permissions.");
        }
        public static void CreatePrivateDirectory(string path, string[] trustees) {
            var descriptor = PrivateDescriptor(trustees, true);
            var bytes = new byte[descriptor.BinaryLength]; descriptor.GetBinaryForm(bytes, 0);
            IntPtr buffer = Marshal.AllocHGlobal(bytes.Length);
            try {
                Marshal.Copy(bytes, 0, buffer, bytes.Length);
                var security = new SecurityAttributes { Length = Marshal.SizeOf(typeof(SecurityAttributes)), Descriptor = buffer, Inherit = false };
                if (!CreateDirectoryW(path, ref security))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "The private installation folder could not be created safely.");
            } finally { Marshal.FreeHGlobal(buffer); }
        }
    }
}
'@
}

function Assert-InstallSecurity([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Get-InstallCanonicalPath([string]$Path) {
    Assert-InstallSecurity ($Path -match '^[A-Za-z]:\\' -and $Path.Substring(2) -notmatch ':' -and $Path -notmatch '[\x00-\x1f]') 'Choose an absolute folder on a local disk.'
    $full = [IO.Path]::GetFullPath($Path)
    Assert-InstallSecurity ($full.TrimEnd('\') -ieq $Path.TrimEnd('\')) 'Installation paths cannot contain traversal components.'
    return $full.TrimEnd('\')
}

function Get-InstallAncestorPaths([string]$Root) {
    $paths = [Collections.Generic.List[string]]::new()
    $parent = [IO.Directory]::GetParent($Root)
    while ($null -ne $parent) { $paths.Insert(0, $parent.FullName); $parent = $parent.Parent }
    return $paths.ToArray()
}

function Assert-InstallOwner($Security, [string[]]$Trusted) {
    Assert-InstallSecurity ($null -ne $Security.Owner -and $Trusted -contains $Security.Owner.Value) 'An installation path is owned by another account. Use a private folder or ask an administrator to repair it.'
    Assert-InstallSecurity ($null -ne $Security.DiscretionaryAcl) 'An installation path has unrestricted permissions.'
}

function Assert-InstallAncestorSecurity($Security, [string[]]$Trusted) {
    Assert-InstallOwner $Security $Trusted
    # DELETE, FILE_DELETE_CHILD, WRITE_DAC/OWNER and generic write/all can
    # replace a checked descendant or undermine its security descriptor.
    $dangerous = 0x500D0040L
    foreach ($ace in $Security.DiscretionaryAcl) {
        if (([int]$ace.AceFlags -band [int][Security.AccessControl.AceFlags]::InheritOnly) -ne 0) { continue }
        Assert-InstallSecurity ($ace -is [Security.AccessControl.CommonAce] -and -not $ace.IsCallback) 'An installation parent uses unsupported permissions.'
        if ($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessDenied) { continue }
        Assert-InstallSecurity ($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed) 'An installation parent uses unsupported permissions.'
        $mask = ([int64]$ace.AccessMask) -band 0xffffffffL
        if ($Trusted -notcontains $ace.SecurityIdentifier.Value -and $ace.SecurityIdentifier.Value -ne 'S-1-3-4') {
            Assert-InstallSecurity (($mask -band $dangerous) -eq 0) 'Another account can replace an installation parent. Choose a private folder; parent permissions were not changed.'
        }
    }
}

function Assert-InstallPrivateSecurity($Security, [string[]]$Trusted, [bool]$Directory, [bool]$RequireProtected = $true) {
    Assert-InstallOwner $Security $Trusted
    if ($RequireProtected) {
        Assert-InstallSecurity (([int]$Security.ControlFlags -band [int][Security.AccessControl.ControlFlags]::DiscretionaryAclProtected) -ne 0) 'Installation permissions are not protected from inheritance.'
    }
    Assert-InstallSecurity ($Security.DiscretionaryAcl.Count -eq $Trusted.Count) 'Installation permissions include unexpected access rules.'
    $seen = @{}
    $flags = if ($Directory) { 3 } else { 0 }
    foreach ($ace in $Security.DiscretionaryAcl) {
        Assert-InstallSecurity ($ace -is [Security.AccessControl.CommonAce] -and -not $ace.IsCallback -and $ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed) 'Unexpected installation access rule.'
        $sid = $ace.SecurityIdentifier.Value
        # Inno-created children inherit these same three grants. Only the app
        # root must stop inheritance; every parent/child is verified in order.
        $actualFlags = [int]$ace.AceFlags -band (-bnot 16)
        Assert-InstallSecurity ($Trusted -contains $sid -and -not $seen.ContainsKey($sid) -and $ace.AccessMask -eq 0x1f01ff -and $actualFlags -eq $flags) 'Installation permissions are not private to this user and Windows administrators.'
        $seen[$sid] = $true
    }
}

function Invoke-InstallDirectoryProtection {
    param([string]$Root, [string]$RegisteredRoot = '', [switch]$VerifyOnly)
    $root = Get-InstallCanonicalPath $Root
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $trusted = @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544' | Select-Object -Unique)
    $identity.Dispose()
    $ancestorTrusted = @($trusted) + $script:InstallTrustedInstallerSid
    Assert-InstallSecurity ($root -ine [IO.Path]::GetPathRoot($root).TrimEnd('\')) 'A drive root cannot be an installation folder.'
    $reserved = @([Environment]::GetFolderPath('UserProfile'), [Environment]::GetFolderPath('LocalApplicationData'),
        [Environment]::GetFolderPath('ApplicationData'), [Environment]::GetFolderPath('MyDocuments'),
        [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Windows'),
        [Environment]::GetFolderPath('System'), [Environment]::GetFolderPath('ProgramFiles'),
        [Environment]::GetFolderPath('ProgramFilesX86'), [IO.Path]::Combine([Environment]::GetFolderPath('LocalApplicationData'), 'Programs'))
    Assert-InstallSecurity ($reserved -notcontains $root) 'Choose a separate folder for the translator.'
    $registered = -not [string]::IsNullOrWhiteSpace($RegisteredRoot)
    if ($registered) {
        Assert-InstallSecurity ((Get-InstallCanonicalPath $RegisteredRoot) -ieq $root) 'An existing installation must be repaired in its registered folder before changing location.'
        Assert-InstallSecurity ([IO.Directory]::Exists($root)) 'The registered installation folder is missing.'
    }
    Assert-InstallSecurity ([IO.Directory]::Exists([IO.Path]::GetDirectoryName($root))) 'The installation parent does not exist.'
    $handles = [Collections.Generic.List[object]]::new()
    $entries = [Collections.Generic.List[object]]::new()
    $created = $false
    try {
        foreach ($parent in @(Get-InstallAncestorPaths $root)) {
            $handle = [L2InstallSecurity.Native]::OpenAncestor($parent)
            $handles.Add($handle)
            $null = [L2InstallSecurity.Native]::Inspect($handle, $parent)
            Assert-InstallAncestorSecurity ([L2InstallSecurity.Native]::Security($handle)) $ancestorTrusted
        }
        if (-not [IO.Directory]::Exists($root)) {
            Assert-InstallSecurity (-not $VerifyOnly) 'The installation folder is missing.'
            [L2InstallSecurity.Native]::CreatePrivateDirectory($root, $trusted)
            $created = $true
        }
        $pending = [Collections.Generic.Queue[object]]::new()
        $pending.Enqueue(@($root, 0))
        while ($pending.Count -gt 0) {
            $item = $pending.Dequeue(); $path = [string]$item[0]; $depth = [int]$item[1]
            Assert-InstallSecurity ($depth -le $script:InstallSecurityMaxDepth -and $entries.Count -lt $script:InstallSecurityMaxEntries) 'The selected installation contains too many entries.'
            $directory = [IO.Directory]::Exists($path)
            $handle = [L2InstallSecurity.Native]::Open($path, (-not $VerifyOnly), $directory)
            $handles.Add($handle)
            $info = [L2InstallSecurity.Native]::Inspect($handle, $path)
            Assert-InstallSecurity ((($info.Attributes -band 0x10) -ne 0) -eq $directory) 'An installation entry changed during inspection.'
            Assert-InstallOwner ([L2InstallSecurity.Native]::Security($handle)) $trusted
            $entries.Add([pscustomobject]@{Path=$path; Handle=$handle; Directory=$directory})
            if ($directory) {
                $children = @([IO.Directory]::EnumerateFileSystemEntries($path))
                if ($path -ieq $root) {
                    Assert-InstallSecurity ($registered -or $children.Count -eq 0) 'A nonempty unregistered folder will not be changed. Choose an empty private folder.'
                    if ($registered) {
                        foreach ($name in @('Lineage2ChatTranslator.exe', 'unins000.exe')) {
                            Assert-InstallSecurity ([IO.File]::Exists([IO.Path]::Combine($root, $name))) 'The registered translator installation is incomplete.'
                        }
                        foreach ($child in $children) {
                            $name = [IO.Path]::GetFileName($child)
                            Assert-InstallSecurity ($name -in @('_internal', 'licenses', 'Lineage2ChatTranslator.exe', 'USER-README.txt', 'THIRD-PARTY-NOTICES.txt') -or $name -match '^unins[0-9]+\.(exe|dat|msg)$') 'The registered folder contains unrelated files; its permissions were not changed.'
                        }
                    }
                }
                foreach ($child in $children) { $pending.Enqueue(@($child, $depth + 1)) }
            }
        }
        # Every existing node is preflighted before changing any DACL. Existing
        # unsafe permissions are not restored if a later repair step fails.
        if (-not $VerifyOnly) {
            foreach ($entry in $entries) {
                [L2InstallSecurity.Native]::Protect($entry.Handle, $trusted, $entry.Directory)
            }
        }
        foreach ($entry in $entries) {
            $null = [L2InstallSecurity.Native]::Inspect($entry.Handle, $entry.Path)
            Assert-InstallPrivateSecurity ([L2InstallSecurity.Native]::Security($entry.Handle)) $trusted $entry.Directory ($entry.Path -ieq $root)
        }
        # A second directory inventory catches entries added during preflight.
        $expected = @{}; foreach ($entry in $entries) { $expected[$entry.Path] = $true }
        foreach ($entry in $entries) {
            if ($entry.Directory) {
                foreach ($child in [IO.Directory]::EnumerateFileSystemEntries($entry.Path)) {
                    Assert-InstallSecurity ($expected.ContainsKey($child)) 'The installation changed during permission checks. Retry setup.'
                }
            }
        }
        return [pscustomobject]@{ok=$true; message='Installation permissions verified.'; entries=$entries.Count; created=$created; verified_only=[bool]$VerifyOnly}
    } finally {
        for ($i=$handles.Count-1; $i -ge 0; $i--) { $handles[$i].Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $code = 1
    try {
        $registered = ''
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64)
        try {
            $key = $base.OpenSubKey($script:InstallSecurityRegistry)
            if ($null -ne $key) {
                try {
                    $name = [string]$key.GetValue('DisplayName', '')
                    Assert-InstallSecurity ($name -eq $script:InstallSecurityProduct -or $name.StartsWith($script:InstallSecurityProduct + ' ')) 'The installation registration belongs to another product.'
                    $registered = [string]$key.GetValue('InstallLocation', '')
                    Assert-InstallSecurity (-not [string]::IsNullOrWhiteSpace($registered)) 'The registered installation folder is missing.'
                } finally { $key.Dispose() }
            }
        } finally { $base.Dispose() }
        $result = Invoke-InstallDirectoryProtection -Root $InstallRoot -RegisteredRoot $registered -VerifyOnly:$VerifyOnly
        $code = 0
    } catch {
        $result = [pscustomobject]@{ok=$false; message='Installation permissions could not be made safe. ' + $_.Exception.Message; entries=0; created=$false; verified_only=[bool]$VerifyOnly}
    }
    if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
        [IO.File]::WriteAllText($ResultPath, (ConvertTo-Json -InputObject $result -Compress), [Text.UTF8Encoding]::new($false))
    }
    exit $code
}
