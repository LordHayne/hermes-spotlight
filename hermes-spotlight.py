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
- Optional logo button that opens the full Hermes desktop app
- Desktop-agnostic: any Wayland/X11 session, no Adw/layer-shell required

Requirements: python3 (>=3.9) with PyGObject (gi) — GTK4 only.
Talks to: http://127.0.0.1:8642 (hermes gateway API server)

Config: ~/.config/hermes-spotlight/config.json (created on first run)
Launch: bind your compositor's "Spawn/run command" shortcut to
        hermes-spotlight (see README for GNOME/KDE/COSMIC examples)
"""
import html
import json
import os
import re
import shutil
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
    },
}

PLACEHOLDER = "Ask Hermes…   (↑ history, /new, /stop)"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
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
window {{ background: {t['window_bg']}; }}
.spot {{ padding: 10px 10px 8px 10px; }}
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
.logobtn {{
  background: {t['accent_bg']};
  border: 1px solid {t['entry_border']};
  border-radius: 999px;
  padding: 3px;
}}
.logobtn:hover {{ background: {t['accent_bg_hover']}; }}
spinner {{ padding: 1px; }}
.msg-user, .msg-ai {{ padding: 8px 12px; border-radius: 10px; margin: 3px 0; }}
.msg-user {{ background: {t['user_bubble']}; color: {t['text']}; }}
.msg-ai   {{ background: {t['ai_bubble']}; color: {t['ai_text']}; }}
.msg-user text, .msg-ai text {{ color: inherit; }}
.toolstatus {{ color: {t['accent']}; font-size: 11px; padding: 2px 12px; }}
.codeblock {{
  background: {t['code_bg']}; color: {t['code_fg']};
  padding: 8px 10px; margin: 4px 0;
  border-radius: 8px;
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
# Markdown-lite
# --------------------------------------------------------------------------
def _md_inline(text: str, code_fg: str, code_bg: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`",
                  rf'<span font_family="monospace" background="{code_bg}" '
                  rf'foreground="{code_fg}">\1</span>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  r'<span foreground="#7aa2f7" underline="single">\1</span>',
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
            lbl = Gtk.Label(label=part.rstrip("\n"), wrap=True, xalign=0,
                            wrap_mode=Pango.WrapMode.WORD_CHAR,
                            selectable=True)
            lbl.add_css_class("codeblock")
        else:
            lbl = Gtk.Label(label="", wrap=True, xalign=0, selectable=True,
                            wrap_mode=Pango.WrapMode.WORD_CHAR)
            try:
                lbl.set_markup(_md_inline(part, theme["inline_code_fg"],
                                          theme["code_bg"]))
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
        self.session_cache = os.path.join(
            os.path.dirname(cfg["history_file"]), "session")
        self.session_id = None
        self._busy = False
        self._t0 = time.time()
        self._hist = self._load_history()
        self._hist_idx = len(self._hist)
        self._stream = None

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

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("spot")
        box.append(overlay)
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

    def _on_entry_key(self, _c, keyval, _k, _s):
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
            try:
                os.makedirs(os.path.dirname(self.session_cache), exist_ok=True)
                with open(self.session_cache, "w") as f:
                    f.write(self.session_id)
            except OSError:
                pass
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
    def _load_cached_session(self):
        try:
            with open(self.session_cache) as f:
                sid = f.read().strip()
            if sid:
                _get(self.cfg["api_base"], self.key, f"/api/sessions/{sid}")
                return sid
        except Exception:
            pass
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
        status = Gtk.Label(label="", xalign=0)
        status.add_css_class("toolstatus")
        stream_lbl = Gtk.Label(label="", wrap=True, xalign=0, selectable=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR)
        b = self._bubble([status, stream_lbl], "msg-ai")
        self._status, self._stream_lbl, self._ai_bubble = status, stream_lbl, b
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
        self._busy = on
        self.spinner.set_visible(on)
        if on:
            self.spinner.start()
            self.entry.set_placeholder_text(hint or "Hermes is thinking…")
        else:
            self.spinner.stop()
            self.entry.set_placeholder_text(PLACEHOLDER)

    # --------------------------------------------------------------- send
    def _on_send(self, *_):
        text = self.entry.get_text().strip()
        if not text or self._busy or not self.key:
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
        self._set_user_bubble(text)
        self._grow()
        self._set_busy(True)
        threading.Thread(target=self._worker, args=(text,), daemon=True).start()

    def _new_conversation(self):
        self.session_id = None
        try:
            os.makedirs(os.path.dirname(self.session_cache), exist_ok=True)
            open(self.session_cache, "w").close()
        except OSError:
            pass
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
            try:
                if not self.session_id:
                    self.session_id = self._load_cached_session() \
                        or self._new_session()
                GLib.idle_add(self._prep_ai_bubble)
                handle = _StreamHandle(self.cfg["api_base"], self.key,
                                        self.session_id, text)
                self._stream = handle
                for ev, payload in handle.events():
                    self._on_event(ev, payload)
                    if handle.stopped:
                        GLib.idle_add(self._finish,
                                      "⏹ Stopped — the answer still finishes "
                                      "in the background and lands in the "
                                      "session history.")
                        return
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
        lbl = getattr(self, "_status", None)
        if lbl is not None:
            lbl.set_text(text)
        return False

    def _append_delta(self, delta):
        lbl = getattr(self, "_stream_lbl", None)
        if lbl is not None:
            lbl.set_text(lbl.get_text() + delta)
        self._grow()
        self._scroll_down()
        return False

    def _finish(self, content):
        self._stream = None
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
        cfg = load_config()
        key = load_api_key(cfg)
        win = Spotlight(self, cfg, key)
        self._win = win
        win.present()
        win.entry.grab_focus()
        if not key:
            win._prep_ai_bubble()
            win._set_tool_status(
                "⚠ No API key — set api_key in ~/.config/hermes-spotlight/"
                "config.json or API_SERVER_KEY in ~/.hermes/.env")


if __name__ == "__main__":
    App().run(None)