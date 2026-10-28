"""Additional real-Tk cache stress; no mapping, focus, audio or clipboard.

Includes the released-overbudget hit regression and native resource cleanup.
Use --quick for a short check; omit for serial stress and a repeated resize soak.
"""
import argparse
import gc
import json
from pathlib import Path
import sys
import time
import traceback
import tkinter as tk

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--quick', action='store_true')
args = parser.parse_args()
sys.path.insert(0, str(args.repo))
import window as W

root = tk.Tk()
root.withdraw()
checks = []
timings = {}

def photo(colour='#135724', side=8):
    image = tk.PhotoImage(master=root, width=side, height=side)
    image.put(colour, to=(0, 0, side, side))
    return image

def measured(name, case):
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        detail = case()
        checks.append({'name': name, 'passed': True, 'detail': detail})
    except Exception:
        checks.append({'name': name, 'passed': False, 'error': traceback.format_exc()})
    timings[name] = {'wall_seconds': time.perf_counter() - wall,
                     'cpu_seconds': time.process_time() - cpu}

def pixel_ok(image):
    assert root.tk.call(str(image), 'get', 0, 0) == (19, 87, 36)

def release_then_hit():
    cache = W._ImageCache(max_bytes=512)
    labels = []
    try:
        for i in range(96):
            labels.append(tk.Label(root, image=cache.image(i, photo)))
        pinned = cache.bytes
        assert pinned == 96 * 8 * 8 * 4
        assert len(cache) == 96
        for label in labels:
            label.destroy()
        labels.clear()
        last = cache.image(95, lambda: 1 / 0)
        pixel_ok(last)
        after = cache.bytes
        assert after <= cache.max_bytes, (
            f'Formerly pinned images remain after a cache hit: {after} > {cache.max_bytes}; '
            'no future miss is guaranteed once a window becomes idle')
        return {'pinned_bytes': pinned, 'after_release_hit_bytes': after}
    finally:
        for label in labels:
            label.destroy()
        cache.clear()

def canvas_variants():
    # Attach all variants before asking the cache to evict. Production callers
    # attach each image immediately (or retain a Python owner); building an
    # unpinned batch under an undersized budget would evict an early image
    # before it acquires Tk users, which does not exercise live-image protection.
    cache = W._ImageCache(max_bytes=2048)
    canvas = tk.Canvas(root)
    try:
        images = [cache.image(name, photo) for name in ('normal', 'active', 'disabled', 'hidden')]
        canvas.create_image(0, 0, image=images[0], activeimage=images[1], disabledimage=images[2])
        canvas.create_image(0, 0, image=images[3], state='hidden')
        names = [str(image) for image in images]
        del images
        cache.max_bytes = 512
        for i in range(32):
            cache.image(('evict', i), photo)
        for name in names:
            assert root.tk.getboolean(root.tk.call('image', 'inuse', name))
            assert root.tk.call(name, 'get', 0, 0) == (19, 87, 36)
        canvas.destroy()
        cache.image('new-after-destroy', photo)
        assert cache.bytes <= cache.max_bytes
        assert not set(names) & set(root.tk.call('image', 'names'))
        return {'protected_variants': len(names), 'after_destroy_bytes': cache.bytes}
    finally:
        if canvas.winfo_exists():
            canvas.destroy()
        cache.clear()

def shared_widget_owners():
    cache = W._ImageCache(max_bytes=256)
    image = cache.image('shared', photo)
    name = str(image)
    label = tk.Label(root, image=image)
    canvas = tk.Canvas(root)
    canvas.create_image(0, 0, image=image)
    del image
    try:
        label.destroy()
        for i in range(30):
            cache.image(i, photo)
        assert root.tk.getboolean(root.tk.call('image', 'inuse', name))
        assert root.tk.call(name, 'get', 0, 0) == (19, 87, 36)
        canvas.destroy()
        cache.image('released', photo)
        assert name not in root.tk.call('image', 'names')
        assert cache.bytes == 256
        return {'shared_owner_release_order': 'label, canvas'}
    finally:
        if label.winfo_exists():
            label.destroy()
        if canvas.winfo_exists():
            canvas.destroy()
        cache.clear()

