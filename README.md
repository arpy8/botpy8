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
- **paused** — if the LLM's daily quota runs out in any phase, the project
  keeps that phase and `state.json` records `paused_until`; runs before then
  exit immediately, and the next one after it resumes where it stopped.
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
make loop      # cycles back to back; sleeps through daily-quota pauses
make test      # run the pytest suite
make qa SLUG=glyph    # QA an existing project and print bug reports
make fix SLUG=glyph   # QA it, then have the developer agent fix what QA found
```

## Raspberry Pi setup

Runs well on a Pi 4/5 with 8 GB RAM (4 GB at a stretch), Raspberry Pi OS
64-bit (Bookworm), and an SSD or a large SD card: `node_modules` and Chromium
take several GB, and each project adds more.

1. Install system tools, Node.js 22 + pnpm, and uv:
   ```bash
   sudo apt update && sudo apt install -y git make curl
   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
   sudo corepack enable && corepack prepare pnpm@latest --activate
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Clone and install (includes Playwright's Chromium and its system libraries):
   ```bash
   git clone https://github.com/arpy8/botpy8_soul ~/botpy8_soul && cd ~/botpy8_soul
   uv sync && uv run playwright install --with-deps chromium
   ```
3. Copy `.env` (`OPENROUTER_API_KEY`, `BOTPY8_GITHUB_TOKEN`) and `soul.md` over
   from your machine. Both are gitignored, so never commit them.
4. Give the Pi push access to this repo as you, because the registry commit is
   pushed here. Use `gh auth login && gh auth setup-git`, or a deploy key with
   write access to this repo only. The bot token in `.env` covers the
   per-project repos.
5. Smoke test: `make dry-run` (plan, build and QA, without publishing), then `make run`.
6. Run it continuously as a service:
   ```bash
   sudo cp deploy/botpy8-soul.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now botpy8-soul
   journalctl -u botpy8-soul -f
   ```

`--loop` runs one cycle after another (5 minutes apart; change it with
`--interval`). Only one run can hold `.botpy8_soul.lock` at a time, so a
manual `make run` while the service is busy exits immediately. When the free
OpenRouter quota (about 1,000 requests a day) runs out, the current project
keeps its phase and the loop sleeps until the quota resets. Expect roughly
1–3 projects a day on the free tier.
