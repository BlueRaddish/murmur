"""murmur - hold Ctrl+Win, talk, release. Local Whisper types what you said.

No cloud, no LLM rewriting. Technical terms are protected by vocab.txt,
which is fed to Whisper as a prompt so it prefers those spellings.

Modes
  hold        hold Ctrl+Win, speak, release -> typed.
  persistent  double-tap Ctrl+Win -> keeps recording until Ctrl+Win is pressed again.
  headset     (opt-in) the wired-headset button toggles recording. It is its own trigger,
              never translated into Ctrl+Win, so no stray modifier keystrokes reach apps.

A glassy disc at the bottom of the screen shows the mode and live mic level, so you can
see it is actually hearing you. The tray's "Open murmur" window keeps a history of
everything transcribed (default 7 days, adjustable) in case a paste goes missing.
"""
import argparse
import json
import os
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

DEFAULTS = {"model": "small.en", "device": "cpu", "language": None, "mic": None, "headset_button": False,
            "retention_days": 7, "color": "#e63c3c", "color_busy": "#ffaa32", "opacity": 0.9}


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
    """Chunks are stored already at 16 kHz so snapshot() during recording is a concatenate."""

    def __init__(self, device=None):
        self.device = device
        self._chunks: list = []
        self._lock = threading.Lock()
        self._stream = None
        self._rate = SAMPLE_RATE
        self.level = 0.0  # RMS of the latest chunk, 0..1
        self.recent = np.zeros(2048, dtype=np.float32)  # last 128 ms at 16 kHz, for the visualizer

    def _cb(self, data, *_):
        mono = resample(data[:, 0].copy(), self._rate, SAMPLE_RATE)
        with self._lock:
            self._chunks.append(mono)
        self.level = float(np.sqrt((data ** 2).mean()))
        n = min(len(mono), len(self.recent))
        self.recent = np.concatenate([self.recent[n:], mono[-n:]])

    def samples(self) -> np.ndarray:
        return self.recent

    def snapshot(self) -> np.ndarray:
        with self._lock:
            chunks = list(self._chunks)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def start(self) -> None:
        with self._lock:
            self._chunks = []
        self.level = 0.0
        try:
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                          device=self.device, callback=self._cb)
            self._rate = SAMPLE_RATE
        except sd.PortAudioError:  # device refuses 16k (WASAPI does): use native rate, resample later
            self._rate = int(sd.query_devices(self.device, "input")["default_samplerate"])
            self._stream = sd.InputStream(samplerate=self._rate, channels=1, dtype="float32",
                                          device=self.device, callback=self._cb)
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


class Take:
    """One recording's streaming state: text committed so far and how many samples it covers."""

    def __init__(self):
        self.active = True
        self.parts: list = []
        self.committed = 0
        self.done = threading.Event()


