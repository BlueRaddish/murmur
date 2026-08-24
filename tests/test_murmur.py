"""Run: python tests/test_murmur.py  — no framework, asserts only."""
import sys, os, subprocess, tempfile, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import murmur

# clean(): whitespace only, never rewrites words
assert murmur.clean("  hello   world \n foo ") == "hello world foo"
assert murmur.clean("git rebase --onto main") == "git rebase --onto main"

# load_vocab(): skips comments/blank lines
p = Path(tempfile.mktemp(suffix=".txt"))
p.write_text("# c\n\nfoo\nbar, baz\n", encoding="utf-8")
assert murmur.load_vocab(p) == "foo, bar, baz", murmur.load_vocab(p)
assert murmur.load_vocab(Path("nope.txt")) == ""

# state machine: hold, double-tap -> persistent, chord again stops, headset toggle
from pynput.keyboard import Key

class FakeRec:
    def __init__(self): self.calls = []; self.level = 0.0
    def start(self): self.calls.append("start")
    def stop(self): self.calls.append("stop"); return np.zeros(0, dtype=np.float32)
    def snapshot(self, start=0): return np.zeros(0, dtype=np.float32)
    total = property(lambda self: len(self.snapshot()))

def fresh():
    m = murmur.Murmur.__new__(murmur.Murmur)
    m.cfg = {"trigger_vk": None}; m.recorder = FakeRec(); m.on_state = lambda s: None; m.capture = None
    m.state = "idle"; m.held = set(); m.recording = m.persistent = m.chord_was_down = False
    m.last_chord_release = 0.0; m.lock = __import__("threading").Lock(); m.ctl = __import__("threading").Lock(); m.pending = 0
    m.handle = lambda audio, take=None: None  # never touch the model here
    m.take = None
    return m

def press(m, k): m.on_press(k)
def release(m, k): m.on_release(k)
def chord(m, down=True):
    if down: press(m, Key.ctrl_l); press(m, Key.cmd)
    else: release(m, Key.cmd); release(m, Key.ctrl_l)

# hold mode
m = fresh(); chord(m); assert m.recording and not m.persistent
chord(m, False); assert not m.recording and m.recorder.calls == ["start", "stop"]
# extra keys while holding don't restart; order of release doesn't matter
m = fresh(); chord(m); press(m, Key.shift); release(m, Key.shift)
release(m, Key.ctrl_l); assert not m.recording; release(m, Key.cmd)
assert m.recorder.calls == ["start", "stop"]
# double tap -> persistent; release keeps recording; next chord stops
import time as _t
m = fresh(); chord(m); chord(m, False); m.last_chord_release = _t.monotonic()
chord(m); assert m.persistent and m.recording; chord(m, False); assert m.recording
chord(m); assert not m.recording; chord(m, False); assert not m.recording
assert m.recorder.calls == ["start", "stop", "start", "stop"]
# slow second tap is just another hold
m = fresh(); chord(m); chord(m, False); m.last_chord_release = _t.monotonic() - 1.0
chord(m); assert m.recording and not m.persistent; chord(m, False); assert not m.recording
# trigger key: ignored when unbound, toggles persistent when bound, always suppressed when bound
class D: vkCode = murmur.VK_MEDIA_PLAY_PAUSE
class L:
    def __init__(self): self.suppressed = 0
    def suppress_event(self): self.suppressed += 1
m = fresh(); m.listener = L()
m.win32_event_filter(0x100, D()); assert not m.recording and m.listener.suppressed == 0
# capture: the next key-down is reported once and swallowed, and does not toggle
got = []; m.capture = got.append
m.win32_event_filter(0x100, D()); m.win32_event_filter(0x101, D())
assert got == [murmur.VK_MEDIA_PLAY_PAUSE] and m.capture is None and not m.recording and m.listener.suppressed == 1
m.listener.suppressed = 0
m.cfg["trigger_vk"] = murmur.VK_MEDIA_PLAY_PAUSE
m.win32_event_filter(0x100, D()); assert m.recording and m.persistent
m.win32_event_filter(0x101, D()); assert m.recording            # key-up does nothing but is swallowed
m.win32_event_filter(0x100, D()); assert not m.recording
assert m.listener.suppressed == 3
# headset start then chord stops it too
m = fresh(); m.toggle(); assert m.persistent; chord(m); assert not m.recording
# streaming off in config: the take is transcribed whole at release, handle() never waits
m = fresh(); m.cfg["streaming"] = False; chord(m); assert m.take.done.is_set(); chord(m, False)
m = fresh(); chord(m); assert not m.take.done.is_set(); chord(m, False)
print("state machine ok")

