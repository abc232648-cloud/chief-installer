"""Main entrypoint for the Chief Unified Installer.

When double-clicked or executed without arguments on a graphical desktop,
launches the graphical installer. When arguments are supplied or running
headless, runs the CLI interface.
"""
import os
import shutil
import sys

try:
    from .cli import run_cli
except ImportError:
    from deployment.installer.cli import run_cli

_FROZEN_EXECUTABLE_ENV = "CHIEF_INSTALLER_FROZEN_EXECUTABLE"


def _configure_packaged_python() -> None:
    """Use a real host Python for child `python -m ...` operations in frozen builds.

    PyInstaller sets ``sys.executable`` to the packaged Chief Installer binary.
    Reusing that path with ``-m`` causes the installer to invoke itself as though
    it were Python. Preserve the frozen executable path for privileged helper
    re-entry, then point ``sys.executable`` at the host Python interpreter that
    supported Linux installations are required to provide.
    """
    if not getattr(sys, "frozen", False):
        return

    frozen_executable = str(sys.executable)
    os.environ[_FROZEN_EXECUTABLE_ENV] = frozen_executable

    host_python = shutil.which("python3") or shutil.which("python")
    if host_python:
        sys.executable = host_python


def _run_internal_privileged_helper(argv) -> int:
    """Run the strictly constrained Linux helper inside the packaged binary."""
    try:
        from .privileged.linux import main as helper_main
    except ImportError:
        from deployment.installer.privileged.linux import main as helper_main
    return helper_main(["chief-installer-helper", *argv])


def main() -> int:
    # Frozen Linux builds re-enter this executable for privileged helper work.
    # Handle that internal mode before normal CLI dispatch.
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-privileged-helper":
        return _run_internal_privileged_helper(sys.argv[2:])

    _configure_packaged_python()

    # If explicit CLI arguments or help flags were provided, run CLI
    if len(sys.argv) > 1 and not (len(sys.argv) == 2 and sys.argv[1] == "--gui"):
        return run_cli(sys.argv[1:])

    # Check for graphical environment
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.name == "nt")

    if has_display:
        try:
            try:
                from .ui.app import launch_gui
            except ImportError:
                from deployment.installer.ui.app import launch_gui
            return launch_gui()
        except Exception as exc:
            sys.stderr.write(f"GUI launch failed ({exc}), falling back to terminal setup...\n")
            return run_cli([])
    else:
        return run_cli([])


if __name__ == "__main__":
    sys.exit(main())
