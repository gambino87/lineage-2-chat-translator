param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
# Configuration, cancellation, authenticated service ownership and shutdown all
# belong to the regular launcher. No independent service/download path is kept.
& (Join-Path $PSScriptRoot 'Start Translator.ps1') -Configure -CheckOnly:$CheckOnly
