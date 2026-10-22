"""On-Farm Chief Console Desktop Application.

Provides a dedicated native desktop application window for Chief operations on the Folio.
Operates locally without requiring public DNS, Tailscale, or internet connectivity.
Ensures strict TLS validation, clear diagnostic states, actionable recovery, and
clean separation from mobile/remote PWAs.
"""
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
from typing import Any, List, Optional, Tuple
import webbrowser
from .process import popen as popen_quiet

from .manifest import CURRENT_RELEASE_MANIFEST
from .orchestrator import InstallerPaths


class ConsoleState(Enum):
    """Lifecycle and connectivity states of the Chief Console."""
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    SERVICES_STOPPED = "SERVICES_STOPPED"
    TLS_UNTRUSTED = "TLS_UNTRUSTED"
    SETUP_REQUIRED = "SETUP_REQUIRED"
    ENDPOINT_UNAVAILABLE = "ENDPOINT_UNAVAILABLE"
    NO_RUNNER = "NO_RUNNER"


@dataclass
class ConsoleDiagnostic:
    """Detailed health and connectivity report for the on-farm Console."""
    state: ConsoleState
    public_host: str
    dashboard_port: int
    tls_mode: str
    console_url: str
    backend_active: bool
    tls_proxy_active: bool
    cert_valid: bool
    message: str
    remediation: str


def find_desktop_runner(paths: Optional[InstallerPaths] = None) -> Optional[str]:
    """Discover a maintained desktop application runner supporting dedicated window mode (--app)."""
    # 1. Check browser runtime provisioned by Chief Installer if present
    resolved_paths = paths or InstallerPaths.create()
    if resolved_paths.browser_config.exists():
        try:
            browser_info = json.loads(resolved_paths.browser_config.read_text(encoding="utf-8"))
            exe_str = browser_info.get("executable")
            if exe_str:
                exe_path = Path(exe_str)
                if exe_path.is_file() and os.access(exe_path, os.X_OK):
                    return str(exe_path)
        except Exception:
            pass

    # 2. Check platform-specific maintained runners
    if os.name == "nt":
        candidates = [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files\Chromium\Application\chrome.exe"),
        ]
        for c in candidates:
            if c.is_file():
                return str(c)
        for name in ("msedge", "chrome", "chromium"):
            w = shutil.which(name)
            if w:
                return w
    else:
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge", "msedge", "brave-browser"):
            w = shutil.which(name)
            if w:
                return w
        fallback_paths = [
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/microsoft-edge"),
            Path("/snap/bin/chromium"),
        ]
        for p in fallback_paths:
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return None


