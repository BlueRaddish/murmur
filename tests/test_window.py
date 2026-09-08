"""Run: python tests/test_window.py  — no framework, asserts only. Needs a display (Windows has one)."""
import json
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tkinter as tk
import window as W

# --- tokens ---------------------------------------------------------------------------------
for acc in ("#00ff00", "#e63c3c", "#ffffff", "#000000", "#3c8cff"):
    for dark in (False, True):
        p = W.palette(acc, dark)
        assert all(n in p for n in ("base", "layer", "card", "ctl", "ctl_hover", "ctl_press", "ink", "muted",
                                    "ring", "primary", "primary_hover", "on_primary", "tint", "tint_hover",
                                    "danger", "danger_text", "on_danger", "stroke_edge", "stroke_field",
                                    "stroke_strong", "disabled")), p
        for g in ("layer", "card", "ctl", "base"):
            assert W.contrast(p["ink"], p[g]) >= 4.5, (acc, dark, "ink on", g)
            assert W.contrast(p["muted"], p[g]) >= 4.5, (acc, dark, "muted on", g)
        assert W.contrast(p["on_primary"], p["primary"]) >= 3.0, (acc, dark, "on_primary")
        assert W.contrast(p["on_danger"], p["danger"]) >= 3.0, (acc, dark, "on_danger")
        # the focus ring is non-text UI: 3:1 against the grounds it is drawn on, or it is decoration
        for g in ("layer", "card", "base"):
            assert W.contrast(p["ring"], p[g]) >= 3.0, (acc, dark, "ring on", g, p["ring"])
        # where the chroma is spent: secondary TEXT is a tinted neutral (light muted used to be
        # #20701d, a span of 83 - full accent), the ring genuinely reads as the accent
        span = lambda hx: max(W.rgb(hx)) - min(W.rgb(hx))
        assert span(p["muted"]) <= 0.30 * span(p["primary"]), (acc, dark, "muted", p["muted"])
        assert span(p["ring"]) >= 0.60 * span(p["primary"]), (acc, dark, "ring", p["ring"])
        # the surface ladder is visible: base under layer in both themes, a raised control above
        # it; in dark every step (base -> layer -> card -> ctl) is its own value, >= 1.09 apart
        assert p["base"] != p["layer"] and p["ctl"] != p["layer"]
        if dark:
            assert p["layer"] != p["card"] != p["ctl"]
            for lo, hi in (("base", "layer"), ("layer", "card"), ("card", "ctl"), ("base", "ctl")):
                assert W.contrast(p[lo], p[hi]) >= 1.09, (lo, hi, p[lo], p[hi])
        # the ground-relative tokens are functions: premixed against the surface they sit on
        assert p["sub"](p["layer"]) != p["sub"](p["base"]) and p["stroke"](p["layer"]) != p["layer"]
        # neutrals are achromatic since 2026-08-28 (a tinted ramp read as pink under a red accent);
        # the light ground is pure white by the user's word
        for n in ("base", "layer", "card", "ctl", "ctl_hover", "ctl_press", "ink", "muted", "stroke_edge",
                  "stroke_field", "stroke_strong", "disabled"):
            assert len(set(W.rgb(p[n]))) == 1, (acc, dark, n, p[n])
        assert dark or p["layer"] == "#ffffff", (acc, p["layer"])
assert W.hex_hue("#808080") == 250.0 and W.hex_hue("#000000") == 250.0   # achromatic -> fallback
assert abs(W.hex_hue("#00ff00") - 142.5) < 1.0
# the window's accent is the brand green whatever the bar colour says (a red bar made it pink)
import brand
assert abs(W.hex_hue(W.palette(brand.GREEN, False)["primary"]) - W.hex_hue(brand.GREEN)) < 1.0
assert W.norm_hex("ABC") == "#aabbcc" and W.norm_hex("#E63C3C") == "#e63c3c"
assert W.norm_hex("nope") is None and W.norm_hex(None, "#123456") == "#123456"
assert W.mix("#000000", "#ffffff", 0.5) == "#808080"
print("tokens ok")

root = tk.Tk()
root.withdraw()

# --- text helpers ---------------------------------------------------------------------------
fnt = W.tkfont.Font(root, family="Segoe UI", size=10)
assert W.ellipsize(fnt, "short enough", 400) == "short enough"
long = "the quick brown fox jumps over the lazy dog " * 8
cut = W.ellipsize(fnt, long, 160)
assert cut.endswith("…") and fnt.measure(cut) <= 160 and long.startswith(cut[:-1])

now = time.mktime((2026, 8, 26, 12, 0, 0, 0, 0, -1))
mk = lambda *a: time.mktime(a + (0, 0, -1))
assert W.when(now - 3600, now) == "Today 11:00"
assert W.when(mk(2026, 8, 25, 9, 10, 0), now) == "Yesterday 09:10"
d3 = mk(2026, 8, 23, 21, 5, 0)
assert W.when(d3, now) == time.strftime("%a", time.localtime(d3)) + " 21:05"
assert W.when(mk(2026, 8, 12, 8, 0, 0), now) == "Aug 12"
assert W.when(mk(2025, 8, 12, 8, 0, 0), now) == "Aug 12 2025"
# the day-group header: the same day arithmetic without the clock, the weekday spelled out
assert W.day_of(now - 3600, now) == "Today" and W.day_of(mk(2026, 8, 25, 9, 10, 0), now) == "Yesterday"
assert W.day_of(d3, now) == time.strftime("%A", time.localtime(d3))
assert W.day_of(mk(2026, 8, 12, 8, 0, 0), now) == "Aug 12" and W.day_of(mk(2025, 8, 12, 8, 0, 0), now) == "Aug 12 2025"
print("text helpers ok")

# --- History.insert -------------------------------------------------------------------------
hp = Path(tempfile.mkdtemp()) / "h.jsonl"
h = W.History(hp, 7)
for t in ("one", "two", "three"):
    h.append(t)
before = [i["text"] for i in h.items]
gone = h.items[1]
h.delete(1)
assert [i["text"] for i in h.items] == ["one", "three"]
h.insert(1, gone)
assert [i["text"] for i in h.items] == before
assert [json.loads(l)["text"] for l in hp.read_text(encoding="utf-8").splitlines()] == before
print("history insert ok")

# --- the window -----------------------------------------------------------------------------
LONG = ("Go ahead and set up the status line so it shows the model and the branch, "
        "then tell me what you think about the way the transcript wraps. ") * 11   # ~1400 chars
SHORT = "Open the config folder for me please"                                     # seven words


def fake_history(n=45):
    p = Path(tempfile.mkdtemp()) / "h.jsonl"
    hs = W.History(p, 7)
    t0 = time.time()
    for i in range(n):
        body = LONG[:1400] if i == 3 else SHORT if i == 4 else \
            f"Dictation number {n - i}, a line of transcript that runs on a while so the row has to cut it."
        hs.items.append({"t": t0 - i * 3 * 3600, "text": body})
    hs.items.reverse()          # oldest first, like the file
    hs.save()
    return hs


def fresh_cfg():
    return {"retention_days": 7, "color": "#00ff00", "color_busy": "#e63c3c", "opacity": 0.9,
            "haze": True, "model": "small.en", "language": None, "mic": None,
            "trigger_vk": 0xB3}   # note: no "theme" key


clip = []
sys.modules["pyperclip"] = types.SimpleNamespace(copy=clip.append)

hist, cfg, saves = fake_history(), fresh_cfg(), []
win = W.AppWindow(root, hist, cfg, saves.append, theme="light",
                  links={"vocab": lambda: None, "folder": lambda: None})
win.show()
root.update()


def upd(pred=lambda: True, n=50):
    """Pump Tk until `pred` holds (a resize lands a few idle passes later)."""
    for _ in range(n):
        root.update()
        if pred():
            return True
        time.sleep(0.02)
    return False


assert len(hist.items) == 45 and win.sel is hist.items[-1]        # newest selected by default
assert "45 dictations" in win.count.cget("text") and "7 days" in win.count.cget("text")
assert len(win.rows) == 45 and win.rows[0][0] is hist.items[-1]   # newest first

# --- the sidebar: one canvas, the lockup on it -----------------------------------------------
side, px_ = win.side, win.px
assert isinstance(side, tk.Canvas) and win.win.grid_slaves(row=0, column=0) == [side]
assert side.winfo_width() == px_(W.W_SIDE) and int(side.cget("takefocus")) == 0   # not a tab stop
im_id, tx_id = side.find_withtag("lock_mark")[0], side.find_withtag("lock_text")[0]
assert side.type(im_id) == "image" and side.itemcget(tx_id, "text") == "murmur"
ib, tb, desc = side.bbox(im_id), side.bbox(tx_id), win.mf["title"].metrics("descent")
assert ib[3] == tb[3] - desc, (ib, tb)                 # the mark's feet on the word's baseline
assert abs((ib[3] - ib[1]) - 40 / 64 * win.px(W.H_MARK)) <= 1   # cropped to ink, not the box
assert (side.coords(tx_id)[0] - side.coords(im_id)[0]
        == win.mark(win.px(W.H_MARK), win.pal["muted"]).width() + win.px(W.GAP_MARK))
