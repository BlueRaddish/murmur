"""murmur - hold Ctrl+Win, talk, release. Local Whisper types what you said.

Completely local: audio is transcribed on this machine and discarded; the only network
access is the one-time model download. No LLM rewriting. Technical terms are protected by
vocab.txt, which is fed to Whisper as a prompt so it prefers those spellings.

Modes
  hold        hold Ctrl+Win, speak, release -> typed.
  persistent  double-tap Ctrl+Win -> keeps recording until Ctrl+Win is pressed again.
  trigger key (opt-in) any single key - a wired headset's button, a media key, F13 - bound
              in Settings by pressing it, toggles recording. It is its own trigger, never
              translated into Ctrl+Win, so no stray modifier keystrokes reach apps.

A glassy disc at the bottom of the screen shows the mode and live mic level, so you can
see it is actually hearing you. The tray's "Open murmur" window keeps a history of
everything transcribed (default 7 days, adjustable) in case a paste goes missing.
"""
import ctypes
import time


def process_age() -> float:
    """Seconds since this process was created (Windows), so the startup timeline also counts
    the PyInstaller bootloader and its runtime hooks, which run before this file. 0 elsewhere."""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
        ft = (ctypes.c_uint64 * 4)()     # creation, exit, kernel, user as FILETIME (100 ns units)
        k32.GetProcessTimes(k32.GetCurrentProcess(), *(ctypes.byref(ft, 8 * i) for i in range(4)))
        now = ctypes.c_uint64()
        k32.GetSystemTimeAsFileTime(ctypes.byref(now))
        return max(0.0, (now.value - ft[0]) / 1e7)
    except Exception:
        return 0.0


T0 = time.monotonic() - process_age()   # the startup timeline in the log counts from launch

import argparse
import contextlib
import json
import os
import sys
import threading
from pathlib import Path

# CTranslate2 ships Intel OpenMP, whose worker threads spin for 200 ms after every parallel
# region. On a CPU that other apps keep busy that spinning is pure loss: measured 15.6 s -> 9.9 s
# for the same 6 s of audio with the spin off. Must be set before the DLL loads.
os.environ.setdefault("KMP_BLOCKTIME", "0")

import numpy as np
import pyperclip
import sounddevice as sd
from pynput import keyboard
from pynput.keyboard import Controller, Key

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", HERE))  # PyInstaller puts --add-data files here (_internal/)
APPDIR = Path(os.environ.get("APPDATA", HERE)) / "murmur"
SAMPLE_RATE = 16000
DOUBLE_TAP_S = 0.4          # second chord press within this window = persistent mode
VK_MEDIA_PLAY_PAUSE = 0xB3  # what a wired headset's inline button sends on Windows

# Hold both of these to record. Key.cmd is the Win key on Windows, Cmd on macOS.
CTRL_KEYS = {Key.ctrl, Key.ctrl_l, Key.ctrl_r}
CMD_KEYS = {Key.cmd, Key.cmd_l, Key.cmd_r}

DEFAULTS = {"model": "small.en", "device": "cpu", "language": None, "mic": None, "trigger_vk": None,
            "beam_size": 5, "streaming": True, "retention_days": 7, "color": "#e63c3c",
            "color_busy": "#ffaa32", "opacity": 0.9, "haze": False, "indicator": "waves",
            # Promptify: which engine writes the prompt, its model (blank = the engine's default),
            # a key for the API engines, a base URL for the custom one, the target the prompt is
            # written for, and whether the "this leaves the machine" line has been acknowledged
            "prompt_engine": "claude", "prompt_model": "", "prompt_key": "", "prompt_url": "",
            "prompt_target": "code", "prompt_ack": False}


def since_launch() -> str:
    return f"[+{time.monotonic() - T0:.1f}s]"


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if FROZEN:  # no console; keep a small log for diagnosis
        try:
            APPDIR.mkdir(parents=True, exist_ok=True)
            with open(APPDIR / "murmur.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("headset_button") and raw.get("trigger_vk") is None:   # pre-0.5 option
                raw["trigger_vk"] = VK_MEDIA_PLAY_PAUSE
            cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
        except (OSError, ValueError) as e:
            log(f"config ignored: {e}")
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2), encoding="utf-8")


