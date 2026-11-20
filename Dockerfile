FROM python:3.11-slim

# ffmpeg (video handling) + tesseract (OCR for screenshots) so nothing needs
# installing by hand.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Untrusted media (yt-dlp downloads, ffmpeg, tesseract) shouldn't be handled as root.
RUN useradd --create-home --uid 1000 sidequest && chown -R sidequest /app
USER sidequest

ENV PYTHONUNBUFFERED=1
EXPOSE 8787

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
