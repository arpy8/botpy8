# botpy8

An autonomous pipeline that plans, builds, and publishes small software
projects in the style of arpy8 (Arpit Sengar), without a human in the loop
per cycle.

## Architecture

Three agents run in sequence, each a plain function module with `run()`
(fresh start) and `resume()` (pick up mid-work) entry points:

1. **Planner** (`agents/planner.py`) — reads `soul.md` (developer identity,
   gitignored, never committed) and `projects.json` (everything already
   built), picks an unused domain, and generates a new project idea: name,
   slug, pitch, a first-person "why I built this" story, stack, file
   structure, and features. Writes `plans/<slug>.json`.
2. **Builder** (`agents/builder.py`) — a tool-using development agent with a
   terminal scoped to `projects/<slug>/` (`agents/workspace.py`). It has
   `run_command`, `write_file`, `read_file`, `list_files` and `finish`
   tools. UI projects start from `create-next-app` (App Router, TypeScript,
   Tailwind) and the agent builds the app on top of that scaffold, installs
   packages with pnpm, adds a smoke test, and runs build/lint/test itself,
   fixing errors as it goes. When it calls `finish`, the harness
   independently runs `pnpm install --frozen-lockfile` → `pnpm run build` → `pnpm test` (or `pytest`)
   and checks the README has a "How it was built" section; failures go back
   to the agent until it passes or runs out of attempts. Model:
   `nvidia/nemotron-3-ultra-550b-a55b:free` via OpenRouter, override with
   `BOTPY8_SOUL_MODEL` (must support tool calling).
   **QA agent** (`agents/qa.py`) — for web apps, once the developer's
   build/tests pass, `finish` also starts the production build and hands it
   to a QA agent that drives it in real Chromium via Playwright (viewport
   1280×800, devicePixelRatio 2, like a typical laptop). It exercises every
   feature in the plan — clicking, dragging, keyboard shortcuts, dialogs,
   save/reload — and verifies each result with screenshots, DOM snapshots,
   console errors and a canvas pixel-diff tool that measures exactly where a
   click painted. Each reproduced bug comes back as a report (component,
   steps, expected, actual, severity); critical/major ones go to the
   developer agent to fix, and QA re-checks them on the next `finish`, up to
   4 rounds. Model: `nvidia/nemotron-3.5-super-vl:free` (vision + tools),
   override with `BOTPY8_SOUL_QA_MODEL`.
3. **Validator/Publisher** (`agents/validator_publisher.py`) — re-runs the
   same verification plus the project's lint script from scratch (on failure
   it hands the output back to the builder instead of deleting the work).
   It then publishes through `agents/github.py`: it creates a public repo
   `botpy8/<slug>`, commits the project there as botpy8 (using its noreply email), and
   pushes. Finally it appends the project and its repo URL to `projects.json`,
   commits that to this repo as you, and pushes to `main`. This repo tracks
   only the registry; `projects/<slug>/` is a local, gitignored build folder.

   **Your own git credentials are never touched.** Bot commits use the
   project repo's *local* `user.name`/`user.email`. The bot token reaches
   git only as an Authorization header in `GIT_CONFIG_*` environment
   variables for that one `git push`, with `credential.helper` reset to empty
   for that process. So git doesn't read your stored GitHub login and doesn't
   save the bot's. The token never appears in a remote URL or any config file.

`orchestrator.py` ties them together: at boot it reads `state.json` and
resumes whatever phase was in progress, or starts a fresh cycle if the state
file is `done`, `failed`, absent, or corrupted.

## Resume behavior

`state.json` (gitignored, atomic read/write via `utils/state.py`) is the
single source of truth for pipeline phase:
`planning → building → validating → publishing → done` (or `failed`).
A crash or reboot at any point is safe to resume from:

- **planning** — Planner is idempotent, just re-runs.
- **building** — the agent restarts with a listing of what's already in
  the project directory (and the last validation failure, if any) and
  continues from there.
- **validating** — re-runs lint/tests/smoke-check from scratch; never
  regenerates source files.
- **publishing** — each of `repo_url`, `code_pushed`, `registry_updated`, `pushed`
  is an independent flag, checked before its sub-step runs and flushed to
  disk immediately after it succeeds, so a crash mid-publish never re-does
  (or skips) a step that already happened.

## Setup

```bash
uv sync
cp .env.example .env   # fill in OPENROUTER_API_KEY and BOTPY8_GITHUB_TOKEN
```

Also required at the repo root (not tracked in git):

- `soul.md` — a developer identity/context document the Planner reads in
  full on every cycle.

## Usage

```bash
make dry-run   # plan + build, skip commit/push — safe to try repeatedly
make run       # full cycle, pushes to main on success
make test      # run the pytest suite
make qa SLUG=glyph    # QA an existing project and print bug reports
make fix SLUG=glyph   # QA it, then have the developer agent fix what QA found
```

## Raspberry Pi setup

1. Flash Raspberry Pi OS (64-bit, ARM64), Python 3.11+ preinstalled or
   installed via `pyenv`/`apt`.
2. Install `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
3. Clone this repo, `cd` into it, `make install`.
4. Add `soul.md` and `.env` manually (both gitignored — copy them over,
   don't generate them on the Pi).
   Node.js 20+ and pnpm are also needed — the builder runs `pnpm dlx
   create-next-app`, `pnpm add`, and `pnpm run build` for UI projects.
5. Confirm `git push` works headlessly (SSH key or credential helper
   already configured for this repo's remote — botpy8_soul does not manage git
   credentials).
6. Run on a schedule via cron, e.g. every 6 hours:
   `0 */6 * * * cd /home/pi/botpy8_soul && make run >> logs/cron.log 2>&1`
7. `make dry-run` first to confirm the pipeline produces a project without
   pushing, before trusting it to run unattended.
