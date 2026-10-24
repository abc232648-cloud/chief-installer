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


def _resolve_python_alias(candidate: str) -> str:
    """Avoid app-alias filesystem virtualization for installed AppData files."""
    from pathlib import Path
    if os.name != "nt" or "windowsapps" not in {p.lower() for p in Path(candidate).parts}:
        return candidate
    import json
    from deployment.installer.process import run
    result = run(
        [candidate, "-c", "import json,sys; print(json.dumps(sys.executable))"],
        cwd=os.environ.get("SystemRoot", r"C:\Windows"),
        capture_output=True, text=True, timeout=30, check=True,
    )
    resolved = Path(json.loads(result.stdout.strip()))
    if (not resolved.is_absolute() or not resolved.is_file() or
            "windowsapps" in {p.lower() for p in resolved.parts}):
        raise RuntimeError("Python app alias did not resolve to an installed interpreter")
    return str(resolved)


def _restore_external_loader_environment() -> None:
    """Undo PyInstaller's Linux loader-path override for child processes.

    PyInstaller temporarily prepends its extraction directory to
    ``LD_LIBRARY_PATH`` so the frozen installer can load its bundled native
    libraries. The dynamic loader has already consumed that setting for this
    process by the time Python code runs, but subprocesses inherit the modified
    environment. System Python, the Chief venv, pip, Playwright, git, Node and
    Caddy must resolve against the host/runtime libraries instead of the
    installer's private bundle.

    PyInstaller preserves the pre-launch value as ``LD_LIBRARY_PATH_ORIG`` when
    one existed. Restore that value, otherwise remove the injected path.
    """
    if os.name == "nt":
        return

    if "LD_LIBRARY_PATH_ORIG" in os.environ:
        original = os.environ.get("LD_LIBRARY_PATH_ORIG", "")
        if original:
            os.environ["LD_LIBRARY_PATH"] = original
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


def _configure_packaged_python() -> None:
    """Prepare frozen builds to invoke real host/runtime child processes safely.

    PyInstaller sets ``sys.executable`` to the packaged Chief Installer binary.
    Reusing that path with ``-m`` causes the installer to invoke itself as though
    it were Python. Preserve the frozen executable path for privileged helper
    re-entry, point ``sys.executable`` at the host Python interpreter required by
    supported Linux installations, and remove PyInstaller's private loader path
    from the environment inherited by external child processes.
    """
    if not getattr(sys, "frozen", False):
        return

    frozen_executable = str(sys.executable)
    os.environ[_FROZEN_EXECUTABLE_ENV] = frozen_executable

    host_python = shutil.which("python3") or shutil.which("python")
    if host_python:
        sys.executable = _resolve_python_alias(host_python)

    _restore_external_loader_environment()


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

    if len(sys.argv) > 1 and sys.argv[1] == '--desktop-control':
        from deployment.installer.desktop_runtime import control_main
        try:
            return control_main(sys.argv[2:])
        except Exception:
            import json
            print(json.dumps({'status': 'INCOMPLETE', 'reason': 'Service state could not be verified.'}))
            return 1

    if sys.argv[1:] == ["--release-info"]:
        import json
        from deployment.installer.manifest import DEFAULT_RELEASE_MANIFEST
        print(json.dumps(DEFAULT_RELEASE_MANIFEST.to_dict(), sort_keys=True))
        return 0

    if "--console-smoke-test" in sys.argv:
        try:
            from .console import console_smoke_test
        except ImportError:
            from deployment.installer.console import console_smoke_test
        return console_smoke_test()

    if "--console" in sys.argv:
        try:
            from .console import main as console_main
        except ImportError:
            from deployment.installer.console import main as console_main
        return console_main(sys.argv[1:])

    if sys.argv[1:] == ["--gui-smoke-test"]:
        # Exercise the actual bundled GUI without starting any installation.
        from deployment.installer.ui.app import InstallerApp
        app=InstallerApp()
        app.update_idletasks()
        app.destroy()
        print("Chief installer GUI smoke test passed")
        return 0

    # Check for graphical environment
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.name == "nt")

    # If --uninstall is requested without explicit non-interactive flag in a GUI environment, open GUI uninstaller
    if "--uninstall" in sys.argv and "--non-interactive" not in sys.argv and has_display:
        try:
            try:
                from .ui.app import launch_gui
                from .ui.controller import ScreenId
            except ImportError:
                from deployment.installer.ui.app import launch_gui
                from deployment.installer.ui.controller import ScreenId
            return launch_gui(start_screen=ScreenId.UNINSTALL_OPTIONS)
        except Exception as exc:
            if not sys.stdin.isatty():
                sys.stderr.write(f"ERROR: GUI uninstaller launch failed: {exc}\n")
                return 1
            sys.stderr.write(f"GUI uninstall launch failed ({exc}), falling back to terminal uninstaller...\n")
            return run_cli(sys.argv[1:])

    # If explicit CLI arguments or help flags were provided, run CLI
    if len(sys.argv) > 1 and not (len(sys.argv) == 2 and sys.argv[1] == "--gui"):
        return run_cli(sys.argv[1:])

    if "--gui" in sys.argv:
        if not has_display:
            sys.stderr.write("ERROR: Graphical interface requested (--gui) but no graphical display is available.\n")
            return 1
        try:
            try:
                from .ui.app import launch_gui
            except ImportError:
                from deployment.installer.ui.app import launch_gui
            return launch_gui()
        except Exception as exc:
            sys.stderr.write(f"ERROR: GUI launch failed: {exc}\n")
            return 1

    if has_display:
        try:
            try:
                from .ui.app import launch_gui
            except ImportError:
                from deployment.installer.ui.app import launch_gui
            return launch_gui()
        except Exception as exc:
            if not sys.stdin.isatty():
                sys.stderr.write(f"ERROR: GUI launch failed: {exc}\n")
                return 1
            sys.stderr.write(f"GUI launch failed ({exc}), falling back to terminal setup...\n")
            return run_cli([])
    else:
        if not sys.stdin.isatty():
            sys.stderr.write("ERROR: No graphical display available and standard input is not a terminal.\n")
            return 1
        return run_cli([])


if __name__ == "__main__":
    sys.exit(main())
