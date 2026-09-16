"""murmur - hold Ctrl+Win, talk, release. Local Whisper types what you said.

Completely local: audio is transcribed on this machine and discarded; the only network
access is the one-time model download. No LLM rewriting. Technical terms are protected by
vocab.txt, which is fed to Whisper as a prompt so it prefers those spellings.

Modes
  hold        hold Ctrl+Win, speak, release -> typed.
  persistent  double-tap Ctrl+Win -> keeps recording until Ctrl+Win is pressed again.
  triple-tap  three fast taps -> opens the murmur window instead of recording.
  trigger key (opt-in) any single key - a wired headset's button, a media key, F13 - bound
              in Settings by pressing it. A single tap still does the key's own job (a
              headset's Play/Pause keeps playing and pausing); double-tap starts or stops a
              recording, triple-tap opens the window, a real hold records while held. It is
              its own trigger,
              never translated into Ctrl+Win, so no stray modifier keystrokes reach apps.

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
import zlib
from pathlib import Path

# CTranslate2 ships Intel OpenMP, whose worker threads spin for 200 ms after every parallel
# region. On a CPU that other apps keep busy that spinning is pure loss: measured 15.6 s -> 9.9 s
# for the same 6 s of audio with the spin off. Must be set before the DLL loads.
os.environ.setdefault("KMP_BLOCKTIME", "0")

import numpy as np
import pyperclip
from pynput import keyboard
from pynput.keyboard import Controller, Key

sd = None   # sounddevice: 0.20 s of the 0.46 s this module costs to import, all of it PortAudio's
            # DLL, and nothing needs it until a recording starts. `audio()` below loads it on the
            # loader thread instead, so the tray icon and the overlay do not wait for it.


def audio():
    """The sounddevice module, imported on first use."""
    global sd
    if sd is None:
        import sounddevice
        sd = sounddevice
    return sd


FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", HERE))  # PyInstaller puts --add-data files here (_internal/)
APPDIR = Path(os.environ.get("APPDATA", HERE)) / "murmur"
SAMPLE_RATE = 16000
DOUBLE_TAP_S = 0.4          # press within this window of the last release continues a tap run:
                            # two taps = persistent mode, three = open the window
HOLD_S = 0.3                # a trigger key still down after this long is a hold (records while
                            # held). A headset's inline button never gets here: Windows reports it
                            # as an instant down/up pair however long it is held (murmur.log: every
                            # "hold" ended in 0.1-0.3 s), so on that button only taps exist.
PASS_MARK = 0x6D726D72      # dwExtraInfo on the key murmur re-sends, so its own filter lets it by
LOOP_CR = 2.4               # text whose zlib compression ratio passes this is a repetition loop -
                            # Whisper's own hallucination threshold. The user's "uh + long pause"
                            # takes logged 26.24 three windows running (2026-09-15)
VK_MEDIA_PLAY_PAUSE = 0xB3  # what a wired headset's inline button sends on Windows
FRAME_MS = 33               # the overlay's timer: 30 fps. A frame costs 2-7 ms since the render
                            # moved to device pixels (2026-09-15); at 25 fps the motion still read
                            # as steppy on the user's machine.

# Hold both of these to record. Key.cmd is the Win key on Windows, Cmd on macOS.
CTRL_KEYS = {Key.ctrl, Key.ctrl_l, Key.ctrl_r}
CMD_KEYS = {Key.cmd, Key.cmd_l, Key.cmd_r}

DEFAULTS = {"model": "base.en", "device": "cpu", "language": None, "mic": None, "trigger_vk": None,
            "beam_size": 5, "streaming": True, "retention_days": 7, "color": "#e63c3c",
            "color_busy": "#ffaa32", "opacity": 0.9, "haze": False, "theme": "system", "wave": "ribbon",
            # Promptify: which engine writes the prompt, its model (blank = the engine's default),
            # the target the prompt is written for, whether one dictation may split into several
            # prompts, and whether the "this leaves the machine" line has been acknowledged
            "prompt_engine": "claude", "prompt_models": {}, "prompt_target": "code",
            "prompt_split": False, "prompt_ack": False,
            # the Obsidian bridge: a vault path (None = off), folders kept out of its index, and
            # whether the with-vault disclosure has been acknowledged
            "vault_path": None, "vault_exclude": [], "vault_ack": False}


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


def install_crash_log(root=None) -> None:
    """Send every unhandled exception to murmur.log.

    The shipped build is --noconsole, so it has no stderr: without these three hooks a crash in
    a Tk callback (the likeliest kind - every button and timer runs there), or in one of the
    worker threads, is printed to nowhere. The window simply stops responding and the log shows
    the run ending normally. The tray's "Open config/log folder" is how a user gets the trace."""
    import traceback

    def crash(where, exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log(f"{where}:\n" + "".join(traceback.format_exception(exc_type, exc, tb)).rstrip())

    sys.excepthook = lambda t, e, tb: crash("crash", t, e, tb)
    threading.excepthook = lambda a: crash(f"crash in thread {a.thread.name}",
                                           a.exc_type, a.exc_value, a.exc_traceback)
    if root is not None:
        root.report_callback_exception = lambda t, e, tb: crash("crash in a UI callback", t, e, tb)


NULLABLE = {"language": str, "mic": (int, str), "trigger_vk": int, "vault_path": str}


def fits(key: str, value) -> bool:
    """Is `value` the shape this key is used as? The tray offers "Open config/log folder", so
    config.json is a hand-edited file, and a key filter alone lets "retention_days": "seven"
    through - it then raises a TypeError after every dictation, far from here and, in a windowed
    build, silently. Keys whose default is None carry their type in NULLABLE."""
    want = DEFAULTS[key]
    if value is None:
        return True                             # None means "unset" for every key that allows it
    if want is None:
        return isinstance(value, NULLABLE.get(key, object))
    if isinstance(want, bool):                  # bool before int: in Python a bool IS an int
        return isinstance(value, bool)
    if isinstance(want, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(want))


MUTEX_NAME = "Local\\murmur-running"       # "Local\\" = per login session, so two users each get one
OPEN_EVENT = "Local\\murmur-open-window"
_instance_lock = None                      # held for the life of the process; never garbage-collected


def claim_instance(name: str = MUTEX_NAME, event: str = OPEN_EVENT) -> bool:
    """True if this process is the only murmur. False means one is already running - and it has
    been asked to show its window, which is what clicking the shortcut is for.

    `name`/`event` are arguments only so the tests can claim a pair of their own: claiming the
    real ones would fail the suite whenever murmur itself is running, which is most of the time.

    Without this, launching murmur again (easy: the app is invisible apart from a tray icon, so
    the Start menu entry looks like it did nothing) leaves two keyboard hooks and two overlays
    running, and every dictation gets pasted twice."""
    global _instance_lock
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.OpenEventW.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.CreateMutexW(None, False, name)
        if h and ctypes.get_last_error() == 183:        # ERROR_ALREADY_EXISTS
            ev = k32.OpenEventW(0x0002, False, event)        # EVENT_MODIFY_STATE
            if ev:
                k32.SetEvent(ctypes.c_void_p(ev))
                k32.CloseHandle(ctypes.c_void_p(ev))
            k32.CloseHandle(ctypes.c_void_p(h))
            return False
        _instance_lock = h
    except Exception as e:      # not Windows, or the call failed: never block the app over this
        log(f"single-instance check skipped: {e}")
    return True


def watch_open_requests(on_open, event: str = OPEN_EVENT) -> None:
    """A later launch sets this event instead of starting a second murmur; show the window."""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateEventW.restype = ctypes.c_void_p
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ev = k32.CreateEventW(None, False, False, event)        # auto-reset
        if not ev:
            return

        def wait():
            while k32.WaitForSingleObject(ctypes.c_void_p(ev), 0xFFFFFFFF) == 0:
                on_open()
        threading.Thread(target=wait, daemon=True, name="open-watch").start()
    except Exception as e:
        log(f"open-request watch skipped: {e}")


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("headset_button") and raw.get("trigger_vk") is None:   # pre-0.5 option
                raw["trigger_vk"] = VK_MEDIA_PLAY_PAUSE
            for k, v in raw.items():
                if k not in DEFAULTS:
                    continue
                if fits(k, v):
                    cfg[k] = v
                else:
                    log(f"config: {k}={v!r} is not a {type(DEFAULTS[k]).__name__ if DEFAULTS[k] is not None else 'valid value'}"
                        f", using {cfg[k]!r}")
        except (OSError, ValueError) as e:
            log(f"config ignored: {e}")
    from window import MODELS            # the picker's list is the one allowed set
    if cfg.get("model") not in MODELS:   # a medium/large left over from before the size cap
        log(f"config: model {cfg.get('model')!r} is not offered any more, using {DEFAULTS['model']}")
        cfg["model"] = DEFAULTS["model"]
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    """Written beside the target and renamed over it: os.replace is atomic, so a crash or a power
    cut during the write leaves the previous settings intact rather than a truncated file that
    loads as defaults."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.new")
    tmp.write_text(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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


def compression_ratio(text: str) -> float:
    b = text.encode("utf-8")
    return len(b) / max(1, len(zlib.compress(b)))


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
    for i, d in enumerate(audio().query_devices()):
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
        self.dropped = 0  # samples released after being transcribed; snapshots start here at the earliest
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

    @staticmethod
    def _join(chunks: list, need: int) -> np.ndarray:
        """The last `need` samples of `chunks`, copying only the chunks that carry them."""
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

    def snapshot(self, start: int = 0) -> np.ndarray:
        """Audio from sample `start` on (or from the oldest sample still held, if that is later).
        Only the chunks past `start` are copied, so a streaming pass on a long take costs its
        window, not the whole take."""
        with self._lock:
            chunks, total, start = list(self._chunks), self.total, max(start, self.dropped)
        return self._join(chunks, total - start)

    def release(self, upto: int) -> None:
        """Drop audio that has been transcribed and committed: nothing ever reads below the
        take's committed pointer again. Without this a take holds all of itself for its whole
        life - a 30 min take measured 126 MB held, plus 115 MB more for the copy stop() makes,
        a 244 MB peak for audio finished with minutes earlier. Whole chunks only, so the
        sample indices the caller holds stay meaningful."""
        with self._lock:
            n, k = self.dropped, 0
            while k < len(self._chunks) and n + len(self._chunks[k]) <= upto:
                n += len(self._chunks[k])
                k += 1
            if k:
                del self._chunks[:k]     # one shift per pass, not one per chunk
                self.dropped = n

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self.total = self.dropped = 0
        self.level = 0.0
        dev = resolve_device(self.device)
        if dev != self._opened:
            try:
                log(f"mic: {audio().query_devices(dev, 'input')['name']}")
            except Exception as e:
                log(f"mic: {dev!r} ({e})")
            self._opened = dev
        a = audio()
        try:   # the rate that worked last time (16 k to begin with), so a take starts on the first open
            self._stream = a.InputStream(samplerate=self._rate, channels=1, dtype="float32",
                                         device=dev, callback=self._cb)
        except a.PortAudioError:  # device refuses it (WASAPI refuses 16k): native rate, resample later
            self._rate = int(a.query_devices(dev, "input")["default_samplerate"])
            self._stream = a.InputStream(samplerate=self._rate, channels=1, dtype="float32",
                                         device=dev, callback=self._cb)
        try:
            self._stream.start()
        except Exception:
            self._stream.close()   # otherwise the half-open stream holds the mic until restart
            self._stream = None
            raise

    def stop(self) -> tuple:
        """The take's remaining audio and the sample index it starts at. Both are read under one
        lock: a streaming pass still in flight may release more audio immediately afterwards, and
        a base taken separately from the audio would slice the tail at the wrong place."""
        self._stream.stop()
        self._stream.close()
        self.level = 0.0
        self.recent = np.zeros_like(self.recent)
        with self._lock:
            chunks, total, base = list(self._chunks), self.total, self.dropped
        return self._join(chunks, total - base), base


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
        self.base = 0                     # first sample the audio handed to handle() carries
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
        self.on_open = lambda: None       # triple-tap: run_app points this at the window
        self.state = "idle"
        self.held: set = set()
        self.recording = False
        self.persistent = False
        self.chord_was_down = False
        self.trigger_down = False
        self.trig_taps = 0                # taps in the trigger key's current run
        self.trig_gen = 0                 # bumped on every trigger edge: stale timers see it and stand down
        self.trig_held = False            # the current press became a hold (a take is recording)
        self.later = lambda s, fn, *a: threading.Timer(s, fn, args=a).start()   # tests swap this
        self.last_chord_release = 0.0
        self.taps = 0                     # length of the current fast-tap run
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
        audio()   # PortAudio's DLL (0.20 s) on this thread, not the UI thread and not the first take
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
            audio, base = self.recorder.stop()
            self.pending += 1
            self._set("busy")
            take, self.take = self.take, None
            take.active = False
            take.base = base
        threading.Thread(target=self.handle, args=(audio, take), daemon=True).start()

    def toggle(self) -> None:
        """Tray menu: one click starts persistent, the next stops."""
        if self.recording:
            self.stop()
        else:
            self.start(persistent=True)

    def discard(self) -> None:
        """Stop without transcribing - the fraction-of-a-second take a triple-tap's second
        press started."""
        with self.ctl:
            if not self.recording:
                return
            self.recording = self.persistent = False
            self.recorder.stop()
            take, self.take = self.take, None
            take.active = False
            log("[discard]")
            self._set("idle")

    # --- hotkey -------------------------------------------------------------
    def _chord_down(self) -> bool:
        return bool(self.held & CTRL_KEYS) and bool(self.held & CMD_KEYS)

    def chord_pressed(self, now: float) -> None:
        run = now - self.last_chord_release < DOUBLE_TAP_S
        self.taps = self.taps + 1 if run else 1
        if self.taps == 3:                  # triple-tap: the window, not a take
            self.taps = 0
            self.discard()
            self.on_open()
            return
        if self.persistent:                 # press while persistent = stop
            self.stop()
            return
        self.start(persistent=run)

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
        waiting capture callback (binding the trigger key), and act on the bound trigger key.
        The trigger's edges are swallowed and counted (`_trig_down` / `_trig_up`); a run of one
        tap is sent back out as the key itself, so Play/Pause still plays and pauses. Everything
        else - and murmur's own re-sent key - passes through untouched."""
        vk = data.vkCode
        if getattr(data, "dwExtraInfo", 0) == PASS_MARK:
            return True
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
            if down and not self.trigger_down:   # holding a key auto-repeats WM_KEYDOWN
                self.trigger_down = True
                self._trig_down()
            elif not down and self.trigger_down:
                self.trigger_down = False
                self._trig_up(vk)
            self.listener.suppress_event()
        return True

    def _trig_down(self) -> None:
        self.trig_gen += 1
        self.trig_held = False
        self.later(HOLD_S, self._trig_hold, self.trig_gen)

    def _trig_hold(self, gen) -> None:
        """Still down after HOLD_S: a real hold - record while held (a keyboard key, not a headset)."""
        if gen != self.trig_gen or not self.trigger_down:
            return
        self.trig_held, self.trig_taps = True, 0
        if not self.recording:
            self.start(persistent=False)

    def _trig_up(self, vk) -> None:
        self.trig_gen += 1
        if self.trig_held:
            self.trig_held = False
            if not self.persistent:
                self.stop()
            return
        self.trig_taps += 1
        self.later(DOUBLE_TAP_S, self._trig_resolve, self.trig_gen, vk)

    def _trig_resolve(self, gen, vk) -> None:
        """No further tap within DOUBLE_TAP_S: act on the run. The wait is the price of letting
        a single tap stay the key's own - it reaches the media player 0.4 s late."""
        if gen != self.trig_gen:
            return
        n, self.trig_taps = self.trig_taps, 0
        if n >= 3:
            self.on_open()
        elif n == 2:
            self.stop() if self.recording else self.start(persistent=True)
        elif self.recording:
            self.stop()                          # a tap while a take runs ends it
        else:
            self.send_key(vk)

    @staticmethod
    def send_key(vk) -> None:
        """Re-send the trigger key (down + up) marked with PASS_MARK so our filter lets it by."""
        try:
            u = ctypes.windll.user32
            u.keybd_event(vk, 0, 0x1, PASS_MARK)          # KEYEVENTF_EXTENDEDKEY
            u.keybd_event(vk, 0, 0x1 | 0x2, PASS_MARK)    # ... | KEYEVENTF_KEYUP
        except Exception as e:
            log(f"  trigger pass-through failed: {e}")

    # --- transcription ------------------------------------------------------
    def _segments(self, audio: np.ndarray, prev: str = "") -> list:
        # Whisper copies the prompt's punctuation style: a comma list with no full stop made every
        # window come out unpunctuated and lower-case, so the vocab list is closed with a period.
        # The context is passed exactly as committed: Whisper ends every sentence it finishes
        # with punctuation, so a bare word at the end means mid-sentence, and closing it made the
        # next window start a new sentence ("Docker runs The Kubernetes tests").
        # A committed tail that is itself a loop ("uh, uh, uh, ...") is never fed back: as the next
        # window's prompt it pulled that window into the same loop (measured: a looping tail gave
        # "uh, uh, uh, uh" where a clean one gave none) - the cascade the user's log showed.
        tail = prev[-200:].strip()
        if tail and compression_ratio(tail) > LOOP_CR:
            tail = ""
        vocab_prompt = (self.vocab + ".") if self.vocab else None
        prompt = " ".join(p for p in (vocab_prompt, tail) if p) or None
        # beam 5 (Whisper's classic). beam_size 1 in config is 1.6x faster and scored the same on
        # TTS'd technical text, but real speech is noisier and the user rates accuracy first.
        # temperature=0: no retry ladder. Whisper's default re-decodes a piece at up to five
        # temperatures x five samples when its log-prob dips under -1.0, and keeps the *sampled*
        # result: an ordinary sentence cost 20.7 s instead of 3 s and came back as "py installer",
        # "inno setup". One deterministic decode is both faster and truer to what was said.
        with boosted():
            segs = self._decode(audio, prompt)
            worst = max((s.compression_ratio for s in segs), default=0.0)
            if worst > LOOP_CR:
                # a repetition loop (a filler then a long pause does it): decode the same audio once
                # more without the committed context and with 3-grams unable to repeat, and keep
                # whichever came back less repetitive. Decode-side only - no text is ever edited.
                again = self._decode(audio, vocab_prompt, no_repeat_ngram_size=3)
                better = max((s.compression_ratio for s in again), default=0.0)
                log(f"  repetition loop: compression {worst:.2f}, re-decoded {better:.2f}")
                if better < worst:
                    segs = again
        low = [s for s in segs if s.avg_logprob < -1.0 or s.compression_ratio > LOOP_CR]
        if low:   # diagnostics only, never acted on: the text is whatever Whisper heard
            log(f"  low confidence: logprob {min(s.avg_logprob for s in low):.2f}, "
                f"compression {max(s.compression_ratio for s in low):.2f}")
        return segs

    def _decode(self, audio: np.ndarray, prompt, **extra) -> list:
        segs, _ = self.model.transcribe(
            audio, language=self.language, beam_size=self.cfg.get("beam_size") or 5,
            initial_prompt=prompt, vad_filter=True, condition_on_previous_text=False,
            temperature=0.0, **extra,
        )
        return list(segs)

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
        self.recorder.release(take.committed)   # this pass is the only writer of `committed`
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
                tail = self.transcribe(audio[take.committed - take.base:], " ".join(take.parts))
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
        log(f"ready: hold Ctrl+Win and talk; double-tap for persistent, triple-tap for the window.  {since_launch()}")
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
    import vault
    promptify.log = log               # engine diagnostics (never its text) into murmur.log
    vault.log = log

    scale = set_dpi_aware()           # before Tk() so tkinter gets real pixels too
    root = tk.Tk()
    root.withdraw()
    install_crash_log(root)           # Tk swallows callback errors into a stderr this build lacks
    root.tk.call("tk", "scaling", scale * 96 / 72)
    holder = {"app": None}
    history = History(APPDIR / "history.jsonl", cfg["retention_days"])
    overlay = Overlay(lambda: holder["app"].recorder.samples() if holder["app"] else None, scale)
    overlay.configure(cfg)
    # the vault index refreshes itself on a daemon thread, well after startup is done
    root.after(60000, lambda: vault.refresh_if_stale(cfg, APPDIR))

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
        holder["app"].on_open = open_window
        holder["app"].run()
        # build the window now, hidden, so the first triple-tap costs a deiconify (measured
        # 1.4 s for the build on this laptop)
        root.after(1500, lambda: (win.prebuild(), log(f"window prebuilt  {since_launch()}")))

    def tick() -> None:
        try:
            overlay.tick()
        except Exception as e:   # a draw bug must not stop the timer (the overlay would freeze)
            log(f"overlay: {e}")
        root.after(FRAME_MS, tick)

    watch_open_requests(open_window)   # a second launch raises this window instead of starting again
    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=load, daemon=True).start()
    log(f"ui up  {since_launch()}")
    root.after(FRAME_MS, tick)
    try:
        root.mainloop()
    finally:
        overlay.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="murmur", description=__doc__.split("\n")[0])
    from window import MODELS
    p.add_argument("--model", choices=MODELS, help="faster-whisper model (tiny.en, base.en, small.en; or tiny/base/small for other languages)")
    p.add_argument("--device", choices=["cpu", "cuda"])
    p.add_argument("--language", help="force a language code, e.g. en, ko. default: en for *.en models")
    p.add_argument("--mic", help="input device index or name substring (see --list-devices)")
    p.add_argument("--trigger-vk", type=lambda v: int(v, 0), help="virtual-key code bound as the trigger key (e.g. 0xB3 = Play/Pause)")
    p.add_argument("--vocab", type=Path, help="terms file fed to Whisper as a prompt")
    p.add_argument("--config", type=Path, default=APPDIR / "config.json")
    p.add_argument("--console", action="store_true", help="no tray/overlay/window; log to the console")
    p.add_argument("--list-devices", action="store_true", help="print audio devices and exit")
    a = p.parse_args(argv)
    install_crash_log()
    if a.list_devices:
        print(audio().query_devices())
        return 0
    if not claim_instance():
        log("murmur is already running; asked it to open its window")
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