assert side.coords(im_id)[0] == px_(16)                # the mark on E, like the footer links
# the grain under everything (the first items), the specular over it, the lockup above both
frost = side.find_withtag("frost")
assert frost and frost == side.find_all()[:len(frost)] and im_id > max(frost)
# a mark taller than the word must not push the nav down - it pokes up into the top pad instead
nav_y = lambda w: w.side.coords(w.nav["history"]["pill"])[1]
was, W.H_MARK = W.H_MARK, 40
tall = W.AppWindow(root, fake_history(2), fresh_cfg(), lambda c: None, theme="light")
tall.show()
root.update()
assert tall.side.rise > 0 and tall.side.bbox(tall.side.find_withtag("lock_mark")[0])[1] < px_(W.PAD_TOP)
assert nav_y(tall) == nav_y(win)
tall.win.destroy()
W.H_MARK = was
# the nav starts at 64 (16 + a 32 px lockup row + 16), 36 px rows 4 apart. A row is ONE image
# per state (pill + icon + the chord's cap boxes composed over the row's own slice of the
# grain, so it is opaque: four alpha blits per row cost 3 ms a switch, an opaque blit nothing)
# plus its label and cap texts: the active row's pill `sub` on base, the grain running on at
# its corners; an idle row's just the grain, the icon and the boxes (still the hit area); the
# bar 10 under the active pill's top; every row's Ctrl-digit chord right-aligned 8 inside the
# pill and never touching the label
assert nav_y(win) == px_(W.PAD_TOP) + px_(W.H_CTL) + px_(16)
assert side.coords(win.nav["promptify"]["pill"])[1] == nav_y(win) + px_(W.H_NAV) + px_(4)
assert side.itemcget(win.nav["history"]["pill"], "image") == str(win._nav_img("history", "sel"))
assert side.itemcget(win.nav["settings"]["pill"], "image") == str(win._nav_img("settings", "idle"))
assert side.itemcget(win.nav["history"]["label"], "fill") == win.pal["ink"] and side.itemcget(win.nav["settings"]["label"], "fill") == win.pal["muted"]
assert side.coords(win.nav_bar)[1] == nav_y(win) + px_(10) and side.coords(win.nav_bar)[0] == px_(8) + px_(4)
pix = lambda img, x, y: tuple(int(v) for v in root.tk.splitlist(root.tk.call(str(img), "get", x, y)))
opaque = lambda img, x, y: int(root.tk.call(str(img), "transparency", "get", x, y)) == 0
wd, wc, w_pill = win.kbd_img("1").width(), win.kbd_img("Ctrl").width(), px_(W.W_SIDE - 16)
sel, idle = win._nav_img("history", "sel"), win._nav_img("history", "idle")
assert sel.width() == idle.width() == w_pill and sel.height() == px_(W.H_NAV)
assert all(opaque(im, x, y) for im in (sel, idle) for x, y in ((0, 0), (px_(60), px_(4)), (w_pill - 1, px_(W.H_NAV) - 1)))
assert pix(sel, px_(60), px_(4)) == W.rgb(win.pal["sub"](win.pal["base"]))          # the pill ...
assert pix(sel, 0, 0) == pix(idle, 0, 0) and abs(pix(idle, 0, 0)[0] - W.rgb(win.pal["base"])[0]) <= 16   # ... its corner the grain
assert pix(idle, px_(60), px_(4)) != W.rgb(win.pal["sub"](win.pal["base"])) or pix(idle, px_(60), px_(5)) != W.rgb(win.pal["sub"](win.pal["base"]))
assert pix(sel, w_pill - px_(8) - wd // 2, px_(W.H_NAV) // 2) == W.rgb(win.pal["ctl"]) == pix(idle, w_pill - px_(8) - wd // 2, px_(W.H_NAV) // 2)   # the digit's cap
assert any(pix(sel, px_(12) + dx, px_(18) + dy) == W.rgb(win.pal["ink"]) for dx in range(px_(16)) for dy in range(-px_(6), px_(7)))   # the icon, in ink
for i, name in enumerate(("history", "promptify", "settings"), 1):
    caps = [side.itemcget(c, "text") for c in win.nav[name]["caps"]]
    assert caps == [str(i), "Ctrl"] and all(side.type(c) == "text" for c in win.nav[name]["caps"]), caps
    x_digit, x_ctrl = (side.coords(c)[0] for c in win.nav[name]["caps"])
    assert x_digit + wd / 2 == px_(8) + w_pill - px_(8) and x_ctrl + wc / 2 + px_(4) == x_digit - wd / 2
    lab_r = side.coords(win.nav[name]["label"])[0] + win.mf["body"].measure(side.itemcget(win.nav[name]["label"], "text"))
    assert x_ctrl - wc / 2 - lab_r >= px_(8), name
# the sidebar's grain: gaussian noise around `base`, subtle (sigma 3 / 2.5: a 100-sample mean
# within 2 of base, nothing past 5 sigma, and it IS noise), one tile repeated; the footer links
# hang from the bottom edge
tile = side.itemcget(frost[0], "image")
b = W.rgb(win.pal["base"])[0]
vals = [int(root.tk.splitlist(root.tk.call(tile, "get", x, y))[0]) for x in range(3, 96, 9) for y in range(3, 96, 9)]
assert abs(sum(vals) / len(vals) - b) <= 2 and max(abs(v - b) for v in vals) <= 15 and len(set(vals)) > 3, (b, vals[:10])
assert len(win.foot_links) == 2 and [side.itemcget(i, "text") for i in win.foot_links] == ["vocab.txt", "config folder"]
assert all(side.bbox(i)[3] == side.winfo_height() - px_(16) for i in win.foot_links)
print("sidebar ok")

# the idle pill is a fully transparent image and STILL the row's hit area (a canvas hit-tests an
# image by its bounding box): a real click 2 px inside the pill's corner, on nothing drawn
side.event_generate("<Button-1>", x=px_(8) + 2, y=win.nav["settings"]["y"] + 2)
root.update()
assert win.view == "settings" and side.find_withtag("current") == (win.nav["settings"]["pill"],)
# hover by geometry: the blend frame now, the full hover 40 ms on, idle at once on leave; the
# pointer crossing from the pill onto the label inside one row repaints nothing
side.event_generate("<Motion>", x=px_(8) + 2, y=win.nav["promptify"]["y"] + 2)
root.update()
pr = win.nav["promptify"]
assert side.itemcget(pr["pill"], "image") == str(win._nav_img("promptify", "blend")) and ("blend", "promptify") in win.jobs
assert side.cget("cursor") == "hand2" and side.itemcget(pr["label"], "fill") == win.pal["ink"]
assert upd(lambda: side.itemcget(pr["pill"], "image") == str(win._nav_img("promptify", "hover")))
assert ("blend", "promptify") not in win.jobs
side.event_generate("<Motion>", x=px_(8) + px_(48), y=win.nav["promptify"]["y"] + px_(18))   # onto the label
root.update()
assert side.itemcget(pr["pill"], "image") == str(win._nav_img("promptify", "hover")) and ("blend", "promptify") not in win.jobs
side.event_generate("<Leave>")
root.update()
assert side.itemcget(pr["pill"], "image") == str(win._nav_img("promptify", "idle")) and side.cget("cursor") == ""
assert side.itemcget(pr["label"], "fill") == win.pal["muted"]
win.motion = False                                       # reduced motion: no blend frame
side.event_generate("<Motion>", x=px_(8) + 2, y=win.nav["promptify"]["y"] + 2)
root.update()
assert side.itemcget(pr["pill"], "image") == str(win._nav_img("promptify", "hover")) and ("blend", "promptify") not in win.jobs
side.event_generate("<Leave>")
win.motion = True
# a click while the blend frame is still due: the row is selected and STAYS selected
side.event_generate("<Motion>", x=px_(8) + 2, y=win.nav["history"]["y"] + 2)
side.event_generate("<Button-1>", x=px_(8) + 2, y=win.nav["history"]["y"] + 2)
assert win.view == "history" and ("blend", "history") not in win.jobs
assert upd(lambda: "slide" not in win.jobs, n=30)
assert side.itemcget(win.nav["history"]["pill"], "image") == str(win._nav_img("history", "sel"))
side.event_generate("<Leave>")
root.update()
assert side.itemcget(win.nav["history"]["pill"], "image") == str(win._nav_img("history", "sel"))
# the bar slides after the switch (3 frames from 40 ms), and the switch itself stays in budget:
# min of nine so a busy CPU cannot fail it, a regression can
win.go("settings")
assert "slide" in win.jobs and side.coords(win.nav_bar)[1] == nav_y(win) + px_(10)   # not yet moved
root.update()
assert upd(lambda: "slide" not in win.jobs, n=30) and side.coords(win.nav_bar)[1] == win.nav["settings"]["y"] + px_(10)
win.motion = False
win.go("history")
assert "slide" not in win.jobs and side.coords(win.nav_bar)[1] == nav_y(win) + px_(10)   # snapped
win.motion = True
ts = []
for v in ("promptify", "settings", "history") * 3:
    t0 = time.perf_counter()
    win.go(v)
    root.update()
    ts.append(time.perf_counter() - t0)
assert min(ts) <= 0.15, ts
upd(lambda: "slide" not in win.jobs, n=30)
# the keyboard reaches every view: Ctrl+1/2/3, Ctrl+, for Settings, Ctrl+W hides (a key event
# goes to the focus window, so the window has to hold it - `tall` took it when it was destroyed)
win.win.focus_force()
root.update()
win.win.event_generate("<Control-Key-2>")
root.update()
assert win.view == "promptify"
win.win.event_generate("<Control-comma>")
root.update()
assert win.view == "settings"
win.win.event_generate("<Control-Key-1>")
root.update()
assert win.view == "history"
win.win.event_generate("<Control-w>")
root.update()
assert not win.win.winfo_viewable()
win.show()
root.update()
upd(lambda: "slide" not in win.jobs, n=30)
print("nav ok")

win.go("settings")
root.update()
assert win.views["settings"].winfo_ismapped() and win.view == "settings"
win.go("history")
root.update()

# selecting, keyboard, copy
win._select(win.rows[3][0])
assert win.detail.get("1.0", "end").strip() == win.rows[3][0]["text"].strip()
win._nav_key(1)                     # reaches the list, focuses it, moves
assert win.sel is win.rows[4][0]
win._list().move(-1)                # once focused, Up/Down are the list's own binding
assert win.sel is win.rows[3][0]
win.b_copy.f.event_generate("<Button-1>")
win.b_copy.f.event_generate("<ButtonRelease-1>")
root.update()
assert clip and clip[-1] == win.rows[3][0]["text"] and win.s_text.cget("text") == "Copied"

# Edit: the card becomes writable, Save persists the fix and drops the entry's draft (it was
# made FROM the old words); Esc abandons; leaving the row abandons
it_e = win.sel
it_e["draft"] = {"prompts": ["stale"], "questions": []}
win._edit_toggle()
assert win.editing and win.b_edit.f.cget("text") == "Save" and win.detail.cget("state") == "normal"
win.detail.delete("1.0", "end")
win.detail.insert("1.0", "my para vault, not power")
win._edit_toggle()
root.update()
assert not win.editing and win.b_edit.f.cget("text") == "Edit"
assert it_e["text"] == "my para vault, not power" and "draft" not in it_e
assert win.s_text.cget("text") == "Saved · draft cleared" and win.detail.cget("state") == "disabled"
saved_e = [l for l in hist.path.read_text(encoding="utf-8").splitlines() if "not power" in l]
assert saved_e, "the edit did not reach the history file"
win._edit_toggle()
win.detail.insert("end", " ABANDONED")
win._select(win.rows[5][0])             # leaving the row abandons the edit
assert not win.editing and "ABANDONED" not in win.rows[3][0]["text"]
win._select(win.rows[3][0])
win._edit_toggle()
win.detail.insert("end", " ABANDONED")
win.detail.focus_force()
root.update()
win.detail.event_generate("<Escape>")
root.update()
assert not win.editing and "ABANDONED" not in win.detail.get("1.0", "end")
it_e["text"] = LONG[:1400]              # the height tests below need the long transcript back
hist.save()
win.refresh()
win._select(it_e)

# the longest transcript is ellipsised in the row but whole in the detail panel
row_text = [win.list.itemcget(i, "text") for i in win.list.find_all()
            if win.list.type(i) == "text"]
assert any(t.endswith("…") for t in row_text), "no row was ellipsised"
win._select(next(it for it in hist.items if len(it["text"]) > 1300))
assert len(win.detail.get("1.0", "end")) > 1300

# one text edge: the card starts 12 before E and its text lands on E, like the rows
E = lambda w: w.winfo_rootx() - win.views["history"].winfo_rootx()
assert E(win.card) == px_(4) and E(win.detail) == px_(16), (E(win.card), E(win.detail))
# the status is meta in the footer's flash slot (`.right`, at the pane's right edge), the
# footer the view's last row at 28 and the view's full width; "Copied" wears the check (it
# goes with the flash), "Editing" does not and swaps the footer's hints for the edit strip
assert (win.status is win.foot_h.right and win.status.master is win.foot_h and win.foot_h.grid_info()["row"] == 4
        and W.tkfont.Font(root, font=win.s_text.cget("font")).actual("size") == 10)
assert win.foot_h.winfo_height() == px_(W.H_FOOT) and win.foot_h.winfo_width() == win.views["history"].winfo_width()
assert win.foot_h.right.winfo_x() + win.foot_h.right.winfo_width() == win.foot_h.winfo_width() - px_(16)
# the footer is one canvas - the hints are items, a Label per cap cost ~0.3 ms per view
# switch (an HWND each): the words that are showing, in order; the caps carry the tag "cap"
hint_words = lambda foot: [foot.itemcget(i, "text") for i in foot.find_all()
                           if foot.type(i) == "text" and "cap" not in foot.gettags(i)
                           and foot.itemcget(i, "state") != "hidden"]
assert hint_words(win.foot_h) == ["move", "filter", "close"]
assert isinstance(win.foot_h, tk.Canvas) and len(win.foot_h.find_withtag("cap")) == 8   # 5 view + 3 edit
assert min(foot.bbox("view")[0] for foot in (win.foot_h, win.foot_p, win.foot_s)) >= px_(16) - 1   # hints from E
win._edit_toggle()
root.update()
assert win.s_text.cget("text") == "Editing" and hint_words(win.foot_h) == ["save", "cancel"]
assert not win.s_text.icon.winfo_ismapped()
win._edit_abort()
win._select(win.sel)
root.update()
assert hint_words(win.foot_h) == ["move", "filter", "close"] and win.s_text.cget("text") == ""
win.copy_selected()
root.update()
assert win.s_text.cget("text") == "Copied" and win.s_text.icon.winfo_ismapped()
assert win.s_text.icon.winfo_x() < win.s_text.winfo_x()                    # the check before the word
# the height rule: 45 rows cannot fit, so the card shows at most 6 lines and its own thumb; a
# one-line dictation gets one line and no thumb; a window under 480 tall allows 3
root.update()
assert int(win.detail.cget("height")) == 6 and win.dsb.winfo_ismapped() and win.list.sb.winfo_ismapped()
assert win.detail.winfo_width() <= win.measure
win._select(next(it for it in hist.items if it["text"] == SHORT))
root.update()
assert int(win.detail.cget("height")) == 1 and not win.dsb.winfo_ismapped()
win.win.geometry(f"{px_(600)}x{px_(400)}")
win._select(next(it for it in hist.items if len(it["text"]) > 1300))
assert upd(lambda: int(win.detail.cget("height")) == 3), win.detail.cget("height")
win.win.geometry(f"{px_(780)}x{px_(560)}")
assert upd(lambda: int(win.detail.cget("height")) == 6)
# ... and when the list fits, the card gets the rest of the pane, past 6 lines
few = W.AppWindow(root, fake_history(4), fresh_cfg(), lambda c: None, theme="light")
few.show()
root.update()
few._select(next(it for it in few.history.items if len(it["text"]) > 1300))
root.update()
assert int(few.detail.cget("height")) > 6 and few.dsb.winfo_ismapped() and not few.list.sb.winfo_ismapped()
few.win.destroy()

# delete + undo puts it back at its index
victim = win.rows[2][0]
idx = hist.items.index(victim)
win._select(victim)
win.delete_selected()
root.update()
assert len(hist.items) == 44 and win.s_undo.winfo_ismapped()
win._undo()
root.update()
assert len(hist.items) == 45 and hist.items[idx] is victim and win.sel is victim

# clear all: inline confirm, Keep restores, Clear empties and shows the empty state
win._clear_ask()
root.update()
assert win.confirm.winfo_ismapped() and "Clear 45 dictations?" == win.c_label.cget("text")
win._clear_cancel()
root.update()
assert not win.confirm.winfo_ismapped() and win.b_clear.f.winfo_ismapped()
# Esc, one step at a time: the confirm goes first (the window stays), then, with nothing left
# to undo, the window hides ("Esc close" in the footer is true); Ctrl+F lands in History
win._clear_ask()
root.update()
win.list.focus_force()
root.update()
win._escape()
root.update()
assert not win.confirm.winfo_ismapped() and win.win.winfo_viewable()
win._escape()
root.update()
assert not win.win.winfo_viewable()
win.show()
root.update()
win.go("settings")
win.win.event_generate("<Control-f>")
root.update()
assert win.view == "history"
upd(lambda: "slide" not in win.jobs, n=30)
print("history view ok")

# --- settings -------------------------------------------------------------------------------
win.go("settings")
root.update()
assert cfg.get("theme") is None                          # missing key = follow the system

# the frame: group labels are meta/muted on E over cards that start 12 before it; the row
# labels land on E; the thumb is 4 px; the rule under the title shows only while scrolled
glab = win.cards[0].master.pack_slaves()[0]
assert glab.cget("text") == "Window" and glab.cget("fg") == win.pal["muted"]
assert W.tkfont.Font(root, font=glab.cget("font")).actual("size") == 10
Es = lambda w: w.winfo_rootx() - win.views["settings"].winfo_rootx()
assert Es(glab) == px_(16) and Es(win.cards[0]) == px_(4) and Es(win.r_mic.head) == px_(16)
# the thumb: 4 px painted (clam's 1 px strips either side are clipped by its column), in the
# pane's right padding; the cards end where it starts, 4 before the pane's edge, thumb or not
assert win.ssb.winfo_reqwidth() == px_(4) + 2 and win.ssb.winfo_ismapped()
Es_r = lambda w: w.winfo_rootx() + w.winfo_width() - win.views["settings"].winfo_rootx()
assert Es_r(win.cards[0]) == win.views["settings"].winfo_width() - px_(4), Es_r(win.cards[0])
assert Es_r(win.ssb.master) == win.views["settings"].winfo_width() and win.ssb.master.winfo_width() == px_(4)
srule = win.views["settings"].grid_slaves(row=1)[0]
assert srule.cget("bg") == win.pal["layer"]
# the footer is the last row; the Saved flash lives in its right slot, with the check
assert win.foot_s.grid_info()["row"] == 3 and win.saved.master is win.foot_s.right
assert hint_words(win.foot_s) == ["next", "toggle", "adjust", "close"]
win.save()
root.update()
assert win.saved.cget("text") == "Saved" and win.saved.icon.winfo_ismapped()
assert upd(lambda: win.saved.cget("text") == "" and not win.saved.icon.winfo_ismapped(), n=120)
# at 600 wide the footer keeps its 28 px and the pane's width, the flash slot its place at the
# pane's right edge (the real hints still fit there); hints that do NOT fit run on under the
# slot and clip at the canvas edge - never a wrap, never a taller footer
win.win.geometry(f"{px_(600)}x{px_(400)}")
edge = lambda w: w.winfo_rootx() + w.winfo_width()
assert upd(lambda: win.foot_s.winfo_width() == win.views["settings"].winfo_width() < px_(600) - px_(W.W_SIDE))
assert win.foot_s.winfo_height() == px_(W.H_FOOT) and win.foot_s.bbox("view")[2] <= win.foot_s.right.winfo_x()
assert edge(win.foot_s.right) == edge(win.r_mic.right), (edge(win.foot_s.right), edge(win.r_mic.right))
wide = win._footer(win.views["settings"], [(("Ctrl", "Space"), "a hint that runs on and on")] * 6)
wide.grid(row=5, column=0, sticky="ew")
assert upd(lambda: wide.winfo_width() == win.foot_s.winfo_width())
assert wide.bbox("view")[2] > wide.winfo_width() and wide.winfo_height() == px_(W.H_FOOT)
assert edge(wide.right) == edge(win.foot_s.right)
wide.destroy()
win.win.geometry(f"{px_(780)}x{px_(560)}")
assert upd(lambda: win.foot_s.winfo_width() == win.views["settings"].winfo_width() > px_(500))
win.sc.yview_moveto(1.0)
root.update()
assert srule.cget("bg") == win.pal["divider"](win.pal["layer"])
win.sc.yview_moveto(0.0)
root.update()
assert srule.cget("bg") == win.pal["layer"]
# field widths: hex and language 96, days 64, combos 200
assert win.ctl["color"]["entry"].master.winfo_width() == px_(96) == win.e_lang.master.winfo_width()
assert win.e_days.master.winfo_width() == px_(64)
assert win.r_mic.right.winfo_children()[0].winfo_width() == px_(200)
# the segment: a `ctl_press` track with no hairline, the picked cell a `ctl` key
seg_img = lambda i: win.ctl["theme"][i][0].cget("image")
i_on = next(i for i, (l, v) in enumerate(win.ctl["theme"]) if v == "system")
i_off = next(i for i, (l, v) in enumerate(win.ctl["theme"]) if v == "dark")
pix = lambda i, x, y: tuple(int(v) for v in root.tk.splitlist(root.tk.call(seg_img(i), "get", x, y)))
h_seg = win.px(W.H_CTL)
assert pix(i_on, win.ctl["theme"][i_on][0].winfo_width() // 2, h_seg // 2) == W.rgb(win.pal["ctl"])
assert pix(i_off, win.ctl["theme"][i_off][0].winfo_width() // 2, h_seg // 2) == W.rgb(win.pal["ctl_press"])
assert pix(i_off, win.ctl["theme"][i_off][0].winfo_width() // 2, 0) == W.rgb(win.pal["ctl_press"])   # no hairline

# one right edge: every control column, the trigger row's button group and the header agree
edge = lambda w: w.winfo_rootx() + w.winfo_width()
for name, w in (("segment", win.ctl["theme"][0][0].master), ("hex", win.ctl["color"]["entry"].master),
                ("haze", win.ctl["haze"]), ("remove", win.b_rm.f), ("saved", win.saved)):
    assert edge(w) == edge(win.r_mic.right), (name, edge(w), edge(win.r_mic.right))

# every view stays gridded (a switch is a raise), so Tab is guarded: from a settings control
# the ring walks settings and the sidebar only, and a Tab pressed inside a COVERED view lands
# in the shown one
win.ctl["haze"].focus_force()
root.update()
seen = []
for _ in range(60):
    win.win.focus_get().event_generate("<Tab>")
    root.update()
    f = str(win.win.focus_get())
    if f in seen:
        break
    seen.append(f)
    assert f.startswith(str(win.views["settings"]) + ".") or f.startswith(str(win.side)), f
assert len(seen) >= 10, seen                             # ... and the whole path is reachable
assert win.views["settings"].winfo_ismapped() and win.views["history"].winfo_ismapped()
win.b_copy.f.focus_force()                               # a History widget, covered right now
root.update()
win.b_copy.f.event_generate("<Tab>")
root.update()
assert win._in_shown(win.win.focus_get()), win.win.focus_get()
n = len(saves)
win.ctl["color"]["dots"][3].event_generate("<Button-1>")  # a preset dot
root.update()
assert cfg["color"] == W.PRESETS[3] == cfg["color"].lower() and len(saves) == n + 1

n, e = len(saves), win.ctl["color"]["entry"]
e.focus_force()
root.update()
e.delete(0, "end")
e.insert(0, "#12FF34")
e.event_generate("<Return>")
root.update()
assert cfg["color"] == "#12ff34" and e.get() == "#12ff34" and len(saves) == n + 1

n = len(saves)
e.delete(0, "end")
e.insert(0, "not a colour")
e.event_generate("<Return>")
root.update()
assert len(saves) == n and cfg["color"] == "#12ff34"
assert "hex colour" in win.r_color.err.cget("text") and win.r_color.err.winfo_ismapped()
e.delete(0, "end")
e.insert(0, "#12ff34")
e.event_generate("<Return>")

n = len(saves)
win.ctl["haze"].event_generate("<Button-1>")
root.update()
assert cfg["haze"] is False and len(saves) == n + 1

n, sl = len(saves), win.ctl["opacity"]
sl.event_generate("<Button-1>", x=10, y=16)
sl.event_generate("<B1-Motion>", x=40, y=16)
assert len(saves) == n                                   # dragging does not write
sl.event_generate("<ButtonRelease-1>", x=40, y=16)
root.update()
assert len(saves) == n + 1 and 0.2 <= cfg["opacity"] < 0.9
s_lab = next(w for w in sl.master.winfo_children() if isinstance(w, tk.Label))
assert s_lab.cget("text") == f"{cfg['opacity'] * 100:.0f} %", s_lab.cget("text")   # a percentage, not 0.85

n = len(saves)
win.e_days.focus_force()
root.update()
win.e_days.delete(0, "end")
win.e_days.insert(0, "3")
win.e_days.event_generate("<Return>")
root.update()
assert cfg["retention_days"] == 3.0 and hist.days == 3.0 and len(saves) == n + 1
assert "3 days" in win.count.cget("text")
n = len(saves)
win.e_days.focus_force()
root.update()
win.e_days.delete(0, "end")
win.e_days.insert(0, "soon")
win.e_days.event_generate("<Return>")
root.update()
assert len(saves) == n and cfg["retention_days"] == 3.0 and win.r_days.err.winfo_ismapped()

n = len(saves)
win.v_model.set("base.en")
win.r_model.restart()
win._set_model()
root.update()
assert cfg["model"] == "base.en" and len(saves) == n + 1 and win.r_model.chip is not None
# "restart to apply" is plain meta text after the label - no pill, no accent
assert (win.r_model.chip.cget("text") == "restart to apply" and win.r_model.chip.cget("fg") == win.pal["muted"]
        and not win.r_model.chip.cget("image") and win.r_model.chip.winfo_ismapped())

win._captured(0xB0)
assert cfg["trigger_vk"] == 0xB0 and win.l_trig.cget("text") == W.MEDIA_KEYS[0xB0]
win.clear_key()
assert cfg["trigger_vk"] is None and not win.b_rm.f.winfo_ismapped()
if win.mics:   # the microphone is saved by name, never by index
    win.v_mic.set(f"{win.mics[0][0]}: {win.mics[0][1]}"); win._set_mic()
    assert cfg["mic"] == win.mics[0][1] and isinstance(cfg["mic"], str)
    win.v_mic.set(W.MIC_DEFAULT); win._set_mic(); assert cfg["mic"] is None

# language: "auto" is a placeholder, never the value; a code commits on FocusOut, blank clears
e = win.e_lang
assert e.get() == "auto" and e.value() == "" and e.cget("fg") == win.pal["muted"]
e.focus_force()
root.update()
assert e.get() == "" and e.cget("fg") == win.pal["ink"]
e.insert(0, "de")
win.e_days.focus_force()
root.update()
assert cfg["language"] == "de" and e.get() == "de"
e.focus_force()
root.update()
e.delete(0, "end")
win.e_days.focus_force()
root.update()
assert cfg["language"] is None and e.get() == "auto" and e.value() == ""

# --- promptify: the two-pane view - states, chips, receive(), update, copy, persistence, keys -
# connect.status / Login / forget_openrouter are faked: the real ones read credential files
# (fine), open browsers and delete a key file (never here).
import json as _json
import time as _time
import promptify as PF
import connect as C
calls, logins = [], []
STATUS = {"claude": ("connected", "Max")}
C.status = lambda key, home=None, appdir=None: STATUS.get(key, ("none", "Not connected"))
C.forget_openrouter = lambda appdir=None: STATUS.pop("openrouter", None)


class FakeLogin:
    """connect.Login's shape, firing its events synchronously: a url on start, done on submit,
    cancelled on cancel. Never a browser, never a credential file."""

    def __init__(self, key, on_event, **kw):
        self.key, self.on_event = key, on_event
        logins.append(self)

    def start(self):
        self.on_event("url", "https://example.invalid/sign-in")
        return self

    def cancel(self):
        self.on_event("cancelled", "")

    def submit(self, code):
        STATUS[self.key] = ("connected", "Max")
        self.on_event("done", "Max")


C.Login = FakeLogin


def fake_draft(cfg_, text, target="code", prompts=None, questions=None, answers=None, cancel=None,
               workdir=None, vault_ctx=None):
    calls.append(dict(text=text, target=target, prompts=prompts, questions=questions, answers=answers,
                      vault_ctx=vault_ctx))
    _time.sleep(0.05)                 # long enough for the ticker to see the worker alive once
    if prompts is None:
        return {"prompts": ["Do the thing.", "Second ask."],
                "questions": [{"q": "How many?", "why": "count", "options": ["3", "5"]},
                              {"q": "Where?", "why": "path", "options": []}],
                "notes": "heard x, wrote y", "engine": "claude", "model": "sonnet", "target": target,
                "wall": 0.1, "t": 0}
    return {"prompts": [p_ + " (updated)" for p_ in prompts], "questions": [], "notes": "",
            "engine": "claude", "model": "sonnet", "target": target, "wall": 0.1, "t": 0}


def slow_draft(cfg_, text, target="code", prompts=None, questions=None, answers=None, cancel=None, workdir=None, vault_ctx=None):
    for _ in range(50):               # a second, unless cancelled - and it records nothing
        _time.sleep(0.02)
        if cancel is not None and cancel.is_set():
            raise PF.Cancelled()
    return fake_draft(cfg_, text, target, prompts, questions, answers)


def settle(pred, n=100):
    for _ in range(n):
        root.update()
        if pred():
            return True
        _time.sleep(0.02)
    return False


def primaries():
    return [b for b in win.primaries if win.on_screen(b.f)]   # covered views are mapped too


PF.draft, PF.available = fake_draft, lambda c: (True, "")
assert len(W.SAMPLE80) == 80 and win.measure == win.mf["body"].measure(W.SAMPLE80)
for dark_ in (False, True):          # the chips' tint holds ink text
    pt = W.palette(brand.GREEN, dark_)
    assert W.contrast(pt["ink"], pt["tint"]) >= 4.5 and W.contrast(pt["ink"], pt["tint_hover"]) >= 4.5

# the view: the same rows, two lines each, at a clamped share of the width; the detail pane
win.go("promptify")
root.update()
assert win.view == "promptify" and win.views["promptify"].winfo_ismapped()
assert win.win.tk.call("winfo", "children", str(win.views["promptify"].master))  # all views live in the cell
assert win.plist.rows and win.plist.rows[0][0] is hist.items[-1] and win.plist.H == win.px(W.H_ROW2)
assert win.pcount.cget("text") == str(len(hist.items)) and len(hist.items) >= 10   # 3-day retention pruned the 45
lp = win.views["promptify"].grid_slaves(row=0, column=0)[0]
assert lp.winfo_width() == max(win.px(176), min(win.px(240), int(0.36 * win.views["promptify"].winfo_width()))), lp.winfo_width()
# one text edge: no Label in this view carries Tk's default border/padding, so its text sits on
# E like the rows' and the buttons' glyphs (title on E, count 12 after it; the body lines, the
# drafting line, the meta row and the sheet's labels below)
Ep = lambda w, pane: w.winfo_rootx() - pane.winfo_rootx()
bare = lambda l: int(l.cget("bd")) == 0 and int(l.cget("padx")) == 0 and int(l.cget("pady")) == 0
title, count = lp.grid_slaves(row=0)[0].pack_slaves()[:2]
assert Ep(title, lp) == px_(16) and bare(title) and bare(count)
assert Ep(count, lp) == px_(16) + win.mf["title"].measure("Dictations") + px_(12)
# the newest dictation has no draft: the empty state - one primary, the engine control, the segment
win._select(hist.items[-1])
root.update()
assert win.sel is hist.items[-1] and win.pstate == "empty"
assert win.b_main.f.cget("text") == "Promptify" and len(primaries()) == 1
assert win.b_eng.f.cget("text") == "Claude Code" and win.seg_host.winfo_ismapped() and win.head_rows == 2
# pick: nothing selected, no primary at all
win._select(None)
root.update()
assert win.pstate == "pick" and primaries() == [] and not win.b_main.f.winfo_ismapped()
lab = win.p_inner.winfo_children()[0]
assert lab.cget("text") == "Pick a dictation on the left." and Ep(lab, win.dpane) == px_(16) and bare(lab)
# no engine: hollow dot, "No engine", the segment gone, Connect an engine
STATUS.pop("claude")
win._eng_cache.clear()                                # the fake changed under the cache
win._select(hist.items[-1])
root.update()
assert win.pstate == "noengine" and win.b_main.f.cget("text") == "Connect an engine" and len(primaries()) == 1
assert win.b_eng.f.cget("text") == "No engine" and not win.seg_host.winfo_ismapped()
STATUS["claude"] = ("connected", "Max")
win._eng_cache.clear()                                # the fake changed under the cache
# first use asks once; "Not now" backs out, "Promptify" remembers and runs
cfg["prompt_ack"] = False
win._select(hist.items[-1])
root.update()
assert win.pstate == "empty" and win.seg_host.winfo_ismapped()
win.promptify()
root.update()
assert win.pstate == "ack" and not calls and win.b_main.f.cget("text") == "Promptify" and len(primaries()) == 1
win.b_ack_no.cmd()
root.update()
assert win.pstate == "empty"
win.promptify()
root.update()
win._ack_go()
root.update()
assert cfg["prompt_ack"] is True and saves[-1]["prompt_ack"] is True
assert win.pstate == "drafting" and not win.b_main.on and win.b_cancel.f.winfo_ismapped()
assert Ep(win.l_draft, win.dpane) == px_(16) and bare(win.l_draft)
assert settle(lambda: win.pstate == "draft"), win.pstate
assert calls[-1]["text"] == win.sel["text"] and calls[-1]["prompts"] is None and calls[-1]["target"] == "code"
F = win.p_fields
assert F["text"].get("1.0", "end").strip() == "Do the thing." and len(F["answers"]) == 2
assert win.sel["draft"]["prompts"] == ["Do the thing.", "Second ask."]
assert win.b_main.f.cget("text") == "Copy prompt" and win.b_main.on and len(primaries()) == 1
meta = [w.cget("text") for w in win.p_inner.winfo_children()[-2].winfo_children()]
assert meta == ["Drafted by Claude Code · ", "sonnet", " · 0 s"], meta
root.update()
mlabs = win.p_inner.winfo_children()[-2].winfo_children()
assert Ep(mlabs[0], win.dpane) == px_(16) and all(bare(l) for l in mlabs)
assert mlabs[1].winfo_x() == mlabs[0].winfo_x() + mlabs[0].winfo_width()   # no holes around the model id
q_l = F["chips"][0].master.winfo_children()[0]
assert q_l.cget("text") == "How many?" and Ep(q_l, win.dpane) == px_(16) and bare(q_l)
# the body's thumb lives in the pane's right padding: the prompt ends where the primary does
assert edge(F["text"]) == edge(win.b_main.f) == edge(win.dpane) - px_(16), (edge(F["text"]), edge(win.b_main.f), edge(win.dpane))
assert win.psb.winfo_reqwidth() == px_(4) + 2 and edge(win.psb.master) == edge(win.dpane)
# the drafted row got its dot (the two highlights + one dot image)
assert sum(win.plist.type(i) == "image" for i in win.plist.find_all()) == 3
# the header: two rows at 780 (measured, not hard-coded), one row when the pane is wide enough
assert win.head_rows == 2 and win.seg_host.grid_info()["row"] == 1
win.win.geometry(f"{win.px(1000)}x{win.px(640)}")
assert settle(lambda: win.head_rows == 1)
assert win.seg_host.grid_info()["row"] == 0
win.win.geometry(f"{win.px(780)}x{win.px(560)}")
assert settle(lambda: win.head_rows == 2)
# ... and re-decided the moment the engine name changes, with no resize to prompt it: a window
# that holds Codex's header on one row but not Claude Code's (the parts are measured, not the
# frame's idle-time aggregate)
need = lambda: (win.eng_dot.winfo_reqwidth() + win.b_eng.w + 2 * px_(12) + win.seg.winfo_reqwidth() + win.b_main.w)
cfg["prompt_engine"], STATUS["codex"] = "codex", ("connected", "ChatGPT")
win._eng_cache.clear()                                # the fake changed under the cache
win._refresh_engine()
root.update()
assert win.b_eng.f.cget("text") == "Codex"
n_codex = need()
for W_ in range(px_(700), px_(1200), 2):
    main_ = W_ - px_(W.W_SIDE) - 1
    detail_ = main_ - max(px_(176), min(px_(240), int(0.36 * main_))) - 1
    if detail_ - 2 * px_(16) >= n_codex + px_(10):
        break
win.win.geometry(f"{W_}x{px_(640)}")
assert settle(lambda: win.head_rows == 1 and win.dpane.winfo_width() == detail_), (win.head_rows, win.dpane.winfo_width(), detail_)
win._use("claude")
root.update()
assert win.b_eng.f.cget("text") == "Claude Code" and need() > win.dpane.winfo_width() - 2 * px_(16)
assert win.head_rows == 2 and win.seg_host.grid_info()["row"] == 1
assert edge(win.b_main.f) == edge(win.dpane) - px_(16), (edge(win.b_main.f), edge(win.dpane))
STATUS.pop("codex")
win._eng_cache.clear()                                # the fake changed under the cache
win.win.geometry(f"{win.px(780)}x{win.px(560)}")
assert settle(lambda: win.head_rows == 2)
# chips: a chip fills its answer and is the chosen one; typing something else un-chooses it;
# the strip is ONE tab stop with a cursor
strip, a0 = F["chips"][0], F["answers"][0]
assert F["chips"][1] is None and [c.label for c in strip.chips] == ["3", "5"]
assert a0.value() == "" and a0.get("1.0", "end-1c") == "Type an answer"
strip.chips[1].cmd()
root.update()
assert a0.value() == "5" and strip.chips[1].is_chosen and not strip.chips[0].is_chosen
assert strip.chips[1].w == strip.chips[1].pad * 2 + win.mf["body"].measure("5") + win.px(12)
a0.set("6")
assert not strip.chips[1].is_chosen and a0.value() == "6"
strip.focus_force()
root.update()
strip.event_generate("<Right>")
strip.event_generate("<space>")
root.update()
assert strip.kb[0] == 1 and a0.value() == "5" and strip.chips[1].is_chosen and win.win.focus_get() is a0
strip.focus_force()
root.update()
win._escape()
root.update()
assert win.win.focus_get() is win.plist            # Esc from the strip (a Frame, not a field) -> the list
# a take that lands while an answer field has focus goes in at the caret; the placeholder is gone
a1 = F["answers"][1]
a1.focus_force()
root.update()
assert a1.get("1.0", "end-1c") == "" and a1.value() == ""
assert win.receive("in the repo") and win.receive("under src") and a1.value() == "in the repo under src"
win.plist.focus_force()
root.update()
assert not win.receive("nowhere") and a1.get("1.0", "end-1c") == "in the repo under src"
# Assume: the button swaps the answer row for a covering bar, pass 2 carries the sentinel,
# clicking the bar (or a chip picked later) hands the question back
btn0, bar0, set0 = F["assume_ui"][0]
assert F["assume"][0] == [False] and btn0.cmd is not None
btn0.cmd()
root.update()
assert F["assume"][0] == [True] and bar0.winfo_ismapped() and not F["answers"][0].host.master.winfo_ismapped()
calls.clear()
win._update()
assert settle(lambda: win.pstate == "draft" and calls)
assert calls[0]["answers"][0] == PF.ASSUME          # the sentinel, not the field text
win._run_draft(win.p_item)                          # fresh pass 1: the questions come back
assert settle(lambda: win.pstate == "draft" and win.p_fields.get("assume_ui"))
F = win.p_fields
btn0, bar0, set0 = F["assume_ui"][0]
set0(True)
root.update()
bar0.event_generate("<Button-1>")
root.update()
assert F["assume"][0] == [False] and not bar0.winfo_ismapped()
strip0 = F["chips"][0]
set0(True)
root.update()
strip0.chips[0].cmd()                               # a chip while assumed hands it back filled
root.update()
assert F["assume"][0] == [False] and F["answers"][0].value() == strip0.chips[0].label
F["answers"][0].set("5")                            # restore the state the tests below built
F["answers"][1].set("in the repo under src")
# the second prompt is reachable; a take into the prompt; edits ride along into pass 2
win._switch_prompt(1)
assert F["text"].get("1.0", "end").strip() == "Second ask."
win._switch_prompt(0)
# the prompt's keyboard focus is a 2 px ring bar on its left edge, in the padding before E -
# ground until then; nothing moves and the text stays on E
t_ = F["text"]
assert t_.ring.cget("bg") == win.pal["layer"] and Ep(t_, win.dpane) == px_(16) and t_.ring.winfo_width() == px_(2)
xy0 = (t_.winfo_rootx(), t_.winfo_rooty(), t_.winfo_width())
F["text"].focus_force()
F["text"].mark_set("insert", "end-1c")
root.update()
assert t_.ring.cget("bg") == win.pal["ring"] and (t_.winfo_rootx(), t_.winfo_rooty(), t_.winfo_width()) == xy0
assert win.receive("Edited.") and F["text"].get("1.0", "end").strip() == "Do the thing. Edited."
win._update()
root.update()
assert win.pstate == "drafting" and not win.b_main.on and win.b_main.f.cget("text") == "Copy prompt"
assert settle(lambda: win.pstate == "draft" and calls[-1]["prompts"] is not None)
assert calls[-1]["prompts"] == ["Do the thing. Edited.", "Second ask."] and calls[-1]["answers"] == ["5", "in the repo under src"]
assert calls[-1]["questions"][0]["q"] == "How many?"
assert win.sel["draft"]["prompts"][0] == "Do the thing. Edited. (updated)" and not win.p_fields["answers"]
labels = [w.cget("text") for w in win.p_inner.winfo_children() if isinstance(w, tk.Label)]
assert "No questions · the dictation was specific enough." in labels
# Copy prompt: the clipboard, and the button says Copied at its held width
w0 = win.b_main.w
win._copy_prompt()
assert clip[-1] == "Do the thing. Edited. (updated)" and win.b_main.f.cget("text") == "Copied" and win.b_main.w == w0
saved = next(l for l in (_json.loads(x) for x in hist.path.read_text(encoding="utf-8").splitlines())
             if l["text"] == win.sel["text"])
assert saved["draft"]["prompts"][0].endswith("(updated)")          # the draft lives on the entry, on disk
# Original: closed per draft, the whole dictation when open
assert not win.l_orig.winfo_ismapped() and win.b_orig.f.cget("text") == "Original ›"
win._toggle_orig()
root.update()
assert win.l_orig.winfo_ismapped() and win.b_orig.f.cget("text") == "Original ▾" and win.l_orig.cget("text") == win.sel["text"]
# Esc: a field hands focus to the list; the list's Return copies a drafted row's prompt
win.p_fields["text"].focus_force()
root.update()
win._escape()
root.update()
assert win.win.focus_get() is win.plist
win.b_main.f.focus_force()                          # ... and from the header's primary
root.update()
win._escape()
root.update()
assert win.win.focus_get() is win.plist
n_clip = len(clip)
win._plist_go()
assert len(clip) == n_clip + 1
# leaving and coming back re-shows the kept draft without another call
n_calls = len(calls)
win.go("settings")
win.go("promptify")
root.update()
assert len(calls) == n_calls and win.pstate == "draft" and win.p_fields["text"].get("1.0", "end").strip().endswith("(updated)")
# Ctrl+D in History: the Promptify view with the row selected, drafting at once when it has none
win.go("history")
win._select(hist.items[-2])
n_calls = len(calls)
win._to_promptify()
assert win.view == "promptify" and win.sel is hist.items[-2]
assert settle(lambda: win.pstate == "draft" and len(calls) == n_calls + 1)
win.go("history")
win._select(hist.items[-1])
n_calls = len(calls)
win._to_promptify()
root.update()
assert win.pstate == "draft" and len(calls) == n_calls     # with a draft it only shows it
# drafting: the primary stays, faded; the counter line; Cancel / Esc go back; nothing else moves
PF.draft = slow_draft
it = hist.items[-3]
win._select(it)
root.update()
assert win.pstate == "empty"
win.promptify()
root.update()
assert win.pstate == "drafting" and not win.b_main.on and win.b_main.f.cget("text") == "Promptify"
assert win.l_draft.cget("text").startswith("Drafting with Claude Code · ") and win.b_cancel.f.winfo_ismapped()
win._escape()
root.update()
assert win.pstate == "empty" and win.b_main.on and win.drafting is None and "tick" not in win.jobs
assert settle(lambda: not win.draft_thread.is_alive())      # the worker saw the flag and left
# an engine that cannot run: the no-engine state, no call
PF.available = lambda c: (False, "Claude Code is not connected")
n_calls = len(calls)
win.promptify()
assert win.pstate == "noengine" and len(calls) == n_calls
PF.available = lambda c: (True, "")


def failing(*a, **k):
    raise PF.PromptifyError("Codex hit its usage limit (resets 14:00)")


# a failing engine: the error state - danger on the dot only - and Try again re-runs the same call
PF.draft = failing
win.promptify()
assert settle(lambda: win.pstate == "error"), win.pstate
assert win.b_main.f.cget("text") == "Try again" and win.eng_err["claude"].startswith("Codex hit")
assert win.b_eng.f.cget("text") == "Claude Code" and win.eng_dot.cget("image") == str(win.dot(8, 6, win.pal["danger"]))
labels = [w.cget("text") for w in win.p_inner.winfo_children() if isinstance(w, tk.Label)]
assert labels[0] == "Couldn’t draft — Codex hit its usage limit (resets 14:00)" and labels[1].startswith("Pick another engine")
PF.draft = fake_draft
win._retry()
assert win.pstate == "drafting" and not win.b_main.on and win.b_main.f.cget("text") == "Try again"   # stays, faded
assert "claude" not in win.eng_err                     # the marker goes with the retry, not its result
assert settle(lambda: win.pstate == "draft") and "claude" not in win.eng_err
assert win.eng_dot.cget("image") == str(win.dot(8, 6, win.pal["ring"]))
# the tab order: list → engine → segment → primary → Original › → 1 of 2 → prompt → chip strip →
# answer → Assume → answer → Assume → Update prompt → (round to the list): nothing hidden, nothing twice
F = win.p_fields
want = [win.plist, win.b_eng.f, win.seg, win.b_main.f, win.b_orig.f, win.ctl["which"][0][0].master,
        F["text"], F["chips"][0], F["answers"][0], F["assume_ui"][0][0].f, F["answers"][1],
        F["assume_ui"][1][0].f, win.b_update.f, win.plist]
win.plist.focus_force()                              # the guarded ring (Tab), not Tk's raw one:
root.update()                                        # covered views are mapped and Tk would walk them
seen = []
for _ in range(len(want) - 1):
    assert win._tab(False) == "break"
    seen.append(win.win.focus_get())
assert seen == want[1:], [str(s) for s in seen]
# Return on an undrafted row drafts it
win._select(hist.items[-4])
root.update()
assert win.pstate == "empty"
win._plist_go()
assert win.pstate == "drafting" and settle(lambda: win.pstate == "draft")

# the button register: kinds, disabled, the held width, the solid button's inner ring
fr = tk.Frame(win.views["promptify"], bg=win.pal["layer"])
fr.place(x=0, y=0)
hits = []
b = W._Btn(win, fr, "Copy prompt", lambda: hits.append(1), kind="primary")
b.f.pack()
b.f.focus_force()                                           # a generated key needs the focus
root.update()
b._ring(False)
img0 = b.f.cget("image")
win.kbd = False                                             # focus-visible: a mouse focus rings nothing
b._ring(True)
assert b.f.cget("image") == img0
win.kbd = True
b._ring(True)
assert b.f.cget("image") != img0                            # the inner ring, in on_primary
b._ring(False)
b.enable(False)
b.f.event_generate("<Return>")
assert not hits and int(b.f.cget("takefocus")) == 0
b.enable(True)
b.f.event_generate("<Return>")
assert hits == [1]
w0 = b.w
b.text("Copied")
assert b.w == w0
b.text("Try again", hold=False)
assert b.w < w0
for kind in ("secondary", "text", "chip", "danger"):
    W._Btn(win, fr, "x", lambda: None, kind=kind)
c = W._Chip(win, fr, "an option", lambda: None)
w1 = c.w
c.chosen(True)
assert c.w == w1 + win.px(12) and c.f.itemcget(c.i_dot, "state") == "normal"
c.chosen(False)
assert c.w == w1 and c.f.itemcget(c.i_dot, "state") == "hidden"
win.primaries.remove(b)
fr.destroy()
print("promptify view ok")

# --- the engines sheet: statuses, a fake Login, Use, the model field, Disconnect -------------
win._open_sheet()
root.update()
assert win.sheet_open and win.sheet.winfo_ismapped() and not win.draft_f.winfo_ismapped()
R = win.e_rows
assert [R[k]["action"] for k in PF.ORDER] == ["active", "connect", "connect", "connect"]
assert R["claude"]["status"].cget("text") == "Connected · " + PF.ENGINES["claude"]["login"]
assert R["codex"]["status"].cget("text").startswith("Not connected · ") and R["claude"]["btn"] is None
assert primaries() == []                                   # the sheet has no primary
# its text edge: ‹ Draft's glyph on E, "Engines" 12 after it, the intro and the names on E, the
# status 16 after E (the 8 px dot + 8), the action ending 16 before the pane's edge, thumb or not
b_back, tl = win.sheet.grid_slaves(row=0)[0].pack_slaves()[:2]
assert Ep(b_back, win.dpane) + win.b_back.pad == px_(16) and bare(tl)
assert Ep(tl, win.dpane) == Ep(b_back, win.dpane) + b_back.winfo_width() - win.b_back.pad + px_(12)
intro = win.e_inner.winfo_children()[0]
name_l = R["claude"]["status"].master.master.winfo_children()[0]
assert name_l.cget("text") == "Claude Code" and Ep(intro, win.dpane) == px_(16) == Ep(name_l, win.dpane)
assert bare(intro) and bare(name_l) and bare(R["claude"]["status"])
assert Ep(R["claude"]["status"], win.dpane) == px_(32)
assert edge(R["codex"]["btn"].f) == edge(win.dpane) - px_(16), (edge(R["codex"]["btn"].f), edge(win.dpane))
assert win.esb.winfo_reqwidth() == px_(4) + 2
# the model field writes cfg["prompt_models"] on commit; blank shows "default" and means it
m = R["claude"]["model"]
assert m is win.e_model and m.value() == "" and m.get("1.0", "end-1c") == "default"
m.focus_force()
root.update()
assert m.get("1.0", "end-1c") == ""
m.set("opus")
n = len(saves)
win.b_back.f.focus_force()                                  # FocusOut commits
root.update()
assert cfg["prompt_models"]["claude"] == "opus" and len(saves) == n + 1 and PF.engine_spec(cfg)["model"] == "opus"
# Connect: the row goes to connecting with Cancel (and no paste-code field: that is claude's)
R["codex"]["btn"].cmd()
root.update()
assert logins[-1].key == "codex" and "codex" in win.logins
R = win.e_rows
assert R["codex"]["action"] == "cancel" and R["codex"]["status"].cget("text").startswith("Connecting · ") and "code" not in R["codex"]
R["codex"]["btn"].cmd()                                     # Cancel
root.update()
assert "codex" not in win.logins and win.e_rows["codex"]["action"] == "connect"
# claude, login expired: Connect, the url arrives, the paste-code fallback, done: connected, still active
STATUS["claude"] = ("expired", "Login expired")
win._eng_cache.clear()                                # the fake changed under the cache
win._engine_rows()
R = win.e_rows
assert R["claude"]["action"] == "connect" and R["claude"]["status"].cget("text").startswith("Login expired · ")
R["claude"]["btn"].cmd()
root.update()
R = win.e_rows
assert R["claude"]["action"] == "cancel" and "code" in R["claude"] and "model" not in R["claude"]
R["claude"]["code"].focus_force()
root.update()
R["claude"]["code"].set("the-code")
R["claude"]["code"].event_generate("<Return>")             # submits
root.update()
win._login_tick()
assert STATUS["claude"][0] == "connected" and "claude" not in win.logins and win.e_rows["claude"]["action"] == "active"
assert cfg.get("prompt_engine", "claude") == "claude"       # it was the engine: nothing to write
# the split toggle lives in the sheet, default off; flipping it writes prompt_split
root.update()                       # drain pending rebuilds so the fetched toggle is the live one
sw = win.ctl["prompt_split"]
assert not cfg.get("prompt_split")
sw.event_generate("<Button-1>")
root.update()
assert cfg["prompt_split"] is True and saves
sw.event_generate("<Button-1>")
root.update()
assert cfg["prompt_split"] is False


class FailingLogin(FakeLogin):
    def start(self):
        self.on_event("error", "No browser could be opened.")
        return self


# a sign-in that fails: the reason in the row, Connect again
C.Login = FailingLogin
win.e_rows["gemini"]["btn"].cmd()
root.update()
assert win.e_rows["gemini"]["action"] == "connect" and win.e_rows["gemini"]["status"].cget("text") == "Error · No browser could be opened."
C.Login = FakeLogin
# a failure of a signed-in engine (a timeout, a limit) is said on its row, danger dot, but the
# engine stays usable: Active/Use and the Model line, never Connect (a sign-in is not the remedy)
win.eng_err["claude"] = "Timed out after 150 s."
win._engine_rows()
R = win.e_rows
assert R["claude"]["action"] == "active" and "model" in R["claude"] and R["claude"]["btn"] is None
assert R["claude"]["status"].cget("text") == "Error · Timed out after 150 s. · " + PF.ENGINES["claude"]["login"]
win.eng_err["claude"] = "Claude Code: usage limit reached."
win._engine_rows()
assert win.e_rows["claude"]["status"].cget("text").startswith("Usage limit · Claude Code: usage limit")
win.eng_err.clear()
win._engine_rows()
# Use: the engine becomes active, the header follows; Disconnect (OpenRouter only) forgets its key
STATUS["openrouter"] = ("connected", "OpenRouter")
win._eng_cache.clear()                                # the fake changed under the cache
win._engine_rows()
R = win.e_rows
assert R["openrouter"]["action"] == "use" and "disconnect" in R["openrouter"] and "disconnect" not in R["claude"]
R["openrouter"]["btn"].cmd()
root.update()
assert cfg["prompt_engine"] == "openrouter" and saves[-1]["prompt_engine"] == "openrouter"
assert win.e_rows["openrouter"]["action"] == "active" and win.e_rows["claude"]["action"] == "use"
assert win.b_eng.f.cget("text") == "OpenRouter"
win.e_rows["openrouter"]["disconnect"].cmd()
root.update()
assert "openrouter" not in STATUS and win.e_rows["openrouter"]["action"] == "connect" and win.b_eng.f.cget("text") == "No engine"
# ... and with no engine connected, the first one to sign in becomes the active one
win.e_rows["codex"]["btn"].cmd()
root.update()
logins[-1].submit("x")
root.update()
win._login_tick()
assert cfg["prompt_engine"] == "codex" and win.e_rows["codex"]["action"] == "active" and win.b_eng.f.cget("text") == "Codex"
# the sheet's tab order: ‹ Draft → each row's button → the Model field; Esc closes it
root.update()                                               # the rebuilt rows get mapped
R = win.e_rows
want = [win.b_back.f, R["claude"]["btn"].f, R["codex"]["model"], R["gemini"]["btn"].f, R["openrouter"]["btn"].f]
stop, seen = win.b_back.f, []
for _ in range(len(want) - 1):
    stop = stop.tk_focusNext()
    seen.append(stop)
assert seen == want[1:], [str(s) for s in seen]
win._escape()
root.update()
assert not win.sheet_open and win.draft_f.winfo_ismapped() and win.pstate == "draft"
cfg["prompt_engine"] = "claude"
win._show(win._state_for(win.sel))
assert win.b_eng.f.cget("text") == "Claude Code"
# the Obsidian vault block: off by default with Use/Choose, on with Refresh/Off; picking a
# vault clears its acknowledgement so the next Promptify discloses what now travels
import vault as VLT
_kv, _ris = VLT.known_vaults, VLT.refresh_if_stale
VLT.known_vaults = lambda: [r"C:\fake\para"]
VLT.refresh_if_stale = lambda c, a: None
assert not (cfg.get("vault_path") or "")
win._engine_rows()
root.update()
win._pick_vault(r"C:\fake\para")
root.update()
assert cfg["vault_path"] == r"C:\fake\para" and cfg["vault_ack"] is False and saves[-1]["vault_path"]
win._pick_vault(None)
root.update()
assert cfg["vault_path"] is None
# with a vault on and unacknowledged, the ack state shows once even though prompt_ack is set
cfg.update(vault_path=r"C:\fake\para", vault_ack=False, prompt_ack=True)
PF.draft, PF.available = fake_draft, lambda c: (True, "")
win.sel.pop("draft", None)
win.p_err = None
win.promptify()
root.update()
assert win.pstate == "ack"
win._ack_go()
for _ in range(100):
    root.update()
    if win.pstate == "draft":
        break
    _time.sleep(0.02)
assert cfg["vault_ack"] is True and win.pstate == "draft"
# the Context line renders from vault_notes, and pass 2 reuses pass 1's vault block verbatim
d_ = win.sel["draft"]
d_["vault_notes"] = [{"path": "1-Projects/x/README.md", "title": "x"}]
d_["vault_ctx"] = "<vault_context>ctx</vault_context>"
win._show("draft")
root.update()
labels = [w2 for w in win.p_inner.winfo_children() for w2 in ([w] + list(w.winfo_children()))]
assert any(isinstance(w, tk.Label) and w.cget("text") == "Context ·" for w in labels)
win.p_fields["answers"][0].set("five")
win._update()
for _ in range(100):
    root.update()
    if calls[-1]["prompts"] is not None:
        break
    _time.sleep(0.02)
assert calls[-1]["vault_ctx"] == "<vault_context>ctx</vault_context>"
cfg.update(vault_path=None)
VLT.known_vaults, VLT.refresh_if_stale = _kv, _ris
print("vault ui ok")
print("engines sheet ok")
print("settings ok")

# clear-all really clears, and the empty state draws
win.go("history")
win._clear_ask()
win._clear_do()
root.update()
assert hist.items == [] and not win.act.winfo_ismapped() and not win.b_clear.f.winfo_ismapped()
assert any("No dictations yet" == win.list.itemcget(i, "text") for i in win.list.find_all())
assert win.count.cget("text").startswith("no dictations · "), win.count.cget("text")
# ... and in the Promptify pane the empty state is two lines, wrapped inside the pane (never
# clipped at its hairline), and the detail pane says to dictate first
win.go("promptify")
root.update()
texts = [i for i in win.plist.find_all() if win.plist.type(i) == "text"]
assert [win.plist.itemcget(i, "text") for i in texts] == ["No dictations yet", "Hold ", "Ctrl+Win", " and talk."]
assert all(win.plist.bbox(i)[2] <= win.plist.winfo_width() - px_(16) for i in texts), [win.plist.bbox(i) for i in texts]
assert win.pstate == "pick" and win.p_inner.winfo_children()[0].cget("text") == "Dictate something first."
win.go("history")
root.update()

# the wheel handler is installed on the root's "all" tag - a withdrawn window gets no <Leave>,
# so hide() has to take it down itself or it scrolls a canvas nobody can see
win.list.event_generate("<Enter>")
root.update()
assert root.bind_all("<MouseWheel>") != ""
# Appearance: Dark rebuilds the window in place - same geometry, view and selection - and
# the cfg choice beats the constructor's theme override; System hands it back
win.go("settings")
root.update()
assert settle(lambda: win.drafting is None)          # a rebuild waits for a running draft to land
geo, sel_ = win.win.geometry(), win.sel
next(l for l, v in win.ctl["theme"] if v == "dark").event_generate("<Button-1>")
root.update()
assert cfg["theme"] == "dark" and win.dark and W.lum(win.pal["layer"]) < 0.1 and win.win.winfo_exists()
assert win.view == "settings" and win.sel is sel_ and win.win.geometry().split("+")[0] == geo.split("+")[0]
next(l for l, v in win.ctl["theme"] if v == "system").event_generate("<Button-1>")
root.update()
assert cfg["theme"] == "system" and not win.dark               # the override says light
win.hide()
root.update()
assert root.bind_all("<MouseWheel>") == "", "wheel binding leaked past hide()"
win.win.destroy()

# --- other builds ---------------------------------------------------------------------------
h2, c2, s2 = fake_history(), fresh_cfg(), []
dark = W.AppWindow(root, h2, c2, s2.append, theme="dark")
dark.show()
root.update()
assert dark.pal["ink"] != win.pal["ink"] and W.lum(dark.pal["layer"]) < 0.1
assert dark.foot_links == [], "footer links must be absent without `links`"
dark.win.destroy()

big = W.AppWindow(root, fake_history(4), fresh_cfg(), lambda c: None, scale=1.5, theme="light")
big.show()
root.update()
assert big.win.winfo_reqwidth() > 0 and big.px(24) == 36
big.win.destroy()

off = fresh_cfg()
off["retention_days"] = 0
zero = W.AppWindow(root, fake_history(0), off, lambda c: None, theme="light")
zero.show()
root.update()
assert "history is off" in zero.count.cget("text")
assert any("History is off" == zero.list.itemcget(i, "text") for i in zero.list.find_all())
zero.win.destroy()
root.destroy()
print("builds ok")
print("window ok")
