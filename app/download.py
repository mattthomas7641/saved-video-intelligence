"""Download a TikTok video + metadata via yt-dlp."""
from datetime import datetime
from pathlib import Path

import yt_dlp

from app.config import VIDEOS_DIR, THUMBS_DIR


class DownloadResult:
    def __init__(self, video_path, thumbnail_path, author, caption, hashtags,
                 upload_date, duration_seconds):
        self.video_path = video_path
        self.thumbnail_path = thumbnail_path
        self.author = author
        self.caption = caption
        self.hashtags = hashtags
        self.upload_date = upload_date
        self.duration_seconds = duration_seconds


def download_video(url: str, video_id: int) -> DownloadResult:
    out_template = str(VIDEOS_DIR / f"{video_id}.%(ext)s")
    thumb_template = str(THUMBS_DIR / f"{video_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": {"default": out_template, "thumbnail": thumb_template},
        # Prefer H.264: TikTok's HEVC (bytevc1) variants sometimes arrive without an audio track.
        "format": "download/best[vcodec=h264]/best",
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            if "No video formats" not in str(e):
                raise
            return _photo_post_fallback(ydl, url, video_id)

    video_path = Path(ydl.prepare_filename(info))
    if not video_path.exists():
        # yt-dlp may have merged/remuxed to a different extension
        candidates = list(VIDEOS_DIR.glob(f"{video_id}.*"))
        candidates = [c for c in candidates if c.suffix.lower() in (".mp4", ".webm", ".mkv")]
        video_path = candidates[0] if candidates else None

    thumb_candidates = list(THUMBS_DIR.glob(f"{video_id}.*"))
    thumb_path = thumb_candidates[0] if thumb_candidates else None

    upload_date = None
    raw_date = info.get("upload_date")  # YYYYMMDD
    if raw_date:
        try:
            upload_date = datetime.strptime(raw_date, "%Y%m%d")
        except ValueError:
            upload_date = None

    hashtags = ",".join(info.get("tags") or [])

    return DownloadResult(
        video_path=str(video_path) if video_path else None,
        thumbnail_path=str(thumb_path) if thumb_path else None,
        author=info.get("uploader") or info.get("channel"),
        caption=info.get("description") or info.get("title"),
        hashtags=hashtags,
        upload_date=upload_date,
        duration_seconds=info.get("duration"),
    )


def _photo_post_fallback(ydl, url: str, video_id: int) -> DownloadResult:
    """Photo/slideshow posts have no video stream. Keep the metadata + a
    thumbnail so the caption and hashtags can still be analyzed."""
    import urllib.request

    info = ydl.extract_info(url, download=False, process=False)
    thumb_path = None
    thumbs = info.get("thumbnails") or []
    if thumbs:
        try:
            dest = THUMBS_DIR / f"{video_id}.jpg"
            urllib.request.urlretrieve(thumbs[0]["url"], dest)
            thumb_path = str(dest)
        except Exception:  # noqa: BLE001
            thumb_path = None

    upload_date = None
    if info.get("upload_date"):
        try:
            upload_date = datetime.strptime(info["upload_date"], "%Y%m%d")
        except ValueError:
            pass

    return DownloadResult(
        video_path=None,
        thumbnail_path=thumb_path,
        author=info.get("uploader") or info.get("channel"),
        caption=info.get("description") or info.get("title"),
        hashtags=",".join(info.get("tags") or []),
        upload_date=upload_date,
        duration_seconds=None,
    )
