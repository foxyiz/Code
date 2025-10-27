# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['fEngine.py'],
    pathex=[],
    binaries=[],
    datas=[('x/xActions.py', 'x'), ('y', 'y'), ('z/zDash_template.html', 'z')],
    hiddenimports=['pandas', 'x.xActions', 'numpy', 'selenium', 'requests', 'urllib3', 'requests.adapters', 'requests.auth', 'requests.cookies', 'requests.exceptions', 'requests.sessions', 'requests.utils', 'multiprocessing.spawn', 'multiprocessing.semaphore_tracker'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Foxyiz',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
