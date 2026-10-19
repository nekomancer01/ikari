# PyInstaller spec for all three platforms. Usually run through packaging/build.py, which also
# makes the zip/installer, .dmg or AppImage. Direct use, from the repo root:
#   pyinstaller packaging/subsentiment.spec --noconfirm
# Output:
#   Windows: dist/ikari/ikari.exe
#   macOS:   dist/ikari.app
#   Linux:   dist/ikari/ikari
import os
import re
import sys

from PyInstaller.utils.hooks import collect_data_files

HERE = SPECPATH  # noqa: F821 (provided by PyInstaller)
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "src", "subsentiment", "gui", "assets")

with open(os.path.join(ROOT, "src", "subsentiment", "__init__.py"), encoding="utf-8") as f:
    VERSION = re.search(r'__version__ = "([^"]+)"', f.read()).group(1)

datas = collect_data_files("subsentiment") + collect_data_files("vaderSentiment")
hiddenimports = []
if sys.platform == "darwin":
    hiddenimports += ["objc", "AppKit", "Foundation"]  # gui/native/macos.py imports these lazily


def windows_version_file():
    """Write the .exe's file-properties resource for this version; return its path."""
    nums = (tuple(int(n) for n in re.findall(r"\d+", VERSION)) + (0, 0, 0, 0))[:4]
    path = os.path.join(workpath, "version_info.txt")  # noqa: F821 (provided by PyInstaller)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable("040904B0", [
      StringStruct("FileDescription", "ikari"),
      StringStruct("FileVersion", "{VERSION}"),
      StringStruct("InternalName", "ikari"),
      StringStruct("OriginalFilename", "ikari.exe"),
      StringStruct("ProductName", "ikari"),
      StringStruct("ProductVersion", "{VERSION}")])]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])])
  ]
)
""")
    return path


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

exe_options = {}
if sys.platform == "win32":
    exe_options = {"icon": os.path.join(ASSETS, "app.ico"), "version": windows_version_file()}
elif sys.platform == "darwin":
    exe_options = {"icon": os.path.join(ASSETS, "app.icns")}

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ikari",
    console=False,  # GUI app: no console window
    **exe_options,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ikari")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ikari.app",
        icon=os.path.join(ASSETS, "app.icns"),
        bundle_identifier="io.github.nekomancer01.ikari",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "ikari",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,  # allow dark mode
        },
    )
