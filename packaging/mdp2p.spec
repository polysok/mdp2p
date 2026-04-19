# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the mdp2p end-user binary.

Produces a single-file executable bundling the TUI, CLI, libp2p,
cryptography, and the Markdown reader — no Python install required.

Build (from repo root):
    pyinstaller packaging/mdp2p.spec --clean --noconfirm

Output: dist/mdp2p  (or dist/mdp2p.exe on Windows)
"""

import os

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# Paths resolved against the spec file, not the current working directory,
# so the build works whether launched from repo root or from packaging/.
HERE = os.path.dirname(os.path.abspath(SPEC))  # noqa: F821 — SPEC injected by PyInstaller
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))

# CFFI-based and native-extension packages need their dynamic libs and
# compiled _cffi_backend modules copied in — collect_all handles all of
# data files + submodules + dynamic libs in one call.
_cffi_datas, _cffi_binaries, _cffi_hiddens = collect_all("coincurve")
_cffi2_datas, _cffi2_binaries, _cffi2_hiddens = collect_all("cffi")

hidden_imports = (
    collect_submodules("libp2p")
    + collect_submodules("trio")
    + collect_submodules("multiaddr")
    + collect_submodules("textual")
    + collect_submodules("rich")
    + collect_submodules("cryptography")
    + collect_submodules("mdp2p_client")
    + collect_submodules("bundle")
    + collect_submodules("peer")
    + _cffi_hiddens
    + _cffi2_hiddens
    # Root-level modules declared as py-modules in pyproject.toml: PyInstaller
    # may miss them because they are not imported from a package __init__.
    + [
        "naming",
        "peer_zero",
        "pinstore",
        "wire",
        "mdp2p_logging",
    ]
)

datas = (
    # Textual ships CSS/TCSS theme files as package data.
    collect_data_files("textual")
    # Our own locale bundles (fr/en/zh/ar/hi).
    + collect_data_files("mdp2p_client", includes=["locales/*.json"])
    + _cffi_datas
    + _cffi2_datas
)

binaries = _cffi_binaries + _cffi2_binaries

a = Analysis(
    [os.path.join(HERE, "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI toolkits we never use; skipping trims ~15 MB on macOS.
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        # Dev-only; don't need to ship the test runner.
        "pytest",
        "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mdp2p",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX breaks macOS codesigning and Windows Defender often flags it.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
