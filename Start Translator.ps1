param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'ollama_service.ps1')

$locationFile = Join-Path $PSScriptRoot 'runtime.local.json'
$savedLocation = $null
if (Test-Path -LiteralPath $locationFile) {
    try { $savedLocation = Get-Content -LiteralPath $locationFile -Raw | ConvertFrom-Json } catch { }
}
$windowsLocalData = [Environment]::GetFolderPath('LocalApplicationData')
$windowsProfile = [Environment]::GetFolderPath('UserProfile')
$candidateRoots = @()
if ($savedLocation.data_root) { $candidateRoots += [string]$savedLocation.data_root }
if ($windowsProfile) { $candidateRoots += Join-Path $windowsProfile 'L2ChatTranslator' }
if ($windowsLocalData) { $candidateRoots += Join-Path $windowsLocalData 'L2ChatTranslator' }
if ($env:LOCALAPPDATA) { $candidateRoots += Join-Path $env:LOCALAPPDATA 'L2ChatTranslator' }
$candidateRoots = @($candidateRoots | Select-Object -Unique)
$dataRoot = $candidateRoots | Where-Object {
    Test-Path -LiteralPath (Join-Path $_ 'ollama\ollama.exe') -PathType Leaf
} | Select-Object -First 1
if (-not $dataRoot) {
    $checked = ($candidateRoots | ForEach-Object { Join-Path $_ 'ollama\ollama.exe' }) -join "`n"
    throw "Cannot find the installed Ollama executable. Checked:`n$checked`nIf sharing this error for support, redact personal folder names first. Do not download the model again until its existing location has been checked."
}
$runtimeDir = Join-Path $dataRoot 'ollama'
$modelsDir = Join-Path $dataRoot 'models'
$logsDir = Join-Path $dataRoot 'logs'
$ollamaExe = Join-Path $runtimeDir 'ollama.exe'
$endpoint = 'http://127.0.0.1:11435'
$translationModel = 'qwen3.5:9b-q4_K_M'
$appPath = Join-Path $PSScriptRoot 'translator_app.py'

if (-not (Test-Path -LiteralPath $appPath)) { throw "Translator app is missing: $appPath" }
if (-not $CheckOnly) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }

$pythonw = [string]$savedLocation.pythonw
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = Join-Path $windowsLocalData 'Programs\Python\Python312\pythonw.exe'
}
if (-not (Test-Path -LiteralPath $pythonw)) {
    $command = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($command) { $pythonw = $command.Source } else { throw 'Python with Tkinter is required to run the translator.' }
}
$expectedPythonPaths = @($pythonw, ($pythonw -replace 'pythonw\.exe$', 'python.exe'))
$appArgument = '(^|\s)"?' + [regex]::Escape($appPath) + '"?(?=\s|$)'
$alreadyRunning = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.ExecutablePath -in $expectedPythonPaths -and $_.CommandLine -match $appArgument } |
    Select-Object -First 1
if ($alreadyRunning -and -not $CheckOnly) {
    $latestSource = Get-Item -LiteralPath $appPath, (Join-Path $PSScriptRoot 'translator_core.py'), (Join-Path $PSScriptRoot 'game_slang.py'), (Join-Path $PSScriptRoot 'game_log_setup.py'), (Join-Path $PSScriptRoot 'chat_overlay.py'), (Join-Path $PSScriptRoot 'reply_composer.py') |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($alreadyRunning.CreationDate.ToUniversalTime() -lt $latestSource.LastWriteTimeUtc) {
        throw 'An earlier translator version is still open or closing. Close its window with X and wait for shutdown to finish, then double-click Start Translator.cmd again to load the fixes. Clear and Pause do not update a running window.'
    }
    throw 'The translator is already open or closing. Use its existing window, or close it with X and wait for its Closing window to disappear before opening a fresh one.'
}

function Get-LocalService {
    try { return Invoke-RestMethod -Uri "$endpoint/api/version" -TimeoutSec 3 } catch { return $null }
}

$service = Get-LocalService
$migrationPending = $false
if ($service) {
    $existingListener = Get-LocalOllamaListener
    $existingProcess = Get-Process -Id $existingListener.OwningProcess -ErrorAction Stop
    if ($existingProcess.Path -ne $ollamaExe) {
        if ($service.version -ne '0.40.14' -or $existingListener.LocalAddress -ne '127.0.0.1' -or
            $existingProcess.Path -notin @($savedLocation.previous_executables)) {
            throw 'Port 11435 is occupied by a different service. Close it before starting the translator.'
        }
        if ($CheckOnly) {
            $migrationPending = $true
        } else {
            # This is the exact earlier dedicated runtime recorded during relocation.
            # A manual launch switches only this Ollama service to the accessible copy.
            Stop-Process -InputObject $existingProcess -ErrorAction Stop
            if (-not $existingProcess.WaitForExit(10000)) { throw 'The earlier translation service did not stop; retry shortly.' }
            $service = $null
        }
    }
}
if (-not $service) {
    if ($CheckOnly) { throw "Installed Ollama found at $ollamaExe, but the local service is not running. CheckOnly does not launch apps." }
    $environment = Get-PrivateOllamaEnvironment -ModelsDirectory $modelsDir
    $previousEnvironment = @{}
    try {
        foreach ($name in $environment.Keys) {
            $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process')
        }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $server = Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WorkingDirectory $runtimeDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logsDir "ollama-$stamp.stdout.log") -RedirectStandardError (Join-Path $logsDir "ollama-$stamp.stderr.log")
    } finally {
        foreach ($name in $previousEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
        }
    }
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Milliseconds 500
        $service = Get-LocalService
        if ($service) { break }
        if ($server.HasExited) { throw "The local translation service stopped. See $logsDir." }
    }
    if (-not $service) { throw "The local translation service did not become ready. See $logsDir." }
}
if ($service.version -ne '0.40.14') {
    throw "The service on port 11435 is version $($service.version); expected portable Ollama 0.40.14."
}
$listener = Get-LocalOllamaListener
$listenerProcess = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
if (($listenerProcess.Path -ne $ollamaExe -and -not ($CheckOnly -and $migrationPending)) -or $listener.LocalAddress -ne '127.0.0.1') {
    throw 'Port 11435 is occupied by a different service. Close it before starting the translator.'
}
$models = Invoke-RestMethod -Uri "$endpoint/api/tags" -TimeoutSec 10
if (-not ($models.models | Where-Object { $_.name -eq $translationModel })) {
    throw "The local $translationModel model is missing. Run Setup Ollama.ps1 to download it."
}

if ($CheckOnly) {
    Write-Output "Installed Ollama: $ollamaExe"
    Write-Output "Python: $pythonw"
    Write-Output "Local service and $translationModel model verified. No windows opened."
    if ($migrationPending) { Write-Output 'The next manual launch will switch the dedicated Ollama service to the accessible installation.' }
    return
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
# pythonw has no console. Only its error stream is retained for startup diagnostics.
# Windows argument parsing requires trailing backslashes to be doubled before a closing quote.
$quotedDataRoot = '"' + ($dataRoot -replace '(\\+)$', '$1$1') + '"'
Start-Process -FilePath $pythonw -ArgumentList ('"' + $appPath + '" --runtime-root ' + $quotedDataRoot) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardError (Join-Path $logsDir "translator-$stamp.stderr.log") | Out-Null
