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
        assert len([k for k in p if k[:1] == "s" and k[1:].isdigit()]) == 12, p
        assert all(n in p for n in W.STEPS)
        assert W.contrast(p["ink"], p["surface"]) >= 4.5, (acc, dark, "ink")
        assert W.contrast(p["muted"], p["surface"]) >= 4.5, (acc, dark, "muted")
        assert W.contrast(p["on_primary"], p["primary"]) >= 3.0, (acc, dark, "on_primary")
        assert W.contrast(p["on_danger"], p["danger"]) >= 3.0, (acc, dark, "on_danger")
        # the focus ring is non-text UI: 3:1 against the ground it is drawn on, or it is decoration
        assert W.contrast(p["ring"], p["bg"]) >= 3.0, (acc, dark, "ring", p["ring"])
        # where the chroma is spent: secondary TEXT is a tinted neutral (light muted used to be
        # #20701d, a span of 83 - full accent), the ring genuinely reads as the accent
        span = lambda hx: max(W.rgb(hx)) - min(W.rgb(hx))
        assert span(p["muted"]) <= 0.30 * span(p["primary"]), (acc, dark, "muted", p["muted"])
        assert span(p["ring"]) >= 0.60 * span(p["primary"]), (acc, dark, "ring", p["ring"])
        assert p["bg"] != p["surface"] != p["hover"]           # the surface ladder is visible
        # neutrals are achromatic since 2026-08-28 (a tinted ramp read as pink under a red accent);
        # the light ground is pure white by the user's word
        for n in ("bg", "surface", "hover", "active", "selected", "border", "border_strong", "muted", "ink"):
            assert len(set(W.rgb(p[n]))) == 1, (acc, dark, n, p[n])
        assert dark or p["bg"] == "#ffffff", (acc, p["bg"])
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
            "trigger_vk": 0xB3}   # note: no "indicator" key


clip = []
sys.modules["pyperclip"] = types.SimpleNamespace(copy=clip.append)

hist, cfg, saves = fake_history(), fresh_cfg(), []
win = W.AppWindow(root, hist, cfg, saves.append, theme="light",
                  links={"vocab": lambda: None, "folder": lambda: None})
win.show()
root.update()

assert len(hist.items) == 45 and win.sel is hist.items[-1]        # newest selected by default
assert "45 dictations" in win.count.cget("text") and "7 days" in win.count.cget("text")
assert len(win.rows) == 45 and win.rows[0][0] is hist.items[-1]   # newest first

# --- the sidebar lockup ---------------------------------------------------------------------
drop = lambda w: (w.nav["history"][0].winfo_rooty()
                  - w.win.grid_slaves(row=0, column=0)[0].winfo_rooty())
lock = win.win.grid_slaves(row=0, column=0)[0].pack_slaves()[0]
im_id, tx_id = lock.find_all()
assert lock.type(im_id) == "image" and lock.itemcget(tx_id, "text") == "murmur"
ib, tb, desc = lock.bbox(im_id), lock.bbox(tx_id), win.mf["title"].metrics("descent")
assert ib[3] == tb[3] - desc, (ib, tb)                 # the mark's feet on the word's baseline
assert abs((ib[3] - ib[1]) - 40 / 64 * win.px(W.H_MARK)) <= 1   # cropped to ink, not the box
assert (lock.coords(tx_id)[0] - lock.coords(im_id)[0]
        == win.mark(win.px(W.H_MARK), win.pal["muted"]).width() + win.px(W.GAP_MARK))
# a mark taller than the word must not push the nav down - the overshoot comes out of the top pad
was, W.H_MARK = W.H_MARK, 40
tall = W.AppWindow(root, fake_history(2), fresh_cfg(), lambda c: None, theme="light")
tall.show()
root.update()
assert tall.win.grid_slaves(row=0, column=0)[0].pack_slaves()[0].rise > 0   # ... and it happened
assert drop(tall) == drop(win)
tall.win.destroy()
W.H_MARK = was
print("lockup ok")

win.go("settings")
root.update()
assert win.views["settings"].winfo_ismapped() and win.view == "settings"
win.go("history")
root.update()

