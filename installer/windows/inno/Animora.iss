; ============================================================
;  Animora — Professional Windows Installer (Inno Setup 6)
;  Builds: dist\Animora-Setup.exe
;
;  Blender version coupling:
;  -------------------------
;  Animora is built on a Blender fork. The runtime ships its bundled
;  scripts/addons under {app}/<MAJOR.MINOR>/... — that nested version
;  directory name is required by Blender and must match the version
;  we built against. When upgrading the Blender base (5.1 → 5.2),
;  bump BlenderVersion below AND scripts/animora_config.py in lockstep.
;  See docs/UPGRADE_BLENDER.md.
; ============================================================

#define MyAppName        "Animora"
; BlenderVersion: the INTERNAL Blender base — used ONLY for the {app}\<ver>\
; install dir Blender requires. Never shown to users.
#define BlenderVersion   "5.1"
; MyAppVersion: Animora's PRODUCT version — what users see in the installer,
; Programs list, and About. Keep in sync with ANIMORA_VERSION in
; scripts/animora_config.py. (V1 = 1.x; intentionally NOT the Blender 5.1.)
#define MyAppVersion     "1.3"
#define MyAppPublisher   "Animora Technologies"
#define MyAppURL         "https://animora.tech"
#define MyAppExeName     "Animora.exe"
; The windowed launcher (renamed blender-launcher.exe by stage_for_installer).
; Launching THIS instead of Animora.exe avoids the console/terminal window
; that Animora.exe (a console-subsystem binary) pops up on start.
#define MyAppLauncher    "Animora-launcher.exe"
#define MyAppId          "{{C9F5B8A2-3D7E-4A91-9F2C-1E5B8C7A4D63}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/support
AppUpdatesURL={#MyAppURL}/download
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=C:\Users\Administrator\Desktop\Animora\dist
OutputBaseFilename=Animora-Setup
SetupIconFile=C:\Users\Administrator\Desktop\Animora\blender-fork\release\windows\icons\winblender.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
WizardStyle=modern
WizardImageFile=C:\Users\Administrator\Desktop\Animora\installer\windows\inno\wizard-image.bmp
WizardSmallImageFile=C:\Users\Administrator\Desktop\Animora\installer\windows\inno\wizard-small.bmp
WizardImageStretch=yes
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=2
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0
LicenseFile=C:\Users\Administrator\Desktop\Animora\installer\windows\inno\license.txt
ShowLanguageDialog=no
DisableWelcomePage=no
DisableReadyPage=no
DisableFinishedPage=no
AllowNoIcons=yes
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoDescription={#MyAppName} Setup
VersionInfoCopyright=Copyright (C) 2026 {#MyAppPublisher}
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce
Name: "quicklaunchicon"; Description: "Create a &Quick Launch shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "associate_anim"; Description: "Associate &.anim files with Animora"; GroupDescription: "File associations:"; Flags: checkedonce
Name: "associate_blend"; Description: "Open &.blend files with Animora"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
; Animora ships from the *staged* tree (build/windows/animora-stage/) which has
; already been rebranded: blender.exe -> Animora.exe, blender-launcher.exe ->
; Animora-launcher.exe, *.pdb excluded, etc. The recipient never sees
; "blender_*" filenames in the install progress bar.
; Run `python scripts/stage_for_installer.py` before invoking this .iss.
Source: "C:\Users\Administrator\Desktop\Animora\build\windows\animora-stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; VC++ 2015-2022 Redistributable — installed silently as prerequisite if missing.
Source: "C:\Users\Administrator\Desktop\Animora\installer\windows\inno\redist\VC_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

; ── Recording build: bundled AI engine + addon marker ───────────────────
; Present ONLY when build/backend-dist/ exists (produced by
; `python scripts/freeze_backend.py`). The `external` + `skipifsourcedoesntexist`
; flags make these entries a NO-OP for normal/production builds that never
; ran the freeze step — so the same .iss produces either a recording build
; or a plain build depending on whether the freeze output is present.
;
; 1. The frozen backend → {app}\engine\  (addon auto-launches engine\animora-backend.exe)
Source: "C:\Users\Administrator\Desktop\Animora\build\backend-dist\animora-backend\*"; DestDir: "{app}\engine"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
; 2. The bundle marker → the addon dir. Its presence is what flips the
;    addon into recording mode (auto-launch + auto-connect, no sign-in).
Source: "C:\Users\Administrator\Desktop\Animora\build\backend-dist\bundle_config.json"; DestDir: "{app}\{#BlenderVersion}\scripts\addons_core\animora_panel"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
; Launch via the windowed launcher (no console window); keep the Animora.exe
; icon for the shortcut's appearance.
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppLauncher}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppLauncher}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppLauncher}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Registry]
; .anim file association → animorafile ProgID
Root: HKA; Subkey: "Software\Classes\.anim"; ValueType: string; ValueName: ""; ValueData: "animorafile"; Flags: uninsdeletevalue; Tasks: associate_anim
Root: HKA; Subkey: "Software\Classes\.anim\OpenWithProgids"; ValueType: none; ValueName: "animorafile"; Flags: uninsdeletevalue; Tasks: associate_anim
Root: HKA; Subkey: "Software\Classes\animorafile"; ValueType: string; ValueName: ""; ValueData: "Animora File"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\animorafile\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"",0"
Root: HKA; Subkey: "Software\Classes\animorafile\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
; The legacy animora:// URL-scheme handler is GONE: sign-in now returns via a
; loopback HTTP callback (RFC 8252 §7.3) served by the running app itself —
; no protocol registration, no second headless process. Delete the stale key
; that older installers wrote so nothing on the machine still answers
; animora:// links.
Root: HKA; Subkey: "Software\Classes\animora"; ValueType: none; Flags: deletekey
; Optional: also register Animora as a handler for .blend
Root: HKA; Subkey: "Software\Classes\.blend\OpenWithProgids"; ValueType: none; ValueName: "animorafile"; Flags: uninsdeletevalue; Tasks: associate_blend
; ApplicationsRegistration so Animora appears in "Open With…" menu
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

