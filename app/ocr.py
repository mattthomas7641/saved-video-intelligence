"""Extract on-screen text (promo codes, overlays) from sampled video frames."""
import shutil
import subprocess
import tempfile
from pathlib import Path


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def extract_on_screen_text(video_path: str, duration_seconds: float | None, num_frames: int = 4) -> str:
    """Best-effort OCR over a handful of evenly-spaced frames. Returns "" if
    ffmpeg/tesseract aren't installed rather than failing the whole pipeline.
    """
    if not video_path or not _ffmpeg_available() or not _tesseract_available():
        return ""

    import pytesseract
    from PIL import Image

    duration = duration_seconds or 15
    timestamps = [max(0.5, duration * (i + 1) / (num_frames + 1)) for i in range(num_frames)]

    texts = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, ts in enumerate(timestamps):
            frame_path = Path(tmp) / f"frame_{i}.png"
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                        "-frames:v", "1", "-q:v", "2", str(frame_path),
                    ],
                    check=True, capture_output=True, timeout=30,
                )
                if frame_path.exists():
                    text = pytesseract.image_to_string(Image.open(frame_path))
                    text = text.strip()
                    if text:
                        texts.append(text)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue

    # De-duplicate near-identical lines across frames while preserving order.
    seen = set()
    deduped = []
    for block in texts:
        for line in block.splitlines():
            line = line.strip()
            if line and line.lower() not in seen:
                seen.add(line.lower())
                deduped.append(line)
    return "\n".join(deduped)
