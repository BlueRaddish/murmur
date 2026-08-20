# murmur

Hold **Ctrl+Win**, talk, let go. What you said gets typed into whatever window has focus.

Local [faster-whisper](https://github.com/SYSTEMRAN/faster-whisper) does the transcription.
Nothing leaves your machine, nothing is subscription-gated, and **no language model
"cleans up" your words** — `rebase`, `tmux`, `GDScript` come out as you said them. A
`vocab.txt` of your own terms is handed to Whisper as a prompt so it prefers those spellings.

## Install

```
pip install -r requirements.txt
python murmur.py
```

First run downloads the model (`small.en`, ~250 MB). Then: hold Ctrl+Win (Ctrl+Cmd on a
Mac keyboard), speak, release. A high beep means recording, a low beep means it stopped.

## Options

```
--model tiny.en|base.en|small.en|medium.en|large-v3   default small.en
--device cpu|cuda                                     cuda needs an NVIDIA GPU + CUDA libs
--language en|ko|...                                  default: en for *.en models, else auto
--mic 2  or  --mic "Headset"                          pick an input; see --list-devices
--vocab path/to/terms.txt
```

`tiny.en` is near-instant on CPU and fine for short phrases. `small.en` is noticeably more
accurate and still ~1-2 s for a sentence on a laptop CPU. Multilingual dictation: use a
model without `.en` (`small`, `medium`, `large-v3`).

## vocab.txt

One term or comma-separated group per line, `#` for comments. Add whatever Whisper keeps
mangling. Too long a list dilutes itself; a few dozen terms is the sweet spot.

## Run at login

`murmur.bat` starts it minimized. Put a shortcut to it in
`shell:startup` (Win+R, type that, Enter).

## How it works

`pynput` listens for the chord globally. While held, `sounddevice` records the mic at
16 kHz. On release the audio goes to faster-whisper (int8 on CPU), the text is placed on the
clipboard, Ctrl+V is sent, and your previous clipboard is restored. ~150 lines, see
`murmur.py`.

## Test

```
python tests/test_murmur.py
```

Synthesizes a sentence with Windows TTS and checks Whisper gets the technical words back.