def find_vocab() -> Path:
    """User copy in %APPDATA%\\murmur wins; the one shipped next to the program is the seed."""
    user, shipped = APPDIR / "vocab.txt", RES / "vocab.txt"
    if not user.exists() and shipped.exists():
        try:
            APPDIR.mkdir(parents=True, exist_ok=True)
            user.write_bytes(shipped.read_bytes())
        except OSError:
            return shipped
    return user


def load_vocab(path: Path) -> str:
    """vocab.txt: one term per line, '#' comments. Joined into a Whisper prompt."""
    if not path.exists():
        return ""
    terms = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    return ", ".join(terms)


def clean(text: str) -> str:
    """Whitespace only. Deliberately no rewriting - that is the point of this tool."""
    return " ".join(text.split())


PRIORITY_CLASSES = {"normal": 0x20, "above": 0x8000, "high": 0x80}


@contextlib.contextmanager
def boosted(cls: str = "above"):
    """Raise this process's priority class for the duration of a transcription burst.
    murmur idles at normal priority; while it is working the user is waiting on it, and on a
    busy machine the class decides everything: measured 15.9 s (normal) vs 8.2 s (above normal)
    vs 8.1 s (high) for the same 6 s of audio - so above-normal takes the whole win without
    starving the rest of the machine. Restored afterwards, no-op off Windows."""
    k32 = getattr(getattr(ctypes, "windll", None), "kernel32", None)
    if k32 is None or cls not in PRIORITY_CLASSES:
        yield
        return
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    me = k32.GetCurrentProcess()
    before = k32.GetPriorityClass(me)
    k32.SetPriorityClass(me, PRIORITY_CLASSES[cls])
    try:
        yield
    finally:
        k32.SetPriorityClass(me, before or PRIORITY_CLASSES["normal"])


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear interpolation, no anti-alias filter. Fine for speech at 44.1/48k -> 16k;
    upgrade path is scipy.signal.resample_poly if a device ever needs more."""
    if src == dst or len(audio) == 0:
        return audio
    idx = np.arange(0, len(audio), src / dst)
    return np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)


def resolve_device(device):
    """Config `mic` -> what sounddevice opens: None = the default input, an index as is, a name
    substring -> the first MME input whose name contains it (host API 0, one entry per mic - the
    list Settings shows). Looked up at every start, because indices shift whenever Windows
    re-enumerates audio devices: on 2026-08-27 the headset moved from 2 to 1 and a config that
    said 2 recorded the internal mic's hiss for hours. An unknown name falls back to the default."""
    if device is None or isinstance(device, int):
        return device
    q = str(device).strip().lower()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and d["hostapi"] == 0 and q in d["name"].lower():
            return i
    log(f"mic {device!r} not found, using the default input")
    return None