# selecting, keyboard, copy
win._select(win.rows[3][0])
assert win.detail.get("1.0", "end").strip() == win.rows[3][0]["text"].strip()
win._move(1)
assert win.sel is win.rows[4][0]
win._move(-1)
assert win.sel is win.rows[3][0]
win.b_copy.f.event_generate("<Button-1>")
win.b_copy.f.event_generate("<ButtonRelease-1>")
root.update()
assert clip and clip[-1] == win.rows[3][0]["text"] and win.s_text.cget("text") == "Copied"

# the longest transcript is ellipsised in the row but whole in the detail panel
row_text = [win.list.itemcget(i, "text") for i in win.list.find_all()
            if win.list.type(i) == "text"]
assert any(t.endswith("…") for t in row_text), "no row was ellipsised"
win._select(next(it for it in hist.items if len(it["text"]) > 1300))
assert len(win.detail.get("1.0", "end")) > 1300

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
print("history view ok")

# --- settings -------------------------------------------------------------------------------
win.go("settings")
root.update()
assert cfg.get("indicator") is None                      # missing key treated as waveform

# one right edge: every control column, the trigger row's button group and the header agree
edge = lambda w: w.winfo_rootx() + w.winfo_width()
for name, w in (("segment", win.ctl["style"][0][0].master), ("hex", win.ctl["color"]["entry"].master),
                ("haze", win.ctl["haze"]), ("remove", win.b_rm.f), ("saved", win.saved)):
    assert edge(w) == edge(win.r_mic.right), (name, edge(w), edge(win.r_mic.right))

# the hidden view is unmapped, not merely lowered: Tab must not walk widgets nobody can see
stop, seen = win.ctl["haze"], []
for _ in range(20):
    stop = stop.tk_focusNext()
    if str(stop) in seen:
        break
    seen.append(str(stop))
    assert str(stop).startswith(str(win.views["settings"]) + "."), stop
assert len(seen) >= 10, seen                             # ... and the whole path is reachable
win.ctl["style"][1][0].event_generate("<Button-1>")      # the "Light" segment
root.update()
assert cfg["indicator"] == "light" and saves[-1]["indicator"] == "light"
assert "light" in win.r_color.desc.cget("text")

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

win._captured(0xB0)
assert cfg["trigger_vk"] == 0xB0 and win.l_trig.cget("text") == W.MEDIA_KEYS[0xB0]
win.clear_key()
assert cfg["trigger_vk"] is None and not win.b_rm.f.winfo_ismapped()
if win.mics:   # the microphone is saved by name, never by index
    win.v_mic.set(f"{win.mics[0][0]}: {win.mics[0][1]}"); win._set_mic()
    assert cfg["mic"] == win.mics[0][1] and isinstance(cfg["mic"], str)
    win.v_mic.set("(system default)"); win._set_mic(); assert cfg["mic"] is None

# --- promptify: a fake engine, the panel over the list, chips, voice, update, copy, persist ----
import json as _json
import time as _time
import promptify as PF
calls = []


def fake_draft(cfg_, text, target="code", prompts=None, questions=None, answers=None, cancel=None, workdir=None):
    calls.append(dict(text=text, target=target, prompts=prompts, questions=questions, answers=answers))
    if prompts is None:
        return {"prompts": ["Do the thing.", "Second ask."],
                "questions": [{"q": "How many?", "why": "count", "options": ["3", "5"]},
                              {"q": "Where?", "why": "path", "options": []}],
                "notes": "heard x, wrote y", "engine": "claude", "model": "sonnet", "target": target,
                "wall": 0.1, "t": 0}
    return {"prompts": [p_ + " (updated)" for p_ in prompts], "questions": [], "notes": "",
            "engine": "claude", "model": "sonnet", "target": target, "wall": 0.1, "t": 0}


def settle(pred, n=100):
    for _ in range(n):
        root.update()
        if pred():
            return True
        _time.sleep(0.02)
    return False


