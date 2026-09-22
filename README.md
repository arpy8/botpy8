# MiniMe

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
2. **Builder** (`agents/builder.py`) — reads the plan, scaffolds
   `projects/<slug>/` via an LLM (OpenRouter, `nvidia/nemotron-3-ultra-550b-a55b:free`),
   writes a README with an Overview/Stack/Setup/Usage/Architecture/"How it
   was built" structure, and runs the project's own smoke-check.
3. **Validator/Publisher** (`agents/validator_publisher.py`) — re-runs lint
   and the smoke-check from scratch, then commits `projects/<slug>/` and an
   updated `projects.json` and pushes straight to `main`. All generated
   projects live as subfolders of this repo — there is no per-project GitHub
   repo and no `GITHUB_TOKEN`.

`orchestrator.py` ties them together: at boot it reads `state.json` and
resumes whatever phase was in progress, or starts a fresh cycle if the state
file is `done`, `failed`, absent, or corrupted.

## Resume behavior

`state.json` (gitignored, atomic read/write via `utils/state.py`) is the
single source of truth for pipeline phase:
`planning → building → validating → publishing → done` (or `failed`).
A crash or reboot at any point is safe to resume from:

- **planning** — Planner is idempotent, just re-runs.
- **building** — Builder skips any file that already exists on disk, but
  always re-runs the smoke-check.
- **validating** — re-runs lint/tests/smoke-check from scratch; never
  regenerates source files.
- **publishing** — each of `files_committed`, `registry_updated`, `pushed`
  is an independent flag, checked before its sub-step runs and flushed to
  disk immediately after it succeeds, so a crash mid-publish never re-does
  (or skips) a step that already happened.

## Setup

```bash
uv sync
cp .env.example .env   # fill in OPENROUTER_API_KEY
```

Also required at the repo root (not tracked in git):

- `soul.md` — a developer identity/context document the Planner reads in
  full on every cycle.

## Usage

```bash
make dry-run   # plan + build, skip commit/push — safe to try repeatedly
make run       # full cycle, pushes to main on success
make test      # run the pytest suite
```

## Raspberry Pi setup

1. Flash Raspberry Pi OS (64-bit, ARM64), Python 3.11+ preinstalled or
   installed via `pyenv`/`apt`.
2. Install `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
3. Clone this repo, `cd` into it, `make install`.
4. Add `soul.md` and `.env` manually (both gitignored — copy them over,
   don't generate them on the Pi).
5. Confirm `git push` works headlessly (SSH key or credential helper
   already configured for this repo's remote — MiniMe does not manage git
   credentials).
6. Run on a schedule via cron, e.g. every 6 hours:
   `0 */6 * * * cd /home/pi/minime && make run >> logs/cron.log 2>&1`
7. `make dry-run` first to confirm the pipeline produces a project without
   pushing, before trusting it to run unattended.