def churn():
    baseline = set(root.tk.call('image', 'names'))
    canvas = tk.Canvas(root)
    item = canvas.create_image(0, 0)
    iterations = 1000 if args.quick else 20000
    cache = W._ImageCache(max_bytes=8192)
    try:
        for i in range(iterations):
            image = cache.image(i, photo)
            canvas.itemconfigure(item, image=image)
            if i % 127 == 0:
                assert cache.bytes == sum(value.width() * value.height() * 4
                                          for value in cache.values())
                assert cache.bytes <= cache.max_bytes
                pixel_ok(image)
        canvas.destroy()
        del image
        cache.clear()
        gc.collect()
        assert set(root.tk.call('image', 'names')) == baseline
        return {'insertions': iterations, 'native_image_names_return_to_baseline': True}
    finally:
        if canvas.winfo_exists():
            canvas.destroy()
        cache.clear()

def repeat_hits():
    cache = W._ImageCache(max_bytes=512)
    try:
        image = cache.image('key', photo)
        iterations = 10000 if args.quick else 1000000
        def forbidden():
            raise AssertionError('Cache hit invoked renderer')
        for i in range(iterations):
            assert cache.image('key', forbidden) is image
        assert cache.bytes == 256 and len(cache) == 1
        return {'hits': iterations, 'render_count': 1}
    finally:
        cache.clear()


def resize_soak():
    import memory_diagnostics
    app = W.AppWindow(root, None, {}, lambda _: None)
    baseline = set(root.tk.call('image', 'names'))
    canvas = tk.Canvas(root)
    item = canvas.create_image(0, 0, anchor='nw')
    snapshots = []
    rounds, sizes = (3, 100) if args.quick else (10, 600)
    try:
        for round_ in range(rounds):
            for offset in range(sizes):
                width = 400 + (offset if round_ % 2 == 0 else sizes - 1 - offset)
                image = app.rr(width, 64, 8, '#e8eee8', '#ffffff')
                canvas.itemconfigure(item, image=image)
                assert app.imgs.bytes <= app.imgs.max_bytes
            gc.collect()
            snapshots.append(dict(round=round_, cached_bytes=app.imgs.bytes,
                                  images=len(app.imgs), **memory_diagnostics.process_memory()))
        warmed = snapshots[3:]
        if warmed:
            commitment = [s['private_commit_bytes'] for s in warmed]
            # Allow native allocator slack after warmup, while detecting the old
            # hundreds-of-MiB retention pattern. Resident paging is not the metric.
            assert max(commitment) - min(commitment) <= 16 * 1024 * 1024, snapshots
        canvas.destroy()
        del image
        app.imgs.clear()
        gc.collect()
        assert set(root.tk.call('image', 'names')) == baseline
        return {'rounds': rounds, 'sizes_per_round': sizes, 'samples': snapshots,
                'native_images_released': True}
    finally:
        if canvas.winfo_exists():
            canvas.destroy()
        app.imgs.clear()

try:
    measured('released-overbudget-cache-hit', release_then_hit)
    measured('canvas-alternate-hidden-image-pinning', canvas_variants)
    measured('shared-widget-owners', shared_widget_owners)
    measured('churn-accounting-cleanup', churn)
    measured('repeated-hit-no-render', repeat_hits)
    measured('repeated-resize-memory-soak', resize_soak)
    assert not root.winfo_ismapped()
finally:
    root.destroy()

report = {'checks': checks, 'timings': timings,
          'failed': sum(not check['passed'] for check in checks),
          'scope': 'real Tk image cache only; no model, window mapping, focus or user input'}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({'output': str(args.output), 'passed': len(checks) - report['failed'],
                  'failed': report['failed']}), flush=True)
raise SystemExit(bool(report['failed']))
