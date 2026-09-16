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
    m.trigger_down = False; m.taps = 0; m.on_open = lambda: None
    m.trig_taps = m.trig_gen = 0; m.trig_held = False
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
m.last_chord_release -= 1                       # a slow later press: stop, not a tap run
chord(m); assert not m.recording; chord(m, False); assert not m.recording
assert m.recorder.calls == ["start", "stop", "start", "stop"]
# slow second tap is just another hold
m = fresh(); chord(m); chord(m, False); m.last_chord_release = _t.monotonic() - 1.0
chord(m); assert m.recording and not m.persistent; chord(m, False); assert not m.recording
# trigger key: ignored when unbound; bound, every edge is swallowed and counted. Timers are
# queued (m.later) and fired by hand, so the test controls "no further tap came"
class D: vkCode = murmur.VK_MEDIA_PLAY_PAUSE
class L:
    def __init__(self): self.suppressed = 0
    def suppress_event(self): self.suppressed += 1
def trig_setup(m):
    m.listener = L(); m.cfg["trigger_vk"] = murmur.VK_MEDIA_PLAY_PAUSE
    m.timers, m.sent = [], []
    m.later = lambda s, fn, *a: m.timers.append((s, fn, a))
    m.send_key = m.sent.append
def fire(m, delay):                              # run every queued timer of that delay, in order
    due = [t for t in m.timers if t[0] == delay]
    m.timers = [t for t in m.timers if t[0] != delay]
    for _, fn, a in due: fn(*a)
def tap(m): m.win32_event_filter(0x100, D()); m.win32_event_filter(0x101, D())
m = fresh(); m.listener = L()
m.win32_event_filter(0x100, D()); assert not m.recording and m.listener.suppressed == 0
# capture: the next key-down is reported once and swallowed, and does not record
got = []; m.capture = got.append
m.win32_event_filter(0x100, D()); m.win32_event_filter(0x101, D())
assert got == [murmur.VK_MEDIA_PLAY_PAUSE] and m.capture is None and not m.recording and m.listener.suppressed == 1
# a single tap is the key's own: nothing records, and after the tap window it is re-sent
m = fresh(); trig_setup(m)
tap(m); assert m.listener.suppressed == 2 and not m.recording and m.sent == []
fire(m, murmur.HOLD_S)                           # the hold timer: stale (the key came up)
fire(m, murmur.DOUBLE_TAP_S); assert m.sent == [murmur.VK_MEDIA_PLAY_PAUSE] and not m.recording
# murmur's own re-sent key is not caught by the filter
class Mine: vkCode = murmur.VK_MEDIA_PLAY_PAUSE; dwExtraInfo = murmur.PASS_MARK
n = m.listener.suppressed
m.win32_event_filter(0x100, Mine()); m.win32_event_filter(0x101, Mine())
assert m.listener.suppressed == n and not m.trigger_down
# double-tap starts a recording (no pass-through), a later double-tap stops it
m = fresh(); trig_setup(m)
tap(m); tap(m); fire(m, murmur.HOLD_S)
assert len([t for t in m.timers if t[0] == murmur.DOUBLE_TAP_S]) == 2
fire(m, murmur.DOUBLE_TAP_S)                     # the first tap's resolve is stale, the second's acts
assert m.recording and m.persistent and m.sent == []
tap(m); tap(m); fire(m, murmur.HOLD_S); fire(m, murmur.DOUBLE_TAP_S)
assert not m.recording and m.recorder.calls == ["start", "stop"] and m.sent == []
# a single tap during a recording stops it instead of reaching the player
tap(m); tap(m); fire(m, murmur.HOLD_S); fire(m, murmur.DOUBLE_TAP_S); assert m.recording
tap(m); fire(m, murmur.HOLD_S); fire(m, murmur.DOUBLE_TAP_S)
assert not m.recording and m.sent == []
# triple-tap opens the window and records nothing
opened = []; m = fresh(); trig_setup(m); m.on_open = lambda: opened.append(1)
tap(m); tap(m); tap(m); fire(m, murmur.HOLD_S); fire(m, murmur.DOUBLE_TAP_S)
assert opened == [1] and not m.recording and m.recorder.calls == [] and m.sent == []
# a real hold (a keyboard key): records while held, auto-repeat downs ignored, stops on release
m = fresh(); trig_setup(m)
m.win32_event_filter(0x100, D()); m.win32_event_filter(0x100, D())
assert len(m.timers) == 1 and not m.recording
fire(m, murmur.HOLD_S); assert m.recording and not m.persistent
m.win32_event_filter(0x101, D()); assert not m.recording and m.timers == [] and m.sent == []
# triple-tap on the chord too
opened = []; m = fresh(); m.on_open = lambda: opened.append(1)
chord(m); chord(m, False); chord(m); chord(m, False); chord(m)
assert opened == [1] and not m.recording
chord(m, False); assert not m.recording
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
# repetition loops (a filler then a long pause): a looping window is re-decoded once without the
# committed context and with no_repeat_ngram_size=3, and the less repetitive result is kept; a
# looping committed tail is never fed back as the prompt; ordinary windows decode once, untouched
class LSeg:
    avg_logprob, temperature, start, end = -0.1, 0.0, 0.0, 1.0
    def __init__(self, text, cr): self.text, self.compression_ratio = text, cr
