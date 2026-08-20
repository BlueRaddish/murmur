"""murmur - hold Ctrl+Win, talk, release. Local Whisper types what you said.

No cloud, no LLM rewriting. Technical terms are protected by vocab.txt,
which is fed to Whisper as a prompt so it prefers those spellings.

Modes
  hold        hold Ctrl+Win, speak, release -> typed.
  persistent  double-tap Ctrl+Win -> keeps recording until Ctrl+Win is pressed again.
  headset     (opt-in) the wired-headset button toggles recording. It is its own trigger,
              never translated into Ctrl+Win, so no stray modifier keystrokes reach apps.

While recording, a small pill at the bottom of the screen shows the mode and a live
mic level, so you can see it is actually hearing you.
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

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

DEFAULTS = {"model": "small.en", "device": "cpu", "language": None, "mic": None, "headset_button": False}


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


def beep(kind: str) -> None:
    """start: one high. stop: one low. persistent: two rising. Feedback only."""
    try:
        import winsound
        for f in {"start": (880,), "stop": (440,), "persistent": (880, 1175)}[kind]:
            winsound.Beep(f, 60)
    except Exception:
        pass


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
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


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear interpolation, no anti-alias filter. Fine for speech at 44.1/48k -> 16k;
    upgrade path is scipy.signal.resample_poly if a device ever needs more."""
    if src == dst or len(audio) == 0:
        return audio
    idx = np.arange(0, len(audio), src / dst)
    return np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)


class Recorder:
    def __init__(self, device=None):
        self.device = device
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._rate = SAMPLE_RATE
        self.level = 0.0  # RMS of the latest chunk, 0..1, for the on-screen meter

    def _cb(self, data, *_):
        self._q.put(data.copy())
        self.level = float(np.sqrt((data ** 2).mean()))

    def start(self) -> None:
        self._q = queue.Queue()
        self.level = 0.0
        try:
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                          device=self.device, callback=self._cb)
            self._rate = SAMPLE_RATE
        except sd.PortAudioError:  # device refuses 16k (WASAPI does): use native rate, resample later
            self._rate = int(sd.query_devices(self.device, "input")["default_samplerate"])
            self._stream = sd.InputStream(samplerate=self._rate, channels=1, dtype="float32",
                                          device=self.device, callback=self._cb)
        self._stream.start()

    def stop(self) -> np.ndarray:
        self._stream.stop()
        self._stream.close()
        self.level = 0.0
        chunks = []
        while not self._q.empty():
            chunks.append(self._q.get())
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return resample(np.concatenate(chunks)[:, 0], self._rate, SAMPLE_RATE)


class Typist:
    """Types text into the focused window via clipboard paste (reliable for unicode
    and instant for long text), then restores whatever was on the clipboard."""

    def __init__(self):
        self.kb = Controller()

    def type(self, text: str) -> None:
        old = None
        try:
            old = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text)
        with self.kb.pressed(Key.ctrl):
            self.kb.press("v")
            self.kb.release("v")
        time.sleep(0.15)  # let the target app read the clipboard before we restore it
        if old is not None:
            pyperclip.copy(old)


class Murmur:
    """States: idle, recording (hold), persistent, busy (transcribing).
    Every transition goes through start()/stop() so UI, beeps and log stay in step."""

    def __init__(self, cfg: dict, vocab: str, on_state=None):
        from faster_whisper import WhisperModel
        log(f"loading {cfg['model']} on {cfg['device']}...")
        compute = "int8" if cfg["device"] == "cpu" else "float16"
        self.model = WhisperModel(cfg["model"], device=cfg["device"], compute_type=compute)
        self.cfg = cfg
        self.vocab = vocab
        self.language = cfg["language"] or ("en" if cfg["model"].endswith(".en") else None)
        self.recorder = Recorder(cfg["mic"])
        self.typist = Typist()
        self.on_state = on_state or (lambda s: None)
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

    def _set(self, state: str) -> None:
        self.state = state
        self.on_state(state)

    # --- recording control --------------------------------------------------
    def start(self, persistent: bool) -> None:
        with self.ctl:
            if self.recording:
                if persistent and not self.persistent:  # double-tap while holding: upgrade in place
                    self.persistent = True
                    beep("persistent")
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
            beep("persistent" if persistent else "start")
            log("[persistent]" if persistent else "[rec]")
            self._set("persistent" if persistent else "recording")

    def stop(self) -> None:
        with self.ctl:
            if not self.recording:
                return
            self.recording = self.persistent = False
            audio = self.recorder.stop()
            beep("stop")
            self.pending += 1
            self._set("busy")
        threading.Thread(target=self.handle, args=(audio,), daemon=True).start()

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
        """Headset button: act on key-down, swallow both down and up so the media
        player never sees it. Only when the option is on; otherwise untouched."""
        if self.cfg["headset_button"] and data.vkCode == VK_MEDIA_PLAY_PAUSE:
            if msg in (0x100, 0x104):  # WM_KEYDOWN, WM_SYSKEYDOWN
                self.toggle()
            self.listener.suppress_event()
        return True

    # --- transcription ------------------------------------------------------
    def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < SAMPLE_RATE * 0.3:  # under 300ms: a tap, not speech
            return ""
        segments, _ = self.model.transcribe(
            audio, language=self.language, beam_size=5,
            initial_prompt=self.vocab or None, vad_filter=True,
            condition_on_previous_text=False,
        )
        return clean(" ".join(s.text for s in segments))

    def handle(self, audio: np.ndarray) -> None:
        with self.lock:  # one transcription at a time
            try:
                t0 = time.time()
                text = self.transcribe(audio)
                if not text:
                    log("  (nothing heard)")
                    return
                log(f"  {text}  [{time.time() - t0:.1f}s]")
                self.typist.type(text)
            except Exception as e:
                log(f"  error: {e}")
            finally:
                with self.ctl:
                    self.pending -= 1
                    if not self.recording and self.pending == 0:
                        self._set("idle")

    def run(self) -> None:
        log("ready: hold Ctrl+Win and talk; double-tap for persistent mode.")
        self._set("idle")
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release,
                                          win32_event_filter=self.win32_event_filter)
        self.listener.start()

    def quit(self) -> None:
        if self.listener:
            self.listener.stop()


