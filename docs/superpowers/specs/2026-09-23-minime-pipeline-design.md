# MiniMe — Core Pipeline Design

## Status
Approved for planning (sub-project 1 of 2 — companion website is a separate spec, built after this).

## Purpose

MiniMe is an autonomous system that repeatedly plans, builds, and publishes small
software projects in the style of Arpit Sengar (arpy8), without human intervention
per cycle. This spec covers the core pipeline only: the three agents, the
orchestrator, and the crash-safe resume mechanism. The companion Next.js website
that displays published projects is a separate sub-project, specified after this
one is implemented, since it only needs to read the artifacts this pipeline
produces.

Success for this sub-project: running `python orchestrator.py` repeatedly (or on
a loop/cron) produces new, working, committed-and-pushed projects under
`projects/<slug>/` in this same repository, each with a genuine, first-person
"why I built this" narrative — and the whole pipeline survives being killed and
restarted at any point without creating duplicate work or corrupting state.

## Non-goals (this spec)

- The companion website (separate spec, separate plan).
- Creating separate GitHub repositories per generated project — all projects
  live as subfolders of this repo (see Decision below).
- GitHub Actions / Vercel deploy wiring — belongs to the website spec.

## Key decision: single-repo publishing

Originally scoped as "create a new GitHub repo per project." Changed per
explicit direction: **all generated projects live as subfolders of the MiniMe
repo itself**, under `projects/<slug>/`. Publishing a project is therefore a
plain local git commit + push to `main` — not a GitHub API repo-creation call.
This removes `GITHUB_TOKEN` and the GitHub REST API from this sub-project's
scope entirely. Git push auth (SSH key / credential helper / `gh` login) is
assumed to already be configured on the host machine; MiniMe does not manage
git credentials.

Publishing pushes directly to `main` — no PR, no review step. This is a
deliberate autonomy choice: the whole point of MiniMe is to run unattended.

## LLM provider

Code generation uses **OpenRouter**, not the NVIDIA endpoint originally
sketched. Matches the existing scratch test in `main.py`:
- `base_url`: `https://openrouter.ai/api/v1`
- API key: env var `OPENROUTER_API_KEY`
- Model: `nvidia/nemotron-3-ultra-550b-a55b:free`
- OpenAI-compatible client (`openai` Python package, `base_url` override)

## Developer identity (`soul.md`)

`soul.md` already exists at the repo root and is the Planner's primary creative
input — a profile of arpy8's skills, aesthetic, naming conventions, and past
projects. It must:
- Be added to `.gitignore` (never committed, in this repo or any generated one).
- Be read in full at the start of every Planner cycle — never cached across
  runs, since it may be hand-edited between cycles.

## Domain rotation

Software-only domains (no physical hardware, runs on a Pi):

| Domain | Examples |
|---|---|
| CLI tools / dev utilities | clipboard tools, GitHub profile tools, pip packages |
| Web apps (full-stack) | FastAPI + Next.js, Dockerized, dark shadcn UI |
| AI / LLM integrations | chat tools, RAG apps, prompt utilities, API wrappers |
| Creative coding / generative | canvas animations, ASCII art, SVG generators, music visualizers |
| ML demos | Streamlit apps, Hugging Face-deployable classifiers |
| Browser tools | WebRTC experiments, browser extensions, interactive HTML pages |
| Python packages | pip-installable utilities wrapping reusable logic |
| Automation / bots | scraping, notification bots, workflow automation |

Planner reads the last-used domain from `state.json` and picks a different one
each cycle (simple round-robin or random-excluding-last is sufficient — no need
for full history tracking).

## Naming convention

Short, lowercase, punchy — 2 to 6 characters preferred, in the style of
`wylt`, `exl`, `lo62`, `baatcheet` (see `soul.md`). Never generic names like
`project-1` or `ai-tool`. Planner must check the chosen slug doesn't already
exist under `projects/` and regenerate if it collides.

## Aesthetic defaults (for any generated project with a UI)

