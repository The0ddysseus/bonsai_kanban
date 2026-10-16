#!/bin/sh
# Adds Bonsai to your desktop's application menu (no root needed). Works
# with any desktop that follows the freedesktop.org standards (GNOME, KDE,
# Cinnamon, XFCE, MATE, ...).
#   ./install.sh              install / update the launcher
#   ./install.sh --uninstall  remove it again
# The launcher runs the app either from a venv in this folder, or, if
# there's no venv, the "bonsai" command that "pipx install ." provides.
# Re-run this if you move the project folder or switch between the two.
set -e

APP_ID="bonsai-kanban"
OLD_ID="kanban-app"  # the launcher's name before the app was called Bonsai
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
OLD_FILES="$DATA_HOME/applications/$OLD_ID.desktop $DATA_HOME/icons/hicolor/scalable/apps/$OLD_ID.svg"

refresh_caches() {
    update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true
    gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
}

if [ "$1" = "--uninstall" ]; then
    # shellcheck disable=SC2086  # OLD_FILES is a list of paths
    rm -f "$DESKTOP_FILE" "$ICON_FILE" $OLD_FILES
    refresh_caches
    echo "Removed the Bonsai launcher. (Your boards are untouched.)"
    exit 0
fi

if [ -x "$APP_DIR/venv/bin/python" ]; then
    EXEC="\"$APP_DIR/venv/bin/python\" \"$APP_DIR/main.py\""
    WORK_DIR="$APP_DIR"
elif INSTALLED="$(command -v bonsai)"; then
    EXEC="\"$INSTALLED\""
    WORK_DIR=""
else
    echo "Bonsai isn't set up yet. Either install it:"
    echo "  pipx install ."
    echo "or make a venv in this folder:"
    echo "  python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    echo "then run ./install.sh again."
    exit 1
fi

mkdir -p "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")"
# shellcheck disable=SC2086
rm -f $OLD_FILES  # the old "Kanban" menu entry, so it isn't listed twice
cp "$APP_DIR/assets/bonsai.svg" "$ICON_FILE"

cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Bonsai
GenericName=Kanban Board
Comment=Kanban for Linux: boards, a Gantt timeline and calendar sync
Exec=$EXEC
Icon=$APP_ID
Terminal=false
Categories=Office;ProjectManagement;
Keywords=bonsai;kanban;tasks;todo;gantt;planner;board;
StartupNotify=true
StartupWMClass=$APP_ID
DESKTOP
if [ -n "$WORK_DIR" ]; then
    echo "Path=$WORK_DIR" >> "$DESKTOP_FILE"
fi

refresh_caches
echo "Installed: $DESKTOP_FILE"
echo "Bonsai should now appear in your application menu (under Office)."
