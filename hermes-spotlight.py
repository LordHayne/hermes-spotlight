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
import html
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
from gi.repository import Gtk, GLib, Gdk, Pango

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
selection {{ background-color: rgba(122, 162, 247, 0.35); }}
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
.sugg {{ padding: 6px 12px; border-radius: 8px; margin: 1px 2px; }}
.sugglabel {{ color: {t['text']}; font-size: 14px; }}
.sugghint {{ color: {t['placeholder']}; font-size: 11px; }}
.suggsel {{ background: {t['accent_bg_hover']}; }}
.codeblock {{
  background: {t['code_bg']}; color: {t['code_fg']};
  padding: 10px 12px; margin: 6px 0;
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
            try:
                lbl.set_markup(_highlight_code(part.rstrip("\n"),
                                                theme["syntax"]))
            except Exception:
                lbl.set_text(part.rstrip("\n"))
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
# SSE stream handle
# --------------------------------------------------------------------------
class _StoppedError(Exception):
    pass


class _StreamHandle:
    def __init__(self, base, key, sid, text):
        req = urllib.request.Request(
            f"{base}/api/sessions/{sid}/chat/stream",
            data=json.dumps({"message": text}).encode(),
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
        self._last_grow_len = 0

        w = int(cfg.get("width", 700))
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(w, 72)

        # --- search entry --------------------------------------------------
        self.entry = Gtk.Entry(placeholder_text=PLACEHOLDER)
        self.entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.PRIMARY, "system-search-symbolic")
        self.entry.connect("activate", self._on_send)
        ek = Gtk.EventControllerKey()
        ek.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(ek)
        # live app suggestions while typing
        self.entry.connect("changed", self._on_entry_changed)

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
            self.set_size_request(int(self.cfg.get("width", 700)), 72)
            return
        for app in apps:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("sugg")
            if app.get("icon"):
                icon = self._app_icon(app["icon"])
                if icon is not None:
                    row.append(icon)
            lbl = Gtk.Label(label=app["name"], xalign=0,
                            ellipsize=Pango.EllipsizeMode.END, hexpand=True)
            lbl.add_css_class("sugglabel")
            row.append(lbl)
            hint = Gtk.Label(label="⏎ start", xalign=1)
            hint.add_css_class("sugghint")
            row.append(hint)
            click = Gtk.GestureClick()
            click.connect("released", lambda *_a, a=app: self._launch_app(a))
            row.add_controller(click)
            self.sugg_box.append(row)
            self.sugg_rows.append(row)
        self.sugg_box.set_visible(True)
        self.set_size_request(int(self.cfg.get("width", 700)),
                              72 + 8 + len(apps) * 34)

    def _app_icon(self, icon_name: str):
        """Load an app icon: absolute path or icon-theme name, 20px."""
        if icon_name.startswith("/"):
            if os.path.exists(icon_name):
                try:
                    tex = Gdk.Texture.new_from_filename(icon_name)
                    img = Gtk.Image.new_from_paintable(tex)
                    img.set_pixel_size(20)
                    return img
                except Exception:
                    return None
            return None
        try:
            theme = Gtk.IconTheme.get_for_display(self.get_display())
            pb = theme.lookup_icon(icon_name, None, 20, 1, 1,
                                   Gtk.TextDirection.NONE,
                                   Gtk.IconLookupFlags.FORCE_REGULAR)
            return Gtk.Image.new_from_paintable(pb)
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
            if keyval == Gdk.KEY_Up:
                self._app_sel = max(0, self._app_sel - 1)
            else:
                self._app_sel = len(self.sugg_rows) - 1 \
                    if self._app_sel + 1 >= len(self.sugg_rows) \
                    else self._app_sel + 1
            for i, row in enumerate(self.sugg_rows):
                row.set_has_tooltip(i == self._app_sel)  # cheap visual tick
                if i == self._app_sel:
                    row.add_css_class("suggsel")
                else:
                    row.remove_css_class("suggsel")
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
        self._closed = True
        if self.session_id:
            self._save_session(self.session_id)
        self.get_application().quit()
        return True

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

    def _grow(self, extra=0):
        total = extra
        for child in list(self.flow):
            for lbl in list(child):
                t = lbl.get_text() or ""
                total += len(t) // 62 + t.count("\n") + 1
        self.set_size_request(int(self.cfg.get("width", 700)),
                              min(600, 100 + total * 23))

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
        apps = self._last_sugg if (self.sugg_rows and not force_ask) else []
        if apps:
            sel = apps[self._app_sel] if 0 <= self._app_sel < len(apps) \
                else apps[0]
            self._launch_app(sel)
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
        # per-send state: fresh AI bubble, fresh growth tracker,
        # stale widget refs cleared (old bubbles may be removed already)
        self._ai_bubble = None
        self._status = None
        self._stream_lbl = None
        self._ai_prepped = False
        self._last_grow_len = 0
        self._set_user_bubble(text)
        self._grow()
        self._set_busy(True)
        threading.Thread(target=self._worker, args=(text,), daemon=True).start()

    def _new_conversation(self):
        self._stop_stream()
        self.session_id = None
        # clear widget refs — the flow children are removed below, stale
        # refs would swallow later hints/errors into removed widgets
        self._ai_bubble = None
        self._status = None
        self._stream_lbl = None
        self._ai_prepped = False
        self._last_grow_len = 0
        self._save_session("")
        child = self.flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        self.set_size_request(int(self.cfg.get("width", 700)), 72)
        self._set_busy(False)
        self.entry.set_placeholder_text("New conversation — ask anything…")

    def _stop_stream(self):
        if self._stream is not None:
            self._stream.stop()
            self._set_busy(False)

    def _worker(self, text):
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
                handle = _StreamHandle(self.cfg["api_base"], self.key,
                                        self.session_id, text)
                self._stream = handle
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
                if i < attempts:
                    GLib.idle_add(self._set_busy, True,
                                  f"Gateway waking up… ({i}/{attempts-1})")
                    time.sleep(5)
                else:
                    GLib.idle_add(self._finish,
                                  f"⚠ Gateway unreachable: {e}\n"
                                  f"Is it running? "
                                  f"`systemctl --user status hermes-gateway`")
                    return

    def _on_event(self, ev, p):
        if ev == "assistant.delta":
            GLib.idle_add(self._append_delta, p.get("delta", ""))
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

    def _append_delta(self, delta):
        if self._closed:
            return False
        lbl = getattr(self, "_stream_lbl", None)
        if lbl is not None:
            new = lbl.get_text() + delta
            lbl.set_text(new)
            # throttle: resize only when the wrapped line count changed
            if len(new) // 62 != self._last_grow_len // 62:
                self._last_grow_len = len(new)
                self._grow()
        else:
            self._grow()
        self._scroll_down()
        return False

    def _finish(self, content):
        if self._closed:
            return False
        self._stream = None
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
        super().__init__(application_id="com.hermes.spotlight")

    def do_activate(self):
        # Reuse the existing window: a second activation (e.g. pressing the
        # shortcut while the spotlight is open) must focus, not duplicate.
        win = getattr(self, "_win", None)
        if win is None:
            cfg = load_config()
            key = load_api_key(cfg)
            win = Spotlight(self, cfg, key)
            self._win = win
            if not key:
                win._show_hint(
                    "⚠ No API key — set api_key in "
                    "~/.config/hermes-spotlight/config.json or "
                    "API_SERVER_KEY in ~/.hermes/.env")
        win.present()
        win.entry.grab_focus()


if __name__ == "__main__":
    App().run(None)