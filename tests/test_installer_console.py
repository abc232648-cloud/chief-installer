"""Automated tests for Chief Console Desktop Application, Runner Discovery, and Strict TLS Validation."""
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import pytest

from deployment.installer.console import (
    ConsoleState,
    ConsoleDiagnostic,
    build_app_mode_command,
    find_desktop_runner,
    probe_local_console,
    launch_console_window,
    console_smoke_test,
)
from deployment.installer.orchestrator import InstallerPaths


def make_paths(tmp_path: Path, tls_mode: str = "LOCAL", public_host: str = "chief-farm.local") -> InstallerPaths:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    instance_config = config_dir / "production-instance.json"
    instance_config.write_text(
        json.dumps({
            "mode": "PRODUCTION",
            "public_host": public_host,
            "dashboard_port": 18765,
            "tls_mode": tls_mode,
        }),
        encoding="utf-8",
    )
    return InstallerPaths(
        root=tmp_path / "root",
        tooling=tmp_path / "tooling",
        release_source=tmp_path / "release_source",
        runtime=tmp_path / "runtime",
        python=tmp_path / "python",
        node_root=tmp_path / "node",
        pwa_root=tmp_path / "pwa",
        managed=tmp_path / "managed",
        production=tmp_path / "production",
        caddy_root=tmp_path / "caddy",
        config_root=config_dir,
        config=instance_config,
        identity_config=config_dir / "installation-identity.json",
        browser_config=config_dir / "browser.json",
        caddyfile=config_dir / "Caddyfile",
        state_root=tmp_path / "state",
        meta=tmp_path / "meta",
        source_manifest=tmp_path / "manifest.json",
        backup=tmp_path / "backup",
        unit_stage=tmp_path / "unit_stage",
        unit_dest=tmp_path / "unit_dest",
        archive_base=tmp_path / "archive",
    )


def test_probe_local_console_detects_incomplete_setup(tmp_path):
    paths = make_paths(tmp_path)
    paths.config.unlink()
    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.SETUP_REQUIRED
    assert "not been completed" in diag.message.lower()
    assert "chief installer" in diag.remediation.lower()


def test_probe_local_console_detects_stopped_services(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    def mock_refused(address, timeout=None):
        raise ConnectionRefusedError('Connection refused')
    monkeypatch.setattr(socket, 'create_connection', mock_refused)
    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.SERVICES_STOPPED
    assert diag.public_host == "chief-farm.local"
    assert diag.console_url == "https://chief-farm.local/"
    assert not diag.backend_active
    assert not diag.tls_proxy_active


def test_probe_local_console_local_tls_missing_root_ca_fails_closed(tmp_path, monkeypatch):
    paths = make_paths(tmp_path, tls_mode="LOCAL")

    # Simulate backend (18765) and TLS proxy (443) active
    def mock_create_connection(address, timeout=None):
        class DummySocket:
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return DummySocket()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    # Caddy root CA certificate file does not exist
    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.TLS_UNTRUSTED
    assert not diag.cert_valid
    assert "HTTPS certificate validation failed" in diag.message
    assert "Caddy root CA" in diag.remediation


def test_probe_local_console_public_mode_untrusted_ca_fails_closed(tmp_path, monkeypatch):
    paths = make_paths(tmp_path, tls_mode="DOMAIN", public_host="farm.example.com")

    def mock_create_connection(address, timeout=None):
        class DummySocket:
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return DummySocket()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    # Mock SSLContext.wrap_socket raising SSLCertVerificationError (untrusted CA)
    class FailingSSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            raise ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate")

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: FailingSSLContext())

    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.TLS_UNTRUSTED
    assert not diag.cert_valid
    assert "self-signed certificate" in diag.message
    assert "Verify public TLS certificate chain" in diag.remediation


def test_probe_local_console_expired_cert_fails_closed(tmp_path, monkeypatch):
    paths = make_paths(tmp_path, tls_mode="DOMAIN", public_host="farm.example.com")

    def mock_create_connection(address, timeout=None):
        class DummySocket:
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return DummySocket()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    class ExpiredSSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            raise ssl.SSLCertVerificationError("certificate verify failed: certificate has expired")

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: ExpiredSSLContext())

    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.TLS_UNTRUSTED
    assert not diag.cert_valid
    assert "certificate has expired" in diag.message


def test_probe_local_console_hostname_mismatch_fails_closed(tmp_path, monkeypatch):
    paths = make_paths(tmp_path, tls_mode="DOMAIN", public_host="farm.example.com")

    def mock_create_connection(address, timeout=None):
        class DummySocket:
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return DummySocket()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    class MismatchSSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            raise ssl.SSLCertVerificationError("hostname 'farm.example.com' doesn't match 'other.com'")

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: MismatchSSLContext())

    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.TLS_UNTRUSTED
    assert not diag.cert_valid
    assert "doesn't match" in diag.message


