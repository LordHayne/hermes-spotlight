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
from gi.repository import Gtk, GLib, GObject, Gdk, GdkPixbuf, Gio, Pango

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
    "selection_context": True,          # offer highlighted text as context
    "notify": True,                     # desktop notification when an answer
                                        # finishes while the window is hidden
    "ghost_suggestions": True,          # grey completion from your history
    "cards": True,                      # native cards (weather) in answers
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

PLACEHOLDER = "Ask Hermes…   (↑ history, /new, /stop, /status)"
BUSY_HINT = "Hermes is thinking…   (/stop or Ctrl+C to stop)"
SEL_MAX = 8000                  # chars of highlighted text sent as context


# --------------------------------------------------------------------------
# Debug log: "debug": true in config.json (or HERMES_SPOTLIGHT_DEBUG=1)
# appends clipboard/selection/focus events to
# ~/.cache/hermes-spotlight/debug.log — for desktop-specific issues that
# headless tests cannot see.
# --------------------------------------------------------------------------
DEBUG_LOG = os.path.expanduser("~/.cache/hermes-spotlight/debug.log")
_debug = bool(os.environ.get("HERMES_SPOTLIGHT_DEBUG"))


def _log(msg: str):
    if not _debug:
        return
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} {msg}\n")
    except OSError:
        pass


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
# /status — local system snapshot, no agent round trip
# --------------------------------------------------------------------------
def _cpu_ranges(s: str) -> set:
    """'0-5,12-17' -> {0..5, 12..17}"""
    out = set()
    for part in s.strip().split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


def _compact_ranges(nums) -> str:
    """{6..11, 18..23} -> '6–11, 18–23'"""
    nums, out = sorted(nums), []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"{nums[i]}–{nums[j]}" if j > i else str(nums[i]))
        i = j + 1
    return ", ".join(out)


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _cpu_times():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return idle, sum(v)


def _hwmon_temp(names) -> float | None:
    base = "/sys/class/hwmon"
    try:
        for h in sorted(os.listdir(base)):
            if _read(f"{base}/{h}/name") in names:
                t = _read(f"{base}/{h}/temp1_input")
                if t:
                    return int(t) / 1000
    except OSError:
        pass
    return None


STEAM_LIBS = [
    "~/.local/share/Steam/steamapps",
    "~/.steam/steam/steamapps",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps",
]


def _steam_game_name(appid: str) -> str:
    for lib in STEAM_LIBS:
        txt = _read(os.path.expanduser(f"{lib}/appmanifest_{appid}.acf"))
        m = re.search(r'"name"\s+"([^"]+)"', txt)
        if m:
            return m.group(1)
    return f"Steam app {appid}"


def _running_games() -> list:
    """Steam games (the SteamLaunch reaper carries AppId=…), deduplicated."""
    ids = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if "SteamLaunch" in cmd:
                m = re.search(r"AppId=(\d+)", cmd)
                if m and m.group(1) not in ids and m.group(1) != "0":
                    ids.append(m.group(1))
    except OSError:
        pass
    return [_steam_game_name(i) for i in ids]


