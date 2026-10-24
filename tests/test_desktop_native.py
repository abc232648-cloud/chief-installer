"""Actual hidden Electron/Chromium acceptance against disposable local HTTPS."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from deployment.installer.process import run


def test_native_desktop_uses_verified_tls_and_sandboxed_renderer(tmp_path):
    engine = os.environ.get('CHIEF_DESKTOP_ENGINE')
    if not engine:
        pytest.skip('Set CHIEF_DESKTOP_ENGINE to the checksum-verified native Electron engine.')
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    now = datetime.now(timezone.utc)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Disposable Chief desktop test CA')])
    root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(root_key, hashes.SHA256()))
    host = 'chief-desktop-test.local'
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(root_name).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(root_key, hashes.SHA256()))
    ca = tmp_path / 'ca.crt'; ca.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    cert = tmp_path / 'leaf.crt'; cert.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key = tmp_path / 'leaf.key'
    key.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    key.chmod(0o600)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<!doctype html><title>Chief native acceptance</title><h1>Chief Agent</h1>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Security-Policy', "default-src 'none'")
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    class TLSServer(ThreadingHTTPServer):
        def process_request_thread(self, request, client_address):
            # Browsers may preconnect without immediately sending a handshake.
            # Never block accepting the independent certificate-verification connection.
            try:
                request.settimeout(5)
                request = context.wrap_socket(request, server_side=True)
                super().process_request_thread(request, client_address)
            except (ssl.SSLError, OSError):
                request.close()
    server = TLSServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    app = Path(__file__).resolve().parents[1] / 'deployment/installer/desktop'
    settings = {'version': 1, 'origin': f'https://{host}:{server.server_port}',
                'stateRoot': str(tmp_path / 'state'), 'installationRoot': str(tmp_path / 'chief'),
                'home': str(tmp_path), 'helper': str(tmp_path / 'never-invoked-helper'), 'localCA': str(ca)}
    cfg = tmp_path / 'desktop.json'; cfg.write_text(json.dumps(settings))
    environment = dict(os.environ)
    for name in ('ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS', 'NODE_PATH'):
        environment.pop(name, None)
    try:
        engine_args = [engine]
        if os.name != 'nt' and os.environ.get('WAYLAND_DISPLAY'):
            engine_args.append('--ozone-platform=wayland')
        result = run([*engine_args, str(app), '--chief-config', str(cfg), '--smoke-test'],
                     capture_output=True, text=True, env=environment, timeout=50)
        assert result.returncode == 0, (result.stdout, result.stderr)
        status = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))
        assert status['status'] == 'SUCCESS' and status['nodeExposed'] is False
        assert status['electron'] == '45.0.0'
        assert status['chromium'].startswith('156.')
        # A different CA must not turn the same origin into a trusted connection.
        wrong_ca = tmp_path / 'wrong.crt'; wrong_ca.write_text('invalid CA')
        settings['localCA'] = str(wrong_ca)
        settings['stateRoot'] = str(tmp_path / 'other-state')
        cfg.write_text(json.dumps(settings))
        rejected = run([*engine_args, str(app), '--chief-config', str(cfg), '--smoke-test'],
                       capture_output=True, text=True, env=environment, timeout=50)
        assert rejected.returncode != 0, (rejected.stdout, rejected.stderr)
        assert '"status":"SUCCESS"' not in rejected.stdout
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)
