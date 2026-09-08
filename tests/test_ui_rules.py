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
    assert W.contrast(p["ring"], p["bg"]) >= 3.0, (dark, "ring on the ground", p["ring"], p["bg"])
    assert W.contrast(p["on_primary"], p["primary"]) >= 4.5, (dark, "text on the primary")

# --- 2. every SP-derived spacing the file uses is one of the six ---------------------------
src = Path(W.__file__).read_text(encoding="utf-8")
used = sorted({int(i) for i in re.findall(r"\bSP\[(\d)\]", src)})
assert used, "no SP-derived spacing found"
for i in used:
    assert W.SP[i] in SPACING, (f"SP[{i}]", W.SP[i], "is not in", sorted(SPACING))

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
    fr = tk.Frame(win.views["promptify"], bg=pal["bg"])
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
    win.win.destroy()

root.destroy()
print("ui rules ok: contrasts, spacing", used, "one primary, measure, focus colours")
