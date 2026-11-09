$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'ollama_service.ps1')

$version = '0.40.14'
$archiveSha256 = '65d68cade6cf6375f398b518224f8c8a15e062387f38cf477abfc848de7e7e29'
$archiveUrl = "https://github.com/ollama/ollama/releases/download/v$version/ollama-windows-amd64.zip"
$windowsLocalData = [Environment]::GetFolderPath('LocalApplicationData')
if (-not $windowsLocalData) { $windowsLocalData = $env:LOCALAPPDATA }
$windowsProfile = [Environment]::GetFolderPath('UserProfile')
if (-not $windowsProfile) { $windowsProfile = $env:USERPROFILE }
$dataRoot = Join-Path $windowsProfile 'L2ChatTranslator'
$runtimeDir = Join-Path $dataRoot 'ollama'
$modelsDir = Join-Path $dataRoot 'models'
$logsDir = Join-Path $dataRoot 'logs'
$archivePath = Join-Path $dataRoot "ollama-windows-amd64-$version.zip"
$receiptPath = Join-Path $runtimeDir 'verified-install.json'
$ollamaExe = Join-Path $runtimeDir 'ollama.exe'
$endpoint = 'http://127.0.0.1:11435'
$translationModel = 'qwen3.5:9b-q4_K_M'

foreach ($directory in @($dataRoot, $runtimeDir, $modelsDir, $logsDir)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$installed = $false
if ((Test-Path -LiteralPath $receiptPath) -and (Test-Path -LiteralPath $ollamaExe)) {
    try {
        $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
        $installed = $receipt.version -eq $version -and $receipt.archiveSha256 -eq $archiveSha256 -and
            (Get-FileHash -LiteralPath $ollamaExe -Algorithm SHA256).Hash -eq $receipt.executableSha256
    } catch { $installed = $false }
}

if (-not $installed) {
    $archiveVerified = (Test-Path -LiteralPath $archivePath) -and
        (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -eq $archiveSha256
    if (-not $archiveVerified) {
        Write-Host "Downloading official Ollama $version (about 1.6 GB)..."
        & curl.exe --fail --location --retry 3 --connect-timeout 30 --silent --show-error --output $archivePath $archiveUrl
        if ($LASTEXITCODE -ne 0) { throw "Ollama download failed (curl exit $LASTEXITCODE). Run this setup again to retry." }
    }
    Write-Host 'Verifying the official archive checksum...'
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $archiveSha256) {
        throw "Archive checksum mismatch. Expected $archiveSha256; received $actualHash. No files were installed."
    }
    Write-Host 'Extracting the complete portable runtime and GPU libraries...'
    Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeDir -Force
    if (-not (Test-Path -LiteralPath $ollamaExe)) { throw 'The verified archive did not contain the expected ollama.exe.' }
    [ordered]@{
        version = $version
        source = $archiveUrl
        archiveSha256 = $archiveSha256
        executableSha256 = (Get-FileHash -LiteralPath $ollamaExe -Algorithm SHA256).Hash.ToLowerInvariant()
        installedAtUtc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $receiptPath -Encoding UTF8
    # Only this exact verified download is removed; runtime/model folders are retained.
    Remove-Item -LiteralPath $archivePath -Force
} else {
    Write-Host "Verified portable Ollama $version is already installed."
}

function Get-LocalService {
    try { return Invoke-RestMethod -Uri "$endpoint/api/version" -TimeoutSec 3 } catch { return $null }
}

function Install-TranslationModel {
    $request = [System.Net.HttpWebRequest]::Create("$endpoint/api/pull")
    $request.Method = 'POST'
    $request.ContentType = 'application/json'
    $request.Proxy = $null
    $request.Timeout = 600000
    $request.ReadWriteTimeout = 600000
    $payload = @{ model = $translationModel; stream = $true } | ConvertTo-Json -Compress
    $body = [Text.Encoding]::UTF8.GetBytes($payload)
    $request.ContentLength = $body.Length
    $stream = $request.GetRequestStream()
    try { $stream.Write($body, 0, $body.Length) } finally { $stream.Dispose() }
    $response = $request.GetResponse()
    $reader = New-Object IO.StreamReader($response.GetResponseStream())
    $lastStatus = ''
    $lastProgress = -1
    $lastDigest = ''
    $success = $false
    try {
        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if (-not $line) { continue }
            $update = $line | ConvertFrom-Json
            if ($update.error) { throw "Model download failed: $($update.error)" }
            if ($update.total -gt 0) {
                if ($update.digest -ne $lastDigest) { $lastDigest = $update.digest; $lastProgress = -1 }
                $percent = [Math]::Floor(100 * [double]$update.completed / [double]$update.total)
                $progressBucket = [Math]::Floor($percent / 10)
                if ($progressBucket -ne $lastProgress) {
                    Write-Host ("Model download: {0}% ({1:N0}/{2:N0} MB)" -f $percent, ($update.completed / 1MB), ($update.total / 1MB))
                    $lastProgress = $progressBucket
                }
            } elseif ($update.status -ne $lastStatus) {
                Write-Host $update.status
                $lastStatus = $update.status
            }
            if ($update.status -eq 'success') { $success = $true }
        }
        if (-not $success) { throw 'Model download ended before completion. Rerun setup to resume it.' }
    } finally {
        $reader.Dispose()
        $response.Dispose()
    }
}

$environment = Get-PrivateOllamaEnvironment -ModelsDirectory $modelsDir
$previousEnvironment = @{}
try {
    foreach ($name in $environment.Keys) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process')
    }
    $service = Get-LocalService
    if (-not $service) {
        Write-Host 'Starting the dedicated local translation service...'
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $server = Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WorkingDirectory $runtimeDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logsDir "ollama-$stamp.stdout.log") -RedirectStandardError (Join-Path $logsDir "ollama-$stamp.stderr.log")
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            Start-Sleep -Milliseconds 500
            $service = Get-LocalService
            if ($service) { break }
            if ($server.HasExited) { throw "Ollama stopped during startup. See $logsDir." }
        }
        if (-not $service) { throw "Ollama did not become ready. See $logsDir." }
    }
    if ($service.version -ne $version) {
        throw "Port 11435 is occupied by Ollama $($service.version), but this setup expects $version. Close that service and rerun setup."
    }
    $listener = Get-LocalOllamaListener
    $listenerProcess = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
    if ($listenerProcess.Path -ne $ollamaExe -or $listener.LocalAddress -ne '127.0.0.1') {
        throw 'Port 11435 is occupied by a different service. Close it before running setup.'
    }
    $models = Invoke-RestMethod -Uri "$endpoint/api/tags" -TimeoutSec 10
    if (-not ($models.models | Where-Object { $_.name -eq $translationModel })) {
        Write-Host "Downloading $translationModel into the private local model folder..."
        Install-TranslationModel
    } else {
        Write-Host "The local $translationModel model is already installed."
    }
    $models = Invoke-RestMethod -Uri "$endpoint/api/tags" -TimeoutSec 10
    if (-not ($models.models | Where-Object { $_.name -eq $translationModel })) { throw "The $translationModel model was not found after download." }
    [ordered]@{
        data_root = $dataRoot
        pythonw = Join-Path $windowsLocalData 'Programs\Python\Python312\pythonw.exe'
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime.local.json') -Encoding UTF8
    Write-Host ''
    Write-Host 'Setup complete. Double-click Start Translator.cmd to open the translator.'
    Write-Host "Local service: $endpoint"
    Write-Host "Runtime and models: $dataRoot"
} finally {
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
