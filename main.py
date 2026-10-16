import sys
from pathlib import Path

# When installed as a package (pipx install .), these modules live inside
# bonsai_kanban/ but import each other by plain name (import database), the
# same as when run from the project folder. This makes that work in both.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

import database as db
import theme
from main_window import MainWindow

# Must match the launcher installed by install.sh (bonsai-kanban.desktop) so
# the desktop pairs the running window with it, rather than showing a
# generic Python icon in the taskbar.
APP_ID = "bonsai-kanban"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "bonsai.svg"


def main():
    db.init_db()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)  # becomes the window's WM_CLASS on X11
    app.setDesktopFileName(APP_ID)  # used on Wayland
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    theme.apply()  # the saved light/dark/system choice, before any window shows
    window = MainWindow()
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