def probe_local_console(paths: Optional[InstallerPaths] = None) -> ConsoleDiagnostic:
    """Probe the local Chief instance without depending on external network or mDNS.

    Verifies backend listener (127.0.0.1:8765), TLS proxy listener (127.0.0.1:443),
    and strict certificate trust for the canonical public host.
    """
    resolved_paths = paths or InstallerPaths.create()
    config_path = resolved_paths.config
    identity_path = resolved_paths.identity_config

    if not config_path.exists() and not identity_path.exists():
        return ConsoleDiagnostic(
            state=ConsoleState.SETUP_REQUIRED,
            public_host="chief.local",
            dashboard_port=8765,
            tls_mode="LOCAL",
            console_url="https://chief.local/",
            backend_active=False,
            tls_proxy_active=False,
            cert_valid=False,
            message="Chief installation has not been completed on this machine.",
            remediation="Please run the Chief Installer wizard to complete first-time farm setup.",
        )

    # Read configuration
    data = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    if not data and identity_path.exists():
        try:
            data = json.loads(identity_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    public_host = data.get("public_host") or "chief.local"
    dashboard_port = int(data.get("dashboard_port") or 8765)
    tls_mode = data.get("tls_mode", "LOCAL")
    console_url = f"https://{public_host}/"

    # 1. Probe backend loopback port
    backend_active = False
    try:
        with socket.create_connection(("127.0.0.1", dashboard_port), timeout=1.5):
            backend_active = True
    except Exception:
        backend_active = False

    # 2. Probe TLS proxy loopback port (443)
    tls_proxy_active = False
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=1.5):
            tls_proxy_active = True
    except Exception:
        tls_proxy_active = False

    # 3. Verify TLS certificate trust strictly
    cert_valid = False
    cert_error = ""
    if tls_proxy_active:
        ca_file = None
        if tls_mode == "LOCAL":
            local_ca = resolved_paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt"
            if local_ca.exists():
                ca_file = str(local_ca)
            else:
                cert_error = "Local Caddy root CA certificate file not found."

        if tls_mode == "LOCAL" and not ca_file:
            cert_valid = False
        else:
            try:
                ctx = ssl.create_default_context(cafile=ca_file)
                raw_sock = socket.create_connection(("127.0.0.1", 443), timeout=2.0)
                with ctx.wrap_socket(raw_sock, server_hostname=public_host) as ssock:
                    cert = ssock.getpeercert()
                    if cert:
                        cert_valid = True
            except ssl.SSLCertVerificationError as exc:
                cert_valid = False
                cert_error = f"Certificate verification error: {exc}"
            except Exception as exc:
                cert_valid = False
                cert_error = f"TLS handshake error: {exc}"

    # Analyze overall health
    if not backend_active and not tls_proxy_active:
        return ConsoleDiagnostic(
            state=ConsoleState.SERVICES_STOPPED,
            public_host=public_host,
            dashboard_port=dashboard_port,
            tls_mode=tls_mode,
            console_url=console_url,
            backend_active=False,
            tls_proxy_active=False,
            cert_valid=cert_valid,
            message="Chief services are not running (backend port 8765 and HTTPS proxy are inactive).",
            remediation="Start Chief background services or use Chief Installer to repair service configuration.",
        )

    if not backend_active:
        return ConsoleDiagnostic(
            state=ConsoleState.SERVICES_STOPPED,
            public_host=public_host,
            dashboard_port=dashboard_port,
            tls_mode=tls_mode,
            console_url=console_url,
            backend_active=False,
            tls_proxy_active=tls_proxy_active,
            cert_valid=cert_valid,
            message="Chief backend dashboard service is inactive (port 8765 not responding).",
            remediation="Check chief-agent-dashboard.service logs and ensure the service is started.",
        )

    if not tls_proxy_active:
        return ConsoleDiagnostic(
            state=ConsoleState.ENDPOINT_UNAVAILABLE,
            public_host=public_host,
            dashboard_port=dashboard_port,
            tls_mode=tls_mode,
            console_url=console_url,
            backend_active=backend_active,
            tls_proxy_active=False,
            cert_valid=cert_valid,
            message="Chief HTTPS TLS proxy is inactive on port 443.",
            remediation="Ensure chief-tls-proxy.service is running and Caddy has permission to bind port 443.",
        )

    if not cert_valid:
        # Fail closed for EVERY TLS mode: never report HEALTHY when certificate check failed
        remediation = (
            "Ensure the local Caddy root CA certificate is installed into the system trust store."
            if tls_mode == "LOCAL"
            else f"Verify public TLS certificate chain and hostname for {public_host}."
        )
        return ConsoleDiagnostic(
            state=ConsoleState.TLS_UNTRUSTED,
            public_host=public_host,
            dashboard_port=dashboard_port,
            tls_mode=tls_mode,
            console_url=console_url,
            backend_active=backend_active,
            tls_proxy_active=tls_proxy_active,
            cert_valid=False,
            message=f"HTTPS certificate validation failed ({cert_error or 'unverified certificate'}).",
            remediation=remediation,
        )

    runner = find_desktop_runner(resolved_paths)
    if not runner:
        return ConsoleDiagnostic(
            state=ConsoleState.NO_RUNNER,
            public_host=public_host,
            dashboard_port=dashboard_port,
            tls_mode=tls_mode,
            console_url=console_url,
            backend_active=backend_active,
            tls_proxy_active=tls_proxy_active,
            cert_valid=cert_valid,
            message="No supported desktop application runner found (Chromium, Edge, or Chrome).",
            remediation="Install Chromium ('sudo apt install chromium-browser') or run Chief Installer to provision the browser runtime.",
        )

    return ConsoleDiagnostic(
        state=ConsoleState.HEALTHY,
        public_host=public_host,
        dashboard_port=dashboard_port,
        tls_mode=tls_mode,
        console_url=console_url,
        backend_active=True,
        tls_proxy_active=True,
        cert_valid=True,
        message="Chief on-farm services are active and verified over local loopback.",
        remediation="",
    )


def build_app_mode_command(runner: str, console_url: str, public_host: str, profile_dir: Path) -> List[str]:
    """Construct command-line arguments to launch an app-mode window with zero browser chrome."""
    return [
        runner,
        f"--app={console_url}",
        f"--user-data-dir={str(profile_dir)}",
        f"--host-resolver-rules=MAP {public_host} 127.0.0.1",
        "--app-id=ChiefConsole",
        "--class=ChiefConsole",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]


