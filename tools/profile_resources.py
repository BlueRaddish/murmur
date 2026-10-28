"""File-only resource probe: hidden Tk renderer or read-only live observation.

Never opens the microphone, clipboard, key listeners, visible windows or Hub.
Window scenario exercises production AppWindow.rr/img and real Tk canvas usage.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def counters(process):
    info = process.memory_info()
    return {'commit_bytes': info.private, 'resident_bytes': info.rss,
            'cpu_seconds': sum(process.cpu_times()[:2]), 'handles': process.num_handles(),
            'threads': process.num_threads(), 'io': process.io_counters()._asdict()}


def main(args):
    import psutil
    if args.live_pid:
        process = psutil.Process(args.live_pid)
        rows = []
        start = time.perf_counter()
        while True:
            rows.append(dict(seconds=time.perf_counter() - start, **counters(process)))
            if time.perf_counter() - start >= args.seconds:
                break
            time.sleep(min(2, args.seconds - (time.perf_counter() - start)))
        result = {'scenario': 'read-only uncontrolled live observation',
                  'pid': process.pid, 'exe': process.exe(), 'created': process.create_time(),
                  'samples': rows, 'gpu_energy_network_bytes': 'not measured'}
    else:
        import tkinter as tk
        target = args.window_file or ROOT / 'window.py'
        spec = importlib.util.spec_from_file_location('resource_probe_window', target)
        window = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = window
        spec.loader.exec_module(window)
        root = tk.Tk()
        root.withdraw()
        scratch = tempfile.TemporaryDirectory(prefix='murmur-render-profile-')
        try:
            prebuild = None
            if args.prebuild:
                import murmur
                app = window.AppWindow(root, window.History(Path(scratch.name) / 'history.jsonl', 7),
                                       dict(murmur.DEFAULTS), lambda _: None, scale=2.0, theme='light')
                begin = time.perf_counter()
                app.prebuild()
                root.update_idletasks()
                prebuild = {'seconds': time.perf_counter() - begin, 'cached_images': len(app.imgs)}
                assert not app.win.winfo_ismapped()
            else:
                app = window.AppWindow(root, None, {}, lambda _: None)
            canvas = tk.Canvas(root)
            item = canvas.create_image(0, 0, anchor='nw')
            process = psutil.Process()
            initial = counters(process)
            points = []
            started, cpu = time.perf_counter(), time.process_time()
            for i in range(args.sizes):
                photo = app.rr(400 + i, 64, 8, '#e8eee8', '#ffffff')
                canvas.itemconfigure(item, image=photo)
                # No mapping/focus: Tk still tracks the real canvas image instance.
                assert root.tk.getboolean(root.tk.call('image', 'inuse', str(photo)))
                if (i + 1) % 100 == 0 or i == args.sizes - 1:
                    points.append(dict(sizes=i+1, cached_images=len(app.imgs), **counters(process)))
            elapsed, work = time.perf_counter() - started, time.process_time() - cpu
            canvas.destroy()
            del photo
            after_release = counters(process)
            estimate = sum(im.width() * im.height() * 4 for im in app.imgs.values())
            result = {'scenario': 'hidden production rounded-background resize stress',
                      'source': str(target), 'source_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                      'sizes': args.sizes, 'wall_seconds': elapsed, 'cpu_seconds': work,
                      'initial': initial, 'samples': points, 'after_widget_release': after_release,
                      'cached_rgba_bytes_estimate': estimate, 'hidden': not root.winfo_ismapped(),
                      'prebuild': prebuild,
                      'gpu_energy_network_bytes': 'not measured'}
            assert result['hidden'] and len(points) > 0
        finally:
            if args.prebuild and app.win is not None:
                app._cancel_all()
            root.destroy()
            scratch.cleanup()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'scenario': result['scenario'],
                      'samples': len(result['samples'])}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window-file', type=Path)
    parser.add_argument('--sizes', type=int, default=600)
    parser.add_argument('--prebuild', action='store_true', help='also measure full hidden window prebuild at 200%% DPI')
    parser.add_argument('--live-pid', type=int)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.sizes < 1 or args.seconds <= 0:
        parser.error('sizes and seconds must be positive')
    main(args)
