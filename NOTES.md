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
  PyInstaller leaks SetDllDirectory into children. A second Workflow (4 agents) verified the
  other engines as far as this machine allows: Codex's native codex.exe sits under the npm
  shim's node_modules and is called directly (`exec - --json --ephemeral --ignore-user-config
  -s read-only --output-schema ... -c model_instructions_file=...`); its auth failure is a slow
  retry chain, so `codex login status` (exit code; the text is on stderr, 50 ms) runs first;
  the ChatGPT usage window was exhausted, so the only real Codex run ends in a clean "usage
  limit" error in 2.4 s. Gemini CLI is not installed: its argv, env (GEMINI_SYSTEM_MD,
  NO_BROWSER, GEMINI_CLI_NO_RELAUNCH) and the cwd settings file that turns tools off are
  docs-only. APIs (no keys here; shapes from docs + unauthenticated probes): api.openai.com
  wants max_completion_tokens and no temperature; Anthropic's current id is claude-sonnet-5,
  sampling params 400 on it, `thinking: disabled` is right except for Fable which refuses it;
  OpenRouter's id is anthropic/claude-sonnet-5 and it can answer 200 with an error object;
  Ollama wants any bearer token. Hence `post_degrading`: a 400 that names one of the optional
  keys we sent drops that key and retries. TLS from the frozen exe uses the Windows store - no
  certifi. Anthropic's support page says `-p` usage
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
- 1.0.0, the release pass: efficiency, then compaction, then the bug check (2026-09-16).
  Run in that order on purpose - the efficiency pass adds code as often as it removes it, so
  compaction should see what actually ships. Both procedures are now standing documents in PARA
  (3-Resources/methods/efficiency-optimization.md, and the pre-deletion danger list + final bug
  check added to major-release-cleanup.md), each item traced to a dated source rather than to
  habit.

  EFFICIENCY. Two changes, both measured, and three hunches killed by measurement.
  * A take held every chunk of itself for its whole life, and stop() concatenated the lot again:
    30 min measured 126 MB held + 115 MB for the copy, a 244 MB peak, for audio the streaming
    loop had finished with minutes before. The loop commits as it goes and nothing reads below
    `committed`, so Recorder.release(upto) drops whole committed chunks, snapshots start at the
    oldest sample still held, and stop() returns the audio WITH the sample index it starts at -
    both under one lock, because a pass still in flight can release more immediately afterwards
    and a base read separately would slice the tail at the wrong place and paste committed text
    twice. After: 0.6 MB held, 1.2 MB peak, and the tail handed to Whisper is 9 s instead of
    1800 s. The test feeds chunks whose samples carry their own index, so a slipped tail shows
    up as a value and not only as a length.
  * sounddevice was 0.20 s of the 0.46 s murmur.py cost to import, all of it PortAudio's DLL, and
    nothing needs it until a recording starts: audio() now loads it on the loader thread beside
    the VAD warm-up, so neither the tray icon nor the first take waits. Import 456 -> 181 ms.
    (Deferring it to first use instead would only have moved the stall onto the first dictation.)
  * Measured and left alone, with the numbers, so the next pass does not re-open them: the idle
    overlay costs 0.03-0.05 % of a core (the idle frame is a cached still); the window's image
    cache is 70 images / 0.60 MB and stays exactly that across an 800-step resize (only one key
    carries a pixel size, and it is a fixed-width field); the audio callback runs 0.014 ms median,
    under 1 % of its 20 ms deadline, and does not degrade at 90 000 chunks held - so its
    allocations, which sounddevice's docs warn against, are fine here and stay.

  COMPACTION. The static chain came back clean (pyflakes, ruff F+ERA, vulture at 100), so the
  haul was small and that is the honest result of having done this in September already. Deleted:
  when() - it formatted "Today 14:32" for a list row, but the list grew day-group headers (day_of)
  with a plain clock beside each row, and its five assertions were the only callers left;
  kbd_chord() - the app packs keycaps directly, only a test called it; vault's IO_DEADLINE_S -
  no reference anywhere. 22 lines. Each was checked against the new pre-deletion danger list
  (dynamic dispatch, entry points, persisted names, public API) first. on_screen() is also
  test-only and was KEPT: it is the readable form of _in_shown, and removing it would copy the
  visibility predicate into two test files - recorded here so the next pass does not re-litigate.
  deptry named huggingface_hub as an unused dependency; it is pinned for the error behaviour
  load_model relies on and is never imported, which is exactly deptry's documented false-positive
  class - kept. onnxruntime looked like 36 MB of dead weight in the bundle and is NOT: faster-
  whisper imports it lazily inside the VAD filter, which murmur turns on and warms at startup.
  Excluding it would have shipped a build that broke on the first dictation.

  THE BUG CHECK. Four real defects, none of which any test or lint would have found, all in the
  states a developer's own machine never reaches again:
  * No single-instance guard. murmur is invisible apart from a tray icon, so clicking the Start
    menu entry while it is already running looks like it did nothing - and started a second copy:
    two keyboard hooks, two overlays, every dictation pasted twice. A named mutex now claims the
    instance, and a second launch sets a named event that asks the running one to open its
    window, which is what the click meant. Proven with two real processes (the mutex is
    per-process, so nothing in-process can test it), including that the lock does not outlive the
    process holding it.
  * A crash went nowhere. The build is --noconsole, so it has no stderr, and nothing installed
    sys.excepthook, threading.excepthook or Tk's report_callback_exception. A failure in a UI
    callback - the likeliest kind, since every button and timer runs there - left the window
    silently unresponsive and a log that looked like a normal run. All three now write the
    traceback to murmur.log, which the tray's "Open config/log folder" opens.
  * load_config filtered unknown KEYS but never checked VALUES, and the tray invites hand-editing.
    Reproduced: "retention_days": "seven" loaded fine, then raised TypeError after every dictation
    (history silently lost, and in a windowed build with no trace). fits() now checks each value
    against the shape of its default - bool before int, since a bool is an int in Python - and
    NULLABLE carries the types of the keys whose default is None. A rejected value is logged and
    the default used.
  * save_config truncated the file before rewriting it. Now written beside the target and renamed
    over it with os.replace, so a crash mid-write leaves the previous settings rather than a
    truncated file that loads as defaults. (vault's index already did this; config was the outlier.)
  Checked and clean: no secret ever committed (the credential files live in the user's own
  profile); all 22 silent exception handlers are narrowly typed and each has a reason; first run
  with nothing on disk, a path with a space and Korean characters, a corrupt history file, a
  missing vocab and an unwritable config directory all behave; every background thread is a daemon
  and every long write is atomic.

- 1.0.0, the overlay's frame cost (2026-09-15, evening). "The graphics are really slow, and that
  makes it really choppy - that takes away the point of slow." Measured per style and scale,
  interleaved against the pre-liquid version: at 200 % a frame cost 36-49 ms against the 40 ms
  timer (the old fused bars 49 ms - it had been marginal all along), at 100 % 10-16 ms on a CPU
  that Whisper keeps busy. 30 ms of it was _fill's per-pixel lighting, and it ran on the 2x
  SUPERSAMPLE - four times the pixels the screen shows - because every layer was built at SS and
  the whole frame reduced at the end. Fix: shapes are still drawn at SS (that is the
  anti-aliasing) but `_mask` reduces to device pixels immediately, and fill, pulse, haze, glass
  and the dissolve all work there; `_render` no longer reduces. `_stick1()` caches the reduced
  stick, which the pulse now uses as its clip (eroded by a cheap gain instead of redrawing a
  rounded rect at SS every frame), and the haze's very wide blur runs at half resolution and
  scales back up. Same measurement after: 1.8-2.5 ms at 100 %, 4.9-6.7 ms at 200 %, with the old
  fused bars at 17 ms in the same run - about 3x cheaper than the version that felt smooth. A/B
  of the same frames before and after: mean |diff| 2.4-4.3 per channel (soft glow edges only),
  nothing visible at 4x zoom. With that headroom the app's timer went 40 -> 33 ms (FRAME_MS, 30
  fps): the render is 2-7 ms of it either way, and 25 fps was itself part of the steppiness.
  Two tests followed the pipeline: the transition test's idle reference no longer reduces
  `_base()`, and the dissolve test lights a reduced mask.
- 1.0.0, the equaliser's split (2026-09-15, evening). "When it pops up, it needs to be a bit
  slower. The merging could be a bit slower, so that we can show the visual transformations."
  Cause: nothing timed it - the stick under the pills vanished once the loudest band passed a
  third, which the fast attack reached in a frame or two. Now Overlay.split (0 = pills fused into
  the stick, 1 = apart) eases in tick with its own time constants (SPLIT_S 0.5 to open, MERGE_S
  0.7 to close, SPLIT_HOLD_S 0.15 so a pause between words holds it) and drives the equaliser's
  shape: the gaps open (pill width 1.0 -> 0.66 of the pitch), the pills rise (heights x split),
  the stick fades out under them, and while in between the mask is blended with a blur +
  contrast-ramp version so neighbouring pills pinch apart like liquid instead of cross-fading.
  Measured on TTS speech: opens in 0.48 s, merges in 0.92 s after the voice stops. A first hold of
  0.35 s left a flat row of beads sitting before the merge (the pills reach zero height before the
  hold ends); 0.15 s turns it into beads rolling together. The exponential's tails snap at 2 % so
  the resting stick becomes a cached still again; only the equaliser splits. Test pins it: no frame
  jumps the split by 0.4, open within 0.8 s, a short pause held, monotonic close to exactly 0.
- 1.0.0, three waveform styles in Settings > Appearance (2026-09-15, later). The liquid glass
  was "too slow... the individual waves are too much of a bump... needs more diversity in the
  peaks". Measured causes: the shape was a slow-drifting envelope - lobe axes wandering at
  0.02-0.06 Hz, the profile smoothed twice - so speech mostly scaled one fixed blob; and only 20
  bands, spread into neighbours, drawn as 15 equal lobes. The user asked for many versions to
  choose from: a rig in the scratchpad (wavevar/variants.py) rendered eight shapes plus the two
  references from the same TTS speech with the real overlay code, measuring motion (mean share of
  the stick area changing per frame; current 0.092) and peak variety (spread of the tallest
  point across twelve slices; current 43.6), published as the artifact "murmur waveform lineup".
  They picked the glass ribbon (E: 0.164 / 70.5), the glass equaliser (B: 0.173 / 79.9) and the
  fast liquid glass (A: 0.148 / 53.8), all three selectable. overlay.py now carries a WAVES table
  (bands, cols, attack, fall, spread, smooth, wobble Hz, mirror) and one engine: _analyse takes
  the style's band count and rates, _columns turns bands into column heights (mirrored lows-centre
  or scattered, each column shimmering on its own clock of active time so a fading take stays
  still), _spectrum_mask draws the style. The slow lobe-axis/field machinery is gone. One flaw
  found in the state render: equaliser pills alone relaxed into a row of beads while transcribing;
  the stick is now laid in under them in proportion to quiet, so they grow out of it and melt
  back. config "wave" (ribbon default; an unknown value falls back); Settings' first group is
  now "Appearance" with rows Theme and Waveform, the latter a three-cell segment with new wave /
  bars / drop glyphs, applied live through on_save. Test-suite notes from the day: the suites
  showed timing-dependent failures under load that also fail on the committed tree (a fake draft
  that finished mid-check - now gated on an Event; the History action-row placement read before
  its layout pass; a rare leftover id-keyed timer after the theme rebuild that five instrumented
  runs never reproduced) - recorded, not chased further. The display went from 192 to 96 DPI with
  a second monitor mid-session; that turned out not to matter (a DPI-unaware test process sees 96
  either way).
- 1.0.0, the user's test pass (2026-09-15), three asks. (1) The tap-to-latch was wrong for them:
  "don't make it tap start and tap stop on the headphones... a single click on my play pause
  button doesn't allow me to actually use it as a play pause button". The trigger key's grammar
  is now its own (the chord is untouched): every edge is swallowed and counted; a run that ends
  (no tap for DOUBLE_TAP_S) with ONE tap is sent back out as the key itself via keybd_event with
  dwExtraInfo = PASS_MARK, which the filter lets by - so Play/Pause plays and pauses, 0.4 s late;
  two taps start or stop a recording; three open the window; a single tap during a recording
  stops it; a key still down after HOLD_S (a keyboard key - a headset button never gets there)
  records while held. Timers go through Murmur.later so the tests fire them by hand. Verified
  live with F24 (not Play/Pause, so no music was touched): the hook sees the mark
  (dwExtraInfo 1836215666) on murmur's own re-sent edges and None on a plain keybd_event.
  (2) "When I say um or uh and then pause for a long time, it fills in that blank with a hell
  of a lot of fuzz" - their own message above came back with ~150 "uh," and a run of "sssss".
  murmur.log for that take: windows at compression ratio 26.24 three passes running with logprob
  -0.03 (Whisper's repetition hallucination, confident and identical each pass - the committed
  loop fed back as the next window's prompt). TTS could not reproduce the full loop (24 cases of
  filler + 4-14 s room tone at two noise levels: compression 1.23-1.28 throughout), but a
  deliberate poisoned-prompt test showed the cascade mechanism: a looping tail as prompt gave
  "uh, uh, uh, uh, way voice UI thing" where a clean tail gave "way voice UI thing", and
  no_repeat_ngram_size=3 changed nothing else across all 24 clean cases. Fix, decode-side only
  (no text is edited - the no-rewriting rule stands): a committed tail whose zlib compression
  ratio exceeds LOOP_CR 2.4 is never fed back as the prompt; a window that comes back with a
  segment over 2.4 is decoded once more with only the vocab prompt and no_repeat_ngram_size=3,
  and the less repetitive result is kept (a re-decode that is no better is rejected, tested).
  Costs nothing on ordinary windows (one decode, as before). Not verified on the user's real
  voice - that needs their next "um... pause" take; the log line "repetition loop: compression
  X, re-decoded Y" will show it firing. (3) "Improve the wave voice UI thing... reference the
  glass texture resource": the only glass resource filed is 3-Resources/ui-design-method/
  liquid-gooey (Jakub Antalik; nothing glass was filed after 09-01 - assumption named). Its two
  ideas carried into PIL: the gooey metaball (a blurred silhouette through a hard contrast ramp,
  so shapes merge through necks) and the two-layer render (the silhouette carries shape and
  shadow, the crisp material rides on top). The recording shape was 14 fused bars under a 0.6
  blur - a fuzzy symmetric "lemon" with stair-stepped edges and a white seam through the middle.
  Now: 15 overlapping ellipse lobes on the stick, blurred 1.05 S and ramped (v-110)*6, with a
  droplet thrown above any lobe past 72 % that fuses back or pinches off; the exact stick laid
  back in (ImageChops.lighter) because a blur + threshold thins a 7 px capsule. Material: glass
  denser at the rim than the body (Fresnel, from a 2.2 S blur of the mask), lighter at the top
  of each column, a crisp white specular line = the mask minus itself moved down 1.4 S, a lighter
  caustic along the bottom rim, a thin dark outline for white grounds, one tighter glow. Same
  spectrum engine, dissolves, haze and pulse. Three render rounds reviewed at true pixels (v1
  too smooth and pale; v2 knob-like droplet; v3 shipped). Per frame 17.5 vs 17.0 ms min
  interleaved (MaxFilter swapped for a blur+gain grow: 2.2 -> 0.6 ms). Two test findings were
  real, not noise: the fade's last step used to rise by 0.5 % - _dissolve truncated floats to
  uint8 (always down), so the final dissolve frames sat just under the idle stick; it now rounds.
  Two assertion constants moved with reasons written beside them: the lit haze reaches the old
  edge at 7 (was 20: a crisper body has less soft mass) and the lit glass carries 1.82x the idle
  alpha (the old glow 2.05x). Sheet: scratchpad murmur-waveform-before-after.png.
- 1.0.0, the trigger key that would not hold (2026-09-14). "If I'm only pressing down and
  continuing to press down on my play pause button, it won't record... the keyboard chord
  works." Evidence before theory: murmur.log shows every trigger "hold" ending in software within
  0.1-0.3 s ("[rec]" then "(nothing heard: 0.2s)"), while chord holds run as long as held. A
  headset's inline button is a consumer control: Windows reports it as an instant down/up pair
  however long it is physically held, so the chord grammar (hold = record while held) can never
  hold on that hardware - the take started at the down and stopped at the OS's immediate up. Fix
  (LATCH_S = 0.3): a trigger key released within 0.3 s of its press is a tap and LATCHES the take
  open (persistent) until the next press; a real hold (a keyboard media key, F13) still records
  while held and stops at the release. The double-tap grammar survives: a second tap inside the
  0.4 s window on a latched take is a no-op (chord_pressed returns instead of stopping), a third
  still opens the window, a later press stops. The chord is unchanged - a quick Ctrl+Win tap still
  records nothing rather than starting a take by surprise. Assumption named: the instant-up
  reading rests on the log durations, not on a HID trace; the latch works either way.
- 1.0.0, the ultracode pass (2026-09-07..09): "make the ui more modern... optimization... ui
  transitions looking more clean... reduce all unnecessary code and make compact for installation".
  Measured before anything: a view switch cost 90-160 ms (Tk re-laid the whole subtree out on
  grid_remove/grid; plus a PATH scan - 242 path-exists checks - on every Promptify switch via
  connect.status -> find_exe), the first window open 1.4 s, dist 299 MB. (1) SPEED: every view stays
  gridded and go() raises the target (experiment: 143-164 -> 16-25 ms Promptify, 35 -> 15 History,
  162 -> 44 Settings); the Promptify pane rebuilds only when (state, item, text, draft, sheet) changed
  (p_memo); engine status cached per session with a 60 s TTL (cleared by the sheet's actions); the
  window is prebuilt hidden 1.5 s after the model is ready; a _tab guard keeps focus inside the shown
  view. Gotcha found on the way: Tk paths are prefixes of each other (.!toplevel.!frame prefixes
  .!toplevel.!frame2) - the containment test needs path + "." or every sibling matches, and the
  earlier "Tk skips covered widgets" reading came from exactly that bug. (2) SIZE, by a verified
  workflow: PyAV (66 MB of FFmpeg DLLs) is import-bound only - faster_whisper/audio.py imports av at
  module level but murmur always hands numpy to transcribe - so stubs/rthook_av.py stands in and
  --collect-all av is gone; pandas (via tqdm.pandas(), lazy), lxml/bs4/soupsieve/xlsxwriter/dateutil
  (pandas' io), pytest/_pytest/pygments (pandas.conftest, httpx's optional CLI), pydantic(+core)
  (huggingface_hub's webhooks, guarded), hf_xet (is_xet_available() gate), tzdata (zoneinfo
  try/except), sqlite3 (filelock's optional ReadWriteLock), PIL._avif/_imagingft (try/except codecs)
  are all excluded; huggingface_hub collected without duplicate .py sources. dist 299 -> 175 MB
  (172.3 MiB, 1,207 files); a refuter re-measured, ran the frozen exe (model ready, VAD warm), ran the
  real transcription suite, and proved the first-run download still works without hf_xet. (3) DESIGN
  by the method: BRIEF3.md (what "modern" means here as six checkable properties), REFERENCES.md,
  three mockup directions rendered by headless Chrome - A Fluent-2 native, B glass echoing the
  overlay, C quiet-pro icon rail - judged by three lenses (taste 8/7/5 for B/A/C, method 7/5.5/5,
  build 7/3/8): A won 21 vs C 18 vs B 16.5, with B's icon+label segment and C's zero-shadow rule
  grafted; B's glow was the "larper" tell. Spec BUILD3.md (881 lines, function by function). Build
  stages landed: 1 tokens/palette/renderers (Fluent elevation borders, grain, specular, nine
  geometry-drawn glyphs, keycaps), 2 sidebar as one Canvas with icons + Ctrl 1/2/3 caps + the
  indicator bar, footer hint strips, Ctrl+, Ctrl+W Ctrl+F Esc bindings, reduced-motion detection,
  3 RowList day groups + right-aligned mono time column + draft mark, the filter field, action-row
  caps, button hover blend - and a pre-existing bug: show_sel ran before the list had a size so
  every fresh list opened scrolled. Stages 4-7 followed once the session limit reset: 4 Promptify
  (segment cells as Canvases with the >_ Code / globe Web icons, field underline, chips with icons,
  header chevron + ellipsize, the drafting skeleton), 5 Settings + the sheet (62 px rows, the raised
  trigger keycap, outlined-off toggle, inner-dot slider, the engines sheet restyled), 6 motion and
  states (hover/press blends, the nav bar slide after the switch, toggle frames, the drafting track,
  reduced-motion detection - all after() ids in self.jobs), 7 reconciliation (the spec walked line
  by line, vulture clean, ./test.sh green). Then three independent screenshot reviews of a 64-shot
  set (600/780/1000 x light/dark): 38 findings, 21 musts collapsing to eight distinct issues - all
  at 600 wide or in keyboard wiring - fixed in d342c9e (see the review-fixes entry above), recheck
  found nothing open. Adversarial verify: all seven suites green; every drawn keycap traced to a
  bind(); focus-visible proven by generated clicks (no ring) vs Tab (ring); Edit / Assume / Split /
  Appearance / Engines all covered by named asserts. Timing, min-of-N on a CPU busier than on
  09-07: this tree 45 / 32 / 78 ms (Promptify / History / Settings) against the pre-UI3 commit run
  side by side the same hour at 44 / 26 / 88 - parity on Promptify, faster on Settings, History
  +6 ms of Tk paint for its day headers, time column and marks (all in update(), <1 ms of Python).
  The refuter called that a fail against the 40/30/80 budgets set from the 09-07 numbers, which the
  control shows were the machine's, not the code's; accepted as parity. dist with the new UI:
  172.4 MB. Not pushed.
- 1.0.0, "can't find murmur in the Windows search bar" + the model cap (2026-09-01). Search:
  Windows finds apps through Start Menu shortcuts, and the mirror installs since 08-31 (robocopy
  /MIR of dist into %LOCALAPPDATA%\Programs\murmur, because Inno Setup vanished) never made
  one - worse, /MIR deleted Inno's unins000.exe/.dat from the 0.9.0 install, so the Start Menu
  folder held only a dead "Uninstall murmur" link, no app link, no startup link, and the
  uninstall registry entry was gone. Fixed by hand for this machine: murmur.lnk in the Start
  Menu group and the Startup folder (what installer.iss [Icons] makes), the dead link removed.
  ... which turned out to be only the surface. The recreated shortcuts vanished within a minute:
  Get-MpThreatDetection shows Windows Defender flagging murmur.exe as Trojan:Win32/Bearfoos.A!ml
  (an ML heuristic, ThreatID 2147731250) on 09-01 03:22 - that detection's resources were the
  exe, both .lnk files AND the Inno uninstall registry key, all quarantined: THAT is where the
  Start Menu entry, the startup link and the uninstaller went - and again on 09-07 06:29 / 06:34
  (the new shortcuts; the running process terminated), after which Start-Process refuses the
  exe outright ("the file contains a virus or potentially unwanted software"). Bisect with
  MpCmdRun -Scan -ScanType 3: a bare PyInstaller hello-world exe is clean, a PyInstaller exe
  with a pynput keyboard hook + pyperclip + ctypes is clean, murmur.exe is flagged - so it is
  not the (often-blamed) PyInstaller bootloader, it is Defender's model on murmur's own binary.
  Remedies, in order: an exclusion for %LOCALAPPDATA%\Programs\murmur (admin:
  Add-MpPreference -ExclusionPath ...) for this machine; a false-positive submission to
  Microsoft (microsoft.com/wdsi/filesubmission) for everyone; code signing (Authenticode - e.g.
  SignPath's free OSS signing) for the release, since unsigned + no reputation is what !ml
  detections punish. The published 0.9.0 setup.exe may be hitting the same detection on other
  machines. The real installer fix still stands: winget install JRSoftware.InnoSetup, then
  build.ps1. Model
  cap: "anything beyond 500 MB is overkill... remove those options completely" - MODELS is now
  tiny/base/small (.en and multilingual); medium.en, medium and large-v3 are gone from the
  picker, --model (choices=) and the README; load_config falls back to the default for a config
  still naming one, with a log line. Biggest offered: small at ~480 MB.
- 1.0.0, the default model (2026-09-01): "make default model on install base, as the tiny.en
  is lowkey not that great" - the shipped default was small.en all along; their own config said
  tiny.en (switched in Settings on 08-28). DEFAULTS model -> base.en (~150 MB first download
  instead of ~480), README to match, and their config pointed at base.en so the change is real
  on this machine (base.en was already in the HF cache here). small.en stays the accuracy pick
  in Settings.
- 1.0.0, the second verdict (2026-09-01, three asks). (1) "A dark mode option inside the
  settings": a Window > Appearance segment (System / Light / Dark, cfg "theme", default
  "system"); the palette is baked into every widget, so a change rebuilds the toplevel in place
  (geometry, view, selection kept; jobs cancelled first; a running draft's ticker lives in the
  old window, so the rebuild waits for the draft to land). The cfg choice beats the
  constructor's theme override (tests and screenshots), which beats the Windows setting. (2)
  "A bit of a cut off in the middle of the haze... we defined the widget size a bit too small":
  exactly that - the layered window was W+2*PAD by H+2*PAD (92 x 51 logical) and the haze's
  Gaussian (sigma 11-12 logical, x2.2-2.4 gain) is visible ~3 sigma past the shape, so the
  window edge sliced through it. Now the window grows by HAZE_PAD = 36 each side while the haze
  is on; the per-pixel _fill work stays on the small CORE canvas (cw x ch) and only the haze
  layer is built on the big one (a cheap L-channel blur), the core composited over it; the
  stick keeps its screen position (_place anchors it, not the window). Rendered check: alpha
  at the row where the edge used to be = 2 / 20 / 8 (idle / recording / busy), 0 at the new
  edges - the haze crosses the old line and fades out inside the window. Cached with the
  static fill so the busy state costs nothing extra per frame. (3) "Just remove the style
  part... we'll just use waveform": the Light style is gone - Settings row, cfg key
  "indicator" (old configs drop it silently), _light, _mic_level, phase/lvl/lpeak, the tick
  branch - and the tests with it (the dissolve test now uses a _fill frame). 
- 1.0.0, the user's first verdict (2026-09-01, seven asks, all in the same version - it never
  shipped). (1) "Takes a long time to promptify... tried it with the para": murmur.log had the
  real call - ONE claude engine call, rc 0, 93.6 s; the vault index rebuild ran 12 min later in
  the background and note reads were deadline-capped at 2 s, so para was not the cost. An
  interleaved bench (2 rounds x 2 golden dictations + a fixed-overhead probe): CLI+service floor
  ~4 s; the v2 prompt ran 21-67 s with the emitted "analysis" field unbounded (one 3,711-char
  analysis - the worked example's ~200-word analysis taught the model to write long ones, the
  examples-beat-rules lesson again); a variant with a 60-word example analysis and an explicit
  "under 80 words, fragments" cap held analyses to ~450 chars and ran 18-37 s. Fidelity gate
  (mechanical term retention, same metric both arms, 5 golden dictations): >= the stored
  current-prompt outputs wherever those were comparable (two stored outputs were the known
  "Test prompt." transients). Adopted. Also: three silent no-output CLI replies in ~15 calls
  today (rc 0, empty stdout) - the one retry now fires on silence whatever the exit code. And
  the vault hot path no longer opens a note at all: the index (v2, rebuilds itself) caches the
  frontmatter lines + 1,800 chars of body per note, so a draft never waits on the mount. (2)
  Edit the transcript before Promptify ("para" came out as "power"): Edit on the History card
  makes the transcript writable, the same button saves (Ctrl+Enter too, Esc abandons, leaving
  the row abandons); a changed text is written to history.jsonl, both lists repaint, and the
  entry's draft is dropped - it was made FROM the old words ("Saved - draft cleared"). (3) One
  take = one prompt by default: message carries "Split: never|allowed", the system prompt keeps
  independent asks as Parts of one prompt unless allowed, draft() folds a stray more_prompts on
  pass 1 as a belt; "Split into prompts" toggle in Promptify > Engines (prompt_split, default
  off). (4) Assume instead of leave-blank-to-skip: a text button at the right of each answer
  field; on, a tint bar (H_CTL, R_CTL, ellipsised label) covers the whole answer row and the
  field is gone; click/Space hands it back, so does a chip picked meanwhile. Pass 2 sends the
  ASSUME sentinel, message() expands it to the delegation instruction, the prompt rule adds an
  "Assumed:" line and retires the question. Gotcha: the button must be packed BEFORE the
  expanding field or pack leaves it no cavity at all (it was created, managed, and unmapped).
  (5) "A green outline around the whole lot" on click: every click-focused control rang. Now
  focus-visible: rings mark keyboard focus only - AppWindow.kbd, set True on Tab (bound on the
  toplevel's bindtag, which runs BEFORE the "all" tag that moves focus) and False by every
  mouse handler before it calls focus_set; _segment paints its ring only when kbd, _Btn._ring
  reads it, highlight-ring widgets (colour dots, opacity slider, toggles) go through
  focus_visible(). Arrow keys inside a focused control bring the ring back. (6) Pixelated
  title-bar icon: iconbitmap alone let Windows stretch a small .ico frame at 200%; the window
  now also hands Tk iconphoto renders of brand.tile at px(16) and px(32) device pixels - the
  real-screen grab shows a clean tile. (7) The view switch "not flowy": go() mapped the target
  frame FIRST and then painted its list and rebuilt the Promptify pane on screen. Researched
  (WPF/iOS/Electron all pre-render then reveal; Tk: mutate while unmapped, update_idletasks
  before mapping, grid_remove over destroy, never mix pack/grid on one master, no per-widget
  alpha exists): go() now prepares the hidden view, flushes idle tasks, then swaps frames in
  one event cycle. (8) The scrollbar's "tick bar in the middle": clam's thumb element draws its
  own grip (`gripcount`, default 5) - not a layout element; gripcount=0 on both styles.
  Screenshot review of every changed surface in the scratchpad (rv_*.png).
- 1.0.0: the compaction pass, the tap grammar, the README (2026-09-01). (1) First run of the
  major-version cleanup procedure (reference copy: PARA vault,
  3-Resources/programming/major-release-cleanup.md). Baseline: 7 suites green, source 5824
  lines. Static evidence: pyflakes 6 finds; vulture 60%-confidence gave 29 candidates, most
  of them dynamic-dispatch false positives (_body_* reached via _show, the HTTP handler's
  do_GET/log_message, ctypes struct fields, pystray callback params) - each candidate got a
  repo-wide grep before a verdict. Deleted with evidence: connect.OpenRouterConnect.key_status
  + KEY_URL (written for a usage read-out that was never wired; 0 callers, 0% coverage),
  overlay's nested profile() (superseded by lobes(); only comments still said "profile"),
  brand.MARK_16 (the written-out 16 px path; _G16 generates the real geometry - MARK_64 stays,
  it draws the SVGs), window._move (app-side 0 callers, kept alive by its own test; tests
  rewired to _nav_key + the list's own move), the clear_all back-compat alias (0 consumers),
  murmur.py's duplicate `import connect` (window.py imports it at top level; PyInstaller
  follows either), two unused locals, four unused test imports, and the stale "a key for the
  API engines, a base URL for the custom one" DEFAULTS comment (pre-login-only remnant).
  Dynamic evidence: coverage.py across all 7 suites = 87% total; every uncovered block >= 8
  lines answers "what real event executes you?" (first-run vocab seed, ctypes DPI/boot paths,
  real-network http_json, dynamic settings rows) - no dead code beyond the static finds. No
  profile-driven efficiency work: startup is already instrumented (since_launch) and dominated
  by the model load; nothing else showed. (2) luna routing reverted - the user: luna is free
  on web chat only; codex default model back to blank (Codex's own pick), openrouter back to
  anthropic/claude-sonnet-5; claude stays the default engine. (3) The tap grammar: the bound
  trigger key now follows the chord exactly (hold = record while held, double-tap =
  persistent, a later press stops) instead of one-press toggle, guarded against WM_KEYDOWN
  auto-repeat; triple-tap - three fast presses, chord or trigger key - opens the window
  (run_app hands Murmur.on_open = open_window, marshalled via root.after). The second tap of
  a triple starts a fraction-of-a-second take; discard() stops it without transcribing (the
  first tap's tiny take still transcribes to nothing, same as a double-tap always did). Tap
  runs are counted against DOUBLE_TAP_S from the last release; a run broken by time resets to
  1. (4) README rebuilt for 1.0: an honest comparison table (Win+H, cloud AI dictation, raw
  Whisper), "the first five minutes" onboarding, screenshots in docs/ (PrintWindow by HWND at
  the real DPI scale, light+dark via <picture>, staged history + a real-shaped Promptify
  draft with a Context link; overlay strip grabbed over a clean fullscreen ground), the
  lockup in the header, the mark + one quiet star line in the footer. Screenshot harness kept
  in the session scratchpad (readme_shots.py), not the repo. Source after the pass: 5826
  lines (deletions minus the ~50 the tap grammar added).
- Promptify v2, the vault bridge, luna routing (2026-09-01, v0.10). Three asks. (1) "In depth
  research about prompt engineering" from the Anthropic lectures et al: two research agents mined
  the deep-dive roundtable (Albert/Askell/Witten/Hershey), Prompting 101, the interactive
  tutorial and Academy courses, plus the non-Anthropic canon (OpenAI's meta-prompt and optimizer
  cookbooks, DLAI, Boonstra's whitepaper, Schulhoff, DSPy/OPRO). The convergent levers landed in
  promptify.txt v2 (10.7k -> 18k chars): a required "analysis" field FIRST in the schema (key
  order is generation order - structured reasoning space: decisions verbatim, musings,
  mis-hearing suspects, ambiguities with their readings, then the <=3 blocking questions, then a
  fidelity self-check), two worked dictation->JSON examples with deliberately off-domain content
  (a community garden; a rename) so the model copies the moves and not the vocabulary - one
  showing questions [], or a mini model asks three every time; a whole-dictation out (a memo is
  returned cleaned, never given an invented goal); explicit precedence lines for the three rule
  tensions the optimizer taxonomy found (length vs labels, never-add vs the delegation sentence,
  splitting vs the path question); a processing-order line; pass 2 may not reorder sections; an
  end-of-prompt reminder block (the lectures: repeat what matters at the end); and the hedged-ask
  rule ("I feel like the layout needs to be smoother" is an ask, not a musing) added after the
  golden A/B caught v2 demoting a real request. A/B on 5 frozen real dictations (kept local,
  never in this repo), old vs new through claude/sonnet: key-phrase retention >= old on every
  sample (2/2, 13-14/17, 41/49...), better splits (independent asks -> more_prompts), sharper
  questions (it caught "Codex vs codecs"); one transient "Test prompt." output and intermittent
  silent CLI crashes (rc!=0, both streams empty, right after claude.exe auto-updated) -> the
  claude engine retries once on a silent crash. (2) The Obsidian bridge (vault.py): the user's
  vault is an rclone mount where a cold walk took 198 s and a full content scan never finished,
  so the hot path reads ONLY a cached head-index (%APPDATA%\murmur\vault_index.json: title,
  lookup names incl. the parent folder for README hubs, tags, a 300-char description from the
  first 4 KB), built on a daemon thread, TTL 6 h, exclusion changes delete the index first.
  Matching inverts: the index's name dictionary is matched against the spoken token stream
  ("second brain" finds second-brain.md); >=1 real name hit required, folder weights (Archives
  0.25), recency; 3 notes x 1200 chars <= 3000 total; live reads deadline-guarded at 2 s total
  (a read of a hung mount is abandoned on its daemon thread - it cannot be interrupted).
  <vault_context><note path title modified> after the dictation on both passes (pass 2 reuses
  pass 1's block verbatim); the system prompt treats it as background the speaker never said,
  dictation wins on conflict, used notes cited in notes and shown as clickable Context links
  (obsidian://open). journal/diary/private/people are never indexed; off by default; enabling
  the vault re-shows the disclosure once (vault_ack). (3) "Route our default model to gpt luna,
  as luna is free now": on the routes murmur has it is NOT free - OpenRouter lists
  openai/gpt-5.6-luna at $0.20/$1.20 per M (~$0.002/call, no :free variant), and this machine's
  Codex now answers "Upgrade to Plus ... try again at Sep 28" (the free-tier message; the plan
  appears lapsed), so luna became the default MODEL on both GPT routes (codex -m gpt-5.6-luna,
  openrouter openai/gpt-5.6-luna) while claude stays the default ENGINE - switching the active
  engine to a blocked route would fail every press. Luna is reasoning-class (codex sets
  model_reasoning_effort on it), so v2 keeps structure + examples and no CoT scaffolding; its
  fidelity on the golden set is untested until a GPT route opens. Also: OpenRouter replies cut
  at the token limit (finish_reason length) now say so instead of failing JSON parse.
- Login-only engines, a Promptify section, the window redone (2026-08-29, v0.9). The user's
  verdict on the first Promptify build, in three parts. (1) "These engines should be linked
  through login on web, not like directly taking [keys]": the key-based engines went (OpenAI,
  Anthropic, Gemini API, custom URL) and `connect.py` holds what is left - the CLIs' own
  browser sign-ins and OpenRouter's OAuth. A 5-agent hands-on pass established the mechanics
  without completing any login: `claude auth login` prints its URL on stdout wrapped in OSC-8
  escapes, opens the browser itself (rundll32, or env BROWSER), listens on a random loopback
  port, and offers "Paste code here if prompted >" on stdin for the manual page; `codex login`
  prints everything on stderr, opens the browser in a way that cannot be suppressed, uses port
  1455 (1457 fallback, GET /cancel evicts a stuck one); `gemini` has no login command - the
  first headless run asks "[Y/n]:" on stdout and hangs forever if stdin is closed before it
  does, so the sign-in answers "y" and every exec keeps stdin open and answers "n" (a dead
  login must never open a browser mid-dictation), and the dictation travels as -p, not stdin;
  OpenRouter is the one provider with first-party OAuth for desktop apps (PKCE, loopback
  callback, code -> key at /api/v1/auth/keys; never fetch its /auth page from Python -
  Cloudflare 403s non-browsers). None of the four times out on its own: the timeout is
  murmur's (5 min, kill the tree). Status dots come from the credential files alone
  (presence and claude's refreshTokenExpiresAt; exit codes are presence-only and gemini's can
  be a libuv crash) - values are never read, logged or shown. Anthropic's policy: sign in to
  the unmodified claude.exe with your own subscription is allowed; routing its tokens elsewhere
  is not, so murmur only ever runs the binary. Exec-time env scrubs: ANTHROPIC_API_KEY,
  ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN (claude), OPENAI_API_KEY, CODEX_API_KEY,
  CODEX_ACCESS_TOKEN (codex) - a key in the environment silently switches the CLI to API
  billing. Residue of the verification (no credentials): %LOCALAPPDATA%\murmur-test, deleted.
  (2) "A separate section on the left that deals with promptify": a third sidebar item, two
  panes (their popup choice), the engines sheet lives there and the Settings group is gone.
  (3) "I don't like the ui within the promptify. Reference our memories of ui to refactor all
  our ui": the method, properly this time - a written §1.1 brief (ui/BRIEF2.md; reference
  Raycast Settings, their choice), three mockup directions rendered by headless Chrome, a
  three-lens judge panel (craft §11 · the user's own words · Tk implementability: raycast
  24.5, airy 21, dense 20.5), the winner refined with the grafts (two-line list rows, tinted
  chips with a chosen dot instead of a second solid green, the target segment on one header
  row only when it fits, "Dictations · N" instead of a second view title, the state word
  inside the engine row's status line), then a 31 KB build spec with every number. What the
  first panel got wrong, named: five hierarchy levels, chips as bordered buttons inside
  bordered cards inside a panel, the answer field a third nested box, the primary at the
  bottom, a status sentence in the body flow. Lesson: a feature dropped into an existing
  window still needs its own brief and a mockup pass - "it fits the tokens" is not a design.
  The build: two sequential agents on window.py (no worktrees - one file), then an independent
  reviewer who re-rendered every state by HWND and drove the view by script (3 musts: Tk
  Labels' default 3 px chrome breaking the left edge, the list pane's empty state wrapping at
  W_COL, the one-row header rule measured on a stale width; 8 shoulds), a fix pass, a re-check
  that passed with two cosmetic residuals (the 600-wide empty pane's key line overruns the
  right padding by 5 px; a stale probe). `tests/test_ui_rules.py` now asserts the contrasts,
  the spacing scale, one primary per state, the measure and the focus colours.
- Two of the user's asks after trying 0.9 (2026-08-29): a Promptify button back in History
  (it goes to the Promptify section with the same row, drafting at once when there is no
  draft - the Ctrl+D path), and the sidebar mark in the brand green instead of muted ("the
  logo should be green, not just gray"); the wordmark stays muted. Pushed and released as
  0.9.0 on their word - the first publication since 0.2.0.
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
