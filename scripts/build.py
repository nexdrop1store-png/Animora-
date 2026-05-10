"""
Animora build orchestrator.

Steps:
  1. Run rebrand.py (asset injection + string patching)
  2. Configure cmake build directory
  3. Compile
  4. Package (platform-specific installer)

Usage:
    python scripts/build.py [--platform {windows,macos,linux}] [--config {Release,Debug}]
                            [--skip-rebrand] [--skip-compile] [--jobs N]
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("build")

REPO_ROOT = Path(__file__).resolve().parent.parent
FORK_ROOT = REPO_ROOT / "blender-fork"
BUILD_DIR = REPO_ROOT / "build"
DIST_DIR = REPO_ROOT / "dist"

CMAKE_COMMON_FLAGS = [
    "-DWITH_PYTHON_INSTALL=ON",
    "-DWITH_PYTHON_INSTALL_NUMPY=ON",
    "-DWITH_INTERNATIONAL=ON",
    "-DWITH_CODEC_FFMPEG=ON",
    "-DWITH_IMAGE_OPENEXR=ON",
    "-DWITH_CYCLES=ON",
    "-DWITH_MOD_FLUID=ON",
    # Animora-specific: disable Blender splash URL opening
    "-DWITH_BLENDER_THUMBNAILER=OFF",
]

CMAKE_PLATFORM_FLAGS: dict[str, list[str]] = {
    "windows": [
        "-G", "Visual Studio 17 2022",
        "-A", "x64",
        "-DWITH_WINDOWS_BUNDLE_CRT=ON",
    ],
    "macos": [
        "-G", "Xcode",
        "-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64",
        "-DWITH_CODESIGN=ON",
    ],
    "linux": [
        "-G", "Ninja",
        "-DWITH_INSTALL_PORTABLE=ON",
    ],
}


def detect_platform() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    log.info("$ %s", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, cwd=cwd, check=False)
    if check and result.returncode != 0:
        log.error("Command failed with exit code %d", result.returncode)
        sys.exit(result.returncode)
    return result.returncode


def step_rebrand() -> None:
    log.info("--- Step 1: Rebrand ---")
    run([sys.executable, str(REPO_ROOT / "scripts" / "rebrand.py")])


def step_cmake_configure(target_platform: str, config: str, build_dir: Path) -> None:
    log.info("--- Step 2: CMake Configure ---")
    build_dir.mkdir(parents=True, exist_ok=True)
    cmake_flags = CMAKE_COMMON_FLAGS + CMAKE_PLATFORM_FLAGS.get(target_platform, [])
    cmake_flags += [f"-DCMAKE_BUILD_TYPE={config}"]
    run(["cmake", str(FORK_ROOT)] + cmake_flags, cwd=build_dir)


def step_compile(build_dir: Path, config: str, jobs: int) -> None:
    log.info("--- Step 3: Compile ---")
    run(
        ["cmake", "--build", ".", "--config", config, "--parallel", str(jobs)],
        cwd=build_dir,
    )


def step_package(target_platform: str, build_dir: Path, config: str) -> None:
    log.info("--- Step 4: Package ---")
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    if target_platform == "windows":
        _package_windows(build_dir, config)
    elif target_platform == "macos":
        _package_macos(build_dir)
    else:
        _package_linux(build_dir)


def _package_windows(build_dir: Path, config: str) -> None:
    nsis_script = REPO_ROOT / "installer" / "windows" / "animora.nsi"
    if not nsis_script.exists():
        log.warning("NSIS script not found — skipping installer packaging")
        return
    run(["makensis", str(nsis_script)], cwd=build_dir)
    # Sign if cert available
    cert_path = os.environ.get("WINDOWS_CERT_PATH")
    cert_pass = os.environ.get("WINDOWS_CERT_PASSWORD")
    installer = list(build_dir.glob("Animora-*-windows-x64.exe"))
    if cert_path and cert_pass and installer:
        run([
            "signtool", "sign",
            "/f", cert_path,
            "/p", cert_pass,
            "/tr", "http://timestamp.digicert.com",
            "/td", "sha256",
            "/fd", "sha256",
            str(installer[0]),
        ])
        shutil.copy(installer[0], DIST_DIR / installer[0].name)
    elif installer:
        shutil.copy(installer[0], DIST_DIR / installer[0].name)


def _package_macos(build_dir: Path) -> None:
    pkg_script = REPO_ROOT / "installer" / "macos" / "build_pkg.sh"
    if not pkg_script.exists():
        log.warning("macOS pkg script not found — skipping")
        return
    run(["bash", str(pkg_script), str(build_dir), str(DIST_DIR)])


def _package_linux(build_dir: Path) -> None:
    appimage_script = REPO_ROOT / "installer" / "linux" / "build_appimage.sh"
    if not appimage_script.exists():
        log.warning("AppImage script not found — skipping")
        return
    run(["bash", str(appimage_script), str(build_dir), str(DIST_DIR)])


def smoke_test(build_dir: Path, target_platform: str) -> None:
    log.info("--- Smoke Test ---")
    if target_platform == "windows":
        binaries = list(build_dir.rglob("animora.exe"))
    else:
        binaries = list(build_dir.rglob("animora"))
    if not binaries:
        log.warning("Animora binary not found — skipping smoke test")
        return
    binary = binaries[0]
    result = run([str(binary), "--background", "--python-exit-code", "1", "-noaudio"], check=False)
    if result == 0:
        log.info("Smoke test PASSED")
    else:
        log.error("Smoke test FAILED (exit %d)", result)
        sys.exit(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Animora")
    parser.add_argument(
        "--platform",
        choices=["windows", "macos", "linux"],
        default=detect_platform(),
    )
    parser.add_argument("--config", choices=["Release", "Debug", "RelWithDebInfo"], default="Release")
    parser.add_argument("--skip-rebrand", action="store_true")
    parser.add_argument("--skip-compile", action="store_true")
    parser.add_argument("--skip-package", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    build_dir = BUILD_DIR / args.platform / args.config

    log.info("=== Animora Build: platform=%s config=%s ===", args.platform, args.config)

    if not args.skip_rebrand:
        step_rebrand()

    if not args.skip_compile:
        step_cmake_configure(args.platform, args.config, build_dir)
        step_compile(build_dir, args.config, args.jobs)

    if not args.skip_package:
        step_package(args.platform, build_dir, args.config)

    if args.smoke_test:
        smoke_test(build_dir, args.platform)

    log.info("=== Build complete. Output: %s ===", DIST_DIR)


if __name__ == "__main__":
    main()
