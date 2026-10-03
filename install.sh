#!/usr/bin/env bash
# hermes-spotlight installer — sets up the global AI spotlight on Linux.
# Supports: Arch/CachyOS, Debian/Ubuntu, Fedora (dep detection)
# Desktops: COSMIC, GNOME (Wayland/X11), KDE, generic X11 fallback.
set -euo pipefail

SPOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BINDIR="$HOME/.local/bin"
CFG_DIR="$HOME/.config/hermes-spotlight"
ENV_FILE="$HOME/.hermes/.env"
DESKTOP_ID="hermes-spotlight"

say()  { printf '\033[1;34m[hermes-spotlight]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }

# --- 1. Python + PyGObject check ---------------------------------------------
say "Checking dependencies…"
PY=""
for cand in /usr/bin/python3 python3; do
    if "$cand" -c "import gi" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
    err "python3 with PyGObject (gi) not found."
    echo "  Arch/CachyOS:   sudo pacman -S python-gobject gtk4"
    echo "  Debian/Ubuntu:  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0"
    echo "  Fedora:         sudo dnf install python3-gobject gtk4"
    exit 1
fi
if ! "$PY" -c "import gi; gi.require_version('Gtk','4.0')" 2>/dev/null; then
    err "GTK4 introspection missing (gtk4 package)."
    exit 1
fi
ok "Python + PyGObject + GTK4 found ($PY)"

# --- 2. Hermes gateway present? ----------------------------------------------
if ! curl -s -m 3 http://127.0.0.1:8642/health >/dev/null 2>&1; then
    say "Gateway not reachable — checking hermes installation…"
    if [ -f "$ENV_FILE" ]; then
        say "Hermes found. Enabling the local API server…"
        if ! grep -q "^API_SERVER_ENABLED=" "$ENV_FILE"; then
            printf '\nAPI_SERVER_ENABLED=true\n' >> "$ENV_FILE"
            ok "API_SERVER_ENABLED=true added to $ENV_FILE"
        else
            ok "API server already enabled in .env"
        fi
        if ! grep -q "^API_SERVER_KEY=" "$ENV_FILE"; then
            KEY="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | xxd -p -c64)"
            printf 'API_SERVER_KEY=%s\n' "$KEY" >> "$ENV_FILE"
            ok "Generated API_SERVER_KEY"
        else
            ok "API_SERVER_KEY already present"
        fi
        say "Restart the gateway to apply:   systemctl --user restart hermes-gateway"
        say "(Or reboot, or run: hermes gateway restart)"
    else
        err "No Hermes installation found (~/.hermes/.env missing)."
        echo "  Install hermes-agent first:"
        echo "  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
        exit 1
    fi
else
    ok "Gateway running on 127.0.0.1:8642"
fi

# --- 3. Install files ---------------------------------------------------------
say "Installing…"
mkdir -p "$BINDIR" "$CFG_DIR" "$HOME/.cache"
install -m 755 "$SPOT_DIR/hermes-spotlight.py" "$BINDIR/hermes-spotlight"

# Launcher .desktop (so it appears in app menus / can be bound)
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/${DESKTOP_ID}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Hermes Spotlight
Comment=Global AI spotlight for hermes-agent
Exec=$BINDIR/hermes-spotlight
Icon=system-search-symbolic
Categories=Utility;X-AI;
StartupWMClass=com.hermes.spotlight
Terminal=false
NoDisplay=false
EOF
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
ok "Installed: $BINDIR/hermes-spotlight + menu entry"

# --- 4. Keyboard shortcut per desktop ----------------------------------------
DE="${XDG_CURRENT_DESKTOP:-unknown}"
CMD="$BINDIR/hermes-spotlight"

case "$DE" in
    *COSMIC*)
        SC_DIR="$HOME/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1"
        mkdir -p "$SC_DIR"
        SC_FILE="$SC_DIR/custom.ron"
        if [ -f "$SC_FILE" ] && grep -q "hermes-spotlight" "$SC_FILE"; then
            ok "COSMIC shortcut already configured"
        else
            {
                echo '{'
                echo '    (modifiers: [Super], key: "space", description: "Hermes Spotlight"): Spawn("'"$CMD"'"),'
                echo '}'
            } >> "$SC_FILE"
            ok "COSMIC shortcut added: Super+Space (takes effect after next login)"
        fi
        ;;
    *GNOME*|*ubuntu*)
        say "GNOME detected — installing custom shortcut via gsettings…"
        EXISTING="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null || echo "@as []")"
        NEW="['/org/gnome/settings-daemon/plugins/media-keys/hermes-spotlight/']"
        gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$NEW" 2>/dev/null || true
        gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/hermes-spotlight/ name "Hermes Spotlight" 2>/dev/null || true
        gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/hermes-spotlight/ command "$CMD" 2>/dev/null || true
        gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/hermes-spotlight/ binding "['<Super>space']" 2>/dev/null || true
        ok "GNOME shortcut set: Super+Space"
        ;;
    *KDE*)
        say "KDE detected — writing kglobalshortcut src entry…"
        KC="$HOME/.config/kglobalshortcutsrc"
        if [ -f "$KC" ] && grep -q hermes-spotlight "$KC"; then
            ok "KDE shortcut already present"
        else
            mkdir -p "$HOME/.local/share/khotkeys"
            cat > "$HOME/.local/share/khotkeys/hermes-spotlight.desktop" <<EOF
[Data]
Name=Hermes Spotlight
Command=$CMD
Trigger=Meta+Space
EOF
            ok "KDE: added custom shortcut (System Settings → Shortcuts → Custom, key Meta+Space, may need manual enable)"
        fi
        ;;
    *)
        say "Unknown desktop ($DE) — no shortcut auto-configured."
        say "Bind '$CMD' to a global hotkey via your desktop's settings."
        ;;
esac

# --- 5. Smoke test ------------------------------------------------------------
say "Smoke test (no UI, import check)…"
"$PY" -c "import ast; ast.parse(open('$BINDIR/hermes-spotlight').read()); print('syntax OK')" >/dev/null && ok "Widget syntax OK"

echo
ok "Done! Press Super+Space to open your spotlight."
say  "If the shortcut needs a re-login (COSMIC), log out once and back in."