- Dark theme by default (shadcn dark, or equivalent Tailwind dark palette).
- Minimal chrome — personality in small details, not visual noise.
- Tailwind CSS for styling; shadcn/ui components where applicable.
- Astro or Next.js for any static or hybrid site.

## Repository layout (this sub-project's additions)

```
minime/
├── agents/
│   ├── planner.py
│   ├── builder.py
│   └── validator_publisher.py
├── utils/
│   └── state.py              # shared atomic read/write helper
├── orchestrator.py
├── projects/                 # generated projects, committed to this repo
│   └── <slug>/
├── plans/                    # per-cycle plan JSONs — gitignored
├── projects.json             # auto-updated registry (committed)
├── state.json                # runtime pipeline state — gitignored
├── logs/                     # gitignored
├── soul.md                   # developer identity context — gitignored
├── .gitignore
├── .env.example               # OPENROUTER_API_KEY
├── Makefile                   # targets: install, run, dry-run
└── README.md
```

`.gitignore` must include: `soul.md`, `plans/`, `logs/`, `state.json`, `.env`,
`__pycache__/`, `*.pyc`. `projects/` and `projects.json` are **not** gitignored
— they are the whole point of publishing.

## Agents

### 1. Planner (`agents/planner.py`)

- Reads `soul.md` in full (never cached).
- Reads `state.json` for the last-used domain; picks a different one.
- Reads `projects.json` for every previously built project (name, slug, pitch,
  domain, stack) and passes this list into the idea-generation prompt so the
  new idea doesn't repeat a prior project's concept, not just its slug — a
  slug collision check alone is not enough, since the same idea could get a
  different name.
- Generates: project slug (naming convention above, collision-checked against
  `projects/`), one-line pitch, a first-person "story" (why build this — a
  few sentences in arpy8's voice, drawn from `soul.md`'s interests/patterns),
  domain, tech stack, file/folder structure, feature list, aesthetic notes.
- Writes the full plan to `plans/<slug>.json`.
- Updates `state.json`: `phase` → `building`, `project_slug`, `domain`,
  `plan_file`.
- Idempotent: safe to re-run from scratch; always overwrites the plan file.

### 2. Builder (`agents/builder.py`)

- Reads `plans/<slug>.json`.
- Scaffolds source files, configs, and dependencies under `projects/<slug>/`.
- Before writing each file, checks whether it already exists (resume support);
  skips files already present, still proceeds to later steps.
- Applies aesthetic defaults to any UI layer.
- Generates `projects/<slug>/README.md` covering: Overview, Tech Stack, Setup,
  Usage, Architecture, and "How it was built" — the last section is the story
  content from the plan, written as a genuine first-person build narrative,
  not a template fill-in.
- Uses the LLM client described above for all code generation.
- Runs the project's smoke-check (`pytest` / `npm test` / `make test`,
  whichever the stack implies) and records pass/fail — always re-run, even on
  resume, since a partial build is not a passed build.
- Updates `state.json`: `phase` → `validating` once build + smoke-check pass.

### 3. Validator + Publisher (`agents/validator_publisher.py`)

