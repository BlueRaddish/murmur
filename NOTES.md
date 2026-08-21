# Working notes

Decisions
- Single file, no package. Clipboard-paste over keystroke typing: instant for long text,
  unicode-safe; clipboard is restored 150 ms after paste.
- No post-processing beyond whitespace. That is the feature.
- Chord = any Ctrl + any Win/Cmd (pynput `Key.cmd` is the Win key on Windows). Edge-triggered:
  the chord fires once when both become held, and "released" once when either goes up.
- Double-tap window 0.4 s, measured from the previous chord *release* to the next press.
- Headset button = VK_MEDIA_PLAY_PAUSE, handled in pynput's win32_event_filter and suppressed
  there so media players don't see it. It calls the same toggle() as the tray menu; it never
  synthesizes Ctrl+Win.
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

- Streaming (v0.4): Take per recording; _stream_pass commits all-but-last segment and advances the
  pointer to the *start* of the last segment (a VAD gap; segment.end lands inside the last word,
  and cutting at the snapshot point split words 3 times out of 4 in review). A take turns
  streaming off once a pass measures slower than realtime. Measured on this
  laptop under heavy load: small.en ~0.4x realtime, base.en ~1.5-2x; 12 threads collapses (227 s)
  so cpu_threads stays default. Streaming only helps when the model is faster than realtime.
- Overlay (v0.4): 48x7 glass stick (_glass builds shadow/glow/body/specular/rim from a mask, cached
  per state). Recording is a spectrum visualizer in the cava/easyeffects mould: Recorder keeps the
  last 2048 samples; _analyse does FFT -> 20 log bands (90 Hz-5.5 kHz) -> log magnitude ->
  auto-gain to a decaying peak -> neighbour spreading (1/1.6^d, "monstercat") -> fast attack /
  slow fall. _spectrum_mask mirrors the bands (lows centre, highs at the tips), smooths, and the
  result is the stick's own silhouette; _fill paints it as one piece with a bright core line,
  glow and rim. Rejected on the way: bars inside the stick, swelling level blob, separated bars,
  sine strands (read as "blob + separate waves"). Idle never glows (no white flash on fade).
  ~18 ms/frame. Asymmetric on purpose (lows left / highs right; bottom half = spectrum rolled 2
  bands and x0.78). Release: anim eases at 0.06 and bands fall at 0.955 once idle, and the spectrum
  shape keeps drawing (cross-faded to the resting stick) until flat. Colours come from config
  (`color`, `color_busy`) via Overlay.set_colors; persistent mode shares the recording colour - the
  old amber read as "orange while recording" to the user.

Environment facts (this laptop, 2026-08-20)
- Apple Audio driver: the *Internal Digital Microphone* device returns junk (slow 0-0.25
  ramps). The *Headset Microphone* (`--mic 2`) carries real audio. WASAPI endpoints refuse
  16 kHz, hence the native-rate fallback + resample.
- A running Wispr holds the mic; close it before testing.

Verified
- tests/test_murmur.py (state machine + real tiny.en transcription of TTS audio).
- Script and frozen exe both driven live with injected key events: pill shows recording /
  persistent with level bar, tray icon present, log written to %APPDATA%\murmur.
- Human-voice run of the script confirmed by the user 2026-08-20.

Not done
- Headset button not tested with real hardware (no wired headset button to press here);
  tested by feeding VK_MEDIA_PLAY_PAUSE through the filter.
- No GPU here; cuda path untested. No code signing: SmartScreen will warn on the installer.
