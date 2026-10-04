#!/usr/bin/env python3
"""hermes-spotlight — global AI Spotlight for the Linux desktop.

A minimal, transparent Spotlight-style launcher that talks to a local
hermes-agent gateway API server — the same agent, memory and tools as
your CLI/desktop sessions, one keystroke away.

Features
- Borderless, translucent, opens as a single search bar, grows with the
  conversation
- Live SSE streaming of the answer, live tool-call status (⚙ terminal: …)
- Markdown-lite rendering (code blocks, bold, inline code, lists)
- Input history (ArrowUp/Down), /new, /stop slash commands
- Session resume: the conversation survives close and reboot
  (reads ~/.cache/hermes-spotlight/session, falls back to the legacy
  ~/.cache/hermes-spotlight-session file)
- Single instance: activating twice focuses the existing window
- Optional logo button that opens the full Hermes desktop app
- Desktop-agnostic: any Wayland/X11 session, no Adw/layer-shell required

Requirements: python3 (>=3.9) with PyGObject (gi) — GTK4 only.
Talks to: http://127.0.0.1:8642 (hermes gateway API server)

Config: ~/.config/hermes-spotlight/config.json (created/migrated on run)
Launch: bind your compositor's "Spawn/run command" shortcut to
        hermes-spotlight (see README for GNOME/KDE/COSMIC examples)
"""
import os
import subprocess
import sys

# overridable so tests never grab the real instance's D-Bus name
APP_ID = os.environ.get("HERMES_SPOTLIGHT_APP_ID", "com.hermes.spotlight")


def _activate_running_instance() -> bool:
    """Fast path: when a resident spotlight is already running, ask it over
    D-Bus to show itself (~10 ms) instead of paying for a full GTK start-up
    (~300 ms). Forwards the compositor's activation token so the window
    may take focus on Wayland."""
    token = (os.environ.get("XDG_ACTIVATION_TOKEN")
             or os.environ.get("DESKTOP_STARTUP_ID") or "")
    pdata = "{}"
    if token and "'" not in token and "\\" not in token:
        pdata = "{'activation-token': <'%s'>}" % token
    try:
        r = subprocess.run(
            ["gdbus", "call", "--session", "--dest", APP_ID,
             "--object-path", "/" + APP_ID.replace(".", "/"),
             "--method", "org.freedesktop.Application.Activate", pdata],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


if (__name__ == "__main__"
        and not os.environ.get("HERMES_SPOTLIGHT_AUTOTEST")
        and _activate_running_instance()):
    sys.exit(0)

import base64
import html
import io
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request
import urllib.error
import uuid

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Pango

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
APP_DIR = os.path.expanduser("~/.config/hermes-spotlight")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "api_base": "http://127.0.0.1:8642",
    "api_key": "",                      # filled by installer or auto-read
    "env_path": os.path.expanduser("~/.hermes/.env"),
    "theme": "tokyo-night",             # tokyo-night | midnight | rose-pine
    "width": 700,
    "logo_paths": [                     # first hit is used, else fallback icon
        os.path.expanduser("~/.hermes/hermes-agent/apps/desktop/assets/icon.png"),
        "/usr/share/hermes-spotlight/icon.png",
    ],
    "app_desktop_id": "hermes",         # gtk-launch <id> for the full app
    "history_file": os.path.expanduser("~/.cache/hermes-spotlight-history"),
    "session_file": os.path.expanduser("~/.cache/hermes-spotlight/session"),
    "system_context": True,             # send OS/hardware context with asks
    "resident": True,                   # hide instead of quit: instant reopen
    "max_height": 600,                  # window grows with content up to this
}

THEMES = {
    "tokyo-night": {
        "window_bg":  "rgba(22, 22, 30, 0.72)",
        "entry_bg":   "rgba(36, 40, 59, 0.85)",
        "entry_border": "rgba(122, 162, 247, 0.28)",
        "entry_border_focus": "rgba(122, 162, 247, 0.85)",
        "accent":     "#7aa2f7",
        "accent_bg":  "rgba(122, 162, 247, 0.08)",
        "accent_bg_hover": "rgba(122, 162, 247, 0.25)",
        "text":       "#c0caf5",
        "placeholder": "#565f89",
        "user_bubble": "rgba(61, 89, 161, 0.92)",
        "ai_bubble":  "rgba(36, 40, 59, 0.82)",
        "ai_text":    "#a9b1d6",
        "code_bg":    "#16161e",
        "code_fg":    "#c0caf5",
        "inline_code_fg": "#9ece6a",
        "window_bg_top": "rgba(25, 26, 38, 0.78)",
        "window_bg_bottom": "rgba(18, 18, 26, 0.88)",
        "edge": "rgba(122, 162, 247, 0.20)",
        "syntax": {"keyword": "#bb9af7", "string": "#9ece6a",
                   "comment": "#565f89", "number": "#ff9e64",
                   "func": "#7aa2f7", "decor": "#2ac3de"},
    },
    "midnight": {
        "window_bg":  "rgba(12, 12, 18, 0.75)",
        "entry_bg":   "rgba(28, 28, 40, 0.88)",
        "entry_border": "rgba(94, 129, 172, 0.30)",
        "entry_border_focus": "rgba(94, 129, 172, 0.9)",
        "accent":     "#82aaff",
        "accent_bg":  "rgba(130, 170, 255, 0.08)",
        "accent_bg_hover": "rgba(130, 170, 255, 0.25)",
        "text":       "#d0d8f0",
        "placeholder": "#58608a",
        "user_bubble": "rgba(48, 70, 130, 0.92)",
        "ai_bubble":  "rgba(28, 28, 40, 0.82)",
        "ai_text":    "#b0bcd8",
        "code_bg":    "#101018",
        "code_fg":    "#d0d8f0",
        "inline_code_fg": "#c3e88d",
        "window_bg_top": "rgba(18, 18, 28, 0.80)",
        "window_bg_bottom": "rgba(10, 10, 16, 0.90)",
        "edge": "rgba(130, 170, 255, 0.22)",
        "syntax": {"keyword": "#c792ea", "string": "#c3e88d",
                   "comment": "#58608a", "number": "#f78c6c",
                   "func": "#82aaff", "decor": "#89ddff"},
    },
    "rose-pine": {
        "window_bg":  "rgba(26, 23, 36, 0.75)",
        "entry_bg":   "rgba(38, 35, 50, 0.88)",
        "entry_border": "rgba(226, 140, 160, 0.28)",
        "entry_border_focus": "rgba(226, 140, 160, 0.85)",
        "accent":     "#ebbcba",
        "accent_bg":  "rgba(235, 188, 186, 0.08)",
        "accent_bg_hover": "rgba(235, 188, 186, 0.25)",
        "text":       "#e0def4",
        "placeholder": "#6e6a86",
        "user_bubble": "rgba(71, 58, 96, 0.92)",
        "ai_bubble":  "rgba(38, 35, 50, 0.85)",
        "ai_text":    "#d2d0e6",
        "code_bg":    "#1f1d2e",
        "code_fg":    "#e0def4",
        "inline_code_fg": "#9ccfd8",
        "window_bg_top": "rgba(33, 29, 45, 0.78)",
        "window_bg_bottom": "rgba(24, 21, 34, 0.88)",
        "edge": "rgba(235, 188, 186, 0.20)",
        "syntax": {"keyword": "#c4a7e7", "string": "#9ccfd8",
                   "comment": "#6e6a86", "number": "#f6c177",
                   "func": "#ebbcba", "decor": "#ea9a97"},
    },
}

