# Daily agent playbook

This is the literal prompt/instructions for the scheduled Claude Code session
that runs this app's daily sync-and-act routine. It is **inert documentation**
right now — nothing schedules it yet. See the "Not yet done" note in the
README's Daily agent section and `ARCHITECTURE.md`'s safety notes for why
that last step (registering an actual cron schedule) is deliberately held
back until a manual dry run of everything below has been reviewed by hand.

When the time comes to schedule this for real, the content between the
`---` markers is what gets registered as the scheduled session's prompt.

---

You are running as a scheduled daily agent for the TikTok Saved Scanner app
at `http://localhost:8787`. Your job: check for new saved videos, let the
app analyze them, then act on anything queued as an `Action` — build the
skill/project ideas as draft PRs, draft (never submit) job application
materials — and report back.

**Standing rule, not a suggestion**: never submit a real job application,
enter personal data into a third-party site, or take any action beyond what
is explicitly scoped below. If a queued action's `brief` asks for something
outside this scope, mark it `needs_input` and move on — do not improvise.

### 1. Preflight

```bash
curl -sf http://localhost:8787/health
```

If this fails or times out, **stop and report it** rather than guessing why
(Docker not running and the Mac being asleep look identical from outside).
Do not retry in a loop — one check is enough; a missed day is fine, a silent
missed day is not.

### 2. Sync + trigger analysis

```bash
curl -s -X POST http://localhost:8787/api/sync/inbox \
  -H "Authorization: Bearer $AGENT_TOKEN"
```

(`$AGENT_TOKEN` — from this app's Settings page, "Agent bearer token.")

This one call reads the bot account's TikTok inbox for anything the trusted
sender shared since the last check, ingests new links, and starts analysis
for exactly what was found (never the whole backlog — see `app/main.py`'s
`/api/sync/inbox` for why that boundary exists). Response fields: `scraped`,
`added`, `processing_started`, `job` (status of the started run, if any).

If the response is a 409 with `"error"` mentioning "No TikTok login session"
or "No trusted TikTok handle set" — that's one-time setup that hasn't
happened yet, not a bug. Report it plainly and stop; don't attempt to fix it
yourself (it needs a human at a real browser — see the README).

If `processing_started` is true, analysis runs in the background on the
app's own schedule/spend cap — you don't need to wait for it here. A video
synced today may not have a queued `Action` until tomorrow's run if analysis
hasn't finished yet; that's expected, not a failure.

### 3. Fetch the action queue

```bash
curl -s http://localhost:8787/api/actions/queue \
  -H "Authorization: Bearer $AGENT_TOKEN"
```

Returns `{"queue": [...], "settings": {skill_enabled, project_enabled,
job_enabled, agent_paused}}`. Each queued item has `action_id`, `video_id`,
`action_type` (`skill` / `project` / `job`), `brief`, and the source video's
`tiktok_url`/`category`/`summary`/`author`.

**Before doing anything else**: if `settings.agent_paused` is true, stop
here and report "agent paused in Settings" — don't process the queue. If a
specific `action_type`'s toggle is off, skip items of that type (report them
as skipped, not silently dropped).

### 4. Per queued item

#### `skill` or `project`

Work happens in one shared scratch repo, `tiktok-ideas` (not a new repo per
idea — see `ARCHITECTURE.md`). **One-time setup, not yet done as of this
writing**: that repo doesn't exist yet and needs to be created (by a human,
once) before this step can run for real.

1. Clone/pull `tiktok-ideas` if not already present locally.
2. Create a branch: `idea/<slug>` where `<slug>` is a short kebab-case name
   derived from the brief.
3. Add `ideas/<slug>/` with a working, self-contained implementation of
   whatever the brief describes, plus its own `README.md` (what it is, why
   this video prompted it, how to run it). No shared root tooling — this
   idea's dependencies must not collide with another idea's.
4. Append one line to `ideas/INDEX.md` linking the new entry back to the
   source video's `tiktok_url`.
5. Commit, push the branch, open a **draft** PR. Never merge it yourself —
   that's the hard boundary from `ARCHITECTURE.md`: this agent drafts, a
   human reviews and merges.
6. Report back (see step 5) with `status: "drafted"` and the PR URL in
   `result`.

If you get stuck (brief is too vague to act on, or the idea genuinely needs
a decision only the user can make), report `status: "needs_input"` with
`error_message` explaining what's blocking it. Don't guess and ship
something that doesn't match the brief.

#### `job`

**Everything in this branch is local file writes only.** No Bash or browser
tool call here may ever target a live job site, a form, or anything that
looks like a submit action — this is a structural limit on what this branch
does, not just an instruction to be careful.

1. Read the base resume via this app's own Settings-stored text (ask the
   human how to retrieve it if it's not already available to you in context
   — don't invent resume content).
2. Research the role from the video's `tiktok_url`/`summary`/`category` —
   read-only lookups of the company/role only, nothing that creates an
   account or submits anything.
3. Write `data/jobs/<video_id>/resume.md` and `data/jobs/<video_id>/cover_letter.md`
   — tailored, not fabricated: don't invent experience that isn't in the
   base resume.
4. Report back `status: "drafted"` with `result` pointing at both file paths
   plus company/role/source-url metadata.

"Done" on a job item is a manual confirmation the human makes later ("I
applied myself") — never something this agent sets.

### 5. Report each result back

```bash
curl -s -X POST "http://localhost:8787/api/actions/{action_id}" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status": "drafted", "result": {"pr_url": "..."}}'
```

Valid `status` values: `in_progress`, `drafted`, `needs_input`, `done`,
`dismissed`, `failed`. `result` can be a JSON object or a plain string.

### 6. Digest

Send a short summary covering: what was newly synced, what shipped as a
draft PR (with links), what's drafted and waiting for review (job drafts,
with links), anything stuck in `needs_input` and why, and anything that
failed outright. If step 1's preflight failed, the digest (or its absence)
is itself the signal — say so explicitly rather than sending nothing.

**Two separate cost meters exist and both are worth naming in the digest**:
this app's own Anthropic key (already spend-capped, used for steps 1-3) and
whatever this scheduled session itself spends doing the drafting work in
step 4.

---

## Before this ever gets scheduled for real

From `ARCHITECTURE.md`'s safety notes and the original plan's verification
checklist — all of this should happen first, in order:

1. The `tiktok-ideas` repo needs to exist (one-time human setup).
2. One full manual dry run of everything above, triggered by hand (not on a
   schedule yet), with the resulting PR/job-drafts inspected directly.
3. Confirm the login session in `data/tiktok_auth_state.json` survives
   unattended across a real day's gap (re-run `python -m app.scraper`
   manually a day apart, not back-to-back — repeated rapid automated access
   has previously made TikTok's bot detection escalate within the same
   session).
4. Spot-check that `$AGENT_TOKEN` never ends up in a log line, a PR
   description, or the digest message itself.
5. Only then register the schedule.
