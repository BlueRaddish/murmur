"""Run: python tests/test_ui_rules.py - the design rules window.py has to keep, as asserts.
No framework. Needs a display (Windows has one); no engine, no login, no clipboard."""
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tkinter as tk

import brand
import connect as C
import promptify as PF
import window as W

SPACING = {2, 4, 8, 12, 16, 24}
# ordinary English (the app's own copy) sets what an "average character" is wide: the body
# measure has to be 78..82 of those, or the 80-character sample that sets it is not average
PARA = ("What you say is typed where your cursor is, and kept here for seven days. "
        "The engine writes the prompt; each signs in on its own and murmur keeps no keys. "
        "Connect opens the engine's own sign-in in your browser, and a dictation is sent only "
        "to the engine you pick, only when you press Promptify.")

# --- 1. the two contrasts every screen leans on, both palettes -----------------------------
for dark in (False, True):
    p = W.palette(brand.GREEN, dark)
    assert W.contrast(p["ring"], p["layer"]) >= 3.0, (dark, "ring on the layer", p["ring"], p["layer"])
    assert W.contrast(p["ring"], p["card"]) >= 3.0, (dark, "ring on a card", p["ring"], p["card"])
    assert W.contrast(p["on_primary"], p["primary"]) >= 4.5, (dark, "text on the primary")

# --- 2. every SP-derived spacing the file uses is one of the six ---------------------------
src = Path(W.__file__).read_text(encoding="utf-8")
used = sorted({int(i) for i in re.findall(r"\bSP\[(\d)\]", src)})
assert used, "no SP-derived spacing found"
for i in used:
    assert W.SP[i] in SPACING, (f"SP[{i}]", W.SP[i], "is not in", sorted(SPACING))

# --- §4. every timed callback is a NAMED job: `_rebuild` cancels `self.jobs`, so an after()
# whose id is not stored there would outlive the window it was scheduled for. Three documented
# exceptions: the idle fits of `_uline` / `_textbox` guard themselves (`winfo_exists`), and the
# hotkey thread's marshal into Tk (`root.after(0, ...)`) belongs to no window
FREE = {"self.win.after_idle(fit)",
        "app.capture = lambda vk: self.root.after(0, lambda: self._captured(vk))"}
timed = [(n, l.strip()) for n, l in enumerate(src.splitlines(), 1) if re.search(r"\.after(_idle)?\(", l)]
assert len(timed) >= 15, ("the after() inventory shrank", len(timed))
for n, line in timed:
    assert "jobs[" in line or line in FREE, ("an after() outside self.jobs", n, line)
assert W.MOTION_FAST // 2 in (40, 41, 42) and W.MOTION_NORMAL // 3 == 50   # the frame gaps §4 names

# --- a window, no real engine ---------------------------------------------------------------
sys.modules["pyperclip"] = types.SimpleNamespace(copy=lambda s: None)
C.status = lambda key, home=None, appdir=None: ("connected", "Max") if key == "claude" else ("none", "")
PF.available = lambda c: (True, "")
root = tk.Tk()
root.withdraw()


def build(theme):
    import tempfile
    import time
    h = W.History(Path(tempfile.mkdtemp()) / "h.jsonl", 7)
    for i in range(3):
        h.items.append({"t": time.time() - i * 3600, "text": f"Dictation {i}: open the config folder for me."})
    h.items[-1]["draft"] = {"prompts": ["Open the config folder."], "notes": "",
                            "questions": [{"q": "Which one?", "why": "there are two", "options": ["a", "b"]}],
                            "engine": "claude", "model": "sonnet", "target": "code", "wall": 1.0, "t": 0}
    h.save()
    cfg = {"retention_days": 7, "color": "#1f9a3a", "color_busy": "#11782a", "opacity": 0.85, "haze": True,
           "model": "small.en", "language": None, "mic": None, "trigger_vk": 0xB3, "prompt_engine": "claude",
           "prompt_ack": True}
    w = W.AppWindow(root, h, cfg, lambda c: None, theme=theme,
                    links={"vocab": lambda: None, "folder": lambda: None})
    w.show()
    root.update()
    return w


