# murmur

Hold **Ctrl+Win**, talk, let go. What you said gets typed into whatever window has focus.

**Completely local.** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) runs
on your own CPU; your voice never leaves the machine. And **no language model "cleans up"
your words** - `rebase`, `tmux`, `GDScript` come out as you said them. A `vocab.txt` of
your own terms is handed to Whisper as a prompt so it prefers those spellings.

## Everything stays on this machine

Cloud dictation tools stream your microphone to a server and hand the transcript to an LLM
before it reaches you. murmur has no server side at all:

- **Audio** is recorded into memory, transcribed by Whisper on this computer, and discarded.
  No account, no API key, no telemetry.
- **Network** is used exactly once: the first run downloads the model (~480 MB) from Hugging
  Face. After that murmur checks the local copy first and never goes online again - it works
  with Wi-Fi off.
- **Text** goes to your clipboard and, if you keep history on, to
  `%APPDATA%\murmur\history.jsonl` - a plain file you can read, prune (Settings > retention,
  0 keeps nothing) or delete.
- **Nothing rewrites you.** What Whisper hears is what gets typed, technical terms included.

## Install

Grab `murmur-setup.exe` from the [Releases](https://github.com/BlueRaddish/murmur/releases)
page. Per-user install, no admin; tick "Start murmur when I sign in". It lives in the tray.

Or from source:

```
pip install -r requirements.txt
python murmur.py
```

First run downloads the model (`small.en`, ~480 MB) - the bar pulses until the model is ready.

## Use

| Action | What happens |
|---|---|
| Hold Ctrl+Win, speak, release | text is typed where the cursor is |
| Double-tap Ctrl+Win | **persistent mode**: keeps recording until you press Ctrl+Win again |
| Extra trigger key (opt-in) | toggles recording, same as persistent mode |
| Tray icon right-click | start/stop, headset option, edit vocab, open log, quit |

(Ctrl+Cmd on a Mac keyboard.) A tiny glass stick sits at the bottom of the screen:
near-invisible at rest. While you talk it turns red and becomes a live spectrum of your
voice - one glowing shape that swells and ripples across its whole length and pinches
back into the stick at the tips, then relaxes slowly when you stop; it auto-scales to your
mic. Colours, opacity and haze are yours: Settings has an accent colour (recording) and a
transcribing colour with preset swatches, an opacity slider and a haze toggle; Save applies
them live. An orange-to-yellow pulse runs along it while
transcribing. If it stays a flat red line while you talk, it is listening to the wrong
input: pick the microphone in Settings.

**Latency.** Transcription runs while you are still talking, phrase by phrase: every time
you pause for most of a second the phrase you just said is transcribed and committed, and
run-on speech is taken in 6-20 s pieces at the sentence boundaries Whisper itself finds. On
release only the piece in flight and what you said since your last pause are left, so a
two-minute dictation lands in a few seconds instead of half a minute. Pieces are never
smaller than a phrase on purpose: each Whisper call costs about the same (~1 s on an idle
CPU, 4-5 s on a busy one) whether it gets 3 s or 15 s of audio, and fragments without
context are misheard. While it works murmur raises its own process priority a notch, which
on a busy machine halves the time. `beam_size` in `config.json` is 1 (greedy); 5 is
Whisper's classic setting and 1.6x slower for no measurable gain on technical dictation.
`base.en` is another 2x faster than `small.en` but mangles technical terms; switch in
Settings if you prefer speed.

**History.** Double-click the tray icon (or "Open murmur") for a window with everything
transcribed in the last 7 days - copy it back if a paste went missing or you overwrote the
clipboard. The retention window is adjustable in Settings (0 keeps nothing). Stored in
`%APPDATA%\murmur\history.jsonl`, local only.

**Extra trigger key.** Settings > "Press a key..." binds any single key to toggle
recording: a wired headset's inline button, a media key, F13 on a macro pad. murmur swallows
that key so nothing else reacts to it, and it is never turned into a Ctrl+Win keystroke, so
nothing extra reaches the app you're typing into. Different headsets send different codes;
the capture records whatever yours sends (the log shows the code).

## Options

Settings tab in the app window, or `%APPDATA%\murmur\config.json`; command-line flags override
for one run.

```
--model tiny.en|base.en|small.en|medium.en|large-v3   default small.en
--device cpu|cuda                                     cuda needs an NVIDIA GPU + CUDA libs
--language en|ko|...                                  default: en for *.en models, else auto
--mic 2  or  --mic "Headset"                          pick an input; see --list-devices
--trigger-vk 0xB3                                     key code that toggles recording (0xB3 = Play/Pause)
--console                                             no tray/pill, log to the terminal
```

`tiny.en` is near-instant on CPU and fine for short phrases. `small.en` is noticeably more
accurate and still ~1-2 s for a sentence on a laptop CPU. Multilingual dictation: use a
model without `.en` (`small`, `medium`, `large-v3`).

## vocab.txt

`%APPDATA%\murmur\vocab.txt` (tray menu > Edit vocab.txt). One term or comma-separated
group per line, `#` for comments. Add whatever Whisper keeps mangling. Too long a list
dilutes itself; a few dozen terms is the sweet spot. Restart murmur after editing.

## Build

```
pip install -r requirements.txt pyinstaller
winget install JRSoftware.InnoSetup      # optional, for the installer
.\build.ps1                              # dist\murmur\murmur.exe and dist\murmur-setup.exe
```

## How it works

`pynput` listens for the chord globally. While held, `sounddevice` records the mic at
16 kHz and a background thread keeps transcribing with faster-whisper (int8 on CPU): a piece
closes when the last 0.8 s are silent (Silero VAD) or when 6 s of run-on speech have piled
up, in which case every segment but the last is committed and the pointer moves to where that
last segment starts. On release the remaining tail is transcribed, the committed text is
joined in front, the whole thing is placed on the clipboard and Ctrl+V is sent; the text
stays on the clipboard afterwards. The model is loaded from the local cache without asking
Hugging Face first, and CTranslate2's OpenMP threads are told not to spin-wait
(`KMP_BLOCKTIME=0`), which matters on a busy CPU. `overlay.py` renders the stick with PIL into
a per-pixel-alpha layered window (crisp at any DPI, click-through);
`window.py` is the tkinter history/settings window; the tray icon is pystray.

## Test

```
python tests/test_murmur.py
```

Drives the hold/double-tap/headset state machine with fake key events, then synthesizes a
sentence with Windows TTS and checks Whisper gets the technical words back.
