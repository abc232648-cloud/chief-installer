"""Graphical User Interface for Chief Unified Installer using Tkinter/TTK."""
import os
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional, Any

from .controller import InstallerUIController, ScreenId
from ..events import InstallEvent
from ..manifest import CURRENT_RELEASE_MANIFEST, ReleaseManifest
from ..models import PreflightReport, StepStatus
from ..platforms.base import PlatformAdapter
from ..platforms.linux import LinuxPlatformAdapter


class InstallerApp(tk.Tk):
    """Main Chief Installer GUI window."""

    def __init__(
        self,
        platform_adapter: Optional[PlatformAdapter] = None,
        manifest: Optional[ReleaseManifest] = None,
        controller: Optional[InstallerUIController] = None,
        paths: Optional[Any] = None,
    ):
        super().__init__()
        self.title("Chief Installer")
        self.geometry("640x520")
        self.minsize(580, 480)

        # Style configuration
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        if platform_adapter is None:
            if sys.platform == "win32" or os.name == "nt":
                from ..platforms.windows import WindowsPlatformAdapter
                platform_adapter = WindowsPlatformAdapter()
            else:
                platform_adapter = LinuxPlatformAdapter()

        self.controller = controller or InstallerUIController(
            platform_adapter=platform_adapter,
            manifest=manifest or CURRENT_RELEASE_MANIFEST,
            paths=paths,
        )

        self.container = ttk.Frame(self, padding="24 20 24 20")
        self.container.pack(fill=tk.BOTH, expand=True)

        self.current_frame: Optional[ttk.Frame] = None
        self.controller.subscribe_screen_change(self._on_screen_changed)
        self.controller.subscribe_progress(self._on_progress_update)

        # Render initial screen
        self._render_screen(self.controller.current_screen)

    def _on_screen_changed(self, screen_id: ScreenId) -> None:
        self.after(0, lambda: self._render_screen(screen_id))

    def _on_progress_update(self, event: InstallEvent) -> None:
        self.after(0, lambda: self._update_progress_ui(event))

    def _clear_container(self) -> None:
        if self.current_frame:
            self.current_frame.destroy()
            self.current_frame = None

    def _render_screen(self, screen_id: ScreenId) -> None:
        self._clear_container()
        frame = ttk.Frame(self.container)
        frame.pack(fill=tk.BOTH, expand=True)
        self.current_frame = frame

        if screen_id == ScreenId.WELCOME:
            self._build_welcome(frame)
        elif screen_id == ScreenId.PREFLIGHT:
            self._build_preflight(frame)
        elif screen_id == ScreenId.EXISTING_INSTALL:
            self._build_existing_install(frame)
        elif screen_id == ScreenId.IDENTITY:
            self._build_identity(frame)
        elif screen_id == ScreenId.PASSWORD:
            self._build_password(frame)
        elif screen_id == ScreenId.FARM_OPERATIONS:
            self._build_farm_operations(frame)
        elif screen_id == ScreenId.CONFIRMATION:
            self._build_confirmation(frame)
        elif screen_id == ScreenId.PROGRESS:
            self._build_progress(frame)
        elif screen_id == ScreenId.FAILURE:
            self._build_failure(frame)
        elif screen_id == ScreenId.COMPLETION:
            self._build_completion(frame)
        elif screen_id == ScreenId.UNINSTALL_OPTIONS:
            self._build_uninstall_options(frame)
        elif screen_id == ScreenId.UNINSTALL_PROGRESS:
            self._build_uninstall_progress(frame)
        elif screen_id == ScreenId.UNINSTALL_RESULT:
            self._build_uninstall_result(frame)

    # 1. Welcome Screen
    def _build_welcome(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Install Chief", font=("Helvetica", 18, "bold"))
        title.pack(anchor=tk.W, pady=(0, 8))

        subtitle = ttk.Label(
            parent,
            text=f"Release {self.controller.manifest.release_id}",
            font=("Helvetica", 10),
            foreground="#555555",
        )
        subtitle.pack(anchor=tk.W, pady=(0, 16))

        desc = ttk.Label(
            parent,
            text=(
                "Welcome to the Chief installation setup.\n\n"
                "This installer will set up Chief's private database, runtime, Owner and Staff applications, "
                "local HTTPS security, and background services on your computer.\n\n"
                "No terminal commands or manual configuration are required."
            ),
            wraplength=540,
            justify=tk.LEFT,
        )
        desc.pack(anchor=tk.W, pady=(0, 24))

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        cancel_btn = ttk.Button(btn_bar, text="Cancel", command=self.destroy)
        cancel_btn.pack(side=tk.LEFT)

        uninstall_btn = ttk.Button(btn_bar, text="Uninstall Chief...", command=self.controller.start_uninstall)
        uninstall_btn.pack(side=tk.LEFT, padx=(8, 0))

        install_btn = ttk.Button(btn_bar, text="Install Chief", command=self.controller.next_from_welcome)
        install_btn.pack(side=tk.RIGHT)

    # 2. Preflight Screen
    def _build_preflight(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Checking Your Computer", font=("Helvetica", 16, "bold"))
        title.pack(anchor=tk.W, pady=(0, 12))

        status_label = ttk.Label(parent, text="Detecting operating system and hardware requirements...")
        status_label.pack(anchor=tk.W, pady=(0, 16))

        details_frame = ttk.Frame(parent)
        details_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 16))

        details_text = tk.Text(details_frame, height=10, width=60, wrap=tk.WORD, font=("Courier", 9))
        details_text.pack(fill=tk.BOTH, expand=True)

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        back_btn = ttk.Button(btn_bar, text="Back", command=self.controller.go_back)
        back_btn.pack(side=tk.LEFT)

        next_btn = ttk.Button(btn_bar, text="Next", state=tk.DISABLED, command=self.controller.next_from_preflight)
        next_btn.pack(side=tk.RIGHT)

        def on_report(report: PreflightReport):
            self.after(0, lambda: self._update_preflight_ui(report, status_label, details_text, next_btn))

        self.controller.run_preflight_async(callback=on_report)

    def _update_preflight_ui(self, report: PreflightReport, label: ttk.Label, text_widget: tk.Text, next_btn: ttk.Button):
        text_widget.delete("1.0", tk.END)
        text_widget.insert(tk.END, f"Platform: {report.platform.distro_name} {report.platform.distro_version} ({report.platform.architecture})\n\n")
        for check in report.checks:
            mark = "✓" if check.passed else "✗"
            text_widget.insert(tk.END, f"{mark} {check.name}: {check.message}\n")

        if report.passed:
            label.config(text="✓ All system requirements satisfied.", foreground="green")
            next_btn.config(state=tk.NORMAL)
        else:
            label.config(text="✗ Some system requirements were not met.", foreground="red")

    # 3. Existing Installation Screen
    def _build_existing_install(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Existing Chief Installation Detected", font=("Helvetica", 16, "bold"))
        title.pack(anchor=tk.W, pady=(0, 12))

        desc = ttk.Label(
            parent,
            text=(
                "A previous Chief installation was found on this computer.\n\n"
                "The previous deployment will be safely archived to:\n"
                "~/Chief_Previous_Installations/<timestamp>\n\n"
                "Existing data and logs will NOT be deleted. A fresh, clean production "
                "environment will be created for this installation."
            ),
            wraplength=540,
            justify=tk.LEFT,
        )
        desc.pack(anchor=tk.W, pady=(0, 20))

        ack_var = tk.BooleanVar(value=False)
        ack_chk = ttk.Checkbutton(
            parent,
            text="I understand the previous Chief deployment will be moved to the archive folder.",
            variable=ack_var,
        )
        ack_chk.pack(anchor=tk.W, pady=(0, 24))

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        back_btn = ttk.Button(btn_bar, text="Back", command=self.controller.go_back)
        back_btn.pack(side=tk.LEFT)

        uninstall_btn = ttk.Button(btn_bar, text="Uninstall Chief...", command=self.controller.start_uninstall)
        uninstall_btn.pack(side=tk.LEFT, padx=(8, 0))

        def on_next():
            try:
                self.controller.next_from_existing_install(ack_var.get())
            except ValueError as e:
                messagebox.showerror("Acknowledgement Required", str(e))

        next_btn = ttk.Button(btn_bar, text="Next", command=on_next)
        next_btn.pack(side=tk.RIGHT)

    # 4. Identity Screen
    def _build_identity(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Chief Ownership & Identity", font=("Helvetica", 16, "bold"))
        title.pack(anchor=tk.W, pady=(0, 16))

        lbl1 = ttk.Label(parent, text="Who is this Chief for? (Your name or organisation):")
        lbl1.pack(anchor=tk.W, pady=(0, 4))
        name_entry = ttk.Entry(parent, width=40)
        name_entry.insert(0, self.controller.owner_name)
        name_entry.pack(anchor=tk.W, pady=(0, 16))

        lbl2 = ttk.Label(parent, text="Owner Username:")
        lbl2.pack(anchor=tk.W, pady=(0, 4))
        user_entry = ttk.Entry(parent, width=40)
        user_entry.insert(0, self.controller.owner_username or "owner")
        user_entry.pack(anchor=tk.W, pady=(0, 24))

        dns_choice = tk.StringVar(value=self.controller.dns_mode)
        ttk.Label(parent, text='DNS for Chief browsing and managed downloads (host DNS unchanged):').pack(anchor=tk.W)
        ttk.Radiobutton(parent, text='Quad9 Secure DoH (recommended)', variable=dns_choice, value='quad9').pack(anchor=tk.W)
        ttk.Radiobutton(parent, text='System DNS', variable=dns_choice, value='system').pack(anchor=tk.W)
        ttk.Label(parent, text='Secure DNS failures stop the connection; there is no automatic fallback.').pack(anchor=tk.W)


        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        back_btn = ttk.Button(btn_bar, text="Back", command=self.controller.go_back)
        back_btn.pack(side=tk.LEFT)

        uninstall_btn = ttk.Button(btn_bar, text="Uninstall Chief...", command=self.controller.start_uninstall)
        uninstall_btn.pack(side=tk.LEFT, padx=(8, 0))

        def on_next():
            try:
                self.controller.dns_mode = dns_choice.get()
                self.controller.validate_identity(name_entry.get(), user_entry.get())
            except ValueError as e:
                messagebox.showerror("Invalid Input", str(e))

        next_btn = ttk.Button(btn_bar, text="Next", command=on_next)
        next_btn.pack(side=tk.RIGHT)

    # 5. Password Screen
    def _build_password(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Owner Password", font=("Helvetica", 16, "bold"))
        title.pack(anchor=tk.W, pady=(0, 12))

        desc = ttk.Label(
            parent,
            text="Set a strong password for your Chief Owner account. Passwords are never saved in logs.",
            wraplength=540,
        )
        desc.pack(anchor=tk.W, pady=(0, 16))

        lbl1 = ttk.Label(parent, text="Password (min 8 characters):")
        lbl1.pack(anchor=tk.W, pady=(0, 4))
        p1_entry = ttk.Entry(parent, width=35, show="*")
        p1_entry.pack(anchor=tk.W, pady=(0, 16))

        lbl2 = ttk.Label(parent, text="Confirm Password:")
        lbl2.pack(anchor=tk.W, pady=(0, 4))
        p2_entry = ttk.Entry(parent, width=35, show="*")
        p2_entry.pack(anchor=tk.W, pady=(0, 24))

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        back_btn = ttk.Button(btn_bar, text="Back", command=self.controller.go_back)
        back_btn.pack(side=tk.LEFT)

        uninstall_btn = ttk.Button(btn_bar, text="Uninstall Chief...", command=self.controller.start_uninstall)
        uninstall_btn.pack(side=tk.LEFT, padx=(8, 0))

        def on_next():
            try:
                self.controller.validate_password(p1_entry.get(), p2_entry.get())
            except ValueError as e:
                messagebox.showerror("Invalid Password", str(e))

        next_btn = ttk.Button(btn_bar, text="Next", command=on_next)
        next_btn.pack(side=tk.RIGHT)

    # 6. Farm Operations Screen
    def _build_farm_operations(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Farm Operations", font=("Helvetica", 16, "bold"))
        title.pack(anchor=tk.W, pady=(0, 12))

        desc = ttk.Label(
            parent,
            text="Would you like to enable Farm management features (flocks, morning checklists, treatments)?",
            wraplength=540,
        )
        desc.pack(anchor=tk.W, pady=(0, 16))

        farm_choice = tk.StringVar(value="YES" if self.controller.farm_operations == "ENABLED" else "NO")

        r1 = ttk.Radiobutton(parent, text="Enable Farm operations now", variable=farm_choice, value="YES")
        r1.pack(anchor=tk.W, pady=(0, 6))

        r2 = ttk.Radiobutton(parent, text="Not now (Standard Chief)", variable=farm_choice, value="NO")
        r2.pack(anchor=tk.W, pady=(0, 16))

        farm_fields = ttk.Frame(parent)
        farm_fields.pack(anchor=tk.W, fill=tk.X, pady=(0, 16))

        lbl_farm = ttk.Label(farm_fields, text="First Farm Name:")
        lbl_farm.pack(anchor=tk.W, pady=(0, 4))
        farm_entry = ttk.Entry(farm_fields, width=35)
        farm_entry.insert(0, self.controller.farm_name or "Main Farm")
        farm_entry.pack(anchor=tk.W, pady=(0, 12))

        lbl_org = ttk.Label(farm_fields, text=f"Farm Organisation (default: {self.controller.owner_name}):")
        lbl_org.pack(anchor=tk.W, pady=(0, 4))
        org_entry = ttk.Entry(farm_fields, width=35)
        org_entry.insert(0, self.controller.organization_name or self.controller.owner_name)
        org_entry.pack(anchor=tk.W)

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        back_btn = ttk.Button(btn_bar, text="Back", command=self.controller.go_back)
        back_btn.pack(side=tk.LEFT)

        uninstall_btn = ttk.Button(btn_bar, text="Uninstall Chief...", command=self.controller.start_uninstall)
        uninstall_btn.pack(side=tk.LEFT, padx=(8, 0))

        def on_next():
            enabled = farm_choice.get() == "YES"
            try:
                self.controller.validate_farm_operations(enabled, org_entry.get(), farm_entry.get())
            except ValueError as e:
                messagebox.showerror("Missing Information", str(e))

        next_btn = ttk.Button(btn_bar, text="Next", command=on_next)
        next_btn.pack(side=tk.RIGHT)

    # 7. Confirmation Screen
    def _build_confirmation(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Ready to Install Chief", font=("Helvetica", 16, "bold"))
        title.pack(anchor=tk.W, pady=(0, 16))

        summary = self.controller.get_confirmation_summary()
        summary_frame = ttk.LabelFrame(parent, text="Installation Summary", padding="12 12 12 12")
        summary_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 20))

        for k, v in summary.items():
            row = ttk.Frame(summary_frame)
            row.pack(fill=tk.X, pady=4)
            lbl_k = ttk.Label(row, text=f"{k}:", font=("Helvetica", 9, "bold"), width=22)
            lbl_k.pack(side=tk.LEFT)
            lbl_v = ttk.Label(row, text=str(v), font=("Helvetica", 9))
            lbl_v.pack(side=tk.LEFT)

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        back_btn = ttk.Button(btn_bar, text="Back", command=self.controller.go_back)
        back_btn.pack(side=tk.LEFT)

        install_btn = ttk.Button(btn_bar, text="Install Chief", command=self.controller.start_installation)
        install_btn.pack(side=tk.RIGHT)

    # 8. Progress Screen
    def _build_progress(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Installing Chief...", font=("Helvetica", 16, "bold"))
        title.pack(anchor=tk.W, pady=(0, 16))

        self.progress_bar = ttk.Progressbar(parent, mode="indeterminate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 16))
        self.progress_bar.start(15)

        self.status_step_lbl = ttk.Label(parent, text="Starting installation...", font=("Helvetica", 10, "bold"))
        self.status_step_lbl.pack(anchor=tk.W, pady=(0, 4))

        self.status_msg_lbl = ttk.Label(parent, text="", font=("Helvetica", 9), foreground="#555555")
        self.status_msg_lbl.pack(anchor=tk.W, pady=(0, 16))

        log_frame = ttk.LabelFrame(parent, text="Technical Log", padding="8 8 8 8")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 16))

        self.progress_text = tk.Text(log_frame, height=8, width=60, font=("Courier", 8), wrap=tk.WORD)
        self.progress_text.pack(fill=tk.BOTH, expand=True)

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        cancel_btn = ttk.Button(btn_bar, text="Cancel", command=self.controller.cancel_installation)
        cancel_btn.pack(side=tk.RIGHT)

    def _update_progress_ui(self, event: InstallEvent) -> None:
        if hasattr(self, "status_step_lbl") and self.status_step_lbl.winfo_exists():
            self.status_step_lbl.config(text=f"→ {event.display_name}")
            self.status_msg_lbl.config(text=event.message)
            if hasattr(self, "progress_text") and self.progress_text.winfo_exists():
                mark = "✓" if event.status == StepStatus.SUCCESS else ("✗" if event.status == StepStatus.FAILED else "→")
                self.progress_text.insert(tk.END, f"{mark} {event.display_name}: {event.message}\n")
                self.progress_text.see(tk.END)

    # 9. Failure Screen
    def _build_failure(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Installation Could Not Continue", font=("Helvetica", 16, "bold"), foreground="red")
        title.pack(anchor=tk.W, pady=(0, 12))

        reason = ttk.Label(
            parent,
            text=f"Reason: {self.controller.failed_reason}",
            wraplength=540,
            foreground="#333333",
        )
        reason.pack(anchor=tk.W, pady=(0, 16))

        diag_frame = ttk.LabelFrame(parent, text="Sanitized Diagnostic Report", padding="8 8 8 8")
        diag_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 16))

        diag_text = tk.Text(diag_frame, height=10, width=60, font=("Courier", 8))
        diag_text.pack(fill=tk.BOTH, expand=True)
        diag_text.insert(tk.END, self.controller.diagnostic_report or "No diagnostic report generated.")

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        close_btn = ttk.Button(btn_bar, text="Exit", command=self.destroy)
        close_btn.pack(side=tk.RIGHT)

    # 10. Completion Screen
    def _build_completion(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Chief is Ready!", font=("Helvetica", 18, "bold"), foreground="green")
        title.pack(anchor=tk.W, pady=(0, 12))

        urls = self.controller.install_result.urls if self.controller.install_result else {}
        chief_url = urls.get("chief", f"https://{self.controller.public_host or 'chief.local'}/")
        owner_url = urls.get("owner", f"https://{self.controller.public_host or 'chief.local'}/owner/")
        desc = ttk.Label(
            parent,
            text=(
                "Installation completed successfully.\n\n"
                f"Chief Console (On-Farm): {chief_url}\n"
                f"Remote Owner PWA:         {owner_url}\n\n"
                "Launch the on-farm console to configure your system, or open a companion app below."
            ),
            wraplength=540,
        )
        desc.pack(anchor=tk.W, pady=(0, 24))

        action_box = ttk.Frame(parent)
        action_box.pack(anchor=tk.W, fill=tk.X, pady=(0, 24))

        open_chief_btn = ttk.Button(action_box, text="Open Chief Console", command=lambda: self.controller.open_chief_console())
        open_chief_btn.pack(side=tk.LEFT, padx=(0, 8))

        open_owner_btn = ttk.Button(action_box, text="Open Owner PWA", command=lambda: self.controller.open_chief_url("owner"))
        open_owner_btn.pack(side=tk.LEFT, padx=(0, 8))

        open_staff_btn = ttk.Button(action_box, text="Open Staff App", command=lambda: self.controller.open_chief_url("staff"))
        open_staff_btn.pack(side=tk.LEFT, padx=(0, 8))

        open_jobs_btn = ttk.Button(action_box, text="Open Job Agent", command=lambda: self.controller.open_chief_url("jobs"))
        open_jobs_btn.pack(side=tk.LEFT)

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        close_btn = ttk.Button(btn_bar, text="Close", command=self.destroy)
        close_btn.pack(side=tk.RIGHT)


    # 11. Uninstall Options Screen
    def _build_uninstall_options(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Uninstall Chief", font=("Helvetica", 18, "bold"))
        title.pack(anchor=tk.W, pady=(0, 6))

        subtitle = ttk.Label(
            parent,
            text="Choose uninstallation mode.",
            font=("Helvetica", 10),
            foreground="#555555",
        )
        subtitle.pack(anchor=tk.W, pady=(0, 14))

        mode_var = tk.StringVar(value=self.controller.uninstall_mode)
        confirm_var = tk.BooleanVar(value=self.controller.purge_confirmed)

        options_frame = ttk.Frame(parent)
        options_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # Radio 1: Standard Uninstall
        std_rb = ttk.Radiobutton(
            options_frame,
            text="Standard Uninstall (Recommended)",
            variable=mode_var,
            value="standard",
            command=lambda: self._on_uninstall_mode_changed(mode_var.get(), confirm_var.get()),
        )
        std_rb.pack(anchor=tk.W, pady=(0, 2))

        std_desc = ttk.Label(
            options_frame,
            text=(
                "- Stops and unregisters background services and scheduled tasks\n"
                "- Removes application launchers and desktop shortcuts\n"
                "- Removes runtime files, Node.js, Caddy binary, and web applications\n"
                "- PRESERVES: Farm database (chief.sqlite), user identities, authentication\n"
                "  tokens, WireGuard setup files (~/Chief_Device_Setup), and backup archives."
            ),
            font=("Helvetica", 9),
            foreground="#333333",
            wraplength=540,
            justify=tk.LEFT,
        )
        std_desc.pack(anchor=tk.W, padx=(24, 0), pady=(0, 14))

        # Radio 2: Full Removal (Purge)
        purge_rb = ttk.Radiobutton(
            options_frame,
            text="Full Removal (Purge Everything)",
            variable=mode_var,
            value="purge",
            command=lambda: self._on_uninstall_mode_changed(mode_var.get(), confirm_var.get()),
        )
        purge_rb.pack(anchor=tk.W, pady=(0, 2))

        purge_desc = ttk.Label(
            options_frame,
            text=(
                "- Completely removes all Chief background services and launchers\n"
                "- Removes runtime, web applications, and proxy configuration\n"
                "- PERMANENTLY DELETES farm database, user accounts, authentication tokens,\n"
                "  exported setup keys (~/Chief_Device_Setup), TLS certificates, and archives."
            ),
            font=("Helvetica", 9),
            foreground="#990000",
            wraplength=540,
            justify=tk.LEFT,
        )
        purge_desc.pack(anchor=tk.W, padx=(24, 0), pady=(0, 10))

        # Confirmation checkbox for Full Removal
        self.purge_confirm_chk = ttk.Checkbutton(
            options_frame,
            text="I understand this will permanently delete all farm data, databases, and setup credentials.",
            variable=confirm_var,
            command=lambda: self._on_uninstall_confirm_toggled(mode_var.get(), confirm_var.get()),
        )
        self.purge_confirm_chk.pack(anchor=tk.W, padx=(24, 0), pady=(0, 10))

        # Button Bar
        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        back_btn = ttk.Button(btn_bar, text="Back", command=self.controller.go_back)
        back_btn.pack(side=tk.LEFT)

        self.uninstall_submit_btn = ttk.Button(
            btn_bar,
            text="Uninstall Chief",
            command=lambda: self._on_uninstall_submit(mode_var.get(), confirm_var.get()),
        )
        self.uninstall_submit_btn.pack(side=tk.RIGHT)

        self._update_uninstall_mode_ui(mode_var.get(), confirm_var.get())

    def _on_uninstall_mode_changed(self, mode: str, confirmed: bool) -> None:
        self.controller.set_uninstall_mode(mode)
        self._update_uninstall_mode_ui(mode, confirmed)

    def _on_uninstall_confirm_toggled(self, mode: str, confirmed: bool) -> None:
        self.controller.set_purge_confirmed(confirmed)
        self._update_uninstall_mode_ui(mode, confirmed)

    def _update_uninstall_mode_ui(self, mode: str, confirmed: bool) -> None:
        if not hasattr(self, "uninstall_submit_btn") or not self.uninstall_submit_btn.winfo_exists():
            return
        if mode == "purge":
            self.purge_confirm_chk.configure(state="normal")
            if confirmed:
                self.uninstall_submit_btn.configure(state="normal", text="Permanently Purge Chief")
            else:
                self.uninstall_submit_btn.configure(state="disabled", text="Permanently Purge Chief")
        else:
            self.purge_confirm_chk.configure(state="disabled")
            self.uninstall_submit_btn.configure(state="normal", text="Uninstall Chief")

    def _on_uninstall_submit(self, mode: str, confirmed: bool) -> None:
        self.controller.set_uninstall_mode(mode)
        self.controller.set_purge_confirmed(confirmed)
        try:
            self.controller.execute_uninstall()
        except ValueError as exc:
            messagebox.showerror("Confirmation Required", str(exc))

    # 12. Uninstall Progress Screen
    def _build_uninstall_progress(self, parent: ttk.Frame) -> None:
        title = ttk.Label(parent, text="Uninstalling Chief...", font=("Helvetica", 18, "bold"))
        title.pack(anchor=tk.W, pady=(0, 12))

        prog = ttk.Progressbar(parent, mode="indeterminate")
        prog.pack(fill=tk.X, pady=(0, 16))
        prog.start(15)

        msg = ttk.Label(
            parent,
            text=self.controller.uninstall_message or "Stopping services and removing components...",
            font=("Helvetica", 10),
            foreground="#555555",
        )
        msg.pack(anchor=tk.W, pady=(0, 20))

    # 13. Uninstall Result Screen
    def _build_uninstall_result(self, parent: ttk.Frame) -> None:
        res = self.controller.uninstall_result or {}
        success = res.get("success", False)

        if success:
            title = ttk.Label(parent, text="Chief Uninstalled Successfully", font=("Helvetica", 18, "bold"), foreground="green")
            title.pack(anchor=tk.W, pady=(0, 12))

            mode_text = "Full Removal (Purged)" if res.get("purge") else "Standard Uninstall"
            stopped = len(res.get("stopped_services", []))
            unregistered = len(res.get("unregistered_tasks", []))
            launchers = len(res.get("removed_launchers", []))
            paths = len(res.get("removed_paths", []))

            summary = (
                f"Mode: {mode_text}\n\n"
                f"• Stopped background services: {stopped}\n"
                f"• Unregistered background tasks: {unregistered}\n"
                f"• Removed desktop launchers: {launchers}\n"
                f"• Removed component paths: {paths}\n\n"
            )
            if not res.get("purge"):
                summary += "Your farm database, configuration, credentials, and backups were safely preserved on this computer."
            else:
                summary += "All farm data, credentials, and archives were completely removed."

            desc = ttk.Label(parent, text=summary, font=("Helvetica", 10), wraplength=540, justify=tk.LEFT)
            desc.pack(anchor=tk.W, pady=(0, 24))
        else:
            title = ttk.Label(parent, text="Uninstallation Incomplete", font=("Helvetica", 18, "bold"), foreground="red")
            title.pack(anchor=tk.W, pady=(0, 12))

            err = self.controller.uninstall_error or "; ".join(res.get("failed_items", ["Unknown error"]))
            desc = ttk.Label(
                parent,
                text=f"The uninstaller encountered issues:\n\n{err}\n\nPlease close any programs that might have Chief files open and retry.",
                font=("Helvetica", 10),
                foreground="#333333",
                wraplength=540,
                justify=tk.LEFT,
            )
            desc.pack(anchor=tk.W, pady=(0, 24))

        btn_bar = ttk.Frame(parent)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        close_btn = ttk.Button(btn_bar, text="Close", command=self.destroy)
        close_btn.pack(side=tk.RIGHT)


def launch_gui(platform_adapter: Optional[PlatformAdapter] = None, start_screen: Optional[ScreenId] = None) -> int:
    """Entrypoint to launch the graphical installer window."""
    try:
        app = InstallerApp(platform_adapter=platform_adapter)
        if start_screen:
            app.controller.set_screen(start_screen)
        app.mainloop()
        return 0
    except Exception as exc:
        sys.stderr.write(f"GUI launch error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(launch_gui())
