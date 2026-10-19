# PyInstaller spec: builds dist/SubredditSentiment/ (a folder with SubredditSentiment.exe).
# Run from the repo root:  pyinstaller packaging/subsentiment.spec --noconfirm
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = SPECPATH  # noqa: F821 (provided by PyInstaller)

datas = collect_data_files("subsentiment") + collect_data_files("vaderSentiment")
hiddenimports = collect_submodules("keyring.backends")  # keyring picks its backend at runtime

a = Analysis(
    [os.path.join(HERE, "launch.py")],
    pathex=[],
    datas=datas,
    hiddenimports=hiddenimports,
    # Qt modules the app never uses; keeps the download smaller.
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SubredditSentiment",
    icon=os.path.join(HERE, "..", "src", "subsentiment", "gui", "assets", "app.ico"),
    console=False,  # GUI app: no console window
    version=os.path.join(HERE, "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="SubredditSentiment")
