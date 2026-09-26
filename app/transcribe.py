"""Local speech-to-text via faster-whisper."""
from functools import lru_cache

from app.config import WHISPER_MODEL


@lru_cache(maxsize=1)
def _get_model():
    from faster_whisper import WhisperModel
    return WhisperModel(WHISPER_MODEL, device="auto", compute_type="int8")


def transcribe(video_path: str) -> str:
    model = _get_model()
    try:
        segments, _info = model.transcribe(video_path, beam_size=5, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()
    except (ValueError, IndexError):
        # No usable audio (silent clip, photo slideshow, music only): faster-whisper
        # fails language detection. Treat as no speech; caption/OCR still get analyzed.
        return ""
