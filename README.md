# murmur

Hold **Ctrl+Win**, talk, let go. What you said gets typed into whatever window has focus.

Local [faster-whisper](https://github.com/SYSTRAN/faster-whisper) does the transcription.
Nothing leaves your machine, nothing is subscription-gated, and **no language model
"cleans up" your words** - `rebase`, `tmux`, `GDScript` come out as you said them. A
`vocab.txt` of your own terms is handed to Whisper as a prompt so it prefers those spellings.

## Install

Grab `murmur-setup.exe` from the [Releases](https://github.com/BlueRaddish/murmur/releases)
page. Per-user install, no admin; tick "Start murmur when I sign in". It lives in the tray.

Or from source:

```
pip install -r requirements.txt
python murmur.py
```

First run downloads the model (`small.en`, ~250 MB) - the pill says "loading model..."
until it is ready.

## Use

| Action | What happens |
|---|---|
| Hold Ctrl+Win, speak, release | text is typed where the cursor is |
| Double-tap Ctrl+Win | **persistent mode**: keeps recording until you press Ctrl+Win again |
| Headset button (opt-in) | toggles recording, same as persistent mode |
| Tray icon right-click | start/stop, headset option, edit vocab, open log, quit |

(Ctrl+Cmd on a Mac keyboard.) A pill at the bottom of the screen shows the mode and a live
mic level while recording, so you can see it is hearing you. Beeps: one high = recording,
two rising = persistent, one low = stopped.

**Headset button.** Wired headsets' inline button reaches Windows as the Play/Pause media
key. With the option on (tray menu or `--headset-button`) murmur treats that key as its own
start/stop trigger and swallows it so your music player doesn't react. It is never turned
into a Ctrl+Win keystroke, so nothing extra reaches the app you're typing into.

## Options

Saved in `%APPDATA%\murmur\config.json` (created on first run); command-line flags override.

```
--model tiny.en|base.en|small.en|medium.en|large-v3   default small.en
--device cpu|cuda                                     cuda needs an NVIDIA GPU + CUDA libs
--language en|ko|...                                  default: en for *.en models, else auto
--mic 2  or  --mic "Headset"                          pick an input; see --list-devices
--headset-button                                      wired-headset button toggles recording
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
16 kHz. On release the audio goes to faster-whisper (int8 on CPU), the text is placed on the
clipboard, Ctrl+V is sent, and your previous clipboard is restored. The pill is a tiny
tkinter window; the tray icon is pystray. One file, `murmur.py`.

## Test

```
python tests/test_murmur.py
```

Drives the hold/double-tap/headset state machine with fake key events, then synthesizes a
sentence with Windows TTS and checks Whisper gets the technical words back.
