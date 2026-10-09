# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

A single user: the person running their own self-hosted instance, going through their own TikTok saved-video library (currently ~7,300 videos). There is no multi-tenant or shared-account use case — "Settings" configures one person's deployment, not an org. The same person is also the one who will show this project to others (see Positioning) — so the interface is read both by its one daily user and, occasionally, by a technical third party evaluating the work.

## Product Purpose

Turns a TikTok "saved videos" export (plus an optional daily DM-based sync from a dedicated bot account) into a searchable, categorized, summarized personal library: transcribes and OCRs each video, has Claude categorize/summarize/score it ("worth rewatching"), flags promo codes or dated offers that may be stale, and — as of the most recent work — detects when a saved video points at something actionable (a skill to adopt, a project to build, a job to apply to) and queues it for a separate daily agent to act on (draft a PR, draft job materials; never auto-submit anything).

Success = the user can open the dashboard and quickly find what's actually worth their time in a library too large to rewatch by hand, and (for actionable videos) see draft work show up without having to do it themselves.

## Positioning

Self-hosted and single-user by deliberate design, not as a limitation: no central service, no third-party downloading other people's TikTok content, your data stays on your machine, your own Anthropic API key. That avoids both the privacy exposure and the ToS/legal exposure a multi-user hosted version would carry. This project is also explicitly meant to read as a strong engineering portfolio piece (tests, CI, typed schemas, real migrations, documented architecture decisions) — the visual design should carry the same signal: that of a considered, deliberately-built product, not a quick personal script with a UI bolted on.

## Operating Context

- Runs locally (Docker or a Python venv) at `localhost:8787`, opened in a normal desktop browser. Not currently optimized for mobile, though it should degrade reasonably.
- The dashboard is the home base: a topic tree (groups → categories), several tabs/views (everything / worth rewatching / may be expired / not revisited / archived / queue), free-text search, sort, and an "overview" mode (topic panels + top picks) vs. a flat filtered list.
- Other pages: video detail (full transcript/summary/metadata for one video), bulk analysis (collect+analyze thousands of videos with a spend cap), onboarding (first-run, before any data exists), settings (API key, resume upload, action-type toggles, bearer token, TikTok trusted-sender handle), and two new pages from the daily-agent feature — Actions (queued skill/project ideas, each linking to a draft PR once worked) and Jobs (drafted resume/cover-letter per job lead, read-only review surface, no submit button anywhere).
- Heavy day-to-day interaction is scanning/filtering/skimming many video cards quickly, not reading any single one closely — most of the time the user is deciding "is anything here worth a closer look," not studying one result.

## Capabilities and Constraints

- Backend: FastAPI + Jinja2 server-rendered templates (no JS framework), vanilla CSS. Redesign should stay within this stack — no build step, no framework migration — vanilla CSS/HTML/minimal vanilla JS only.
- Must preserve all existing functionality: every route, every piece of current UI behavior (filters, drill-down, pagination, bulk controls, settings forms) continues to work: this is a redesign of appearance and layout, not a feature change.
- Real production data exists (~7,300 videos) and the dashboard's topic-tree/overview logic is already covered by characterization tests (`tests/test_dashboard_characterization.py`) pinning current *behavior* (not appearance) — redesign must not change what these tests assert about.
- No login/auth on the browsable pages (by design, single-user, loopback-only); a few machine-facing `/api/*` routes are bearer-token-authenticated but have no UI of their own.

## Brand Commitments

None fixed. Product is currently named "Saved Video Intelligence" (recently renamed from "TikTok Saved Scanner") but the name itself, along with all colors/typography/visual identity, is explicitly open for the redesign to reconsider.

## Evidence on Hand

Real, substantial production data: ~7,300 real analyzed/categorized videos, a real topic taxonomy (`app/config.py`'s `GROUPS`/`CATEGORIES`), real summaries/scores/transcripts. No curated marketing copy, testimonials, or case studies exist or are needed — this isn't a marketing surface.

## Product Principles

1. Fast scanning over deep reading — the dominant interaction is skimming many items to find what's worth attention, not reading any one closely.
2. Density with clarity, not density via clutter — a library this large needs to show a lot at once, but the current design's clutter complaint means today's density reads as noisy rather than information-rich; the redesign should fix that distinction, not just add whitespace and lose scannability.
3. Looks deliberately engineered, not scripted — visual craft should match the care already put into the backend (tests, CI, typed APIs, real migrations) as a portfolio signal.
4. No dark patterns, no manufactured urgency — this is a personal tool surfacing the user's own saved content, not a product trying to drive engagement.

## Accessibility & Inclusion

No specific requirement established beyond reasonable default practice (sufficient contrast, keyboard-navigable forms/filters, readable type sizes) — single-sighted-user personal tool, not a public-facing product with a formal accessibility mandate.