PLACEHOLDER = "Ask Hermes…   (↑ history, /new, /stop)"


# --------------------------------------------------------------------------
# System context (sent as ephemeral system_message so the agent knows the
# machine it is talking about; appended, never replacing, the core prompt)
# --------------------------------------------------------------------------
def collect_system_context(max_apps: int = 5) -> str:
    """One compact snapshot of the machine: OS, DE, kernel, GPU, RAM,
    running apps. Cached 60s — the widget is opened many times, the
    values change slowly. All local reads, no subprocess spam."""
    cache = os.path.expanduser("~/.cache/hermes-spotlight/syscontext.txt")
    try:
        if time.time() - os.path.getmtime(cache) < 60:
            return open(cache).read()
    except OSError:
        pass
    ctx = {}
    # OS + kernel (single file reads, no subprocess)
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    ctx["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    ctx["kernel"] = os.uname().release
    ctx["desktop"] = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    ctx["display"] = ("wayland" if os.environ.get("WAYLAND_DISPLAY")
                      else "x11")
    # RAM: first MemTotal line of /proc/meminfo
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    ctx["ram_gb"] = round(int(line.split()[1]) / 1.048576e6)
                    break
    except OSError:
        pass
    # GPU + CPU model: one lspci/cpuinfo read via subprocess (fast, cached)
    try:
        r = subprocess.run(["lspci"], capture_output=True, text=True,
                           timeout=5)
        gpus = [l.split(":", 2)[2].strip()
                for l in r.stdout.splitlines()
                if " VGA " in l or " 3D " in l]
        if gpus:
            ctx["gpu"] = "; ".join(gpus)
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    ctx["cpu"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    # running GUI apps (from our own app index — cheap)
    ctx["running_apps"] = _running_apps(max_apps)
    lines = [f"Machine context of the user asking via hermes-spotlight:"]
    for k in ("os", "kernel", "desktop", "display", "cpu", "gpu", "ram_gb"):
        if k in ctx:
            lines.append(f"- {k}: {ctx[k]}")
    if ctx.get("running_apps"):
        lines.append(f"- currently running apps: "
                     f"{', '.join(ctx['running_apps'])}")
    out = "\n".join(lines)
    try:
        with open(cache, "w") as f:
            f.write(out)
    except OSError:
        pass
    return out


def _running_apps(max_apps: int) -> list:
    """Names of running GUI apps, derived from /proc cmdlines matched
    against the desktop index. Local and heuristic; best effort."""
    out = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().split(b"\x00")[0].decode(
                        "utf-8", "replace")
            except OSError:
                continue
            if not cmd:
                continue
            base = os.path.basename(cmd)
            if base in _KNOWN_APP_PROCESSES:
                name = _KNOWN_APP_PROCESSES[base]
                if name not in out:
                    out.append(name)
            if len(out) >= max_apps:
                break
    except OSError:
        pass
    return out


# A small mapping: process names -> human-readable app names. Keep tiny;
# this is a hint for the agent, not an exhaustive process list.
_KNOWN_APP_PROCESSES = {
    "steam": "Steam", "gamescope": "Gamescope", "oncehuman": "Once Human",
    "firefox": "Firefox", "chromium": "Chromium", "chrome": "Chrome",
    "discord": "Discord", "spotify": "Spotify", "code": "VS Code",
    "obsidian": "Obsidian", "gimp": "GIMP", "kdenlive": "Kdenlive",
    "vlc": "VLC", "mpv": "mpv", "obs": "OBS Studio",
}


# --------------------------------------------------------------------------
# App index (launcher mode)
# --------------------------------------------------------------------------
DESKTOP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.expanduser("~/.local/share/applications"),
    # Flatpak (Chrome, Spotify etc. often installed here) and Snap
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
    "/var/lib/snapd/desktop/applications",
]
DESKTOP_CACHE = os.path.expanduser("~/.cache/hermes-spotlight/apps.json")
DESKTOP_CACHE_TTL = 900          # rescan at most every 15 min


def _parse_desktop_file(path: str):
    """Extract (id, name, exec, keywords) from a .desktop file. None if not
    launchable (NoDisplay, hidden, missing Exec/Name)."""
    name = exec_ = kw = None
    info_icon = None
    no_display = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("[") and name and exec_:
                    break
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k == "Name" and name is None:
                    name = v.strip()
                elif k == "Exec":
                    exec_ = v.strip()
                elif k == "Keywords":
                    kw = v.strip()
                elif k == "NoDisplay":
                    no_display = v.strip().lower() == "true"
                elif k == "Icon":
                    info_icon = v.strip()
                elif k == "Type" and v.strip() != "Application":
                    return None
    except OSError:
        return None
    if not name or not exec_ or no_display:
        return None
    # strip field codes (%f %u %F %U %d %D %n %N %i %c %k) and quotes
    cmd = re.sub(r"\s%[a-zA-Z]", "", exec_).strip()
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    if not argv or not argv[0]:
        return None
    return {"id": os.path.basename(path)[:-8], "name": name,
            "argv": argv, "keywords": (kw or "").lower(),
            "name_l": name.lower(), "icon": info_icon}


def build_app_index(force=False) -> list:
    """Scan DESKTOP_DIRS for launchable apps. Cached in apps.json."""
    now = time.time()
    if not force and os.path.exists(DESKTOP_CACHE):
        try:
            with open(DESKTOP_CACHE) as f:
                blob = json.load(f)
            if now - blob.get("ts", 0) < DESKTOP_CACHE_TTL:
                return blob["apps"]
        except (OSError, ValueError, KeyError):
            pass
    apps = []
    seen = set()
    for d in DESKTOP_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".desktop") or fn in seen:
                continue
            info = _parse_desktop_file(os.path.join(d, fn))
            if info:
                seen.add(fn)
                apps.append(info)
    try:
        os.makedirs(os.path.dirname(DESKTOP_CACHE), exist_ok=True)
        with open(DESKTOP_CACHE, "w") as f:
            json.dump({"ts": now, "apps": apps}, f)
    except OSError:
        pass
    return apps


