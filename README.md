# ✦ hermes-spotlight

**A minimal, transparent Spotlight-style AI launcher for the Linux desktop.**

Press `Alt+Space`, ask anything — your local [hermes-agent](https://github.com/NousResearch/hermes-agent) answers with live streaming, tool-call status and Markdown rendering, in a slim translucent bar that feels native to your desktop.

![hermes-spotlight](screenshot.png)

## Why

Raycast-style AI launchers are great — but Mac-only and closed-source. On Linux there was nothing that turns a **local, tool-using AI agent** into a one-keystroke overlay. hermes-spotlight does exactly that: it talks to your local Hermes gateway API server, so it has the **same agent, memory, skills, and tools** as your CLI and desktop app sessions. Ask in the spotlight, continue in the app — the conversation is shared.

- 🖥️ **100% local** — talks only to `127.0.0.1:8642`, no cloud, no telemetry
- ⚡ **Instant** — stays resident after first use: re-opens in ~30 ms, no Electron
- 🎨 **3 themes** — Tokyo Night, Midnight, Rose Pine (or add your own)
- 📋 **Markdown answers** — code blocks with a copy button, bold, lists; text is selectable
- 🚀 **App launcher** — type an app name, Enter starts it; the last row (or Shift+Enter) asks Hermes instead
- 🔄 **Live streaming** — answer text streams in, tool calls show as `⚙ terminal: …`
- 🧠 **Session memory** — the conversation survives closing, reboots
- ⌨️ **Quality of life** — input history (↑/↓), `/new`, `/stop`
- 🪟 **Logo button** — jump straight to the full Hermes desktop app

## The Vision — the assistant every OS is missing

Every desktop OS ships an "AI assistant" that is either a thin chat wrapper (Siri), a cloud bolt-on nobody asked for (Windows Copilot), or nothing at all (most Linux desktops). What's missing everywhere is the same thing: **an assistant that is part of the OS experience — local, aware of the machine, and actually able to do things.**

hermes-spotlight is an attempt at that missing layer for Linux:

- **One keystroke, always there.** Not an app you open — an overlay your desktop grows, like Spotlight on macOS. You don't "use" it, you just ask.
- **The agent, not a wrapper.** Behind the bar is a real agent with tools (terminal, files, browser), persistent memory and skills. It doesn't just answer — it executes. Ask it "why is my game stuttering" and it diagnoses your GPU driver state, because it *knows* your OS, GPU, RAM and running apps (system context is sent with every question).
- **100% local, by architecture.** Not "we respect your privacy" — there is simply no cloud path. The widget talks only to `127.0.0.1`. Your machine context, your conversations, your keys.
- **Native, not Electron.** ~200 lines of GTK4, stdlib-only client, no bundled Chromium. It should feel like the desktop grew it.

**Where this goes:** quick answers → app launching → system diagnosis → eventually the place where you handle everything that isn't a full app: "clean my shader cache", "why did that crash", "set up the new drive". The bar stays slim; the agent grows.

The North Star: *the user should never think "I need to open a terminal for this" — asking should always be the shortest path.*

## Requirements

- Linux with a desktop session (COSMIC, GNOME, KDE, anything — Wayland or X11)
- `python3` with PyGObject + GTK4:
  - Arch / CachyOS: `sudo pacman -S python-gobject gtk4`
  - Debian / Ubuntu: `sudo apt install python3-gi gir1.2-gtk-4.0`
  - Fedora: `sudo dnf install python3-gobject gtk4`
- [hermes-agent](https://github.com/NousResearch/hermes-agent) with the gateway running locally

## Install

```bash
git clone https://github.com/LordHayne/hermes-spotlight.git
cd hermes-spotlight
./install.sh
```

The installer:

1. Checks Python + PyGObject + GTK4
2. Enables the gateway's local API server and generates an API key if missing
3. Installs `hermes-spotlight` to `~/.local/bin` + an app-menu entry
4. Binds **Alt+Space** automatically on COSMIC and GNOME (KDE: creates the shortcut, enable once in System Settings)

On COSMIC the shortcut activates after the next login. GNOME is instant.

## Configure

Config lives in `~/.config/hermes-spotlight/config.json` (auto-created):

```json
{
  "api_base": "http://127.0.0.1:8642",
  "api_key": "",
  "theme": "tokyo-night",
  "width": 700,
  "max_height": 600,
  "resident": true
}
```

- `api_key` empty → the key is read from `API_SERVER_KEY` in `~/.hermes/.env`
- `theme`: `tokyo-night`, `midnight`, `rose-pine`
- `max_height`: the window grows with the answer up to this height, then scrolls
- `resident`: `true` keeps the process alive hidden after closing, so the next
  shortcut press opens it instantly (~80 MB RAM). Config changes apply after
  a restart: `pkill -f "bin/hermes-spotlight$"` (re-running `./install.sh` does this)
- No config needed for the default setup — it just works.

## Bind another key

<details>
<summary>COSMIC</summary>

Edit `~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom.ron`:

```ron
{
    (modifiers: [Alt], key: "space", description: Some("Hermes Spotlight")): Spawn("/home/YOU/.local/bin/hermes-spotlight"),
}
```

</details>

<details>
<summary>GNOME</summary>

```bash
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['/org/gnome/settings-daemon/plugins/media-keys/hermes-spotlight/']"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/hermes-spotlight/ name "Hermes Spotlight"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/hermes-spotlight/ command "$HOME/.local/bin/hermes-spotlight"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/hermes-spotlight/ binding "['<Alt>space']"
```

</details>

<details>
<summary>KDE</summary>

System Settings → Shortcuts → Add Custom → command `~/.local/bin/hermes-spotlight`, bind `Meta+Space`.

</details>

## Uninstall

```bash
./uninstall.sh
```

(Keeps your config and conversation history — delete `~/.config/hermes-spotlight/` manually if you really want it gone.)

## How it works

```
Alt+Space ──▶ hermes-spotlight (GTK4 window, ~200 lines UI)
                    │
                    │  POST /api/sessions/{id}/chat/stream (SSE)
                    ▼
              hermes-agent gateway (127.0.0.1:8642)
                    │  same agent core as CLI / desktop / messaging
                    ▼
              your tools: terminal, files, browser, skills, memory…
```

The gateway API server is an official hermes-agent platform adapter (`gateway/platforms/api_server.py`) — any OpenAI-compatible frontend can talk to it. hermes-spotlight uses the native session endpoints, so spotlight conversations show up in your app/CLI session list with full memory.

## License

MIT — see [LICENSE](LICENSE).

## Credits

Built by [LordHayne](https://github.com/LordHayne) with his AI agent (dogfooding at its finest 🐕). Not affiliated with Nous Research — just a happy user building on top of their excellent agent.