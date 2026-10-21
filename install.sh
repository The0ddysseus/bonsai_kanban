#!/bin/sh
# Adds Bonsai to your desktop's application menu (no root needed). Works
# with any desktop that follows the freedesktop.org standards (GNOME, KDE,
# Cinnamon, XFCE, MATE, ...).
#   ./install.sh              install / update the launcher
#   ./install.sh --alerts     install / update the meeting alerter's service
#   ./install.sh --uninstall  remove both again
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
# Always here, rather than under $XDG_CONFIG_HOME: that's where systemd looks
# even if XDG_CONFIG_HOME is set differently in this terminal (e.g. one in a
# Flatpak'd editor).
SERVICE="bonsai-alerts.service"
SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE"

refresh_caches() {
    update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true
    gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
}

if [ "$1" = "--uninstall" ]; then
    # shellcheck disable=SC2086  # OLD_FILES is a list of paths
    rm -f "$DESKTOP_FILE" "$ICON_FILE" $OLD_FILES
    refresh_caches
    echo "Removed the Bonsai launcher. (Your boards are untouched.)"
    if [ -f "$SERVICE_FILE" ]; then
        systemctl --user disable --now "$SERVICE" 2>/dev/null || true
        rm -f "$SERVICE_FILE"
        systemctl --user daemon-reload
        echo "Removed the meeting alerts service. (Its settings in ~/.config/$APP_ID are kept.)"
    fi
    exit 0
fi

if [ "$1" = "--alerts" ]; then
    if [ -x "$APP_DIR/venv/bin/python" ]; then
        EXEC="\"$APP_DIR/venv/bin/python\" \"$APP_DIR/alerter.py\""
    elif INSTALLED="$(command -v bonsai-alerts)"; then
        EXEC="\"$INSTALLED\""
    else
        echo "Bonsai isn't set up yet. Either install it:"
        echo "  pipx install '.[alerts]'"
        echo "or make a venv in this folder:"
        echo "  python3 -m venv venv && venv/bin/pip install -r requirements.txt piper-tts"
        echo "then run ./install.sh --alerts again."
        exit 1
    fi
    if ! command -v systemctl > /dev/null; then
        echo "systemctl isn't available here. Run this from a normal terminal (not one"
        echo "inside a sandboxed app such as a Flatpak editor)."
        exit 1
    fi
    mkdir -p "$(dirname "$SERVICE_FILE")"
    sed "s|@EXEC@|$EXEC|" "$APP_DIR/packaging/systemd/$SERVICE" > "$SERVICE_FILE"
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE"
    systemctl --user restart "$SERVICE"  # starts it, or picks up a new version
    echo "Installed and started: $SERVICE_FILE"
    echo "Meeting alerts now run whenever you're logged in. Next:"
    echo "  - settings: copy alerts.example.toml to ~/.config/$APP_ID/alerts.toml"
    echo "  - a voice for the speech: see \"Spoken meeting alerts\" in the README"
    echo "  - try it:  journalctl --user -u bonsai-alerts -f   (its log)"
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
