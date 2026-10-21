"""Tests for local hostname resolution, /etc/hosts loopback mapping, and NSS configuration."""
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

    # Mock subprocess.run for systemctl commands
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
