"""Tests for local hostname resolution, /etc/hosts loopback mapping, NSS configuration, and Avahi contract."""
from pathlib import Path
import pytest

from deployment.installer.privileged.linux import (
    _update_hosts_file,
    _update_nsswitch_file,
    run_configure_avahi,
)


def test_update_hosts_file_adds_canonical_host(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n::1 localhost ip6-localhost\n", encoding="utf-8")

    _update_hosts_file(hosts, "chief-bigi-ventures.local")

    content = hosts.read_text(encoding="utf-8")
    assert "127.0.0.1\tchief-bigi-ventures.local" in content
    assert "localhost" in content


def test_update_hosts_file_is_idempotent(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")

    _update_hosts_file(hosts, "chief-farm.local")
    _update_hosts_file(hosts, "chief-farm.local")

    content = hosts.read_text(encoding="utf-8")
    assert content.count("chief-farm.local") == 1


def test_update_hosts_file_replaces_old_chief_alias(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tchief-old-farm.local\n", encoding="utf-8")

    _update_hosts_file(hosts, "chief-new-farm.local")

    content = hosts.read_text(encoding="utf-8")
    assert "chief-new-farm.local" in content
    assert "chief-old-farm.local" not in content


def test_update_hosts_file_fails_closed_on_write_error(tmp_path, monkeypatch):
    """Verify that if updating /etc/hosts fails, it raises OSError and run_configure_avahi returns 1."""
    unwritable = tmp_path / "non_existent_dir" / "hosts"

    with pytest.raises(OSError) as exc_info:
        _update_hosts_file(unwritable, "chief.local")
    assert "Failed to update hosts file" in str(exc_info.value)

    conf = tmp_path / "avahi-daemon.conf"
    conf.write_text("[server]\n", encoding="utf-8")

    rc = run_configure_avahi(
        "chief-error-test",
        conf_path_override=conf,
        hosts_path_override=unwritable,
    )
    assert rc == 1  # Fails closed, does not silently continue to 0


def test_update_nsswitch_file_adds_mdns_if_missing(tmp_path):
    nss = tmp_path / "nsswitch.conf"
    nss.write_text("passwd: files systemd\nhosts: files dns\nnetworks: files\n", encoding="utf-8")

    _update_nsswitch_file(nss)

    content = nss.read_text(encoding="utf-8")
    assert "hosts: files mdns4_minimal [NOTFOUND=return] dns" in content


def test_update_nsswitch_file_leaves_existing_mdns_unchanged(tmp_path):
    nss = tmp_path / "nsswitch.conf"
    original = "hosts: files mdns4_minimal [NOTFOUND=return] dns myhostname\n"
    nss.write_text(original, encoding="utf-8")

    _update_nsswitch_file(nss)

    content = nss.read_text(encoding="utf-8")
    assert content == original


def test_run_configure_avahi_updates_conf_and_hosts(tmp_path, monkeypatch):
    conf = tmp_path / "avahi-daemon.conf"
    conf.write_text("[server]\nuse-ipv4=yes\n", encoding="utf-8")
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    nss = tmp_path / "nsswitch.conf"
    nss.write_text("hosts: files dns\n", encoding="utf-8")

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0))

    rc = run_configure_avahi(
        "chief-test-host",
        conf_path_override=conf,
        hosts_path_override=hosts,
        nsswitch_path_override=nss,
    )
    assert rc == 0
    assert "host-name=chief-test-host" in conf.read_text(encoding="utf-8")
    assert "127.0.0.1\tchief-test-host.local" in hosts.read_text(encoding="utf-8")
    assert "mdns4_minimal" in nss.read_text(encoding="utf-8")


def test_local_vs_remote_name_resolution_contract(tmp_path, monkeypatch):
    """Verify local loopback resolution in /etc/hosts vs LAN mDNS publishing in avahi-daemon.conf."""
    label = "chief-bigi-ventures"
    conf = tmp_path / "avahi-daemon.conf"
    conf.write_text("[server]\nuse-ipv4=yes\n", encoding="utf-8")
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0))

    rc = run_configure_avahi(
        label,
        conf_path_override=conf,
        hosts_path_override=hosts,
    )
    assert rc == 0

    # 1. Local resolution: /etc/hosts guarantees immediate 127.0.0.1 resolution
    hosts_content = hosts.read_text(encoding="utf-8")
    lines = [line.strip().split() for line in hosts_content.splitlines() if line.strip()]
    loopback_mappings = {p: parts[0] for parts in lines for p in parts[1:]}
    assert loopback_mappings.get(f"{label}.local") == "127.0.0.1"

    # 2. Remote resolution: avahi-daemon.conf advertises the host label to LAN Wi-Fi devices
    conf_content = conf.read_text(encoding="utf-8")
    assert f"host-name={label}" in conf_content
    # Avahi does not publish 127.0.0.1 over mDNS
    assert "127.0.0.1" not in conf_content
