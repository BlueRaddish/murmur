# Working notes

Decisions
- Single file, no package. Clipboard-paste over keystroke typing: instant for long text,
  unicode-safe; the text stays on the clipboard (restoring the old clipboard raced the paste).
- No post-processing beyond whitespace. That is the feature.
- Chord = any Ctrl + any Win/Cmd (pynput `Key.cmd` is the Win key on Windows). Edge-triggered:
  the chord fires once when both become held, and "released" once when either goes up.
- Double-tap window 0.4 s, measured from the previous chord *release* to the next press.
- Trigger key (v0.5, replaces the fixed headset button): cfg trigger_vk, bound by capture - the
  window sets Murmur.capture, win32_event_filter reports the next key-down's vkCode once and
  swallows it. Verified through the real hook with injected Play/Pause (0xB3) and F13 (0x7C).
  Bound key toggles on key-down and is suppressed both ways; never synthesizes Ctrl+Win. Old
  headset_button configs migrate to trigger_vk=0xB3.
- Overlay is a pure Win32 layered window (UpdateLayeredWindow, premultiplied BGRA from PIL via
  numpy), ticked from the hidden Tk root's after() loop. tkinter's canvas was the pixelation:
  not DPI-aware and no anti-aliasing. Static layers (glow/shadow/disc) are cached per state;
  only bars/spinner draw per frame (~45 ms/frame at 200% DPI, idle does not redraw).
- SetProcessDpiAwareness(2) runs before Tk() so the window is crisp too; ttk row height is set
  by hand because it does not follow DPI.
- History: %APPDATA%\murmur\history.jsonl, pruned on load and append; retention_days in config; pystray runs in a thread; the model loads in a third so
  the UI is up immediately and shows "loading model...".
- Console output is ASCII only: the Windows console is cp1252 and a non-ASCII print inside a
  pynput callback kills the listener.
- PyInstaller 6 puts --add-data files in _internal/; resources resolve via sys._MEIPASS.
- build.ps1 must not use $ErrorActionPreference=Stop: PS 5.1 turns native stderr into errors.

- Streaming (v0.4, superseded by v0.6 below): Take per recording; _stream_pass committed
  all-but-last segment and advanced the pointer to the *start* of the last segment (segment.end
  lands inside the last word, and cutting at the snapshot point split words 3 times out of 4 in
  review). It switched streaming off once a pass measured slower than realtime - which, judged
  on the cold first pass, was every take on this laptop. 12 threads collapsed (227 s), so
  cpu_threads stays default.