class Recorder:
    """Chunks are stored already at 16 kHz so snapshot() during recording is a concatenate."""

    def __init__(self, device=None):
        self.device = device
        self._opened = "?"   # last device actually opened, so the log names the mic when it changes
        self._chunks: list = []
        self._lock = threading.Lock()
        self._stream = None
        self._rate = SAMPLE_RATE
        self.level = 0.0  # RMS of the latest chunk, 0..1
        self.total = 0    # samples recorded so far (at 16 kHz)
        self.recent = np.zeros(2048, dtype=np.float32)  # last 128 ms at 16 kHz, for the visualizer

    def _cb(self, data, *_):
        mono = resample(data[:, 0].copy(), self._rate, SAMPLE_RATE)
        with self._lock:
            self._chunks.append(mono)
            self.total += len(mono)
        self.level = float(np.sqrt((data ** 2).mean()))
        n = min(len(mono), len(self.recent))
        self.recent = np.concatenate([self.recent[n:], mono[-n:]])

    def samples(self) -> np.ndarray:
        return self.recent

    def snapshot(self, start: int = 0) -> np.ndarray:
        """Audio from sample `start` on. Only the chunks past `start` are copied, so a streaming
        pass on a long take costs its window, not the whole take."""
        with self._lock:
            chunks, total = list(self._chunks), self.total
        need = total - start
        if need <= 0:
            return np.zeros(0, dtype=np.float32)
        out, n = [], 0
        for c in reversed(chunks):
            out.append(c)
            n += len(c)
            if n >= need:
                break
        audio = np.concatenate(out[::-1])
        return audio[len(audio) - need:]

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self.total = 0
        self.level = 0.0
        dev = resolve_device(self.device)
        if dev != self._opened:
            try:
                log(f"mic: {sd.query_devices(dev, 'input')['name']}")
            except Exception as e:
                log(f"mic: {dev!r} ({e})")
            self._opened = dev
        try:   # the rate that worked last time (16 k to begin with), so a take starts on the first open
            self._stream = sd.InputStream(samplerate=self._rate, channels=1, dtype="float32",
                                          device=dev, callback=self._cb)
        except sd.PortAudioError:  # device refuses it (WASAPI refuses 16k): native rate, resample later
            self._rate = int(sd.query_devices(dev, "input")["default_samplerate"])
            self._stream = sd.InputStream(samplerate=self._rate, channels=1, dtype="float32",
                                          device=dev, callback=self._cb)
        try:
            self._stream.start()
        except Exception:
            self._stream.close()   # otherwise the half-open stream holds the mic until restart
            self._stream = None
            raise

    def stop(self) -> np.ndarray:
        self._stream.stop()
        self._stream.close()
        self.level = 0.0
        self.recent = np.zeros_like(self.recent)
        return self.snapshot()


class Typist:
    """Puts the text on the clipboard and pastes it into the focused window (reliable for
    unicode, instant for long text). The text stays on the clipboard afterwards, so it can
    be pasted again by hand if the target app missed it."""

    VK_CONTROL, VK_LWIN, VK_RWIN, VK_MENU, VK_SHIFT = 0x11, 0x5B, 0x5C, 0x12, 0x10

    def __init__(self):
        self.kb = Controller()

    @staticmethod
    def foreground_is_ours() -> bool:
        """True when the focused window belongs to this process (the settings window)."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value == os.getpid()
        except Exception:
            return False

    @staticmethod
    def modifiers_down() -> bool:
        try:
            import ctypes
            gk = ctypes.windll.user32.GetAsyncKeyState
            return any(gk(vk) & 0x8000 for vk in (Typist.VK_CONTROL, Typist.VK_LWIN, Typist.VK_RWIN,
                                                   Typist.VK_MENU, Typist.VK_SHIFT))
        except Exception:
            return False

    def type(self, text: str) -> None:
        if self.foreground_is_ours():
            log("  (murmur's own window is focused; kept in history, not pasted)")
            return
        # the clipboard can be briefly locked by another app: retry rather than lose the text
        for attempt in range(8):
            try:
                pyperclip.copy(text)
                break
            except Exception as e:
                if attempt == 7:
                    log(f"  clipboard: {e}")
                    return
                time.sleep(0.05)
        # if Ctrl/Win are still physically held (short tail after a fast release, or persistent
        # mode's stop chord), Ctrl+V would become Ctrl+Win+V - wait for them to come up first
        t0 = time.monotonic()
        while self.modifiers_down() and time.monotonic() - t0 < 1.5:
            time.sleep(0.02)
        with self.kb.pressed(Key.ctrl):
            self.kb.press("v")
            self.kb.release("v")


class Take:
    """One recording's streaming state: text committed so far and how many samples it covers."""

    def __init__(self):
        self.active = True
        self.parts: list = []
        self.committed = 0
        self.stalled = 0                  # pending size at the last pass that could commit nothing
        self.done = threading.Event()


