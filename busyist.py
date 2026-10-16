r"""
buZyist: Todoist -> BUSY Bar pomodoros, as a Windows tray app.

    .venv\Scripts\pythonw.exe busyist.py      (or double-click run.bat)

The app is the buzyist package (CLAUDE.md maps its modules); this script
starts it and holds the version, which build.ps1, the PyInstaller spec, the
Inno script and the release workflow read from the line below.
"""

import sys

__version__ = "2.6.1"

if __name__ == "__main__":
    sys.modules.setdefault("busyist", sys.modules[__name__])  # app_version() reads this copy, not a second

from buzyist.app import App
from buzyist.main import main

__all__ = ["App", "__version__", "main"]

if __name__ == "__main__":
    main()
