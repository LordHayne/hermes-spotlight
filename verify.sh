#!/usr/bin/env bash
# hermes-spotlight verification suite — one command, all layers.
#
#   ./verify.sh          headless logic + launcher + highlighter + CSS
#   ./verify.sh --full   additionally: install/uninstall round-trip
#                        (touches ~/.local/bin and custom.ron — backups
#                        are taken and restored automatically)
#
# Exits non-zero on any failure. No display needed for the default mode.
set -u
FULL=0
[ "${1:-}" = "--full" ] && FULL=1
PASS=0; FAIL=0
V()  { if [ "$1" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $2"; else FAIL=$((FAIL+1)); echo "FAIL: $2"; fi; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
W="$HERE/hermes-spotlight.py"

load() {  # load the widget module headlessly (no .py suffix on install)
/usr/bin/python3 - "$1" <<'PY'
import importlib.util, importlib.machinery, sys, os
path = sys.argv[1] if os.path.exists(sys.argv[1]) else \
    os.path.expanduser("~/.local/bin/hermes-spotlight")
spec = importlib.util.spec_from_file_location("hs", path,
    loader=importlib.machinery.SourceFileLoader("hs", path))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
sys.modules["hs"] = m
PY
}

echo "== hermes-spotlight verify ($([ $FULL -eq 1 ] && echo full || echo quick)) =="

# ---------------------------------------------------------------- static
/usr/bin/python3 -c "import ast; ast.parse(open('$W').read())"; V $? "widget syntax"
bash -n "$HERE/install.sh";   V $? "install.sh syntax"
bash -n "$HERE/uninstall.sh"; V $? "uninstall.sh syntax"

# ------------------------------------------------------------ core logic
/usr/bin/python3 - <<'PY' && V 0 "markdown-lite + XSS escaping (all themes)" || V 1 "markdown-lite"
import importlib.util, importlib.machinery
spec = importlib.util.spec_from_file_location("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py",
    loader=importlib.machinery.SourceFileLoader("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for name, t in m.THEMES.items():
    out = m._md_inline("**b** *i* `c` [l](u) <x> # H\n- li", t["inline_code_fg"], t["code_bg"], t["accent"])
    assert "<b>b</b>" in out and "&lt;x&gt;" in out and t["accent"] in out, name
print("ok")
PY

/usr/bin/python3 - <<'PY' && V 0 "syntax highlighter: balanced pango markup + 8 edge cases" || V 1 "syntax highlighter"
import importlib.util, importlib.machinery
spec = importlib.util.spec_from_file_location("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py",
    loader=importlib.machinery.SourceFileLoader("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cases = ["echo 'hello' # c", "", "a = 'unterminated", "if [ -f /x ]; then echo fi; fi",
         "x = 3.14 + 2j", "@dec\ndef f():\n    pass", "# only comment",
         'def f(n):\n    """doc"""\n    a, b = 0, 1  # <tags>\n    yield a\n']
for t in m.THEMES.values():
    syn = t["syntax"]
    for c in cases:
        out = m._highlight_code(c, syn)
        assert out.count("<span") == out.count("</span>"), repr(c)
        assert not any(0xE000 <= ord(ch) <= 0xF8FF for ch in out), repr(c)
print("ok")
PY

/usr/bin/python3 - <<'PY' && V 0 "SSE parser (keepalive / bad json / stop)" || V 1 "SSE parser"
import importlib.util, importlib.machinery, io
spec = importlib.util.spec_from_file_location("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py",
    loader=importlib.machinery.SourceFileLoader("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
h = m._StreamHandle.__new__(m._StreamHandle)
h.resp = io.BytesIO(b": keepalive\n\nevent: assistant.delta\ndata: {\"delta\": \"ok\"}\n\nevent: x\ndata: [bad\n\n")
h.stopped = False
evs = list(h.events())
assert ("assistant.delta", {"delta": "ok"}) in evs
print("ok")
PY

/usr/bin/python3 - <<'PY' && V 0 "config merge/migrate + api-key priority + session paths" || V 1 "config"
import importlib.util, importlib.machinery, os, json, tempfile
spec = importlib.util.spec_from_file_location("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py",
    loader=importlib.machinery.SourceFileLoader("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
tmp = tempfile.mkdtemp(prefix="hs-verify-cfg-")
m.CONFIG_PATH = os.path.join(tmp, "config.json")
cfg = m.load_config()
assert cfg["session_file"].endswith("/hermes-spotlight/session")
json.dump({"theme": "midnight"}, open(m.CONFIG_PATH, "w"))
cfg = m.load_config()
assert cfg["theme"] == "midnight" and "session_file" in cfg
envf = os.path.join(tmp, "env"); open(envf, "w").write("API_SERVER_KEY=envkey\n")
c2 = dict(cfg); c2["api_key"] = ""; c2["env_path"] = envf
assert m.load_api_key(c2) == "envkey"
print("ok")
PY

# ------------------------------------------------------------- launcher
/usr/bin/python3 - <<'PY' && V 0 "desktop parser (field codes / NoDisplay / shlex)" || V 1 "desktop parser"
import importlib.util, importlib.machinery, os, tempfile
spec = importlib.util.spec_from_file_location("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py",
    loader=importlib.machinery.SourceFileLoader("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
tmp = tempfile.mkdtemp(prefix="hs-verify-launch-")
p = os.path.join(tmp, "g.desktop")
open(p, "w").write("[Desktop Entry]\nType=Application\nName=My App\nExec=myapp --foo %U\n")
a = m._parse_desktop_file(p)
assert a and a["argv"] == ["myapp", "--foo"]
open(p, "w").write("[Desktop Entry]\nType=Application\nName=X\nExec=x\nNoDisplay=true\n")
assert m._parse_desktop_file(p) is None
print("ok")
PY

/usr/bin/python3 - <<'PY' && V 0 "app index + ranking + dedup" || V 1 "app index + ranking"
import importlib.util, importlib.machinery, os, json, tempfile
spec = importlib.util.spec_from_file_location("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py",
    loader=importlib.machinery.SourceFileLoader("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
tmp = tempfile.mkdtemp(prefix="hs-verify-idx-")
m.DESKTOP_CACHE = os.path.join(tmp, "apps.json")
apps = m.build_app_index(force=True)
assert len(apps) > 20 and any("steam" in a["name_l"] for a in apps)
assert m.build_app_index() == apps                       # cache hit
fake = [{"name": "Steam", "name_l": "steam", "keywords": "", "argv": ["s"], "id": "1"},
        {"name": "Steam", "name_l": "steam", "keywords": "", "argv": ["s"], "id": "2"},
        {"name": "Steam Link", "name_l": "steam link", "keywords": "", "argv": ["s"], "id": "3"}]
r = m.match_apps("steam", fake)
assert len(r) == 2 and {a["id"] for a in r} == {"1", "3"}  # dedup by name
assert m.match_apps("zzz", fake) == []
print("ok")
PY

/usr/bin/python3 - <<'PY' && V 0 "CSS: gradient + selection + all selectors (all themes)" || V 1 "CSS"
import importlib.util, importlib.machinery
spec = importlib.util.spec_from_file_location("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py",
    loader=importlib.machinery.SourceFileLoader("hs", "/home/thomas/hermes-spotlight/hermes-spotlight.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for tname in m.THEMES:
    css = m.build_css({"theme": tname, "width": 700, "api_base": "x",
                       "history_file": "/tmp/x", "session_file": "/tmp/y"}).decode()
    assert "linear-gradient(to bottom" in css and "selection" in css, tname
    for sel in ["entry ", ".msg-user", ".msg-ai", ".codeblock", ".sugg", ".suggsel"]:
        assert sel in css, (tname, sel)
print("ok")
PY

# ------------------------------------------------- UI (headless Broadway)
# Real GTK widgets on an invisible display; own app id, so a running
# spotlight is never touched. Skipped when gtk4-broadwayd is missing.
if command -v gtk4-broadwayd >/dev/null; then
    BW=":$((40 + RANDOM % 50))"
    # broadwayd names the socket after display + 1
    BWSOCK="${XDG_RUNTIME_DIR:-/tmp}/broadway$(( ${BW#:} + 1 )).socket"
    rm -f "$BWSOCK"            # stale socket from a killed run
    gtk4-broadwayd "$BW" >/dev/null 2>&1 & BWPID=$!
    for _ in $(seq 1 40); do   # wait for the display socket
        [ -S "$BWSOCK" ] && break
        sleep 0.05
    done
    UI_ERR="$(mktemp)"
    UI_OUT="$(GDK_BACKEND=broadway BROADWAY_DISPLAY="$BW" timeout 30 \
        /usr/bin/python3 "$HERE/tests/ui_headless.py" "$W" 2>"$UI_ERR")"
    UI_RC=$?
    echo "$UI_OUT" | grep -E "^(PASS|FAIL)" | sed 's/^/  /'
    V $UI_RC "UI: launcher rows, sizing, batching, copy, resident"
    [ $UI_RC -ne 0 ] && grep -iv warn "$UI_ERR" | tail -5
    rm -f "$UI_ERR"

    # fast path: a second launch hands over to the resident instance
    export HERMES_SPOTLIGHT_APP_ID="test.spot.verify$$"
    GDK_BACKEND=broadway BROADWAY_DISPLAY="$BW" /usr/bin/python3 "$W" >/dev/null 2>&1 & SPID=$!
    OBJ="/$(echo "$HERMES_SPOTLIGHT_APP_ID" | tr . /)"
    for _ in $(seq 1 60); do
        gdbus introspect --session --dest "$HERMES_SPOTLIGHT_APP_ID" --object-path "$OBJ" >/dev/null 2>&1 && break
        sleep 0.05
    done
    T0=$(date +%s%N)
    GDK_BACKEND=broadway BROADWAY_DISPLAY="$BW" timeout 5 /usr/bin/python3 "$W" >/dev/null 2>&1
    RC=$?; MS=$(( ($(date +%s%N) - T0) / 1000000 ))
    kill -0 "$SPID" 2>/dev/null; ALIVE=$?
    [ $RC -eq 0 ] && [ $ALIVE -eq 0 ] && [ $MS -lt 150 ]
    V $? "resident fast path: re-open in ${MS} ms (< 150), instance kept"
    kill "$SPID" "$BWPID" 2>/dev/null; wait "$SPID" "$BWPID" 2>/dev/null
    rm -f "$BWSOCK"            # broadwayd leaves it behind on SIGTERM
    unset HERMES_SPOTLIGHT_APP_ID
else
    echo "SKIP: UI tests (gtk4-broadwayd not installed)"
fi

# ---------------------------------------------------- full: round trip
if [ $FULL -eq 1 ]; then
    SC="$HOME/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom.ron"
    BIN="$HOME/.local/bin/hermes-spotlight"
    DT="$HOME/.local/share/applications/hermes-spotlight.desktop"
    BKUP="$(mktemp /tmp/hs-verify-custom-XXXXXX.ron)"
    [ -f "$SC" ] && cp "$SC" "$BKUP"
    trap '[ -f "$BKUP" ] && cp "$BKUP" "$SC" && rm -f "$BKUP"; echo "[cleanup] custom.ron restored"; rm -rf /tmp/hs-verify-*' EXIT
    cat > "$SC" <<'EOF'
{
    (modifiers: [Alt], key: "p", description: Some("User binds our binary himself")): Spawn("/home/thomas/.local/bin/hermes-spotlight"),
}
EOF
    XDG_CURRENT_DESKTOP="COSMIC" bash "$HERE/install.sh" >/dev/null 2>&1; V $? "install (foreign entry present)"
    [ "$(grep -c '"Hermes Spotlight"' "$SC")" -eq 1 ] && V 0 "idempotent: one entry" || V 1 "idempotent: one entry"
    grep -q "himself" "$SC" && V 0 "user's own binding preserved" || V 1 "user's own binding preserved"
    [ "$(grep -c '^{$' "$SC")" -eq 1 ] && V 0 "one RON map" || V 1 "one RON map"
    [ -x "$BIN" ] && V 0 "binary executable" || V 1 "binary executable"
    desktop-file-validate "$DT" 2>/dev/null; V $? "desktop-file-validate"
    bash "$HERE/uninstall.sh" >/dev/null 2>&1; V $? "uninstall"
    [ ! -f "$BIN" ] && V 0 "binary removed" || V 1 "binary removed"
    grep -q "himself" "$SC" && V 0 "user's entry survives uninstall" || V 1 "user's entry survives uninstall"
    XDG_CURRENT_DESKTOP="COSMIC" bash "$HERE/install.sh" >/dev/null 2>&1; V $? "reinstall (working state)"
fi

echo
echo "=== RESULT: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]