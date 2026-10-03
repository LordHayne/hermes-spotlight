#!/usr/bin/env bash
# hermes-spotlight uninstaller
set -euo pipefail
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }

rm -f "$HOME/.local/bin/hermes-spotlight" && ok "Removed ~/.local/bin/hermes-spotlight"
rm -f "$HOME/.local/share/applications/hermes-spotlight.desktop" && ok "Removed menu entry"

# COSMIC shortcut
SC="$HOME/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom.ron"
if [ -f "$SC" ] && grep -q "Hermes Spotlight" "$SC"; then
    grep -v "Hermes Spotlight" "$SC" > "$SC.tmp" || true
    # keep valid map braces even if now empty
    if ! grep -q "Spawn\|System\|Close" "$SC.tmp"; then
        echo '{' > "$SC.tmp"
        echo '}' >> "$SC.tmp"
    fi
    mv "$SC.tmp" "$SC"
    ok "Removed COSMIC shortcut line"
fi

# KDE
rm -f "$HOME/.local/share/khotkeys/hermes-spotlight.desktop" 2>/dev/null && ok "Removed KDE shortcut file"

# config/cache are kept on purpose (your conversations survive uninstall)
echo
ok "Uninstalled. Config kept at ~/.config/hermes-spotlight/ (delete manually if you want)."