def _gpu_status() -> dict | None:
    """NVIDIA via nvidia-smi, else AMD/Intel via sysfs (amdgpu)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,clocks.gr,"
             "memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            f = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
            num = lambda s: float(s) if re.match(r"^[\d.]+$", s) else None
            return {"name": f[0].replace("NVIDIA GeForce ", ""),
                    "util": num(f[1]), "clock": num(f[2]),
                    "vram_used": num(f[3]), "vram_total": num(f[4]),
                    "temp": num(f[5]), "power": num(f[6])}
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    for card in sorted(os.listdir("/sys/class/drm")) if os.path.isdir(
            "/sys/class/drm") else []:
        dev = f"/sys/class/drm/{card}/device"
        busy = _read(f"{dev}/gpu_busy_percent")
        if not re.match(r"^card\d+$", card) or not busy:
            continue
        used, total = _read(f"{dev}/mem_info_vram_used"), \
            _read(f"{dev}/mem_info_vram_total")
        clock = re.search(r"(\d+)Mhz \*", _read(f"{dev}/pp_dpm_sclk"))
        temp = None
        try:
            for h in os.listdir(f"{dev}/hwmon"):
                t = _read(f"{dev}/hwmon/{h}/temp1_input")
                temp = int(t) / 1000 if t else None
        except OSError:
            pass
        return {"name": "GPU", "util": float(busy),
                "clock": float(clock.group(1)) if clock else None,
                "vram_used": int(used) / 2**20 if used else None,
                "vram_total": int(total) / 2**20 if total else None,
                "temp": temp, "power": None}
    return None


def collect_status() -> dict:
    """One snapshot for the /status card. Blocks ~250 ms (CPU load is the
    difference of two /proc/stat samples) — call it off the UI thread."""
    i0, t0 = _cpu_times()
    time.sleep(0.25)
    i1, t1 = _cpu_times()
    st = {"cpu_load": 100 * (1 - (i1 - i0) / max(1, t1 - t0))}
    st["cpu_temp"] = _hwmon_temp({"k10temp", "zenpower", "coretemp"})
    online = _cpu_ranges(_read("/sys/devices/system/cpu/online") or "0")
    present = _cpu_ranges(_read("/sys/devices/system/cpu/present") or "0")
    st["threads"] = (len(online), len(present))
    st["parked"] = _compact_ranges(present - online)
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        mem[k] = int(v.split()[0]) if v.split() else 0
    st["ram"] = ((mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) / 2**20,
                 mem.get("MemTotal", 0) / 2**20)
    st["gpu"] = _gpu_status()
    st["games"] = _running_games()
    st["apps"] = _running_apps(6)
    return st


def status_rows(st: dict) -> list:
    """(label, fraction 0..1 or None, value text) rows for the card."""
    rows = []
    cpu = [f"{st['cpu_load']:.0f} %"]
    if st.get("cpu_temp") is not None:
        cpu.append(f"{st['cpu_temp']:.0f} °C")
    on, total = st["threads"]
    cpu.append(f"{on}/{total} threads" +
               (f" · {st['parked']} parked" if st["parked"] else ""))
    rows.append(("CPU", st["cpu_load"] / 100, " · ".join(cpu)))
    used, total = st["ram"]
    rows.append(("RAM", used / total if total else None,
                 f"{used:.1f} / {total:.0f} GB"))
    g = st.get("gpu")
    if g:
        val = [g["name"]]
        if g["util"] is not None:
            val.append(f"{g['util']:.0f} %")
        if g["clock"] is not None:
            val.append(f"{g['clock']:.0f} MHz")
        if g["temp"] is not None:
            val.append(f"{g['temp']:.0f} °C")
        if g["power"] is not None:
            val.append(f"{g['power']:.0f} W")
        rows.append(("GPU", (g["util"] or 0) / 100, " · ".join(val)))
        if g["vram_used"] is not None and g["vram_total"]:
            rows.append(("VRAM", g["vram_used"] / g["vram_total"],
                         f"{g['vram_used'] / 1024:.1f} / "
                         f"{g['vram_total'] / 1024:.0f} GB"))
    if st.get("games"):
        rows.append(("Game", None, ", ".join(st["games"])))
    if st.get("apps"):
        rows.append(("Apps", None, ", ".join(st["apps"])))
    return rows


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
    global _debug
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    _debug = _debug or bool(cfg.get("debug"))
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
.chiplabel {{ color: {t['ai_text']}; }}
label link {{ color: {t['accent']}; }}
label link:hover {{ text-decoration: underline; }}
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
.answercopy {{ margin: 2px -6px -4px 0; }}
.ghost {{ color: {t['placeholder']}; font-size: 15px; }}
.card {{ background: {t['accent_bg']}; border: 1px solid {t['edge']};
        border-radius: 12px; padding: 12px 14px; margin: 6px 0; }}
.cardicon {{ font-size: 34px; }}
.cardtitle {{ color: {t['text']}; font-weight: bold; font-size: 15px; }}
.cardsub {{ color: {t['placeholder']}; font-size: 12px; }}
.cardtemp {{ color: {t['text']}; font-weight: bold; font-size: 20px; }}
.daytile {{ background: rgba(255, 255, 255, 0.03); border-radius: 10px;
           padding: 6px 4px; }}
.daytile.best {{ background: {t['accent_bg_hover']}; }}
.dayname {{ color: {t['placeholder']}; font-weight: bold; font-size: 11px; }}
.dayicon {{ font-size: 22px; }}
.daytemp {{ color: {t['text']}; font-size: 12px; }}
.daynote {{ color: {t['accent']}; font-size: 10px; }}
.statustitle {{ color: {t['accent']}; font-weight: bold; font-size: 12px;
               margin-bottom: 6px; }}
.statuskey {{ color: {t['placeholder']}; font-size: 12px; }}
.statusval {{ color: {t['ai_text']}; font-size: 12px; }}
levelbar {{ min-width: 110px; }}
levelbar trough {{ background: rgba(255, 255, 255, 0.07); border: none;
                  border-radius: 3px; min-height: 6px; padding: 0; }}
levelbar block {{ border: none; border-radius: 3px; min-height: 6px; }}
levelbar block.filled {{ background: {t['accent']}; }}
levelbar.hot block.filled {{ background: {t['syntax']['number']}; }}
levelbar block.empty {{ background: transparent; }}
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
    # one pass for [text](url) and bare URLs, so a link's own text is
    # never linked twice; only http(s) becomes clickable
    def _link(m):
        label, url, bare = m.group(1), m.group(2), m.group(3)
        if bare:
            return f'<a href="{bare}">{bare}</a>'
        if re.match(r"https?://", url):
            return f'<a href="{url}">{label}</a>'
        return f'<span foreground="{accent}" underline="single">{label}</span>'
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)"
                  r"|(?<![\w\"=/>])(https?://[^\s<]*[^\s<.,;:!?)\]'\"*])",
                  _link, text)
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


def _md_split_point(text: str, start: int) -> int:
    """Last paragraph break after `start` that lies outside a code fence,
    or -1. Everything before it can be rendered as Markdown while the rest
    of the answer is still streaming."""
    idx = text.rfind("\n\n")
    while idx > start:
        if text.count("```", 0, idx) % 2 == 0:
            return idx
        idx = text.rfind("\n\n", 0, idx)
    return -1


def ghost_completion(typed: str, history: list) -> str:
    """Rest of the most frequent (then most recent) earlier question that
    starts with what is typed — shown greyed out, Tab/→ accepts it."""
    t = typed.lower()
    if len(t.strip()) < 2 or t.startswith("/"):
        return ""
    counts, last = {}, {}
    for i, h in enumerate(history):
        if len(h) > len(typed) and h.lower().startswith(t):
            counts[h] = counts.get(h, 0) + 1
            last[h] = i
    if not counts:
        return ""
    best = max(counts, key=lambda h: (counts[h], last[h]))
    return best[len(typed):]


def _with_selection(question: str, sel: str) -> str:
    """Prepend highlighted text from another app as context."""
    return (f"Context: text I have highlighted on my screen:\n"
            f'"""\n{sel}\n"""\n\n{question}')


# --------------------------------------------------------------------------
# Cards: the agent appends a ```card block with JSON (see CARD_PROMPT) and
# the spotlight renders it natively. Unknown or broken cards are dropped,
# the text answer always stands on its own.
# --------------------------------------------------------------------------
CARD_PROMPT = """\
Rich cards: the user's spotlight renders some answers as native cards.
When the user asks about the weather or a forecast, answer normally and
then append exactly one fenced block (real data only, omit unknown fields):
```card
{"type": "weather", "place": "<city>", "title": "<day/date asked about>",
 "icon": "<icon>", "min": <°C>, "max": <°C>, "summary": "<short conditions>",
 "days": [{"day": "<short day name>", "icon": "<icon>", "min": <°C>,
           "max": <°C>, "note": "<1-2 words, optional>",
           "best": <true on the nicest day, optional>}]}
```
icon is one of: sun, partly, cloud, fog, showers, rain, storm, snow, wind.
Up to 7 days. Write texts in the user's language. Never mention the card."""

