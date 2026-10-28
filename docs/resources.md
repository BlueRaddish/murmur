# Runtime resource optimization

Public version 1.0.1 includes the numeric-library memory defaults and bounded UI
image reuse. Probes use local files and hidden windows, without microphone,
clipboard, key listeners or visible windows.

## Finding and change

AppWindow previously retained every rendered image until a complete window
rebuild. Resizable backgrounds and changing labels/colours create new keys
while old images remain in the dictionary. This is retained cache data, not
proof of an unreachable-object leak or the cause of all live-process growth.

The cache now evicts least recently used images that Tk reports unused, targeting
an estimated 8 MiB RGBA reuse budget. A historical prebuilt window at 200% DPI used
about 2.6 MiB of cached pixels; this budget leaves reuse headroom. Native overhead
is additional. Live widget images and the image being returned stay alive even
if they exceed the budget, so this is not a hard process-memory cap.
When the cache remains over budget, hits also trigger eviction of images that
widgets have since released. Returning to the budget does not require a new
image to be rendered.

Tk widgets keep image names rather than Python references: deleting a PhotoImage
still used by a widget blanks it. Eviction checks the real
[Tk image inuse command](https://www.tcl-lang.org/man/tcl8.6/TkCmd/image.htm).
Rendering and the model/decoder/streaming path are unchanged.

## Historical matched observations

The following measurements were collected on October 28, 2026 from an unpublished
local application build with the NumPy/MKL memory defaults already applied.
They isolate the cache behavior in that build; they are not measurements of the
independent public v1.0.1 package.

Windows/Python3.13, real Tk PhotoImages and production AppWindow.rr/img. A
withdrawn canvas receives 600 distinct 64-pixel-high backgrounds, widths 400-999.
Fresh processes in old/new/new/old order, no mapping, focus calls or user input:

| Resource after stress | Old cache | Bounded cache |
| --- | ---: | ---: |
| Retained images | 600 | 33 |
| Estimated retained RGBA pixels | 102.47 MiB | 7.92 MiB |
| Total private commitment, two runs | 220.20 / 221.54 MiB | 46.91 / 46.74 MiB |
| CPU seconds, two runs | 2.844 / 2.984 | 2.203 / 2.672 |
| Wall seconds, two runs | 2.917 / 3.108 | 2.322 / 2.770 |
| Handle growth during stress | 0 | 0 |

About 174 MiB less total commitment in this stress scenario. The short timings
show no added CPU cost here, not a general speedup or reliable tail latency.
A full hidden-window prebuild plus stress at 200% DPI ended at 238.66 versus 61.73
MiB; prebuild itself was 1.005 versus 0.799 s (one sample each, not a startup-speedup
claim). Ordinary idle usage may save much less when few unique images have been
generated. A previously running copy needs a later relaunch to use the change.

A read-only 20-second observation of the installed older process recorded 366.84
MiB commitment throughout, 418 handles throughout and 0.328 CPU seconds (1.64% of
one logical core over this interval). Process I/O counters rose 1155 read bytes
and 24336 write bytes; these counters are not disk-only or network-only
attribution. Activity was uncontrolled, so this is not a certified idle test.
No CPU/thread/latency bottleneck was established from that observation. Prior
model-memory and packing-latency evidence remains in [memory.md](memory.md).

GPU utilization/VRAM, per-process network bytes, energy and a long-duration soak
were not measured. No improvement in those categories is claimed. No recurring
profiler/telemetry runs in normal use. Log retention, model eviction and worker
counts were left alone without a measured case to change them.

## Repeat the checks

The development probe requires psutil in addition to normal app dependencies;
it is not a new dependency of the shipped app. Output parents are created.

```powershell
python tests/test_image_cache.py
python tests/stress_resources.py --output C:\temp\murmur-stress.json
python tools/profile_resources.py --output C:\temp\murmur-cache.json
python tools/profile_resources.py --prebuild --output C:\temp\murmur-full-cache.json
python tools/profile_resources.py --live-pid PROCESS_ID --seconds 20 --output C:\temp\murmur-live.json
```

For A/B, retain the old window.py outside the checkout and pass its path with
--window-file. Alternate configurations and keep inputs identical. The old
600-size report fails the 8 MiB unused-cache budget while the new one passes.
Missing/empty reports and worker errors cannot count as successful checks.

test_image_cache exercises real Label/Canvas lifetimes, eviction after release,
LRU reuse, exact PNG equality, bounded resize growth and an oversized image. It
also covers trimming on a cache hit after widgets release over-budget images.
stress_resources runs six independent cases and writes a JSON report; a nonzero
exit or failed case is a failed check. Add --quick for a shorter smoke test,
which reduces the iteration counts and does not replace the full stress run.
The full source test runner is test.sh; several legacy UI suites take desktop
focus, so run them in a suitable test session.

## Candidate stress checks

Focused stress testing of the independent public candidate passed six cases:
20,000 image creations/evictions; 1,000,000 cache hits; normal, active, disabled
and hidden Canvas images; shared image ownership; reuse after releasing
over-budget live images; and 6,000 production resize renders. The reuse case
first failed against the original cache implementation, exposing retained unused
images. The corrected cache trims on that hit, and the regression is included in
test_image_cache.

The resize workload alternated forward and reverse sweeps of 600 widths for ten
rounds, completing in 30.8 s (28.2 CPU seconds). At round boundaries, reusable
pixels stayed within 7.89-7.92 MiB, with 33-74 cached images. Process private
commitment ranged from 48.0 to 53.0 MiB; its range after the first three rounds
was 5.0 MiB, within the check's 16 MiB allocator-slack allowance. Native Tk image
names returned to their starting set after cleanup. This demonstrates bounded
retention during the tested resize workload, not a long-duration application
memory plateau or a general idle-RAM prediction.

All nine source suites passed: image_cache, memory, murmur, promptify, connect,
vault, brand, window and ui_rules. The two focus-taking Tk UI suites ran on an
inactive Windows desktop; the foreground window and clipboard sequence stayed
unchanged. The transcription suite completed a real tiny.en decode of file-only
Windows TTS and all subsequent assertions, using normal development imports.

An earlier transcription attempt failed with mkl_malloc while system commitment
headroom was below 1 GiB. The successful normal-import retry started with
2543 MiB of headroom, remained above 1790 MiB and peaked at 440.4 MiB child
private commitment. A separate source run using the packaged import policy also
passed. The earlier allocation failure remains a limit of testing under severe
memory pressure; passing retries do not establish success under every memory
condition. The packaged import policy run is not verification of a built binary.

These results do not establish packaged-build or human-speech acceptance. The
model, quality and streaming settings are unchanged.
