---
name: Saved Video Intelligence
description: A naturalist's field guide for your saved TikTok library — organized by family, identified by terse marks, color reserved for exactly two things.
colors:
  bg: "oklch(90% 0.026 88)"
  surface: "oklch(94% 0.016 88)"
  surface-raised: "oklch(97% 0.008 85)"
  rail: "oklch(87% 0.030 88)"
  ink: "oklch(16% 0.010 80)"
  ink-2: "oklch(34% 0.014 80)"
  ink-3: "oklch(46% 0.016 80)"
  line: "oklch(80% 0.020 80)"
  line-strong: "oklch(68% 0.024 80)"
  family: "oklch(47% 0.085 142)"
  family-ink: "oklch(98% 0.01 142)"
  family-soft: "oklch(89% 0.035 142)"
  flag: "oklch(47% 0.135 35)"
  flag-ink: "oklch(98% 0.01 35)"
  flag-soft: "oklch(90% 0.05 35)"
typography:
  display:
    fontFamily: "Spectral, Iowan Old Style, Georgia, serif"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Work Sans, ui-sans-serif, system-ui, sans-serif"
    fontWeight: 400
    lineHeight: 1.55
  dense:
    fontFamily: "Barlow Condensed, ui-sans-serif, system-ui, sans-serif"
    fontWeight: 500
    letterSpacing: "0.01em"
rounded:
  all: "0px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "24px"
components:
  btn-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface-raised}"
    typography: "{typography.dense}"
    rounded: "{rounded.all}"
    padding: "9px 15px"
  btn-primary-hover:
    backgroundColor: "{colors.family}"
    textColor: "{colors.family-ink}"
  tag-warn:
    backgroundColor: "{colors.flag-soft}"
    textColor: "{colors.flag}"
    typography: "{typography.dense}"
    rounded: "{rounded.all}"
  chip-active:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface-raised}"
    rounded: "{rounded.all}"
---

## Overview

**Creative North Star: The Field Guide.** Every saved video is treated as a species entry in a naturalist's field guide — filed under a family (topic group), identified at a glance by a few terse marks (category, a filled-bar score, status), and browsed via a flat tab index rather than a nested tree. The direction was chosen through Impeccable's direction-roll process (seed `07e8fd5a`, re-roll round 1, candidate 6 of 7) against six catalog challengers; see `.impeccable/surfaces/templates-dashboard-html.md` for the full contract and the weighing record.

The system refuses two defaults on purpose: the generic white-card/blue-accent SaaS dashboard (every AI-generated admin panel), and a rainbow-per-category tint system (the incumbent design's main source of visual noise). Color is reserved for exactly two signals — **family** (green, the active topic / "this is browsable now") and **flag** (rust, "this needs your attention" — an expired-looking promo, a failed analysis, an actionable queue item). Nothing else on the page ever takes a hue.

## Colors

Background runs cream-to-manila (`bg` → `rail`, darkest for the nav rail, lightest for raised surfaces) evoking real field-guide page stock, not a "safe tasteful" default — it was pushed deliberately buff/manila rather than pure warm-pink-cream specifically to read as printed stock rather than wall paint. Ink is a warm near-black, never pure `#000`. The only two chromatic colors: `family` (muted forest green) for active/browsable state, `flag` (muted rust) for anything needing attention. Every other UI element — category icons, secondary text, borders — stays strictly monochrome on the ink/line scale.

## Typography

Three faces, each doing one job, none of them Instrument Sans or Newsreader (the incumbent's defaults, both on the common-AI-default list):

- **Display** (`Spectral`) — page titles, video headlines, section headers. A naturalist-publishing serif with real character.
- **Body** (`Work Sans`) — paragraphs: summaries, transcripts, descriptions. Normal-width for actual reading.
- **Dense** (`Barlow Condensed`) — everything else: nav labels, table cells, badges, counts, form labels. Condensed on purpose — it fits more terse data per row without feeling cramped, which is the direct, functional answer to "the dashboard feels cluttered": more information in less visual space, not less information.

`.num` (counts, scores, dates) always renders in the dense face with tabular figures.

## Layout

Two-level shell: a sticky left rail (236px) is the family tab index — flat, not nested-and-expanding the way the incumbent's tree was; drilling into a category happens as chips in the main content area, not deeper rail nesting. The rail collapses to a horizontal scrolling strip under 900px. Main content is a dense entry list (`.table`/`.trow`) with a fixed-column grid that sheds columns at breakpoints (1180px drops creator/date; 900px drops everything but thumbnail, title, and score). The "processing" status block sits quiet at the rail's bottom, a single line + hairline meter — not a competing bordered section with its own buttons.

## Shapes

Zero border-radius, everywhere, without exception — the field guide's hairline-ruled, letterpress character. No rounded corners, no pill shapes except the already-established `.chip`/`.tag-*` family (kept for familiar legibility as inline status markers, not cards). Borders are 1px hairlines in `line`/`line-strong`; nothing heavier.

## Components

- **Buttons**: ink-filled primary, hairline-bordered secondary, borderless quiet variant for row-level actions. Hover shifts the primary to `family`, never a generic darken.
- **Score bar** (`.rating`): the worth-rewatching score renders as a filled horizontal bar (length = value / 5), not five dots — a value read from shape, not a count. Scores ≥4 render in `family` green; lower scores stay ink-gray.
- **Notice/alert strip** (`.notice`): a plain hairline-bounded strip (top + bottom rule, no card, no colored left border — the left-accent-border pattern was flagged by Impeccable's mechanical detector as a recognizable AI-UI tell and deliberately removed). The icon alone carries the color signal.
- **Tabs/chips**: active state is a 3px left rule in `family` (rail/tree) or a solid ink fill (toolbar chips) — never a background tint per topic.
- **Panels** (overview grid): bordered cards with real `14px` gaps — deliberately NOT a gapless hairline mosaic (an earlier draft used `gap:1px` + background-as-grid-line, which broke visibly on an incomplete last row; reverted to individually-bordered cards for robustness).

## Do's and Don'ts

- **Do** reserve color for family-active and flag-attention states only. Everything else is ink/line monochrome.
- **Do** use the dense condensed face for anything tabular or terse; reserve the display serif for actual headlines.
- **Do** keep border-radius at zero across the whole system.
- **Don't** reintroduce a per-category color-tint system (`.tint-*` classes) — this was the incumbent design's primary source of visual noise and was removed deliberately.
- **Don't** use a colored `border-left` accent on cards/alerts — flagged explicitly by the mechanical detector; use a plain hairline top/bottom rule with a colored icon instead.
- **Don't** add shadows or multi-level card nesting; depth comes from hairline rules and the rail/main two-level shell only.
