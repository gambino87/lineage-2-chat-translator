param([switch]$CheckOnly, [switch]$Configure)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Source execution shares the installed launcher's protected storage,
# authenticated service, single-instance lock and shutdown lifecycle.
# Never start an independent service or trust runtime.local.json executable paths.
$entrypoint = Join-Path $PSScriptRoot 'installer_main.py'
if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw 'The translator launcher source is missing. Restore installer_main.py before starting.'
}

$windowsLocalData = [Environment]::GetFolderPath('LocalApplicationData')
$executableNames = if ($CheckOnly) { @('python.exe') } else { @('pythonw.exe', 'python.exe') }
$candidates = @()
foreach ($name in $executableNames) {
    $candidates += Join-Path $PSScriptRoot ".build-tools\venv\Scripts\$name"
    if ($windowsLocalData) {
        $candidates += Join-Path $windowsLocalData "Programs\Python\Python312\$name"
    }
}
$python = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $python) {
    foreach ($name in $executableNames) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { $python = $command.Source; break }
    }
}
if (-not $python) {
    throw 'Python 3.12 with Tkinter is required to run the source launcher. Install Python or use the published translator installer; this script does not download Python.'
}

if ($CheckOnly) {
    # Diagnostics read metadata/hardware only. They never open a window,
    # contact the model API, install files or start a service.
    & $python $entrypoint --diagnose
    if ($LASTEXITCODE -ne 0) { throw 'The translator diagnostic check failed. Review the error above.' }
    return
}

$arguments = @('"' + $entrypoint + '"')
if ($Configure) { $arguments += '--configure' }
Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
