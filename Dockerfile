FROM python:3.11-slim

# ffmpeg (frame extraction/audio) + tesseract (OCR) so users don't need to
# install anything themselves.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8787

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
