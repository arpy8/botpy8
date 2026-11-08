# botpy8

An autonomous pipeline that plans, builds, tests and publishes small software
projects in the style of arpy8 (Arpit Sengar), **one project a night**, with
no human in the loop. Each finished project gets its own public repo on the
[botpy8](https://github.com/botpy8) account. This repo holds the pipeline,
the lessons it has learned (`playbook.md`) and a registry of everything it has
built (`projects.json`).

Built so far: [glyph](https://github.com/botpy8/glyph).

## How it works

```
 Supervisor (orchestrator.py: plain code, a state machine with budgets, retries, a schedule and a lock)

 product ─► approval ─► design ─► scaffold ─► tests ─► develop ─► review ─► qa ─► release ─► retro ─► publish
  (LLM)     (human,     (LLM)     (code)      (LLM)    (LLM per    (code     (LLM    (code)     (LLM)    (code)
            optional)                                    task)     + LLM)   + code)
```

The principle: **LLMs do the open-ended work, and code enforces every check.**
Each agent hands the next one a typed artifact (`utils/contracts.py`, validated
with pydantic). Invalid output goes back to the agent that produced it, and
nothing moves forward until a deterministic gate passes.

| Phase | Agent | What it does |
|---|---|---|
| product | `agents/product.py` | Reads `soul.md` and `projects.json`, picks a domain, and writes `plans/<slug>.spec.json`: the idea, a first-person build story, and 3–8 features, each with **Given/When/Then acceptance criteria** a test can check. Picks a template. |
| approval | supervisor | Optional and off by default. Notifies you and waits for `make approve` or `make reject`, approving automatically after a timeout. |
| design | `agents/architect.py` | Writes `plans/<slug>.design.json`: modules, the data model, state and edge-case notes, and a **task graph**. Code rejects cycles, unknown dependencies, and any criterion that no task covers. |
| scaffold | `agents/scaffold.py` | Copies one of **our own templates** (`templates/nextjs`, `templates/python-cli`), installs it, and requires the template's own checks to pass. The project becomes its own git repo. |
| tests | `agents/test_author.py` | Writes one acceptance test per criterion **before any code** (Playwright for web apps, pytest driving the CLI for tools), plus a `CONTRACT.md` listing every selector and command the tests use. Tests are fingerprinted, so only the Test Author may change them. |
| develop | `agents/developer.py` | Implements the task graph **one task at a time**. Each task gets a short, focused brief and its own gate: typecheck, lint, unit tests, build, and that task's acceptance tests. The harness commits each task. The Developer can dispute a test it believes is wrong; the Test Author then fixes it or explains why it stands. Finally the Developer writes the README and must pass the full gate. |
| review | `agents/reviewer.py` | Code checks for unfinished work (TODO, FIXME, console.log). Then an LLM reviews the diff against the spec, design, contract and playbook. Blocking findings go back to the Developer, for at most 2 rounds. |
| qa | `agents/qa.py` | Web apps only. Runs the production build in Chromium (1280×800, devicePixelRatio 2) and explores beyond the written tests, using screenshots, DOM snapshots, canvas pixel diffs, safe storage inspection and console errors. Every blocking bug first becomes a **failing regression test** (written by the Test Author), and then the Developer fixes it. QA re-checks known bugs every round, for at most 4 rounds. |
| release | `agents/release.py` | Starting from a clean install, checks typecheck, lint, every unit, acceptance and regression test, the build, the README, that tests are untouched, that Tailwind is wired up, and that no checks are disabled. One fix round is allowed. |
| retro | `agents/retro.py` | Turns this project's bugs and review findings into general lessons in `playbook.md`. The Architect, Developer and Reviewer read the playbook on every later project. |
| publish | `agents/publisher.py` | Creates the public repo `botpy8/<slug>` and pushes the project's history as the bot. Then commits `projects.json` (and any new playbook lessons) to this repo as you. |

All agents share one tool loop (`agents/agent.py`). It gives them a terminal and
file tools scoped to the project (`agents/workspace.py`; Git Bash on Windows),
plus `finish`. Calling `finish` doesn't end the work by itself; the agent's gate
decides whether it's done.

### Built-in guard rails

- **Templates we own.** `templates/nextjs` is Next.js 16 with TypeScript,
  Tailwind v4, Vitest, Playwright (at devicePixelRatio 2) and pnpm. It also has
  ESLint rules for bug patterns QA has caught before (`eslint-rules/botpy8.mjs`):
  - native `alert`, `confirm` and `prompt`
  - `devicePixelRatio` in pointer math
  - global key handlers that swallow typing in inputs

  `templates/python-cli` is uv, pytest, ruff and hatchling.
- **Tests are the spec.** Acceptance tests are written before the code, the
  Developer can't edit them, and every QA bug becomes a regression test.
- **Lessons compound.** `playbook.md` (committed) grows after every project.
  It was seeded with what went wrong building glyph.
- **Your git credentials are never touched.** Bot commits use each project
  repo's *local* identity. The bot token reaches git only as an Authorization
  header for a single process, with `credential.helper` reset. It never appears
  in a URL or a file.

## Schedule

`make loop` (what the Pi service runs) keeps the current project moving at all
times: through the day and across midnight. It pauses only when the LLM quota
runs out, and continues after the reset.

A **new** project starts only when both of these are true:

1. The daily start time has been reached (`BOTPY8_SOUL_START_AT`, default
   `00:00` in the machine's local time zone).
2. The previous project was already finished at that start time.

A project that takes longer than a day just continues. Once it's done, the next
project waits for the following start time. So at most one new project starts
per day.

If the machine was off at the start time, that day's project starts when it
comes back, as long as nothing was in progress. A project that fails twice is
parked (kept on disk, and you're notified), so it never blocks the schedule.

`make status` shows the next start time and why a new project would or
wouldn't start.

## State, budgets and resume

State lives in SQLite (`botpy8_soul.db`, gitignored). It holds one row per
project, an event log, and every LLM call with its token counts. The supervisor
saves after every step and agents checkpoint after every task, so after a crash
or reboot the pipeline resumes where it stopped.

Policies the supervisor owns:

- **One run at a time.** An OS-level lock on `.botpy8_soul.lock` means a
  manual `make run` exits immediately while the service is busy.
- **Daily budget.** It stops at `BOTPY8_SOUL_DAILY_BUDGET` requests (default
  950). That's under OpenRouter's daily limit for free models, which is 1000
  requests on accounts that have bought at least $10 of credits (50
  otherwise). If the quota
  runs out anyway, the project **pauses in place** until OpenRouter resets it
  at 00:00 UTC (05:30 in India).
- **Retry, then park.** A failed phase is retried once on the next cycle. A
  second failure parks the project and notifies you.
- **Models per role.** `BOTPY8_SOUL_MODEL_<ROLE>` overrides the default model
  for product, architect, test_author, developer, reviewer, qa or retro. A
  project typically takes several hundred requests, so on free models it can
  span more than one quota day. Paid models remove that limit.

## Setup

```bash
make install           # uv sync + Playwright's Chromium
cp .env.example .env   # fill in OPENROUTER_API_KEY and BOTPY8_GITHUB_TOKEN
```

These must also be in place:

- `soul.md` at the repo root (gitignored). It's the developer identity the
  Product agent reads.
- Node.js 20+ with pnpm, and `uv`, on PATH.
- For the registry commit, push access to this repo as you.

### Configuration (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `OPENROUTER_API_KEY` | required | LLM access |
| `BOTPY8_GITHUB_TOKEN` | required to publish | Bot account token: a classic token with only the `public_repo` scope |
| `BOTPY8_SOUL_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` | Default model for every agent |
| `BOTPY8_SOUL_MODEL_<ROLE>` | QA: `nvidia/nemotron-3.5-super-vl:free` | Per-role model override |
| `BOTPY8_SOUL_START_AT` | `00:00` | Local time a new project may start |
| `BOTPY8_SOUL_DAILY_BUDGET` | `950` | LLM requests per day before pausing |
| `BOTPY8_SOUL_APPROVAL` | `auto` | Set to `manual` to approve each idea first |
| `BOTPY8_SOUL_APPROVAL_TIMEOUT_MIN` | `60` | Minutes before an idea is approved automatically |
| `BOTPY8_SOUL_NTFY_TOPIC` / `_NTFY_URL` | off | Phone notifications via [ntfy](https://ntfy.sh) |
| `BOTPY8_SOUL_SANDBOX` | `local` | Set to `docker` to run every command in a per-project container |
| `BOTPY8_SOUL_SANDBOX_MEMORY` / `_CPUS` | `3g` / `2` | Container limits |

## Usage

```bash
make dry-run          # one cycle, everything except publishing
make run              # one cycle
make loop             # the scheduled loop (see Schedule)
make status           # projects, phases, today's request budget, schedule, recent events
make approve          # approve the idea waiting for approval (BOTPY8_SOUL_APPROVAL=manual)
make reject
make qa SLUG=glyph    # QA an existing project and print bug reports
make test             # the pipeline's own test suite
```

`orchestrator.py --loop --dry-run` runs the schedule without publishing.
While a project is in progress, `--loop` runs a cycle every 5 minutes
(`--interval`). Between projects, or during a quota pause, it sleeps until the
next start time or reset, waking at least every 30 minutes to log its state.

**Notifications:** set `BOTPY8_SOUL_NTFY_TOPIC` and subscribe to that topic in
the ntfy app. You'll get new ideas (when approval is on), published projects
and parked projects on your phone.

**Docker sandbox:** with `BOTPY8_SOUL_SANDBOX=docker`, every agent command,
gate and the QA app server run inside a per-project container. The image is
built from `deploy/sandbox.Dockerfile` on first use, and containers get CPU and
memory limits and shared caches. The default (`local`) scopes commands to the
project folder, but it is not a security boundary.

## Repository layout

```
orchestrator.py        supervisor: state machine, schedule, budget, CLI
agents/                one module per phase, plus agent.py (tool loop), gates.py,
                       checks.py, workspace.py, sandbox.py, repo.py, github.py
utils/                 contracts, store (SQLite), llm, schedule, lock, notify
templates/             project templates the scaffold copies
playbook.md            lessons learned, read by agents on every project
projects.json          registry of published projects
projects/<slug>/       working copies (gitignored)
plans/                 specs and designs per project
deploy/                systemd unit and sandbox Dockerfile
tests/                 the pipeline's test suite
```

## Raspberry Pi setup

It runs well on a Pi 4 or 5 with 8 GB of RAM (4 GB at a stretch), Raspberry Pi
OS 64-bit (Bookworm), and an SSD or a large SD card. `node_modules` and
Chromium take several GB, and each project adds more.

1. Install system tools, Node.js 22 with pnpm, and uv:
   ```bash
   sudo apt update && sudo apt install -y git make curl
   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
   sudo corepack enable && corepack prepare pnpm@latest --activate
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Clone and install (this includes Playwright's Chromium and its system
   libraries):
   ```bash
   git clone https://github.com/arpy8/botpy8_soul ~/botpy8_soul && cd ~/botpy8_soul
   uv sync && uv run playwright install --with-deps chromium
   ```
3. Copy `.env` and `soul.md` over from your machine. Both are gitignored, so
   never commit them.
4. Set the time zone (`sudo raspi-config` → Localisation), so `00:00` means your
   midnight.
5. Give the Pi push access to this repo as you, because the registry commit is
   pushed here. Use `gh auth login && gh auth setup-git`, or a deploy key with
   write access to this repo only. The bot token in `.env` covers the
   per-project repos.
6. Smoke test with `make dry-run`, then `make run`.
7. Run it continuously as a service:
   ```bash
   sudo cp deploy/botpy8-soul.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now botpy8-soul
   journalctl -u botpy8-soul -f
   ```