[InstallDelete]
; V1 auth files replaced by the auth/ package + loopback flow. The [Files]
; section overwrites but never deletes, and a stale auth.py would shadow the
; new auth/ package at import time.
Type: files; Name: "{app}\{#BlenderVersion}\scripts\addons_core\animora_panel\auth.py"
Type: files; Name: "{app}\{#BlenderVersion}\scripts\addons_core\animora_panel\auth_core.py"
Type: files; Name: "{app}\{#BlenderVersion}\scripts\addons_core\animora_panel\auth_flow.py"
Type: files; Name: "{app}\{#BlenderVersion}\scripts\addons_core\animora_panel\deep_link.py"
Type: files; Name: "{app}\{#BlenderVersion}\scripts\addons_core\animora_panel\animora_url_handler.py"
Type: filesandordirs; Name: "{app}\{#BlenderVersion}\scripts\addons_core\animora_panel\__pycache__"
; Upgrades from older builds may have stale GPU/runtime DLL pollution left in
; the install dir. If opengl32.dll (or the Mesa companions) remains beside
; Animora.exe, Windows loads it before the vendor driver and launch fails with
; "OpenGL 4.3 or higher required" even on supported hardware.
Type: files; Name: "{app}\opengl32.dll"
Type: files; Name: "{app}\libEGL.dll"
Type: files; Name: "{app}\libGLESv1_CM.dll"
Type: files; Name: "{app}\libGLESv2.dll"
Type: files; Name: "{app}\libgallium_wgl.dll"
Type: files; Name: "{app}\vulkan_lvp.dll"
Type: files; Name: "{app}\vulkan_dzn.dll"
Type: files; Name: "{app}\d3d10warp.dll"
Type: files; Name: "{app}\dxil.dll"
Type: files; Name: "{app}\spirv_to_dxil.dll"
Type: files; Name: "{app}\clon12compiler.dll"
Type: files; Name: "{app}\openclon12.dll"
Type: files; Name: "{app}\msav1enchmft.dll"
Type: files; Name: "{app}\msh264enchmft.dll"
Type: files; Name: "{app}\msh265enchmft.dll"
Type: files; Name: "{app}\va.dll"
Type: files; Name: "{app}\va_win32.dll"
Type: files; Name: "{app}\vaon12_drv_video.dll"
Type: files; Name: "{app}\VkLayer_MESA_anti_lag.dll"
Type: files; Name: "{app}\VkLayer_MESA_anti_lag.json"
Type: files; Name: "{app}\lvp_icd.x86_64.json"
Type: files; Name: "{app}\dzn_icd.x86_64.json"

[Run]
; Install VC++ Redistributable BEFORE launching Animora to eliminate the
; side-by-side configuration error that occurs on machines lacking the runtime.
Filename: "{tmp}\VC_redist.x64.exe"; \
  Parameters: "/install /quiet /norestart"; \
  StatusMsg: "Installing Microsoft Visual C++ Redistributable…"; \
  Check: VCRedistNeedsInstall; \
  Flags: waituntilterminated
Filename: "{app}\{#MyAppLauncher}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
; v1.x auto-update relaunch — deliberately NOT postinstall/skipifsilent
; (those are what the entry above skips in silent mode); Check: gates
; this to fire ONLY when /ANIMORAUPDATE was passed on the command line,
; so a plain /VERYSILENT install (e.g. IT deployment) is unaffected.
Filename: "{app}\{#MyAppLauncher}"; Flags: nowait; Check: IsAutoUpdateRelaunch

[UninstallDelete]
; Clean up runtime caches (don't touch user data in AppData\Roaming).
; Path tracks BlenderVersion so an upgrade rewires this automatically.
Type: filesandordirs; Name: "{app}\{#BlenderVersion}\config"
Type: filesandordirs; Name: "{app}\{#BlenderVersion}\cache"

[Code]
function InitializeSetup(): Boolean;
begin
  { CloseApplications=force in [Setup] handles "Animora is running" automatically. }
  Result := True;
end;

{ v1.x in-app auto-update (addons/animora_panel/updater.py): the addon
  downloads this same installer, verifies its SHA-256 against the
  published app_releases.windows_sha256, then launches it with
  /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ANIMORAUPDATE and quits.
  The existing [Run] "Launch Animora" entry above has Flags: skipifsilent,
  which SUPPRESSES the post-install launch specifically when running
  silently — correct for an unattended/IT-deployed install, wrong for
  an auto-update the user explicitly asked for. This function gates a
  SECOND, always-fires (non-skipifsilent) Run entry on the presence of
  the custom /ANIMORAUPDATE switch, so ordinary silent installs
  (e.g. IT deployment scripts) are completely unaffected — only a run
  carrying this specific marker relaunches Animora automatically. }
function IsAutoUpdateRelaunch(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/ANIMORAUPDATE') = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

{ Check whether VC++ 2015-2022 Redistributable (>= 14.40) is already installed.
  Returns True if Setup needs to run the bundled installer. }
function VCRedistNeedsInstall: Boolean;
var
  Bld: Cardinal;
begin
  Result := True;
  if RegQueryDWordValue(HKLM,
       'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Bld', Bld) then
  begin
    { 14.40.33810 = VC++ 2015-2022 Redist 14.40 (May 2024).
      Anything >= that satisfies Animora's runtime requirements. }
    if Bld >= 33810 then
      Result := False;
  end;
end;
