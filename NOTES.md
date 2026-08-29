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
- Overlay fade rewrite + light indicator (2026-08-26). The busy->idle fade "stopped glowing,
  left an outline for a few seconds, then showed a bit of green". Three causes, one each:
  three independent per-frame exponentials (anim 0.06 ~3 s, bands 0.955 ~4 s, busymix ~1 s)
  that stall into a long dim tail and stutter when the CPU drops frames; _fill scaled glow and
  rim by `active` but not the body, so the glow died first and left a flat body with a faint
  rim; and busymix kept decaying while idle, crossfading the colour back to the green accent
  during the fade-out (plus Image.blend mixing straight-alpha RGBA, and _render falling from
  _fill to _base once the bands relaxed - a renderer swap mid-fade). Now: one time-based level
  (clock attribute, 180 ms ease-out in / 650 ms smoothstep out, new segment from wherever the
  level is), colour/pulse/breath frozen while idle (busymix and bands reset when it reaches 0),
  every non-idle state rendered through _fill, and one premultiplied _dissolve(idle, active,
  level) so glow, haze, body, rim and pulse fade together. Gone by ~0.7 s, no tail.
- `indicator` in config ("waves" | "light", Overlay.set_colors is now configure()): light keeps
  the resting frosted stick and puts an LED behind it - disc 0.8*H with a lighter core, halo
  (1.8*H, blur 3*S, a 0.7) and bloom (blur 7*S, a 0.35) past the bar, brightness
  0.55+0.45*breath (2.2 s, 0.9 s while transcribing) lifted 0.35*mic level (RMS auto-gained,
  fast attack / slow release). No spectrum analysed in light style, no sweep pulse; same
  colours, same dissolve. Sheets: scratchpad ui/fade_waves.png, light_breath.png.
