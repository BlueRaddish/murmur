# Windows memory diagnosis and fix

The October 26 investigation reproduced the high committed-memory footprint and
identified two process-wide numeric-library defaults. The fix keeps the same
Whisper model, int8 compute, beam size, streaming boundaries and transcript text.

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
three times per process. Every transcript was identical. Warm median times were
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
launch UI/Hub, save config, collect transcripts or upload anything. Missing cached
models produce a failed report and a nonzero exit code. Unavailable native
counters also fail loudly with a nonzero exit; they cannot appear as zero usage.

The JSON includes process ID, model/device, the three relevant environment values,
package versions when bundled, native memory counters, stage deltas and loaded
library inventory. Library image size is not private heap attribution and must
not be summed with private commit. Normal dictation has no allocation tracing
or continuous sampler; checkpoint callbacks run only in diagnostic mode.

## Verification and limits

Executed relevant suites: test_murmur (including real tiny.en TTS transcription,
streaming, recorder, fake clipboard typing and overlay rendering), test_memory,
test_plugins, test_hub_integration, test_promptify, test_connect and test_vault.
Memory tests cover before-import defaults, respected explicit overrides, real
Windows counters and loaded-library inventory. Source and frozen diagnostic modes
both passed; the frozen report created no config and left personal config bytes
unchanged. Compiled module equality against source was checked for murmur,
memory_diagnostics, plugins and window; a deliberately modified source was rejected.

The UI was not changed. Focus-taking legacy UI suites were not run; they are not
evidence for this change. No physical microphone/hotkey trial, long-duration leak
soak, GPU memory benchmark or human speech quality study is claimed. Existing
config and decoder behavior remain the user's choices. The packaged app was
staged alongside the old installation, with a reversible shortcut update.

Primary references: [CTranslate2 environment variables](https://github.com/OpenNMT/CTranslate2/blob/master/docs/environment_variables.md)
and [CTranslate2 memory management](https://opennmt.net/CTranslate2/memory.html).
