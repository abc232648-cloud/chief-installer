# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for native Chief Windows Installer executable."""
import os
import sys
from pathlib import Path

block_cipher = None

project_root = Path(SPECPATH).resolve().parents[2]

a = Analysis(
    [str(project_root / 'deployment' / 'installer' / '__main__.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'deployment.installer',
        'deployment.installer.contract',
        'deployment.installer.models',
        'deployment.installer.errors',
        'deployment.installer.events',
        'deployment.installer.state',
        'deployment.installer.manifest',
        'deployment.installer.logging',
        'deployment.installer.orchestrator',
        'deployment.installer.cli',
        'deployment.installer.platforms.base',
        'deployment.installer.platforms.windows',
        'deployment.installer.ui.app',
        'deployment.installer.ui.controller',
        'zipfile',
        'xml.etree.ElementTree',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', '_pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Chief-Installer-Windows',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
