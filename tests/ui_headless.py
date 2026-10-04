"""Headless UI checks for hermes-spotlight (run via verify.sh on a GTK
Broadway display): launcher rows, measured sizing, delta batching, copy
button, resident hide/re-show. Never touches the real D-Bus name."""
import os, sys, tempfile, threading
os.environ["HERMES_SPOTLIGHT_APP_ID"]="test.spot.ui"
import importlib.util, importlib.machinery
p=sys.argv[1]
spec=importlib.util.spec_from_file_location("hs",p,loader=importlib.machinery.SourceFileLoader("hs",p))
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from gi.repository import Gtk, GLib, Gio, Gdk
res=[]
def ok(c,msg): res.append(c); print(("PASS " if c else "FAIL ")+msg)
cfg=dict(m.DEFAULT_CONFIG); cfg["history_file"]=os.path.join(tempfile.mkdtemp(), "hist")
m.load_config=lambda: cfg
m.load_api_key=lambda c: ""
def steps(win):
    launched=[]; win._launch_app=lambda a: launched.append(a["name"])
    # 1 suggestions
    q = next(a["name"] for a in win._apps)[:4]
    win.entry.set_text(q); n=len(win._last_sugg)
    ok(len(win.sugg_rows)==n+1 and win.sugg_rows[0].has_css_class("suggsel") and win.sugg_rows[-1].has_css_class("suggask"), f"suggestions '{q}': {n} apps + ask row, first preselected")
    win.entry.emit("activate"); ok(len(launched)==1, "Enter launches first app")
    win.entry.set_text(q); win._select_sugg(99)
    ok(win._app_sel==n and win.sugg_rows[-1].has_css_class("suggsel"), "Down clamps to Ask row")
    win.entry.emit("activate"); ok(len(launched)==1 and not win.sugg_rows, "Enter on Ask row asks instead of launching")
    # 2 sizing
    win._new_conversation(); win._grow(); h0=win.get_size_request()[1]
    win._set_user_bubble("question"); win._prep_ai_bubble()
    def feed():
        for i in range(400): win._queue_delta(f"word{i} ")
    calls=[0]; orig=win._append_delta
    def counting(d): calls[0]+=1; return orig(d)
    win._append_delta=counting
    t=threading.Thread(target=feed); t.start(); t.join()
    def after_stream():
        txt=win._stream_lbl.get_text()
        ok(txt.count("word")==400 and txt.endswith("word399 "), f"400 deltas arrive complete, in order")
        ok(calls[0]<=3, f"batched into {calls[0]} UI update(s) instead of 400")
        h1=win.get_size_request()[1]
        ok(h0 < h1 <= 600, f"height grows with content: {h0} -> {h1} (cap 600)")
        win._finish("short **answer**\n```python\nprint('hi')\n```")
        h2=win.get_size_request()[1]
        ok(h2 < h1, f"height shrinks for short final answer: {h2}")
        ov=[w for w in win._ai_bubble if isinstance(w, Gtk.Overlay)]
        ok(len(ov)==1, "code block rendered with overlay")
        btn=[c for c in ov[0] if isinstance(c, Gtk.Button)][0]
        btn.emit("clicked")
        ok(btn.get_icon_name()=="object-select-symbolic", "copy button ticks")
        def got(cb, r):
            try: ok(cb.read_text_finish(r)=="print('hi')", "clipboard holds code")
            except Exception as e: ok(False, f"clipboard: {e}")
            win._new_conversation(); ok(win.get_size_request()[1]==h0 and not win.scroll.get_visible(), "/new shrinks back")
            # 3 resident
            win.close()
            def check_hidden():
                ok(not win.get_visible() and not win._closed, "Esc/close hides, process stays")
                app.activate()
                ok(win.get_visible(), "re-activation shows the same window")
                app.release(); win._closed=True; app.quit(); return False
            GLib.timeout_add(100, check_hidden)
        btn.get_clipboard().read_text_async(None, got)
        return False
    GLib.timeout_add(150, after_stream)
    return False
class TApp(m.App):
    def do_activate(self):
        first = getattr(self,"_win",None) is None
        m.App.do_activate(self)
        if first: self._win._autotest_mode=False; GLib.timeout_add(300, steps, self._win)
app=TApp()
GLib.timeout_add_seconds(15, app.quit)
app.run(None)
print(f"== {sum(res)}/{len(res)} passed")
sys.exit(0 if res and all(res) else 1)