- Runs lint, tests, and a final smoke check from scratch (does not trust the
  Builder's last run when resuming into this phase).
- On validation success, updates `state.json`: `phase` → `publishing`, then
  executes publish sub-steps in order, flushing `state.json` immediately after
  each one succeeds (each flag independently idempotent on resume):
  - `files_committed` — `git add projects/<slug>/`
  - `registry_updated` — append entry to `projects.json` (name, slug, pitch,
    story, domain, stack, path, timestamp), `git add projects.json`
  - `pushed` — `git commit` (single commit covering both) + `git push origin main`
- Sets `phase` → `done` once all three flags are `true`.
- On validation failure: returns errors to the Builder for one retry
  (`state.json.retry_count` incremented). If the retry also fails: `phase` →
  `failed`, error details logged to `logs/`, workspace and plan left on disk
  for inspection. The orchestrator must never block on a failed cycle — it
  starts a fresh cycle next.

## State file — `state.json`

Single source of truth for pipeline state, written atomically (temp file +
`os.replace()`) after every meaningful transition.

```json
{
  "run_id": "2026-09-23_03-41",
  "phase": "building",
  "project_slug": "vxlt",
  "domain": "cli-tools",
  "plan_file": "plans/vxlt.json",
  "files_committed": false,
  "registry_updated": false,
  "pushed": false,
  "retry_count": 0,
  "last_updated": "2026-09-23T03:47:12Z"
}
```

`phase` ∈ `planning | building | validating | publishing | done | failed`.

### Atomic write helper — `utils/state.py`

```python
import json, os, tempfile

STATE_FILE = "state.json"

def read_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def write_state(state: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(STATE_FILE))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tf:
        json.dump(state, tf, indent=2)
        tmp_path = tf.name
    os.replace(tmp_path, STATE_FILE)
```

All agents import and use only `read_state` / `write_state` — never open
`state.json` directly.

## Per-phase resume behaviour

- **`planning`** — plan file may be partial/absent. Re-run Planner in full;
  overwrite `plans/<slug>.json`. Safe to redo.
- **`building`** — `projects/<slug>/` may be partially written. Builder skips
  files that already exist. Smoke-check always re-run.
- **`validating`** — workspace is complete; re-run lint + tests + smoke-check
  from scratch. Do not regenerate source files.
- **`publishing`** — check each boolean flag before its sub-step; skip if
  already `true`. Flush `state.json` after each sub-step.

## Orchestrator (`orchestrator.py`)

- Boot-time resume logic runs first, before anything else:
  1. Read `state.json`.
  2. If `phase` is `done`, `failed`, or the file is absent/malformed → start a
     fresh cycle.
  3. Otherwise → print `[resume] Restarting from phase: <phase> — project:
     <slug>` and call the appropriate agent's `resume(state)` entry point.
- Each agent exposes two entry points: `run(plan)` (fresh start) and
  `resume(state)` (pick up mid-work from disk).
- Lightweight scheduler: simple loop with a configurable sleep interval
  (env var or CLI flag), or designed to be invoked by an external cron —
  either works, no daemon process required.
- Logs each run to `logs/YYYY-MM-DD_HH-MM.log`.
- Headless: ARM64, Python 3.11+, no GUI dependencies.
- Recovers from any agent exception without crashing the loop: catches,
  logs, sets `phase` → `failed`, continues to the next cycle.
- `--dry-run`: runs Planner + Builder, skips Validator's publish sub-steps
  entirely (commit/push never happen).

## Error handling summary

- Builder/Validator exceptions are caught per-cycle, never propagate to crash
  the orchestrator loop.
- One retry at the Builder step on validation failure; second failure moves
  the cycle to `failed` and stops touching that slug.
- All secrets via environment variables only (`OPENROUTER_API_KEY`) — never
  hardcoded, never written into a generated project.

## Testing

- Every generated project includes a runnable smoke-check appropriate to its
  stack (`pytest`, `npm test`, or `make test`).
- MiniMe itself gets pytest coverage for: atomic state read/write correctness,
  resume behavior from each phase (given a hand-crafted `state.json` fixture),
  domain rotation never repeating consecutively, and slug collision handling.
- Code style: `ruff` + `black` for Python.

## Deliverables

1. `utils/state.py` — atomic state helper.
2. `agents/planner.py`, `agents/builder.py`, `agents/validator_publisher.py` —
   each with `run()` and `resume()`.
3. `orchestrator.py` with boot-time resume logic and `--dry-run`.
4. `.gitignore` covering all local/identity/runtime files.
5. `.env.example` (`OPENROUTER_API_KEY`).
6. `Makefile` — targets: `install`, `run`, `dry-run`.
7. Pytest suite covering state helper + resume behavior.
8. Top-level `README.md`: architecture, agent pipeline, resume behavior, Pi
   setup guide.
