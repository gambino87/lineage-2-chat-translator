"""Real Windows helper transactions using inert, isolated program-file fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import app_updater


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell/CNG/file transaction integration')
class IncrementalRunnerTests(unittest.TestCase):
    def run_harness(self, body):
        declarations = app_updater.RUNNER_PATH.read_text(encoding='utf-8').split(
            '\ntry {\n    $checkedJob', 1)[0].replace(
                'param([Parameter(Mandatory = $true)][string]$JobPath)', '')
        with tempfile.TemporaryDirectory(prefix='l2-incremental-runner-') as folder:
            script = Path(folder) / 'fixture.ps1'
            script.write_text('param([string]$TestDirectory)\n' + declarations + '\n' + body,
                              encoding='utf-8-sig')
            result = subprocess.run([
                str(app_updater._powershell()), '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(script), '-TestDirectory', folder,
            ], capture_output=True, text=True, timeout=50, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_real_signed_manifest_validation_rejects_unsafe_or_changed_records(self):
        result = self.run_harness(r'''
$stage=$TestDirectory
$pathLocks=New-UpdatePathLocks
$testKey=[Security.Cryptography.RSACng]::new(3072)
$releasePublicKeyBlob=[Convert]::ToBase64String($testKey.Key.Export([Security.Cryptography.CngKeyBlobFormat]::GenericPublicBlob))
$job=[pscustomobject]@{version='99.8.7';from_version='1.8.7';installer_file='Lineage-2-Chat-Translator-99.8.7-Setup.exe';installer_bytes=2048;installer_sha256=('a'*64)}
function New-Manifest {
    return @{
        schema_version=1;product=$productName;platform='windows-x64';version=$job.version
        files=@(@{path='Lineage2ChatTranslator.exe';bytes=10;sha256=('b'*64);blob=@{bytes=20;sha256=('c'*64)}})
        cleanup=@{schema_version=1;product=$productName;version=$job.version;current_paths=@('Lineage2ChatTranslator.exe');obsolete_files=@()}
    }
}
function Sign-Manifest($Manifest) {
    [IO.File]::WriteAllText((Join-Path $stage 'files.json'),($Manifest|ConvertTo-Json -Compress -Depth 10),[Text.UTF8Encoding]::new($false))
    $identity=Get-FileIdentity (Join-Path $stage 'files.json')
    $release=@{product=$productName;platform='windows-x64';version=$job.version;validation=@{packaged_self_test_passed=$true};installer=@{file=$job.installer_file;bytes=$job.installer_bytes;sha256=$job.installer_sha256};incremental=@{schema_version=1;manifest=@{file=('updates/'+$job.version+'/files.json');bytes=$identity.bytes;sha256=$identity.sha256}}}
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($release|ConvertTo-Json -Compress -Depth 10))
    [IO.File]::WriteAllBytes((Join-Path $stage 'release.json'),$bytes)
    [IO.File]::WriteAllBytes((Join-Path $stage 'release.sig'),$testKey.SignData($bytes,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pss))
    Assert-SignedRelease
}
try {
    $cases=[Collections.Generic.List[string]]::new()
    Sign-Manifest (New-Manifest)
    Assert-FileManifest
    $cases.Add('valid independently signed target inventory')
    foreach($bad in @('../outside','_internal/../outside','_internal/C:evil','_internal/CON.dll','_internal/a.','_internal/a ','_internal/settings.json','models/a','unins000.exe')) {
        $manifest=New-Manifest
        $manifest.files[0].path=$bad
        $manifest.cleanup.current_paths=@($bad)
        Sign-Manifest $manifest
        $rejected=$false
        try {Assert-FileManifest} catch {$rejected=$true}
        if(-not $rejected){throw ('Accepted unsafe path '+$bad)}
        $cases.Add('reject '+$bad)
    }
    foreach($case in @('duplicate','overlap','cleanup mismatch','large file','large blob','wrong product','retired executable','retired current path')) {
        $manifest=New-Manifest
        switch($case) {
            'duplicate' {$manifest.files+=@{path='Lineage2ChatTranslator.exe';bytes=1;sha256=('d'*64);blob=@{bytes=2;sha256=('e'*64)}};$manifest.cleanup.current_paths+=@('Lineage2ChatTranslator.exe')}
            'overlap' {$manifest.files+=@{path='_internal/a';bytes=1;sha256=('d'*64);blob=@{bytes=2;sha256=('e'*64)}};$manifest.files+=@{path='_internal/a/b';bytes=1;sha256=('f'*64);blob=@{bytes=2;sha256=('e'*64)}};$manifest.cleanup.current_paths+=@('_internal/a','_internal/a/b')}
            'cleanup mismatch' {$manifest.cleanup.current_paths=@('USER-README.txt')}
            'large file' {$manifest.files[0].bytes=134217729}
            'large blob' {$manifest.files[0].blob.bytes=134217729}
            'wrong product' {$manifest.product='other'}
            'retired executable' {$manifest.cleanup.obsolete_files=@(@{path='_internal/old.exe';hashes=@(('e'*64))})}
            'retired current path' {$manifest.cleanup.obsolete_files=@(@{path='Lineage2ChatTranslator.exe';hashes=@(('e'*64))})}
        }
        Sign-Manifest $manifest
        $rejected=$false
        try {Assert-FileManifest} catch {$rejected=$true}
        if(-not $rejected){throw ('Accepted '+$case)}
        $cases.Add($case)
    }
    Sign-Manifest (New-Manifest)
    [IO.File]::AppendAllText((Join-Path $stage 'files.json'),' ')
    $rejected=$false
    try {Assert-FileManifest} catch {$rejected=$true}
    if(-not $rejected){throw 'Accepted unsigned manifest bytes'}
    $cases.Add('unsigned manifest substitution')
    @{cases=$cases.ToArray();application_executed=$false}|ConvertTo-Json -Compress -Depth 3
} finally {$testKey.Dispose();$pathLocks.Dispose()}
''')
        self.assertEqual(len(result['cases']), 19)
        self.assertFalse(result['application_executed'])

    def test_fallback_runs_only_after_safe_rollback_and_preserves_failed_recovery(self):
        source = app_updater.RUNNER_PATH.read_text(encoding='utf-8')
        branch = source.split('    $incrementalFinished = $false\n', 1)[1].split(
            '    if (-not $incrementalFinished) {', 1)[0]
        harness = r'''
$executable='C:\inert-fixture\Lineage2ChatTranslator.exe'
$incrementalMode=$true
$cases=[Collections.Generic.List[string]]::new()
function Invoke-IncrementalUpdate {
    if($scenario -eq 'success'){return $executable}
    if($scenario -eq 'rollback failed'){$script:preserveStage=$true;$script:restartPermitted=$false}
    else{$script:restored=$true}
    throw 'inert fixture update failure'
}
function Download-FullInstaller {
    if(-not $script:restored -or $preserveStage -or -not $restartPermitted){throw 'Fallback attempted before a safe rollback'}
    $script:downloads++
}
foreach($scenario in @('success','safe rollback','rollback failed')){
    $preserveStage=$false;$restartPermitted=$true;$script:restored=$false;$script:downloads=0
    $incrementalFinished=$false;$rejected=$false
    try {
__BRANCH__
    }catch{$rejected=$true}
    if($scenario -eq 'success' -and (-not $incrementalFinished -or $script:downloads -ne 0 -or $rejected)){throw 'Successful update downloaded a full installer'}
    if($scenario -eq 'safe rollback' -and ($incrementalFinished -or $script:downloads -ne 1 -or $rejected)){throw 'Safe rollback did not use one fallback'}
    if($scenario -eq 'rollback failed' -and (-not $rejected -or $script:downloads -ne 0 -or -not $preserveStage)){throw 'Failed rollback allowed a fallback or lost recovery data'}
    $cases.Add($scenario)
}
$pathLocks=New-UpdatePathLocks
try{
    $stage=Join-Path $TestDirectory 'unfinished-stage'
    $null=[IO.Directory]::CreateDirectory($stage)
    $safeStage=$true;$preserveStage=$false
    Write-Record (Join-Path $stage 'transaction.json') @{state='applying'}
    Write-Record (Join-Path $stage 'owner.json') @{app_id=$appIdentity;nonce=('a'*32)}
    [IO.File]::WriteAllText((Join-Path $stage 'rollback-00000.bak'),'original-fixture')
    $job=[pscustomobject]@{nonce=('a'*32)}
    $rejected=$false
    try{Remove-OwnedStage}catch{$rejected=$true}
    if(-not $rejected -or -not [IO.File]::Exists((Join-Path $stage 'rollback-00000.bak'))){throw 'Unfinished recovery backup was deleted'}
    $cases.Add('unfinished journal blocks cleanup')
    Write-Record (Join-Path $stage 'transaction.json') @{state='committed'}
    [IO.File]::WriteAllText((Join-Path $stage 'transaction.pending'),'{')
    $rejected=$false
    try{Remove-OwnedStage}catch{$rejected=$true}
    if(-not $rejected -or -not [IO.File]::Exists((Join-Path $stage 'rollback-00000.bak'))){throw 'Pending recovery journal was deleted'}
    $cases.Add('pending journal blocks cleanup')
    $original=Join-Path $TestDirectory 'hardlink-original'
    [IO.File]::WriteAllText($original,'hardlink-fixture')
    $alias=Join-Path $TestDirectory 'hardlink-alias'
    $null=New-Item -ItemType HardLink -Path $alias -Target $original
    $rejected=$false
    try{Get-FileIdentity $alias|Out-Null}catch{$rejected=$true}
    if(-not $rejected){throw 'A hardlinked file was accepted'}
    $cases.Add('hardlink rejected')
}finally{$pathLocks.Dispose()}
@{cases=$cases.ToArray();installer_executed=$false}|ConvertTo-Json -Compress -Depth 3
'''.replace('__BRANCH__', branch)
        result = self.run_harness(harness)
        self.assertEqual(result['cases'], ['success', 'safe rollback', 'rollback failed',
                                          'unfinished journal blocks cleanup', 'pending journal blocks cleanup', 'hardlink rejected'])
        self.assertFalse(result['installer_executed'])

    def test_full_fallback_transfer_verifies_signature_size_hash_and_executable_header(self):
        result = self.run_harness(r'''
$pathLocks=New-UpdatePathLocks
$testKey=[Security.Cryptography.RSACng]::new(3072)
$releasePublicKeyBlob=[Convert]::ToBase64String($testKey.Key.Export([Security.Cryptography.CngKeyBlobFormat]::GenericPublicBlob))
$job=[pscustomobject]@{version='99.8.7';from_version='1.8.7';installer_file='Lineage-2-Chat-Translator-99.8.7-Setup.exe';installer_bytes=2048;installer_sha256=''}
$cases=[Collections.Generic.List[string]]::new()
function New-FullInstallerRequest([string]$Url){
    if($Url -cne 'https://40.160.140.227/downloads/Lineage-2-Chat-Translator-99.8.7-Setup.exe'){throw 'Fallback URL changed'}
    $script:requests++
    $request=[pscustomobject]@{}
    $request|Add-Member ScriptMethod GetResponse {return $script:fixtureResponse}
    $request|Add-Member ScriptMethod Abort {}
    return $request
}
try{
    foreach($scenario in @('valid','bad signature','redirect','truncated','oversized','wrong checksum','bad executable header')){
        $stage=Join-Path $TestDirectory ($scenario.Replace(' ','-')+'-download')
        $null=[IO.Directory]::CreateDirectory($stage)
        $body=[byte[]]::new(2048);$body[0]=77;$body[1]=90
        if($scenario -eq 'bad executable header'){$body[0]=0}
        $sha=[Security.Cryptography.SHA256]::Create()
        try{$job.installer_sha256=[BitConverter]::ToString($sha.ComputeHash($body)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
        $release=@{product=$productName;platform='windows-x64';version=$job.version;validation=@{packaged_self_test_passed=$true};installer=@{file=$job.installer_file;bytes=$job.installer_bytes;sha256=$job.installer_sha256}}
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($release|ConvertTo-Json -Compress -Depth 6))
        [IO.File]::WriteAllBytes((Join-Path $stage 'release.json'),$bytes)
        [IO.File]::WriteAllBytes((Join-Path $stage 'release.sig'),$testKey.SignData($bytes,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pss))
        if($scenario -eq 'bad signature'){[IO.File]::AppendAllText((Join-Path $stage 'release.json'),' ')}
        if($scenario -eq 'truncated'){$body=[byte[]]::new(2047)}
        if($scenario -eq 'oversized'){$body=[byte[]]::new(2049)}
        if($scenario -eq 'wrong checksum'){$body[10]=1}
        $script:inputStream=[IO.MemoryStream]::new($body,$false)
        $uri='https://40.160.140.227/downloads/'+$job.installer_file
        if($scenario -eq 'redirect'){$uri='https://example.invalid/substituted.exe'}
        $script:fixtureResponse=[pscustomobject]@{StatusCode=[Net.HttpStatusCode]::OK;ResponseUri=[Uri]$uri;ContentLength=-1}
        $script:fixtureResponse|Add-Member ScriptMethod GetResponseStream {return ,$script:inputStream}
        $script:fixtureResponse|Add-Member ScriptMethod Dispose {}
        $script:requests=0;$rejected=$false
        try{Download-FullInstaller}catch{$rejected=$true}
        if($scenario -eq 'valid'){
            if($rejected){throw 'Valid signed installer fallback was rejected'}
            $lock=Open-VerifiedInstaller (Join-Path $stage 'installer.exe');$lock.Dispose()
        }elseif(-not $rejected -or [IO.File]::Exists((Join-Path $stage 'installer.exe'))){throw ('Accepted unsafe fallback: '+$scenario)}
        if($scenario -eq 'bad signature' -and $script:requests -ne 0){throw 'Fetched installer before signature verification'}
        $script:inputStream.Dispose()
        $cases.Add($scenario)
    }
    @{cases=$cases.ToArray();installer_executed=$false}|ConvertTo-Json -Compress -Depth 3
}finally{$testKey.Dispose();$pathLocks.Dispose()}
''')
        self.assertEqual(result['cases'], ['valid', 'bad signature', 'redirect', 'truncated',
                                          'oversized', 'wrong checksum', 'bad executable header'])
        self.assertFalse(result['installer_executed'])

    def test_changed_missing_retired_files_and_failed_selftest_rollback(self):
        result = self.run_harness(r'''
function Identity-Bytes([string]$Text) {
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes($Text)
    $sha=[Security.Cryptography.SHA256]::Create()
    try {$hash=[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    return @{bytes=$bytes.Length;sha256=$hash}
}
function Write-Fixture([string]$Path,[string]$Text) {
    $null=[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
    [IO.File]::WriteAllText($Path,$Text,[Text.UTF8Encoding]::new($false))
}
function New-FileRecord([string]$Path,[string]$Text) {
    $id=Identity-Bytes $Text
    return @{path=$Path;bytes=$id.bytes;sha256=$id.sha256;blob=@{bytes=30;sha256=('a'*64)}}
}
function Test-InstalledProgram {
    $script:probes++
    if($script:failSelfTest){throw 'inert fixture self-test failure'}
    return (Join-Path $root 'Lineage2ChatTranslator.exe')
}
$originalWriteTransaction=${function:Write-Transaction}
function Write-Transaction($Value) {
    if($scenario -eq 'partial temporary rollback' -and -not $script:injectedFailure -and
        @($Value.entries|Where-Object{$_.temporary_id -ne ''}).Count -gt 0){
        $script:injectedFailure=$true
        throw 'inert fixture storage failure after temporary creation'
    }
    & $originalWriteTransaction $Value
}
$cases=[Collections.Generic.List[string]]::new()
foreach($scenario in @('success','selftest rollback','partial temporary rollback','missing payload','changed payload','modified obsolete DLL')) {
    $root=Join-Path $TestDirectory ($scenario.Replace(' ','-')+'-install')
    $stage=Join-Path $TestDirectory ($scenario.Replace(' ','-')+'-stage')
    $null=[IO.Directory]::CreateDirectory($root)
    $null=[IO.Directory]::CreateDirectory($stage)
    $pathLocks=New-UpdatePathLocks
    $preferences=Join-Path $TestDirectory ($scenario.Replace(' ','-')+'-preferences')
    $null=[IO.Directory]::CreateDirectory($preferences)
    $job=[pscustomobject]@{nonce=('a'*32);version='99.8.7';from_version='1.8.7';preferences=$preferences}
    $restartPermitted=$true;$preserveStage=$false;$script:probes=0
    $script:injectedFailure=$false
    $script:failSelfTest=$scenario -eq 'selftest rollback'
    try {
        Write-Fixture (Join-Path $root 'Lineage2ChatTranslator.exe') 'old-exe'
        Write-Fixture (Join-Path $root '_internal/keep.dll') 'unchanged'
        Write-Fixture (Join-Path $root '_internal/change.dll') 'old-library'
        Write-Fixture (Join-Path $root '_internal/retired.dll') 'known-retired'
        Write-Fixture (Join-Path $root '_internal/modified.txt') 'owner-data'
        Write-Fixture (Join-Path $root 'settings.json') 'preferences-fixture'
        Write-Fixture (Join-Path $preferences 'installed-files.json') 'previous-inventory'
        Write-Fixture (Join-Path $preferences 'installed-release.json') 'previous-release'
        Write-Fixture (Join-Path $preferences 'installed-release.sig') 'previous-signature'
        $unchanged=(Get-Item -LiteralPath (Join-Path $root '_internal/keep.dll')).LastWriteTimeUtc
        $files=@((New-FileRecord 'Lineage2ChatTranslator.exe' 'new-exe'),(New-FileRecord '_internal/keep.dll' 'unchanged'),(New-FileRecord '_internal/change.dll' 'new-library'),(New-FileRecord '_internal/new/dependency.dll' 'new-dependency'))
        foreach($file in $files){
            if($file.path -ne '_internal/keep.dll'){
                $text=switch($file.path){'Lineage2ChatTranslator.exe'{'new-exe'}'_internal/change.dll'{'new-library'}default{'new-dependency'}}
                Write-Fixture (Join-Path $stage ($file.sha256+'.payload')) $text
            }
        }
        $manifest=@{schema_version=1;product=$productName;platform='windows-x64';version=$job.version;files=$files;cleanup=@{schema_version=1;product=$productName;version=$job.version;current_paths=@($files|ForEach-Object{$_.path});obsolete_files=@(@{path='_internal/retired.dll';hashes=@((Identity-Bytes 'known-retired').sha256)},@{path='_internal/modified.txt';hashes=@((Identity-Bytes 'earlier-data').sha256)})}}
        [IO.File]::WriteAllText((Join-Path $stage 'files.json'),($manifest|ConvertTo-Json -Compress -Depth 10),[Text.UTF8Encoding]::new($false))
        Write-Fixture (Join-Path $stage 'release.json') 'inert-already-verified-release'
        Write-Fixture (Join-Path $stage 'release.sig') 'inert-already-verified-signature'
        $manifestId=Get-FileIdentity (Join-Path $stage 'files.json')
        $signedRelease=[pscustomobject]@{incremental=[pscustomobject]@{schema_version=1;manifest=[pscustomobject]@{file=('updates/'+$job.version+'/files.json');bytes=$manifestId.bytes;sha256=$manifestId.sha256}}}
        if($scenario -eq 'missing payload'){[IO.File]::Delete((Join-Path $stage ($files[0].sha256+'.payload')))}
        if($scenario -eq 'changed payload'){Write-Fixture (Join-Path $stage ($files[0].sha256+'.payload')) 'tampered'}
        if($scenario -eq 'modified obsolete DLL'){Write-Fixture (Join-Path $root '_internal/retired.dll') 'unknown-retired'}
        $rejected=$false
        $failure=''
        try {$null=Invoke-IncrementalUpdate $root}catch{$rejected=$true;$failure=$_.Exception.Message}
        if($scenario -eq 'success'){
            if($rejected){throw ('The valid file transaction failed: '+$failure)}
            foreach($file in $files){Assert-FileMatches (Get-ProgramFile $root $file.path) $file}
            if([IO.File]::Exists((Join-Path $root '_internal/retired.dll'))){throw 'Retired file survived'}
            if((Read-Record (Join-Path $stage 'transaction.json') $maxFileManifestBytes).state -cne 'committed'){throw 'Transaction not committed'}
            if([IO.File]::ReadAllText((Join-Path $preferences 'installed-files.json')) -cne [IO.File]::ReadAllText((Join-Path $stage 'files.json'))){throw 'Successful inventory not persisted'}
        }else{
            if(-not $rejected){throw ('Failed to reject '+$scenario)}
            if([IO.File]::ReadAllText((Join-Path $root 'Lineage2ChatTranslator.exe')) -cne 'old-exe'){throw 'Original executable not restored'}
            if([IO.File]::ReadAllText((Join-Path $root '_internal/change.dll')) -cne 'old-library'){throw 'Original library not restored'}
            if([IO.File]::Exists((Join-Path $root '_internal/new/dependency.dll'))){throw 'New dependency not rolled back'}
            if($scenario -in @('selftest rollback','partial temporary rollback')){
                if([IO.File]::ReadAllText((Join-Path $root '_internal/retired.dll')) -cne 'known-retired'){throw 'Retired file not restored'}
                if((Read-Record (Join-Path $stage 'transaction.json') $maxFileManifestBytes).state -cne 'rolled_back'){throw 'Rollback journal not complete'}
                if(-not $restartPermitted -or $preserveStage){throw 'Successful rollback prevented retry'}
            }
            if([IO.File]::ReadAllText((Join-Path $preferences 'installed-files.json')) -cne 'previous-inventory'){throw 'Previous update inventory not restored'}
            if([IO.File]::ReadAllText((Join-Path $preferences 'installed-release.json')) -cne 'previous-release'){throw 'Previous release receipt not restored'}
        }
        if([IO.File]::ReadAllText((Join-Path $root '_internal/modified.txt')) -cne 'owner-data'){throw 'Modified obsolete data changed'}
        if([IO.File]::ReadAllText((Join-Path $root 'settings.json')) -cne 'preferences-fixture'){throw 'Settings changed'}
        if((Get-Item -LiteralPath (Join-Path $root '_internal/keep.dll')).LastWriteTimeUtc -ne $unchanged){throw 'Unchanged file rewritten'}
        if(@(Get-ChildItem -LiteralPath $root -Recurse -File|Where-Object{$_.Name -like '*.l2ct-*'}).Count -ne 0){throw 'Temporary program file survived rollback'}
        $cases.Add($scenario)
    }finally{$pathLocks.Dispose()}
}
@{cases=$cases.ToArray();application_executed=$false}|ConvertTo-Json -Compress -Depth 3
''')
        self.assertEqual(result['cases'], ['success', 'selftest rollback', 'partial temporary rollback', 'missing payload',
                                          'changed payload', 'modified obsolete DLL'])
        self.assertFalse(result['application_executed'])


if __name__ == '__main__':
    unittest.main()
