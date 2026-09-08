"""PyInstaller runtime hook: stand in for PyAV so the FFmpeg bundle stays out of the build.

faster_whisper/__init__.py imports faster_whisper.audio, whose only module-level need is
`import av`; every use of av (av.open, av.audio.resampler, av.audio.fifo, av.error) sits
inside decode_audio(), and transcribe.py calls decode_audio() only when the input is NOT a
numpy array. murmur always hands WhisperModel.transcribe a numpy array (Recorder ->
np.concatenate -> Murmur.transcribe), so av is import-bound and never executed. Bundling it
cost 66 MB (av.libs: avcodec, libx265, SvtAv1, avfilter ...). build.ps1 excludes the real
package (--exclude-module av) and this hook plants an empty `av` in sys.modules before the
main script runs, so the import succeeds and any real call fails loudly.

Runtime hooks run before murmur.py, ahead of every faster_whisper import. A stub package on
--paths would not work: PyInstaller appends pathex, so site-packages' av would win.
"""
import os
import sys
import types

_MSG = ("audio file decoding is not bundled - murmur feeds Whisper from the microphone "
        "(PyAV/FFmpeg was left out of the build; see stubs/rthook_av.py)")


def _missing(name):
    # Dunder probes are introspection, not a use of PyAV: inspect.getmodule() walks every module
    # in sys.modules asking hasattr(m, '__file__'), and hasattr only swallows AttributeError (an
    # ImportError there broke torch's import in the unfrozen self-check). A single class cannot
    # subclass both (C layout conflict), so public names get the ImportError that PyAV guards expect.
    if name.startswith("__"):
        raise AttributeError(name)
    raise ImportError(_MSG, name="av")


_av = types.ModuleType("av", doc=__doc__)
_av.__path__ = []          # a package with no submodules: `import av.audio` -> ModuleNotFoundError
_av.__getattr__ = _missing  # av.open / av.audio / av.error ... -> ImportError
sys.modules.setdefault("av", _av)

# hf_xet is excluded from the build as well (huggingface_hub falls back to plain HTTP when
# is_xet_available() is False). The check reads package metadata, which a future spec change
# could collect by accident; the env switch makes the fallback deterministic.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