def mapped_primaries(w):
    return [b for b in w.primaries if w.on_screen(b.f)]     # covered views are mapped too


for theme in ("light", "dark"):
    win = build(theme)
    pal = win.pal

    # --- 3. never two primaries on screen: every detail-pane state, the sheet, the other views
    assert len(mapped_primaries(win)) == 1, ("history", theme)          # Copy
    win.go("settings")
    root.update()
    assert mapped_primaries(win) == [], ("settings", theme)
    win.go("promptify")
    win._select(win.history.items[-1])
    win.p_err = (win.history.items[-1], "it failed")
    for state in W.AppWindow.STATES:
        win._show(state)
        root.update()
        n = len(mapped_primaries(win))
        assert n <= 1, (state, theme, n)
        assert (n == 0) == (state == "pick"), (state, theme, n)         # pick is the one without
    win.p_err = None
    win._open_sheet()
    root.update()
    assert mapped_primaries(win) == [], ("sheet", theme)
    win._close_sheet()
    root.update()

    # --- 4. the body measure is 80 average characters, not 80 wide (or narrow) ones
    assert win.measure == win.mf["body"].measure(W.SAMPLE80)
    avg = win.mf["body"].measure(PARA) / len(PARA)
    assert 78 * avg <= win.measure <= 82 * avg, (win.measure, "is", round(win.measure / avg, 1), "characters")

    # --- 5. each button kind's focus colour reads against its own fill: the accent ring on the
    # flat kinds, the text colour (an inner ring) on the solid ones
    fr = tk.Frame(win.views["promptify"], bg=pal["layer"])
    for kind in ("primary", "danger", "secondary", "text", "chip"):
        b = W._Btn(win, fr, "x", lambda: None, kind=kind)
        ring = b.fg if kind in ("primary", "danger") else pal["ring"]
        assert W.contrast(ring, b.fill) >= 3.0, (theme, kind, ring, b.fill, W.contrast(ring, b.fill))
        if kind == "primary":
            win.primaries.remove(b)
    # ... and the CHOSEN chip's fill, tint_hover, where the strip's keyboard cursor can sit
    assert W.contrast(pal["ring"], pal["tint_hover"]) >= 3.0, (theme, pal["ring"], pal["tint_hover"])
    fr.destroy()

    # --- and the things a screenshot would otherwise be the only check of
    # the thumb paints 4 px: clam's 1 px `bordercolor` strip either side is inside `arrowsize`
    assert win.ssb.winfo_reqwidth() == win.px(W.SP[0]) + 2, "the scroll thumb is 4 px + clam's strips"

    # --- 6. keycaps: a word's cap is its mono9 width + 6 a side, a glyph's a 20 px square; the
    # image is the raised key - `ctl` with the elevation edge along its bottom between the arcs
    # (the lip) and, in dark, the lit top; caps are decorative and every one is registered
    at = lambda img, x, y: tuple(int(v) for v in root.tk.splitlist(root.tk.call(img, "get", x, y)))
    cap, one = win.kbd_img("Ctrl"), win.kbd_img("1")
    assert cap.width() == win.mf["mono9"].measure("Ctrl") + 2 * win.px(6) and cap.height() == win.px(W.H_KBD)
    assert one.width() == one.height() == win.px(W.H_KBD)
    assert at(cap, cap.width() // 2, cap.height() // 2) == W.rgb(pal["ctl"]), theme
    assert at(cap, cap.width() // 2, cap.height() - 1) == W.rgb(pal["stroke_edge"]), theme
    assert at(cap, cap.width() // 2, 0) == W.rgb(pal["stroke_top"] if theme == "dark" else pal["stroke"](pal["ctl"])), theme
    k = win.keycap(win.views["settings"], "Esc")
    assert k in win.caps_all and k.cget("text") == "Esc" and int(k.cget("takefocus")) == 0
    assert [c.cget("text") for c in win.kbd_chord(win.views["settings"], "Ctrl", "D").winfo_children()] == ["Ctrl", "D"]
    assert win.icon("search", pal["muted"]) is win.icon("search", pal["muted"])      # cached, one per key
    assert win.icon("search", pal["muted"]).width() == win.px(W.ICON)

    # --- §6 dark parity of the Settings controls: every state is a token in both themes. The
    # toggle on (haze is on in this cfg) is a `ring` track with a white knob; the trigger cap
    # the raised recipe (lit top in dark, its hairline in light, the `stroke_edge` lip in both,
    # ink text on `ctl`, 4.5+); the slider a `stroke_strong` track, `ring` fill, a `ctl` knob
    # with the `ring` core; the chosen colour dot a 2 px `ink` ring on the card
    tg = win.ctl["haze"]
    assert at(tg.cget("image"), win.px(8), win.px(10)) == W.rgb(pal["ring"]) and at(tg.cget("image"), win.px(26), win.px(10)) == (255, 255, 255)
    lt = win.l_trig
    assert at(lt.cget("image"), win.px(44), 0) == W.rgb(pal["stroke_top"] if theme == "dark" else pal["stroke"](pal["ctl"])), theme
    assert at(lt.cget("image"), win.px(44), win.px(24) - 1) == W.rgb(pal["stroke_edge"]) and lt.cget("fg") == pal["ink"], theme
    assert W.contrast(lt.cget("fg"), pal["ctl"]) >= 4.5 and W.contrast(pal["muted"], pal["ctl"]) >= 4.5, theme   # cap text
    sl = win.ctl["opacity"]
    lines = [i for i in sl.find_all() if sl.type(i) == "line"]
    assert {sl.itemcget(i, "fill") for i in lines} == {pal["stroke_strong"], pal["ring"]}, theme
    knob = sl.itemcget(next(i for i in sl.find_all() if sl.type(i) == "image"), "image")
    assert at(knob, win.px(10), win.px(10)) == W.rgb(pal["ring"]) and at(knob, win.px(10), win.px(17)) == W.rgb(pal["ctl"]), theme
    assert W.contrast(pal["ring"], pal["card"]) >= 3.0 and W.contrast(pal["stroke_strong"], pal["card"]) >= 1.3, theme   # the track reads
    win.ctl["color"]["pick"](W.PRESETS[1])
    chosen = win.ctl["color"]["dots"][1]
    assert at(chosen.cget("image"), win.px(11), 1) == W.rgb(pal["ink"]) and at(chosen.cget("image"), win.px(11), win.px(11)) == W.rgb(W.PRESETS[1]), theme
    assert W.contrast(pal["ink"], pal["card"]) >= 4.5 and W.contrast(pal["stroke_field"], pal["card"]) >= 3.0, theme   # ring, off-toggle outline

    # --- M6. the sidebar is `base` under a grain (gaussian, sigma 3 / 2.5 - subtle: a 100-sample
    # mean within 2 of base, nothing past 5 sigma, and it IS noise); the content ground is the
    # user's white in light, exactly
    side = win.side
    tile = side.itemcget(side.find_withtag("frost")[0], "image")
    b = W.rgb(pal["base"])[0]
    vals = [int(root.tk.splitlist(root.tk.call(tile, "get", x, y))[0]) for x in range(3, 96, 9) for y in range(3, 96, 9)]
    assert abs(sum(vals) / len(vals) - b) <= 2 and max(abs(v - b) for v in vals) <= 15, (theme, b, vals[:10])
    assert len(set(vals)) > 3, (theme, "no grain")
    assert theme == "dark" or win.views["history"]["bg"] == "#ffffff" == pal["layer"]

    # --- M4. the shortcut-coverage audit, both ways: every keycap on screen names a key that is
    # bound (a `Ctrl` `F` chord needs a Control-f binding, a lone `Esc` an Escape one), and
    # every key shortcut in the bind() inventory has a cap on screen. The caps are the Labels
    # in `caps_all` plus the "cap" text items on the sidebar and the footers (canvases; a
    # footer chord shares a chord tag, a nav row's is `nav[..]["caps"]`); the bindings are the
    # toplevel's and the controls' (`bind()` hands them back normalised: <Control-Key-f>,
    # <Key-Escape>)
    KEYSYM = {"↵": "Return", "Esc": "Escape", "↑": "Up", "↓": "Down", "←": "Left", "→": "Right",
              "Del": "Delete", "Space": "space", "Tab": "Tab"}
    keysym = lambda cap: KEYSYM.get(cap, cap.lower() if cap.isalpha() else cap)
    bound = set()
    for wdg in (win.win, win.list, win.plist, win.detail, win.ctl["haze"], win.ctl["opacity"],
                win.ctl["theme"][0][0].master):
        bound |= {tuple(s.strip("<>").split("-")) for s in wdg.bind()}
    has = lambda sym, ctrl: any(s[-1] == sym and (("Control" in s) == ctrl) for s in bound)
    chords = []
    for foot in (win.foot_h, win.foot_p, win.foot_s):           # the footers' chords ...
        c = foot
        tags = sorted({t for i in c.find_withtag("cap") for t in c.gettags(i) if t[-1].isdigit()})
        chords += [[c.itemcget(i, "text") for i in c.find_withtag(t)] for t in tags]
    chords += [[side.itemcget(c, "text") for c in row["caps"] if side.type(c) == "text"][::-1]
               for row in win.nav.values()]                     # ... the nav rows' (drawn digit-first) ...
    chords += [[win.act.itemcget(i, "text") for i in win.act.find_withtag(name)]   # ... and the card's
               for name in ("copy", "prompt", "edit", "del")]   # action row (one canvas, a tag per chord)
    win._show("draft")                                            # ... and the draft's Update prompt chord
    root.update()
    chords += [[win.p_foot.itemcget(i, "text") for i in win.p_foot.find_withtag("update")]]
    assert len(chords) == 3 + 2 + 3 + 4 + 3 + 4 + 1, chords       # History view + edit, Promptify, Settings, nav, card, draft
    assert chords[-1] == ["Ctrl", "↵"], chords[-1]
    for chord in chords:
        ctrl, keys = chord[0] == "Ctrl", [c for c in chord if c != "Ctrl"]
        assert keys, chord
        for cap in keys:
            assert has(keysym(cap), ctrl), (theme, "cap without a binding", chord, sorted(bound))
    drawn = {l.cget("text") for l in win.caps_all} | {
        c.itemcget(i, "text") for c in (side, win.foot_h, win.foot_p, win.foot_s, win.act, win.p_foot)
        for i in c.find_withtag("cap")}
    assert {"↵", "Ctrl", "C", "D", "Del", "Esc", "↑", "↓", "F", "1", "2", "3", "Tab", "Space", "←", "→"} <= drawn, drawn
    # `Ctrl ,` and `Ctrl W` are the two documented undrawn synonyms (of the Settings row and Esc)
    CAP = {v: k for k, v in KEYSYM.items()}
    undrawn = {("Control", "Key", "comma"), ("Control", "Key", "w")}
    for seq in bound - undrawn:
        if "Key" not in seq or seq[-1] == "Key" or "Shift" in seq:
            continue                                            # mouse events, bare <Key>, Tab's shift twin
        cap = CAP.get(seq[-1], seq[-1].upper())
        assert cap in drawn and ("Control" not in seq or "Ctrl" in drawn), (theme, "shortcut without a cap", seq)
    win.win.destroy()

root.destroy()

# --- 7. icons: every glyph draws in its colour, inside the 16 box's 1 px safe margin --------
import io
from PIL import Image
for name in W.GLYPHS:
    im = Image.open(io.BytesIO(W.icon_png(name, "#1b1b1b")))
    a = im.getchannel("A")
    assert im.size == (16, 16) and a.getextrema()[1] == 255, (name, "blank or faint")
    l, t, r, b = a.getbbox()
    assert 1 <= l and 1 <= t and r <= 15 and b <= 15, (name, "past the safe margin", (l, t, r, b))
    assert {im.getpixel((x, y))[:3] for x in range(16) for y in range(16)
            if im.getpixel((x, y))[3] == 255} == {(27, 27, 27)}, (name, "not its colour")
assert Image.open(io.BytesIO(W.icon_png("options", "#1b1b1b", 32))).size == (32, 32)   # the grid scales
print("ui rules ok: contrasts, spacing", used, "one primary, measure, focus colours, keycaps, icons")