# --- UI: tray icon + on-screen pill ---------------------------------------------
COLORS = {"idle": "#787878", "recording": "#dc3232", "persistent": "#f09620", "busy": "#3c82dc",
          "loading": "#3c82dc"}
LABELS = {"idle": "ready (Ctrl+Win)", "recording": "recording", "persistent": "persistent, Ctrl+Win stops",
          "busy": "transcribing...", "loading": "loading model..."}


def make_icon(state: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((8, 8, 56, 56), fill=COLORS[state])
    return img


class Pill:
    """Borderless always-on-top strip at the bottom centre of the primary screen.
    Shown while recording/transcribing, hidden when idle. Runs in the main thread
    (tkinter requires it); other threads post states through a queue."""

    W, H = 330, 36

    def __init__(self, get_level):
        import tkinter as tk
        self.tk = tk
        self.get_level = get_level
        self.q: queue.Queue = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True, "-alpha", 0.92)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+{sh - self.H - 80}")
        self.c = tk.Canvas(self.root, width=self.W, height=self.H, bg="#1e1e1e", highlightthickness=0)
        self.c.pack()
        self.dot = self.c.create_oval(12, 11, 26, 25, fill=COLORS["idle"], outline="")
        self.text = self.c.create_text(34, self.H // 2, anchor="w", fill="#f0f0f0",
                                       font=("Segoe UI", 10), text="")
        self.bar_bg = self.c.create_rectangle(self.W - 70, 14, self.W - 12, 22, fill="#3a3a3a", outline="")
        self.bar = self.c.create_rectangle(self.W - 70, 14, self.W - 70, 22, fill="#6fd36f", outline="")
        self.state = "idle"

    def post(self, state: str) -> None:
        self.q.put(state)

    def _tick(self) -> None:
        while not self.q.empty():
            self.state = self.q.get()
            self.c.itemconfig(self.dot, fill=COLORS[self.state])
            self.c.itemconfig(self.text, text=LABELS[self.state])
            if self.state == "idle":
                self.root.withdraw()
            else:
                self.root.deiconify()
                self.root.lift()
        if self.state in ("recording", "persistent"):
            # log-ish scale: speech RMS of 0.03 is clearly audible, 0.2 is loud
            lvl = min(1.0, self.get_level() / 0.2) ** 0.5
            self.c.coords(self.bar, self.W - 70, 14, self.W - 70 + int(58 * lvl), 22)
        self.root.after(50, self._tick)

    def run(self) -> None:
        self.root.after(50, self._tick)
        self.root.mainloop()

    def close(self) -> None:
        self.root.after(0, self.root.destroy)


def run_app(factory, cfg_path: Path) -> None:
    """Main thread: pill + tk loop. Background thread: tray icon. Model loads after both show."""
    import pystray
    holder = {"app": None}
    pill = Pill(lambda: holder["app"].recorder.level if holder["app"] else 0.0)
    icon = pystray.Icon("murmur", make_icon("loading"), "murmur: " + LABELS["loading"])

    def on_state(state: str) -> None:
        icon.icon = make_icon(state)
        icon.title = "murmur: " + LABELS[state]
        pill.post(state)

    def toggle_headset(icon_, item) -> None:
        app = holder["app"]
        if app is None:
            return
        app.cfg["headset_button"] = not app.cfg["headset_button"]
        save_config(cfg_path, app.cfg)
        log(f"headset button {'on' if app.cfg['headset_button'] else 'off'}")

    def quit_all(icon_, item) -> None:
        if holder["app"]:
            holder["app"].quit()
        icon.stop()
        pill.close()

    icon.menu = pystray.Menu(
        pystray.MenuItem("Start/stop recording", lambda i, it: holder["app"] and holder["app"].toggle()),
        pystray.MenuItem("Headset button toggles recording", toggle_headset,
                         checked=lambda it: bool(holder["app"] and holder["app"].cfg["headset_button"])),
        pystray.MenuItem("Edit vocab.txt", lambda i, it: os.startfile(find_vocab())),
        pystray.MenuItem("Open config/log folder", lambda i, it: os.startfile(APPDIR)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_all),
    )

    def load() -> None:
        pill.post("loading")
        try:
            holder["app"] = factory()
        except Exception as e:
            log(f"failed to start: {e}")
            quit_all(None, None)
            return
        holder["app"].on_state = on_state
        holder["app"].run()

    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=load, daemon=True).start()
    pill.run()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="murmur", description=__doc__.split("\n")[0])
    p.add_argument("--model", help="faster-whisper model (tiny.en, base.en, small.en, medium.en, large-v3)")
    p.add_argument("--device", choices=["cpu", "cuda"])
    p.add_argument("--language", help="force a language code, e.g. en, ko. default: en for *.en models")
    p.add_argument("--mic", help="input device index or name substring (see --list-devices)")
    p.add_argument("--headset-button", action="store_true", help="wired-headset button toggles recording")
    p.add_argument("--vocab", type=Path, help="terms file fed to Whisper as a prompt")
    p.add_argument("--config", type=Path, default=APPDIR / "config.json")
    p.add_argument("--console", action="store_true", help="no tray/pill; log to the console")
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
    if a.headset_button:
        cfg["headset_button"] = True
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
    run_app(factory, a.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