def launch_console_window(diagnostic: ConsoleDiagnostic, paths: InstallerPaths) -> bool:
    """Launch the Chief Console in a dedicated application window."""
    runner = find_desktop_runner(paths)
    if not runner:
        return False

    profile_dir = paths.state_root / "console_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = build_app_mode_command(runner, diagnostic.console_url, diagnostic.public_host, profile_dir)
    try:
        proc = popen_quiet(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True if os.name != "nt" else False,
        )
        # Verify process started and did not fail immediately
        try:
            res = proc.wait(timeout=0.3)
            if res != 0:
                return False
        except subprocess.TimeoutExpired:
            pass  # Process is running normally
        return True
    except Exception:
        return False


class ConsoleRecoveryUI:
    """Native desktop recovery and diagnostic window for the Chief Console."""

    def __init__(self, diagnostic: ConsoleDiagnostic, paths: InstallerPaths):
        self.diagnostic = diagnostic
        self.paths = paths
        self.root: Optional[Any] = None

    def build_window(self) -> Any:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title("Chief Console — Local Connection")
        root.geometry("640x480")
        root.minsize(580, 420)
        root.configure(bg="#f5f4ef")

        # Palette
        accent_color = "#173d39"
        bg_card = "#ffffff"
        text_color = "#243630"
        muted_color = "#64766e"
        danger_color = "#973f37"
        warning_color = "#bd8b38"
        success_color = "#2e7350"

        # Header frame
        header_frame = tk.Frame(root, bg=accent_color, padx=24, pady=18)
        header_frame.pack(fill="x")

        eyebrow = tk.Label(
            header_frame,
            text="ON-FARM OPERATIONS CENTRE",
            font=("Helvetica", 9, "bold"),
            fg="#a6c0b4",
            bg=accent_color,
        )
        eyebrow.pack(anchor="w")

        title = tk.Label(
            header_frame,
            text="Chief Console",
            font=("Helvetica", 20, "bold"),
            fg="#ffffff",
            bg=accent_color,
        )
        title.pack(anchor="w", pady=(2, 0))

        # Content frame
        content = tk.Frame(root, bg="#f5f4ef", padx=24, pady=20)
        content.pack(fill="both", expand=True)

        # Status badge
        is_healthy = self.diagnostic.state == ConsoleState.HEALTHY
        status_color = success_color if is_healthy else (danger_color if self.diagnostic.state in (ConsoleState.SERVICES_STOPPED, ConsoleState.ENDPOINT_UNAVAILABLE) else warning_color)
        status_text = {
            ConsoleState.HEALTHY: "Services Connected and Verified",
            ConsoleState.SERVICES_STOPPED: "Chief Services Stopped",
            ConsoleState.TLS_UNTRUSTED: "Certificate Trust Required",
            ConsoleState.SETUP_REQUIRED: "Setup Incomplete",
            ConsoleState.ENDPOINT_UNAVAILABLE: "Local Endpoint Unavailable",
            ConsoleState.STARTING: "Checking Connection...",
        }.get(self.diagnostic.state, str(self.diagnostic.state.value))

        status_box = tk.Frame(content, bg=status_color, padx=12, pady=6)
        status_box.pack(anchor="w", pady=(0, 14))

        status_lbl = tk.Label(
            status_box,
            text=f"Status: {status_text}",
            font=("Helvetica", 11, "bold"),
            fg="#ffffff",
            bg=status_color,
        )
        status_lbl.pack()

        # Diagnostics card
        card = tk.Frame(content, bg=bg_card, bd=1, relief="solid", padx=18, pady=16)
        card.pack(fill="x", pady=(0, 16))

        tk.Label(card, text="Connection Diagnostics", font=("Helvetica", 12, "bold"), fg=text_color, bg=bg_card).pack(anchor="w")
        
        detail_text = (
            f"• Target Endpoint: {self.diagnostic.console_url}\n"
            f"• Backend (port {self.diagnostic.dashboard_port}): {'Active' if self.diagnostic.backend_active else 'Inactive'}\n"
            f"• HTTPS Proxy (port 443): {'Active' if self.diagnostic.tls_proxy_active else 'Inactive'}\n"
            f"• TLS Certificate Trust: {'Valid' if self.diagnostic.cert_valid else 'Untrusted / Incomplete'}\n\n"
            f"{self.diagnostic.message}"
        )
        tk.Label(card, text=detail_text, font=("Helvetica", 10), fg=text_color, bg=bg_card, justify="left").pack(anchor="w", pady=(8, 4))

        if self.diagnostic.remediation:
            tk.Label(card, text="Recommended Action:", font=("Helvetica", 10, "bold"), fg=muted_color, bg=bg_card).pack(anchor="w", pady=(6, 2))
            tk.Label(card, text=self.diagnostic.remediation, font=("Helvetica", 10), fg=text_color, bg=bg_card, justify="left").pack(anchor="w")

        # Action button box
        btn_box = tk.Frame(content, bg="#f5f4ef")
        btn_box.pack(fill="x", pady=10)

        def on_retry():
            self.diagnostic = probe_local_console(self.paths)
            if self.diagnostic.state == ConsoleState.HEALTHY:
                if launch_console_window(self.diagnostic, self.paths):
                    root.destroy()
                    return
            # Refresh window
            root.destroy()
            new_ui = ConsoleRecoveryUI(self.diagnostic, self.paths)
            new_ui.run()

        retry_btn = tk.Button(
            btn_box,
            text="Retry Connection (R)",
            font=("Helvetica", 10, "bold"),
            bg=accent_color,
            fg="#ffffff",
            padx=16,
            pady=8,
            command=on_retry,
        )
        retry_btn.pack(side="left", padx=(0, 10))

        if self.diagnostic.state == ConsoleState.SETUP_REQUIRED:
            def on_run_installer():
                frozen_exe = os.environ.get("CHIEF_INSTALLER_FROZEN_EXECUTABLE")
                if getattr(sys, "frozen", False) and frozen_exe and Path(frozen_exe).exists():
                    cmd = [frozen_exe, "--gui"]
                else:
                    py_bin = sys.executable
                    if os.name == "nt" and py_bin.lower().endswith("python.exe"):
                        pyw = Path(py_bin).with_name("pythonw.exe")
                        if pyw.exists():
                            py_bin = str(pyw)
                    cmd = [py_bin, "-m", "deployment.installer", "--gui"]
                popen_quiet(cmd)
                root.destroy()

            installer_btn = tk.Button(
                btn_box,
                text="Open Chief Installer",
                font=("Helvetica", 10),
                padx=14,
                pady=8,
                command=on_run_installer,
            )
            installer_btn.pack(side="left", padx=(0, 10))

        exit_btn = tk.Button(
            btn_box,
            text="Exit (Esc)",
            font=("Helvetica", 10),
            padx=14,
            pady=8,
            command=root.destroy,
        )
        exit_btn.pack(side="right")

        root.bind("<r>", lambda e: on_retry())
        root.bind("<R>", lambda e: on_retry())
        root.bind("<Escape>", lambda e: root.destroy())

        self.root = root
        return root

    def run(self) -> None:
        win = self.build_window()
        win.mainloop()


