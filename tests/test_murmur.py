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
    def snapshot(self): return np.zeros(0, dtype=np.float32)

def fresh():
    m = murmur.Murmur.__new__(murmur.Murmur)
    m.cfg = {"headset_button": False}; m.recorder = FakeRec(); m.on_state = lambda s: None
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
# headset button: ignored when off, toggles persistent when on, always suppressed when on
class D: vkCode = murmur.VK_MEDIA_PLAY_PAUSE
class L:
    def __init__(self): self.suppressed = 0
    def suppress_event(self): self.suppressed += 1
m = fresh(); m.listener = L()
m.win32_event_filter(0x100, D()); assert not m.recording and m.listener.suppressed == 0
m.cfg["headset_button"] = True
m.win32_event_filter(0x100, D()); assert m.recording and m.persistent
m.win32_event_filter(0x101, D()); assert m.recording            # key-up does nothing but is swallowed
m.win32_event_filter(0x100, D()); assert not m.recording
assert m.listener.suppressed == 3
# headset start then chord stops it too
m = fresh(); m.toggle(); assert m.persistent; chord(m); assert not m.recording
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
m.transcribe = lambda a: ""; m.typist = None
real_handle(m, np.zeros(0)); assert "idle" not in states
real_handle(m, np.zeros(0)); assert states[-1] == "idle" and m.pending == 0

# streaming: a pass commits all segments but the last and advances the sample pointer;
# the final handle() transcribes only the tail and joins committed text in front
class Seg:
    def __init__(self, text, end, start=None): self.text, self.end, self.start = text, end, (end - 1.5 if start is None else start)
class FakeModel:
    def __init__(self): self.calls = []
    def transcribe(self, audio, **kw):
        self.calls.append((len(audio), kw.get("initial_prompt")))
        n = len(audio) / 16000
        if n >= 6: return iter([Seg(" first sentence.", 2.0), Seg(" second one.", 4.0), Seg(" third partial", n, start=4.4)]), None
        return iter([Seg(" the tail.", n)]), None
m = fresh(); m.model = FakeModel(); m.vocab = "tmux"; m.language = "en"
m.recorder.snapshot = lambda: np.zeros(16000 * 7, dtype=np.float32)
take = murmur.Take()
assert m._stream_pass(take) == 7 * 16000 and take.parts == ["first sentence. second one."]
assert take.committed == int(4.4 * 16000)   # start of the last (uncommitted) segment, not the end of the committed one
assert m.model.calls[-1] == (7 * 16000, "tmux")
m.recorder.snapshot = lambda: np.zeros(16000 * 5, dtype=np.float32)   # only 1 s new since commit: wait
assert m._stream_pass(take) == 0
# a pass that finishes after the recording stopped uses the same boundary rule (never mid-word)
take2 = murmur.Take()
_t = m.model.transcribe
def stop_midway(audio, **kw): take2.active = False; return _t(audio, **kw)
m.model.transcribe = stop_midway
m.recorder.snapshot = lambda: np.zeros(16000 * 7, dtype=np.float32)
assert m._stream_pass(take2) == 7 * 16000 and take2.committed == int(4.4 * 16000) and take2.parts == ["first sentence. second one."]
m.model.transcribe = _t
# a segment end past the buffer cannot push committed past the audio
take3 = murmur.Take(); m.model.transcribe = lambda audio, **kw: (iter([Seg(" a", 1.0), Seg(" b", 99.0, start=99.0)]), None)
m.recorder.snapshot = lambda: np.zeros(16000 * 3, dtype=np.float32)
m._stream_pass(take3); assert take3.committed == 3 * 16000
m.model.transcribe = _t
m.recorder.snapshot = lambda: np.zeros(16000 * 5, dtype=np.float32)
take.active = False; take.done.set()
out = []; m.typist = type("T", (), {"type": lambda self, t: out.append(t)})(); m.on_text = lambda t: None
m.pending = 1; murmur.Murmur.handle(m, np.zeros(16000 * 7, dtype=np.float32), take)
assert out == ["first sentence. second one. the tail."], out
assert m.model.calls[-1][0] == 7 * 16000 - int(4.4 * 16000) and "first sentence" in m.model.calls[-1][1]   # tail only, with context
print("streaming ok")
# language derives from model at load time, is never written to config
assert "language" in murmur.DEFAULTS and murmur.DEFAULTS["language"] is None

# config round-trip
cp = Path(tempfile.mktemp(suffix=".json"))
assert murmur.load_config(cp) == murmur.DEFAULTS
c = dict(murmur.DEFAULTS, mic=2, headset_button=True); murmur.save_config(cp, c)
assert murmur.load_config(cp) == c; os.remove(cp)

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

# overlay renders every state at zero and full level without raising (no window needed)
import overlay
o = overlay.Overlay.__new__(overlay.Overlay)
o.scale, o.w, o.h = 2.0, int((overlay.Overlay.W + 28) * 2), int((overlay.Overlay.H + 28) * 2)
o.frame, o.anim, o._cache = 3, 1.0, {}
from collections import deque
for lvl in (0.0, 0.001, 0.5):
    o.hist = deque([lvl] * overlay.Overlay.BARS, maxlen=overlay.Overlay.BARS)
    for st in ("idle", "recording", "persistent", "busy", "loading"):
        o.state = st; img = o._render(); assert img.size == (o.w, o.h)
print("overlay render ok")
