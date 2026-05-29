@echo off
setlocal enabledelayedexpansion
title Animora Setup

set "TARGET=%LOCALAPPDATA%\Animora"
set "SRC=%~dp0bin"

echo.
echo  ============================================================
echo                       Animora Installer
echo  ============================================================
echo.
echo  Installing to: !TARGET!
echo.

:: Stop any running instance of Animora so we can overwrite the binary
taskkill /F /IM blender.exe >nul 2>&1

:: Clean previous install (preserve user data which lives in AppData\Roaming, not here)
if exist "!TARGET!" (
    echo  Removing previous installation...
    rmdir /S /Q "!TARGET!" 2>nul
)

mkdir "!TARGET!" 2>nul

echo  Copying files (this may take a minute)...
:: robocopy /MOVE moves files (fast on same drive), recurses subdirs
robocopy "!SRC!" "!TARGET!" /MOVE /E /NFL /NDL /NJH /NJS /R:2 /W:1 >nul
if errorlevel 8 (
    echo  ERROR: file copy failed.
    pause
    exit /b 1
)

echo  Registering .anim file association...
reg add "HKCU\Software\Classes\.anim" /f /ve /t REG_SZ /d "animorafile" >nul
reg add "HKCU\Software\Classes\animorafile" /f /ve /t REG_SZ /d "Animora File" >nul
reg add "HKCU\Software\Classes\animorafile\DefaultIcon" /f /ve /t REG_SZ /d "\"!TARGET!\blender.exe\",1" >nul
reg add "HKCU\Software\Classes\animorafile\shell\open\command" /f /ve /t REG_SZ /d "\"!TARGET!\blender.exe\" \"%%1\"" >nul

:: Also register .blend so existing Blender files open in Animora when double-clicked
reg add "HKCU\Software\Classes\.blend\OpenWithProgids" /f /v "animorafile" /t REG_NONE >nul 2>&1

echo  Creating Start Menu and Desktop shortcuts...

set "PS_CREATE_LNK=$WS=(New-Object -COM WScript.Shell); $tgt='!TARGET!\blender.exe'; $wd='!TARGET!';"

powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS_CREATE_LNK% $s=$WS.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Programs'),'Animora.lnk')); $s.TargetPath=$tgt; $s.WorkingDirectory=$wd; $s.IconLocation=$tgt+',0'; $s.Description='Animora - AI-native 3D creation'; $s.Save()"
powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS_CREATE_LNK% $s=$WS.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'Animora.lnk')); $s.TargetPath=$tgt; $s.WorkingDirectory=$wd; $s.IconLocation=$tgt+',0'; $s.Description='Animora - AI-native 3D creation'; $s.Save()"

:: Register Add/Remove Programs entry
set "UNINST_KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Animora"
reg add "%UNINST_KEY%" /f /v "DisplayName" /t REG_SZ /d "Animora" >nul
reg add "%UNINST_KEY%" /f /v "DisplayVersion" /t REG_SZ /d "5.1.1" >nul
reg add "%UNINST_KEY%" /f /v "Publisher" /t REG_SZ /d "Animora Technologies" >nul
reg add "%UNINST_KEY%" /f /v "DisplayIcon" /t REG_SZ /d "!TARGET!\blender.exe" >nul
reg add "%UNINST_KEY%" /f /v "InstallLocation" /t REG_SZ /d "!TARGET!" >nul
reg add "%UNINST_KEY%" /f /v "UninstallString" /t REG_SZ /d "\"!TARGET!\uninstall.bat\"" >nul
reg add "%UNINST_KEY%" /f /v "NoModify" /t REG_DWORD /d 1 >nul
reg add "%UNINST_KEY%" /f /v "NoRepair" /t REG_DWORD /d 1 >nul

:: Write a tiny uninstaller into the install dir
(
    echo @echo off
    echo setlocal
    echo taskkill /F /IM blender.exe ^>nul 2^>^&1
    echo reg delete "HKCU\Software\Classes\.anim" /f ^>nul 2^>^&1
    echo reg delete "HKCU\Software\Classes\animorafile" /f ^>nul 2^>^&1
    echo reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Animora" /f ^>nul 2^>^&1
    echo del "%%USERPROFILE%%\Desktop\Animora.lnk" ^>nul 2^>^&1
    echo del "%%APPDATA%%\Microsoft\Windows\Start Menu\Programs\Animora.lnk" ^>nul 2^>^&1
    echo rmdir /S /Q "!TARGET!"
    echo echo Animora uninstalled.
    echo pause
) > "!TARGET!\uninstall.bat"

echo.
echo  ============================================================
echo                Installation complete!
echo  ============================================================
echo.
echo  Animora has been installed to:
echo    !TARGET!
echo.
echo  Shortcuts created on the Desktop and Start Menu.
echo  Launching Animora now...
echo.

start "" "!TARGET!\blender.exe"

:: Give the user a moment to see the message
timeout /t 3 /nobreak >nul
endlocal
exit /b 0
