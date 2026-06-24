# -*- mode: python ; coding: utf-8 -*-
#
# 打包配置：所有参数集中管理于此处，build.sh 通过 pyinstaller ...spec 引用。
# 修改打包选项（排除模块、隐藏导入、优化级别等）改这个文件即可。

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['PIL', 'fitz', 'numpy', 'PIL._imaging'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['scipy', 'scipy.spatial', 'scipy.optimize', 'scipy.signal',
              'scipy.linalg', 'scipy.stats', 'scipy.integrate',
              'pandas', 'pyarrow', 'pytz', 'dateutil', 'matplotlib',
              'setuptools', 'wheel', 'markupsafe', 'sklearn', 'cv2',
              'torch', 'tensorflow', 'sympy', 'statsmodels'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='A3转A4试卷转换',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,     # macOS 下 strip 会破坏 PIL/_imaging、numpy 扩展的 codesigning
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='A3转A4试卷转换',
)
app = BUNDLE(
    coll,
    name='A3转A4试卷转换.app',
    icon=None,
    bundle_identifier=None,
)