- App window redesign (2026-08-26). The user: "a lot better UI design on the settings and the
  history pop up... a lot more senior, a lot more modern". Built to the UI design method (PARA
  3-Resources/ui-design-method): brief -> tokens -> grey -> colour -> review against screenshots.
  Tokens: a 12-step neutral ramp built in OKLCH from the *accent hue* (config `color`) at 2-6 %
  chroma, accent at step 9, focus ring at 8 with real chroma, muted text at 11 de-chromed to
  .035 (the first cut used the table's .14 and the whole light theme read green); light and dark
  from the Windows AppsUseLightTheme key, dark title bar via DwmSetWindowAttribute(20). Type
  Segoe UI 400 / Segoe UI Semibold 600 at 12/10/9 pt, Cascadia Mono for hex and the key chip.
  Depth = hairlines + surface ladder, radius 0 (Tk cannot round corners; the toggle, colour dots
  and slider knob are PIL-rendered PNGs via tk.PhotoImage(data=...), no ImageTk). Sidebar
  (History / Settings, footer links to vocab.txt and the config folder) + content. History rows on
  a Canvas (time meta + ellipsised text, hover/selected, Up/Down/Return/Ctrl+C/Delete), a detail
  panel sized to its text and capped near 80 ch, Copy the one primary action, Delete with an 8 s
  Undo, Clear all confirmed inline. Settings autosave on every change (no Save button); rows are
  label + description left, control right in a fixed column; restart-needed rows grow a "restart
  to apply" chip. The review (design + behaviour + code) caught: muted at accent chroma, the
  trigger-key row 16 px short of the right edge, a 2.8:1 focus ring, Tab wandering into the hidden
  view, a wheel binding leaking through hide(), the detail measure running to ~200 ch maximised,
  a centred empty state, three controls with no keyboard path - all fixed; tests/test_window.py
  holds the contrast, chroma-budget, one-right-edge, focus-chain and wheel-leak asserts.
- Identity, the Ripple m (2026-08-27, v0.7). murmur had no mark - a grey disc in the tray and a
  plain "murmur" Label. The panel picked the m over a waveform or a mic: it is the product's own
  first letter, and its two arches already *are* the thing (a sound going out) without borrowing
  hardware imagery that says "recording device" when the point is that nothing is recorded
  anywhere. Level build over rising because the tray needs the mark to sit square in a 16 px box.
  Geometry (64 grid, "Ripple m, level"): two half-annuli on the baseline y=52, arch 1 centre
  (20,24) radii 4/12, arch 2 centre (40,28) radii 8/16, three 8-wide legs at x 8/24/48 - the
  small arch's right leg and the big arch's left leg are the same leg, which is what makes it
  read as an m and not as two arches. Bbox 8..56 x 12..52. Brand green #1f9a3a, white mark on it.
  Where it appears: tray icon tinted per state (idle #8b908b neutral, recording the user's
  accent so tray and overlay agree, persistent the accent too and busy/loading cfg color_busy -
  the tray says exactly what the bar says, and the tray title says which mic-open mode it is),
  window title bar and installer/app icon (the tile), sidebar lockup, README lockup.
  The README lockup ships with a fixed brand-green ink rather than currentColor: an <img> has
  nothing to inherit from, and its prefers-color-scheme reads the reader's OS, not GitHub's theme
  toggle, so a dark-theme reader on a light-mode OS got #16221a on #0d1117. No fixed colour
  clears 4.5:1 on both #ffffff and #0d1117 (4.35:1 is the arithmetic ceiling); #1f9a3a is 3.7:1
  and 5.2:1, which a 30 px logotype may have.
  brand.py draws all of it from those numbers - PIL half-discs minus inner discs plus leg rects,
  supersampled 4x - so **no image assets ship**: build.ps1 calls `python brand.py --ico
  murmur.ico` and murmur.ico stays generated and git-ignored. Sizes <= 24 px switch to a coarser
  16-unit grid (legs at 2/6/12, baseline 13) and <= 16 px is thresholded to whole pixels;
  a downsampled 64-grid mark is grey mush next to the system icons. Tiles <= 32 px also drop the
  208/256 inner box and give the mark the whole tile, its own 2/16 and 3/16 margins doing the
  padding: 208/256 of 16 is 13, i.e. scale 0.8125, which drew legs 2/2/1 px wide on a baseline at
  y=11.56 - purpose-drawn but not pixel-fitted, and pixel-fitted is most of what a frame per size
  is for. Pillow's ICO writer resamples one image for every size, so save_ico() hands it a
  purpose-drawn frame per size via append_images. The tray is the one place that gets none of
  this: pystray saves its own ICO from the single image it is handed, so the mark reaches it
  resampled - and it is handed mark(64) because a DPI-aware process on a 200% display has Windows
  ask LoadImage for a 64 px frame, which an icon stopping at 32 would have to upscale.
  tests/test_brand.py pins the PIL render against a headless-Chrome rasterisation of the same
  SVG path (IoU 0.9987), the 16 px pixel grid, the seven ico frames
  (whole-pixel legs in the 16 and 32 px ones included), every tray tint, and the rule that idle,
  mic-open and transcribing are three different icons at the shipped defaults.
- Promptify (2026-08-28, v0.8). The user found the "Prompt Master" skill and wanted the idea
  inside murmur: a History dictation -> a prompt for Claude, with "refining the language" and
  "asking for more context", as a button (never a chord mode - one trigger key is confusing
  enough). Research first (11 agents: the skill and its relatives, Anthropic's prompting canon,
  OpenAI/Google/academic consensus incl. the clarifying-question literature, shipped products,
  and a hands-on engine test), synthesized into a rubric of 15 sourced rules and 11 question
  dimensions, critiqued by three reviewers, revised, then demoed on three real dictations. The
  result is `promptify.txt`, the engine's system prompt: goal line first in the speaker's words,
  plain labels only for sections the dictation fills, fidelity over everything (terms, numbers
  and decisions verbatim; musings stay musings; no invented requirements; no longer than the
  dictation unless a list needs numbering), at most three questions and only where an answer
  changes the work, unanswered ones carried as "Open:" lines so the pass-1 prompt is already
  usable. Speech mis-hearings are fixed only when exactly one reading fits and every fix is
  listed as heard/wrote; the History entry is never touched. Name: the user's own "Promptify"
  (the brief recommended "Draft prompt"). Engines: the user asked for "any login for Claude Code
  or Codex or Gemini or OpenRouter" rather than Claude only, so `promptify.py` is an engine
  layer - three CLIs on their own logins and key-based OpenAI-compatible/Anthropic APIs from
  urllib (no SDKs in a frozen exe). The Claude path is the measured one: `claude -p --safe-mode
  --strict-mcp-config --tools "" --no-session-persistence --output-format json --model sonnet
  --system-prompt-file ... --json-schema ...`, dictation on stdin, MAX_THINKING_TOKENS=0, an
  empty cwd, CREATE_NO_WINDOW; 9-13 s per pass at 23-61 % CPU. The traps, all measured:
  `--bare` drops the OAuth login; without `--safe-mode` a call carries 56k tokens and 89 s
  (CLAUDE.md, hooks, 111 MCP tools); an inherited open stdin costs 3 s; without `--model` it
  runs on the settings model; thinking on turns 10 s into 75-110 s; Haiku dropped one of two
  asks in 3/3 runs, so Sonnet is the default and there is no model picker beyond the text
  field; `Popen.kill()` leaves claude's conhost/cmd children, so cancel is `taskkill /T`;
  PyInstaller leaks SetDllDirectory into children. Anthropic's support page says `-p` usage
  draws on the subscription window; `total_cost_usd` is a list estimate ($0.01-0.03), not a
  charge. Window: the panel takes the list's slot (a 20-30 line prompt cannot live under the
  5-line card), transcript collapsed to three lines on top, an editable prompt field that fits
  its content so the panel is the only scroller, one card per question with chips + a rounded
  answer field, Copy prompt as the panel's primary. Two things learned the hard way:
  `root.after()` from a worker thread raises "main thread is not in main loop" outside
  mainloop (the tests), so the worker only fills a dict and the 200 ms status ticker consumes
  it; and Tk sizes a Text in font lines and ignores spacing1/3, so N spaced lines in a height-N
  Text clip the last one - the panel's Texts have no spacing. Dictating into an answer field
  works because Typist already skips the paste when murmur's own window is in front; the take
  now also lands in the focused panel field (and in History like any take).
