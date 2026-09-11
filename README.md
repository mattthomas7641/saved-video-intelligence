# TikTok Saved Scanner

Go through your entire TikTok Saved/Favorites list, transcribe + summarize each
video, categorize it, score whether it's worth rewatching, and flag anything
with a promo code or dated offer that might be stale.

Runs entirely on your own Mac. Nothing is scraped live from TikTok — it works
from your own official TikTok data export, which is the only reliable and
ToS-compliant way to get your Saved list.

## How it works

1. **Import** — you request your TikTok data export and upload the JSON file.
2. **Download** — `yt-dlp` fetches each saved video + its caption/thumbnail.
3. **Transcribe** — local Whisper (`faster-whisper`) transcribes the audio. Free, runs on-device.
4. **OCR** — a few frames are sampled and OCR'd to catch on-screen text (promo codes, overlays) speech misses.
5. **Analyze** — Claude reads the transcript/caption/OCR text and returns a category, summary, tags, a 1-5 "worth rewatching" score, and flags for promo codes / dated offers / "link in bio".
6. **Relevance check** — anything with a promo code or dated offer, saved more than a few months ago, gets flagged "⚠️ verify before use". This is a heuristic, not a live check — it won't confirm the code still works, just warns you to check.

## One-time setup

### 1. Install system dependencies (Homebrew)

You don't have Homebrew installed yet. Install it first:

```bash
curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o /tmp/brew-install.sh && bash /tmp/brew-install.sh
```

This will ask for your Mac password and print a couple of `export PATH=...`
lines at the end — run those (or open a new terminal) before continuing.

Then install `ffmpeg` (video frame extraction, audio handling) and
`tesseract` (OCR):

```bash
brew install ffmpeg tesseract
```

### 2. Python environment

From this project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Add your Anthropic API key

```bash
cp .env.example .env
```

Open `.env` and paste your key from https://console.anthropic.com/ into
`ANTHROPIC_API_KEY=`. Don't share this file or commit it — it's already in
`.gitignore`.

### 4. Get your TikTok data export

In the TikTok app: **Profile → ☰ → Settings and privacy → Account → Download
your data**, request the **JSON** format. TikTok emails/notifies you when
it's ready (can take a few minutes to a few days). Download and unzip it.

## Running it

```bash
source .venv/bin/activate
python run.py
```

This opens `http://127.0.0.1:8787` in your browser. First run will walk you
through uploading your export, then click **Process saved videos** on the
dashboard to start the pipeline. It processes one video at a time in the
background — you can keep using the dashboard while it runs, and it resumes
where it left off if you stop and restart the app.

## Notes on cost/time

- Transcription runs locally — free, but CPU-bound. Expect roughly
  real-time-ish processing per video with the default `base` Whisper model
  (a 30s video takes roughly 10-30s to transcribe on a typical Mac).
- Claude analysis is the only thing that costs money, and it's cheap — a
  short transcript + caption in, small JSON out, using Haiku by default.
  A few hundred videos should cost low single-digit dollars.
- Downloaded video files are deleted after processing by default (keeps
  disk usage small); set `DELETE_VIDEO_AFTER_PROCESS=false` in `.env` if you
  want to keep them.

## Project layout

```
app/
  main.py        FastAPI routes
  models.py      Video table (SQLModel)
  ingest.py      Parses the TikTok export
  download.py    yt-dlp wrapper
  transcribe.py  faster-whisper wrapper
  ocr.py         ffmpeg frame sampling + tesseract OCR
  analyze.py     Claude categorization/summarization
  relevance.py   Staleness heuristic
  pipeline.py    Orchestrates one video through all steps
  worker.py      Background thread that processes the pending queue
templates/       Jinja2 pages (dashboard, video detail, onboarding, settings)
data/            SQLite DB + thumbnails (gitignored)
```
