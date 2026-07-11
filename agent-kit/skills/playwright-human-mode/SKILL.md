---
name: playwright-human-mode
description: "Visible, supervised, human-mode browser co-working. PRIMARY browser = Playwright MCP run VISIBLY on your local machine (vision, keyboard, mouse, click, screenshot) for real-time round-trip work you watch: program → drive the browser → check → iterate. Logs in with your credentials when it can; on CAPTCHA/2FA it pauses and hands the keyboard to you in the same window, then continues. Use whenever a browser is involved — testing, verifying a deploy, a login/auth flow, admin tasks, automating a site, inspecting a page, or when you mention Playwright, browser automation, 'open a browser', 'click', 'login', 'check the page', 'verify the UI', 'visual test', or similar."
license: MIT
---

# Browser Control — Human Vision Mode (Supervised Co-Working)

> UltraMemory Agent Kit — our Playwright *Human Vision Control* wrapper over the standard
> Playwright MCP. The kit installs the standard Playwright MCP (on your consent) and this skill
> rides on top, adding the speed mandate + supervised co-working discipline below.

## ⚡ SPEED MANDATE — read before any browser action (HARD RULE, do not drift)

This skill is only as fast as you drive it. The default failure mode is
**self-inflicted slowness**: one action per round-trip and a screenshot +
narration after every click. DO NOT. **Near-real-time is the bar.**

**Non-negotiable defaults, every browser session:**
1. **BATCH.** Put independent actions in ONE message, and chain a known UI
   sequence without stopping between each — e.g. *copy password → focus field →
   paste → submit* is ONE message, not four. One round-trip, not five.
2. **Snapshot-first, sparingly.** Act off `browser_snapshot` (the a11y text
   tree). Do NOT re-snapshot after every action — refs stay valid until the DOM
   changes; only re-snapshot when the page actually changed. Screenshot only at
   checkpoints / when pixels matter.
3. **Reuse the session.** `browser_navigate` straight to the target and CHECK if
   you're already authenticated before ever touching a login form.
4. **Narrate per phase, not per click** — "Logging in…", "At consent" — then move.
5. **Gates only at decisions / irreversible / money / login-2FA handoff.**
   Everywhere else, batch and go. Supervision ≠ a click-by-click play-by-play.
6. **`browser_evaluate` for compound / multi-step page work.** For forms and dense
   admin UIs, ONE `browser_evaluate` can find + fill + select + click + read in a
   single call — far fewer round-trips than snapshot→click→snapshot, and it avoids
   generating huge page snapshots. Reach for it whenever a step is multi-part or a
   full snapshot would be a wall of text. (Keep secrets out of the return: act on
   the value in-page and return only a confirmation, e.g. prefix + length.)

**Self-check before EVERY browser tool call:** "Could the next 2–4 steps go in
this one message?" If yes, batch them. If you just did snapshot→act→snapshot→act
one step at a time, you are drifting — stop and batch. This is the rule, not a
suggestion: serial click-by-click wastes round-trips and the human's supervision;
the value is at decisions, not mechanics.

---

The **primary co-working browser is Playwright MCP, run VISIBLY on your local
machine**, driven like a person — vision, mouse, keyboard, click, screenshot. The
human is watching and supervising; work in a real-time round trip. Headless and
out-of-sight automation are the exception, not the default.

## Before you launch a browser (API-first)

