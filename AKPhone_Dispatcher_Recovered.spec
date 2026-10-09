# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project = Path(SPECPATH)
src_dir = project / 'src'

datas, binaries, hiddenimports = collect_all('customtkinter')

# Include akphone_dispatcher package
hiddenimports += [
    'akphone_dispatcher',
    'akphone_dispatcher.core',
    'akphone_dispatcher.core.config',
    'akphone_dispatcher.core.models',
    'akphone_dispatcher.core.payload',
    'akphone_dispatcher.core.scanner',
    'akphone_dispatcher.services',
    'akphone_dispatcher.services.adb_service',
    'akphone_dispatcher.services.notifier',
    'akphone_dispatcher.services.session',
    'akphone_dispatcher.gui',
    'akphone_dispatcher.gui.app',
    'akphone_dispatcher.gui.workers.feeder_worker',
    'akphone_dispatcher.utils',
    'akphone_dispatcher.utils.helpers',
    'akphone_dispatcher.utils.logger',
]

a = Analysis(
    [str(project / 'main.py')],
    pathex=[str(src_dir), str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='AKPhone_Dispatcher_Recovered',
    console=False,
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
)
