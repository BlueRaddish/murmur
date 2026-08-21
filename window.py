"""The app window: transcript history plus settings. Hidden until opened from the tray.

tkinter/ttk; lives on the main thread with the overlay timer. Everything here is
plain widgets on purpose - the overlay is where the polish went.
"""
import json
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "tiny", "base", "small", "medium", "large-v3"]


class History:
    """Append-only JSONL of {"t": epoch, "text": ...}; entries older than the retention
    window are dropped on load and on every append."""

    def __init__(self, path: Path, days: float):
        self.path = path
        self.days = days
        self.items: list = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        self.items = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    self.items.append(json.loads(line))
                except ValueError:
                    continue
        self.prune()

    def prune(self) -> bool:
        cutoff = time.time() - self.days * 86400
        kept = [i for i in self.items if i["t"] >= cutoff]
        changed = len(kept) != len(self.items)
        self.items = kept
        if changed:
            self.save()
        return changed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in self.items),
                             encoding="utf-8")

    def append(self, text: str) -> None:
        self.items.append({"t": time.time(), "text": text})
        if not self.prune():
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.items[-1], ensure_ascii=False) + "\n")

    def delete(self, idx: int) -> None:
        del self.items[idx]
        self.save()

    def clear(self) -> None:
        self.items = []
        self.save()


