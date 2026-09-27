"""Local speech-to-text via faster-whisper (one shared model, safe for several worker threads)."""
import threading

from app.config import WHISPER_MODEL

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            # num_workers lets several threads transcribe at once; cpu_threads keeps each one modest.
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=2, num_workers=4)
        return _model


def transcribe(video_path: str) -> str:
    model = _get_model()
    try:
        segments, _info = model.transcribe(video_path, beam_size=1, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()
    except (ValueError, IndexError):
        # No usable audio (silent clip, photo slideshow, music only): treat as no speech.
        return ""
