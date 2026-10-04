"""Headless feature checks for hermes-spotlight against a fake gateway (run
via verify.sh on a GTK Broadway display): /stop + Ctrl+C mid-answer,
401/404 recovery, live markdown, links, image paste, highlighted-text
context, notification for hidden answers. Never touches the real
gateway or the real D-Bus name."""
import os, sys, json, tempfile, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
os.environ["HERMES_SPOTLIGHT_APP_ID"] = "test.spot.features"
import importlib.util, importlib.machinery
p = sys.argv[1]
spec = importlib.util.spec_from_file_location(
    "hs", p, loader=importlib.machinery.SourceFileLoader("hs", p))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf

res = []
def ok(c, msg): res.append(bool(c)); print(("PASS " if c else "FAIL ") + msg)

# ------------------------------------------------------------ fake gateway
GOOD_KEY = "good"
sessions = set()
chats = []                  # (sid, message) of accepted chat requests
sysmsgs = []                # system_message of each accepted chat request
disconnects = []

class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def _authed(self):
        if self.headers.get("Authorization") != f"Bearer {GOOD_KEY}":
            self._json(401, {"error": {"message": "Invalid API key"}})
            return False
        return True

    def do_GET(self):
        if not self._authed(): return
        sid = self.path.rsplit("/", 1)[-1]
        self._json(200 if sid in sessions else 404, {"id": sid})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self._authed(): return
        if self.path == "/api/sessions":
            sid = f"s{len(sessions) + 1}"; sessions.add(sid)
            return self._json(200, {"session": {"id": sid}})
        sid = self.path.split("/")[3]
        if sid not in sessions:
            return self._json(404, {"error": {"code": "session_not_found"}})
        msg = body["message"]; chats.append((sid, msg))
        sysmsgs.append(body.get("system_message") or "")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        def ev(name, data):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
        try:
            if "SLOW" in json.dumps(msg):
                for i in range(200):           # until the client hangs up
                    ev("assistant.delta", {"delta": f"tok{i} "})
                    time.sleep(0.05)
                return
            text = "Hello **world**\n\nsecond https://example.org"
            for part in ("Hello **wor", "ld**\n\nsec", "ond https://example.org"):
                ev("assistant.delta", {"delta": part}); time.sleep(0.02)
            ev("assistant.completed", {"content": text})
        except (BrokenPipeError, ConnectionResetError):
            disconnects.append(sid)

srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp()
cfg = dict(m.DEFAULT_CONFIG)
cfg.update(api_base=f"http://127.0.0.1:{srv.server_port}", api_key=GOOD_KEY,
           history_file=os.path.join(tmp, "hist"),
           session_file=os.path.join(tmp, "session"),
           system_context=False, selection_context=False)
m.load_config = lambda: cfg

# ------------------------------------------------------------ helpers
def bubble_text(w):
    """All label text inside a widget tree."""
    out = []
    def walk(x):
        if isinstance(x, Gtk.Label) and x.get_visible():
            out.append(x.get_text())
        c = x.get_first_child()
        while c is not None:
            walk(c); c = c.get_next_sibling()
    walk(w)
    return "\n".join(out)

def until(cond, timeout=5.0):
    """Generator helper: poll cond every 50 ms (yield from until(...))."""
    t = time.time()
    while not cond() and time.time() - t < timeout:
        yield 50
    return cond()

def send(win, text):
    win.entry.set_text(text); win._on_send(force_ask=True)

