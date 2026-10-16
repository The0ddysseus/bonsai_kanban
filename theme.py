"""
Light / dark / match-system theme.

The app always uses Qt's Fusion style, with one of the two palettes below.
"system" picks whichever matches the desktop's light/dark preference, and
follows it if it changes while the app is open. The choice is saved in the settings table.

Cards are white in both themes on purpose (like paper); everything else
-- columns, toolbar, dialogs, the Gantt chart -- follows the palette.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

import database as db

SETTING = "theme"
MODES = {"system": "Match system", "light": "Light", "dark": "Dark"}

_mode = "system"
_following_system = False


def _palette(colors: dict, disabled_text: str, disabled_button: str) -> QPalette:
    palette = QPalette()
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(disabled_text))
    palette.setColor(QPalette.Disabled, QPalette.Button, QColor(disabled_button))
    return palette


# Spelled out in full rather than taken from Qt: Qt's "standard" Fusion
# palette follows the desktop's dark/light setting, so it can't be relied
# on to be light.
LIGHT = _palette(
    {
        QPalette.Window: "#efefef",
        QPalette.WindowText: "#1d2939",
        QPalette.Base: "#ffffff",
        QPalette.AlternateBase: "#f7f7f7",
        QPalette.ToolTipBase: "#ffffff",
        QPalette.ToolTipText: "#1d2939",
        QPalette.PlaceholderText: "#8a8a8a",
        QPalette.Text: "#1d2939",
        QPalette.Button: "#f4f4f4",
        QPalette.ButtonText: "#1d2939",
        QPalette.BrightText: "#d92d20",
        QPalette.Light: "#ffffff",
        QPalette.Midlight: "#e3e3e3",
        QPalette.Mid: "#b8b8b8",
        QPalette.Dark: "#9f9f9f",
        QPalette.Shadow: "#767676",
        QPalette.Link: "#1570ef",
        QPalette.LinkVisited: "#7a5af8",
        QPalette.Highlight: "#2f6fd6",
        QPalette.HighlightedText: "#ffffff",
    },
    disabled_text="#9a9a9a",
    disabled_button="#e8e8e8",
)

DARK = _palette(
    {
        QPalette.Window: "#2b2b2b",
        QPalette.WindowText: "#e6e6e6",
        QPalette.Base: "#1f1f1f",
        QPalette.AlternateBase: "#2b2b2b",
        QPalette.ToolTipBase: "#3a3a3a",
        QPalette.ToolTipText: "#e6e6e6",
        QPalette.PlaceholderText: "#8a8a8a",
        QPalette.Text: "#e6e6e6",
        QPalette.Button: "#353535",
        QPalette.ButtonText: "#e6e6e6",
        QPalette.BrightText: "#ff6b6b",
        QPalette.Light: "#4a4a4a",
        QPalette.Midlight: "#3e3e3e",
        QPalette.Mid: "#2f2f2f",
        QPalette.Dark: "#1a1a1a",
        QPalette.Shadow: "#000000",
        QPalette.Link: "#5b9bff",
        QPalette.LinkVisited: "#a78bfa",
        QPalette.Highlight: "#2f6fd6",
        QPalette.HighlightedText: "#ffffff",
    },
    disabled_text="#7a7a7a",
    disabled_button="#2f2f2f",
)


def _system_is_dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _apply_palette():
    app = QApplication.instance()
    dark = _mode == "dark" or (_mode == "system" and _system_is_dark())
    app.setPalette(DARK if dark else LIGHT)
    # Widgets with their own style sheet keep the colours from when the sheet
    # was applied; re-applying it makes them pick up the new palette.
    for widget in app.allWidgets():
        if widget.styleSheet():
            widget.setStyleSheet(widget.styleSheet())


def current_mode() -> str:
    return _mode


def apply(mode: str | None = None) -> None:
    """Switch to "system", "light" or "dark" (default: the saved choice) and
    save it. Safe to call again at any time.
    """
    global _mode, _following_system
    app = QApplication.instance()
    if mode is None:
        mode = db.get_setting(SETTING, "system")
    _mode = mode if mode in MODES else "system"
    db.set_setting(SETTING, _mode)
    if app.style().name().lower() != "fusion":
        app.setStyle(QStyleFactory.create("Fusion"))
    if not _following_system:
        # Re-check whenever the desktop switches between light and dark;
        # only acts while "Match system" is chosen.
        QGuiApplication.styleHints().colorSchemeChanged.connect(
            lambda _scheme: _mode == "system" and _apply_palette()
        )
        _following_system = True
    _apply_palette()
