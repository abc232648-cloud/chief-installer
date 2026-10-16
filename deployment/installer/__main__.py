"""Main entrypoint for the Chief Unified Installer.

When double-clicked or executed without arguments on a graphical desktop,
launches the graphical installer. When arguments are supplied or running
headless, runs the CLI interface.
"""
import os
import sys

try:
    from .cli import run_cli
except ImportError:
    from deployment.installer.cli import run_cli


def main() -> int:
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
