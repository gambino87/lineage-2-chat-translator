$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolsRoot = Join-Path $projectRoot '.build-tools'
$venvPython = Join-Path $toolsRoot 'venv\Scripts\python.exe'
New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $venvPython)) {
    & python -m venv (Join-Path $toolsRoot 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python build environment creation failed.' }
}
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }

# Portable package: extract files, never execute the compiler's installer.
$archive = Join-Path $toolsRoot 'tools.innosetup.6.7.3.zip'
$archiveHash = 'f780898e402ff80612cc8d9fcb8c6e02932bd1cb4c900ffdaa31f9341cfb49f4'
if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveHash) {
    Invoke-WebRequest -Uri 'https://api.nuget.org/v3-flatcontainer/tools.innosetup/6.7.3/tools.innosetup.6.7.3.nupkg' -OutFile $archive
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveHash) { throw 'Portable compiler package checksum mismatch.' }
$portable = Join-Path $toolsRoot 'inno-portable'
Expand-Archive -LiteralPath $archive -DestinationPath $portable -Force
$compilerTools = Join-Path $portable 'tools'
# Verify all executable binaries before running the signature checker/compiler.
$binaries = Get-ChildItem -LiteralPath $compilerTools -File | Where-Object Extension -in '.exe', '.dll'
foreach ($binary in $binaries) {
    $signature = Get-AuthenticodeSignature -LiteralPath $binary.FullName
    if ($signature.Status -ne 'Valid' -or ($signature.SignerCertificate.Subject -notlike '*Pyrsys B.V.*' -and $signature.SignerCertificate.Subject -notlike '*Martijn Laan*')) {
        throw "Invalid upstream compiler signature: $($binary.Name)"
    }
}
foreach ($keyName in @('def01.ispublickey', 'def02.ispublickey')) {
    $keyPath = Join-Path $portable $keyName
    Invoke-WebRequest -Uri "https://files.jrsoftware.org/is/misc/$keyName" -OutFile $keyPath
    $keyHash = if ($keyName -eq 'def01.ispublickey') { '3b0b7e1e478c9ab327746a990512e2b245c9231e88a7fdd60d7874dd439db562' } else { '32bea6bceb4ac7c4e6b3becdf3fb38de77378c5e76d494ab907d87cfab9e597b' }
    if ((Get-FileHash -LiteralPath $keyPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $keyHash) { throw "Upstream public key changed: $keyName. Review the new key before continuing." }
}
# Verify every Inno engine/library signature against upstream public keys.
$sigtool = Join-Path $compilerTools 'ISSigTool.exe'
foreach ($signatureFile in (Get-ChildItem -LiteralPath $compilerTools -Filter '*.issig' -File)) {
    $payload = $signatureFile.FullName.Substring(0, $signatureFile.FullName.Length - 6)
    $signatureText = Get-Content -LiteralPath $signatureFile.FullName -Raw
    $keyName = if ($signatureText -match 'key-id def01') { 'def01.ispublickey' } else { 'def02.ispublickey' }
    & $sigtool ("--key-file=" + (Join-Path $portable $keyName)) verify $payload
    if ($LASTEXITCODE -ne 0) { throw "Upstream ECDSA signature failed: $payload" }
}

$licenseDir = Join-Path $PSScriptRoot 'licenses'
New-Item -ItemType Directory -Path $licenseDir -Force | Out-Null
$basePython = (& $venvPython -c 'import sys; print(sys.base_prefix)').Trim()
$sitePackages = Join-Path $toolsRoot 'venv\Lib\site-packages'
Copy-Item -LiteralPath (Join-Path $basePython 'LICENSE.txt') -Destination (Join-Path $licenseDir 'PYTHON.txt') -Force
Copy-Item -LiteralPath (Join-Path $sitePackages 'pyinstaller-6.22.3.dist-info\licenses\COPYING.txt') -Destination (Join-Path $licenseDir 'PYINSTALLER.txt') -Force
Copy-Item -LiteralPath (Join-Path $basePython 'tcl\tk8.6\license.terms') -Destination (Join-Path $licenseDir 'TK.txt') -Force
Copy-Item -LiteralPath (Join-Path $compilerTools 'license.txt') -Destination (Join-Path $licenseDir 'INNO-SETUP.txt') -Force
Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/tcltk/tcl/core-8-6-15/license.terms' -OutFile (Join-Path $licenseDir 'TCL.txt')
Write-Output "Verified portable compiler: $(Join-Path $compilerTools 'ISCC.exe')"
Write-Output "Build environment: $venvPython"
