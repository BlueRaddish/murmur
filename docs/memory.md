# Windows memory diagnosis and fix

The October 26 investigation reproduced the high committed-memory footprint and
identified two process-wide numeric-library defaults. Public version 1.0.1
includes the independent memory changes. The fix keeps the same
Whisper model, int8 compute, beam size, streaming boundaries and text processing.

The measurements below were collected from an unpublished local application
build on October 26, 2026. They attribute the numeric-library allocations under
matched conditions; they are not measurements of a public v1.0.1 package.

## Measured breakdown

Windows 11, Python 3.13, NumPy 2.4.5, CTranslate2 4.8.1, faster-whisper 1.2.1,
ONNX Runtime 1.29.0. Each row below is a fresh **frozen application** running the
same cached base.en model and six seconds of synthetic silence, with only the
two environment settings varied. The ordinary UI is not started in this probe.

| NumPy/OpenBLAS threads | MKL packed weights | Model-ready private commit |
| --- | --- | ---: |
| 12 (the observed original pool) | on | 1427.8 MiB |
| 1 | on | 1074.4 MiB |
| 12 | off | 520.4 MiB |
| 1 | off | 166.5 MiB |

That is about **1261 MiB / 88% less commitment** in the matched model-ready
comparison. The NumPy thread-pool difference is approximately 353 MiB and the
packed-weight difference approximately 908 MiB. These are measured process
deltas, not guesses based on model-file size.

The previous long-lived installed process had about 1631 MiB private commit and
84 MiB resident. The fixed, newly launched tray application, after model load and
window prebuild, had about 182-186 MiB private commit and 219-223 MiB resident.
Those live snapshots have different process ages and workloads; the controlled
table above is the direct comparison. Windows paging can make a larger committed
process look smaller in current physical RAM. Commit and RSS must remain separate.

The fixed frozen diagnostic's approximate stages were 36 MiB after app imports,
43 MiB after the inference backend import, 156 MiB with weights loaded, and
166 MiB with audio/VAD ready. Releasing the model left about 81 MiB. Stage deltas
can be negative as temporary native allocations are freed; they are not perfectly
independent buckets. The loaded cuDNN dispatcher image was small and was not the
main source of the commitment.

## What changed

Before NumPy/CTranslate2 imports, Murmur now defaults these variables **only in
its own process**:

```text
OPENBLAS_NUM_THREADS=1
CT2_PACKED_GEMM=0
```

Small NumPy array/FFT operations do not need an additional large BLAS pool.
Whisper retains its own default four CPU inference threads. CTranslate2's MKL
packing optimizes matrix reuse by allocating prepacked weight copies; disabling
it avoids the dominant extra commitment. No global Windows environment setting
is changed. Explicit environment values still win, allowing a controlled opt-out.

The model stays ready. A new lazy-loading/idle-eviction state machine was not
necessary to remove the measured waste and would add cold-load and concurrency
tradeoffs. A later idle policy can be evaluated against the smaller remaining
model footprint if desired.

## Speed and output check

In fresh-process interleaved order packed/unpacked/unpacked/packed, the same
base.en int8 decoder transcribed the same file-only synthesized technical sentence
three times per process. OpenBLAS used one thread in both groups, so this
comparison isolates weight packing rather than both memory defaults together.
Every transcript was identical. Warm median times were
1.169 s packed versus 1.196 s unpacked; minimums were 1.044 s and 1.132 s. The
roughly 27 ms median difference is small relative to round-to-round noise on this
busy machine, so this is not a speedup claim. Retained private commitment after
speech was about 1363 MiB packed versus 461 MiB unpacked in the unfrozen process
(which imports additional development dependencies). This is a limited fixture,
not proof of recognition quality or latency for all human speech.

An initial packed benchmark failed to allocate under system commit pressure;
its failure receipt was retained. The full interleaved comparison succeeded
after the build process finished and enough headroom returned.

## Reproduce the diagnostic

```text
murmur.exe --diagnose-memory C:\temp\murmur-memory.json
python murmur.py --diagnose-memory C:\temp\murmur-source-memory.json
```

This is an explicit offline mode that bypasses the normal instance mutex. It
uses a cached model only, samples real Windows private-commit/working-set counters
at production constructor checkpoints, runs synthetic silence through the real
decoder, inventories loaded DLL **names and image sizes**, and releases the model.
It does not open the microphone, install keyboard listeners, touch the clipboard,
launch UI, save config, collect transcripts or upload anything. Missing cached
models produce a failed report and a nonzero exit code. Unavailable native
counters also fail loudly with a nonzero exit; they cannot appear as zero usage.

The JSON includes process ID, model/device, the three relevant environment values,
package versions when bundled, native memory counters, stage deltas and loaded
library inventory. Library image size is not private heap attribution and must
not be summed with private commit. Normal dictation has no allocation tracing
or continuous sampler; checkpoint callbacks run only in diagnostic mode.

## Verification and limits

Run the memory checks on Windows with the normal source dependencies installed:

```powershell
python tests/test_memory.py
python tests/test_murmur.py
```

The memory tests check before-import defaults, respected explicit overrides,
real Windows counters and loaded-library inventory. They also verify diagnostic
routing bypasses the instance mutex and does not create a missing config. The
transcription suite exercises the input state machine, streaming and a real
tiny.en decode of file-only Windows TTS, with fake clipboard typing. That decode
requires a cached model or a first-time download.

On the public candidate, test_memory and the complete test_murmur suite passed,
including the real tiny.en transcription. An initial decode attempt failed with
mkl_malloc while system commitment headroom was below 1 GiB; the normal-import
retry passed after headroom recovered, remaining above 1790 MiB during the run.
A separate source run with the packaged import policy passed as well. Preserve
the failed attempt when assessing memory-pressure limits; a successful retry
does not prove the decoder can run under every system load. All nine source
suites and the six-case cache stress scope are recorded in
[resources.md](resources.md).

For a packaged build, run the same offline diagnostic against its executable and
check the exit status and JSON success flag. A successful source test alone does
not validate the packaged dependencies. Historical source and frozen diagnostics
passed on the unpublished build; that result is not a public-package test receipt.

No physical microphone/hotkey trial, long-duration leak soak, GPU memory
benchmark or human speech quality study is claimed here. Existing config and
decoder behavior remain the user's choices. The UI image cache is a separate
change described in [resources.md](resources.md).

Primary references: [CTranslate2 environment variables](https://github.com/OpenNMT/CTranslate2/blob/master/docs/environment_variables.md)
and [CTranslate2 memory management](https://opennmt.net/CTranslate2/memory.html).