class Murmur:
    """States: idle, recording (hold), persistent, busy (transcribing).
    Every transition goes through start()/stop() so UI and log stay in step."""

    def __init__(self, cfg: dict, vocab: str, on_state=None, on_text=None):
        log(f"importing faster_whisper  {since_launch()}")
        from faster_whisper import WhisperModel
        log(f"loading {cfg['model']} on {cfg['device']}...  {since_launch()}")
        compute = "int8" if cfg["device"] == "cpu" else "float16"
        self.model = self.load_model(WhisperModel, cfg["model"], cfg["device"], compute)
        self.cfg = cfg
        self.vocab = vocab
        self.language = cfg["language"] or ("en" if cfg["model"].endswith(".en") else None)
        self.warm_up()
        log(f"model ready  {since_launch()}")
        self.recorder = Recorder(cfg["mic"])
        self.typist = Typist()
        self.on_state = on_state or (lambda s: None)
        self.on_text = on_text or (lambda t: None)   # history hook
        self.state = "idle"
        self.held: set = set()
        self.recording = False
        self.persistent = False
        self.chord_was_down = False
        self.last_chord_release = 0.0
        self.lock = threading.Lock()      # one transcription at a time
        self.ctl = threading.Lock()       # start/stop/toggle come from listener, tray and filter threads
        self.pending = 0                  # transcriptions queued or running
        self.listener = None
        self.take = None                  # the Take being recorded (streaming state)
        self.capture = None               # callback(vk): the next key pressed is reported here, once

    def _set(self, state: str) -> None:
        self.state = state
        self.on_state(state)

    @staticmethod
    def load_model(WhisperModel, name: str, device: str, compute: str):
        """The cached copy first. Without local_files_only faster-whisper asks huggingface.co
        whether the model changed on every start - ~8 s here, and the only network access
        murmur would ever make. Falls back to the download when the model is not cached."""
        with boosted():   # the user is waiting for this too: 9.6 s -> 6 s on a busy CPU
            try:
                return WhisperModel(name, device=device, compute_type=compute, local_files_only=True)
            except FileNotFoundError:   # huggingface_hub's LocalEntryNotFoundError; anything else is a real error
                log(f"  {name} is not cached; downloading...")
                return WhisperModel(name, device=device, compute_type=compute)

    def warm_up(self) -> None:
        """Create the VAD session now (~0.6 s) rather than inside the first dictation. The model
        itself gets no warm-up run: measured cold-vs-warm difference is ~0.6 s, not worth
        delaying "ready" by a full 30 s-window pass (1 s idle, 6-12 s on a busy CPU)."""
        try:
            from faster_whisper.vad import get_vad_model
            get_vad_model()
        except Exception as e:
            log(f"  vad warm-up skipped: {e}")

    # --- recording control --------------------------------------------------
    def start(self, persistent: bool) -> None:
        with self.ctl:
            if self.recording:
                if persistent and not self.persistent:  # double-tap while holding: upgrade in place
                    self.persistent = True
                    log("[persistent]")
                    self._set("persistent")
                return
            try:
                self.recorder.start()
            except Exception as e:  # mic unplugged, device busy: stay idle, keep the listener alive
                log(f"mic error: {e}")
                return
            self.recording = True
            self.persistent = persistent
            self.take = Take()
            try:
                if not self.cfg.get("streaming", True):
                    raise RuntimeError("streaming off")   # whole take at release, like v0.5
                threading.Thread(target=self._stream_loop, args=(self.take,), daemon=True).start()
            except RuntimeError:
                self.take.done.set()   # no streaming for this take; handle() must not wait forever
            log("[persistent]" if persistent else "[rec]")
            self._set("persistent" if persistent else "recording")

    def stop(self) -> None:
        with self.ctl:
            if not self.recording:
                return
            self.recording = self.persistent = False
            audio = self.recorder.stop()
            self.pending += 1
            self._set("busy")
            take, self.take = self.take, None
            take.active = False
        threading.Thread(target=self.handle, args=(audio, take), daemon=True).start()

    def toggle(self) -> None:
        """Headset button and tray menu: one press starts persistent, the next stops."""
        if self.recording:
            self.stop()
        else:
            self.start(persistent=True)

    # --- hotkey -------------------------------------------------------------
    def _chord_down(self) -> bool:
        return bool(self.held & CTRL_KEYS) and bool(self.held & CMD_KEYS)

    def chord_pressed(self, now: float) -> None:
        if self.persistent:                 # chord again while persistent = stop
            self.stop()
            return
        self.start(persistent=now - self.last_chord_release < DOUBLE_TAP_S)

    def chord_released(self, now: float) -> None:
        self.last_chord_release = now
        if not self.persistent:
            self.stop()

    def on_press(self, key) -> None:
        self.held.add(key)
        down = self._chord_down()
        if down and not self.chord_was_down:
            self.chord_pressed(time.monotonic())
        self.chord_was_down = down

    def on_release(self, key) -> None:
        self.held.discard(key)
        down = self._chord_down()
        if self.chord_was_down and not down:
            self.chord_released(time.monotonic())
        self.chord_was_down = down

    def win32_event_filter(self, msg, data) -> bool:
        """Runs for every key, before pynput's own handling. Two jobs: report the next key to a
        waiting capture callback (binding the trigger key), and act on the bound trigger key -
        key-down toggles, both down and up are swallowed so nothing else (a media player, the
        focused app) sees it. Everything else passes through untouched."""
        vk = data.vkCode
        down = msg in (0x100, 0x104)  # WM_KEYDOWN, WM_SYSKEYDOWN
        if self.capture is not None:
            if down:
                cb, self.capture = self.capture, None
                log(f"  captured key vk=0x{vk:02X}")
                cb(vk)
            self.listener.suppress_event()
            return True
        trig = self.cfg.get("trigger_vk")
        if trig is not None and vk == trig:
            if down:
                self.toggle()
            self.listener.suppress_event()
        return True

    # --- transcription ------------------------------------------------------
    def _segments(self, audio: np.ndarray, prev: str = "") -> list:
        # Whisper copies the prompt's punctuation style: a comma list with no full stop made every
        # window come out unpunctuated and lower-case, so the vocab list is closed with a period.
        # The context is passed exactly as committed: Whisper ends every sentence it finishes
        # with punctuation, so a bare word at the end means mid-sentence, and closing it made the
        # next window start a new sentence ("Docker runs The Kubernetes tests").
        prompt = " ".join(p for p in (self.vocab and self.vocab + ".", prev[-200:].strip()) if p) or None
        # beam 5 (Whisper's classic). beam_size 1 in config is 1.6x faster and scored the same on
        # TTS'd technical text, but real speech is noisier and the user rates accuracy first.
        # temperature=0: no retry ladder. Whisper's default re-decodes a piece at up to five
        # temperatures x five samples when its log-prob dips under -1.0, and keeps the *sampled*
        # result: an ordinary sentence cost 20.7 s instead of 3 s and came back as "py installer",
        # "inno setup". One deterministic decode is both faster and truer to what was said.
        with boosted():
            segs, _ = self.model.transcribe(
                audio, language=self.language, beam_size=self.cfg.get("beam_size") or 5,
                initial_prompt=prompt, vad_filter=True, condition_on_previous_text=False,
                temperature=0.0,
            )
            segs = list(segs)
        low = [s for s in segs if s.avg_logprob < -1.0 or s.compression_ratio > 2.4]
        if low:   # diagnostics only, never acted on: the text is whatever Whisper heard
            log(f"  low confidence: logprob {min(s.avg_logprob for s in low):.2f}, "
                f"compression {max(s.compression_ratio for s in low):.2f}")
        return segs

    def transcribe(self, audio: np.ndarray, prev: str = "") -> str:
        if len(audio) < SAMPLE_RATE * 0.3:  # under 300ms: a tap, not speech
            return ""
        return clean(" ".join(s.text for s in self._segments(audio, prev)))

    STREAM_MIN = 6.0     # seconds of uncommitted audio before a pass is worth its fixed cost
    STREAM_CAP = 20.0    # usual window ceiling: bounds the wait for the pass in flight at release
    STREAM_MAX = 30.0    # a window that came back as one segment may grow to Whisper's own window
    STREAM_GROW = 3.0    # after a pass that could commit nothing, this much new audio before retrying
    STREAM_KEEP = 1.5    # kept back after a window with no result: a word Whisper refused may be starting

    def _stream_loop(self, take: "Take") -> None:
        """Runs while a take is recording: passes go back-to-back as soon as STREAM_MIN seconds
        are uncommitted, so at release only the window in flight plus a short tail remain.
        Every Whisper call costs ~1 s idle / 4-5 s on a busy CPU whatever its length (the
        encoder always sees a padded 30 s window; 3 s of audio took 4.5 s, 15 s took 5.6 s),
        so windows are medium-sized and are cut only where Whisper itself ends a segment,
        decided with the continuation in view - the boundaries a whole-take transcription would
        produce. Cutting at the speaker's silences instead (tried as "phrase pieces", v0.6
        pre-release) turned a real dictation into "Go ahead and... Setup. Obsession."
        A model slower than realtime just gets bigger windows; it is never switched off."""
        while take.active:
            try:
                t0 = time.monotonic()
                covered = self._stream_pass(take)
                if covered:
                    log(f"  stream: {covered / SAMPLE_RATE:.1f}s in {time.monotonic() - t0:.1f}s")
                    continue   # straight on to the next piece if one is already waiting
            except Exception as e:
                log(f"  stream: {e}")
                break
            time.sleep(0.15)
        take.done.set()

    def _stream_pass(self, take: "Take") -> int:
        """Transcribe the next window of uncommitted audio (STREAM_MIN..STREAM_CAP seconds, up to
        STREAM_MAX once a window has come back as a single segment).
        Every segment but the last is committed and the pointer moves to where that last segment
        *starts* - a boundary Whisper chose with the continuation in view, never the window
        edge, which can fall inside a word; the held-back segment is decoded again with the
        next window. A window that could commit nothing (one segment) waits for STREAM_GROW of
        new audio instead of retrying the same audio at once; a window with no speech advances
        almost entirely (STREAM_KEEP held back); a full 30 s window that is one segment is
        committed whole, which is what faster-whisper would do with it at release anyway.
        Background passes yield to a released take's own transcription (self.pending).
        Returns the samples transcribed, 0 if there was nothing to do."""
        n = self.recorder.total - take.committed
        if self.pending or n < SAMPLE_RATE * self.STREAM_MIN or n < take.stalled + SAMPLE_RATE * self.STREAM_GROW:
            return 0
        cap = self.STREAM_MAX if take.stalled else self.STREAM_CAP
        audio = self.recorder.snapshot(take.committed)[: int(SAMPLE_RATE * cap)]
        n = len(audio)
        with self.lock:
            if not take.active:
                return 0
            segs = self._segments(audio, " ".join(take.parts))
        take.stalled = 0
        full = n >= SAMPLE_RATE * self.STREAM_MAX
        advance = min(int(segs[-1].start * SAMPLE_RATE), n) if len(segs) >= 2 else 0
        if len(segs) >= 2 and advance >= SAMPLE_RATE * self.STREAM_KEEP:
            take.parts.append(clean(" ".join(s.text for s in segs[:-1])))
            take.committed += advance
        elif segs and full:   # 30 s and still no usable boundary: take it whole, as a whole-take call would
            take.parts.append(clean(" ".join(s.text for s in segs)))
            take.committed += n
        elif not segs:
            take.committed += max(0, n - int(SAMPLE_RATE * self.STREAM_KEEP))
        else:
            # one segment, or a first segment a few frames long (a stray token at the window start
            # would otherwise move the pointer 20 ms and re-decode the same audio at once, appending
            # the junk every pass): nothing safe to commit, wait for more audio
            take.stalled = n
        return n

    def handle(self, audio: np.ndarray, take: "Take" = None) -> None:
        if take is None:
            take = Take()
            take.done.set()
        t0 = time.time()
        take.done.wait()      # an in-flight pass may still be committing
        waited = time.time() - t0
        with self.lock:  # one transcription at a time
            try:
                tail = self.transcribe(audio[take.committed:], " ".join(take.parts))
                text = clean(" ".join(p for p in take.parts + [tail] if p))
                if not text:
                    rms = float(np.sqrt((audio ** 2).mean())) if len(audio) else 0.0
                    log(f"  (nothing heard: {len(audio) / SAMPLE_RATE:.1f}s, rms {rms:.4f})")
                    return
                shown = f"{len(text.split())} words" if FROZEN else text   # murmur.log keeps counts, not text
                log(f"  {shown}  [{time.time() - t0:.1f}s, of which {waited:.1f}s waiting for the pass in flight]")
                self.typist.type(text)
                try:
                    self.on_text(text)        # bookkeeping: never allowed to cost the paste
                except Exception as e:
                    log(f"  history: {e}")
            except Exception as e:
                log(f"  error: {e}")
            finally:
                with self.ctl:
                    self.pending -= 1
                    if not self.recording and self.pending == 0:
                        self._set("idle")

    def run(self) -> None:
        log(f"ready: hold Ctrl+Win and talk; double-tap for persistent mode.  {since_launch()}")
        self._set("idle")
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release,
                                          win32_event_filter=self.win32_event_filter)
        self.listener.start()

    def quit(self) -> None:
        if self.listener:
            self.listener.stop()


