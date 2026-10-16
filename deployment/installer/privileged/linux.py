"""Constrained privileged helper for Linux operating-system operations.

Invoked via PolicyKit (pkexec) or sudo with strictly whitelisted operations and arguments.
Rejects arbitrary shell strings or unapproved commands.
"""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import List

_ALLOWED_PACKAGES = {
    "git",
    "curl",
    "ca-certificates",
    "python3",
    "python3-venv",
    "python3-pip",
    "xz-utils",
    "libcap2-bin",
    "avahi-daemon",
    "avahi-utils",
    "libnss-mdns",
    "iproute2",
}

_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def run_install_packages(packages: List[str]) -> int:
    """Install explicitly whitelisted system packages."""
    if not packages:
        packages = sorted(_ALLOWED_PACKAGES)

    unauthorized = set(packages) - _ALLOWED_PACKAGES
    if unauthorized:
        sys.stderr.write(f"ERROR: Refusing unauthorized packages: {unauthorized}\n")
        return 2

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"

    # apt update
    sys.stdout.write("Running apt-get update...\n")
    proc_update = subprocess.run(["apt-get", "update"], env=env)
    if proc_update.returncode != 0:
        return proc_update.returncode

    # apt install
    cmd = ["apt-get", "install", "-y", "--no-install-recommends"] + packages
    sys.stdout.write(f"Installing system packages: {' '.join(packages)}...\n")
    proc_install = subprocess.run(cmd, env=env)
    return proc_install.returncode


def run_enable_linger(username: str) -> int:
    """Enable systemd user linger for the specified user."""
    if not _USER_RE.fullmatch(username):
        sys.stderr.write(f"ERROR: Invalid username format: {username!r}\n")
        return 2

    cmd = ["loginctl", "enable-linger", username]
    sys.stdout.write(f"Enabling user linger for {username}...\n")
    proc = subprocess.run(cmd)
    return proc.returncode


def run_configure_avahi(label: str) -> int:
    """Update /etc/avahi/avahi-daemon.conf with host-name and restart avahi-daemon."""
    label = label.lower().strip()
    if not _LABEL_RE.fullmatch(label):
        sys.stderr.write(f"ERROR: Invalid local hostname label: {label!r}\n")
        return 2

    conf_path = Path("/etc/avahi/avahi-daemon.conf")
    existing_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
    lines = existing_text.splitlines(keepends=True)
    out: List[str] = []
    in_server = False
    inserted = False
    seen_server = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_server and not inserted:
                out.append(f"host-name={label}\n")
                inserted = True
            in_server = stripped.lower() == "[server]"
            seen_server = seen_server or in_server
        if in_server and stripped.lower().startswith("host-name="):
            if not inserted:
                out.append(f"host-name={label}\n")
                inserted = True
            continue
        out.append(line)

    if in_server and not inserted:
        out.append(f"host-name={label}\n")
        inserted = True
    if not seen_server:
        out = ["[server]\n", f"host-name={label}\n", "\n"] + out

    payload = "".join(out)
    fd, tmp = tempfile.mkstemp(prefix=".avahi-daemon.conf.", dir=str(conf_path.parent if conf_path.parent.exists() else "/tmp"))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        if conf_path.exists():
            os.chmod(tmp, conf_path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, conf_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # Enable and restart service
    subprocess.run(["systemctl", "enable", "avahi-daemon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc_restart = subprocess.run(["systemctl", "restart", "avahi-daemon"])
    return proc_restart.returncode


def run_set_caddy_capability(executable_path: str) -> int:
    """Grant cap_net_bind_service=+ep to the Caddy binary."""
    p = Path(executable_path).resolve()
    if not p.is_file():
        sys.stderr.write(f"ERROR: Caddy executable not found: {p}\n")
        return 2
    if p.name != "caddy":
        sys.stderr.write(f"ERROR: Refusing to set capability on non-caddy binary: {p.name}\n")
        return 2

    cmd = ["setcap", "cap_net_bind_service=+ep", str(p)]
    sys.stdout.write(f"Setting cap_net_bind_service on {p}...\n")
    proc = subprocess.run(cmd)
    return proc.returncode


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("Usage: chief-installer-helper <command> [<args>...]\n")
        sys.stderr.write("Allowed commands: install-packages, enable-linger, configure-avahi, set-caddy-capability\n")
        return 2

    command = argv[1]
    args = argv[2:]

    if command == "install-packages":
        return run_install_packages(args)
    elif command == "enable-linger":
        if len(args) != 1:
            sys.stderr.write("Usage: chief-installer-helper enable-linger <username>\n")
            return 2
        return run_enable_linger(args[0])
    elif command == "configure-avahi":
        if len(args) != 1:
            sys.stderr.write("Usage: chief-installer-helper configure-avahi <hostname_label>\n")
            return 2
        return run_configure_avahi(args[0])
    elif command == "set-caddy-capability":
        if len(args) != 1:
            sys.stderr.write("Usage: chief-installer-helper set-caddy-capability <executable_path>\n")
            return 2
        return run_set_caddy_capability(args[0])
    else:
        sys.stderr.write(f"ERROR: Unknown or forbidden privileged command: {command!r}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