PF.draft, PF.available = fake_draft, lambda c: (True, "")
win.go("history")
assert win.sel is not None and not win.panel_open
# the first use asks once; "Not now" backs out, "Promptify" remembers and runs
cfg["prompt_ack"] = False
win.promptify()
root.update()
assert win.panel_open and win.b_ack.f.winfo_ismapped() and not calls and not win.box.winfo_ismapped()
win._close_panel()
root.update()
assert not win.panel_open and win.box.winfo_ismapped() and win.card.winfo_ismapped()
win.promptify()
root.update()
win._ack_go()
assert cfg["prompt_ack"] is True and saves[-1]["prompt_ack"] is True
assert settle(lambda: win.p_fields.get("text") is not None), win.p_status.cget("text")
assert calls[-1]["text"] == win.sel["text"] and calls[-1]["prompts"] is None and calls[-1]["target"] == "code"
assert win.p_fields["text"].get("1.0", "end").strip() == "Do the thing." and len(win.p_fields["answers"]) == 2
assert win.sel["draft"]["prompts"] == ["Do the thing.", "Second ask."] and "Drafted by Claude Code" in win.p_status.cget("text")
# a chip fills its answer; a take that lands while an answer field has focus goes in at the caret
win._pick(win.p_fields["answers"][0], "5")
assert win.p_fields["answers"][0].get("1.0", "end").strip() == "5"
win.p_fields["answers"][1].focus_force()
root.update()
assert win.receive("in the repo") and win.receive("under src") \
    and win.p_fields["answers"][1].get("1.0", "end").strip() == "in the repo under src"
win.p_src.focus_force()          # the list is unmapped under the panel; the transcript is not a field
root.update()
assert not win.receive("nowhere")                         # only the panel's own fields take it
# the second prompt is reachable and edits ride along into pass 2 with the answers
win._switch_prompt(1)
assert win.p_fields["text"].get("1.0", "end").strip() == "Second ask."
win._switch_prompt(0)
win.p_fields["text"].insert("end", " Edited.")
win._update()
assert settle(lambda: calls[-1]["prompts"] is not None and win.p_fields.get("text") is not None
              and "updated" in win.p_fields["text"].get("1.0", "end"))
assert calls[-1]["prompts"] == ["Do the thing. Edited.", "Second ask."] and calls[-1]["answers"] == ["5", "in the repo under src"]
assert calls[-1]["questions"][0]["q"] == "How many?"
assert win.sel["draft"]["prompts"][0] == "Do the thing. Edited. (updated)" and not win.p_fields["answers"]
win._copy_prompt()
assert clip[-1] == "Do the thing. Edited. (updated)"
saved = next(l for l in (_json.loads(x) for x in hist.path.read_text(encoding="utf-8").splitlines())
             if l["text"] == win.sel["text"])
assert saved["draft"]["prompts"][0].endswith("(updated)")          # the draft lives on the entry, on disk
# Esc closes; reopening shows the kept draft without another call; the list is back
win._escape()
root.update()
assert not win.panel_open and win.box.winfo_ismapped()
n_calls = len(calls)
win.promptify()
root.update()
assert len(calls) == n_calls and win.p_fields["text"].get("1.0", "end").strip() == "Do the thing. Edited. (updated)"
win._close_panel()
# an engine that cannot run: no panel, the reason in the status line; a failing engine: in the panel
PF.available = lambda c: (False, "Claude Code CLI not found - install it")
win.promptify()
assert not win.panel_open and "not found" in win.s_text.cget("text")
PF.available = lambda c: (True, "")


def failing(*a, **k):
    raise PF.PromptifyError("Claude Code is not logged in: run `claude`")


PF.draft = failing
del win.sel["draft"]
win.promptify()
assert settle(lambda: "not logged in" in win.p_status.cget("text"))
assert win.panel_open and not win.b_cancel.f.winfo_ismapped()
win._close_panel()
PF.draft = fake_draft
print("promptify panel ok")
print("settings ok")

# clear-all really clears, and the empty state draws
win.go("history")
win._clear_ask()
win._clear_do()
root.update()
assert hist.items == [] and not win.act.winfo_ismapped() and not win.b_clear.f.winfo_ismapped()
assert any("No dictations yet" == win.list.itemcget(i, "text") for i in win.list.find_all())

# the wheel handler is installed on the root's "all" tag - a withdrawn window gets no <Leave>,
# so hide() has to take it down itself or it scrolls a canvas nobody can see
win.list.event_generate("<Enter>")
root.update()
assert root.bind_all("<MouseWheel>") != ""
win.hide()
root.update()
assert root.bind_all("<MouseWheel>") == "", "wheel binding leaked past hide()"
win.win.destroy()

# --- other builds ---------------------------------------------------------------------------
h2, c2, s2 = fake_history(), fresh_cfg(), []
dark = W.AppWindow(root, h2, c2, s2.append, theme="dark")
dark.show()
root.update()
assert dark.pal["ink"] != win.pal["ink"] and W.lum(dark.pal["bg"]) < 0.1
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
