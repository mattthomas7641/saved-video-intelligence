# Sidequest

*Formerly Saved Video Intelligence.* Send it anything you come across. It does the follow-up and tells you if it's worth your time.

[![CI](https://github.com/mattthomas7641/saved-video-intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/mattthomas7641/saved-video-intelligence/actions/workflows/ci.yml)

Two things, one pipeline:

- **Share a TikTok, get the follow-up done.** Tap Share → Send to Sidequest and a
  research agent (about $0.01–0.03 an item) acts
  on it within minutes: Gemini watches the video, then it vets every GitHub repo
  a "10 repos you need" video names against the live API, traces AI news to its source, turns a food video
  into an address, hours and the best day to go, and judges it against your own
  projects: where it would plug in and what it would do to their
  architecture. The answer arrives as a phone notification; the full report
  lands on the dashboard. See **Send to Sidequest → research agent**, below.
- **Make a 7,000-video Saved list usable.** Transcribe, OCR, categorize and
  summarize your whole TikTok Saved/Favorites list, score what's worth
  rewatching, and flag promo codes or dated offers that may be stale.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the pipeline/data-model diagrams
and the reasoning behind the bigger design decisions (separate `Action`
table, the Batch API, Telegram long-polling, the in-app research loop, scoped
bearer auth).

Runs entirely on your own machine (or your own server). Videos come in from
the Send to Sidequest Shortcut (or a Telegram bot), from your own official TikTok data export (ToS-compliant,
but manual — see **How it works**), or from an optional inbox sync on a
second, dedicated TikTok account (see **Daily agent**, below — off by
default, and your real account's session is never automated).

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

If you don't have Homebrew installed, install it first:

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

## Send to Sidequest → research agent

From anywhere with a share sheet: **Share → Send to Sidequest**. That covers TikTok,
Instagram, YouTube, X, Safari, Photos, or a fresh screenshot.
- Video links are downloaded and transcribed. If a download is blocked, the
  agent judges it from the link instead.
- Screenshots and photos are OCR'd and shown to the model.
- Articles, product pages and plain text are read as they are.

Within a few minutes a research agent has looked into it and sent the answer to
your phone as a notification. The full report lands on the dashboard's
**Agent** page.

**Tech content** (code, AI, dev tools, repos) is judged against your own projects.
**Everyday content** (food, products, fitness, money, travel) gets a different review:
- whether it's worth it for you
- real prices and costs
- red flags such as paid promotions
- better alternatives
- the practical next step

It never touches your projects.

- **Repo/tool videos** ("10 GitHub repos for building an OS"): every repo is
  checked against the live GitHub API (stars, last push, license, README).
- **AI / news videos**: traced to the primary source, with X search for
  breaking news; says what's actually new and what the video overstated.
- **Food spots / places**: address, hours, which days to go, price, how to book,
  and whether it's still open.
- **Skills, projects, jobs**: same as before (jobs are research-only; nothing
  is ever submitted).
- **Products, recipes, fitness, money, travel**: real price and cheapest
  source, whether the claim holds up, risks and red flags, what to do next.
- **Project fit, for tech content**: for each of your projects (active or
  planned) it could matter to, whether it's worth adding, where exactly it
  plugs in, and what it does to that project's architecture: what it replaces,
  simplifies, or makes faster or cheaper. It opens your project files to back
  that up, and suggests a new project when something fits none of them.

**How it works (~$0.01–0.03 per item):**

1. **Free first:** the app downloads the video on your machine, and looks up any
   GitHub repos and links in it.
2. **Gemini watches it.** The downloaded file goes to Gemini (`gemini-3.8-flash`),
   which takes in the audio and the frames together, so there's no Whisper/OCR
   step and it reads what's on screen (code, menus, prices). This one call fills
   the library's analysis and saves a transcript and the on-screen text.
3. **Gemini researches it.** A tool loop combines Google Search and Google Maps
   (free up to 5,000 grounded requests a month on paid Gemini 3 models) with the
   app's own tools: GitHub lookups, reading your project files (tech only), and
   filing the report. It works from the saved text, so the video is paid for once.

Bulk analysis of the export backlog stays on Claude's half-price Batch API, and
research can be switched to Grok or Claude in Settings.

**Setup (about 5 minutes, all in Settings):**

1. **Research agent:** paste a **Gemini API key** from
   [aistudio.google.com](https://aistudio.google.com/apikey) with billing turned
   on (video input isn't on the free tier). Fill in **About me**, and list any
   ideas you haven't started yet.
2. **Send to Sidequest:** click **Create my private channel**, then follow the steps
   shown:
   - subscribe to your results channel in the free ntfy iPhone app
   - build the 2-step "Send to Sidequest" Shortcut (Get Contents of URL, with the
     values shown)
3. **Your projects:** found automatically in `~/Projects` (`PROJECTS_DIR`).
   Mark each one active, planned or ignored.

**How it's wired:**

- **Delivery:** the Shortcut posts to a private ntfy.sh topic, which this app
  streams. Like Telegram long-polling, there's no tunnel and nothing on your
  machine is exposed. Topic names are long and random, and every message must
  also carry a secret token.
- **Your projects:** read through a secret-safe reader that refuses `.env`
  files, `secrets/`, `data/`, databases, keys, binaries, and any path that
  resolves outside the project. In Docker they're mounted read-only.
- **Telegram** still works as an optional second doorway (Settings → Telegram).

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
- **Built but needs your one-time setup**: `app/scraper.py` reads the inbox
  of a **second, dedicated TikTok account** — not your real one. If that bot
  account ever gets rate-limited or flagged, your real profile and Saved
  list are completely unaffected, since it's never the one being automated.
  1. Create a new TikTok account for this (it only needs to receive DMs; no
     public presence needed). Set its message privacy to accept DMs from
     "Everyone," and note its handle.
  2. In this app's Settings page, enter **your own real handle** as the
     trusted sender — the bot account only acts on messages from that
     handle, everything else is ignored.
  3. Log the bot account into the scraper once, interactively:
     ```bash
     source .venv/bin/activate
     python -m app.scraper login
     ```
     This opens a real, visible browser — log the **bot account** in there
     yourself (this app never sees or handles the credentials), then close
     the window. The session saves to `data/tiktok_auth_state.json`
     (gitignored).
  4. From your real account, send a saved video to the bot account as a DM.
     Run `python -m app.scraper` to confirm it finds the video; if it raises
     `SelectorMismatch`, TikTok's markup didn't match what the script
     expects — see the DOM/network notes in `app/scraper.py`'s docstring.
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

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/mattthomas7641/saved-video-intelligence)

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

## Testing

```bash
source .venv/bin/activate
pytest                 # runs against an isolated temp DB, never data/db.sqlite3
ruff check .
```

Each test gets a fresh, freshly-migrated schema (`tests/conftest.py`) and
isolated `secrets.json`, via the same `SCANNER_DATA_DIR` override the app
itself uses. The Anthropic API is never called live in tests —
`app/analyze.py`'s classification is tested against a mocked client
(`unittest.mock.patch` on `messages.create`, returning a canned `tool_use`
block), so the suite runs free and offline. CI (`.github/workflows/ci.yml`)
runs this plus a Docker-build check on every push.

## Project layout

```
app/
  main.py          FastAPI routes (thin HTTP glue; logic lives below)
  schemas.py       Pydantic request/response models for the /api/* routes
  db.py            Engine + get_db() dependency; schema managed by Alembic
  models.py        Video + Action tables (SQLModel)
  services/
    dashboard_query.py  Query/filter/stats logic for /dashboard
  ingest.py        Parses the TikTok export
  scraper.py       Bot-account inbox reader (daily sync)
  download.py      yt-dlp wrapper
  transcribe.py    faster-whisper wrapper
  ocr.py           ffmpeg frame sampling + tesseract OCR
  analyze.py       Claude categorization/summarization + action detection
  relevance.py     Staleness heuristic
  pipeline.py      Orchestrates one video through all steps
  worker.py        Background thread that processes the pending queue
alembic/           Schema migrations (replaces hand-rolled ALTER TABLE)
templates/         Jinja2 pages (dashboard, video detail, onboarding, settings, actions, jobs)
tests/             pytest suite + fixtures
data/              SQLite DB + thumbnails (gitignored)
```