- Streaming (v0.6): windows of 6-30 s of new audio, every segment but the last committed, pointer
  to that last segment's *start*; a single-segment window stalls for 3 s of new audio instead of
  re-transcribing the same audio at once; a full 30 s single-segment window is committed whole.
  Background passes yield while a released take is being transcribed (pending > 0).
  Recorder.snapshot(start) copies only the chunks past `start`. Never switched off. Windows are
  capped at 20 s (30 s only for a single-segment window) to bound the in-flight wait at release
  on a starved CPU. Review of the rollback added: a boundary under 1.5 s into the window is a
  stray token, not a cut (a 20 ms "..." segment would otherwise advance the pointer 20 ms and
  re-decode the same audio every pass, appending "..." each time) - it stalls like a
  single-segment window; and an empty window result holds back 1.5 s, not 0.5 s, because
  faster-whisper also returns nothing for a word fragment its no-speech gate refused.
  **Rejected (pre-release, on the user's real dictation): "phrase pieces"** - cutting a piece
  whenever the last 0.8 s were silent (Silero, after >= 3 s) and ending run-on windows at a
  0.3 s gap. On TTS it measured 5.6-7.8 s / WER 0.014-0.034; on real speech it produced
  "Go ahead and... On my status line, I want you to also... Setup. The context window...
  Obsession." - the speaker's thinking pauses and unvoiced stretches read as silence, pieces
  lost their continuation, and Whisper both trailed off ("...") and misheard the fragments.
  Rule: cut only where Whisper itself ends a sentence, with the continuation in view. TTS has
  no thinking pauses; it is not a proxy for that failure. beam_size default went back to 5
  for the same reason (greedy matched it on TTS only). `streaming: false` in config = whole
  take at release. The prompt is written as sentences (vocab + "." + context + "."): Whisper
  copies the prompt's punctuation style, and the comma-list prompt made every piece come out
  unpunctuated and lower-case; the committed context is passed as is (closing a bare-word
  ending with a period made the next window start a new sentence: "Docker runs The Kubernetes").
  Sim (61 s TTS spoken in realtime into a fake recorder, real small.en, beam 5, busy CPU;
  sim_stream.py in the session scratchpad), rolled-back code: text 8.8-9.2 s after release at
  WER 0.014-0.020, vs 19.6-22.3 s / 0.041 for the same audio in one call. (The rejected
  phrase-pieces variant had measured 5.6-7.8 s / 0.014-0.034 on the same TTS - faster, and
  useless on real speech.)
- Overlay (v0.4): 48x7 glass stick (_glass builds shadow/glow/body/specular/rim from a mask, cached
  per state). Recording is a spectrum visualizer in the cava/easyeffects mould: Recorder keeps the
  last 2048 samples; _analyse does FFT -> 20 log bands (90 Hz-5.5 kHz) -> log magnitude ->
  auto-gain to a decaying peak -> neighbour spreading (1/1.6^d, "monstercat") -> fast attack /
  slow fall. _spectrum_mask mirrors the bands (lows centre, highs at the tips), smooths, and the
  result is the stick's own silhouette; _fill paints it as one piece with a bright core line,
  glow and rim. Rejected on the way: bars inside the stick, swelling level blob, separated bars,
  sine strands (read as "blob + separate waves"). Idle never glows (no white flash on fade).
  ~20 ms/frame. Shape = 14 fixed-pitch bars fused (104% duty, light corner rounding) so only the
  outer outline exists; the 72%-duty version read as "space between the bars". `haze` adds a
  wide (11-12 px logical) low-alpha glow in every state; `opacity` now scales frost, base, glow,
  rim, specular and pulse alike. Colour presets in Settings (white + 8 hues); white works as an
  accent. Earlier: 14 bars rising from the container, union
  with the stick, blurred 0.5px then glowed; the continuous silhouette read as "bars too small".
  Asymmetry = per side, three wandering lobe centres (the spectrum read by distance
  from each axis, soft-OR'd) x a smooth random field (4 sines, random phases, slow drift); a single
  centred mirror read as symmetric and a lows-left layout as lopsided; the lows-left version read as "too focused to the left". Record -> busy is
  one morph: the shape keeps relaxing while _live_color crossfades accent -> busy (busymix eased)
  and the pulse fades in on top; accent breathes slowly while recording. Resting glass is frosted:
  whitish body with seeded grain, weaker rim. `opacity` (0.2-1) from config scales body/glow/pulse. Release: anim eases at 0.06 and bands fall at 0.955 once idle, and the spectrum
  shape keeps drawing (cross-faded to the resting stick) until flat. Colours come from config
  (`color`, `color_busy`) via Overlay.set_colors; persistent mode shares the recording colour - the
  old amber read as "orange while recording" to the user.

- Performance (v0.6, measured 2026-08-24 on the i7-9750H with the CPU ~95% busy from other apps,
  interleaved A/B, min of rounds):
  - Every Whisper call has a fixed cost (the encoder always sees a padded 30 s window): 3 s of
    audio 4.5 s, 6 s 4.9 s, 15 s 5.6 s (small.en, greedy, above-normal priority). So windows are
    6-30 s of new audio cut only at Whisper's own segment boundaries, never word-sized: word-sized
    would be ~200 encoder passes for 200 words, and context-free fragments hallucinate (and, as
    the rejected phrase-pieces variant showed, so do pause-cut fragments on real speech).
  - Every take in the user's log had hit "stream: model slower than realtime, streaming off":
    the old rule judged the cold first pass and then transcribed the whole take at release
    (130 s -> 36 s). Gone: a slow model just gets bigger pieces.
  - Priority class while transcribing (measured with KMP_BLOCKTIME=0 already set): normal
    15.9 s, above-normal 8.2 s, high 8.1 s for the same 6 s (`boosted()`). In a separate run
    KMP_BLOCKTIME=0 (Intel OpenMP spin-wait off) took normal priority from 15.6 to 9.9 s and was
    neutral at high; the two runs' "normal" figures differ because the foreign load did. 2 threads
    lose to 4 (the default) everywhere; 12 collapsed before.
  - beam 5 vs 3 vs 1 on 60 s of TTS'd technical text: 26.1 / 25.0 / 21.9 s, WER 0.048 / 0.075
    / 0.054 (differences are capitalisation). beam 5 stays the default (real speech is noisier
    than TTS and the user rates accuracy first); `beam_size: 1` in config is the speed knob.
  - distil-small.en: WER 0.63 and slower (42 s vs 10 s for 15 s) - repetition loops into the
    temperature ladder. Rejected. base.en: 2x faster, WER 0.14-0.16 (Py installer, ino setup,
    Reg X, Stlib) - stays optional.
  - temperature=0 only. On the 60 s text in one call the retry ladder never fired; in the realtime
    sim it did, on an ordinary 6 s piece whose log-prob was -1.09 (threshold -1.0): 20.7 s instead
    of 3 s, and the kept result was the temperature-1.0 *sample* ("py installer", "inno setup",
    lower-case). Dictation wants one deterministic decode; low log-prob / high compression is
    logged as a diagnostic, never acted on.
  - WhisperModel() asked huggingface.co on every start: 13.8 s vs 5.7 s with local_files_only.
    Now local first, download only if missing. Model warm-up run dropped: cold-vs-warm is 0.6 s,
    a 30 s-window pass at load cost 1 s idle / 6-12 s busy; only the VAD session is pre-created.
  - Frozen bundle: IPython/jedi/tornado/zmq/nbformat/jsonschema rode in through
    huggingface_hub._login, tqdm.notebook and tokenizers.tools; excluded. setuptools must stay:
    excluding it kills the exe at start (PyInstaller's pkg_resources hook needs its vendored
    jaraco.text).
  - murmur.log holds timings and word counts only (the installed build never logs the text);
    history.jsonl under the retention setting is the only place text is kept.

Environment facts (this laptop, 2026-08-20)
- Apple Audio driver: the *Internal Digital Microphone* device returns junk (slow 0-0.25
  ramps). The *Headset Microphone* (`--mic 2`) carries real audio. WASAPI endpoints refuse
  16 kHz, hence the native-rate fallback + resample.
- A running Wispr holds the mic; close it before testing.
- **2026-08-20 regression:** a patch inserted foreground_is_ours() into the middle of Typist.type(),
  making the paste unreachable for one build; tests/test_murmur.py now exercises Typist end to end.
  Typist no longer restores the old clipboard (the text stays there as a fallback), waits for
  Ctrl/Win to be physically up before Ctrl+V (else Windows sees Win+V), retries a locked clipboard.
- Typist refuses to paste when murmur's own window is foreground (the text stays in history): the
  user reported "no output" right after saving settings, which fits that; the log now also records
  duration and RMS on "nothing heard" so a dead mic is distinguishable from silence.

Verified
- tests/test_murmur.py (state machine + real tiny.en transcription of TTS audio).
- Script and frozen exe both driven live with injected key events: pill shows recording /
  persistent with level bar, tray icon present, log written to %APPDATA%\murmur.
- Human-voice run of the script confirmed by the user 2026-08-20.

Not done
- Headset button not tested with real hardware (no wired headset button to press here);
  tested by feeding VK_MEDIA_PLAY_PAUSE through the filter.
- No GPU here; cuda path untested. No code signing: SmartScreen will warn on the installer.
