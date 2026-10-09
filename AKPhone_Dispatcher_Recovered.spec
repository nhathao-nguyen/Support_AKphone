import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

project = Path(SPECPATH)
src_dir = project / 'src'

if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

datas, binaries, hiddenimports = collect_all('customtkinter')
hiddenimports += collect_submodules('akphone_dispatcher')

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