kws = []
def looping(audio, **kw):
    kws.append(kw)
    if kw.get("no_repeat_ngram_size"):
        return iter([LSeg(" improve the, uh, wave voice", 1.1)]), None
    return iter([LSeg(" improve the," + " uh," * 60, 26.24)]), None
m.model.transcribe = looping; m.vocab = "tmux"
logged = []; _log = murmur.log; murmur.log = logged.append
assert m.transcribe(np.zeros(16000 * 5, dtype=np.float32), prev="Um, secondly,") == "improve the, uh, wave voice"
assert len(kws) == 2 and kws[0]["initial_prompt"] == "tmux. Um, secondly," and "no_repeat_ngram_size" not in kws[0]
assert kws[1]["initial_prompt"] == "tmux." and kws[1]["no_repeat_ngram_size"] == 3
assert any("repetition loop" in l for l in logged)
kws.clear()
m.model.transcribe = lambda audio, **kw: (kws.append(kw), (iter([LSeg(" a clean sentence.", 1.2)]), None))[1]
assert m.transcribe(np.zeros(16000 * 5, dtype=np.float32), prev="the," + " uh," * 40) == "a clean sentence."
assert len(kws) == 1 and kws[0]["initial_prompt"] == "tmux."             # the looping tail was dropped
kws.clear()
assert m.transcribe(np.zeros(16000 * 5, dtype=np.float32), prev="run the tests") == "a clean sentence."
assert len(kws) == 1 and kws[0]["initial_prompt"] == "tmux. run the tests" # a clean tail still rides along
# a re-decode that is no better is not taken
m.model.transcribe = lambda audio, **kw: (iter([LSeg(" s" * 80, 9.0 if kw.get("no_repeat_ngram_size") else 8.0)]), None)
assert m.transcribe(np.zeros(16000 * 5, dtype=np.float32)).count("s") == 80
murmur.log = _log; m.model.transcribe = _t
assert murmur.compression_ratio("uh, " * 40) > murmur.LOOP_CR > murmur.compression_ratio("please rebase the branch onto main")
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
assert murmur.load_config(cp)["trigger_vk"] == murmur.VK_MEDIA_PLAY_PAUSE
cp.write_text('{"model": "medium.en"}', encoding="utf-8")     # a model over the size cap falls back
assert murmur.load_config(cp)["model"] == murmur.DEFAULTS["model"] == "base.en"; os.remove(cp)
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
o = overlay.Overlay(lambda: None, 2.0, window=False)      # window=False: pure rendering, no Win32
assert o.hwnd is None
o.configure({"opacity": "1.7"}); assert o.opacity == 1.0
o.configure({"opacity": "x"}); assert o.opacity == 0.9
assert overlay.hex_rgb("#1a2B3c", None) == (26, 43, 60) and overlay.hex_rgb("abc", None) == (170, 187, 204)
assert overlay.hex_rgb("nope", (1, 2, 3)) == (1, 2, 3) and overlay.hex_rgb(None, (1, 2, 3)) == (1, 2, 3)
o.configure({"color": "#00ff00", "color_busy": "zzz", "haze": True})
assert o.colors["persistent"] == (0, 255, 0) and o.colors["busy"] == overlay.COLORS["busy"]
# the haze grows the window by HAZE_PAD each side (scale 2 here); off, the window is the core
assert o.ext == int(overlay.Overlay.HAZE_PAD * 2.0) and (o.w, o.h) == (o.cw + 2 * o.ext, o.ch + 2 * o.ext)
o.configure({"haze": False}); assert o.ext == 0 and (o.w, o.h) == (o.cw, o.ch)
o.configure({"color": "#00ff00", "haze": True})
tone = (0.1 * np.sin(np.arange(2048) / 16000 * 2 * np.pi * 180) + 0.05 * np.sin(np.arange(2048) / 16000 * 2 * np.pi * 1400)).astype(np.float32)
o.level = 1.0
for x in (None, np.zeros(2048, dtype=np.float32), tone, np.full(2048, np.nan, dtype=np.float32)):
    o._analyse(x)
    for st in ("idle", "recording", "persistent", "busy", "loading"):
        for o.busymix in (0.0, 0.5):
            o.state = st; img = o._render(); assert img.size == (o.w, o.h)
