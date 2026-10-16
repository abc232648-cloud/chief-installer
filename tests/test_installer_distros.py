"""Tests for Linux distribution detection and compatibility profiles (Xubuntu, Ubuntu, Kali)."""
from pathlib import Path
import pytest

from deployment.installer.platforms.linux import LinuxPlatformAdapter


def test_xubuntu_detection(tmp_path, monkeypatch):
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'NAME="Xubuntu"\n'
        'VERSION="24.04.1 LTS (Noble Numbat)"\n'
        'ID=xubuntu\n'
        'ID_LIKE="ubuntu debian"\n'
        'VERSION_ID="24.04"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "__new__", lambda cls, *args: os_release if args and "/etc/os-release" in str(args[0]) else super(Path, cls).__new__(cls, *args))

    adapter = LinuxPlatformAdapter()
    info = adapter.get_platform_info()
    assert info.distro_id == "xubuntu"
    assert info.distro_name == "Xubuntu"
    assert info.is_supported is True


def test_ubuntu_detection(tmp_path, monkeypatch):
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'NAME="Ubuntu"\n'
        'VERSION="24.04.1 LTS (Noble Numbat)"\n'
        'ID=ubuntu\n'
        'ID_LIKE=debian\n'
        'VERSION_ID="24.04"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "__new__", lambda cls, *args: os_release if args and "/etc/os-release" in str(args[0]) else super(Path, cls).__new__(cls, *args))

    adapter = LinuxPlatformAdapter()
    info = adapter.get_platform_info()
    assert info.distro_id == "ubuntu"
    assert info.distro_name == "Ubuntu"
    assert info.is_supported is True


def test_kali_detection(tmp_path, monkeypatch):
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'NAME="Kali GNU/Linux"\n'
        'ID=kali\n'
        'ID_LIKE=debian\n'
        'VERSION="2024.3"\n'
        'VERSION_ID="2024.3"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "__new__", lambda cls, *args: os_release if args and "/etc/os-release" in str(args[0]) else super(Path, cls).__new__(cls, *args))

    adapter = LinuxPlatformAdapter()
    info = adapter.get_platform_info()
    assert info.distro_id == "kali"
    assert "Kali" in info.distro_name
    assert info.is_supported is True


def test_unsupported_distro_detection(tmp_path, monkeypatch):
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'NAME="Fedora Linux"\n'
        'ID=fedora\n'
        'VERSION="40"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "__new__", lambda cls, *args: os_release if args and "/etc/os-release" in str(args[0]) else super(Path, cls).__new__(cls, *args))

    adapter = LinuxPlatformAdapter()
    info = adapter.get_platform_info()
    assert info.distro_id == "fedora"
    assert info.is_supported is False
