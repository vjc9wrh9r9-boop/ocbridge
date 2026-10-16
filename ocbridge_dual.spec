# -*- mode: python ; coding: utf-8 -*-
# OCBridge 双 exe 单目录打包: GUI(windowed) + CLI(console) 共享 _internal 运行时
# 相比 onefile: 启动/子进程秒开 (无每次解压), 两个 exe 合一目录
a = Analysis(
    ['ocbridge_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'matplotlib', 'pandas', 'scipy', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='OCBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon='OCBridge.ico',
)
exe_cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='OCBridge-CLI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon='OCBridge.ico',
)
coll = COLLECT(
    exe_gui,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='OCBridge',
)
