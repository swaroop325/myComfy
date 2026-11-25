"""Text-to-speech via the Piper CLI (voice en_US-lessac-medium)."""
from __future__ import annotations

import os
import subprocess
import wave
from typing import Callable

DEFAULT_MODEL = "/opt/seriesmaker/voices/en_US-lessac-medium.onnx"
TIMEOUT_S = 120


class TtsError(RuntimeError):
    pass


def voice_model() -> str:
    return os.environ.get("SERIESMAKER_PIPER_MODEL", DEFAULT_MODEL)

Runner = Callable[..., "subprocess.CompletedProcess"]


def wav_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def synthesize(text: str, out_path: str, *, model: str | None = None,
               runner: Runner = subprocess.run) -> float:
    """Write a wav narration of ``text`` to out_path; return its duration in seconds."""
    model = model or voice_model()
    try:
        result = runner(
            ["piper", "--model", model, "-f", out_path],  # -f = output file (piper-tts 1.3)
            input=text.encode(), capture_output=True, check=False, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise TtsError(f"piper timed out after {TIMEOUT_S}s") from e
    except OSError as e:
        raise TtsError(f"piper could not run: {e}") from e
    if result.returncode != 0:
        raise TtsError(f"piper failed: {result.stderr[-200:]!r}")
    return wav_duration(out_path)