# mic failure: stays idle, no crash
m = fresh()
def boom(): raise RuntimeError("no mic")
m.recorder.start = boom; chord(m); assert not m.recording; chord(m, False); assert not m.recording
# two back-to-back dictations: "idle" only after the second one finishes
states = []
m = fresh(); m.on_state = states.append
real_handle = murmur.Murmur.handle
chord(m); chord(m, False); m.last_chord_release -= 1; chord(m); chord(m, False)
assert m.pending == 2 and states == ["recording", "busy", "recording", "busy"], states
import threading as _th
m.transcribe = lambda a, prev="": ""; m.typist = None
logged = []; _log = murmur.log; murmur.log = logged.append
real_handle(m, np.zeros(0)); assert "idle" not in states
real_handle(m, np.zeros(0)); assert states[-1] == "idle" and m.pending == 0
murmur.log = _log; assert not any("error" in l for l in logged), logged   # handle() swallows exceptions: check none happened

# streaming: a pass commits all segments but the last and advances the sample pointer;
# the final handle() transcribes only the tail and joins committed text in front
class Seg:
    avg_logprob, compression_ratio, temperature = -0.2, 1.0, 0.0
    def __init__(self, text, end, start=None): self.text, self.end, self.start = text, end, (end - 1.5 if start is None else start)
class FakeModel:
    def __init__(self): self.calls = []; self.beams = []; self.temps = []
    def transcribe(self, audio, **kw):
        self.calls.append((len(audio), kw.get("initial_prompt"))); self.beams.append(kw.get("beam_size")); self.temps.append(kw.get("temperature"))
        n = len(audio) / 16000
        if n >= 6: return iter([Seg(" first sentence.", 2.0), Seg(" second one.", 4.0), Seg(" third partial", n, start=4.4)]), None
        return iter([Seg(" the tail.", n)]), None
m = fresh(); m.model = FakeModel(); m.vocab = "tmux"; m.language = "en"
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 7, dtype=np.float32)[start:]
take = murmur.Take()
assert m._stream_pass(take) == 7 * 16000 and take.parts == ["first sentence. second one."]
assert take.committed == int(4.4 * 16000)   # start of the last (uncommitted) segment, not the end of the committed one
assert m.model.calls[-1] == (7 * 16000, "tmux.")
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 5, dtype=np.float32)[start:]   # only 1 s new since commit: wait
assert m._stream_pass(take) == 0
# a pass that finishes after the recording stopped uses the same boundary rule (never mid-word)
take2 = murmur.Take()
_t = m.model.transcribe
def stop_midway(audio, **kw): take2.active = False; return _t(audio, **kw)
m.model.transcribe = stop_midway
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 7, dtype=np.float32)[start:]
assert m._stream_pass(take2) == 7 * 16000 and take2.committed == int(4.4 * 16000) and take2.parts == ["first sentence. second one."]
m.model.transcribe = _t
# a segment end past the buffer cannot push committed past the audio
take3 = murmur.Take(); m.model.transcribe = lambda audio, **kw: (iter([Seg(" a", 1.0), Seg(" b", 99.0, start=99.0)]), None)
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 7, dtype=np.float32)[start:]
m._stream_pass(take3); assert take3.committed == 7 * 16000
# no result for the window: advance, keeping the last 1.5 s (a word Whisper refused may be starting)
take4 = murmur.Take(); m.model.transcribe = lambda audio, **kw: (iter([]), None)
m._stream_pass(take4); assert take4.committed == 7 * 16000 - 24000 and take4.parts == []
# a first segment a few frames long is not a boundary: stall instead of a 20 ms advance and a re-decode
take4b = murmur.Take(); m.model.transcribe = lambda audio, **kw: (iter([Seg(" ...", 0.02, start=0.0), Seg(" real text", 6.5, start=0.02)]), None)
assert m._stream_pass(take4b) == 7 * 16000 and take4b.committed == 0 and take4b.parts == [] and take4b.stalled == 7 * 16000
# one segment below the cap: nothing committed yet (it may still be mid-sentence)
take5 = murmur.Take(); m.model.transcribe = lambda audio, **kw: (iter([Seg(" run on", 6.5, start=0.2)]), None)
assert m._stream_pass(take5) == 7 * 16000 and take5.committed == 0 and take5.parts == []
# a stalled (single-segment) window may grow to STREAM_MAX (Whisper's 30 s); full and still one segment -> committed whole
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 40, dtype=np.float32)[start:]
seen = []; m.model.transcribe = lambda audio, **kw: (seen.append(len(audio)), (iter([Seg(" run on", 29.5, start=0.2)]), None))[1]
assert take5.stalled and m._stream_pass(take5) == 30 * 16000 and seen == [30 * 16000] and take5.committed == 30 * 16000 and take5.parts == ["run on"]
# an ordinary window is capped at STREAM_CAP even when more audio is pending
seen = []; m.model.transcribe = lambda audio, **kw: (seen.append(len(audio)), (iter([Seg(" a.", 2.0), Seg(" b", 19.0, start=3.0)]), None))[1]
take5b = murmur.Take(); assert m._stream_pass(take5b) == 20 * 16000 and seen == [20 * 16000] and take5b.committed == 3 * 16000
# one segment below the cap: stalled until STREAM_GROW more audio has arrived
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 7, dtype=np.float32)[start:]
m.model.transcribe = lambda audio, **kw: (iter([Seg(" run on", 6.5, start=0.2)]), None)
take11 = murmur.Take(); assert m._stream_pass(take11) == 7 * 16000 and take11.stalled == 7 * 16000
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 8, dtype=np.float32)[start:]
assert m._stream_pass(take11) == 0                     # only 1 s more: no retry yet
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 10, dtype=np.float32)[start:]
assert m._stream_pass(take11) == 10 * 16000            # 3 s more: retried
m.model.transcribe = _t
# a released take being transcribed takes precedence over background passes
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 7, dtype=np.float32)[start:]
m.pending = 1; assert m._stream_pass(murmur.Take()) == 0; m.pending = 0
# prompt style: the vocab list is closed with a period; the context is passed exactly as committed
# (a bare word means mid-sentence: the next window must continue it, not start a new one)
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 7, dtype=np.float32)[start:]; m.model.transcribe = _t
for prev, tail in (("hello there", "tmux. hello there"), ("disabled,", "tmux. disabled,"), ("done.", "tmux. done."), ("", "tmux.")):
    m._segments(np.zeros(16000, dtype=np.float32), prev); assert m.model.calls[-1][1].endswith(tail), (prev, m.model.calls[-1][1])