- Rising, not level (2026-08-28). The user saw the shipped mark and said it was not the one they
  picked: "the murmur with the left peak lower". The brief had two Ripple m builds and I shipped
  the level one on my own reading of "the ripple m". Now the rising build: both arches spring
  from y=28 (arch 1 centre (20,28) r 4/12, arch 2 centre (40,28) r 8/16), so the small arch's top
  sits at y=16 and the big one's at y=12 - a ripple growing, which is the better story anyway.
  The 16 px grid follows (arch 1 centre (5,7) r 1/3). Bbox, margins, lockup layout and every
  ICO frame are unchanged in size; only the geometry constants in brand.py and the two path
  strings moved, and tests/test_brand.py still pins the PIL render to Chrome's raster of the SVG.
- White window (2026-08-28). "It has a pinkish hue... I think our main colour is green, which
  contradicts... just white for the background." The window's neutral ramp was tinted from the
  bar's accent hue - the method's rule - and their bar colour is red, so every surface carried a
  1-2 % red blush. Now the neutrals are achromatic (light ground #ffffff, dark near-black) and the
  accent steps (fills, focus ring) come from the brand green, not the bar colour: the bar colour
  is the user's per-take signal and can be anything; the window is the product. Lesson for the
  method: a tinted-neutral ramp is only safe when the tint hue is the brand's, never a user-set one.
- Mic by name (2026-08-27). The user's first take on 0.7.0 "went green then back to transparent,
  nothing in history": the log showed 6 s takes at a constant rms 0.23 coming back empty in 0.0 s
  (Whisper's VAD dropped everything before the encoder ran). Config said `"mic": 2`, chosen on
  Aug 21 when 2 was the headset; Windows had re-enumerated and 2 was now the internal digital
  mic, which delivers zeros or a flat hiss. Settings now saves the device *name* (the MME entry,
  e.g. "Headset Microphone (Apple Audio"), `resolve_device` looks the index up at every take
  start (first MME input containing the name; unknown -> default), and the log names the mic
  whenever the opened device changes. sounddevice's own substring match was not used: the same
  name exists once per host API (MME, DirectSound, WASAPI, WDM-KS) and it raises on ambiguity.

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