# --- UI: tray icon, overlay, app window -------------------------------------------
COLORS = {"idle": "#8b908b", "recording": "#dc3232", "persistent": "#dc3232", "busy": "#ffaa32",
          "loading": "#ffaa32"}   # tray tints: the fallbacks when config's colours are unusable
LABELS = {"idle": "ready (Ctrl+Win)", "recording": "recording", "persistent": "persistent, Ctrl+Win stops",
          "busy": "transcribing...", "loading": "loading model..."}
CFG_COLOR = {"recording": "color", "persistent": "color", "busy": "color_busy", "loading": "color_busy"}


def tray_color(state: str, cfg: dict):
    """Tint for the tray mark: the colours the bar itself shows - the accent while the mic is open
    (recording and persistent alike, as on the bar; the tray title says which), the transcribing
    colour while it types - so tray and overlay never disagree. idle is a neutral that stays
    visible on a light and a dark taskbar."""
    from overlay import hex_rgb
    fallback = hex_rgb(COLORS[state], (139, 144, 139))
    key = CFG_COLOR.get(state)
    return hex_rgb(cfg.get(key) or "", fallback) if key else fallback


def make_icon(state: str, cfg: dict):
    from brand import mark
    return mark(64, tray_color(state, cfg))


def run_app(factory, cfg: dict, cfg_path: Path) -> None:
    """Main thread: Tk root (hidden) drives the overlay timer and owns the app window.
    Background threads: tray icon, model load. The model loads after the UI is up."""
    import tkinter as tk
    import pystray
    from overlay import Overlay, set_dpi_aware
    from window import AppWindow, History
    import promptify
    promptify.log = log               # engine diagnostics (never its text) into murmur.log

    scale = set_dpi_aware()           # before Tk() so tkinter gets real pixels too
    root = tk.Tk()
    root.withdraw()
    root.tk.call("tk", "scaling", scale * 96 / 72)
    holder = {"app": None}
    history = History(APPDIR / "history.jsonl", cfg["retention_days"])
    overlay = Overlay(lambda: holder["app"].recorder.samples() if holder["app"] else None, scale)
    overlay.configure(cfg)

    def on_save(c: dict) -> None:
        save_config(cfg_path, c)
        overlay.configure(c)
        # a colour change in Settings must reach the tray now, not at the next state change
        icon.icon = make_icon(holder["app"].state if holder["app"] else "loading", c)

    win = AppWindow(root, history, cfg, on_save, scale, get_app=lambda: holder["app"],
                    links={"vocab": lambda: os.startfile(find_vocab()), "folder": lambda: os.startfile(APPDIR)},
                    icon=RES / "murmur.ico")
    icon = pystray.Icon("murmur", make_icon("loading", cfg), "murmur: " + LABELS["loading"])

    def on_state(state: str) -> None:
        icon.icon = make_icon(state, cfg)
        icon.title = "murmur: " + LABELS[state]
        overlay.post(state)

    def on_text(text: str) -> None:
        # marshal to the Tk thread: History is not locked. The window gets the take too: when
        # one of its own fields has focus (an answer in the Promptify panel) the text goes in
        # there - Typist skipped the paste because our window was in front
        def land():
            if cfg["retention_days"] > 0:
                history.append(text)
                win.refresh()
            win.receive(text)
        root.after(0, land)

    def quit_all(icon_=None, item=None) -> None:
        if holder["app"]:
            holder["app"].quit()
        icon.visible = False   # stop() is a no-op until the tray thread is ready; hide regardless
        icon.stop()
        root.after(0, root.destroy)

    def open_window(icon_=None, item=None) -> None:
        root.after(0, win.show)

    icon.menu = pystray.Menu(
        pystray.MenuItem("Open murmur", open_window, default=True),
        pystray.MenuItem("Start/stop recording", lambda i, it: holder["app"] and holder["app"].toggle()),
        pystray.MenuItem("Edit vocab.txt", lambda i, it: os.startfile(find_vocab())),
        pystray.MenuItem("Open config/log folder", lambda i, it: os.startfile(APPDIR)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_all),
    )

    def load() -> None:
        overlay.post("loading")
        try:
            holder["app"] = factory()
        except Exception as e:
            log(f"failed to start: {e}")
            quit_all()
            return
        holder["app"].on_state = on_state
        holder["app"].on_text = on_text
        holder["app"].run()

    def tick() -> None:
        try:
            overlay.tick()
        except Exception as e:   # a draw bug must not stop the timer (the overlay would freeze)
            log(f"overlay: {e}")
        root.after(40, tick)

    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=load, daemon=True).start()
    log(f"ui up  {since_launch()}")
    root.after(40, tick)
    try:
        root.mainloop()
    finally:
        overlay.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="murmur", description=__doc__.split("\n")[0])
    p.add_argument("--model", help="faster-whisper model (tiny.en, base.en, small.en, medium.en, large-v3)")
    p.add_argument("--device", choices=["cpu", "cuda"])
    p.add_argument("--language", help="force a language code, e.g. en, ko. default: en for *.en models")
    p.add_argument("--mic", help="input device index or name substring (see --list-devices)")
    p.add_argument("--trigger-vk", type=lambda v: int(v, 0), help="virtual-key code that toggles recording (e.g. 0xB3 = Play/Pause)")
    p.add_argument("--vocab", type=Path, help="terms file fed to Whisper as a prompt")
    p.add_argument("--config", type=Path, default=APPDIR / "config.json")
    p.add_argument("--console", action="store_true", help="no tray/overlay/window; log to the console")
    p.add_argument("--list-devices", action="store_true", help="print audio devices and exit")
    a = p.parse_args(argv)
    if a.list_devices:
        print(sd.query_devices())
        return 0

    cfg = load_config(a.config)
    if not a.config.exists():
        save_config(a.config, cfg)  # persistent settings live here; flags below are per-run
    for k in ("model", "device", "language", "mic"):
        if getattr(a, k) is not None:
            cfg[k] = getattr(a, k)
    if a.trigger_vk is not None:
        cfg["trigger_vk"] = a.trigger_vk
    if isinstance(cfg["mic"], str) and cfg["mic"].isdigit():
        cfg["mic"] = int(cfg["mic"])
    vocab = load_vocab(a.vocab or find_vocab())
    factory = lambda: Murmur(cfg, vocab)

    if a.console:
        try:
            app = factory()
            app.run()
            while app.listener.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return 0
    run_app(factory, cfg, a.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