WEATHER_ICONS = {"sun": "☀️", "clear": "☀️", "partly": "🌤️", "cloud": "☁️",
                 "fog": "🌫️", "showers": "🌦️", "rain": "🌧️", "storm": "⛈️",
                 "snow": "🌨️", "wind": "💨"}

_CARD_RE = re.compile(r"```card[ \t]*\n?.*?```[ \t]*\n?", re.S)


def strip_cards(text: str) -> str:
    """Answer text without card blocks (copy, notifications)."""
    return _CARD_RE.sub("", text).strip()


def _temp(v) -> str:
    try:
        return f"{round(float(v))}°"
    except (TypeError, ValueError):
        return ""


def _lbl(text, css, **kw):
    lbl = Gtk.Label(label=str(text), **kw)
    lbl.add_css_class(css)
    return lbl


def _weather_card(d: dict):
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    card.add_css_class("card")
    head = Gtk.Box(spacing=12)
    head.append(_lbl(WEATHER_ICONS.get(d.get("icon"), "🌡️"), "cardicon",
                     valign=Gtk.Align.CENTER))
    titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                     valign=Gtk.Align.CENTER)
    title = " · ".join(str(x) for x in (d.get("place"), d.get("title")) if x)
    titles.append(_lbl(title or "Weather", "cardtitle", xalign=0,
                       ellipsize=Pango.EllipsizeMode.END))
    if d.get("summary"):
        titles.append(_lbl(d["summary"], "cardsub", xalign=0, wrap=True))
    head.append(titles)
    lo, hi = _temp(d.get("min")), _temp(d.get("max"))
    if lo or hi:
        head.append(_lbl(f"{lo} → {hi}" if lo and hi else lo or hi,
                         "cardtemp", valign=Gtk.Align.CENTER))
    card.append(head)
    days = [x for x in d.get("days") or [] if isinstance(x, dict)][:7]
    if days:
        row = Gtk.Box(spacing=6, homogeneous=True)
        for day in days:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            tile.add_css_class("daytile")
            if day.get("best"):
                tile.add_css_class("best")
            tile.append(_lbl(day.get("day", ""), "dayname"))
            tile.append(_lbl(WEATHER_ICONS.get(day.get("icon"), "🌡️"),
                             "dayicon"))
            lo, hi = _temp(day.get("min")), _temp(day.get("max"))
            tile.append(_lbl(f"{hi} / {lo}" if lo and hi else hi or lo,
                             "daytemp"))
            if day.get("note"):
                tile.append(_lbl(day["note"], "daynote",
                                 ellipsize=Pango.EllipsizeMode.END))
            row.append(tile)
        card.append(row)
    return card


CARD_RENDERERS = {"weather": _weather_card}


def _card_widget(src: str):
    """JSON card -> widget, or None (unknown type / broken JSON)."""
    try:
        data = json.loads(src)
        return CARD_RENDERERS[data["type"]](data)
    except Exception:
        return None


