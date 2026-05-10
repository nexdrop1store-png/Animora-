@echo off
setlocal

set BUILD_DIR=C:\Users\Administrator\Desktop\Animora\build\windows
set VCVARSALL=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat
set LOG=C:\Users\Administrator\Desktop\Animora\build_log.txt

echo === Initializing MSVC x64 === >> "%LOG%" 2>&1
call "%VCVARSALL%" x64 >> "%LOG%" 2>&1

echo === Starting build === >> "%LOG%" 2>&1
echo Processors: %NUMBER_OF_PROCESSORS% >> "%LOG%" 2>&1

cmake --build "%BUILD_DIR%" --config Release --parallel %NUMBER_OF_PROCESSORS% >> "%LOG%" 2>&1

if errorlevel 1 (
    echo BUILD FAILED >> "%LOG%" 2>&1
    exit /b 1
)

echo BUILD SUCCEEDED >> "%LOG%" 2>&1
echo Binary: %BUILD_DIR%\bin\blender.exe >> "%LOG%" 2>&1

endlocal