def match_apps(query: str, apps: list, limit: int = 5) -> list:
    """Rank apps for a query: prefix > word-start > substring > keyword.
    Duplicate names (e.g. native + flatpak of the same app) collapse."""
    q = query.lower().strip()
    if not q:
        return []
    out, seen_names = [], set()
    for a in apps:
        n = a["name_l"]
        score = 0
        if n == q:
            score = 1000
        elif n.startswith(q):
            score = 500 - len(n)
        else:
            ws = [w for w in n.split() if w.startswith(q)]
            if ws:
                score = 300 - len(n)
            elif q in n:
                score = 200 - len(n)
            elif q in a["keywords"]:
                score = 100 - len(n)
        if score and n not in seen_names:
            seen_names.add(n)
            out.append((score, a))
    out.sort(key=lambda t: -t[0])
    return [a for _, a in out[:limit]]


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # Write back the merged config: creates it on first run and migrates
    # older configs when new keys appear.
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass
    return cfg


def load_api_key(cfg: dict) -> str:
    """API key: config.json > API_SERVER_KEY line in env file."""
    if cfg.get("api_key"):
        return cfg["api_key"].strip()
    try:
        with open(cfg["env_path"]) as f:
            for line in f:
                if line.startswith("API_SERVER_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def build_css(cfg: dict) -> bytes:
    t = THEMES.get(cfg.get("theme", "tokyo-night"), THEMES["tokyo-night"])
    return f"""
window {{
  background:
    linear-gradient(to bottom, {t['window_bg_top']}, {t['window_bg_bottom']});
  border-radius: 16px;
}}
.spot {{ padding: 12px 12px 10px 12px; }}
entry {{
  background: {t['entry_bg']};
  border-radius: 14px;
  padding: 12px 76px 12px 42px;
  font-size: 15px;
  color: {t['text']};
  caret-color: {t['accent']};
  border: 1px solid {t['entry_border']};
  box-shadow: 0 1px 6px rgba(0, 0, 0, 0.35);
}}
entry:focus {{ border-color: {t['entry_border_focus']}; }}
entry image {{ color: {t['accent']}; }}
entry placeholder {{ color: {t['placeholder']}; }}
selection {{ background-color: {t['accent_bg_hover']}; }}
.logobtn {{
  background: {t['accent_bg']};
  border: 1px solid {t['entry_border']};
  border-radius: 999px;
  padding: 3px;
}}
.logobtn:hover {{ background: {t['accent_bg_hover']}; }}
spinner {{ padding: 1px; }}
.msg-user, .msg-ai {{ padding: 10px 14px; border-radius: 12px; margin: 4px 0; }}
.msg-user {{ background: {t['user_bubble']}; color: {t['text']}; }}
.msg-ai   {{ background: {t['ai_bubble']}; color: {t['ai_text']}; }}
.msg-user text, .msg-ai text {{ color: inherit; }}
.toolstatus {{ color: {t['accent']}; font-size: 11px; padding: 3px 14px; }}
.imgchip {{
  background: {t['ai_bubble']}; color: {t['ai_text']};
  padding: 4px 10px; border-radius: 999px; margin-top: 4px;
  font-size: 12px;
}}
.chipx {{ background: transparent; border: none; padding: 2px; }}
.sugg {{ padding: 6px 12px; border-radius: 8px; margin: 1px 2px; }}
.sugglabel {{ color: {t['text']}; font-size: 14px; }}
.sugghint {{ color: {t['placeholder']}; font-size: 11px; }}
.suggsel {{ background: {t['accent_bg_hover']}; }}
.suggask .sugglabel {{ color: {t['accent']}; }}
.copybtn {{
  min-width: 0; min-height: 0; padding: 4px; margin: 10px 6px;
  border-radius: 8px; border: none; box-shadow: none;
  background: {t['accent_bg']}; color: {t['placeholder']};
}}
.copybtn:hover {{ background: {t['accent_bg_hover']}; color: {t['accent']}; }}
.codeblock {{
  background: {t['code_bg']}; color: {t['code_fg']};
  padding: 10px 40px 10px 12px; margin: 6px 0;
  border-radius: 10px;
  font-family: monospace; font-size: 13px;
}}
""".encode()


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def _post(base, key, path, payload, timeout=300):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(base, key, path, timeout=10):
    req = urllib.request.Request(base + path,
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------
# Syntax highlighting (regex-based, Pango markup)
# --------------------------------------------------------------------------
SYNTAX_KEYWORDS = (
    r"\b(?:def|class|return|if|elif|else|for|while|try|except|finally|"
    r"with|as|import|from|in|not|and|or|is|None|True|False|lambda|yield|"
    r"pass|break|continue|raise|global|nonlocal|assert|del|async|await|"
    r"func|fn|let|var|const|struct|enum|impl|trait|pub|mut|match|self|"
    r"void|int|float|str|bool|list|dict|set|new|delete|switch|case|"
    r"public|private|static|echo|fi|then|do|done|esac|function|local)\b"
)


def _highlight_code(code: str, syn: dict) -> str:
    """Regex-based syntax highlighting to Pango markup. Order matters:
    strings/comments are stashed as private-use-unicode placeholders first
    (immune to every later regex), then keywords/numbers run on the rest."""
    # quote=False: raw quotes stay, so string regexes see them; Pango is
    # fine with raw quotes in text content. (& < > are still escaped.)
    text = html.escape(code, quote=False)
    tokens = []

    def _stash(m):
        tokens.append(m.group(0))
        return chr(0xE000 + len(tokens) - 1)     # private-use char

    text = re.sub(r'"""[^"]*"""|"[^"\n]*"|\'[^\'\n]*\'', _stash, text)
    text = re.sub(r"#[^\n]*", _stash, text)
    text = re.sub(r"@[A-Za-z_][\w.]*", _stash, text)          # decorators
    text = re.sub(SYNTAX_KEYWORDS,
                  rf'<span foreground="{syn["keyword"]}">\g<0></span>', text)
    text = re.sub(r"\b([a-z_][\w]*)\s*\(",
                  rf'<span foreground="{syn["func"]}">\1</span>(', text)
    text = re.sub(r"\b\d[\d_]*(?:\.\d+)?j?\b",
                  rf'<span foreground="{syn["number"]}">\g<0></span>', text)

    def _restore(m):
        tok = tokens[ord(m.group(0)) - 0xE000]
        if tok[0] in "\"'":
            color = syn["string"]
        elif tok.startswith("#"):
            color = syn["comment"]
        else:
            color = syn["decor"]
        return f'<span foreground="{color}">{tok}</span>'

    return re.sub(r"[\ue000-\uf8ff]", _restore, text)


# --------------------------------------------------------------------------
# Markdown-lite
# --------------------------------------------------------------------------
def _md_inline(text: str, code_fg: str, code_bg: str, accent: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`",
                  rf'<span font_family="monospace" background="{code_bg}" '
                  rf'foreground="{code_fg}">\1</span>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  rf'<span foreground="{accent}" underline="single">\1</span>',
                  text)
    lines = []
    for ln in text.split("\n"):
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            ln = f'<b><span size="larger">{m.group(2)}</span></b>'
        else:
            ln = re.sub(r"^\s*[-*]\s+", "• ", ln)
        lines.append(ln)
    return "\n".join(lines)


def _copy_code(btn, code: str):
    """Copy a code block to the clipboard, tick the button for a moment."""
    btn.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(code))
    btn.set_icon_name("object-select-symbolic")
    GLib.timeout_add(1200, lambda: btn.set_icon_name("edit-copy-symbolic"))


def _md_widgets(text: str, theme: dict) -> list:
    widgets = []
    parts = re.split(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", text, flags=re.S)
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        if i % 2 == 1:
            lbl = Gtk.Label(label="", wrap=True, xalign=0,
                            wrap_mode=Pango.WrapMode.WORD_CHAR,
                            selectable=True)
            lbl.add_css_class("codeblock")
            code = part.rstrip("\n")
            try:
                lbl.set_markup(_highlight_code(code, theme["syntax"]))
            except Exception:
                lbl.set_text(code)
            btn = Gtk.Button(icon_name="edit-copy-symbolic",
                             halign=Gtk.Align.END, valign=Gtk.Align.START,
                             tooltip_text="Copy")
            btn.add_css_class("copybtn")
            btn.connect("clicked", _copy_code, code)
            ov = Gtk.Overlay(child=lbl)
            ov.add_overlay(btn)
            widgets.append(ov)
            continue
        else:
            lbl = Gtk.Label(label="", wrap=True, xalign=0, selectable=True,
                            wrap_mode=Pango.WrapMode.WORD_CHAR)
            try:
                lbl.set_markup(_md_inline(part, theme["inline_code_fg"],
                                          theme["code_bg"], theme["accent"]))
            except Exception:
                lbl.set_text(part)
        widgets.append(lbl)
    return widgets


# --------------------------------------------------------------------------
# Image paste (vision input)
# --------------------------------------------------------------------------
MAX_IMAGE_EDGE = 1568           # vision models cap ~1568-2048px; keep tokens low


def _content_with_image(text: str, data_url: str) -> list:
    """OpenAI vision content list: text part + image part."""
    return [{"type": "text", "text": text or "What is on this screenshot?"},
            {"type": "image_url",
             "image_url": {"url": data_url, "detail": "low"}}]


def _pixbuf_to_data_url(pb: GdkPixbuf.Pixbuf) -> str | None:
    """Pixbuf -> downscaled jpeg data URL."""
    try:
        w, h = pb.get_width(), pb.get_height()
        scale = min(1.0, MAX_IMAGE_EDGE / max(w, h))
        if scale < 1.0:
            pb = pb.scale_simple(max(1, round(w * scale)),
                                 max(1, round(h * scale)),
                                 GdkPixbuf.InterpType.BILINEAR)
        ok, buf = pb.save_to_bufferv("jpeg", ["quality"], ["82"])
        if not ok:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(buf).decode()
    except Exception:
        return None


def _data_url_to_pixbuf(data_url: str, edge: int) -> GdkPixbuf.Pixbuf | None:
    """Data URL -> small pixbuf for the thumbnail chip."""
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
        loader = GdkPixbuf.PixbufLoader()
        loader.write(raw)
        loader.close()
        pb = loader.get_pixbuf()
        if pb is None:
            return None
        w, h = pb.get_width(), pb.get_height()
        scale = min(edge / max(w, h), 1.0)
        return pb.scale_simple(int(w * scale), int(h * scale),
                               GdkPixbuf.InterpType.BILINEAR)
    except Exception:
        return None


# --------------------------------------------------------------------------
# SSE stream handle
# --------------------------------------------------------------------------
class _StoppedError(Exception):
    pass


class _StreamHandle:
    def __init__(self, base, key, sid, text, system_message=None):
        payload = {"message": text}
        if system_message:
            payload["system_message"] = system_message
        req = urllib.request.Request(
            f"{base}/api/sessions/{sid}/chat/stream",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            method="POST")
        self.resp = urllib.request.urlopen(req, timeout=300)
        self.stopped = False

    def events(self):
        ev_name = None
        try:
            for raw in self.resp:
                line = raw.decode("utf-8").rstrip("\n")
                if not line or line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    ev_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    try:
                        payload = json.loads(line[len("data:"):].strip())
                    except json.JSONDecodeError:
                        continue
                    yield ev_name, payload
        except Exception as e:
            if self.stopped:
                raise _StoppedError() from e
            raise

    def stop(self):
        self.stopped = True
        try:
            self.resp.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class Spotlight(Gtk.ApplicationWindow):
    def __init__(self, app, cfg, key):
        super().__init__(application=app, title="Hermes Spotlight")
        self.cfg = cfg
        self.key = key
        self.theme = THEMES.get(cfg.get("theme", "tokyo-night"),
                                THEMES["tokyo-night"])
        self.session_cache = cfg["session_file"]
        self.session_id = None
        self._busy = False
        self._t0 = time.time()
        self._hist = self._load_history()
        self._hist_idx = len(self._hist)
        self._stream = None
        # lifecycle / per-send state (worker threads must respect _closed)
        self._closed = False
        self._ai_bubble = None
        self._status = None
        self._stream_lbl = None
        self._ai_prepped = False
        # streamed deltas are batched (~30 fps) instead of one UI update
        # per token
        self._delta_buf = []
        self._delta_lock = threading.Lock()
        self._flush_pending = False
        self._icon_cache = {}

        w = int(cfg.get("width", 700))
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(w, 72)

        # --- search entry --------------------------------------------------
        self.entry = Gtk.Entry(placeholder_text=PLACEHOLDER)
        self.entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.PRIMARY, "system-search-symbolic")
        # lambda: "activate" passes the entry as first arg, which would
        # otherwise land in force_ask and disable launching apps via Enter
        self.entry.connect("activate", lambda *_: self._on_send())
        ek = Gtk.EventControllerKey()
        ek.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(ek)
        # live app suggestions while typing
        self.entry.connect("changed", self._on_entry_changed)
        # Ctrl+V image paste (screenshot tools put PNG into the clipboard)
        self._clip = Gdk.Display.get_default().get_clipboard()
        self._pending_image: str | None = None    # data URL
        self._clip.connect("changed", self._on_clipboard_changed)

        self.spinner = Gtk.Spinner(halign=Gtk.Align.END,
                                   valign=Gtk.Align.CENTER,
                                   margin_end=42, visible=False)

        logo_img = self._load_logo()
        if logo_img is not None:
            self.app_btn = Gtk.Button(child=logo_img, halign=Gtk.Align.END,
                                      valign=Gtk.Align.CENTER, margin_end=12)
        else:
            self.app_btn = Gtk.Button(icon_name="starred-symbolic",
                                      halign=Gtk.Align.END,
                                      valign=Gtk.Align.CENTER, margin_end=12)
        self.app_btn.add_css_class("logobtn")
        self.app_btn.set_tooltip_text("Open Hermes app")
        self.app_btn.connect("clicked", self._open_app)

        overlay = Gtk.Overlay(child=self.entry)
        overlay.add_overlay(self.spinner)
        overlay.add_overlay(self.app_btn)

        # --- answers ---------------------------------------------------------
        self.flow = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                            margin_top=2)
        self.scroll = Gtk.ScrolledWindow(child=self.flow, vexpand=True)
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # report the real content height (capped) so _grow can measure it
        self.scroll.set_propagate_natural_height(True)
        self.scroll.set_max_content_height(int(cfg.get("max_height", 600)))
        self.scroll.set_visible(False)

        # --- app suggestions (launcher mode) --------------------------------
        self._apps = build_app_index()
        self._app_sel = -1
        self._last_sugg = []
        self.sugg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                margin_top=4, visible=False)
        self.sugg_rows = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("spot")
        box.append(overlay)
        box.append(self.sugg_box)
        box.append(self.scroll)
        self.set_child(box)

        prov = Gtk.CssProvider()
        prov.load_from_data(build_css(cfg))
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self._on_key)
        self.add_controller(kc)
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("close-request", self._on_close)

        self._autotest_mode = bool(os.environ.get("HERMES_SPOTLIGHT_AUTOTEST"))
        if self._autotest_mode:
            GLib.timeout_add_seconds(2, self._autotest)

    # ------------------------------------------------------------------ misc
    def _load_logo(self):
        for p in self.cfg.get("logo_paths", []):
            if os.path.exists(p):
                try:
                    tex = Gdk.Texture.new_from_filename(p)
                    img = Gtk.Image.new_from_paintable(tex)
                    img.set_pixel_size(22)
                    return img
                except Exception:
                    continue
        return None

    def _load_history(self) -> list:
        try:
            with open(self.cfg["history_file"]) as f:
                return [l.rstrip("\n") for l in f if l.strip()][-100:]
        except OSError:
            return []

    def _save_history(self):
        try:
            os.makedirs(os.path.dirname(self.cfg["history_file"]),
                        exist_ok=True)
            with open(self.cfg["history_file"], "w") as f:
                f.write("\n".join(self._hist[-100:]))
        except OSError:
            pass

    def _autotest(self):
        self.entry.set_text(os.environ.get(
            "HERMES_SPOTLIGHT_AUTOTEST_QUERY",
            "Reply with exactly: Autotest OK"))
        self._on_send()
        return False

    # --------------------------------------------------------------- keys
    def _on_key(self, _c, keyval, _k, _s):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    # ------------------------------------------------------ image paste
    def _on_clipboard_changed(self, *_):
        """Track clipboard so Ctrl+V knows an image is available."""
        fmt = self._clip.get_formats()
        self._clip_has_image = fmt.contain_gtype(GdkPixbuf.Pixbuf) \
            if fmt else False

    def _on_paste_key(self, ctrl, keyval, _kc, state):
        """Ctrl+V: if the clipboard holds an image, attach it as vision
        input instead of pasting text."""
        if keyval in (Gdk.KEY_v, Gdk.KEY_V) and \
                state & Gdk.ModifierType.CONTROL_MASK:
            if getattr(self, "_clip_has_image", False):
                self._attach_clipboard_image()
                return True
        return False

    def _attach_clipboard_image(self):
        def _done(clip, res):
            try:
                texture = clip.read_texture_finish(res)
            except Exception:
                return
            if texture is None:
                return
            pb = Gdk.pixbuf_get_from_texture(texture) \
                if hasattr(Gdk, "pixbuf_get_from_texture") else None
            if pb is None:
                # download_texture fallback
                try:
                    dl = texture.download()
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write_bytes(dl.get_bytes())
                    loader.close()
                    pb = loader.get_pixbuf()
                except Exception:
                    return
            data = _pixbuf_to_data_url(pb)
            if data:
                self._pending_image = data
                GLib.idle_add(self._show_image_chip)
        self._clip.read_texture_async(None, _done)

    def _show_image_chip(self):
        """Small thumbnail chip below the entry showing the attached image."""
        if getattr(self, "img_chip", None) is not None:
            self.img_chip.unparent()
            self.img_chip = None
        if self._pending_image is None:
            return False
        pb = _data_url_to_pixbuf(self._pending_image, 48)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      margin_top=4)
        chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                       margin_top=4)
        chip.add_css_class("imgchip")
        if pb is not None:
            chip.append(Gtk.Image.new_from_pixbuf(pb))
        lbl = Gtk.Label(label="screenshot attached")
        chip.append(lbl)
        x = Gtk.Button(icon_name="window-close-symbolic")
        x.add_css_class("chipx")
        x.set_tooltip_text("Remove image")
        x.connect("clicked", lambda *_: self._remove_image())
        chip.append(x)
        self.img_chip = chip
        # insert directly after the overlay (before suggestions/answers)
        self.get_child().get_first_child()  # .spot box
        spot = self.get_child()
        spot.insert_child_after(chip, spot.get_first_child())
        self._grow(30)
        return False

    def _remove_image(self):
        self._pending_image = None
        chip = getattr(self, "img_chip", None)
        if chip is not None:
            chip.unparent()
            self.img_chip = None
            self._grow(-30)
        self.entry.grab_focus()

    # ------------------------------------------------------ launcher mode
    def _on_entry_changed(self, *_):
        """Live app suggestions while typing (never for /commands)."""
        text = self.entry.get_text().strip()
        if (self._busy or text.startswith("/") or not text
                or len(text) < 2 or not self._apps):
            self._show_suggestions([])
            return
        self._show_suggestions(match_apps(text, self._apps))

    def _show_suggestions(self, apps):
        child = self.sugg_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.sugg_box.remove(child)
            child = nxt
        self.sugg_rows = []
        self._app_sel = -1
        self._last_sugg = list(apps)
        if not apps:
            self.sugg_box.set_visible(False)
            self._grow()
            return
        for app in apps:
            icon = self._app_icon(app["icon"]) if app.get("icon") else None
            self._add_sugg_row(app["name"], "⏎ start", icon,
                               lambda a=app: self._launch_app(a))
        # last row: makes the alternative to launching visible
        ask = self._add_sugg_row("✦ Ask Hermes", "⇧⏎", None,
                                 lambda: self._on_send(force_ask=True))
        ask.add_css_class("suggask")
        # Enter launches the first match — show that it is selected
        self._select_sugg(0)
        self.sugg_box.set_visible(True)
        self._grow()

    def _add_sugg_row(self, label, hint, icon, on_click):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("sugg")
        if icon is not None:
            row.append(icon)
        lbl = Gtk.Label(label=label, xalign=0,
                        ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        lbl.add_css_class("sugglabel")
        row.append(lbl)
        h = Gtk.Label(label=hint, xalign=1)
        h.add_css_class("sugghint")
        row.append(h)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: on_click())
        row.add_controller(click)
        self.sugg_box.append(row)
        self.sugg_rows.append(row)
        return row

    def _select_sugg(self, idx):
        self._app_sel = max(0, min(idx, len(self.sugg_rows) - 1))
        for i, row in enumerate(self.sugg_rows):
            if i == self._app_sel:
                row.add_css_class("suggsel")
            else:
                row.remove_css_class("suggsel")

    def _app_icon(self, icon_name: str):
        """App icon (absolute path or icon-theme name, 20px). Paintables
        are cached — suggestions are rebuilt on every keystroke."""
        if icon_name not in self._icon_cache:
            self._icon_cache[icon_name] = self._load_icon_paintable(icon_name)
        paintable = self._icon_cache[icon_name]
        if paintable is None:
            return None
        img = Gtk.Image.new_from_paintable(paintable)
        img.set_pixel_size(20)
        return img

    def _load_icon_paintable(self, icon_name: str):
        if icon_name.startswith("/"):
            if os.path.exists(icon_name):
                try:
                    return Gdk.Texture.new_from_filename(icon_name)
                except Exception:
                    return None
            return None
        try:
            theme = Gtk.IconTheme.get_for_display(self.get_display())
            return theme.lookup_icon(icon_name, None, 20, 1, 1,
                                     Gtk.TextDirection.NONE,
                                     Gtk.IconLookupFlags.FORCE_REGULAR)
        except Exception:
            return None

    def _launch_app(self, app):
        try:
            subprocess.Popen(app["argv"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
        except Exception:
            pass
        self.close()

    # --------------------------------------------------------------- keys
    def _on_entry_key(self, _c, keyval, _k, state):
        # Shift+Enter: ask Hermes even when an app match is visible
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) \
                and (state & Gdk.ModifierType.SHIFT_MASK):
            self._on_send(force_ask=True)
            return True
        # Arrow keys: navigate app suggestions when visible, else history
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down) and self.sugg_rows:
            step = -1 if keyval == Gdk.KEY_Up else 1
            self._select_sugg(self._app_sel + step)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down) and self._hist:
            if keyval == Gdk.KEY_Up:
                self._hist_idx = max(0, self._hist_idx - 1)
            else:
                self._hist_idx = min(len(self._hist), self._hist_idx + 1)
                if self._hist_idx >= len(self._hist):
                    self.entry.set_text("")
                    return True
            self.entry.set_text(self._hist[self._hist_idx])
            self.entry.set_position(-1)
            return True
        return False

    def _on_active_changed(self, *_):
        if self._autotest_mode:
            return
        if self.is_active():
            self.entry.grab_focus()
        elif time.time() - self._t0 > 1.5 and not self._busy:
            self.close()

    def _on_close(self, *_):
        if self.session_id:
            self._save_session(self.session_id)
        if self.cfg.get("resident", True) and not self._autotest_mode:
            # stay alive hidden: the next shortcut press only re-shows the
            # window (see _activate_running_instance). A running answer
            # keeps streaming into the hidden window.
            self.set_visible(False)
            return True
        self._closed = True
        self.get_application().quit()
        return True

    def reopen(self):
        """Show the resident window again, ready for the next question."""
        self._t0 = time.time()
        self._apps = build_app_index()        # cheap: cached, 15 min TTL
        self.present()
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def _open_app(self, *_):
        try:
            subprocess.Popen(
                ["gtk-launch", self.cfg.get("app_desktop_id", "hermes")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except Exception:
            pass
        self.close()

    # ------------------------------------------------------------- session
    def _save_session(self, sid: str):
        try:
            os.makedirs(os.path.dirname(self.session_cache), exist_ok=True)
            with open(self.session_cache, "w") as f:
                f.write(sid or "")
        except OSError:
            pass

    def _load_cached_session(self):
        # New path first, then the legacy single-file location.
        paths = [self.session_cache,
                 os.path.expanduser("~/.cache/hermes-spotlight-session")]
        for path in paths:
            try:
                with open(path) as f:
                    sid = f.read().strip()
            except OSError:
                continue
            if not sid:
                continue
            try:
                _get(self.cfg["api_base"], self.key, f"/api/sessions/{sid}")
                return sid
            except Exception:
                continue
        return None

    def _new_session(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        r = _post(self.cfg["api_base"], self.key, "/api/sessions", {
            "title": f"Spotlight {stamp}-{uuid.uuid4().hex[:6]}",
            "source": "hermes-spotlight",
        })
        sid = (r.get("session") or {}).get("id") or r.get("session_id")
        if not sid:
            raise RuntimeError(f"session creation failed: {r}")
        return sid

    # ------------------------------------------------------------ ui utils
    def _bubble(self, widgets, css):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        b.add_css_class(css)
        for w in widgets:
            b.append(w)
        self.flow.append(b)
        self.scroll.set_visible(True)
        return b

    def _set_user_bubble(self, text):
        lbl = Gtk.Label(label=text, wrap=True, xalign=0, selectable=True,
                        wrap_mode=Pango.WrapMode.WORD_CHAR)
        self._bubble([lbl], "msg-user")

    def _prep_ai_bubble(self):
        if self._closed:
            return False
        status = Gtk.Label(label="", xalign=0)
        status.add_css_class("toolstatus")
        stream_lbl = Gtk.Label(label="", wrap=True, xalign=0, selectable=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR)
        b = self._bubble([status, stream_lbl], "msg-ai")
        self._status, self._stream_lbl, self._ai_bubble = status, stream_lbl, b
        self._ai_prepped = True
        return False

    def _render_ai_final(self, text):
        b = getattr(self, "_ai_bubble", None)
        if b is None:
            return
        b.remove(self._status)
        b.remove(self._stream_lbl)
        for w in _md_widgets(text, self.theme):
            b.append(w)

    def _scroll_down(self):
        adj = self.scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())

    def _grow(self, *_):
        """Size the window to its content as GTK measures it (entry,
        suggestions, conversation), capped at max_height. The scrolled
        window propagates its natural height, capped by its own max."""
        w = int(self.cfg.get("width", 700))
        self.scroll.set_visible(self.flow.get_first_child() is not None)
        h = self.get_child().measure(Gtk.Orientation.VERTICAL, w)[1]
        self.set_size_request(w, min(int(self.cfg.get("max_height", 600)), h))
        return False

    def _set_busy(self, on, hint=None):
        if self._closed:
            return
        self._busy = on
        self.spinner.set_visible(on)
        if on:
            self.spinner.start()
            self.entry.set_placeholder_text(hint or "Hermes is thinking…")
        else:
            self.spinner.stop()
            self.entry.set_placeholder_text(PLACEHOLDER)

    def _show_hint(self, text):
        """Non-busy inline hint (missing key etc.) — visible, grows window."""
        if self._closed:
            return
        if getattr(self, "_ai_bubble", None) is None:
            self._prep_ai_bubble()
        self._set_tool_status(text)
        self._grow()
        self._scroll_down()

    # --------------------------------------------------------------- send
    def _on_send(self, force_ask=False, *_):
        text = self.entry.get_text().strip()
        if not text or self._busy:
            return
        # Launcher mode: Enter launches the selected/first app match.
        # Shift+Enter always asks Hermes instead.
        # The last suggestion row is "Ask Hermes" — selecting it asks too.
        apps = self._last_sugg if (self.sugg_rows and not force_ask) else []
        if apps and self._app_sel < len(apps):
            self._launch_app(apps[max(0, self._app_sel)])
            return
        self._show_suggestions([])
        if not self.key:
            self._show_hint("⚠ No API key — set api_key in "
                            "~/.config/hermes-spotlight/config.json or "
                            "API_SERVER_KEY in ~/.hermes/.env")
            return
        low = text.lower()
        if low in ("/new", "/neu"):
            self.entry.set_text("")
            self._new_conversation()
            return
        if low in ("/stop", "/stopp"):
            self.entry.set_text("")
            self._stop_stream()
            return
        self._hist.append(text)
        self._hist_idx = len(self._hist)
        self._save_history()
        self.entry.set_text("")
        image = self._pending_image
        self._pending_image = None
        chip = getattr(self, "img_chip", None)
        if chip is not None:
            chip.unparent()
            self.img_chip = None
        # per-send state: fresh AI bubble, fresh growth tracker,
        # stale widget refs cleared (old bubbles may be removed already)
        self._ai_bubble = None
        self._status = None
        self._stream_lbl = None
        self._ai_prepped = False
        self._set_user_bubble(text + ("  📷" if image else ""))
        self._grow()
        self._set_busy(True)
        threading.Thread(target=self._worker, args=(text, image),
                         daemon=True).start()

    def _new_conversation(self):
        self._stop_stream()
        self.session_id = None
        # clear widget refs — the flow children are removed below, stale
        # refs would swallow later hints/errors into removed widgets
        self._ai_bubble = None
        self._status = None
        self._stream_lbl = None
        self._ai_prepped = False
        self._save_session("")
        child = self.flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        self._grow()
        self._set_busy(False)
        self.entry.set_placeholder_text("New conversation — ask anything…")

    def _stop_stream(self):
        if self._stream is not None:
            self._stream.stop()
            self._set_busy(False)

    def _worker(self, text, image=None):
        attempts = 6
        for i in range(1, attempts + 1):
            if self._closed:
                return
            try:
                if not self.session_id:
                    self.session_id = self._load_cached_session() \
                        or self._new_session()
                if not self._ai_prepped:
                    self._ai_prepped = True
                    GLib.idle_add(self._prep_ai_bubble)
                sysmsg = (collect_system_context()
                          if self.cfg.get("system_context", True)
                          else None)
                payload_text = (_content_with_image(text, image)
                                if image else text)
                handle = _StreamHandle(self.cfg["api_base"], self.key,
                                        self.session_id, payload_text, sysmsg)
            except urllib.error.HTTPError as e:
                GLib.idle_add(self._finish,
                              f"⚠ HTTP {e.code}: {e.read().decode()[:150]}")
                return
            except Exception as e:
                # nothing reached the agent yet — safe to retry
                if i < attempts:
                    GLib.idle_add(self._set_busy, True,
                                  f"Gateway waking up… ({i}/{attempts-1})")
                    time.sleep(5)
                    continue
                GLib.idle_add(self._finish,
                              f"⚠ Gateway unreachable: {e}\n"
                              f"Is it running? "
                              f"`systemctl --user status hermes-gateway`")
                return
            # The request is accepted and the agent is running: never resend
            # from here on, a retry would run the prompt (and its tool
            # calls) a second time.
            self._stream = handle
            try:
                for ev, payload in handle.events():
                    self._on_event(ev, payload)
                return
            except urllib.error.HTTPError as e:
                GLib.idle_add(self._finish,
                              f"⚠ HTTP {e.code}: {e.read().decode()[:150]}")
                return
            except _StoppedError:
                return
            except Exception as e:
                GLib.idle_add(self._finish_interrupted,
                              f"⚠ Stream lost: {e}")
                return

    def _finish_interrupted(self, note):
        """Stream broke mid-answer: keep what already streamed in, append
        the note — the answer may continue in the Hermes app session."""
        self._append_delta(self._drain_deltas())
        lbl = getattr(self, "_stream_lbl", None)
        partial = lbl.get_text() if lbl is not None else ""
        return self._finish(f"{partial}\n\n{note}" if partial else note)

    def _on_event(self, ev, p):
        if ev == "assistant.delta":
            self._queue_delta(p.get("delta", ""))
        elif ev == "tool.started":
            name = p.get("tool_name") or "?"
            prev = (p.get("preview") or "")[:70]
            GLib.idle_add(self._set_tool_status, f"⚙ {name}: {prev}")
        elif ev == "tool.completed":
            GLib.idle_add(self._set_tool_status, "")
        elif ev == "assistant.completed":
            GLib.idle_add(self._finish, p.get("content", ""))
        elif ev == "error":
            GLib.idle_add(self._finish, f"⚠ {p.get('message', 'error')}")

    def _set_tool_status(self, text):
        if self._closed:
            return False
        lbl = getattr(self, "_status", None)
        if lbl is not None:
            lbl.set_text(text)
        return False

    def _queue_delta(self, delta):
        """Worker thread: buffer a delta, flush at most every 33 ms."""
        with self._delta_lock:
            self._delta_buf.append(delta)
            if self._flush_pending:
                return
            self._flush_pending = True
        GLib.timeout_add(33, self._flush_deltas)

    def _drain_deltas(self) -> str:
        with self._delta_lock:
            chunk = "".join(self._delta_buf)
            self._delta_buf.clear()
            self._flush_pending = False
        return chunk

    def _flush_deltas(self):
        return self._append_delta(self._drain_deltas())

    def _append_delta(self, delta):
        if self._closed or not delta:
            return False
        lbl = getattr(self, "_stream_lbl", None)
        if lbl is not None:
            lbl.set_text(lbl.get_text() + delta)
        self._grow()
        self._scroll_down()
        return False

    def _finish(self, content):
        if self._closed:
            return False
        self._stream = None
        self._append_delta(self._drain_deltas())   # deltas still buffered
        # interrupted streams can complete with empty content — keep the
        # partial text that already streamed in
        if not content and getattr(self, "_stream_lbl", None) is not None:
            content = self._stream_lbl.get_text() or "⚠ empty response"
        # errors can arrive before any bubble was prepped — create one now
        if getattr(self, "_ai_bubble", None) is None:
            self._prep_ai_bubble()
        self._render_ai_final(content)
        self._set_busy(False)
        self._grow()
        self._scroll_down()
        self.entry.grab_focus()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        # Reuse the existing window: a second activation (e.g. pressing the
        # shortcut while the spotlight is open) must focus, not duplicate.
        win = getattr(self, "_win", None)
        if win is not None:
            win.reopen()
            return
        cfg = load_config()
        key = load_api_key(cfg)
        win = Spotlight(self, cfg, key)
        self._win = win
        if cfg.get("resident", True):
            self.hold()           # hidden window: keep the process alive
        if not key:
            win._show_hint(
                "⚠ No API key — set api_key in "
                "~/.config/hermes-spotlight/config.json or "
                "API_SERVER_KEY in ~/.hermes/.env")
        win.present()
        win.entry.grab_focus()


if __name__ == "__main__":
    App().run(None)