def _md_widgets(text: str, theme: dict) -> list:
    widgets = []
    # split -> [text, lang, code, text, lang, code, …, text]
    parts = re.split(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", text, flags=re.S)
    for i, part in enumerate(parts):
        if i % 3 == 1 or not part.strip():
            continue
        if i % 3 == 2 and parts[i - 1] == "card":
            card = _card_widget(part)
            if card is not None:
                widgets.append(card)
            continue
        if i % 3 == 2:
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
            # blank lines around a code block/card would render as gaps
            part = part.strip("\n")
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
        if pb.get_has_alpha():
            # screenshots are RGBA; the jpeg encoder rejects alpha
            w, h = pb.get_width(), pb.get_height()
            flat = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
            flat.fill(0xffffffff)
            pb.composite(flat, 0, 0, w, h, 0, 0, 1, 1,
                         GdkPixbuf.InterpType.NEAREST, 255)
            pb = flat
        ok, buf = pb.save_to_bufferv("jpeg", ["quality"], ["82"])
        if not ok:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(buf).decode()
    except Exception:
        return None


def _has_image(fmts) -> bool:
    """Clipboard offers an image: image/* mime types from other apps, or
    a texture (any subclass) set inside this process."""
    if fmts is None:
        return False
    if any(t.startswith("image/") for t in fmts.get_mime_types() or []):
        return True
    return any(GObject.type_is_a(g, Gdk.Texture)
               for g in fmts.get_gtypes() or [])


def _texture_to_data_url(texture: Gdk.Texture) -> str | None:
    """Clipboard texture (GTK4) -> downscaled jpeg data URL."""
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write_bytes(texture.save_to_png_bytes())
        loader.close()
        pb = loader.get_pixbuf()
        return _pixbuf_to_data_url(pb) if pb is not None else None
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
        # bumped on every send, /stop and /new: callbacks of an older
        # request see a different value and drop themselves
        self._gen = 0
        self._stream_text = ""      # full streamed answer so far
        self._md_done = 0           # chars of it already rendered as markdown
        self._live_box = None       # markdown widgets of the streamed part
        self._stick_bottom = True
        self._scrolling_programmatic = False
        self._status_card = None    # live /status bubble while refreshing
        self._status_widgets = {}
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
        # Ctrl+V image paste (screenshot tools put PNG into the clipboard).
        # Capture phase: must run before GtkText's own paste shortcut.
        pk = Gtk.EventControllerKey(
            propagation_phase=Gtk.PropagationPhase.CAPTURE)
        pk.connect("key-pressed", self._on_capture_key)
        self.entry.add_controller(pk)
        self._clip = Gdk.Display.get_default().get_clipboard()
        self._pending_image: str | None = None    # data URL
        self._pending_sel: str | None = None      # highlighted text
        self._sel_seen = ""         # last highlighted text sent/dismissed
        self._want_sel = bool(cfg.get("selection_context", True))
        # On Wayland the selection offer arrives with keyboard focus, which
        # can be a moment after the window turns active — so also re-read
        # when it changes shortly after opening.
        self._prim = Gdk.Display.get_default().get_primary_clipboard()
        self._sel_until = 0.0
        self._prim.connect("changed", self._on_prim_changed)

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
        # ghost text: an overlay label whose first part (the typed text) is
        # transparent, so the grey completion lines up behind the caret
        self.ghost = Gtk.Label(xalign=0, halign=Gtk.Align.FILL,
                               valign=Gtk.Align.CENTER, can_target=False,
                               ellipsize=Pango.EllipsizeMode.END,
                               visible=False)
        self.ghost.add_css_class("ghost")
        self._ghost_rest = ""
        overlay.add_overlay(self.ghost)
        self._entry_overlay = overlay
        self.entry.connect("changed", self._update_ghost)
        self.entry.connect("notify::cursor-position", self._update_ghost)
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
        vadj = self.scroll.get_vadjustment()
        vadj.connect("changed", self._on_adj_changed)
        vadj.connect("value-changed", self._on_adj_value_changed)
        self.scroll.set_visible(False)

        # --- app suggestions (launcher mode) --------------------------------
        self._apps = build_app_index()
        self._app_sel = -1
        self._last_sugg = []
        self.sugg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                margin_top=4, visible=False)
        self.sugg_rows = []
        self.chip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                spacing=6, margin_top=6, visible=False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("spot")
        box.append(overlay)
        box.append(self.chip_box)
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
        # Ctrl+C copies text selected in an answer, whichever widget has
        # the keyboard focus (a mouse selection leaves it in the entry)
        cc = Gtk.EventControllerKey(
            propagation_phase=Gtk.PropagationPhase.CAPTURE)
        cc.connect("key-pressed", self._on_copy_key)
        self.add_controller(cc)
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
                return [l.rstrip("\n") for l in f if l.strip()][-500:]
        except OSError:
            return []

    def _save_history(self):
        try:
            os.makedirs(os.path.dirname(self.cfg["history_file"]),
                        exist_ok=True)
            with open(self.cfg["history_file"], "w") as f:
                f.write("\n".join(self._hist[-500:]))
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

    # ------------------------------------------------------- ghost text
    def _update_ghost(self, *_):
        text = self.entry.get_text()
        rest = ""
        if (self.cfg.get("ghost_suggestions", True)
                and self.entry.get_position() == len(text)
                and not self.entry.get_selection_bounds()):
            rest = ghost_completion(text, self._hist)
        self._ghost_rest = rest
        if not rest:
            self.ghost.set_visible(False)
            return
        # line the label up with the entry's text area
        txt = self.entry.get_first_child()
        while txt is not None and not isinstance(txt, Gtk.Text):
            txt = txt.get_next_sibling()
        ok, r = (txt.compute_bounds(self._entry_overlay) if txt is not None
                 else (False, None))
        typed_w = self.ghost.create_pango_layout(text).get_pixel_size()[0]
        if not ok or r.get_width() < 1 or typed_w > r.get_width() - 20:
            self._ghost_rest = ""       # text scrolled: no reliable position
            self.ghost.set_visible(False)
            return
        self.ghost.set_margin_start(int(r.get_x()))
        self.ghost.set_margin_end(max(0, int(self._entry_overlay.get_width()
                                             - r.get_x() - r.get_width())))
        self.ghost.set_markup(f'<span alpha="1">{html.escape(text)}</span>'
                              f'{html.escape(rest)}')
        self.ghost.set_visible(True)

    # ------------------------------------------------------- /status card
    def _show_status(self):
        title = Gtk.Label(label="System", xalign=0)
        title.add_css_class("statustitle")
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        bubble = self._bubble([title, grid], "msg-ai")
        self._status_card = (bubble, grid)
        self._status_widgets = {}
        self._scroll_down(force=True)
        self._refresh_status(bubble)

    def _refresh_status(self, bubble):
        """Collect off the UI thread, fill in, repeat every 2 s while this
        card is the live one (a new question or /new freezes it)."""
        if self._closed or not self._status_card or \
                self._status_card[0] is not bubble:
            return False
        if not self.get_visible():
            GLib.timeout_add(2000, self._refresh_status, bubble)
            return False

        def work():
            try:
                rows = status_rows(collect_status())
            except Exception as e:
                rows = [("⚠", None, f"status failed: {e}")]
            GLib.idle_add(self._fill_status, bubble, rows)
        threading.Thread(target=work, daemon=True).start()
        return False

    def _fill_status(self, bubble, rows):
        if not self._status_card or self._status_card[0] is not bubble:
            return False
        grid = self._status_card[1]
        if [r[0] for r in rows] != list(self._status_widgets):
            child = grid.get_first_child()          # rows changed: rebuild
            while child is not None:
                nxt = child.get_next_sibling()
                grid.remove(child)
                child = nxt
            self._status_widgets = {}
            for i, (key, frac, _val) in enumerate(rows):
                k = Gtk.Label(label=key, xalign=0)
                k.add_css_class("statuskey")
                grid.attach(k, 0, i, 1, 1)
                bar = None
                if frac is not None:
                    bar = Gtk.LevelBar(valign=Gtk.Align.CENTER)
                    for name in ("low", "high", "full"):
                        bar.remove_offset_value(name)
                    grid.attach(bar, 1, i, 1, 1)
                v = Gtk.Label(xalign=0, selectable=True, hexpand=True,
                              ellipsize=Pango.EllipsizeMode.END)
                v.add_css_class("statusval")
                grid.attach(v, 2 if bar else 1, i, 1 if bar else 2, 1)
                self._status_widgets[key] = (bar, v)
        for key, frac, val in rows:
            bar, v = self._status_widgets[key]
            if bar is not None:
                frac = max(0.0, min(1.0, frac or 0.0))
                bar.set_value(frac)
                (bar.add_css_class if frac > 0.85
                 else bar.remove_css_class)("hot")
            v.set_text(val)
        self._grow()
        GLib.timeout_add(2000, self._refresh_status, bubble)
        return False

    def _on_copy_key(self, _c, keyval, _kc, state):
        if not (state & Gdk.ModifierType.CONTROL_MASK
                and keyval in (Gdk.KEY_c, Gdk.KEY_C)):
            return False
        text = self._selected_answer_text()
        _log(f"copy key: focus={type(self.get_focus()).__name__}"
             f" selected={len(text)} chars")
        if not text:
            return False                # entry copy / Ctrl+C stop
        self.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(text))
        return True

    def _selected_answer_text(self) -> str:
        """Text selected in the conversation (first label with a
        selection — GTK keeps one selection at a time per click)."""
        stack = [self.flow]
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.Label) and w.get_selectable():
                has, start, end = w.get_selection_bounds()
                if has and start != end:
                    a, b = sorted((start, end))
                    return w.get_text()[a:b]
            child = w.get_last_child()
            while child is not None:
                stack.append(child)
                child = child.get_prev_sibling()
        return ""

    # ------------------------------------------------- context chips
    # Attachments shown as chips below the entry: a pasted image (vision
    # input) and/or text highlighted in another app. Both are sent with the
    # next question and can be removed with their ✕.
    def _on_capture_key(self, _c, keyval, _kc, state):
        """Entry keys seen before GtkText handles them.
        Tab/→ at the end: accept the ghost completion.
        Ctrl+V: if the clipboard holds an image, attach it instead of
        pasting text. Ctrl+C while answering (nothing selected): stop."""
        if (self._ghost_rest and keyval in (Gdk.KEY_Tab, Gdk.KEY_Right,
                                            Gdk.KEY_KP_Right)
                and not state & (Gdk.ModifierType.CONTROL_MASK
                                 | Gdk.ModifierType.SHIFT_MASK)):
            self.entry.set_text(self.entry.get_text() + self._ghost_rest)
            self.entry.set_position(-1)
            return True
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return False
        _log(f"ctrl key {Gdk.keyval_name(keyval)} state={int(state)}")
        if keyval in (Gdk.KEY_v, Gdk.KEY_V):
            return self._paste_image()
        if (keyval in (Gdk.KEY_c, Gdk.KEY_C) and self._busy
                and not self.entry.get_selection_bounds()):
            self._stop_stream()
            return True
        return False

    def _paste_image(self) -> bool:
        fmts = self._clip.get_formats()
        _log(f"paste: clipboard formats={fmts.to_string() if fmts else None}"
             f" local={self._clip.is_local()}")
        if not _has_image(fmts):
            return False

        def _done(clip, res):
            try:
                texture = clip.read_texture_finish(res)
            except Exception as e:
                _log(f"paste: read_texture failed: {e!r}")
                texture = None
            data = _texture_to_data_url(texture) if texture else None
            if data:
                self._pending_image = data
                self._refresh_chips()
            else:
                self._show_hint("⚠ Could not read the image from the clipboard")
        self._clip.read_texture_async(None, _done)
        return True

    def _offer_selection(self):
        """Read the primary selection (text highlighted anywhere) and offer
        it as a context chip. Skips our own selections and text the user
        already sent or dismissed."""
        prim = self._prim
        fmts = prim.get_formats()
        _log(f"selection: active={self.is_active()} local={prim.is_local()}"
             f" formats={fmts.to_string() if fmts else None}")
        if prim.is_local():
            self.entry.select_region(0, -1)
            return

        def _done(clip, res):
            try:
                text = (clip.read_text_finish(res) or "").strip()
            except Exception as e:
                _log(f"selection: read_text failed: {e!r}")
                text = ""
            _log(f"selection: got {len(text)} chars, seen={text == self._sel_seen}")
            if len(text) >= 2 and text != self._sel_seen:
                self._pending_sel = text[:SEL_MAX]
                self._refresh_chips()
            # only now: selecting entry text takes over the primary selection
            self.entry.select_region(0, -1)
        prim.read_text_async(None, _done)

    def _on_prim_changed(self, *_):
        _log(f"selection changed (active={self.is_active()})")
        if (self.is_active() and time.time() < self._sel_until
                and self.cfg.get("selection_context", True)):
            self._offer_selection()

    def _drop_image(self):
        self._pending_image = None
        self._refresh_chips()
        self.entry.grab_focus()

    def _drop_sel(self):
        self._sel_seen = self._pending_sel or ""
        self._pending_sel = None
        self._refresh_chips()
        self.entry.grab_focus()

    def _refresh_chips(self):
        child = self.chip_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.chip_box.remove(child)
            child = nxt
        if self._pending_image:
            pb = _data_url_to_pixbuf(self._pending_image, 32)
            icon = Gtk.Image.new_from_pixbuf(pb) if pb is not None else None
            self._add_chip(icon, "screenshot attached", "Remove image",
                           self._drop_image)
        if self._pending_sel:
            one_line = " ".join(self._pending_sel.split())
            label = "❝ " + (one_line[:48] + "…" if len(one_line) > 48
                            else one_line)
            self._add_chip(None, label, "Don't send the highlighted text",
                           self._drop_sel)
        self.chip_box.set_visible(self.chip_box.get_first_child() is not None)
        self._grow()
        return False

    def _add_chip(self, icon, label, tooltip, on_remove):
        chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chip.add_css_class("imgchip")
        if icon is not None:
            chip.append(icon)
        lbl = Gtk.Label(label=label, ellipsize=Pango.EllipsizeMode.END)
        lbl.add_css_class("chiplabel")
        chip.append(lbl)
        x = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=tooltip)
        x.add_css_class("chipx")
        x.connect("clicked", lambda *_: on_remove())
        chip.append(x)
        self.chip_box.append(chip)

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
            # the primary selection is only readable once we have focus
            _log(f"window active, want_sel={self._want_sel}")
            if self._want_sel:
                self._want_sel = False
                self._sel_until = time.time() + 2.0
                self._offer_selection()
        elif time.time() - self._t0 > 1.5 and not self._busy:
            self.close()

    def _on_close(self, *_):
        if self.session_id:
            self._save_session(self.session_id)
        if self.cfg.get("resident", True) and not self._autotest_mode:
            # stay alive hidden: the next shortcut press only re-shows the
            # window (see _activate_running_instance). A running answer
            # keeps streaming into the hidden window.
            if self._pending_sel:
                self._pending_sel = None      # re-read fresh on next open
                self._refresh_chips()
            self.set_visible(False)
            return True
        self._closed = True
        self.get_application().quit()
        return True

    def reopen(self):
        """Show the resident window again, ready for the next question."""
        self._t0 = time.time()
        self._apps = build_app_index()        # cheap: cached, 15 min TTL
        self._want_sel = bool(self.cfg.get("selection_context", True))
        self.get_application().withdraw_notification("answer")
        self.present()
        self.entry.grab_focus()
        # selecting our own entry text would take over the primary
        # selection before _offer_selection could read it (it selects
        # the entry itself once done)
        if not self._want_sel:
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

    def _notify(self, content):
        """Desktop notification for an answer that finished while the
        window was hidden; clicking it re-opens the spotlight."""
        body = " ".join(re.sub(r"[`*#>\[\]]", "",
                               strip_cards(content)).split())
        n = Gio.Notification.new("Hermes answered")
        n.set_body(body[:180] + ("…" if len(body) > 180 else ""))
        n.set_default_action("app.show")
        self.get_application().send_notification("answer", n)

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
        # finished paragraphs are rendered as markdown into live_box while
        # the unfinished tail streams into stream_lbl as plain text
        live_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        stream_lbl = Gtk.Label(label="", wrap=True, xalign=0, selectable=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR)
        b = self._bubble([status, live_box, stream_lbl], "msg-ai")
        self._status, self._stream_lbl, self._ai_bubble = status, stream_lbl, b
        self._live_box = live_box
        self._ai_prepped = True
        self._render_stream()
        return False

    def _render_stream(self):
        lbl = self._stream_lbl
        if lbl is None:
            return
        text = self._stream_text
        cut = _md_split_point(text, self._md_done)
        if cut > self._md_done:
            for w in _md_widgets(text[self._md_done:cut], self.theme):
                self._live_box.append(w)
            self._md_done = cut + 2
        tail = text[self._md_done:]
        open_card = tail.rfind("```card")
        if open_card >= 0 and tail.count("```", open_card) == 1:
            tail = tail[:open_card].rstrip()      # JSON still streaming
        lbl.set_text(tail)
        lbl.set_visible(bool(tail))

    def _render_ai_final(self, text):
        b = self._ai_bubble
        if b is None:
            return
        full = text
        b.remove(self._status)
        b.remove(self._stream_lbl)
        # keep the paragraphs already rendered while streaming when the
        # final text agrees with them, so the answer does not flicker
        done = self._stream_text[:self._md_done]
        if self._md_done and text.startswith(done):
            text = text[self._md_done:]
        else:
            child = self._live_box.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self._live_box.remove(child)
                child = nxt
        for w in _md_widgets(text, self.theme):
            self._live_box.append(w)
        # copy the whole answer (markdown source) in one click
        btn = Gtk.Button(icon_name="edit-copy-symbolic", halign=Gtk.Align.END,
                         tooltip_text="Copy answer")
        btn.add_css_class("copybtn")
        btn.add_css_class("answercopy")
        btn.connect("clicked", _copy_code, strip_cards(full))
        b.append(btn)

    def _scroll_down(self, force=False):
        # Relayout happens after this returns, so instead of setting the
        # value now (stale upper bound), the adjustment's "changed" signal
        # applies the final position while _stick_bottom is set. Only a
        # new question forces it back on — a user who scrolled up to read
        # stays where they are while the answer streams.
        if force:
            self._stick_bottom = True

    def _on_adj_changed(self, adj):
        target = adj.get_upper() - adj.get_page_size()
        if self._stick_bottom and abs(adj.get_value() - target) > 0.5:
            self._scrolling_programmatic = True
            adj.set_value(target)

    def _on_adj_value_changed(self, adj):
        if self._scrolling_programmatic:
            self._scrolling_programmatic = False
            return
        # user moved the scrollbar: pin only while they're at the bottom
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 4
        self._stick_bottom = at_bottom

    def _grow(self, *_):
        """Size the window to its content as GTK measures it (entry,
        suggestions, conversation), capped at max_height. Past the cap
        the scrolled window scrolls; stick-to-bottom keeps the newest
        text in view."""
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
            self.entry.set_placeholder_text(hint or BUSY_HINT)
        else:
            self.spinner.stop()
            self.entry.set_placeholder_text(PLACEHOLDER)

    def _show_hint(self, text):
        """Non-busy inline hint (missing key etc.) — visible, grows window."""
        if self._closed:
            return
        if self._ai_bubble is None:
            self._prep_ai_bubble()
        self._set_tool_status(text)
        self._grow()
        self._scroll_down(force=True)

    # --------------------------------------------------------------- send
    def _on_send(self, force_ask=False, *_):
        text = self.entry.get_text().strip()
        low = text.lower()
        # slash commands first: /stop must work while an answer is running
        if low in ("/stop", "/stopp"):
            self.entry.set_text("")
            self._stop_stream()
            return
        if low in ("/new", "/neu"):
            self.entry.set_text("")
            self._new_conversation()
            return
        image, sel = self._pending_image, self._pending_sel
        if self._busy or not (text or image or sel):
            return
        if low in ("/status", "/system"):
            self.entry.set_text("")
            self._show_suggestions([])
            self._show_status()
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
            self.key = load_api_key(load_config())    # added since start?
        if not self.key:
            self._show_hint("⚠ No API key — set api_key in "
                            "~/.config/hermes-spotlight/config.json or "
                            "API_SERVER_KEY in ~/.hermes/.env")
            return
        if text:
            self._hist.append(text)
            self._hist_idx = len(self._hist)
            self._save_history()
        question = text or ("What is on this screenshot?" if image
                            else "Explain this.")
        message = _with_selection(question, sel) if sel else question
        if sel:
            self._sel_seen = sel
        self.entry.set_text("")
        self._pending_image = self._pending_sel = None
        self._refresh_chips()
        # per-send state: fresh AI bubble, fresh stream buffer, stale
        # widget refs cleared (old bubbles may be removed already)
        self._gen += 1
        self._status_card = None        # freeze a live /status card
        self._drain_deltas()            # late deltas of a stopped answer
        self._ai_bubble = None
        self._status = None
        self._stream_lbl = None
        self._live_box = None
        self._ai_prepped = False
        self._stream_text = ""
        self._md_done = 0
        self._set_user_bubble(question + ("  📷" if image else "")
                              + ("  ❝" if sel else ""))
        self._grow()
        self._scroll_down(force=True)
        self._set_busy(True)
        threading.Thread(target=self._worker,
                         args=(message, image, self._gen),
                         daemon=True).start()

    def _new_conversation(self):
        self._stop_stream(note=None)
        self._gen += 1
        self._status_card = None
        self.session_id = None
        # clear widget refs — the flow children are removed below, stale
        # refs would swallow later hints/errors into removed widgets
        self._ai_bubble = None
        self._status = None
        self._stream_lbl = None
        self._live_box = None
        self._ai_prepped = False
        self._stream_text = ""
        self._md_done = 0
        self._save_session("")
        child = self.flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        self._grow()
        self._set_busy(False)
        self.entry.set_placeholder_text("New conversation — ask anything…")

    def _stop_stream(self, note="⏹ Stopped"):
        """Stop the running answer. Closing the SSE connection makes the
        gateway interrupt the agent; whatever streamed in so far stays,
        rendered, with `note` appended."""
        if not self._busy:
            return
        self._gen += 1                  # silence the worker's callbacks
        handle, self._stream = self._stream, None
        if handle is not None:
            handle.stop()
        self._set_busy(False)
        if note:
            self._finish_interrupted(note)

    def _if_gen(self, gen, fn, *args):
        """Idle callback guard: run fn only if its request is current."""
        if gen == self._gen:
            fn(*args)
        return False

    def _worker(self, text, image=None, gen=0):
        def post(fn, *args):
            GLib.idle_add(self._if_gen, gen, fn, *args)

        attempts = 6
        recovered = set()               # one 404/401 recovery each
        for i in range(1, attempts + 1):
            if self._closed or gen != self._gen:
                return
            try:
                if not self.session_id:
                    self.session_id = self._load_cached_session() \
                        or self._new_session()
                if not self._ai_prepped:
                    self._ai_prepped = True
                    post(self._prep_ai_bubble)
                sysmsg = "\n\n".join(filter(None, [
                    collect_system_context()
                    if self.cfg.get("system_context", True) else "",
                    CARD_PROMPT if self.cfg.get("cards", True) else "",
                ])) or None
                payload_text = (_content_with_image(text, image)
                                if image else text)
                handle = _StreamHandle(self.cfg["api_base"], self.key,
                                        self.session_id, payload_text, sysmsg)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:150]
                if e.code == 404 and "session" not in recovered:
                    # session deleted in the app/CLI: start a new one —
                    # nothing reached the agent, so resending is safe
                    recovered.add("session")
                    self.session_id = None
                    self._save_session("")
                    post(self._set_busy, True,
                         "Session is gone — starting a new one…")
                    continue
                if e.code == 401 and "key" not in recovered:
                    # API key rotated while we stayed resident: re-read it
                    recovered.add("key")
                    new_key = load_api_key(load_config())
                    if new_key and new_key != self.key:
                        self.key = new_key
                        continue
                if e.code == 401:
                    post(self._finish,
                         "⚠ Invalid API key (HTTP 401) — check API_SERVER_KEY "
                         "in ~/.hermes/.env or api_key in "
                         "~/.config/hermes-spotlight/config.json")
                    return
                post(self._finish, f"⚠ HTTP {e.code}: {body}")
                return
            except Exception as e:
                # nothing reached the agent yet — safe to retry
                if i < attempts:
                    post(self._set_busy, True,
                         f"Gateway waking up… ({i}/{attempts-1})")
                    time.sleep(5)
                    continue
                post(self._finish,
                     f"⚠ Gateway unreachable: {e}\n"
                     f"Is it running? "
                     f"`systemctl --user status hermes-gateway`")
                return
            # The request is accepted and the agent is running: never resend
            # from here on, a retry would run the prompt (and its tool
            # calls) a second time.
            self._stream = handle
            if gen != self._gen:        # stopped while connecting
                handle.stop()
                return
            try:
                for ev, payload in handle.events():
                    if gen != self._gen:
                        handle.stop()
                        return
                    self._on_event(ev, payload, post)
                return
            except urllib.error.HTTPError as e:
                post(self._finish,
                     f"⚠ HTTP {e.code}: {e.read().decode()[:150]}")
                return
            except _StoppedError:
                return
            except Exception as e:
                post(self._finish_interrupted, f"⚠ Stream lost: {e}")
                return

    def _finish_interrupted(self, note):
        """Stream broke or was stopped mid-answer: keep what already
        streamed in, append the note — the answer may continue in the
        Hermes app session."""
        self._append_delta(self._drain_deltas())
        partial = self._stream_text
        return self._finish(f"{partial}\n\n{note}" if partial else note)

    def _on_event(self, ev, p, post):
        if ev == "assistant.delta":
            self._queue_delta(p.get("delta", ""))
        elif ev == "tool.started":
            name = p.get("tool_name") or "?"
            prev = (p.get("preview") or "")[:70]
            post(self._set_tool_status, f"⚙ {name}: {prev}")
        elif ev == "tool.completed":
            post(self._set_tool_status, "")
        elif ev == "assistant.completed":
            post(self._finish, p.get("content", ""))
        elif ev == "error":
            post(self._finish, f"⚠ {p.get('message', 'error')}")

    def _set_tool_status(self, text):
        if self._closed:
            return False
        if self._status is not None:
            self._status.set_text(text)
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
        self._stream_text += delta
        self._render_stream()
        self._grow()
        return False

    def _finish(self, content):
        if self._closed:
            return False
        self._stream = None
        self._append_delta(self._drain_deltas())   # deltas still buffered
        # interrupted streams can complete with empty content — keep the
        # partial text that already streamed in
        if not content:
            content = self._stream_text or "⚠ empty response"
        # errors can arrive before any bubble was prepped — create one now
        if self._ai_bubble is None:
            self._prep_ai_bubble()
        self._render_ai_final(content)
        self._set_busy(False)
        self._grow()
        if not self.get_visible() and self.cfg.get("notify", True):
            self._notify(content)
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
        # target of the "answer finished" notification
        show = Gio.SimpleAction.new("show", None)
        show.connect("activate", lambda *_: win.reopen())
        self.add_action(show)
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