First ask: can an **API (with token), an MCP server, or a CLI** do this — in
whole or in part? If yes, do that; the browser is the **last resort**.
Preference order: **API → MCP → CLI → browser.** Decompose mixed tasks so the
browser only handles the steps that genuinely need a rendered UI / human
interaction (e.g. a DNS change uses the provider's API token, not a browser).
Keep tokens/keys in a git-ignored env file or your OS secret store. Only once
you've confirmed a browser is actually required do you proceed to the ladder below.

## Priority ladder (use in this order)

1. **Playwright MCP — visible, on your machine (DEFAULT for almost everything).**
   Program → drive the visible browser → screenshot/narrate → check → iterate,
   with the human watching. Profile is **persistent** (logins stick across runs).
   `--caps=vision` gives pixel vision + mouse.
2. **Human handoff (in the same visible window).** When Playwright can't get past
   a login — CAPTCHA, 2FA, a bot-wall — **pause and ask the human to complete that
   step in the open window**, then resume automation. Never auto-solve a CAPTCHA.
3. **Out-of-sight fallback (LAST RESORT, if you've configured one).** CAPTCHA-heavy
   logins or tasks the human doesn't need to watch: a real-Chrome + residential-IP
   runner, if you have one set up. Optional — omit if not configured.
4. **Chrome DevTools MCP** *(if installed)* — when the job is network /
   performance / Core Web Vitals / console debugging, not driving.
5. **Vendor in-browser agents** — generally avoid; product guardrails refuse most
   login/admin/write actions. Only for a quick read-only peek at an already-open page.

## The co-working loop (fast, supervised)

Work like a fast human operator the user is watching — not a narrator that stops
after every click. The loop is **snapshot → act (in batches) → verify at checkpoints**:

1. **Read the page as structure first.** `browser_snapshot` (the accessibility tree)
   is text — faster to fetch and to target than a screenshot. Act off its element
   refs. Reserve `browser_take_screenshot` for *checkpoints* and for what only pixels
   show (layout, canvas/charts, anti-bot, final visual verification).
2. **Batch independent actions.** Fire independent calls together in one message (the
   harness runs them in parallel) and chain an obvious sequence (tick box → fill field
   → click Continue) without a screenshot between each. One round-trip beats five.
3. **Narrate at the phase level, not per action.** "Logging in…", "At the payment
   screen" — a line per *phase*. The human is watching the screen; don't recite each click.
4. **Verify at meaningful states** — logged in, form submitted, final confirmation,
   anything irreversible — and check against the goal; if off, adjust and retry.
5. **Hand off when blocked or at a money/irreversible gate.** CAPTCHA / 2FA → hand the
   keyboard to the human in the window. Purchases, deletes, sends → stop at the threshold
   and confirm before proceeding.

The supervision the human wants is at **decisions and irreversible steps** — not at
every mechanical action. Spend the ceremony there; move fast everywhere else.

## Speed defaults

- **Snapshot-first, screenshot-at-checkpoints.** Default to the a11y tree for acting;
  screenshot only when pixels matter or to verify a key state.
- **Batch / parallelize.** Independent tool calls go in one message; chain a known UI
  sequence without intermediate screenshots.
- **Reuse the session — skip the login.** The persistent profile keeps logins. On a
  site you've used before, `browser_navigate` straight to the target and **check
  whether you're already authenticated** before ever touching a login form. This is
  the single biggest time-save — a fresh login is ~6 round-trips.
- **Don't re-snapshot what hasn't changed.** Element refs stay valid until the DOM
  changes; reuse them instead of re-snapshotting between every action.

## Login flow

1. **Reuse the session first.** The Playwright profile persists to disk (unless
   `--isolated`), so once logged in, stay logged in — check whether you're already
   authenticated before re-entering anything.
2. **Credential login — paste the password, don't type it (leak-free).** Read
   per-site creds from your secret store — a git-ignored env file (keys like
   `<SITE>_USER` / `<SITE>_PASS`), macOS Keychain, or a password-manager CLL such as
   1Password. Keep the **password** out of the transcript entirely: copy it straight
   to the clipboard and paste it into the focused field, so the value never enters
   narration, tool args, or the transcript. On macOS:

   ```bash
   printf '%s' "$SITE_PASS" | pbcopy      # macOS; use wl-copy/xclip on Linux, clip.exe on Windows
   ```

   then focus the field, `browser_press_key` `Meta+v` (Ctrl+v on Linux/Windows), and
   clear the clipboard after (`printf '' | pbcopy`). The email/username is usually not
   secret — type it normally. **Never echo a password** — not in narration, a
   screenshot, or a log. (Alternative: human handoff — the human types it in the open window.)
3. **CAPTCHA / 2FA / bot-wall → human handoff.** Stop and say: "Please log in /
   solve this in the open window, then tell me to continue." Resume after. Do NOT
   attempt to defeat the challenge yourself.

## How to invoke (visible + persistent)

- **Visible:** headed by default — do **not** pass `--headless`.
- **Persistent profile:** the default — do **not** pass `--isolated`, so logins persist.
- **Vision / mouse:** run with `--caps=vision` for pixel control.
- **Realistic viewport** (1440×900 or 1920×1080); screenshot key states.
- **Real Chrome channel** when fingerprint matters: `--browser chrome`.

The kit installs the standard Playwright MCP for you (on consent):
`claude mcp add playwright npx @playwright/mcp@latest -- --caps=vision`.

## Why visible / human mode

Claude Code already catches code-level bugs from reading source. The failure modes
that only surface in a real, visible session are:

- **Layout bugs** — overlap, broken small viewports, misaligned real fonts
- **Interaction bugs** — buttons that render but don't respond, focus traps
- **Flow bugs** — multi-step flows that break only under realistic timing (login, OAuth, validation)
- **Rendering bugs** — CSS cascade / rendering quirks / font availability
- **Bot-detection bugs** — CAPTCHAs, fraud screens, rate limits that only appear headless

These are invisible headless — and watching them is the whole point of supervised co-working.

## Scope (non-negotiable)

Act only on systems and accounts you are authorized for (your own). Never evade
detection or act against third parties. Never auto-solve CAPTCHAs — hand to the human.

## Failure modes

- **Login blocked (CAPTCHA/2FA/bot-wall)** — hand off to the human in the open window. Never auto-solve.
- **Browser won't launch** — likely Playwright binaries missing or OS automation permission not granted. Surface the exact error + fix. Do NOT silently fall back to headless.
- **Tool not installed** (Chrome DevTools MCP) — fall back to Playwright; mention the better tool exists if the task would benefit.
- **Window not visible** (multi-monitor) — may open off-screen; offer a reposition nudge (`window.moveTo(100,100)`).
- **Session expired mid-task** — re-run the login flow (reuse → creds → handoff).
