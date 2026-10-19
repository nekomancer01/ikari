#!/bin/sh
# Installs ikari for the current user (no root needed):
#   app      ~/.local/opt/ikari/
#   command  ~/.local/bin/ikari
#   menu     ~/.local/share/applications/io.github.nekomancer01.ikari.desktop
# Run ./install.sh --uninstall to remove it again. Your saved data is kept.
set -eu
ID=io.github.nekomancer01.ikari
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$HOME/.local/opt/ikari"
BIN="$HOME/.local/bin/ikari"
DESKTOP="$DATA/applications/$ID.desktop"
ICON="$DATA/icons/hicolor/512x512/apps/$ID.png"

refresh() {
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$DATA/applications" || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" || true
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$APP_DIR" "$BIN" "$DESKTOP" "$ICON"
    refresh
    echo "Removed ikari."
    exit 0
fi

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR" "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
cp -R "$HERE/ikari/." "$APP_DIR/"
ln -sf "$APP_DIR/ikari" "$BIN"
cp "$HERE/$ID.png" "$ICON"
sed "s|^Exec=.*|Exec=\"$APP_DIR/ikari\"|" "$HERE/$ID.desktop" > "$DESKTOP"
refresh
echo "Installed. Open ikari from your app menu, or run: $BIN"
