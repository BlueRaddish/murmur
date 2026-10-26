"""Explicit, content-free Windows memory diagnostics. No third-party dependency.

Commit is private memory backed by RAM/pagefile; RSS is the current working set.
Stage deltas include native allocations. DLL image size is inventory, not heap
attribution, and must not be added to commit as if it were private memory.
"""
import ctypes
import gc
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time


def process_memory() -> dict:
    if os.name != "nt":
        raise RuntimeError("This diagnostic currently requires Windows process counters.")

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"private_commit_bytes": counters.PrivateUsage,
            "resident_bytes": counters.WorkingSetSize,
            "peak_commit_bytes": counters.PeakPagefileUsage,
            "peak_resident_bytes": counters.PeakWorkingSetSize}


def loaded_libraries() -> list:
    """Names and image sizes only, not user paths or a claim of per-DLL private RAM."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong]
    kernel.GetModuleFileNameW.restype = ctypes.c_ulong
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.EnumProcessModules.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    psapi.EnumProcessModules.restype = ctypes.c_int

    class ModuleInfo(ctypes.Structure):
        _fields_ = [("base", ctypes.c_void_p), ("size", ctypes.c_ulong), ("entry", ctypes.c_void_p)]

    psapi.GetModuleInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ModuleInfo), ctypes.c_ulong]
    psapi.GetModuleInformation.restype = ctypes.c_int
    handle = kernel.GetCurrentProcess()
    array = (ctypes.c_void_p * 4096)()
    needed = ctypes.c_ulong()
    if not psapi.EnumProcessModules(handle, array, ctypes.sizeof(array), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if needed.value > ctypes.sizeof(array):
        raise RuntimeError("Loaded-library inventory exceeded the diagnostic limit.")
    result = []
    for module in array[:needed.value // ctypes.sizeof(ctypes.c_void_p)]:
        name = ctypes.create_unicode_buffer(32768)
        info = ModuleInfo()
        if not kernel.GetModuleFileNameW(module, name, len(name)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not psapi.GetModuleInformation(handle, module, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        result.append({"name": Path(name.value).name, "image_bytes": info.size})
    return sorted(result, key=lambda item: item["image_bytes"], reverse=True)


class MemoryReport:
    def __init__(self):
        self.stages = []
        self.started = time.perf_counter()

    def mark(self, stage: str) -> None:
        row = {"stage": stage, "elapsed_seconds": round(time.perf_counter() - self.started, 4),
               **process_memory()}
        row["commit_change_bytes"] = row["private_commit_bytes"] - (
            self.stages[-1]["private_commit_bytes"] if self.stages else 0)
        self.stages.append(row)


def run_profile(cfg: dict, vocab: str, destination: Path) -> int:
    """The real constructor/decoder, without claiming the running app's singleton.

    Cached-only loading avoids downloads and any profile/config modification.
    No microphone stream, keyboard listener, tray, clipboard or live Hub starts.
    """
    import numpy as np
    from murmur import Murmur

    report = MemoryReport()
    report.mark("app_imported")
    data = {"schema": 1, "pid": os.getpid(), "model": cfg["model"], "device": cfg["device"],
            "notes": "Stage deltas include native allocations; DLL image sizes are not private RAM. Synthetic silence does not measure real dictation latency or quality.",
            "environment": {key: os.environ.get(key) for key in
                            ("OPENBLAS_NUM_THREADS", "CT2_PACKED_GEMM", "KMP_BLOCKTIME")},
            "stages": report.stages, "success": False}
    try:
        # Exercise production Murmur methods, keeping the diagnostic offline.
        class OfflineMurmur(Murmur):
            @staticmethod
            def load_model(model_type, name, device, compute):
                return model_type(name, device=device, compute_type=compute, local_files_only=True)

        app = OfflineMurmur(cfg, vocab, on_memory=report.mark)
        report.mark("ready")
        start = time.perf_counter()
        text = app.transcribe(np.zeros(16000 * 6, dtype=np.float32))
        data["silence_decode_seconds"] = round(time.perf_counter() - start, 4)
        data["silence_result_empty"] = not bool(text)
        report.mark("silence_decoded")
        data["libraries"] = loaded_libraries()
        data["retained_audio_bytes"] = sum(chunk.nbytes for chunk in app.recorder._chunks)
        del app
        gc.collect()
        report.mark("model_released")
        data["versions"] = {}
        for name in ("numpy", "faster-whisper", "ctranslate2", "onnxruntime"):
            try:
                data["versions"][name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                data["versions"][name] = "metadata not bundled"
        data["success"] = True
    except Exception as error:
        data["error"] = str(error)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".new")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return 0 if data["success"] else 1
