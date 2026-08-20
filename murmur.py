"""murmur — hold Ctrl+Win, talk, release. Local Whisper types what you said.

No cloud, no LLM rewriting. Technical terms are protected by vocab.txt,
which is fed to Whisper as a prompt so it prefers those spellings.
"""
import argparse
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

HERE = Path(__file__).resolve().parent
SAMPLE_RATE = 16000

# Hold both of these to record. Key.cmd is the Win key on Windows, Cmd on macOS.
CTRL_KEYS = {Key.ctrl, Key.ctrl_l, Key.ctrl_r}
CMD_KEYS = {Key.cmd, Key.cmd_l, Key.cmd_r}


def beep(high: bool) -> None:
    try:
        import winsound
        winsound.Beep(880 if high else 440, 60)
    except Exception:  # not Windows, or no sound device — feedback is optional
        pass


def load_vocab(path: Path) -> str:
    """vocab.txt: one term per line, '#' comments. Joined into a Whisper prompt."""
    if not path.exists():
        return ""
    terms = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    return ", ".join(terms)


def clean(text: str) -> str:
    """Whitespace only. Deliberately no rewriting — that is the point of this tool."""
    return " ".join(text.split())


class Recorder:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._stream = None

    def start(self) -> None:
        self._q = queue.Queue()
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            callback=lambda data, *_: self._q.put(data.copy()),
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        self._stream.stop()
        self._stream.close()
        chunks = []
        while not self._q.empty():
            chunks.append(self._q.get())
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[:, 0]


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
    def __init__(self, model_name: str, device: str, vocab: str, language: str | None):
        from faster_whisper import WhisperModel
        print(f"loading {model_name} on {device}...", flush=True)
        compute = "int8" if device == "cpu" else "float16"
        self.model = WhisperModel(model_name, device=device, compute_type=compute)
        self.vocab = vocab
        self.language = language
        self.recorder = Recorder()
        self.typist = Typist()
        self.held: set = set()
        self.recording = False
        self.lock = threading.Lock()

    # --- hotkey -----------------------------------------------------------
    def _chord_down(self) -> bool:
        return bool(self.held & CTRL_KEYS) and bool(self.held & CMD_KEYS)

    def on_press(self, key) -> None:
        self.held.add(key)
        if self._chord_down() and not self.recording:
            self.recording = True
            self.recorder.start()
            beep(True)
            print("[rec]", flush=True)

    def on_release(self, key) -> None:
        self.held.discard(key)
        if self.recording and not self._chord_down():
            self.recording = False
            audio = self.recorder.stop()
            beep(False)
            threading.Thread(target=self.handle, args=(audio,), daemon=True).start()

    # --- transcription ----------------------------------------------------
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
            t0 = time.time()
            text = self.transcribe(audio)
            if not text:
                print("  (nothing heard)", flush=True)
                return
            print(f"  {text}  [{time.time() - t0:.1f}s]", flush=True)
            self.typist.type(text)

    def run(self) -> None:
        print("ready: hold Ctrl+Win and talk; release to type. Ctrl+C to quit.", flush=True)
        with keyboard.Listener(on_press=self.on_press, on_release=self.on_release) as l:
            l.join()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="murmur", description=__doc__.split("\n")[0])
    p.add_argument("--model", default="small.en",
                   help="faster-whisper model (tiny.en, base.en, small.en, medium.en, large-v3). default: small.en")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    p.add_argument("--language", default=None, help="force a language code, e.g. en, ko. default: auto")
    p.add_argument("--vocab", default=HERE / "vocab.txt", type=Path, help="terms file fed to Whisper as a prompt")
    a = p.parse_args(argv)
    if a.model.endswith(".en") and a.language is None:
        a.language = "en"
    try:
        Murmur(a.model, a.device, load_vocab(a.vocab), a.language).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
