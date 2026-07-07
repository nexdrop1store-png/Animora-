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
import shlex
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


def _format_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


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
    run([sys.executable, str(REPO_ROOT / "scripts" / "stage_for_installer.py")])
    _verify_windows_stage()

    inno_script = REPO_ROOT / "installer" / "windows" / "inno" / "Animora.iss"
    if not inno_script.exists():
        log.warning("Inno script not found - skipping installer packaging")
        return

    iscc = shutil.which("ISCC.exe")
    if not iscc:
        roots = [
            Path(os.environ.get("ProgramFiles(x86)", "")),
            Path(os.environ.get("ProgramFiles", "")),
        ]
        for root in roots:
            if not str(root):
                continue
            candidates = sorted(root.glob("Inno Setup*\\ISCC.exe"))
            if candidates:
                iscc = str(candidates[-1])
                break

    if not iscc:
        log.warning("ISCC.exe not found - skipping installer packaging")
        return

    run([iscc, str(inno_script)], cwd=REPO_ROOT)

    installer = DIST_DIR / "Animora-Setup.exe"
    if not installer.exists():
        log.warning("Animora-Setup.exe not produced by Inno packaging")
        return

    cert_path = os.environ.get("WINDOWS_CERT_PATH")
    cert_pass = os.environ.get("WINDOWS_CERT_PASSWORD")
    if cert_path and cert_pass:
        run([
            "signtool", "sign",
            "/f", cert_path,
            "/p", cert_pass,
            "/tr", "http://timestamp.digicert.com",
            "/td", "sha256",
            "/fd", "sha256",
            str(installer),
        ])


def _verify_windows_stage() -> None:
    stage_dir = BUILD_DIR / "windows" / "animora-stage"
    stage_checks = [
        stage_dir / "Animora.exe",
        stage_dir / "Animora-launcher.exe",
    ]

    missing = [path for path in stage_checks if not path.exists()]
    if missing:
        for path in missing:
            log.error("Missing staged runtime artifact: %s", path)
        sys.exit(1)

    commands = [
        [str(stage_dir / "Animora.exe"), "--background", "--version"],
        [str(stage_dir / "Animora-launcher.exe"), "--background", "--version"],
    ]
    for cmd in commands:
        log.info("--- Verify staged runtime: %s ---", _format_cmd(cmd))
        result = subprocess.run(cmd, cwd=stage_dir, check=False)
        if result.returncode != 0:
            log.error("Staged runtime verification failed with exit code %d", result.returncode)
            sys.exit(result.returncode)


def _package_macos(build_dir: Path) -> None:
    pkg_script = REPO_ROOT / "installer" / "macos" / "build_pkg.sh"
    if not pkg_script.exists():
        log.warning("macOS pkg script not found - skipping")
        return
    run(["bash", str(pkg_script), str(build_dir), str(DIST_DIR)])


def _package_linux(build_dir: Path) -> None:
    appimage_script = REPO_ROOT / "installer" / "linux" / "build_appimage.sh"
    if not appimage_script.exists():
        log.warning("AppImage script not found - skipping")
        return
    run(["bash", str(appimage_script), str(build_dir), str(DIST_DIR)])


def smoke_test(build_dir: Path, target_platform: str) -> None:
    log.info("--- Smoke Test ---")
    if target_platform == "windows":
        binaries = list(build_dir.rglob("animora.exe"))
    else:
        binaries = list(build_dir.rglob("animora"))
    if not binaries:
        log.warning("Animora binary not found - skipping smoke test")
        return
    binary = binaries[0]
    result = run([str(binary), "--background", "--python-exit-code", "1", "-noaudio"], check=False)
    if result == 0:
        log.info("Smoke test PASSED")
    else:
        log.error("Smoke test FAILED (exit %d)", result)
        sys.exit(result)


def step_default_startup(target_platform: str) -> None:
    """Regenerate the branded startup.blend BEFORE compiling.

    DataToC bakes blender-fork/release/datafiles/startup.blend into the
    binary at compile time — without this step, whatever file happens to be
    on disk ships silently. build_default_startup.py needs a GUI-capable
    binary from a PREVIOUS build (area_split requires a real window); on a
    first-ever build there is none, so warn loudly and continue — rerun the
    build after the first compile to bake the branded startup.
    """
    log.info("--- Step 1.5: Regenerate default startup.blend ---")
    if target_platform != "windows":
        log.warning("startup regen implemented for the Windows pipeline only — skipping")
        return
    binary = BUILD_DIR / "windows" / "bin" / "blender.exe"
    if not binary.exists():
        binary = BUILD_DIR / "windows" / "bin" / "Animora.exe"
    if not binary.exists():
        log.warning(
            "No previously built binary at %s — SKIPPING startup regen. "
            "The compile will bake whatever startup.blend is on disk; rerun "
            "the build once a binary exists.",
            BUILD_DIR / "windows" / "bin",
        )
        return
    script = REPO_ROOT / "scripts" / "build_default_startup.py"
    # GUI mode on purpose (the script needs a window; it quits itself).
    run([str(binary), "--python", str(script)])


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
    parser.add_argument("--skip-startup", action="store_true",
                        help="Skip regenerating the branded startup.blend before compile")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    build_dir = BUILD_DIR / args.platform / args.config

    log.info("=== Animora Build: platform=%s config=%s ===", args.platform, args.config)

    if not args.skip_rebrand:
        step_rebrand()

    if not args.skip_startup:
        step_default_startup(args.platform)

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