# beam size comes from config (beam 5 by default)
orig = m.model; m.model = FakeModel(); m.recorder.snapshot = lambda start=0: np.zeros(16000 * 7, dtype=np.float32)[start:]
m._stream_pass(murmur.Take()); assert m.model.beams[-1] == 5 and m.model.temps[-1] == 0.0   # beam 5, no retry ladder
m.cfg["beam_size"] = 1; m._stream_pass(murmur.Take()); assert m.model.beams[-1] == 1; del m.cfg["beam_size"]
m.model = orig; m.model.transcribe = _t
m.recorder.snapshot = lambda start=0: np.zeros(16000 * 5, dtype=np.float32)[start:]
take.active = False; take.done.set()
out = []; m.typist = type("T", (), {"type": lambda self, t: out.append(t)})(); m.on_text = lambda t: None
m.pending = 1; murmur.Murmur.handle(m, np.zeros(16000 * 7, dtype=np.float32), take)
assert out == ["first sentence. second one. the tail."], out
assert m.model.calls[-1][0] == 7 * 16000 - int(4.4 * 16000) and m.model.calls[-1][1].endswith("first sentence. second one.")   # tail only, with context, prompt punctuated
print("streaming ok")

# Recorder.snapshot(start) returns exactly the audio past `start`, copying only the chunks needed
rec = murmur.Recorder(); rec._rate = 16000
for i in range(5):
    rec._cb(np.full((1000, 1), i, dtype=np.float32))
assert rec.total == 5000 and len(rec.snapshot()) == 5000 and len(rec.snapshot(1500)) == 3500
assert rec.snapshot(1500)[0] == 1 and rec.snapshot(4999)[0] == 4 and len(rec.snapshot(5000)) == 0
print("recorder snapshot ok")
# language derives from model at load time, is never written to config
assert "language" in murmur.DEFAULTS and murmur.DEFAULTS["language"] is None

