"""Verify the distributable package without installing or launching Chief."""
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest

spec=importlib.util.spec_from_file_location('linux_desktop_build',Path(__file__).resolve().parents[1]/'packaging/installer/linux/build_package.py')
build=importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_rejects_development_zipapp(tmp_path):
    executable=tmp_path/'chief-installer'
    executable.write_bytes(b'#!/usr/bin/env python3\n')
    with pytest.raises(ValueError,match='native Linux x86-64'):
        build.build_desktop_package(executable)


@pytest.mark.skipif(not shutil.which('dpkg-deb'),reason='Debian package tools required')
def test_desktop_package_contains_launchable_application_without_install_hooks(tmp_path):
    executable=tmp_path/'chief-installer'
    executable.write_bytes(b'\x7fELF\x02\x01'+b'\0'*12+b'\x3e\0'+b'fixture only')
    package=build.build_desktop_package(executable)
    extracted=tmp_path/'extracted'
    subprocess.run(['dpkg-deb','--raw-extract',str(package),str(extracted)],check=True)
    binary=extracted/'opt/chief-installer/chief-installer'
    assert binary.read_bytes()==executable.read_bytes()
    assert binary.stat().st_mode & 0o111 == 0o111
    desktop=(extracted/'usr/share/applications/chief-installer.desktop').read_text()
    assert 'Exec=/opt/chief-installer/chief-installer --gui\n' in desktop
    assert 'Terminal=false' in desktop
    assert 'MimeType=' not in desktop
    assert {p.name for p in (extracted/'DEBIAN').iterdir()}=={'control'}
    assert package.with_suffix('.deb.sha256').read_text()==build.sha256_file(package)+'  '+package.name+'\n'
