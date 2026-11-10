# PyInstaller build recipe -> dist\WordCounter\WordCounter.exe
# Build with:  pyinstaller WordCounter.spec --noconfirm --clean
# (run  python tools/get_model.py  first so the speech model is included)

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

hidden, binaries = [], []
datas = [("assets/icon.png", "assets"), ("assets/icon.ico", "assets"), ("models", "models")]
for pkg in ("faster_whisper", "soundcard", "ctranslate2"):
    try:
        datas += collect_data_files(pkg)
        binaries += collect_dynamic_libs(pkg)
        if pkg in ("faster_whisper", "soundcard"):
            hidden += collect_submodules(pkg)
    except Exception as e:
        print(pkg, "collection skipped:", e)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "torch", "tensorflow",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WordCounter",
    icon="assets/icon.ico",
    console=False,
    upx=False,
    version="version_info.txt",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="WordCounter")