# config round-trip
cp = Path(tempfile.mktemp(suffix=".json"))
assert murmur.load_config(cp) == murmur.DEFAULTS
c = dict(murmur.DEFAULTS, mic=2, trigger_vk=0x7C); murmur.save_config(cp, c)
assert murmur.load_config(cp) == c
cp.write_text('{"headset_button": true}', encoding="utf-8")   # pre-0.5 config migrates
assert murmur.load_config(cp)["trigger_vk"] == murmur.VK_MEDIA_PLAY_PAUSE; os.remove(cp)
from window import vk_name
assert vk_name(0xB3) == "Play/Pause" and vk_name(None) == "none" and vk_name(0x41).upper() == "A"

# real transcription: Windows TTS says a sentence, Whisper must get the tech word back
wav = Path(tempfile.mktemp(suffix=".wav"))
ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
      f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('please rebase the branch onto main and run the tests'); $s.Dispose()")
subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
with wave.open(str(wav)) as w:
    assert w.getnchannels() == 1
    rate = w.getframerate()
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
# resample to 16k by linear interpolation (fine for a test)
audio = np.interp(np.arange(0, len(pcm), rate / murmur.SAMPLE_RATE), np.arange(len(pcm)), pcm).astype(np.float32)
m2 = murmur.Murmur(dict(murmur.DEFAULTS, model="tiny.en"), murmur.load_vocab(Path(__file__).parents[1] / "vocab.txt"))
text = m2.transcribe(audio)
print("transcribed:", text)
assert "rebase" in text.lower() and "main" in text.lower(), text
assert m2.transcribe(np.zeros(100, dtype=np.float32)) == ""  # too short -> ignored
os.remove(p); os.remove(wav)
print("all checks passed")

# resample(): length scales, identity when rates match
x = np.sin(np.arange(48000) / 48000 * 2 * np.pi * 440).astype(np.float32)
assert len(murmur.resample(x, 48000, 16000)) == 16000
assert murmur.resample(x, 16000, 16000) is x
print("resample ok")

# History: retention prunes, append persists, delete/clear rewrite the file
from window import History
hp = Path(tempfile.mktemp(suffix=".jsonl"))
h = History(hp, days=7)
h.append("first"); h.append("second")
assert [i["text"] for i in History(hp, 7).items] == ["first", "second"]
h.items[0]["t"] -= 8 * 86400; h.save()
assert [i["text"] for i in History(hp, 7).items] == ["second"]        # old entry gone on load
h = History(hp, 7); h.append("third"); h.delete(0)
assert [i["text"] for i in History(hp, 7).items] == ["third"]
h.clear(); assert History(hp, 7).items == [] and hp.read_text() == ""
h = History(hp, 0); h.append("x"); assert h.items == []                  # 0 days keeps nothing
os.remove(hp)
assert murmur.DEFAULTS["retention_days"] == 7
print("history ok")

# a stream that opens but fails to start is closed, not left holding the mic
class FakeStream:
    closed = False
    def start(self): raise RuntimeError("device yanked")
    def close(self): FakeStream.closed = True
rec = murmur.Recorder(); _orig = murmur.sd.InputStream; murmur.sd.InputStream = lambda **k: FakeStream()
try:
    try: rec.start(); assert False
    except RuntimeError: pass
    assert FakeStream.closed and rec._stream is None
finally: murmur.sd.InputStream = _orig
print("stream cleanup ok")

# Typist: text stays on the clipboard, paste waits for modifiers to come up, clipboard lock is retried
import pyperclip
ty = murmur.Typist(); ty.foreground_is_ours = lambda: False
pressed = []
ty.kb = type("KB", (), {"pressed": lambda self, k: __import__("contextlib").nullcontext(pressed.append(("hold", k))),
                        "press": lambda self, k: pressed.append(("press", k)), "release": lambda self, k: pressed.append(("rel", k))})()
held = {"n": 3}
def mods(): held["n"] -= 1; return held["n"] > 0
ty.modifiers_down = mods
fails = {"n": 2}; _copy = pyperclip.copy
def flaky(t):
    if fails["n"] > 0: fails["n"] -= 1; raise RuntimeError("clipboard locked")
    _copy(t)
pyperclip.copy = flaky
try:
    ty.type("hello clip")
finally:
    pyperclip.copy = _copy
assert pyperclip.paste() == "hello clip" and held["n"] == 0 and ("press", "v") in pressed
print("typist ok")

# load_model: cached copy first (no network), download only when local_files_only fails
calls = []
class FakeWM:
    def __init__(self, name, **kw):
        calls.append(kw.get("local_files_only", False))
        if kw.get("local_files_only") and name == "missing":
            raise FileNotFoundError("not cached")
