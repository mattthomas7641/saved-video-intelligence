# TikTok Saved Scanner

Go through your entire TikTok Saved/Favorites list, transcribe + summarize each
video, categorize it, score whether it's worth rewatching, and flag anything
with a promo code or dated offer that might be stale.

Runs entirely on your own machine (or your own server). You can feed it from
your own official TikTok data export (ToS-compliant, but manual — see
**How it works**), or opt into an optional daily sync that reads your Saved
page directly using your own logged-in session (see **Daily agent**, below —
this one does scrape TikTok, against their terms, and you have to set it up
deliberately; it's off by default).

**This is a self-hosted, single-user app, by design.** There's no central
service you sign up for — you (or anyone else who wants this) run your own
private copy, against your own TikTok export, with your own Anthropic API
key. That keeps it cheap (a few dollars in API usage for a few hundred
videos), keeps your data yours, and avoids the much bigger legal/ToS exposure
a centralized service that downloads *other people's* TikTok content on their
behalf would carry. If you want to host it somewhere other than your own
Mac, see **Deploy your own copy** below.

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

## Daily agent (optional, not yet scheduled)

Beyond summarizing, the app can detect when a saved video points at something
to actually *do* — a skill/technique to adopt, a project to build, or a job
to apply to — and queue it as an `Action`. A separately-scheduled daily
routine can then pick up that queue and do real work: draft-PR a built
version of a project idea, or draft a tailored resume/cover letter for a job
lead. **Submitting a real job application is never automated** — that step
is always yours, by design, not just by default.

This ships in two parts, deliberately not turned all the way on yet:

- **Built and working now**: action detection (part of the normal analysis
  step, no extra cost), the Ideas (`/actions`) and Jobs (`/jobs`) pages, and
  the authenticated `/api/*` routes a future scheduled agent will call.
- **Built but needs your one-time setup**: `app/scraper.py` reads your Saved
  page using a logged-in session you create yourself:
  ```bash
  source .venv/bin/activate
  python -m app.scraper login
  ```
  This opens a real, visible browser — log into TikTok there yourself (this
  app never sees or handles your TikTok credentials), confirm you can see
  your Favorites tab, then close the window. It saves the session to
  `data/tiktok_auth_state.json` (gitignored). Run `python -m app.scraper`
  afterward to confirm it can read your saved links; if it raises
  `SelectorMismatch`, TikTok's page structure didn't match what the script
  expects — see the checklist in `app/scraper.py`'s docstring.
- **Not yet done**: actually registering a daily schedule. That's a
  deliberate, separate step — only worth doing once you've confirmed the
  login session survives unattended across a real day's gap, and reviewed
  one full manual dry run by hand.

Settings has toggles to disable skill/project/job detection individually, a
pause switch for the whole agent, and where to paste a base resume for job
tailoring. Two different costs are involved once this runs for real: the
app's own Anthropic key (for detection, already spend-capped) and whatever
the daily agent session itself spends doing the actual drafting/building.

## Analyzing thousands of videos

Open **Analyze all** in the top bar. The work is split so the slow part is free:

1. **Collect** (free, on your Mac): download, transcribe and read on-screen text, several videos at once (default 4). Roughly 12 seconds of work per video, so about 6 hours for 7,000 videos at 4 at a time. It backs off automatically if TikTok pushes back.
2. **Analyze** (uses Claude credit): either the **Batch API** (half price, results usually within an hour or two) or **live**. Set a spending cap; the newest saves are analyzed first, so a capped run covers what you saved most recently.

Measured cost per video with the default Haiku model: about $0.0027 live, $0.0014 via batch. Everything is saved as it goes, so you can stop and resume at any time. To keep a Mac awake for a long run: `caffeinate -dims`.

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

## Deploy your own copy

Prefer not to install Homebrew/Python locally? Use Docker — it bundles
ffmpeg and tesseract for you.

```bash
cp .env.example .env   # add your ANTHROPIC_API_KEY
docker compose up --build
```

Then open `http://localhost:8787`.

### Host it on a server instead of your Mac

The Docker image runs anywhere Docker does — a spare machine, a VPS, or a
PaaS like Render or Railway. One-click option for Render:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/mattthomas7641/tiktok-saved-scanner)

It'll ask for your `ANTHROPIC_API_KEY` during setup. A couple of things to
know before you do this:

- **This app has no login screen.** Anyone with the URL can use it and see
  your saved videos. If you deploy it somewhere public, put it behind your
  host's access control / a basic-auth proxy, or keep it on a private
  network.
- **Whisper transcription is CPU-heavy.** Free hosting tiers will be slow or
  may time out on longer videos — a small paid instance handles it fine.
- Processing still downloads each video from TikTok server-side, so it
  should only be used against **your own** export — not repurposed into a
  shared service that downloads other users' TikTok content, which is a much
  higher-risk use of `yt-dlp` against TikTok's terms.

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
