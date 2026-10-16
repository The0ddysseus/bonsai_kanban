#! /bin/bash
# The AppImage's entry point (python-appimage fills in the {{ }} parts).
#   Bonsai-x86_64.AppImage              run Bonsai
#   Bonsai-x86_64.AppImage --install    add it to the application menu
#   Bonsai-x86_64.AppImage --uninstall  remove it from the menu again
# The menu entry runs the AppImage from wherever it is when --install is
# run, so move it somewhere permanent (e.g. ~/Applications) first.

APP_ID="bonsai-kanban"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"

refresh_caches() {
    update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true
    gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
}

case "$1" in
--install)
    if [ -z "$APPIMAGE" ]; then
        echo "Run --install from the .AppImage file itself." >&2
        exit 1
    fi
    mkdir -p "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")"
    cp "$APPDIR/$APP_ID.svg" "$ICON_FILE"
    sed -e "s|^Exec=.*|Exec=\"$APPIMAGE\"|" \
        -e "/^X-AppImage/d" \
        "$APPDIR/$APP_ID.desktop" > "$DESKTOP_FILE"
    echo "TryExec=$APPIMAGE" >> "$DESKTOP_FILE"
    refresh_caches
    echo "Installed: $DESKTOP_FILE"
    echo "Bonsai should now appear in your application menu (under Office)."
    echo "If you move $(basename "$APPIMAGE"), run --install again from its new place."
    ;;
--uninstall)
    rm -f "$DESKTOP_FILE" "$ICON_FILE"
    refresh_caches
    echo "Removed the Bonsai menu entry. (Your boards are untouched.)"
    ;;
*)
    exec {{ python-executable }} -I -m bonsai_kanban.main "$@"
    ;;
esac
