# Runtime resource optimization

October 28, 2026. Baseline: 63e1852, with the prior NumPy/MKL memory defaults
already applied. Probes use local files and hidden windows, without microphone,
clipboard, key listeners, visible windows or an optional Hub connection.

## Finding and change

AppWindow previously retained every rendered image until a complete window
rebuild. Resizable backgrounds and changing labels/colours create new keys
while old images remain in the dictionary. This is retained cache data, not
proof of an unreachable-object leak or the cause of all live-process growth.

The cache now evicts least recently used images that Tk reports unused, targeting
an estimated8 MiB RGBA reuse budget. The normal prebuilt window at200% DPI used
about2.6 MiB of cached pixels; this budget leaves reuse headroom. Native overhead
is additional. Live widget images and the image being returned stay alive even
if they exceed the budget, so this is not a hard process-memory cap.

Tk widgets keep image names rather than Python references: deleting a PhotoImage
still used by a widget blanks it. Eviction checks the real
[Tk image inuse command](https://www.tcl-lang.org/man/tcl8.6/TkCmd/image.htm).
Rendering and the model/decoder/streaming path are unchanged.

## Matched observations

Windows/Python3.13, real Tk PhotoImages and production AppWindow.rr/img. A
withdrawn canvas receives600 distinct64-pixel-high backgrounds, widths400-999.
Fresh processes in old/new/new/old order, no mapping, focus calls or user input:

| Resource after stress | Old cache | Bounded cache |
| --- | ---: | ---: |
| Retained images | 600 | 33 |
| Estimated retained RGBA pixels | 102.47 MiB | 7.92 MiB |
| Total private commitment, two runs | 220.20 / 221.54 MiB | 46.91 / 46.74 MiB |
| CPU seconds, two runs | 2.844 / 2.984 | 2.203 / 2.672 |
| Wall seconds, two runs | 2.917 / 3.108 | 2.322 / 2.770 |
| Handle growth during stress | 0 | 0 |

About174 MiB less total commitment in this stress scenario. The short timings
show no added CPU cost here, not a general speedup or reliable tail latency.
A full hidden-window prebuild plus stress at200% DPI ended at238.66 versus61.73
MiB; prebuild itself was1.005 versus0.799s (one sample each, not a startup-speedup
claim). Ordinary idle usage may save much less when few unique images have been
generated. A previously running copy needs a later relaunch to use the change.

A read-only20-second observation of the installed older process recorded366.84
MiB commitment throughout,418 handles throughout and0.328 CPU seconds (1.64% of
one logical core over this interval). Process I/O counters rose1155 read bytes
and24336 write bytes; these counters are not disk-only or network-only
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
python tools/profile_resources.py --output C:\temp\murmur-cache.json
python tools/profile_resources.py --prebuild --output C:\temp\murmur-full-cache.json
python tools/profile_resources.py --live-pid PROCESS_ID --seconds 20 --output C:\temp\murmur-live.json
```

For A/B, retain the old window.py outside the checkout and pass its path with
--window-file. Alternate configurations and keep inputs identical. The old
600-size report fails the8 MiB unused-cache budget while the new one passes.
Missing/empty reports and worker errors cannot count as successful checks.

test_image_cache exercises real Label/Canvas lifetimes, eviction after release,
LRU reuse, exact PNG equality, bounded resize growth and an oversized image.
Nine relevant suites passed: image_cache, murmur (real tiny.en file-only TTS and
streaming), memory, plugins, hub_integration, promptify, connect, vault and
plugins_ui. Off-screen light/dark captures were inspected. test_window and
test_ui_rules take focus and were not run; the unchanged brand suite was also
not run. No human speech trial is claimed.

Raw reports and test receipts:
C:/Users/there/codex/artifacts/murmur-optimization-2026-10-28/.
