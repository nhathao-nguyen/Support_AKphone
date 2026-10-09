from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project = Path(SPECPATH)
datas, binaries, hiddenimports = collect_all('customtkinter')
a = Analysis([str(project / 'generator_gui.py')], pathex=[str(project)],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
             noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='AKPhone_Dispatcher_Recovered', console=False, debug=False,
          strip=False, upx=False, runtime_tmpdir=None)
