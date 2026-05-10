@echo off
setlocal EnableDelayedExpansion

set BLENDER_DIR=C:\Users\Administrator\Desktop\Animora\blender-fork
set BUILD_DIR=C:\Users\Administrator\Desktop\Animora\build\windows
set VCVARSALL=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat

echo === Initializing MSVC x64 environment ===
call "%VCVARSALL%" x64
if errorlevel 1 (
    echo ERROR: Failed to initialize MSVC environment
    exit /b 1
)

echo === Verifying tools ===
where cmake.exe
if errorlevel 1 (
    echo ERROR: cmake.exe not found after vcvarsall
    exit /b 1
)
where ninja.exe
if errorlevel 1 (
    echo ERROR: ninja.exe not found after vcvarsall
    exit /b 1
)

echo CMake and Ninja found.

echo === Creating build directory ===
if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"

echo === Running CMake configure ===
cmake -S "%BLENDER_DIR%" -B "%BUILD_DIR%" ^
    -G "Ninja" ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DWITH_BUILDINFO=On ^
    -DWITH_AUDASPACE=On ^
    -DWITH_CYCLES=On ^
    -DWITH_PYTHON=On ^
    -DWITH_PYTHON_INSTALL=On ^
    -DWITH_PYTHON_INSTALL_NUMPY=On

if errorlevel 1 (
    echo ERROR: CMake configure failed
    exit /b 1
)

echo.
echo === CMake configure succeeded! ===
echo.

if "%1"=="BUILD" (
    echo === Building Animora (this will take 30-90 minutes) ===
    cmake --build "%BUILD_DIR%" --config Release --parallel %NUMBER_OF_PROCESSORS%
    if errorlevel 1 (
        echo ERROR: Build failed
        exit /b 1
    )
    echo.
    echo === Build complete! ===
    echo Binary location: %BUILD_DIR%\bin\
)

endlocal