assert isinstance(murmur.Murmur.load_model(FakeWM, "small.en", "cpu", "int8"), FakeWM) and calls == [True]
calls.clear()
assert isinstance(murmur.Murmur.load_model(FakeWM, "missing", "cpu", "int8"), FakeWM) and calls == [True, False]
# any other failure of the cached load is a real error: no silent trip to the network
class BrokenWM:
    def __init__(self, name, **kw):
        calls.append(kw.get("local_files_only", False)); raise RuntimeError("CUDA not available")
calls.clear()
try:
    murmur.Murmur.load_model(BrokenWM, "small.en", "cuda", "float16"); assert False
except RuntimeError:
    assert calls == [True]
assert murmur.process_age() >= 0 and murmur.T0 <= __import__("time").monotonic()
print("load_model ok")

# boosted(): priority class goes up for the block and comes back down after, even on error
import ctypes
k32 = ctypes.windll.kernel32; k32.GetCurrentProcess.restype = ctypes.c_void_p; k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
before = k32.GetPriorityClass(k32.GetCurrentProcess())
with murmur.boosted("high"):
    assert k32.GetPriorityClass(k32.GetCurrentProcess()) == murmur.PRIORITY_CLASSES["high"]
assert k32.GetPriorityClass(k32.GetCurrentProcess()) == before
try:
    with murmur.boosted("high"):
        raise RuntimeError("x")
except RuntimeError:
    pass
assert k32.GetPriorityClass(k32.GetCurrentProcess()) == before
with murmur.boosted("nonsense"):
    assert k32.GetPriorityClass(k32.GetCurrentProcess()) == before
assert os.environ["KMP_BLOCKTIME"] == "0"
print("boosted ok")

# overlay renders every state with silence, speech-like audio and garbage, without raising
import overlay
o = overlay.Overlay.__new__(overlay.Overlay)
o.scale, o.w, o.h = 2.0, int((overlay.Overlay.W + 44) * 2), int((overlay.Overlay.H + 44) * 2)
o.frame, o.anim, o._cache, o.peak = 3, 1.0, {}, 1.0
o.bands = np.zeros(overlay.Overlay.BANDS, dtype=np.float32); o.colors = dict(overlay.COLORS); o.fall = 0.9
o.opacity, o.busymix, o.rng, o.haze = 0.9, 0.0, np.random.default_rng(1), True; o.noise_phase = o.rng.uniform(0, 6.28, size=(2, 4))
o.axes = [[o.rng.uniform(0.2, 0.8, 3), o.rng.uniform(0, 6.28, 3), o.rng.uniform(0.02, 0.06, 3)] for _ in range(2)]
o.set_colors({"opacity": "1.7"}); assert o.opacity == 1.0
o.set_colors({"opacity": "x"}); assert o.opacity == 0.9
assert overlay.hex_rgb("#1a2B3c", None) == (26, 43, 60) and overlay.hex_rgb("abc", None) == (170, 187, 204)
assert overlay.hex_rgb("nope", (1, 2, 3)) == (1, 2, 3) and overlay.hex_rgb(None, (1, 2, 3)) == (1, 2, 3)
o.set_colors({"color": "#00ff00", "color_busy": "zzz"}); assert o.colors["persistent"] == (0, 255, 0) and o.colors["busy"] == overlay.COLORS["busy"]
tone = (0.1 * np.sin(np.arange(2048) / 16000 * 2 * np.pi * 180) + 0.05 * np.sin(np.arange(2048) / 16000 * 2 * np.pi * 1400)).astype(np.float32)
for x in (None, np.zeros(2048, dtype=np.float32), tone, np.full(2048, np.nan, dtype=np.float32)):
    o._analyse(x)
    for st in ("idle", "recording", "persistent", "busy", "loading"):
        for o.busymix in (0.0, 0.5):
            o.state = st; img = o._render(); assert img.size == (o.w, o.h)
o.bands[:] = 0; o._analyse(tone); assert o.bands.max() > 0.3 and o.bands.min() < o.bands.max()   # tone lifts its bands
# Recorder keeps a rolling window of recent samples
rec = murmur.Recorder(); rec._cb(np.full((300, 1), 0.5, dtype=np.float32)); s_ = rec.samples()
assert len(s_) == 2048 and s_[-1] == 0.5 and s_[0] == 0.0
print("overlay render ok")