class Murmur:
    """States: idle, recording (hold), persistent, busy (transcribing).
    Every transition goes through start()/stop() so UI and log stay in step."""

    def __init__(self, cfg: dict, vocab: str, on_state=None, on_text=None):
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

    def _set(self, state: str) -> None:
        self.state = state
        self.on_state(state)

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
        """Headset button: act on key-down, swallow both down and up so the media
        player never sees it. Only when the option is on; otherwise untouched."""
        if self.cfg["headset_button"] and data.vkCode == VK_MEDIA_PLAY_PAUSE:
            if msg in (0x100, 0x104):  # WM_KEYDOWN, WM_SYSKEYDOWN
                self.toggle()
            self.listener.suppress_event()
        return True

    # --- transcription ------------------------------------------------------
    def _segments(self, audio: np.ndarray, prev: str = "") -> list:
        prompt = ", ".join(p for p in (self.vocab, prev[-200:]) if p) or None
        segs, _ = self.model.transcribe(
            audio, language=self.language, beam_size=5, initial_prompt=prompt,
            vad_filter=True, condition_on_previous_text=False,
        )
        return list(segs)

    def transcribe(self, audio: np.ndarray, prev: str = "") -> str:
        if len(audio) < SAMPLE_RATE * 0.3:  # under 300ms: a tap, not speech
            return ""
        return clean(" ".join(s.text for s in self._segments(audio, prev)))

    STREAM_EVERY = 1.5   # seconds between background passes
    STREAM_MIN = 2.5     # seconds of uncommitted audio before a pass is worth it

    def _stream_loop(self, take: "Take") -> None:
        """Runs while a take is recording. Passes overlap with speech, so release leaves
        only a short tail to transcribe."""
        last = time.monotonic()
        while take.active:
            time.sleep(0.1)
            if time.monotonic() - last < self.STREAM_EVERY:
                continue
            try:
                t0 = time.monotonic()
                covered = self._stream_pass(take)
                if not covered:
                    continue
                if time.monotonic() - t0 > covered / SAMPLE_RATE:
                    # slower than realtime: further passes would only delay the final tail.
                    # Per-take ceiling; upgrade path is a measured speed factor kept in config.
                    log("  stream: model slower than realtime, streaming off for this take")
                    break
            except Exception as e:
                log(f"  stream: {e}")
                break
            last = time.monotonic()
        take.done.set()

    def _stream_pass(self, take: "Take") -> int:
        """Transcribe the uncommitted audio; commit every segment except the last (which may
        still be mid-sentence) and advance to where that last segment *starts* - a VAD gap,
        never the inside of a word (segment.end is too coarse to cut on). Returns the number
        of samples the pass covered, 0 if there was nothing to do."""
        audio = self.recorder.snapshot()[take.committed:]
        if len(audio) < SAMPLE_RATE * self.STREAM_MIN:
            return 0
        with self.lock:
            if not take.active:
                return 0
            segs = self._segments(audio, " ".join(take.parts))
        if len(segs) >= 2:
            take.parts.append(clean(" ".join(s.text for s in segs[:-1])))
            take.committed += min(int(segs[-1].start * SAMPLE_RATE), len(audio))
        return len(audio)

    def handle(self, audio: np.ndarray, take: "Take" = None) -> None:
        if take is None:
            take = Take()
            take.done.set()
        take.done.wait()      # an in-flight pass may still be committing
        with self.lock:  # one transcription at a time
            try:
                t0 = time.time()
                tail = self.transcribe(audio[take.committed:], " ".join(take.parts))
                text = clean(" ".join(p for p in take.parts + [tail] if p))
                if not text:
                    log("  (nothing heard)")
                    return
                log(f"  {text}  [{time.time() - t0:.1f}s]")
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
        log("ready: hold Ctrl+Win and talk; double-tap for persistent mode.")
        self._set("idle")
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release,
                                          win32_event_filter=self.win32_event_filter)
        self.listener.start()

    def quit(self) -> None:
        if self.listener:
            self.listener.stop()


# --- UI: tray icon, overlay, app window -------------------------------------------
COLORS = {"idle": "#787878", "recording": "#dc3232", "persistent": "#f09620", "busy": "#3c82dc",
          "loading": "#3c82dc"}
LABELS = {"idle": "ready (Ctrl+Win)", "recording": "recording", "persistent": "persistent, Ctrl+Win stops",
          "busy": "transcribing...", "loading": "loading model..."}


def make_icon(state: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((8, 8, 56, 56), fill=COLORS[state])
    return img


def run_app(factory, cfg: dict, cfg_path: Path) -> None:
    """Main thread: Tk root (hidden) drives the overlay timer and owns the app window.
    Background threads: tray icon, model load. The model loads after the UI is up."""
    import tkinter as tk
    import pystray
    from overlay import Overlay, set_dpi_aware
    from window import AppWindow, History

    scale = set_dpi_aware()           # before Tk() so tkinter gets real pixels too
    root = tk.Tk()
    root.withdraw()
    root.tk.call("tk", "scaling", scale * 96 / 72)
    holder = {"app": None}
    history = History(APPDIR / "history.jsonl", cfg["retention_days"])
    overlay = Overlay(lambda: holder["app"].recorder.samples() if holder["app"] else None, scale)
    overlay.set_colors(cfg)

    def on_save(c: dict) -> None:
        save_config(cfg_path, c)
        overlay.set_colors(c)

    win = AppWindow(root, history, cfg, on_save, scale)
    icon = pystray.Icon("murmur", make_icon("loading"), "murmur: " + LABELS["loading"])

    def on_state(state: str) -> None:
        icon.icon = make_icon(state)
        icon.title = "murmur: " + LABELS[state]
        overlay.post(state)

    def on_text(text: str) -> None:
        if cfg["retention_days"] > 0:   # marshal to the Tk thread: History is not locked
            root.after(0, lambda: (history.append(text), win.refresh()))

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
    p.add_argument("--headset-button", action="store_true", help="wired-headset button toggles recording")
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
    run_app(factory, cfg, a.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
