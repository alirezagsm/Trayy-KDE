#!/usr/bin/env bash
set -euo pipefail

REPO="alirezagsm/Trayy-KDE"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/trayy}"
DESKTOP_FILE="$HOME/.local/share/applications/trayy.desktop"

command -v curl >/dev/null || { echo "error: curl not found"; exit 1; }
command -v unzip >/dev/null || { echo "error: unzip not found (dnf install unzip)"; exit 1; }

echo ">> fetching latest release..."
API="https://api.github.com/repos/$REPO/releases/latest"
ZIP_URL=$(curl -sL -H "User-Agent: Trayy-Installer" "$API" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['assets'][0]['browser_download_url'])")
TAG=$(curl -sL -H "User-Agent: Trayy-Installer" "$API" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])")
echo ">> latest release: $TAG"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo ">> downloading Trayy.zip..."
curl -sL -H "User-Agent: Trayy-Installer" -o "$TMP/Trayy.zip" "$ZIP_URL"

echo ">> installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
unzip -o "$TMP/Trayy.zip" -d "$INSTALL_DIR"
for f in trayy.pyz trayyd; do
    [ -f "$INSTALL_DIR/$f" ] || { echo "error: $f not found in release zip"; exit 1; }
    chmod +x "$INSTALL_DIR/$f"
done

ICON_URL="https://raw.githubusercontent.com/$REPO/main/media/logo.png"
curl -sL -H "User-Agent: Trayy-Installer" -o "$INSTALL_DIR/trayy.png" "$ICON_URL" \
    || echo "warning: could not download icon — desktop entry will use a fallback icon"

echo ">> creating desktop entry..."
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Trayy
Comment=Send apps to the system tray
Exec=$INSTALL_DIR/trayy.pyz
Icon=$INSTALL_DIR/trayy.png
Terminal=false
Categories=Utility;
StartupNotify=false
EOF

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo ">> done! Trayy is installed at $INSTALL_DIR"
echo "   Launch it from your app menu, or run: $INSTALL_DIR/trayy.pyz"
