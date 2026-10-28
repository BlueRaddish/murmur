"""Real Tk image lifetimes and production rendering; all windows stay withdrawn."""
import base64
from pathlib import Path
import sys
import tkinter as tk
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import window as W

root = tk.Tk()
root.withdraw()
try:
    cache = W._ImageCache(max_bytes=512)
    def photo(colour='#135724'):
        image = tk.PhotoImage(master=root, width=8, height=8)
        image.put(colour, to=(0, 0, 8, 8))
        return image

    # Eviction cannot blank either a Label or a Canvas, including hidden widgets.
    label_image = cache.image('label', photo)
    label = tk.Label(root, image=label_image)
    canvas_image = cache.image('canvas', photo)
    canvas = tk.Canvas(root)
    item = canvas.create_image(0, 0, image=canvas_image)
    names = [str(label_image), str(canvas_image)]
    del label_image, canvas_image
    for i in range(20):
        cache.image(i, photo)
    for name in names:
        assert root.tk.getboolean(root.tk.call('image', 'inuse', name))
        assert root.tk.call(name, 'get', 0, 0) == (19, 87, 36)
    assert len(cache) == 3  # two live instances + the newest return value
    label.destroy()
    canvas.destroy()
    cache.image('after release', photo)
    assert len(cache) == 2 and cache.bytes <= cache.max_bytes
    assert not set(names) & set(root.tk.call('image', 'names'))

    # An all-pinned cache can exceed the budget legitimately. Releasing those
    # widgets and hitting an existing key must trim it without another render.
    overflow = W._ImageCache(max_bytes=512)
    labels = [tk.Label(root, image=overflow.image(i, photo)) for i in range(96)]
    assert overflow.bytes == 96 * 256
    for label in labels:
        label.destroy()
    keep = overflow.image(95, lambda: 1/0)
    assert keep.get(0, 0) == (19, 87, 36)
    assert overflow.bytes <= overflow.max_bytes
    overflow.clear()

    # Reuse is LRU and a hit does not invoke the renderer again.
    cache = W._ImageCache(max_bytes=512)
    first = cache.image('first', photo)
    old = weakref.ref(cache.image('old', photo))
    assert cache.image('first', lambda: 1/0) is first
    cache.image('new', photo)
    assert old() is None and len(cache) == 2
    cache.clear()
    assert len(cache) == 0 and cache.bytes == 0

    # Go through AppWindow's real rendering API at many sizes. Compare exact PNG
    # pixels to a fresh uncached Tk image; the cache cannot change the appearance.
    ui = W.AppWindow(root, None, {}, lambda _: None)
    for width in (80, 400, 999, 120):
        actual = ui.rr(width, 64, 8, '#e8eee8', '#ffffff')
        expected = tk.PhotoImage(master=root, data=base64.b64encode(
            W.rr_png(width, 64, 8, '#e8eee8', ground='#ffffff')).decode())
        assert root.tk.call(str(actual), 'data', '-format', 'png') == root.tk.call(
            str(expected), 'data', '-format', 'png')
    for width in range(400, 1000):
        ui.rr(width, 64, 8, '#e8eee8', '#ffffff')
    assert ui.imgs.bytes <= ui.imgs.max_bytes
    assert len(ui.imgs) < 50
    # A single large image remains valid even when larger than the reuse budget.
    tiny = W._ImageCache(max_bytes=1)
    oversized = tiny.image('large', photo)
    assert oversized.get(0, 0) == (19, 87, 36) and len(tiny) == 1
    assert not root.winfo_ismapped()
finally:
    root.destroy()
print('Tk cache: live Label/Canvas protection, released-image eviction, LRU reuse, pixel equality and bounded resize growth passed')
