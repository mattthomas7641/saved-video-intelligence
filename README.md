# Sidequest (Saved Video Intelligence)

Send it anything you come across. It does the follow-up and tells you if it's worth your time.

[![CI](https://github.com/mattthomas7641/saved-video-intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/mattthomas7641/saved-video-intelligence/actions/workflows/ci.yml)

Send it anything you come across — a TikTok, an Instagram Reel, a YouTube Short,
a link or a screenshot — and a research agent (about $0.03–0.06 an item) does the
follow-up within minutes and puts a clear next step on your **To do** list, so
you actually act on it instead of it sitting in your saves.

- **New tools and agent skills, judged against your own projects.** The most
  common use: a video about a new agent skill or dev tool. The agent reads your
  project code and tells you whether it's worth adopting, where it would plug in
  and what it would change. Repos a video names are vetted against the live
  GitHub API.
- **Everything else, too.** AI news is traced to its source; a food video becomes
  an address, hours and the best day to go; products get real prices and red
  flags like paid promotions.
- **A to-do list that makes you follow through.** Each result arrives as a phone
  notification and stays on the To do list with its next step (try, build, buy,
  go or skip) until you mark it done or "not for me". See
  **Send to Sidequest → research agent**, below.
- **The archive.** The ~450 older TikTok saves analyzed before Sidequest stay
  browsable under **Archive** (topics, "worth rewatching", promo codes that may
  have expired).

See [ARCHITECTURE.md](ARCHITECTURE.md) for the pipeline and data-model diagrams
and the reasoning behind the bigger design decisions.

**Self-hosted and single-user by design**: your own copy, your own API key, your
data on your own machine. No central service downloads anyone else's content.

## How it works

```
Share → Send to Sidequest (iOS Shortcut)  ─┐
Share → Telegram bot (optional)           ─┴─> intake ─> research lane (2 at a time)
   download (yt-dlp) ─> Gemini watches it (audio + frames) ─> free prefetch (GitHub, linked pages)
   ─> tech or everyday? ─> research loop (Google Search/Maps + your project files, tech only)
   ─> report on the home page + a phone notification
```

1. **Send anything** from a share sheet: TikTok, Instagram Reels, YouTube, X,
   Safari, Photos or a screenshot. Video links are downloaded; if a site blocks
   the download, the report says so and judges the link instead. Screenshots are
   OCR'd and shown to the model; links and text are read as they are. Calls to
   action ("comment X and I'll DM you", link in bio, courses) are ignored.
2. **Gemini watches it** (`gemini-3.8-flash`): one call takes in audio and frames,
   so it reads code, menus and prices on screen, and saves a transcript and the
   on-screen text. Measured: about $0.009–0.015 per video.
3. **The research loop** combines Google Search and Maps (free up to 5,000
   grounded requests a month on paid Gemini 3 models) with the app's own tools:
   live GitHub lookups, reading your project files (tech content only) and filing
   the report. It works from the saved text, so the video is paid for once.
   Measured: about $0.025–0.05 per item. A daily cap (default $3) stops it.
4. **Tech content** is judged against your projects: where it would plug in, what
   it changes in the architecture, effort, and whether it's worth doing for fun,
   personal use + resume, or only a weak resume line. **Everyday content** (food,
   products, fitness, money, travel) gets a practical review instead: real prices,
   red flags, alternatives, and the next step. It never touches your projects.
5. **Act on it.** The home page sorts results by topic into **To do**, **Nothing
   to do** (looked into, nothing to act on), **Done** and **Not for me**.

Grok and Claude can be switched in for research in Settings.

### Traces, memory and evals

- **Every paid call is traced.** Each attempt (watching, research, project
  cards) is one row in a run ledger: its model calls, tool calls and searches in
  order, with timings, tokens and cost. Each report links to its trace
  (`/runs/<id>`), and the daily cap sums the ledger, so failed attempts and
  retries count too.
- **Memory across shares.** Each finished report is embedded once
  (`gemini-embedding-2`, well under $0.001 an item). Before researching something
  new, the three most similar past reports are handed to the agent as "things you
  sent before", so it builds on them instead of repeating itself, and the report
  links them. **Settings → Memory** backfills older reports.
- **An eval set from your own history.** **Evals** turns items you've already
  sent into test cases (the expected type, tech/everyday split, verdict and
  projects), which you can correct. A run re-researches each case with the
  current prompts and models and scores type, verdict, project matches, ignored
  calls to action and report format, so a prompt or model change shows up as a
  number. Runs are manual, capped (about $0.03–0.05 a case) and kept off the
  daily cap; cases live only on your machine.

## Setup

Requirements: Docker Desktop, an iPhone for the Shortcut (or Telegram), and a
[Gemini API key](https://aistudio.google.com/apikey) **with billing on** (video
input and Google Search aren't on the free tier).

```bash
cp .env.example .env        # optional: model and timezone overrides
docker compose up -d --build
```

Open `http://127.0.0.1:8787/settings`, then:

1. **Research agent:** paste the Gemini key, fill in **About me** (what you're
   building, where you live: it's what makes "useful for you" mean something),
   and list ideas you haven't started yet.
2. **Send to Sidequest:** click **Create my private channel** and follow the steps
   shown (subscribe in the free ntfy app, build the 2-minute Shortcut).
3. **Your projects:** found in `~/Projects` (`PROJECTS_DIR`), mounted read-only.
   Mark each one active, planned or ignored. Project folders that are symlinks to
   elsewhere need their targets mounted too: list them in a git-ignored
   `docker-compose.override.yml`.

To use it from your phone, put the Mac and phone on one
[Tailscale](https://tailscale.com) network, run `tailscale serve --bg 8787`, and
set `PUBLIC_BASE_URL` to the `https://….ts.net` address so notification links open
the report.

## Security model

The app has no login: it only listens on `127.0.0.1` and assumes you are the one
using it. Things that could still reach it are handled explicitly:

- **Other websites in your browser** (CSRF, DNS rebinding): only our own Host is
  served, cross-site writes are refused, and a CSP blocks third-party images and
  framing (`app/security.py`).
- **Hostile content in what you send**: links found in videos are fetched only if
  they resolve to public addresses (re-checked on every redirect); reports can't
  load images (a prompt-injection exfiltration route); project files the agent
  reads go through a deny-list (no `.env`, `secrets/`, `data/`, keys, binaries,
  symlink escapes) and have credential-shaped strings scrubbed.
- **The relay**: ntfy topic names are long and random, every inbound message needs
  a secret token, and "Research deeper" buttons carry a per-action signature, not
  the token. Telegram answers one paired chat; pairing codes expire in 10 minutes.
- **Secrets** live in `data/secrets.json` (0600, atomic writes, never overwritten
  if unreadable) and never appear in logs or error messages.
- **Dependencies** are pinned; `pip-audit` reports no known vulnerabilities.

Hosting it publicly would additionally need authentication, secrets from the
environment, HTTPS and project cards built from GitHub instead of local folders.

## Testing

```bash
.venv/bin/python -m pytest      # isolated temp DB and secrets, never touches data/
.venv/bin/ruff check .
.venv/bin/pip-audit -r requirements.txt
```

No test calls a real API: Gemini, Grok, Claude, Telegram, ntfy and GitHub are
faked (`httpx.MockTransport`, fake SDK clients), so the suite is free and offline.
CI runs lint, tests and a Docker build on every push.

## Project layout

```
app/
  main.py            FastAPI routes: home (sidequests), archive, video page, settings
  security.py        Host/cross-site guard and security headers
  intake.py          Shared intake + research lane (both doorways)
  ntfy_intake.py     "Send to Sidequest" Shortcut doorway (ntfy relay) + notifications
  telegram_bot.py    Optional Telegram doorway
  pipeline.py        One shared item: download -> watch -> research -> result
  gemini_client.py   Gemini: watch + research loop (default provider)
  xai_client.py      Grok provider;  agent.py: shared prompts, tools, Claude loop
  projects.py        Your projects: scan, cards, secret-safe file reader
  prefetch.py        Free GitHub/page lookups (public addresses only)
  analyze.py         The first-pass analysis schema (topic, summary, type)
  tracing.py         Run traces and the cost ledger behind the daily cap
  memory.py          Embeddings + recall of related past reports
  evals.py           Eval cases, scoring and runs (`python -m app.evals`)
  download.py, ocr.py, transcribe.py   yt-dlp, tesseract, Whisper (non-Gemini providers)
  services/dashboard_query.py          The archive's filters and stats
alembic/             Schema migrations
templates/, static/  Jinja2 pages and CSS (no build step)
tests/               pytest suite
data/                SQLite DB, thumbnails, secrets (git-ignored)
```
