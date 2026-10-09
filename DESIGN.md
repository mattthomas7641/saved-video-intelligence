---
name: Saved Video Intelligence
description: Your library as a guide map — eight flat, fully-saturated topic territories; color marks where you are, never scattered per-row.
colors:
  bg: "#F7F7F5"
  surface: "#FFFFFF"
  rail: "#EEEEEA"
  ink: "#1E1E1C"
  ink-2: "#4E4E48"
  ink-3: "#6A6A65"
  line: "#E1E0DA"
  line-strong: "#C7C6BD"
  t-tech: "#1F6FB8"
  t-money: "#8A6600"
  t-learn: "#6A42B8"
  t-home: "#B83D1F"
  t-health: "#B82E63"
  t-travel: "#146E64"
  t-fun: "#3D7A1C"
  t-other: "#6E6859"
  flag: "#A8321E"
  flag-soft: "#F3DCD2"
typography:
  display:
    fontFamily: "Archivo, ui-sans-serif, system-ui, sans-serif"
    fontWeight: 700
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Archivo, ui-sans-serif, system-ui, sans-serif"
    fontWeight: 400
rounded:
  pill: "999px"
  lg: "24px"
  md: "18px"
  sm: "14px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "24px"
components:
  btn-primary:
    backgroundColor: "{colors.ink}"
    textColor: "#FFFFFF"
    rounded: "{rounded.pill}"
    padding: "10px 17px"
  panel:
    rounded: "{rounded.lg}"
    textColor: "#FFFFFF"
  tag-warn:
    backgroundColor: "{colors.flag-soft}"
    textColor: "{colors.flag}"
    rounded: "{rounded.pill}"
---

## Overview

**Creative North Star: The Guide Map.** The library is a park/zoo guide map: eight topic groups as flat, fully-saturated color territories, sized by how much they hold. Color marks WHERE you are — applied at the overview map and as the current territory's contextual accent — never scattered per-row across a dense list, which is what made an earlier, more muted attempt (also tried and rejected by the user as "basically the same, just reskinned") feel cluttered.

Chosen through Impeccable's bolder-register direction roll (seed `07e8fd5a`, challenger `challenger-zoo-map`) after the user explicitly rejected the first committed direction ("Field Guide") as insufficiently different — both in structure (still a sidebar+table shell) and in palette (a monochromatic warm-tan wash that read as "unreadable"). This system corrects both: eight real, saturated hues replace the single muted accent, and the base ground was pushed to neutral near-white specifically because the user flagged "tan" twice.

## Colors

Base ground and surfaces are neutral near-white/light-gray (`bg`/`surface`/`rail`) — deliberately NOT warm-cream, after the mechanical slop detector flagged a cream-palette default twice during iteration and the user independently called the same thing out as illegible. Ink is near-black, never pure `#000`. Eight topic territories each own one full-strength flat color (verified ≥4.5:1 with white text on every one). The `flag` rust/red is independent of territory color and always means "needs attention" (a stale-looking promo, a failed analysis).

**Contextual accent rule**: `--accent` defaults to ink-neutral. A `[data-territory="<slug>"]` attribute (set on the dashboard shell from the active topic group, or on `<body>` from a single video's own category on the detail page) swaps `--accent`/`--accent-ink`/`--accent-soft` to that territory's color, which then drives the active nav tab, filled tabs, chips, and score bars for that view only. Outside an active territory context, everything stays ink/gray monochrome — this is the mechanism that keeps eight bold hues from becoming eight-hue row-level noise.

## Typography

One typeface throughout — **Archivo** (grotesque sans) — used for both display headings (700 weight, tight -0.02em tracking) and body/UI text (400-600 weight). Deliberately a single face this time, not a three-font system: hierarchy comes from size, weight, and territory color, not from mixing serif/sans/condensed families.

## Layout

Same two-level shell as before (sticky rail + main), but the rail's territory list is now colorized per-group (each `<details>` carries its own `--t-group` custom property, so the open/active topic tints its own row). The real structural shift is the **overview**: eight flat-color territory tiles (`.panel`, 24px radius, white text, color = `--tc` set inline per territory) replace the previous bordered gray cards — this is the page's first viewport and the thing that actually signals "this is a different idea," not just a different palette.

## Shapes

Full capsule radius (999px) on every control — buttons, fields, chips, tags — a direct reversal of the previous (Field Guide) direction's zero-radius rule, earned by this world's "map legend / capsule control" material, not arbitrary. Panels and cards use large radii (18-28px) rather than hairline-bordered rectangles.

## Components

- **Territory tiles** (`.panel`): flat full-strength fill, white text, 24px radius, lifts 3px on hover with the overshoot ease. This is the page's signature moment.
- **Score bar** (`.rating`): unchanged in concept from the prior iteration — a filled pill-capped bar, length = value/5. Colors in the active territory's accent when ≥4, ink-gray otherwise.
- **Buttons**: full pill capsules; primary fills with the current context's accent (ink when none is active); hover inverts to solid ink.
- **Tabrail**: flat list, each topic's `<details>` tints its own open-state background with its own territory color via an inline custom property — browsing stays monochrome until you open a topic, which then owns the color.

## Do's and Don'ts

- **Do** keep territory color contextual (nav/accent/score-bar for the CURRENT view) — never apply a different hue to every row simultaneously.
- **Do** keep the base ground neutral near-white. Cream/tan was tried twice and rejected both by the mechanical detector and by the user directly.
- **Don't** reintroduce a three-typeface system; Archivo alone carries the whole hierarchy.
- **Don't** flatten the overview back into gray bordered cards — the flat-color territory tiles are the direction's signature first viewport.
- **Don't** treat `cubic-bezier(0.34, 1.56, 0.64, 1)` (bounce/overshoot) as a mistake if the detector flags it again — it's a deliberate, direction-specified motion choice, not an accidental default.