def test_probe_local_console_handshake_failure_fails_closed(tmp_path, monkeypatch):
    paths = make_paths(tmp_path, tls_mode="LOCAL", public_host="chief.local")

    # Put a fake local CA
    ca = paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n", encoding="utf-8")

    def mock_create_connection(address, timeout=None):
        class DummySocket:
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return DummySocket()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    class HandshakeFailSSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            raise ssl.SSLError("SSLV3_ALERT_HANDSHAKE_FAILURE")

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: HandshakeFailSSLContext())

    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.TLS_UNTRUSTED
    assert not diag.cert_valid
    assert "SSLV3_ALERT_HANDSHAKE_FAILURE" in diag.message


def test_probe_local_console_missing_runner_detects_no_runner(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    ca = paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n", encoding="utf-8")

    def mock_create_connection(address, timeout=None):
        class DummySocket:
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return DummySocket()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    class ValidSSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            class DummySSLSocket:
                def getpeercert(self): return {"subject": "chief-farm.local"}
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return DummySSLSocket()

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: ValidSSLContext())
    monkeypatch.setattr("deployment.installer.console.find_desktop_runner", lambda *a, **kw: None)

    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.NO_RUNNER
    assert diag.cert_valid is True
    assert "No supported desktop application runner found" in diag.message


def test_probe_local_console_healthy_when_all_verified(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    ca = paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n", encoding="utf-8")

    def mock_create_connection(address, timeout=None):
        class DummySocket:
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return DummySocket()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    class ValidSSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            class DummySSLSocket:
                def getpeercert(self): return {"subject": "chief-farm.local"}
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return DummySSLSocket()

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: ValidSSLContext())
    monkeypatch.setattr("deployment.installer.console.find_desktop_runner", lambda *a, **kw: "/usr/bin/chromium")

    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.HEALTHY
    assert diag.cert_valid is True
    assert diag.backend_active is True
    assert diag.tls_proxy_active is True


def test_find_desktop_runner_checks_browser_runtime_config(tmp_path):
    paths = make_paths(tmp_path)
    # Create fake browser executable
    fake_browser = tmp_path / "fake_chromium"
    fake_browser.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_browser.chmod(0o755)

    paths.browser_config.write_text(
        json.dumps({
            "executable": str(fake_browser),
            "version": "130.0.0.0",
        }),
        encoding="utf-8",
    )

    runner = find_desktop_runner(paths)
    assert runner == str(fake_browser)


def test_console_smoke_test_fails_closed_when_no_runner(monkeypatch):
    monkeypatch.setattr("deployment.installer.console.find_desktop_runner", lambda *a, **kw: None)
    with pytest.raises(RuntimeError) as exc_info:
        console_smoke_test()
    assert "no supported desktop application runner found" in str(exc_info.value).lower()


def test_launch_console_window_returns_false_on_missing_runner(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    diag = ConsoleDiagnostic(
        state=ConsoleState.HEALTHY,
        public_host="chief.local",
        dashboard_port=8765,
        tls_mode="LOCAL",
        console_url="https://chief.local/",
        backend_active=True,
        tls_proxy_active=True,
        cert_valid=True,
        message="",
        remediation="",
    )
    monkeypatch.setattr("deployment.installer.console.find_desktop_runner", lambda *a, **kw: None)
    assert launch_console_window(diag, paths) is False


def test_launch_console_window_returns_false_on_immediate_exit(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    diag = ConsoleDiagnostic(
        state=ConsoleState.HEALTHY,
        public_host="chief.local",
        dashboard_port=8765,
        tls_mode="LOCAL",
        console_url="https://chief.local/",
        backend_active=True,
        tls_proxy_active=True,
        cert_valid=True,
        message="",
        remediation="",
    )
    monkeypatch.setattr("deployment.installer.console.find_desktop_runner", lambda *a, **kw: "/bin/false")

    class DummyProc:
        def wait(self, timeout=None):
            return 1  # Exited with error

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: DummyProc())
    assert launch_console_window(diag, paths) is False


def test_build_app_mode_command_preserves_desktop_isolation_and_loopback_rules(tmp_path):
    runner = "/usr/bin/chromium" if os.name != "nt" else r"C:\Program Files\Edge\msedge.exe"
    console_url = "https://chief-bigi.local/"
    public_host = "chief-bigi.local"
    profile_dir = tmp_path / "console_profile"

    cmd = build_app_mode_command(runner, console_url, public_host, profile_dir)

    assert cmd[0] == runner
    assert cmd[1] == "--app=https://chief-bigi.local/"
    assert f"--user-data-dir={profile_dir}" in cmd
    assert "--host-resolver-rules=MAP chief-bigi.local 127.0.0.1" in cmd
    assert "--class=ChiefConsole" in cmd
    assert "--app-id=ChiefConsole" in cmd