o.bands[:] = 0; o._analyse(tone); assert o.bands.max() > 0.3 and o.bands.min() < o.bands.max()   # tone lifts its bands
for hz in (True, False):                              # haze on and off, every state
    o.configure({"color": "#00ff00", "haze": hz})
    o.level = 1.0
    for st in ("idle", "recording", "persistent", "busy", "loading"):
        o.state = st; assert o._render().size == (o.w, o.h)
# the haze is no longer clipped: with it on, the glow crosses the line where the window edge
# used to be (row ext) and has faded out before the new edge, at rest and lit alike
o.configure({"color": "#00ff00", "haze": True}); o.bands[:] = 0
for o.state, o.level, floor in (("idle", 0.0, 1), ("recording", 1.0, 4)):   # rest is faint by design;
    # the liquid-glass body is crisper than the old blurred bars (less soft mass to spread), so the lit
    # haze reaches the old edge at 7, not 20 - still across the line the window used to clip at
    a = np.asarray(o._render())[..., 3]
    assert a[o.ext].max() >= floor, (o.state, a[o.ext].max())
    assert a[0].max() == 0 and a[-1].max() == 0 and a[:, 0].max() == 0 and a[:, -1].max() == 0
o.configure({"color": "#00ff00", "haze": False})
# Recorder keeps a rolling window of recent samples
rec = murmur.Recorder(); rec._cb(np.full((300, 1), 0.5, dtype=np.float32)); s_ = rec.samples()
assert len(s_) == 2048 and s_[-1] == 0.5 and s_[0] == 0.0
# the mic is kept by name: indices shift when Windows re-enumerates devices (the headset went 2 -> 1)
_qd = murmur.sd.query_devices
murmur.sd.query_devices = lambda *a, **k: [
    {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
    {"name": "Headset Microphone (Apple Audio", "max_input_channels": 1, "hostapi": 0},
    {"name": "Internal Digital Microphone (Ap", "max_input_channels": 1, "hostapi": 0},
    {"name": "Speakers", "max_input_channels": 0, "hostapi": 0},
    {"name": "Headset Microphone (Apple Audio Device)", "max_input_channels": 1, "hostapi": 2}]
assert murmur.resolve_device("headset microphone") == 1 and murmur.resolve_device("Internal") == 2
assert murmur.resolve_device(None) is None and murmur.resolve_device(2) == 2 and murmur.resolve_device("nope") is None
murmur.sd.query_devices = _qd
print("mic by name ok")
print("overlay render ok")

# transitions: one time-based curve (180 ms in, 650 ms out) and one premultiplied dissolve
fake = [0.0]

def fresh_overlay(samples=lambda: None, **cfg):
    ov = overlay.Overlay(samples, 2.0, window=False)
    ov.clock = lambda: fake[0]                         # fake clock: no sleeping in the test
    ov.configure(dict({"color": "#00ff00", "color_busy": "#e63c3c"}, **cfg))   # the user's config
    return ov

def alpha_sum(img):
    return int(np.asarray(img)[..., 3].astype(np.int64).sum())

# enter: level is 1.0 well before 0.25 s, and tick() renders (into .last) with no window
fake[0] = 0.0
o = fresh_overlay(); o.post("recording")
for i in range(8):                                     # 0 .. 0.28 s in 40 ms ticks
    fake[0] = i * 0.04; o.tick()
    if fake[0] >= 0.25: assert o.level == 1.0, (fake[0], o.level)
assert o.level == 1.0 and o.last is not None and o.last.size == (o.w, o.h)

# exit: alpha only falls, the hue does not drift back to the accent, gone by 0.8 s
o = fresh_overlay(samples=lambda: tone)
o.post("recording")
for _ in range(25): fake[0] += 0.04; o.tick()          # 1 s of speech
o.post("busy")
for _ in range(25): fake[0] += 0.04; o.tick()          # 1 s of transcribing
assert o.level == 1.0 and o.busymix > 0.9              # fully lit, fully in the busy colour
idle_ref = np.asarray(o._base()).astype(np.float32)   # device pixels already: the fill runs there
idle_pre = idle_ref[..., :3] * idle_ref[..., 3:4]
o.post("idle"); t0 = fake[0]; frames = []
for _ in range(25):                                    # 1 s of fade
    fake[0] += 0.04; o.tick()
    a = np.asarray(o.last).astype(np.float32)
    # hue of the light *added* to the resting stick (premultiplied): scale-free, so a colour
    # drifting from the busy red (~-170) towards the green accent (+255) shows up at once. The
    # plain mean over lit pixels cannot say this: it rises simply because the frosted glass
    # underneath shows through more as the light fades.
    add = a[..., 3].sum() - idle_ref[..., 3].sum()
    d = a[..., :3] * a[..., 3:4] - idle_pre
    frames.append((round(fake[0] - t0, 2), alpha_sum(o.last), float((d[..., 1].sum() - d[..., 0].sum()) / add) if add > 1000 else None, a))
for (t1, s1, _, _), (t2, s2, _, _) in zip(frames, frames[1:]):
    assert s2 <= s1 * 1.005, (t1, s1, t2, s2)          # monotonically non-increasing (0.5% jitter)
hue = [g for _, _, g, _ in frames if g is not None]
assert len(hue) > 4 and max(hue) <= hue[0] + 5, hue    # never drifts towards green
at08 = [a for t, _, _, a in frames if t >= 0.79][0]                   # the resting stick again
assert np.abs(at08 - idle_ref).max() <= 8 and abs(at08[..., 3].sum() - idle_ref[..., 3].sum()) < idle_ref[..., 3].sum() * 0.005
assert o.level == 0.0 and o.busymix == 0.0 and o.bands.max() == 0.0        # take forgotten
assert frames[-1][1] < frames[0][1] * 0.6   # the lit glass carries 1.82x the idle alpha (the old glow 2.05x);
                                            # 'back to rest' itself is the per-pixel at08 check above

# _dissolve: the ends are exact, the middle is the alpha average
fake[0] = 0.0
o = fresh_overlay(samples=lambda: tone)
mask = o._mask(o._spectrum_mask(1.0))                 # shapes are drawn at SS, lit at device pixels
act = overlay.Image.new("RGBA", mask.size, (0, 0, 0, 0)); o._fill(act, mask, (0, 255, 0)); base = o._base()
assert o._dissolve(base, act, 0.0) is base and o._dissolve(base, act, 1.0) is act
mid = np.asarray(o._dissolve(base, act, 0.5)).astype(np.float32)
assert np.abs(mid[..., 3] - (np.asarray(base)[..., 3].astype(np.float32) + np.asarray(act)[..., 3]) / 2).max() <= 1
# the static frame is cached: once the bands have relaxed and level has settled, the fill
# cannot change, so _render reuses it - and the reused frame must be pixel-identical
fake[0] = 0.0
o = fresh_overlay(samples=lambda: tone)
o.post("recording")
for _ in range(25): fake[0] += 0.04; o.tick()
o.post("busy")
for _ in range(60): fake[0] += 0.04; o.tick()          # the bands relax (fall 0.9) while busy
assert o.level == 1.0 and o.bands.max() == 0.0 and "fill" in o._cache
calls = [0]
real_fill = o._fill
o._fill = lambda *a: (calls.__setitem__(0, calls[0] + 1), real_fill(*a))[1]
cached = np.asarray(o._render())
assert calls[0] == 0, calls                            # served from the slot: no per-pixel work
del o._cache["fill"]
assert np.array_equal(cached, np.asarray(o._render())) and calls[0] == 1   # same pixels recomputed
del o._fill
o.post("recording")                                    # a live shape must not be served the slot
for _ in range(4): fake[0] += 0.04; o.tick()
assert o.bands.max() > 0.01 and not np.array_equal(cached, np.asarray(o.last))

# the haze layer of a static shape is cached with the fill and draws the same image
o.configure({"color": "#00ff00", "haze": True})
o.post("busy")
for _ in range(60): fake[0] += 0.04; o.tick()
assert o.level == 1.0 and o.bands.max() == 0.0 and any(k[0] == "haze" for k in o._cache if isinstance(k, tuple))
lit = np.asarray(o._render()); o._cache.pop("fill")
assert np.array_equal(lit, np.asarray(o._render()))

# Save in the Settings dialog mid-take must not flatten a live waveform
o = fresh_overlay(samples=lambda: tone)
o.post("recording")
for _ in range(25): fake[0] += 0.04; o.tick()
live = o.bands.max(); assert live > 0.3
o.configure({"color": "#00ff00", "opacity": 0.5})      # what run_app's on_save does
assert o.bands.max() == live

# the equaliser's split is timed: it eases open over ~0.5 s once the voice is there (never in one
# frame), holds through a short pause, and merges back into the plain stick after the voice stops;
# the other styles never split
fake[0] = 0.0
o = fresh_overlay(samples=lambda: tone, wave="equaliser")
o.post("recording")
opened = []
for _ in range(30):
    fake[0] += 0.04; o.tick(); opened.append(o.split)
steps = [b - a for a, b in zip([0.0] + opened, opened)]         # (the first tick sees dt 0: the fake clock)
assert max(steps) < 0.4 and 0.0 < opened[1] < 0.4, opened[:3]   # it eases open; no frame jumps it
assert next(i for i, v in enumerate(opened) if v > 0.95) * 0.04 <= 0.8, opened
o.get_samples = lambda: np.zeros(2048, dtype=np.float32)        # the voice stops, still recording
for _ in range(3):
    fake[0] += 0.04; o.tick()
assert o.split > 0.9                                            # a short pause is held open
closing = []
for _ in range(40):
    fake[0] += 0.04; o.tick(); closing.append(o.split)
assert closing[0] > 0.5 and closing[-1] == 0.0 and all(b <= a for a, b in zip(closing, closing[1:]))
assert o.bands.max() < 0.01 and o._render().size == (o.w, o.h)  # still recording: quiet, not snapped to 0
r2 = fresh_overlay(samples=lambda: tone, wave="ribbon"); r2.post("recording")
for _ in range(10):
    fake[0] += 0.04; r2.tick()
assert r2.split == 0.0
print("overlay transitions ok")
