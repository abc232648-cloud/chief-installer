"""Build and package the self-contained Linux Chief Installer artifact."""
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Optional
import zipapp

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PROJECT_ROOT))

from deployment.installer.manifest import ACCEPTED_FIRST_RELEASE_MANIFEST


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def build_desktop_package(executable: Path) -> Path:
    """Wrap the native amd64 installer in a standard desktop-installable package.

    Installing this package only adds the setup application; no Chief deployment
    or privileged helper runs during package installation.
    """
    header=executable.read_bytes()[:20]
    if len(header)<20 or header[:6]!=b"\x7fELF\x02\x01" or header[18:20]!=b"\x3e\x00":
        raise ValueError("Desktop package requires a native Linux x86-64 executable.")
    if not shutil.which("dpkg-deb"):
        raise RuntimeError("dpkg-deb is required to build the Linux desktop package.")
    target=executable.parent / "Chief-Installer-Linux-amd64.deb"
    with tempfile.TemporaryDirectory(prefix="chief-desktop-package-") as directory:
        root=Path(directory)
        binary=root / "opt/chief-installer/chief-installer"
        binary.parent.mkdir(parents=True)
        shutil.copy2(executable,binary);binary.chmod(0o755)
        applications=root / "usr/share/applications"
        applications.mkdir(parents=True)
        shutil.copy2(_PROJECT_ROOT / "packaging/installer/linux/chief-installer.desktop",applications)
        control=root / "DEBIAN/control"
        control.parent.mkdir()
        control.write_text(
            "Package: chief-installer\nVersion: 0.1.1\nArchitecture: amd64\n"
            "Maintainer: Chief Installer maintainers\nSection: admin\nPriority: optional\n"
            f"Depends: libc6 (>= {platform.libc_ver()[1]}), libx11-6, libxext6, libxrender1, libxft2, libfontconfig1\n"
            "Description: Chief graphical setup application\n"
            " Opens the Chief setup wizard. Installing this package does not deploy Chief.\n",
            encoding="utf-8")
        # Archive directories must be traversable regardless of the build umask.
        root.chmod(0o755)
        for item in root.rglob("*"):
            item.chmod(0o755 if item.is_dir() or item==binary else 0o644)
        subprocess.run(["dpkg-deb","--root-owner-group","--build",str(root),str(target)],check=True)
    target.with_suffix(".deb.sha256").write_text(sha256_file(target)+"  "+target.name+"\n",encoding="utf-8")
    return target


def build_package(output_dir: Optional[Path] = None) -> Path:
    out = output_dir or (_PROJECT_ROOT / "dist" / "installer" / "linux")
    out.mkdir(parents=True, exist_ok=True)

    target_executable = out / "chief-installer"
    manifest = ACCEPTED_FIRST_RELEASE_MANIFEST

    print(f"Building Chief Installer package for release: {manifest.release_id}...")

    # If on Linux and pyinstaller is available in environment, build using spec
    if sys.platform.startswith("linux") and shutil.which("pyinstaller"):
        # Hidden imports cannot bundle a module missing from the build Python.
        import tkinter
        import _tkinter
        spec_path = _PROJECT_ROOT / "packaging" / "installer" / "linux" / "chief_installer.spec"
        print(f"Running pyinstaller with spec: {spec_path}...")
        cmd = ["pyinstaller", "--clean", "--distpath", str(out), str(spec_path)]
        res = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
        if res.returncode != 0:
            raise RuntimeError(f"PyInstaller build failed with exit code {res.returncode}")
    else:
        # Build self-executing zipapp bundle as reliable fallback
        print("Packaging zipapp executable bundle...")
        stage_dir = out / ".stage"
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True)

        # Copy deployment/installer package
        shutil.copytree(_PROJECT_ROOT / "deployment", stage_dir / "deployment")

        # Copy __main__.py
        main_source = _PROJECT_ROOT / "deployment" / "installer" / "__main__.py"
        shutil.copy2(main_source, stage_dir / "__main__.py")

        # Copy desktop file
        desktop_file = _PROJECT_ROOT / "packaging" / "installer" / "linux" / "chief-installer.desktop"
        shutil.copy2(desktop_file, stage_dir / "chief-installer.desktop")

        # Create zipapp
        zipapp.create_archive(
            source=stage_dir,
            target=target_executable,
            interpreter="/usr/bin/env python3",
            main=None,
        )
        shutil.rmtree(stage_dir)

    # Set executable permissions
    target_executable.chmod(0o755)

    if target_executable.read_bytes()[:4] == b"\x7fELF":
        build_desktop_package(target_executable)

    digest = sha256_file(target_executable)
    meta = {
        "artifact": target_executable.name,
        "release_id": manifest.release_id,
        "sha256": digest,
        "size_bytes": target_executable.stat().st_size,
    }
    meta_path = out / "INSTALLER_SHA256.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"[OK] Chief installer package built: {target_executable}")
    print(f"  SHA-256: {digest}")
    print(f"  Size: {target_executable.stat().st_size} bytes")
    return target_executable


if __name__ == "__main__":
    build_package()