class AppWindow:
    """root: the (withdrawn) Tk root. cfg: the live config dict shared with the app.
    on_save(cfg): persist settings."""

    def __init__(self, root: tk.Tk, history: History, cfg: dict, on_save, scale: float = 1.0):
        self.root = root
        self.history = history
        self.cfg = cfg
        self.on_save = on_save
        self.win = None
        self.scale = scale

    # --- lifecycle ------------------------------------------------------------
    def show(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self.refresh()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def hide(self) -> None:
        if self.win is not None:
            self.win.withdraw()

    def _build(self) -> None:
        s = self.scale
        w = tk.Toplevel(self.root)
        w.title("murmur")
        w.geometry(f"{int(720 * s)}x{int(520 * s)}")
        w.minsize(int(520 * s), int(360 * s))
        w.protocol("WM_DELETE_WINDOW", self.hide)
        self.win = w
        style = ttk.Style(w)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        # ttk does not scale row height with DPI; fonts are in points so they do
        style.configure("Treeview", rowheight=int(24 * s), font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10))
        style.configure(".", font=("Segoe UI", 10))
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_history(nb)
        self._build_settings(nb)

    def _build_history(self, nb) -> None:
        f = ttk.Frame(nb)
        nb.add(f, text="History")
        cols = ("time", "text")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("time", text="When")
        self.tree.heading("text", text="Text")
        self.tree.column("time", width=int(130 * self.scale), stretch=False, anchor="w")
        self.tree.column("text", width=int(500 * self.scale), anchor="w")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        sb.grid(row=0, column=1, sticky="ns", pady=4)
        self.tree.bind("<Double-1>", lambda e: self.copy_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_full())

        self.full = tk.Text(f, height=4, wrap="word", relief="flat", state="disabled",
                            font=("Segoe UI", 10))
        self.full.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 4))

        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 4))
        ttk.Button(bar, text="Copy", command=self.copy_selected).pack(side="left")
        ttk.Button(bar, text="Delete", command=self.delete_selected).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Clear all", command=self.clear_all).pack(side="left", padx=(6, 0))
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="right")
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)

    def _build_settings(self, nb) -> None:
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Settings")
        f.columnconfigure(0, pad=16)
        r = 0

        ttk.Label(f, text="Keep history for").grid(row=r, column=0, sticky="w", pady=4)
        self.v_days = tk.StringVar(value=str(self.cfg["retention_days"]))
        box = ttk.Frame(f)
        box.grid(row=r, column=1, sticky="w")
        ttk.Spinbox(box, from_=0, to=3650, width=6, textvariable=self.v_days).pack(side="left")
        ttk.Label(box, text="days   (0 = don't keep history)").pack(side="left", padx=(6, 0))
        r += 1

        self.v_headset = tk.BooleanVar(value=bool(self.cfg["headset_button"]))
        ttk.Checkbutton(f, text="Wired-headset button (Play/Pause) starts and stops recording",
                        variable=self.v_headset).grid(row=r, column=0, columnspan=2, sticky="w", pady=4)
        r += 1

        ttk.Label(f, text="Model").grid(row=r, column=0, sticky="w", pady=4)
        self.v_model = tk.StringVar(value=self.cfg["model"])
        ttk.Combobox(f, textvariable=self.v_model, values=MODELS, width=14).grid(row=r, column=1, sticky="w")
        r += 1

        ttk.Label(f, text="Microphone").grid(row=r, column=0, sticky="w", pady=4)
        self.mics = self._list_mics()
        names = ["(system default)"] + [f"{i}: {n}" for i, n in self.mics]
        cur = self.cfg["mic"]
        sel = names[0]
        for i, n in self.mics:
            if cur == i or (isinstance(cur, str) and cur and cur.lower() in n.lower()):
                sel = f"{i}: {n}"
                break
        if cur is not None and sel == names[0]:   # configured but not in the list: keep it, don't silently reset
            sel = f"{cur}: (as configured)"
            names.insert(1, sel)
        self.v_mic = tk.StringVar(value=sel)
        ttk.Combobox(f, textvariable=self.v_mic, values=names, width=46, state="readonly").grid(
            row=r, column=1, sticky="w")
        r += 1

        ttk.Label(f, text="Language").grid(row=r, column=0, sticky="w", pady=4)
        self.v_lang = tk.StringVar(value=self.cfg["language"] or "")
        box = ttk.Frame(f)
        box.grid(row=r, column=1, sticky="w")
        ttk.Entry(box, textvariable=self.v_lang, width=6).pack(side="left")
        ttk.Label(box, text="blank = auto (en for *.en models)").pack(side="left", padx=(6, 0))
        r += 1

        ttk.Label(f, text="Accent colour").grid(row=r, column=0, sticky="w", pady=4)
        self.v_color = tk.StringVar(value=self.cfg.get("color", "#e63c3c"))
        box = ttk.Frame(f)
        box.grid(row=r, column=1, sticky="w")
        ttk.Entry(box, textvariable=self.v_color, width=9).pack(side="left")
        self.sw_color = tk.Label(box, width=3, relief="solid", bd=1)
        self.sw_color.pack(side="left", padx=(6, 0))
        ttk.Label(box, text="hex, e.g. #e63c3c - recording and persistent; applies on Save").pack(side="left", padx=(8, 0))
        self.v_color.trace_add("write", lambda *_: self._swatch(self.sw_color, self.v_color))
        self._swatch(self.sw_color, self.v_color)
        r += 1

        ttk.Label(f, text="Transcribing colour").grid(row=r, column=0, sticky="w", pady=4)
        self.v_color_busy = tk.StringVar(value=self.cfg.get("color_busy", "#ffaa32"))
        box = ttk.Frame(f)
        box.grid(row=r, column=1, sticky="w")
        ttk.Entry(box, textvariable=self.v_color_busy, width=9).pack(side="left")
        self.sw_busy = tk.Label(box, width=3, relief="solid", bd=1)
        self.sw_busy.pack(side="left", padx=(6, 0))
        ttk.Label(box, text="the pulse while text is being typed").pack(side="left", padx=(8, 0))
        self.v_color_busy.trace_add("write", lambda *_: self._swatch(self.sw_busy, self.v_color_busy))
        self._swatch(self.sw_busy, self.v_color_busy)
        r += 1

        ttk.Separator(f).grid(row=r, column=0, columnspan=2, sticky="ew", pady=10)
        r += 1
        ttk.Button(f, text="Save", command=self.save).grid(row=r, column=0, sticky="w")
        self.note = ttk.Label(f, text="Model, microphone and language apply after a restart.",
                              foreground="#666")
        self.note.grid(row=r, column=1, sticky="w")
        f.columnconfigure(1, weight=1)

    @staticmethod
    def _swatch(label, var) -> None:
        v = var.get().strip()
        ok = len(v.lstrip("#")) in (3, 6) and all(c in "0123456789abcdefABCDEF" for c in v.lstrip("#"))
        try:
            label.config(bg=("#" + v.lstrip("#")) if ok else "#ffffff")
        except tk.TclError:
            label.config(bg="#ffffff")

    @staticmethod
    def _list_mics():
        try:
            import sounddevice as sd
            out = []
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and d["hostapi"] == 0:  # first host API (MME): one entry per mic
                    out.append((i, d["name"]))
            return out
        except Exception:
            return []

    # --- actions --------------------------------------------------------------
    def refresh(self) -> None:
        if self.win is None:
            return
        self.history.prune()
        self.tree.delete(*self.tree.get_children())
        for idx in range(len(self.history.items) - 1, -1, -1):
            it = self.history.items[idx]
            when = time.strftime("%b %d  %H:%M", time.localtime(it["t"]))
            self.tree.insert("", "end", iid=str(idx), values=(when, it["text"]))
        n = len(self.history.items)
        self.status.config(text=f"{n} item{'s' if n != 1 else ''} in the last {self.cfg['retention_days']:g} days")

    def _selected_idx(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _show_full(self) -> None:
        idx = self._selected_idx()
        self.full.config(state="normal")
        self.full.delete("1.0", "end")
        if idx is not None:
            self.full.insert("1.0", self.history.items[idx]["text"])
        self.full.config(state="disabled")

    def copy_selected(self) -> None:
        idx = self._selected_idx()
        if idx is None:
            return
        import pyperclip
        pyperclip.copy(self.history.items[idx]["text"])
        self.status.config(text="copied")

    def delete_selected(self) -> None:
        idx = self._selected_idx()
        if idx is None:
            return
        self.history.delete(idx)
        self.refresh()

    def clear_all(self) -> None:
        self.history.clear()
        self.refresh()

    def save(self) -> None:
        try:
            days = float(self.v_days.get())
        except ValueError:
            self.note.config(text="Retention must be a number of days.", foreground="#c00")
            return
        self.cfg["retention_days"] = days
        self.history.days = days
        self.cfg["headset_button"] = bool(self.v_headset.get())
        self.cfg["model"] = self.v_model.get().strip() or self.cfg["model"]
        self.cfg["language"] = self.v_lang.get().strip() or None
        for var, key in ((self.v_color, "color"), (self.v_color_busy, "color_busy")):
            v = var.get().strip()
            if len(v.lstrip("#")) in (3, 6) and all(c in "0123456789abcdefABCDEF" for c in v.lstrip("#")):
                self.cfg[key] = "#" + v.lstrip("#").lower()
        mic = self.v_mic.get()
        if mic.startswith("("):
            self.cfg["mic"] = None
        elif not mic.endswith("(as configured)"):
            self.cfg["mic"] = int(mic.split(":")[0])
        self.on_save(self.cfg)  # cfg is the live dict the app reads: headset/retention apply now
        self.note.config(text="Saved. Model, microphone and language apply after a restart.", foreground="#666")
        self.refresh()