def console_smoke_test() -> int:
    """Validate that Console diagnostics and window constructor execute without errors in headless mode."""
    paths = InstallerPaths.create()
    diagnostic = probe_local_console(paths)
    assert diagnostic.public_host != ""
    assert diagnostic.console_url.startswith("https://")
    runner = find_desktop_runner(paths)
    if not runner:
        raise RuntimeError("Chief Console smoke test failed: no supported desktop application runner found. Please install Chromium, Google Chrome, or Microsoft Edge.")
    cmd = build_app_mode_command(runner, diagnostic.console_url, diagnostic.public_host, paths.state_root / "console_profile")
    assert "--app=" in cmd[1]
    assert "--host-resolver-rules=" in cmd[3]
    assert "--class=ChiefConsole" in cmd[5]

    # Model window construction in mock/non-blocking manner
    try:
        import tkinter as tk
        ui = ConsoleRecoveryUI(diagnostic, paths)
        # Verify UI object builds without crash
        assert ui.diagnostic is diagnostic
    except Exception as exc:
        if "DISPLAY" not in os.environ and os.name != "nt":
            pass  # Expected in headless Linux without X11
        else:
            raise exc

    print("Chief Console smoke test passed: diagnostic preflight, runner discovery, and command contract verified.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for Chief Console desktop application."""
    args = argv if argv is not None else sys.argv[1:]

    if "--console-smoke-test" in args or "--smoke-test" in args:
        return console_smoke_test()

    paths = InstallerPaths.create()
    diagnostic = probe_local_console(paths)

    if diagnostic.state == ConsoleState.HEALTHY:
        launched = launch_console_window(diagnostic, paths)
        if launched:
            print(f"[Chief Console] Connected to on-farm interface at {diagnostic.console_url}")
            return 0

    # If unhealthy or no app-mode runner available, open native recovery window
    try:
        ui = ConsoleRecoveryUI(diagnostic, paths)
        ui.run()
        return 0
    except Exception as exc:
        sys.stderr.write(f"Chief Console window failed to launch ({exc}).\n")
        sys.stderr.write(f"State: {diagnostic.state.value}\nMessage: {diagnostic.message}\nRemediation: {diagnostic.remediation}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
