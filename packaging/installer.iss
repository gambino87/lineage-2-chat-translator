#ifndef AppVersion
  #define AppVersion "1.8.0"
#endif
#ifndef AppSource
  #error AppSource must point to the verified PyInstaller application folder
#endif
#ifndef ReleaseDir
  #error ReleaseDir must point to the release output folder
#endif
#ifndef AppIcon
  #error AppIcon must point to the application icon
#endif
#ifndef AppFileList
  #error AppFileList must point to the verified current payload file list
#endif
#ifndef AppVerifyList
  #error AppVerifyList must point to the verified current payload checksum code
#endif

[Setup]
AppId={{1D8CE3C7-53A6-4C67-AC9F-1C6F0D6B1920}
AppName=Lineage 2 Chat Translator
AppVersion={#AppVersion}
AppVerName=Lineage 2 Chat Translator {#AppVersion}
AppPublisher=Lineage 2 Chat Translator
SetupIconFile={#AppIcon}
DefaultDirName={autopf}\Lineage 2 Chat Translator
DefaultGroupName=Lineage 2 Chat Translator
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=yes
DisableWelcomePage=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
AppendDefaultDirName=no
UninstallDisplayIcon={app}\_internal\assets\L2CT.ico
Uninstallable=yes
OutputDir={#ReleaseDir}
OutputBaseFilename=Lineage-2-Chat-Translator-{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AppMutex=Local\L2ProminenceChatTranslator
SetupMutex=Local\Lineage2ChatTranslatorSetup
SetupLogging=yes
VersionInfoVersion={#AppVersion}.0
VersionInfoDescription=Lineage 2 Chat Translator Setup
VersionInfoCompany=Lineage 2 Chat Translator
ShowLanguageDialog=yes
LanguageDetectionMethod=uilanguage
UsePreviousLanguage=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"; InfoBeforeFile: "languages\info-en.txt"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"; InfoBeforeFile: "languages\info-es.txt"
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"; InfoBeforeFile: "languages\info-pt-BR.txt"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"; InfoBeforeFile: "languages\info-ru.txt"
Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"; InfoBeforeFile: "languages\info-pl.txt"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"; InfoBeforeFile: "languages\info-de.txt"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"; InfoBeforeFile: "languages\info-fr.txt"
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"; InfoBeforeFile: "languages\info-tr.txt"
Name: "ko"; MessagesFile: "compiler:Languages\Korean.isl"; InfoBeforeFile: "languages\info-ko.txt"
Name: "ja"; MessagesFile: "compiler:Languages\Japanese.isl"; InfoBeforeFile: "languages\info-ja.txt"
Name: "zhcn"; MessagesFile: "languages\ChineseSimplified.isl"; InfoBeforeFile: "languages\info-zh-CN.txt"

#include "languages\CustomMessages.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Dirs]
; The security helper creates this folder before Inno records installation.
; Still remove it on uninstall when empty; never recursively delete user files.
Name: "{app}"; Flags: uninsalwaysuninstall

[Files]
; Secure the program directory before installing or running any of its code.
Source: "{#AppSource}\_internal\assets\protect-install-directory.ps1"; DestName: "l2-protect-install-directory.ps1"; Flags: dontcopy
; Extract the trusted helper outside the installed DLL search directory.
Source: "{#AppSource}\_internal\assets\cleanup-program-files.ps1"; DestName: "l2-program-cleanup.ps1"; Flags: dontcopy
#include AppFileList

[Icons]
Name: "{group}\Lineage 2 Chat Translator"; Filename: "{app}\Lineage2ChatTranslator.exe"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\L2CT.ico"; IconIndex: 0
Name: "{group}\{cm:ConfigureProgram}"; Filename: "{app}\Lineage2ChatTranslator.exe"; Parameters: "--configure"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\L2CT.ico"; IconIndex: 0
Name: "{group}\{cm:UninstallProgram,Lineage 2 Chat Translator}"; Filename: "{uninstallexe}"; IconFilename: "{app}\_internal\assets\L2CT.ico"; IconIndex: 0
Name: "{autodesktop}\Lineage 2 Chat Translator"; Filename: "{app}\Lineage2ChatTranslator.exe"; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\_internal\assets\L2CT.ico"; IconIndex: 0

[Run]
Filename: "{app}\Lineage2ChatTranslator.exe"; Description: "{cm:LaunchProgram,Lineage 2 Chat Translator}"; Flags: postinstall nowait skipifsilent; Check: ModelIsReady

[Code]
function SetDllDirectory(PathName: String): BOOL;
  external 'SetDllDirectoryW@kernel32.dll stdcall';

var
  ModelSetupAttempted: Boolean;
  ModelSetupComplete: Boolean;
  AutomaticUpdate: Boolean;
  ProgramSecurityComplete: Boolean;
  ProgramCleanupComplete: Boolean;

#include AppVerifyList

function ProtectInstallationDirectory(VerifyOnly: Boolean): Boolean;
var
  ExitCode, I: Integer;
  ResultPath, HelperPath, PowerShellPath, Arguments, Details: String;
  Lines: TArrayOfString;
begin
  { Keep legacy upgrade paths: older update helpers explicitly target/reopen
    that directory. Repair its permissions instead of silently relocating it.
    The trusted helper never starts an installed executable or changes parent
    folders. A failure must stop setup before program replacement or startup. }
  ResultPath := ExpandConstant('{tmp}\l2-install-security-result.json');
  HelperPath := ExpandConstant('{tmp}\l2-protect-install-directory.ps1');
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  WizardForm.StatusLabel.Caption := CustomMessage('CheckingPermissions');
  Result := False;
  try
    ExtractTemporaryFile('l2-protect-install-directory.ps1');
    Arguments := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + HelperPath +
      '" -InstallRoot "' + ExpandConstant('{app}') + '" -ResultPath "' + ResultPath + '"';
    if VerifyOnly then Arguments := Arguments + ' -VerifyOnly';
    ExitCode := -1;
    Result := Exec(PowerShellPath, Arguments, ExpandConstant('{tmp}'),
      SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
    Details := '';
    if LoadStringsFromFile(ResultPath, Lines) then
      for I := 0 to GetArrayLength(Lines) - 1 do
        Details := Details + Lines[I] + #13#10;
    Log('Program directory security: ' + Details);
  finally
    DeleteFile(ResultPath);
    DeleteFile(HelperPath);
  end;
end;

function CleanRetiredProgramFiles: Boolean;
var
  ExitCode, I: Integer;
  ResultPath, HelperPath, PowerShellPath, Details: String;
  Lines: TArrayOfString;
begin
  { Never start the translator to clean its own DLLs: its loader may pin them.
    The system helper runs from Inno's private temporary directory, after file
    replacement, and exits before model setup, self-test or app restart. }
  ResultPath := ExpandConstant('{tmp}\l2-program-cleanup-result.json');
  HelperPath := ExpandConstant('{tmp}\l2-program-cleanup.ps1');
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  WizardForm.StatusLabel.Caption := CustomMessage('CleaningProgram');
  try
    ExtractTemporaryFile('l2-program-cleanup.ps1');
    ExitCode := -1;
    { Inno 6 disables WOW64 redirection for Exec in our required 64-bit install
      mode, so the system-directory constant selects native PowerShell. }
    Result := Exec(PowerShellPath,
      '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + HelperPath +
      '" -InstallRoot "' + ExpandConstant('{app}') + '" -Version "{#AppVersion}" -ResultPath "' + ResultPath + '"',
      ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
    Details := '';
    if LoadStringsFromFile(ResultPath, Lines) then
      for I := 0 to GetArrayLength(Lines) - 1 do
        Details := Details + Lines[I] + #13#10;
    Log('Program file cleanup: ' + Details);
    if not Result then
      SuppressibleMsgBox(CustomMessage('CleanupFailed') + #13#10#13#10 + Details,
        mbError, MB_OK, IDOK);
  finally
    DeleteFile(ResultPath);
    DeleteFile(HelperPath);
  end;
end;

function FinishInterruptedUpdateRepair: Boolean;
var
  ExitCode, I: Integer;
  ResultPath, Details: String;
  Lines: TArrayOfString;
begin
  { The permission and cleanup helpers have verified every installed file.
    Only now may the app retire recovery backups from an interrupted update. }
  ResultPath := ExpandConstant('{tmp}\l2-update-repair-result.json');
  try
    ExitCode := -1;
    Result := Exec(ExpandConstant('{app}\Lineage2ChatTranslator.exe'),
      '--finish-update-repair --output "' + ResultPath + '"',
      ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
    if not Result then begin
      Details := '';
      if LoadStringsFromFile(ResultPath, Lines) then
        for I := 0 to GetArrayLength(Lines) - 1 do
          Details := Details + Lines[I] + #13#10;
      SuppressibleMsgBox(CustomMessage('CleanupFailed') + #13#10#13#10 + Details,
        mbError, MB_OK, IDOK);
    end;
  finally
    DeleteFile(ResultPath);
  end;
end;

function InitializeSetup: Boolean;
var
  I: Integer;
begin
  { Updates started by older frozen launchers can inherit their _internal DLL
    search directory. Drop it before starting any installer child process. }
  if not SetDllDirectory('') then begin
    SuppressibleMsgBox(CustomMessage('EnvironmentFailed'),
      mbError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;
  AutomaticUpdate := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/AUTOUPDATE=1') = 0 then
      AutomaticUpdate := True;
  Result := True;
  if AutomaticUpdate and not WizardSilent then begin
    SuppressibleMsgBox(CustomMessage('AutomaticUpdateOnly'), mbError, MB_OK, IDOK);
    Result := False;
  end else if CheckForMutexes('Local\Lineage2ChatTranslatorUpdate') and not AutomaticUpdate then begin
    SuppressibleMsgBox(CustomMessage('UpdateBusy'), mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

function InitializeUninstall: Boolean;
begin
  Result := not CheckForMutexes('Local\Lineage2ChatTranslatorUpdate');
  if not Result then
    SuppressibleMsgBox(CustomMessage('UninstallUpdateBusy'), mbError, MB_OK, IDOK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if AutomaticUpdate and (not FileExists(ExpandConstant('{app}\Lineage2ChatTranslator.exe')) or
                         not FileExists(ExpandConstant('{app}\unins000.exe'))) then
    Result := CustomMessage('InstallationMissing');
  if Result <> '' then Exit;
  { Windows may not have created this standard per-user Programs folder yet.
    Do not create arbitrary custom parents or alter an existing parent's ACL. }
  if CompareText(ExtractFileDir(ExpandConstant('{app}')), ExpandConstant('{autopf}')) = 0 then
    if not DirExists(ExpandConstant('{autopf}')) then
      if not ForceDirectories(ExpandConstant('{autopf}')) then begin
        Result := CustomMessage('ProgramsFolderFailed');
        Exit;
      end;
  if not ProtectInstallationDirectory(False) then
    Result := CustomMessage('PermissionFailed');
end;

function ModelIsReady: Boolean;
begin
  Result := ProgramSecurityComplete and ModelSetupComplete;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
  Started, Retry: Boolean;
begin
  if CurStep = ssPostInstall then begin
    { Check all newly installed files before cleanup, model setup or [Run]. }
    ProgramSecurityComplete := ProtectInstallationDirectory(True);
    if not ProgramSecurityComplete then begin
      SuppressibleMsgBox(CustomMessage('InstalledPermissionFailed'),
        mbError, MB_OK, IDOK);
      Exit;
    end;
    ProgramCleanupComplete := CleanRetiredProgramFiles;
    if not ProgramCleanupComplete then Exit;
    ProgramCleanupComplete := VerifyCurrentProgramFiles;
    if not ProgramCleanupComplete then begin
      SuppressibleMsgBox(CustomMessage('CleanupFailed'), mbError, MB_OK, IDOK);
      Exit;
    end;
    ProgramCleanupComplete := FinishInterruptedUpdateRepair;
    if not ProgramCleanupComplete then Exit;
  end;
  if (CurStep = ssPostInstall) and not WizardSilent then begin
    ModelSetupAttempted := True;
    repeat
      WizardForm.StatusLabel.Caption := CustomMessage('SettingUpModel');
      Started := Exec(ExpandConstant('{app}\Lineage2ChatTranslator.exe'),
        '--install-model --ui-language ' + CustomMessage('UiLanguageCode'), '', SW_SHOWNORMAL, ewWaitUntilTerminated, ExitCode);
      ModelSetupComplete := Started and (ExitCode = 0);
      Retry := False;
      if not ModelSetupComplete then
        Retry := MsgBox(CustomMessage('RetryModelSetup'),
          mbError, MB_RETRYCANCEL) = IDRETRY;
    until ModelSetupComplete or not Retry;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and not ProgramSecurityComplete then
    WizardForm.FinishedLabel.Caption := CustomMessage('InstalledPermissionFailed')
  else if (CurPageID = wpFinished) and not ProgramCleanupComplete then
    WizardForm.FinishedLabel.Caption := CustomMessage('CleanupFailed')
  else if (CurPageID = wpFinished) and ModelSetupAttempted and not ModelSetupComplete then
    WizardForm.FinishedLabel.Caption := CustomMessage('FinishModelSetup');
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := 0;
  if not ProgramSecurityComplete then Result := 5
  else if not ProgramCleanupComplete then Result := 4
  else if ModelSetupAttempted and not ModelSetupComplete then Result := 2;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ExitCode, I: Integer;
  ResultPath, Details, IncrementalPlan, IncrementalHelper, IncrementalResult, PowerShellPath: String;
  Lines: TArrayOfString;
  Started: Boolean;
begin
  if CurUninstallStep = usUninstall then begin
    { Retained by Inno until exit, including after the cleanup helper exits. }
    CreateMutex('Local\Lineage2ChatTranslatorUninstall');
    UninstallProgressForm.StatusLabel.Caption := CustomMessage('RemovingData');
    { Inno provides a private temporary directory for this uninstall run. }
    ResultPath := ExpandConstant('{tmp}\l2-uninstall-result.txt');
    try
      Started := Exec(ExpandConstant('{app}\Lineage2ChatTranslator.exe'),
        '--uninstall-cleanup --ui-language ' + CustomMessage('UiLanguageCode') + ' --cleanup-result "' + ResultPath + '"',
        '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
      { Code 2 means managed cleanup succeeded but unconfirmed history remains. }
      if not Started or ((ExitCode <> 0) and (ExitCode <> 2)) then begin
        Details := '';
        if LoadStringsFromFile(ResultPath, Lines) then
          for I := 0 to GetArrayLength(Lines) - 1 do
            Details := Details + Lines[I] + #13#10;
        MsgBox(CustomMessage('UninstallCleanupFailed') + #13#10#13#10 + Details,
          mbError, MB_OK);
        Abort;
      end;
      Details := '';
      if LoadStringsFromFile(ResultPath, Lines) then
        for I := 0 to GetArrayLength(Lines) - 1 do
          Details := Details + Lines[I] + #13#10;
      if ExitCode = 2 then
        SuppressibleMsgBox(Details, mbInformation, MB_OK, IDOK);
      { File updates can add paths absent from the original Inno uninstall
        log. The trusted app exported only authenticated current file hashes
        before removing preferences, then exited so its DLLs are no longer mapped. }
      IncrementalPlan := ExpandConstant('{tmp}\l2-incremental-uninstall.json');
      IncrementalHelper := ExpandConstant('{tmp}\l2-incremental-uninstall.ps1');
      IncrementalResult := ExpandConstant('{tmp}\l2-incremental-uninstall-result.json');
      if FileExists(IncrementalPlan) then begin
        PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
        ExitCode := -1;
        Started := Exec(PowerShellPath,
          '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + IncrementalHelper +
          '" -InstallRoot "' + ExpandConstant('{app}') + '" -UninstallPlan "' + IncrementalPlan +
          '" -ResultPath "' + IncrementalResult + '"',
          ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, ExitCode);
        if not Started or (ExitCode <> 0) then begin
          Details := '';
          if LoadStringsFromFile(IncrementalResult, Lines) then
            for I := 0 to GetArrayLength(Lines) - 1 do
              Details := Details + Lines[I] + #13#10;
          MsgBox(CustomMessage('UninstallCleanupFailed') + #13#10#13#10 + Details, mbError, MB_OK);
          Abort;
        end;
      end;
    finally
      DeleteFile(ResultPath);
      DeleteFile(ExpandConstant('{tmp}\l2-incremental-uninstall.json'));
      DeleteFile(ExpandConstant('{tmp}\l2-incremental-uninstall.ps1'));
      DeleteFile(ExpandConstant('{tmp}\l2-incremental-uninstall-result.json'));
    end;
  end;
end;
