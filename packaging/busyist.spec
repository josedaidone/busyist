# PyInstaller spec for Busyist. Built by build.ps1:
#   pyinstaller packaging/busyist.spec --noconfirm --distpath dist --workpath build
# A one-folder build: it starts faster than --onefile and trips fewer
# antivirus heuristics, and the installer ships the folder.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([\d.]+)"', (ROOT / "busyist.py").read_text(encoding="utf-8")).group(1)
NUMS = tuple(int(n) for n in (VERSION.split(".") + ["0", "0", "0"])[:4])

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMS, prodvers=NUMS),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Busyist"),
            StringStruct("FileDescription", "Busyist - Todoist pomodoros on the BUSY Bar"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "Busyist"),
            StringStruct("LegalCopyright", "MIT License"),
            StringStruct("OriginalFilename", "Busyist.exe"),
            StringStruct("ProductName", "Busyist"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "busyist.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "ui"), "ui"),
        (str(ROOT / "busyist.ico"), "."),
        *collect_data_files("webview"),  # its JS and the WebView2 loader DLLs
    ],
    hiddenimports=[
        *collect_submodules("busylib"),
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "clr",
        "pystray._win32",
    ],
    excludes=[
        # Other GUI back ends pywebview could use; Windows only needs WinForms.
        "tkinter", "webview.platforms.qt", "webview.platforms.gtk",
        "webview.platforms.cocoa", "webview.platforms.cef", "webview.platforms.android",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "gi",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Busyist",
    icon=str(ROOT / "busyist.ico"),
    version=version_info,
    console=False,
    upx=False,  # UPX-packed exes are a classic antivirus false positive
)
coll = COLLECT(exe, a.binaries, a.datas, name="Busyist", upx=False)
