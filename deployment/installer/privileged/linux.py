"""Constrained privileged helper for Linux operating-system operations.

Invoked via PolicyKit (pkexec) or sudo with strictly whitelisted operations and arguments.
Rejects arbitrary shell strings or unapproved commands.
"""
import os
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import List, Optional

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
    "wireguard",
    "wireguard-tools",
    "iptables",
    "chromium",
    "chromium-browser",
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


def _write_avahi_state(state_path: Path, state: dict) -> None:
    """Atomically persist the small ownership record used by setup/uninstall."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_state = tempfile.mkstemp(prefix=".chief-avahi-state.", dir=str(state_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
            handle.write("\n")
        os.chmod(tmp_state, 0o600)
        os.replace(tmp_state, state_path)
    finally:
        if os.path.exists(tmp_state):
            os.unlink(tmp_state)


def _update_hosts_file(hosts_path: Path, canonical_host: str) -> bool:
    """Atomically ensure 127.0.0.1 is mapped to canonical_host in /etc/hosts."""
    try:
        hosts_text = hosts_path.read_text(encoding="utf-8") if hosts_path.exists() else ""
        lines = hosts_text.splitlines()
        found = False
        new_lines: List[str] = []
        for l in lines:
            parts = l.partition("#")[0].strip().split()
            if len(parts) >= 2 and parts[0] == "127.0.0.1":
                if canonical_host.lower() in [alias.lower() for alias in parts[1:] if alias != "#"]:
                    found = True
                    new_lines.append(l)
                else:
                    # Preserve every other loopback alias. Another Chief
                    # installation (or a user's own service) may own it.
                    new_lines.append(l)
            else:
                new_lines.append(l)

        if found:
            return False

        new_lines.append(f"127.0.0.1\t{canonical_host}\t# Chief on-farm console local host")

        payload = "\n".join(new_lines) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".hosts.", dir=str(hosts_path.parent if hosts_path.parent.exists() else "/tmp"))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            if hosts_path.exists():
                os.chmod(tmp, hosts_path.stat().st_mode & 0o777)
            else:
                os.chmod(tmp, 0o644)
            os.replace(tmp, hosts_path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception as exc:
        sys.stderr.write(f"ERROR: Could not update hosts file {hosts_path}: {exc}\n")
        raise OSError(f"Failed to update hosts file {hosts_path}: {exc}") from exc
    return True


def _update_nsswitch_file(nss_path: Path) -> None:
    """Ensure /etc/nsswitch.conf routes .local lookups through libnss-mdns."""
    if not nss_path.exists():
        return
    try:
        nss_text = nss_path.read_text(encoding="utf-8")
        if "hosts:" not in nss_text:
            return
        lines = nss_text.splitlines(keepends=True)
        updated = False
        new_lines: List[str] = []
        for l in lines:
            stripped = l.strip()
            if stripped.startswith("hosts:") and "mdns" not in stripped:
                if "dns" in stripped:
                    new_lines.append(l.replace("dns", "mdns4_minimal [NOTFOUND=return] dns"))
                else:
                    new_lines.append(l.rstrip() + " mdns4_minimal [NOTFOUND=return]\n")
                updated = True
            else:
                new_lines.append(l)

        if updated:
            fd, tmp = tempfile.mkstemp(prefix=".nsswitch.", dir=str(nss_path.parent if nss_path.parent.exists() else "/tmp"))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("".join(new_lines))
                os.chmod(tmp, nss_path.stat().st_mode & 0o777)
                os.replace(tmp, nss_path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
    except Exception as exc:
        sys.stderr.write(f"WARNING: Could not update nsswitch file {nss_path}: {exc}\n")


def run_configure_avahi(
    label: str,
    *,
    conf_path_override: Optional[Path] = None,
    hosts_path_override: Optional[Path] = None,
    nsswitch_path_override: Optional[Path] = None,
    state_path_override: Optional[Path] = None,
) -> int:
    """Configure the Chief mDNS hostname while recording any prior host-name value."""
    label = label.lower().strip()
    if not _LABEL_RE.fullmatch(label):
        sys.stderr.write(f"ERROR: Invalid local hostname label: {label!r}\n")
        return 2

    conf_path = conf_path_override or Path("/etc/avahi/avahi-daemon.conf")
    state_path = state_path_override or conf_path.with_name(conf_path.name + ".chief-state")
    existing_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
    lines = existing_text.splitlines(keepends=True)
    server_host_indexes = []
    in_server = False
    server_section_exists = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_server = stripped.lower() == "[server]"
            server_section_exists = server_section_exists or in_server
        elif in_server and stripped.lower().startswith("host-name="):
            server_host_indexes.append(idx)

    current_values = [lines[idx].strip().split("=", 1)[1].strip().lower() for idx in server_host_indexes]
    state = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            sys.stderr.write(f"ERROR: Existing Chief Avahi state is unreadable: {exc}\n")
            return 1
        if state.get("label") != label:
            sys.stderr.write("ERROR: Existing Chief Avahi state belongs to a different hostname. Clean it up before reconfiguring.\n")
            return 1
        if state.get("hostname_owned", True) and current_values != [label]:
            sys.stderr.write("ERROR: Avahi hostname changed after Chief setup; refusing to overwrite the current configuration.\n")
            return 1

    # If the desired hostname was already present and there is no Chief state,
    # it predates this installation. Leave it unowned so uninstall preserves it.
    needs_config_change = not current_values or any(value != label for value in current_values)
    if state is None:
        state = {
            "version": 1,
            "label": label,
            "original_file_existed": conf_path.exists(),
            "original_host_name_lines": [lines[idx] for idx in server_host_indexes],
            "server_section_added": not server_section_exists,
            "hostname_owned": needs_config_change,
            "hosts_entry_added": False,
        }
        if needs_config_change:
            try:
                _write_avahi_state(state_path, state)
            except Exception as exc:
                sys.stderr.write(f"ERROR: Could not record original Avahi hostname setting: {exc}\n")
                return 1
    elif state.get("hostname_owned", True) and current_values != [label]:
        sys.stderr.write("ERROR: Avahi hostname changed after Chief setup; refusing to overwrite the current configuration.\n")
        return 1

    # Do not rewrite an already configured, pre-existing hostname: it is not
    # owned by this installation and must survive uninstall unchanged.
    if state.get("hostname_owned", False):
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

    # Update loopback hosts mapping so local getent / socket resolution works without relying on mDNS multicast
    hosts_path = hosts_path_override or Path("/etc/hosts")
    try:
        hosts_added = _update_hosts_file(hosts_path, f"{label}.local")
    except Exception as exc:
        sys.stderr.write(f"ERROR: Failed to update hosts file {hosts_path}: {exc}\n")
        return 1

    state["hosts_entry_added"] = bool(state.get("hosts_entry_added")) or hosts_added
    try:
        if state.get("hostname_owned") or state.get("hosts_entry_added"):
            _write_avahi_state(state_path, state)
        elif state_path.exists():
            state_path.unlink()
    except Exception as exc:
        sys.stderr.write(f"ERROR: Could not record Chief local hostname ownership: {exc}\n")
        return 1

    # Update nsswitch.conf so libnss-mdns is active if installed
    nss_path = nsswitch_path_override or Path("/etc/nsswitch.conf")
    _update_nsswitch_file(nss_path)

    # Enable and restart service, propagating both failures.
    proc_enable = subprocess.run(["systemctl", "enable", "avahi-daemon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc_enable.returncode != 0:
        sys.stderr.write(f"ERROR: Failed to enable avahi-daemon service (exit code {proc_enable.returncode})\n")
        return proc_enable.returncode
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


def run_install_wireguard_config(source_path_str: str) -> int:
    """Privileged atomic write of wg0.conf to /etc/wireguard/wg0.conf with 0600 permissions."""
    import shutil
    source_p = Path(source_path_str).resolve()
    if not source_p.is_file():
        sys.stderr.write(f"ERROR: WireGuard source config not found: {source_p}\n")
        return 2

    try:
        conf_text = source_p.read_text(encoding="utf-8")
        if "[Interface]" not in conf_text or "[Peer]" not in conf_text:
            sys.stderr.write("ERROR: Invalid WireGuard config format: missing [Interface] or [Peer]\n")
            return 2
    except Exception as exc:
        sys.stderr.write(f"ERROR: Failed reading source config: {exc}\n")
        return 2

    dest_dir = Path("/etc/wireguard")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except Exception as exc:
        sys.stderr.write(f"ERROR: Failed creating /etc/wireguard directory: {exc}\n")
        return 2

    dest_file = dest_dir / "wg0.conf"
    fd, tmp = tempfile.mkstemp(prefix=".wg0.conf.", dir=str(dest_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(conf_text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest_file)
        try:
            source_p.unlink()
        except OSError:
            pass
        sys.stdout.write(f"WireGuard server config installed to {dest_file} (mode 0600).\n")
        return 0
    except Exception as exc:
        sys.stderr.write(f"ERROR: Failed writing /etc/wireguard/wg0.conf: {exc}\n")
        return 2
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def run_control_wireguard_service(action: str) -> int:
    """Control wg-quick@wg0 systemd service with argument validation."""
    if action not in {"start", "stop", "restart", "enable", "disable", "status", "is-active"}:
        sys.stderr.write(f"ERROR: Invalid WireGuard service action: {action!r}\n")
        return 2
    cmd = ["systemctl", action, "wg-quick@wg0"]
    sys.stdout.write(f"Executing systemctl {action} wg-quick@wg0...\n")
    proc = subprocess.run(cmd)
    return proc.returncode


def run_apply_wireguard_firewall(port_str: str) -> int:
    """Apply fail-closed dedicated WireGuard firewall chains via iptables."""
    import shutil
    try:
        port = int(port_str)
        if not (1 <= port <= 65535):
            raise ValueError()
    except ValueError:
        sys.stderr.write(f"ERROR: Invalid port: {port_str!r}\n")
        return 2

    iptables = shutil.which("iptables")
    if not iptables:
        sys.stderr.write("ERROR: iptables binary not found\n")
        return 2

    def _exec(cmd: List[str]) -> bool:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        return res.returncode == 0

    # Dedicated isolated chains
    _exec([iptables, "-N", "CHIEF-WG-IN"])
    _exec([iptables, "-F", "CHIEF-WG-IN"])
    _exec([iptables, "-N", "CHIEF-WG-FWD"])
    _exec([iptables, "-F", "CHIEF-WG-FWD"])

    # Insert jumps from INPUT and FORWARD
    if not _exec([iptables, "-C", "INPUT", "-j", "CHIEF-WG-IN"]):
        if not _exec([iptables, "-I", "INPUT", "1", "-j", "CHIEF-WG-IN"]):
            run_rollback_wireguard_firewall()
            return 1
    if not _exec([iptables, "-C", "FORWARD", "-j", "CHIEF-WG-FWD"]):
        if not _exec([iptables, "-I", "FORWARD", "1", "-j", "CHIEF-WG-FWD"]):
            run_rollback_wireguard_firewall()
            return 1

    # Rules
    rules = [
        [iptables, "-A", "CHIEF-WG-IN", "-p", "udp", "--dport", str(port), "-j", "ACCEPT"],
        [iptables, "-A", "CHIEF-WG-IN", "-i", "wg0", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"],
        [iptables, "-A", "CHIEF-WG-IN", "-i", "wg0", "-p", "udp", "--dport", "53", "-j", "ACCEPT"],
        [iptables, "-A", "CHIEF-WG-IN", "-i", "wg0", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        [iptables, "-A", "CHIEF-WG-IN", "-i", "wg0", "-j", "DROP"],
        [iptables, "-A", "CHIEF-WG-FWD", "-i", "wg0", "-d", "192.168.0.0/16", "-j", "DROP"],
        [iptables, "-A", "CHIEF-WG-FWD", "-i", "wg0", "-d", "10.0.0.0/8", "-j", "DROP"],
        [iptables, "-A", "CHIEF-WG-FWD", "-i", "wg0", "-d", "172.16.0.0/12", "-j", "DROP"],
        [iptables, "-A", "CHIEF-WG-FWD", "-i", "wg0", "-j", "DROP"],
    ]
    for r in rules:
        if not _exec(r):
            sys.stderr.write(f"ERROR: Failed applying firewall rule: {' '.join(r)}\n")
            run_rollback_wireguard_firewall()
            return 1

    sys.stdout.write("Dedicated WireGuard firewall chains applied successfully.\n")
    return 0


def run_rollback_wireguard_firewall() -> int:
    """Remove dedicated WireGuard firewall chains without touching other host rules."""
    import shutil
    iptables = shutil.which("iptables")
    if not iptables:
        return 0

    def _exec(cmd: List[str]) -> None:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    _exec([iptables, "-D", "INPUT", "-j", "CHIEF-WG-IN"])
    _exec([iptables, "-D", "FORWARD", "-j", "CHIEF-WG-FWD"])
    _exec([iptables, "-F", "CHIEF-WG-IN"])
    _exec([iptables, "-X", "CHIEF-WG-IN"])
    _exec([iptables, "-F", "CHIEF-WG-FWD"])
    _exec([iptables, "-X", "CHIEF-WG-FWD"])
    sys.stdout.write("Dedicated WireGuard firewall chains rolled back successfully.\n")
    return 0




def run_cleanup_local_discovery(
    canonical_host: str,
    *,
    hosts_path_override: Optional[Path] = None,
    conf_path_override: Optional[Path] = None,
    avahi_service_override: Optional[Path] = None,
    state_path_override: Optional[Path] = None,
) -> int:
    """Undo only local discovery changes recorded as owned by this Chief install."""
    canonical_host = canonical_host.strip()
    if not re.match(r"^[a-zA-Z0-9.-]+$", canonical_host):
        sys.stderr.write(f"ERROR: Invalid canonical hostname format: {canonical_host!r}\n")
        return 2

    label = canonical_host[:-6].lower() if canonical_host.lower().endswith(".local") else canonical_host.lower()
    hosts_path = hosts_path_override or Path("/etc/hosts")
    conf_path = conf_path_override or Path("/etc/avahi/avahi-daemon.conf")
    state_path = state_path_override or conf_path.with_name(conf_path.name + ".chief-state")

    state = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("label") != label:
                raise ValueError("Chief Avahi state belongs to a different hostname")
        except Exception as exc:
            sys.stderr.write(f"ERROR: Could not read Chief local discovery ownership state: {exc}\n")
            return 1

    hostname_owned = bool(state and state.get("hostname_owned", True))
    config_changed = False
    if not hostname_owned and state is None and conf_path.exists():
        # Older installs did not record Avahi ownership. Preserve a matching
        # global setting and fail visibly rather than deleting a user's value.
        try:
            in_server = False
            for line in conf_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_server = stripped.lower() == "[server]"
                elif in_server and stripped.lower().startswith("host-name=") and stripped.split("=", 1)[1].strip().lower() == label:
                    raise ValueError("Avahi hostname ownership is unknown; preserving the matching configuration")
        except ValueError as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"ERROR: Could not inspect Avahi configuration: {exc}\n")
            return 1

    if hostname_owned:
        try:
            if not conf_path.exists():
                raise FileNotFoundError(f"Avahi configuration disappeared before cleanup: {conf_path}")
            existing_text = conf_path.read_text(encoding="utf-8")
            lines = existing_text.splitlines(keepends=True)
            current_values = []
            in_server = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_server = stripped.lower() == "[server]"
                elif in_server and stripped.lower().startswith("host-name="):
                    current_values.append(stripped.split("=", 1)[1].strip().lower())

            original_lines = state.get("original_host_name_lines", [])
            if current_values == [label]:
                restore = True
            elif current_values == [line.strip().split("=", 1)[1].strip().lower() for line in original_lines]:
                # A previous uninstall attempt restored the config but failed
                # later; retain idempotency so it can safely finish.
                restore = False
            else:
                raise ValueError("Chief-managed Avahi hostname setting was changed; refusing to overwrite the current configuration")

            if restore:
                out: List[str] = []
                in_server = False
                restored = False

                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("[") and stripped.endswith("]"):
                        in_server = (stripped.lower() == "[server]")
                        out.append(line)
                        continue

                    if in_server and stripped.lower().startswith("host-name=") and stripped.split("=", 1)[1].strip().lower() == label:
                        if restored:
                            raise ValueError("Multiple matching Chief Avahi host-name settings found")
                        out.extend(original_lines)
                        restored = True
                        continue
                    out.append(line)

                if not restored:
                    raise ValueError("Chief-managed Avahi hostname setting was changed or removed")
                # If setup had to create [server], remove that section only
                # when it still contains no user-added directives or comments.
                if state.get("server_section_added", False):
                    cleaned: List[str] = []
                    in_added_server = False
                    candidate: List[str] = []
                    for line in out:
                        stripped = line.strip()
                        if stripped.startswith("[") and stripped.endswith("]"):
                            if in_added_server:
                                if all(not item.strip() for item in candidate):
                                    cleaned.extend([])
                                else:
                                    cleaned.extend(candidate)
                                candidate = []
                            in_added_server = stripped.lower() == "[server]"
                            if not in_added_server:
                                cleaned.append(line)
                            continue
                        if in_added_server:
                            candidate.append(line)
                        else:
                            cleaned.append(line)
                    if in_added_server:
                        if any(item.strip() for item in candidate):
                            cleaned.extend(candidate)
                    out = cleaned
                payload = "".join(out)
                if not state.get("original_file_existed", True) and not payload.strip():
                    conf_path.unlink()
                else:
                    fd, tmp = tempfile.mkstemp(prefix=".avahi-daemon.conf.", dir=str(conf_path.parent if conf_path.parent.exists() else "/tmp"))
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as handle:
                            handle.write(payload)
                        os.chmod(tmp, conf_path.stat().st_mode & 0o777)
                        os.replace(tmp, conf_path)
                    finally:
                        if os.path.exists(tmp):
                            try:
                                os.unlink(tmp)
                            except OSError:
                                pass
                config_changed = True
        except Exception as exc:
            sys.stderr.write(f"ERROR: Could not restore /etc/avahi/avahi-daemon.conf during cleanup: {exc}\n")
            return 1

    # Remove only an entry added by this installation, or a legacy entry bearing
    # the exact installer comment. Other loopback aliases, including the same
    # hostname if pre-existing on another address, remain untouched.
    hosts_added = bool(state and state.get("hosts_entry_added"))
    if hosts_path.exists():
        try:
            lines = hosts_path.read_text(encoding="utf-8").splitlines()
            out: List[str] = []
            removed_owned_alias = False
            for line in lines:
                body, marker, comment = line.partition("#")
                parts = body.split()
                is_loopback = len(parts) >= 2 and parts[0] in ("127.0.0.1", "::1")
                has_canonical = is_loopback and canonical_host.lower() in [alias.lower() for alias in parts[1:]]
                legacy_owned = bool(marker and comment.strip() == "Chief on-farm console local host")
                may_remove = has_canonical and parts[0] == "127.0.0.1" and (hosts_added or (state is None and legacy_owned))
                if may_remove:
                    aliases = [alias for alias in parts[1:] if alias.lower() != canonical_host.lower()]
                    if aliases:
                        out.append(f"{parts[0]}\t{' '.join(aliases)}")
                    continue
                out.append(line)

            if out != lines:
                payload = "\n".join(out) + "\n"
                fd, tmp = tempfile.mkstemp(prefix=".hosts.", dir=str(hosts_path.parent if hosts_path.parent.exists() else "/tmp"))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(payload)
                    os.chmod(tmp, hosts_path.stat().st_mode & 0o777)
                    os.replace(tmp, hosts_path)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
        except Exception as exc:
            sys.stderr.write(f"ERROR: Could not update /etc/hosts during cleanup: {exc}\n")
            return 1

    # Do not remove /etc/avahi/services/chief.service: the installer does not
    # create or track that file, so its ownership cannot be established.
    # Refresh Avahi after restoration and propagate any failure.
    import shutil
    if shutil.which("systemctl"):
        proc = subprocess.run(["systemctl", "try-restart", "avahi-daemon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc.returncode != 0:
            sys.stderr.write(f"ERROR: Failed to restart avahi-daemon service (exit code {proc.returncode})\n")
            return 1
    elif config_changed:
        sys.stderr.write("ERROR: systemctl is unavailable; Avahi configuration was restored but could not be refreshed.\n")
        return 1

    if state_path.exists():
        try:
            state_path.unlink()
        except Exception as exc:
            sys.stderr.write(f"ERROR: Could not remove Chief local discovery state: {exc}\n")
            return 1

    sys.stdout.write(f"Local discovery cleaned up for {canonical_host}\n")
    return 0


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("Usage: chief-installer-helper <command> [<args>...]\n")
        sys.stderr.write("Allowed commands: install-packages, enable-linger, configure-avahi, cleanup-local-discovery, set-caddy-capability, install-wireguard-config, control-wireguard-service, apply-wireguard-firewall, rollback-wireguard-firewall\n")
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
    elif command == "cleanup-local-discovery":
        if len(args) != 1:
            sys.stderr.write("Usage: chief-installer-helper cleanup-local-discovery <canonical_host>\n")
            return 2
        return run_cleanup_local_discovery(args[0])
    elif command == "set-caddy-capability":
        if len(args) != 1:
            sys.stderr.write("Usage: chief-installer-helper set-caddy-capability <executable_path>\n")
            return 2
        return run_set_caddy_capability(args[0])
    elif command == "install-wireguard-config":
        if len(args) != 1:
            sys.stderr.write("Usage: chief-installer-helper install-wireguard-config <source_file>\n")
            return 2
        return run_install_wireguard_config(args[0])
    elif command == "control-wireguard-service":
        if len(args) != 1:
            sys.stderr.write("Usage: chief-installer-helper control-wireguard-service <action>\n")
            return 2
        return run_control_wireguard_service(args[0])
    elif command == "apply-wireguard-firewall":
        if len(args) != 1:
            sys.stderr.write("Usage: chief-installer-helper apply-wireguard-firewall <port>\n")
            return 2
        return run_apply_wireguard_firewall(args[0])
    elif command == "rollback-wireguard-firewall":
        return run_rollback_wireguard_firewall()
    else:
        sys.stderr.write(f"ERROR: Unknown or forbidden privileged command: {command!r}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
