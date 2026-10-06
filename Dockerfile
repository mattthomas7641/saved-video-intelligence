FROM python:3.11-slim

# ffmpeg (frame extraction/audio) + tesseract (OCR) so users don't need to
# install anything themselves, plus the headless-Chromium runtime libs
# Playwright needs for app/scraper.py (TikTok Saved-page sync). Listed
# explicitly rather than via `playwright install --with-deps` — that command
# pulls in some font packages (ttf-unifont, ttf-ubuntu-font-family) that
# don't resolve on arm64 Debian; none of them are needed just to load pages
# and read links headlessly.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg tesseract-ocr \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Browser binary only (no --with-deps — system libs are installed above).
RUN playwright install chromium

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8787

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
