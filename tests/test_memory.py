"""Memory defaults and genuine Windows counters; no microphone/clipboard/UI."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memory_diagnostics import MemoryReport, loaded_libraries, process_memory

flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
environment = os.environ.copy()
environment.pop("OPENBLAS_NUM_THREADS", None)
environment.pop("CT2_PACKED_GEMM", None)
code = ('import murmur,os; '
        'assert os.environ["OPENBLAS_NUM_THREADS"]=="1"; '
        'assert os.environ["CT2_PACKED_GEMM"]=="0"; '
        'assert "faster_whisper" not in __import__("sys").modules; print("defaults applied before backend import")')
subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=environment, check=True,
               capture_output=True, creationflags=flags)
environment.update(OPENBLAS_NUM_THREADS="2", CT2_PACKED_GEMM="1")
subprocess.run([sys.executable, "-c", 'import murmur,os; assert os.environ["OPENBLAS_NUM_THREADS"]=="2"; assert os.environ["CT2_PACKED_GEMM"]=="1"'],
               cwd=ROOT, env=environment, check=True, capture_output=True, creationflags=flags)
memory = process_memory()
assert memory["private_commit_bytes"] > 0 and memory["resident_bytes"] > 0
assert memory["peak_commit_bytes"] >= memory["private_commit_bytes"]
report = MemoryReport()
report.mark("start")
report.mark("after")
assert len(report.stages) == 2 and report.stages[1]["elapsed_seconds"] >= report.stages[0]["elapsed_seconds"]
assert report.stages[1]["commit_change_bytes"] == report.stages[1]["private_commit_bytes"] - report.stages[0]["private_commit_bytes"]
libraries = loaded_libraries()
assert libraries and any("python" in item["name"].lower() for item in libraries)
assert all(item["image_bytes"] > 0 and "\\" not in item["name"] and "/" not in item["name"] for item in libraries)
import murmur
import memory_diagnostics
original_claim = murmur.claim_instance
original_save = murmur.save_config
original_profile = memory_diagnostics.run_profile
received = []
def forbidden(*args, **kwargs):
    raise AssertionError("Diagnostic mode must not claim the normal instance or write config")
try:
    murmur.claim_instance = murmur.save_config = forbidden
    memory_diagnostics.run_profile = lambda cfg, vocab, path: received.append((cfg, path)) or 0
    with tempfile.TemporaryDirectory(prefix="murmur-memory-routing-") as temporary:
        config = Path(temporary) / 'missing-config.json'
        destination = Path(temporary) / 'report.json'
        assert murmur.main(['--config', str(config), '--diagnose-memory', str(destination), '--model', 'tiny.en']) == 0
        assert not config.exists()
        assert received[-1][0]['model'] == 'tiny.en' and received[-1][1] == destination
finally:
    murmur.claim_instance, murmur.save_config = original_claim, original_save
    memory_diagnostics.run_profile = original_profile
print("memory defaults, explicit overrides, native counters and library inventory ok")
print("diagnostic routing bypasses singleton/UI and does not create config")
