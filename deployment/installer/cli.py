"""Command-line interface for the Chief Unified Installer.

Shares the exact same orchestrator, state machine, and contracts as the GUI.
"""
import argparse
import getpass
import os
from pathlib import Path
import platform
import sys
from typing import List, Optional

from .errors import InstallerError
from .events import EventBus, InstallEvent
from .logging import InstallerLogger
from .manifest import ACCEPTED_FIRST_RELEASE_MANIFEST, parse_manifest
from .models import InstallOptions, StepStatus
from .orchestrator import InstallerOrchestrator, InstallerPaths
from .platforms.base import PlatformAdapter
from .platforms.linux import LinuxPlatformAdapter


def get_default_platform_adapter() -> PlatformAdapter:
    """Return appropriate platform adapter for current OS."""
    system = platform.system()
    if system == "Linux":
        return LinuxPlatformAdapter()
    else:
        # Fallback or future platform adapter (e.g. Windows/macOS)
        return LinuxPlatformAdapter()


def print_cli_progress(event: InstallEvent) -> None:
    """Render structured event progress to terminal."""
    if event.status == StepStatus.RUNNING:
        sys.stdout.write(f"\r→ {event.display_name}... {event.message[:50]}")
        sys.stdout.flush()
    elif event.status == StepStatus.SUCCESS:
        sys.stdout.write(f"\r✓ {event.display_name}\n")
        sys.stdout.flush()
    elif event.status == StepStatus.SKIPPED:
        sys.stdout.write(f"\r- {event.display_name} (skipped: {event.message})\n")
        sys.stdout.flush()
    elif event.status == StepStatus.FAILED:
        sys.stdout.write(f"\r✗ {event.display_name} (FAILED: {event.message})\n")
        sys.stdout.flush()


def run_cli(argv: Optional[List[str]] = None, platform_adapter: Optional[PlatformAdapter] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chief-installer",
        description="Chief Unified Installer — installs Chief and built PWAs with local discovery and TLS.",
    )
    parser.add_argument("--owner-name", help="Name of the person or organisation Chief belongs to.")
    parser.add_argument("--username", default="owner", help="Owner username (default: owner).")
    parser.add_argument("--password", help=argparse.SUPPRESS)  # Allowed via CLI for automation, suppressed from help
    parser.add_argument("--farm-operations", choices=["DISABLED", "ENABLED"], default=None, help="Enable farm operations.")
    parser.add_argument("--organization-name", help="Organisation name for initial farm.")
    parser.add_argument("--farm-name", help="First farm name.")
    parser.add_argument("--public-host", help="Advanced override for external DNS hostname.")
    parser.add_argument("--tls-mode", choices=["LOCAL", "PUBLIC"], default="LOCAL", help="TLS mode.")
    parser.add_argument("--manifest", help="Path to release manifest JSON.")
    parser.add_argument("--non-interactive", action="store_true", help="Fail if required fields are missing instead of prompting.")

    parser.add_argument('--dns-mode', choices=['quad9', 'system'], default='quad9',
                        help='Chief DNS: Quad9 Secure DoH (default) or System DNS. Host DNS is unchanged.')
    args = parser.parse_args(argv)

    # Resolve platform
    adapter = platform_adapter or get_default_platform_adapter()

    # Resolve manifest
    if args.manifest:
        manifest = parse_manifest(Path(args.manifest))
    else:
        manifest = ACCEPTED_FIRST_RELEASE_MANIFEST

    # Collect owner name
    owner_name = args.owner_name
    if not owner_name:
        if args.non_interactive:
            sys.stderr.write("ERROR: --owner-name is required in non-interactive mode.\n")
            return 1
        while True:
            try:
                entered = input("Who is this Chief for? (your name or organisation name): ").strip()
            except (KeyboardInterrupt, EOFError):
                sys.stdout.write("\nCancelled.\n")
                return 130
            if entered:
                owner_name = entered
                break
            print("Enter the person or organisation Chief belongs to.")

    # Collect password
    password = args.password
    if not password:
        if args.non_interactive:
            sys.stderr.write("ERROR: Password required.\n")
            return 1
        while True:
            try:
                p1 = getpass.getpass("Owner password: ")
                p2 = getpass.getpass("Confirm password: ")
            except (KeyboardInterrupt, EOFError):
                sys.stdout.write("\nCancelled.\n")
                return 130
            if not p1:
                print("Password cannot be empty.")
                continue
            if p1 != p2:
                print("Passwords do not match. Try again.")
                continue
            password = p1
            break

    # Collect farm operations
    farm_ops = args.farm_operations
    org_name = args.organization_name
    farm_name = args.farm_name

    if farm_ops is None:
        if args.non_interactive:
            farm_ops = "DISABLED"
        else:
            try:
                resp = input("Enable production Farm operations now? [y/N]: ").strip().lower()
                farm_ops = "ENABLED" if resp in ("y", "yes") else "DISABLED"
            except (KeyboardInterrupt, EOFError):
                sys.stdout.write("\nCancelled.\n")
                return 130

    if farm_ops == "ENABLED":
        if not org_name:
            if args.non_interactive:
                org_name = owner_name
            else:
                try:
                    resp = input(f"Farm-owning organisation/company [{owner_name}]: ").strip()
                    org_name = resp if resp else owner_name
                except (KeyboardInterrupt, EOFError):
                    sys.stdout.write("\nCancelled.\n")
                    return 130
        if not farm_name:
            if args.non_interactive:
                sys.stderr.write("ERROR: --farm-name required when farm operations are enabled.\n")
                return 1
            while True:
                try:
                    entered = input("First farm name: ").strip()
                except (KeyboardInterrupt, EOFError):
                    sys.stdout.write("\nCancelled.\n")
                    return 130
                if entered:
                    farm_name = entered
                    break
                print("Enter the farm name Chief should use.")

    options = InstallOptions(
        owner_name=owner_name,
        owner_username=args.username,
        owner_password=password,
        farm_operations=farm_ops,
        organization_name=org_name,
        farm_name=farm_name,
        public_host=args.public_host,
        tls_mode=args.tls_mode,
        dns_mode=args.dns_mode,
    )

    event_bus = EventBus()
    event_bus.subscribe(print_cli_progress)

    logger = InstallerLogger()
    orchestrator = InstallerOrchestrator(
        platform=adapter,
        options=options,
        manifest=manifest,
        event_bus=event_bus,
        logger=logger,
    )

    print(f"\nChief installation — Release {manifest.release_id}")
    print("=" * 60)

    result = orchestrator.execute_all()

    if result.status == "SUCCESS":
        print("\n" + "=" * 60)
        print("INSTALLATION COMPLETE")
        print("=" * 60)
        print(f"Chief Owner: {options.owner_name}")
        print(f"Owner PWA:   {result.urls.get('owner')}")
        print(f"Staff PWA:   {result.urls.get('staff')}")
        print(f"Job PWA:     {result.urls.get('jobs')}")
        if result.archive_path:
            print(f"Archive:     {result.archive_path}")
        print(f"Log:         {result.log_path}")
        return 0
    elif result.status == "CANCELLED":
        print("\nInstallation cancelled.")
        return 130
    else:
        print("\n" + "!" * 60)
        print(f"INSTALLATION FAILED: {result.error}")
        print(f"Error Code: {result.error_code}")
        print(f"Diagnostic log: {result.log_path}")
        print("!" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
