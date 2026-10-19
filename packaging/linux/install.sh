#!/bin/sh
# Installs Subreddit Sentiment for the current user (no root needed):
#   app      ~/.local/opt/SubredditSentiment/
#   command  ~/.local/bin/subreddit-sentiment
#   menu     ~/.local/share/applications/io.github.subsentiment.SubredditSentiment.desktop
# Run ./install.sh --uninstall to remove it again. Your saved data is kept.
set -eu
ID=io.github.subsentiment.SubredditSentiment
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$HOME/.local/opt/SubredditSentiment"
BIN="$HOME/.local/bin/subreddit-sentiment"
DESKTOP="$DATA/applications/$ID.desktop"
ICON="$DATA/icons/hicolor/512x512/apps/$ID.png"

refresh() {
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$DATA/applications" || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" || true
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$APP_DIR" "$BIN" "$DESKTOP" "$ICON"
    refresh
    echo "Removed Subreddit Sentiment."
    exit 0
fi

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR" "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
cp -R "$HERE/SubredditSentiment/." "$APP_DIR/"
ln -sf "$APP_DIR/SubredditSentiment" "$BIN"
cp "$HERE/$ID.png" "$ICON"
sed "s|^Exec=.*|Exec=\"$APP_DIR/SubredditSentiment\"|" "$HERE/$ID.desktop" > "$DESKTOP"
refresh
echo "Installed. Open Subreddit Sentiment from your app menu, or run: $BIN"
