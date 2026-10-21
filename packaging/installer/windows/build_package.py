"""Build and package the self-contained native Windows Chief Installer executable."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PROJECT_ROOT))

from deployment.installer.manifest import CURRENT_RELEASE_MANIFEST


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def build_package(output_dir: Optional[Path] = None) -> Path:
    out = output_dir or (_PROJECT_ROOT / "dist" / "installer" / "windows")
    out.mkdir(parents=True, exist_ok=True)

    target_executable = out / "Chief-Installer-Windows.exe"
    manifest = CURRENT_RELEASE_MANIFEST

    print(f"Building Windows Chief Installer package for release: {manifest.release_id}...")

    spec_path = _PROJECT_ROOT / "packaging" / "installer" / "windows" / "chief_installer_windows.spec"
    print(f"Running PyInstaller with spec: {spec_path}...")

    # Discover pyinstaller runner
    venv_pyinstaller = Path(sys.executable).parent / ("pyinstaller.exe" if os.name == "nt" else "pyinstaller")
    if venv_pyinstaller.exists():
        pyinstaller_cmd = [str(venv_pyinstaller)]
    elif shutil.which("pyinstaller"):
        pyinstaller_cmd = [shutil.which("pyinstaller")]
    else:
        # Check current python environment
        pyinstaller_cmd = [sys.executable, "-m", "PyInstaller"]

    env = dict(os.environ)
    if "PYTHONPATH" in env:
        cleaned = [p for p in env["PYTHONPATH"].split(os.pathsep) if Path(p).resolve() != _PROJECT_ROOT.resolve()]
        if cleaned:
            env["PYTHONPATH"] = os.pathsep.join(cleaned)
        else:
            env.pop("PYTHONPATH", None)

    cmd = [*pyinstaller_cmd, "--clean", "--distpath", str(out), str(spec_path)]
    res = subprocess.run(cmd, cwd=str(out), env=env)
    if res.returncode != 0:
        raise RuntimeError(f"PyInstaller build failed with exit code {res.returncode}")

    if not target_executable.exists():
        raise FileNotFoundError(f"Expected built executable at {target_executable}, but file does not exist")

    # Verify PE binary header
    header = target_executable.read_bytes()[:2]
    if header != b"MZ":
        raise ValueError(f"Invalid executable format: {target_executable} does not start with MZ header (got {header!r})")

    digest = sha256_file(target_executable)
    size = target_executable.stat().st_size

    # Write digest companion files
    sha_file = out / "Chief-Installer-Windows.exe.sha256"
    sha_file.write_text(f"{digest} *{target_executable.name}\n", encoding="utf-8")

    meta = {
        "artifact": target_executable.name,
        "release_id": manifest.release_id,
        "sha256": digest,
        "size_bytes": size,
    }
    meta_path = out / "INSTALLER_SHA256.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"[OK] Native Windows Chief Installer executable built successfully: {target_executable}")
    print(f"  SHA-256: {digest}")
    print(f"  Size: {size:,} bytes")
    return target_executable


if __name__ == "__main__":
    build_package()