# ------------------------------------------------------------ steps
def steps(win, app):
    win._t0 = time.time() + 1e9      # never auto-hide on focus loss

    # 1 links
    out = m._md_inline("see [docs](https://a.b/c?x=1&y=2) and https://e.org/p.",
                       "#fff", "#000", "#7aa2f7")
    ok('<a href="https://a.b/c?x=1&amp;y=2">docs</a>' in out
       and '<a href="https://e.org/p">https://e.org/p</a>.' in out,
       "links: markdown + bare URL clickable, trailing dot excluded")
    lbl = Gtk.Label(); lbl.set_markup(out)
    ok(lbl.get_text().startswith("see docs and https://e.org/p"),
       "links: label accepts the markup")
    ok("<a" not in m._md_inline("[x](file:///etc/passwd)", "#f", "#0", "#1"),
       "links: non-http targets stay unclickable")

    # 2 live markdown split (never inside a code fence)
    win._new_conversation(); win._prep_ai_bubble()
    for d in ("para **one**\n\npara two\n\n```py\nx\n\n", "y\n```\n\nta", "il"):
        win._append_delta(d)
    kids = list(win._live_box)
    ok(len(kids) == 2 and isinstance(kids[1], Gtk.Overlay)
       and "one" in kids[0].get_text() and "**" not in kids[0].get_text(),
       "live markdown: paragraphs + code block rendered while streaming")
    ok(win._stream_lbl.get_text() == "tail", "live markdown: unfinished tail stays plain")
    win._append_delta("x\n\n```sh\necho a\n\n")
    ok(len(list(win._live_box)) == 3, "live markdown: no split inside an open fence")
    first = kids[0]
    win._finish(win._stream_text + "echo b\n```")
    ok(list(win._live_box)[0] is first, "final render keeps streamed widgets (no flicker)")

    # 2b copying out of answers
    btns = [w for w in win._ai_bubble if isinstance(w, Gtk.Button)]
    ok(len(btns) == 1 and btns[0].has_css_class("answercopy"),
       "copy-answer button under the final answer")
    lbl = next(w for w in win._live_box if isinstance(w, Gtk.Label))
    lbl.select_region(5, 8)          # mouse selection; focus stays in entry
    win.entry.grab_focus()
    ok(win._selected_answer_text() == lbl.get_text()[5:8],
       "selected answer text found while the entry has focus")
    ok(win._on_copy_key(None, Gdk.KEY_c, 0, Gdk.ModifierType.CONTROL_MASK),
       "Ctrl+C handled at window level")
    got = []
    win.get_clipboard().read_text_async(None, lambda c, r: got.append(c.read_text_finish(r)))
    yield from until(lambda: got, 2)
    ok(got and got[0] == lbl.get_text()[5:8], "clipboard holds the selected answer text")
    btns[0].emit("clicked"); got.clear()
    win.get_clipboard().read_text_async(None, lambda c, r: got.append(c.read_text_finish(r)))
    yield from until(lambda: got, 2)
    ok(got and got[0].startswith("para **one**") and got[0].endswith("echo b\n```"),
       "copy-answer copies the whole markdown source")
    lbl.select_region(0, 0)
    ok(not win._on_copy_key(None, Gdk.KEY_c, 0, Gdk.ModifierType.CONTROL_MASK),
       "Ctrl+C without selection falls through")

    # 3 401 (stale key) + 404 (deleted session) recovered in one send
    win._new_conversation()
    win.key = "stale"; win.session_id = "gone"
    send(win, "hi")
    yield from until(lambda: not win._busy)
    ok(win.key == GOOD_KEY, "401: key re-read from config, request retried")
    ok(win.session_id == "s1" and chats == [("s1", "hi")],
       "404: new session created, prompt sent exactly once")
    txt = bubble_text(win._ai_bubble)
    ok("second" in txt and "**" not in txt, "answer rendered after recovery")

    # 4 /stop mid-answer
    send(win, "SLOW one")
    yield from until(lambda: "tok3" in win._stream_text)
    win.entry.set_text("/stop"); win.entry.emit("activate")
    ok(not win._busy, "/stop works while busy")
    txt = bubble_text(win._ai_bubble)
    ok("⏹ Stopped" in txt and "tok3" in txt, "stopped answer keeps partial text + note")
    yield from until(lambda: "s1" in disconnects, 3)
    ok("s1" in disconnects, "stop closes the stream (gateway interrupts the agent)")
    yield 300
    ok(bubble_text(win._ai_bubble) == txt, "no late deltas after stop")

    # 5 Ctrl+C mid-answer
    send(win, "SLOW two")
    yield from until(lambda: "tok2" in win._stream_text)
    win.entry.set_text("")
    handled = win._on_capture_key(None, Gdk.KEY_c, 0, Gdk.ModifierType.CONTROL_MASK)
    ok(handled and not win._busy, "Ctrl+C stops while busy")
    ok(not win._on_capture_key(None, Gdk.KEY_c, 0, Gdk.ModifierType.CONTROL_MASK),
       "Ctrl+C idle: normal copy")

    # 6 image paste -> chip -> multimodal message
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 64, 48)  # RGBA like real screenshots
    pb.fill(0x7aa2f7ff)
    win._clip.set_content(Gdk.ContentProvider.new_for_value(Gdk.Texture.new_for_pixbuf(pb)))
    yield 100
    handled = win._on_capture_key(None, Gdk.KEY_v, 0, Gdk.ModifierType.CONTROL_MASK)
    yield from until(lambda: win._pending_image is not None, 2)
    ok(handled and (win._pending_image or "").startswith("data:image/jpeg;base64,"),
       "Ctrl+V with image in clipboard attaches it")
    ok(win.chip_box.get_visible(), "image chip shown")
    n = len(chats)
    send(win, "")                    # image alone is enough to send
    yield from until(lambda: not win._busy)
    msg = chats[n][1] if len(chats) > n else None
    ok(isinstance(msg, list) and msg[1]["type"] == "image_url"
       and msg[1]["image_url"]["url"].startswith("data:image/jpeg"),
       "image sent as multimodal content")
    ok(not win.chip_box.get_visible() and win._pending_image is None,
       "chip cleared after send")
    win._clip.set_content(Gdk.ContentProvider.new_for_value("plain text"))
    yield 100
    ok(not win._on_capture_key(None, Gdk.KEY_v, 0, Gdk.ModifierType.CONTROL_MASK),
       "Ctrl+V with text: normal paste")

    # 7 highlighted-text context
    win._pending_sel = "def foo(): return 42"; win._refresh_chips()
    ok(win.chip_box.get_visible(), "selection chip shown")
    n = len(chats)
    send(win, "what does this do")
    yield from until(lambda: not win._busy)
    msg = chats[n][1] if len(chats) > n else ""
    ok(msg.startswith("Context:") and "def foo()" in msg
       and msg.endswith("what does this do"), "highlighted text sent as context")
    ok(win._sel_seen == "def foo(): return 42" and not win.chip_box.get_visible(),
       "used selection is not offered again")

    # 8 notification for an answer that finishes hidden
    sent = []
    app.send_notification = lambda nid, n: sent.append((nid, n))
    send(win, "hidden")
    win.set_visible(False)
    yield from until(lambda: not win._busy)
    ok(len(sent) == 1 and sent[0][0] == "answer", "hidden answer -> notification")
    app.activate_action("show", None)
    ok(win.get_visible(), "notification action re-opens the window")

    # 9 ghost text from history
    hist = ["wetter graz", "wetter wien", "wetter graz"]
    ok(m.ghost_completion("Wet", hist) == "ter graz"
       and m.ghost_completion("wetter w", hist) == "ien"
       and m.ghost_completion("w", hist) == ""
       and m.ghost_completion("/st", ["/status"]) == "",
       "ghost: most frequent match, min 2 chars, never for /commands")
    win._hist = hist
    win.entry.set_text("wet"); win.entry.set_position(-1)
    yield 100
    win._update_ghost()
    ok(win._ghost_rest == "ter graz" and win.ghost.get_visible(),
       "ghost shown behind the typed text")
    ok(win.ghost.get_margin_start() > 20, f"ghost aligned to the text area (x={win.ghost.get_margin_start()})")
    ok(win._on_capture_key(None, Gdk.KEY_Tab, 0, 0)
       and win.entry.get_text() == "wetter graz",
       "Tab accepts the completion")
    win.entry.set_text("wet"); win.entry.set_position(1); win._update_ghost()
    ok(not win.ghost.get_visible(), "no ghost when the cursor is not at the end")
    win.entry.set_text("")

    # 10 /status card, live refresh, frozen by /new
    win.entry.set_text("/status"); win.entry.emit("activate")
    yield from until(lambda: "CPU" in win._status_widgets, 3)
    ok(list(win._status_widgets)[:2] == ["CPU", "RAM"],
       f"status card rows: {list(win._status_widgets)}")
    txt = bubble_text(win._status_card[0])
    ok("threads" in txt and "GB" in txt, "status card shows threads + memory")
    ok(chats[-1][1] != "/status", "/status is local, never sent to the agent")
    v = win._status_widgets["CPU"][1]; v.set_text("stale")
    yield from until(lambda: v.get_text() != "stale", 4)
    ok(v.get_text() != "stale", "card refreshes itself")
    win._new_conversation()
    ok(win._status_card is None, "/new freezes the card")

    # 11 weather card
    card = ('```card\n{"type": "weather", "place": "Wien", "title": "Di 6.10.",'
            ' "icon": "sun", "min": 8, "max": 19, "summary": "wolkenlos",'
            ' "days": [{"day": "Di", "icon": "sun", "min": 8, "max": 19, "best": true},'
            ' {"day": "Do", "icon": "storm", "min": 12, "max": 19, "note": "Schirm"}]}\n```')
    ws = m._md_widgets("Dienstag wird schön.\n\n" + card + "\n\nViel Spaß!", win.theme)
    ok(len(ws) == 3 and ws[1].has_css_class("card")
       and not any(isinstance(w, Gtk.Overlay) for w in ws),
       "card block renders as a card, not as code")
    t = bubble_text(ws[1])
    ok("Wien · Di 6.10." in t and "8° → 19°" in t and "Schirm" in t and "⛈️" in t,
       "weather card shows place, range, days, icons")
    ok(len(m._md_widgets("x\n\n```card\n{broken\n```", win.theme)) == 1
       and len(m._md_widgets('```card\n{"type": "nope"}\n```', win.theme)) == 0,
       "broken/unknown cards are dropped silently")
    st = m.strip_cards("a\n\n" + card + "\n\nb")
    ok("```" not in st and st.startswith("a") and st.endswith("b"),
       "copy/notify text has no card JSON")
    win._new_conversation(); win._prep_ai_bubble()
    win._append_delta("Sonnig.\n\nMehr Text ```card\n{\"type\": \"wea")
    ok("```" not in win._stream_lbl.get_text() and "Mehr Text" in win._stream_lbl.get_text(),
       "half-streamed card JSON is hidden")
    win._finish("Sonnig.\n\nMehr Text\n\n" + card)
    ok(any(w.has_css_class("card") for w in win._live_box), "final answer shows the card")
    btn = [w for w in win._ai_bubble if isinstance(w, Gtk.Button)][0]
    btn.emit("clicked"); got = []
    win.get_clipboard().read_text_async(None, lambda c, r: got.append(c.read_text_finish(r)))
    yield from until(lambda: got, 2)
    ok(got and "card" not in got[0] and got[0].endswith("Mehr Text"),
       "copy-answer leaves the card JSON out")
    r = m.with_card_reminder
    ok("weather or forecast" in r("wie wird das Wetter morgen?")
       and r("erklär mir python") == "erklär mir python",
       "weather questions carry the card reminder, others don't")
    ok("appointments" in r("welche Termine hab ich morgen?")
       and "package" in r("wo ist mein DHL Paket?")
       and r("frag hermes nach dem sinn des lebens") == "frag hermes nach dem sinn des lebens",
       "reminders per topic; 'hermes' (agent name) is never a carrier trigger")
    both = r("wo ist mein Paket und wie wird das Wetter?")
    ok("package" in both and "weather" in both and both.count("(Spotlight:") == 1,
       "several topics share one reminder")

    # 12 events + package cards
    ev = m._card_widget(json.dumps({"type": "events", "title": "Mon, Oct 5",
        "events": [{"time": "all day", "title": "Mom's birthday"},
                   {"time": "09:30", "end": "10:00", "title": "Standup", "place": "Zoom", "next": True},
                   {"time": "14:00", "title": "Dentist", "place": "Main St 4"}]}))
    t = bubble_text(ev) if ev else ""
    ok(ev is not None and "Mon, Oct 5" in t and "09:30\n10:00" in t and "Zoom" in t
       and "3" in t, "events card: title, times, places, count")
    rows = []
    def walk(x):
        if x.has_css_class("eventrow"): rows.append(x)
        c = x.get_first_child()
        while c is not None: walk(c); c = c.get_next_sibling()
    walk(ev)
    ok(len(rows) == 3 and rows[1].has_css_class("next"), "next appointment highlighted")
    pk = m._card_widget(json.dumps({"type": "package", "title": "Keyboard",
        "carrier": "DHL", "tracking": "00340434161094042557", "stage": "out",
        "status": "Out for delivery", "eta": "today 10–14",
        "events": [{"time": "Oct 5 07:12", "text": "Loaded onto vehicle"}]}))
    t = bubble_text(pk) if pk else ""
    segs = []
    def walk2(x):
        if x.has_css_class("pkgseg"): segs.append(x)
        c = x.get_first_child()
        while c is not None: walk2(c); c = c.get_next_sibling()
    walk2(pk)
    ok(pk is not None and "DHL · 00340434161094042557" in t and "today 10–14" in t
       and "Loaded onto vehicle" in t, "package card: carrier, tracking, eta, events")
    ok([sg.has_css_class("done") for sg in segs] == [True, True, True, True, False],
       "package progress filled up to 'out for delivery'")
    bad = m._card_widget('{"type": "package", "stage": "problem", "events": "x"}')
    ok(bad is not None, "package card tolerates odd fields (events not a list)")
    ok(sysmsgs and "```card" in sysmsgs[-1] and '"type": "weather"' in sysmsgs[-1],
       "card format is sent to the agent in the system message")

    # 13 blocks: the agent's own cards
    spec = {"type": "blocks", "icon": "🎮", "title": "GPU compare", "subtitle": "1440p",
            "value": "4070 wins", "blocks": [
        {"kind": "text", "text": "Close race, **4070** is quieter."},
        {"kind": "stats", "items": [{"label": "FPS", "value": "142", "highlight": True},
                                    {"label": "Watt", "value": "200", "note": "less"}]},
        {"kind": "bars", "items": [{"label": "4070", "value": 92, "text": "92 pts"}]},
        {"kind": "list", "items": [{"lead": "1", "title": "RTX 4070", "sub": "best value", "highlight": True}, "plain item"]},
        {"kind": "kv", "items": [["VRAM", "12 GB"], {"key": "TDP", "value": "200 W"}]},
        {"kind": "table", "columns": ["Card", "Price"], "rows": [["4070", "549 €"], ["7800 XT", "499 €"]]},
        {"kind": "progress", "steps": ["ordered", "shipped", "here"], "current": 1},
        {"kind": "chips", "items": ["DLSS", "quiet"]},
        {"kind": "nonsense", "items": [1]},
        {"kind": "actions", "items": [{"label": "Details", "ask": "details on the RTX 4070"},
                                      {"label": "Shop", "url": "https://example.org"},
                                      {"label": "Evil", "url": "file:///etc/passwd"}]}]}
    bc = m._card_widget(json.dumps(spec), win.theme)
    t = bubble_text(bc) if bc else ""
    ok(bc is not None and all(x in t for x in ("GPU compare", "4070 wins", "142", "92 pts",
       "best value", "plain item", "12 GB", "200 W", "549 €", "shipped", "DLSS")),
       "blocks card renders every block kind")
    kids = list(bc) if bc else []
    ok(len(kids) == 1 + 9, f"unknown block kinds are skipped ({len(kids)} children)")
    btns = []
    def walkb(x):
        if isinstance(x, Gtk.Button): btns.append(x)
        c = x.get_first_child()
        while c is not None: walkb(c); c = c.get_next_sibling()
    walkb(bc)
    ok([b.get_label() for b in btns] == ["Details", "Shop"], "only ask/https buttons are created")
    tb = m._blk_table({"columns": ["", "A"], "rows": [["Boost", "5 GHz"]]}, win.theme)
    cells = [tb.get_child_at(0, 1), tb.get_child_at(1, 1)]
    ok(cells[0].has_css_class("tablekey") and cells[1].has_css_class("tablecell"),
       "empty first header -> first column styled as row labels")
    tb2 = m._blk_table({"columns": ["Name", "A"], "rows": [["x", "y"]]}, win.theme)
    ok(tb2.get_child_at(0, 1).has_css_class("tablecell"), "named first column stays a normal cell")
    ok(m._card_widget('{"type": "blocks", "blocks": [{"kind": "x"}]}') is None,
       "a blocks card with nothing valid is dropped")
    ok("\"type\": \"blocks\"" in m.CARD_PROMPT, "blueprint is part of the card prompt")
    ok("do\nnot repeat" in m.CARD_PROMPT or "not repeat" in " ".join(m.CARD_PROMPT.split()),
       "prompt tells the model not to repeat card data in the text")
    win._new_conversation(); win._prep_ai_bubble()
    win._finish("Here you go.\n\n```card\n" + json.dumps(spec) + "\n```")
    n = len(chats)
    btns.clear(); walkb(win._ai_bubble)
    btns[0].activate()               # "Details" -> win.ask
    yield from until(lambda: not win._busy and len(chats) > n)
    ok(len(chats) > n and chats[-1][1] == "details on the RTX 4070",
       "follow-up button sends its question")

    # 14 duplicate lists next to a card are hidden
    lst = {"type": "blocks", "title": "Top", "blocks": [{"kind": "list", "items": [
        {"title": "Counter-Strike 2"}, {"title": "Dota 2"}, {"title": "Valheim"}]}]}
    blk = "```card\n" + json.dumps(lst) + "\n```"
    ans = ("Top 3:\n\n1. **Counter-Strike 2** — 1.2M\n2. **Dota 2** — 800k\n3. **Valheim** — 220k\n\n"
           + blk + "\n\nCS2 leads by far.")
    d = m.dedupe_card_lists(ans)
    ok("1. **Counter" not in d and "Top 3:" in d and "CS2 leads" in d and blk in d,
       "list repeated in the card is dropped, prose and card stay")
    other = "Tips:\n\n- drink water\n- sleep\n\n" + blk
    ok(m.dedupe_card_lists(other) == other, "a list that is not in the card stays")
    plain = "1. **Counter-Strike 2**\n2. **Dota 2**"
    ok(m.dedupe_card_lists(plain) == plain, "no card -> nothing removed")
    win._new_conversation(); win._prep_ai_bubble()
    win._append_delta(ans[:60])          # list already streamed in
    win._finish(ans)
    t = bubble_text(win._ai_bubble)
    ok("1.2M" not in t and "Counter-Strike 2" in t and "CS2 leads" in t,
       "final render hides the streamed duplicate list")

    # 15 images: MEDIA:/path and ![alt](src)
    img = os.path.join(tmp, "chart.png")
    pbi = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 1280, 720)
    pbi.fill(0x3d59a1ff); pbi.savev(img, "png", [], [])
    ws = m._md_widgets(f"Here is the chart:\n\nMEDIA:{img}\n\nWhat it shows.", win.theme)
    ok([type(w).__name__ for w in ws] == ["Label", "Picture", "Label"],
       "MEDIA:/path line renders as an inline picture")
    ok(ws[1].get_size_request()[1] <= m.IMAGE_MAX_HEIGHT and ws[1].get_size_request()[0] > 0,
       f"picture scaled to fit (size {ws[1].get_size_request()})")
    ws = m._md_widgets(f"![chart]({img})\n\n![remote](https://example.org/c.png)", win.theme)
    ok(isinstance(ws[0], Gtk.Picture) and isinstance(ws[1], Gtk.Label)
       and "🖼 remote" in ws[1].get_text() and "example.org" in ws[1].get_label(),
       "local ![alt](path) inline, web image becomes a link (not fetched)")
    ws = m._md_widgets("MEDIA:/nope/missing.png\n\n![x](/etc/passwd)", win.theme)
    ok(all(isinstance(w, Gtk.Label) for w in ws) and "missing.png" in bubble_text(ws[0]),
       "missing or non-image files stay as text")
    ws = m._md_widgets(f"see ![inline]({img}) here", win.theme)
    ok(len(ws) == 1 and isinstance(ws[0], Gtk.Label), "images inside a sentence are left alone")

    app.release(); win._closed = True; app.quit()

def run(gen):
    try:
        delay = next(gen)
    except StopIteration:
        return False
    except Exception as e:
        import traceback; traceback.print_exc()
        ok(False, f"step crashed: {e}"); app.quit(); return False
    GLib.timeout_add(delay or 1, run, gen)
    return False

class TApp(m.App):
    def do_activate(self):
        first = getattr(self, "_win", None) is None
        m.App.do_activate(self)
        if first:
            self._win._autotest_mode = False
            GLib.timeout_add(300, run, steps(self._win, self))

app = TApp()
GLib.timeout_add_seconds(60, app.quit)
app.run(None)
srv.shutdown()
print(f"== {sum(res)}/{len(res)} passed")
sys.exit(0 if res and all(res) else 1)
