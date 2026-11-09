param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Provision', 'Inspect', 'Sign')]
    [string]$Mode,
    [string]$InputFile,
    [string]$OutputFile,
    # Synthetic regression keys are separate from the one production identity.
    [ValidatePattern('^Lineage2ChatTranslator\.(UpdateSigning\.v1|TestSigning\.[a-f0-9]{32})$')]
    [string]$KeyName = 'Lineage2ChatTranslator.UpdateSigning.v1'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$provider = [System.Security.Cryptography.CngProvider]::MicrosoftSoftwareKeyStorageProvider
$key = $null
$rsa = $null
try {
    if ($Mode -eq 'Provision') {
        if ([System.Security.Cryptography.CngKey]::Exists($KeyName, $provider)) {
            throw 'The signing key already exists. It will not be replaced.'
        }
        $parameters = New-Object System.Security.Cryptography.CngKeyCreationParameters
        $parameters.Provider = $provider
        $parameters.ExportPolicy = [System.Security.Cryptography.CngExportPolicies]::None
        $parameters.KeyUsage = [System.Security.Cryptography.CngKeyUsages]::Signing
        $parameters.KeyCreationOptions = [System.Security.Cryptography.CngKeyCreationOptions]::None
        $length = New-Object System.Security.Cryptography.CngProperty('Length', ([BitConverter]::GetBytes([int]3072)), [System.Security.Cryptography.CngPropertyOptions]::None)
        $parameters.Parameters.Add($length)
        # No overwrite flag, machine key or UI policy: this is the current user's
        # dedicated non-exportable software signing key, never an uploaded file.
        $key = [System.Security.Cryptography.CngKey]::Create([System.Security.Cryptography.CngAlgorithm]::Rsa, $KeyName, $parameters)
    } else {
        if (-not [System.Security.Cryptography.CngKey]::Exists($KeyName, $provider)) {
            throw 'The publisher signing key is missing. Provision it explicitly on the publisher machine.'
        }
        $key = [System.Security.Cryptography.CngKey]::Open($KeyName, $provider)
    }
    $rsa = New-Object System.Security.Cryptography.RSACng($key)
    if ($rsa.KeySize -ne 3072 -or $key.IsMachineKey -or
        $key.ExportPolicy -ne [System.Security.Cryptography.CngExportPolicies]::None -or
        $key.KeyUsage -ne [System.Security.Cryptography.CngKeyUsages]::Signing) {
        throw 'The existing signing key does not satisfy the publisher key policy.'
    }
    $publicBlob = $key.Export([System.Security.Cryptography.CngKeyBlobFormat]::GenericPublicBlob)
    if ($Mode -eq 'Sign') {
        if (-not $InputFile -or -not $OutputFile) { throw 'Signing requires input and output files.' }
        $inputStream = [IO.File]::Open($InputFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        try {
            if ($inputStream.Length -lt 1 -or $inputStream.Length -gt 65536) { throw 'Release metadata must be between 1 and 65536 bytes.' }
            $data = New-Object byte[] ([int]$inputStream.Length)
            $offset = 0
            while ($offset -lt $data.Length) {
                $count = $inputStream.Read($data, $offset, $data.Length - $offset)
                if ($count -eq 0) { throw 'Release metadata changed while reading.' }
                $offset += $count
            }
            if ($inputStream.ReadByte() -ne -1) { throw 'Release metadata changed while reading.' }
        } finally { $inputStream.Dispose() }
        $signature = $rsa.SignData($data, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                                  [System.Security.Cryptography.RSASignaturePadding]::Pss)
        if ($signature.Length -ne 384) { throw 'Unexpected publisher signature size.' }
        if (-not $rsa.VerifyData($data, $signature, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                                [System.Security.Cryptography.RSASignaturePadding]::Pss)) {
            throw 'The new publisher signature failed verification.'
        }
        # Refuse replacement so a signed release cannot silently be overwritten.
        $outputStream = [IO.File]::Open($OutputFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $outputStream.Write($signature, 0, $signature.Length) }
        finally { $outputStream.Dispose() }
    }
    [ordered]@{
        key_name = $KeyName
        algorithm = 'RSA3072-PSS-SHA256'
        public_blob_b64 = [Convert]::ToBase64String($publicBlob)
        exportable = $false
    } | ConvertTo-Json -Compress
} finally {
    if ($null -ne $rsa) { $rsa.Dispose() }
    if ($null -ne $key) { $key.Dispose() }
}
