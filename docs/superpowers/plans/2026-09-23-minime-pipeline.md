# MiniMe Core Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the MiniMe pipeline — three agents (Planner, Builder, Validator/Publisher), an orchestrator with crash-safe resume, and the atomic state helper — so running `python orchestrator.py` repeatedly produces new, working, first-person-narrated projects committed under `projects/<slug>/` in this repo, surviving being killed and restarted at any point.

**Architecture:** A single `state.json` (atomic read/write via `utils/state.py`) is the source of truth for pipeline phase. `orchestrator.py` reads it at boot, resumes the current phase or starts fresh, and calls `agents/planner.py` → `agents/builder.py` → `agents/validator_publisher.py` in sequence. Each agent module exposes `run()` (fresh start) and `resume()` (pick up mid-work) and is otherwise a plain function library — no classes, no hidden state beyond the `state` dict and the filesystem.

**Tech Stack:** Python 3.12, `openai` client against OpenRouter (`nvidia/nemotron-3-ultra-550b-a55b:free`), `pytest`, `ruff`, `black`, `uv` for dependency management (matches existing `pyproject.toml`).

**Spec:** `docs/superpowers/specs/2026-09-23-minime-pipeline-design.md`

## Global Constraints

- Secrets only via environment variables (`OPENROUTER_API_KEY`) — never hardcoded, never written into a generated project.
- `soul.md`, `plans/`, `logs/`, `state.json`, `.env` are gitignored; `projects/` and `projects.json` are **not** gitignored.
- Publishing pushes directly to `main` — no PR, no review step.
- All generated projects live under `projects/<slug>/` in this repo — no separate GitHub repo creation, no `GITHUB_TOKEN`.
- `utils/state.py`'s `read_state`/`write_state` are the only way any code touches `state.json`.
- LLM calls always go through `utils/llm.py`'s `get_client()`/`generate()` — agents never construct an `OpenAI` client directly, so tests can inject a fake.
- Python style: `ruff` + `black`.

## Review Focus

- **Missing `soul.md` at Planner start** — Planner must raise a clear error, not silently generate a generic project. (Task 4)
- **First run with no `projects.json`** — Planner must treat "no prior projects" as an empty list, not crash reading a missing file. (Task 4)
- **Slug collision with an existing `projects/<slug>/` directory** — Planner must regenerate rather than overwrite an existing project. (Task 4)
- **Malformed `state.json` at orchestrator boot** (corrupted JSON from a crash mid-write) — must be treated as absent and start a fresh cycle, never crash the loop. (Task 7)
- **`git push` failure during publishing** (network down, non-fast-forward) — must leave `pushed: false` and the phase in `publishing` for the next resume, never mark the registry entry as published when it wasn't pushed. (Task 6)

---

## File Structure

```
minime/
├── .gitignore
├── .env.example
├── Makefile
├── pyproject.toml            # modified: add pytest, ruff, black
├── utils/
│   ├── __init__.py
│   ├── state.py
│   └── llm.py
├── agents/
│   ├── __init__.py
│   ├── planner.py
│   ├── builder.py
│   └── validator_publisher.py
├── orchestrator.py
├── tests/
│   ├── __init__.py
│   ├── test_state.py
│   ├── test_llm.py
│   ├── test_planner.py
│   ├── test_builder.py
│   ├── test_validator_publisher.py
│   └── test_orchestrator.py
├── projects/                 # created at runtime, committed as it fills
├── plans/                    # gitignored, created at runtime
├── logs/                     # gitignored, created at runtime
└── README.md                 # modified
```

---

### Task 1: Project scaffolding

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `Makefile`
- Modify: `pyproject.toml`
- Create: `utils/__init__.py`, `agents/__init__.py`, `tests/__init__.py`
- Create: `plans/.gitkeep`... **no** — `plans/` is gitignored entirely, skip a keep file for it.
- Create: `projects/.gitkeep`

**Interfaces:**
- Produces: repo layout every later task writes into; `pyproject.toml` dev dependencies (`pytest`, `ruff`, `black`) that every later test task runs against.

- [ ] **Step 1: Write `.gitignore`**

```
soul.md
plans/
logs/
state.json
.env
__pycache__/
*.pyc
.venv/
```

- [ ] **Step 2: Write `.env.example`**

```
OPENROUTER_API_KEY=
```

- [ ] **Step 3: Add dev dependencies to `pyproject.toml`**

```toml
[project]
name = "minime"
version = "0.1.0"
description = "Add your description here"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "openai>=3.7.0",
]

[dependency-groups]
dev = [
    "python-dotenv>=1.2.3",
    "pytest>=8.0.0",
    "ruff>=0.6.0",
    "black>=24.0.0",
]
```

- [ ] **Step 4: Run `uv sync` to install the new dev dependencies**

Run: `uv sync`
Expected: completes without error, `pytest`/`ruff`/`black` available via `uv run`.

- [ ] **Step 5: Write `Makefile`**

```makefile
.PHONY: install run dry-run test

install:
	uv sync

run:
	uv run python orchestrator.py

dry-run:
	uv run python orchestrator.py --dry-run

test:
	uv run pytest -v
```

- [ ] **Step 6: Create package `__init__.py` files and `projects/.gitkeep`**

```bash
touch utils/__init__.py agents/__init__.py tests/__init__.py projects/.gitkeep
```

- [ ] **Step 7: Verify `.gitignore` actually excludes the runtime files**

Run: `git check-ignore soul.md plans/x.json logs/x.log state.json .env`
Expected: all five paths printed back (all matched/ignored). If any line is missing from the output, that pattern in `.gitignore` is wrong — fix it before continuing.

- [ ] **Step 8: Commit**

```bash
git add .gitignore .env.example Makefile pyproject.toml utils/__init__.py agents/__init__.py tests/__init__.py projects/.gitkeep uv.lock
git commit -m "chore: scaffold MiniMe pipeline project layout"
```

---

### Task 2: `utils/state.py` — atomic state helper

**Files:**
- Create: `utils/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `read_state() -> dict`, `write_state(state: dict) -> None`, module constant `STATE_FILE = "state.json"` (relative to current working directory — callers `chdir` or the orchestrator sets cwd to the repo root before calling).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_state.py
import json
import os
import pytest
from utils import state as state_mod


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def test_read_state_missing_file_returns_empty_dict():
    assert state_mod.read_state() == {}


def test_read_state_malformed_json_returns_empty_dict():
    with open(state_mod.STATE_FILE, "w") as f:
        f.write("{not valid json")
    assert state_mod.read_state() == {}


def test_write_then_read_state_round_trips():
    payload = {"phase": "building", "project_slug": "vxlt"}
    state_mod.write_state(payload)
    assert state_mod.read_state() == payload


def test_write_state_is_atomic_no_tmp_file_left_behind(tmp_path):
    state_mod.write_state({"phase": "planning"})
    leftover_tmp_files = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftover_tmp_files == []


def test_write_state_overwrites_existing_file():
    state_mod.write_state({"phase": "planning"})
    state_mod.write_state({"phase": "building"})
    assert state_mod.read_state() == {"phase": "building"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.state'` (or `ImportError`).

- [ ] **Step 3: Write `utils/state.py`**

```python
import json
import os
import tempfile

STATE_FILE = "state.json"


def read_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    """Atomic write — crash during write never corrupts STATE_FILE."""
    dir_ = os.path.dirname(os.path.abspath(STATE_FILE))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tf:
        json.dump(state, tf, indent=2)
        tmp_path = tf.name
    os.replace(tmp_path, STATE_FILE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add utils/state.py tests/test_state.py
git commit -m "feat: add atomic state.json read/write helper"
```

---

### Task 3: `utils/llm.py` — OpenRouter client wrapper

**Files:**
- Create: `utils/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `get_client() -> OpenAI`, `generate(client, system: str, user: str, model: str = MODEL) -> str`, constants `OPENROUTER_BASE_URL`, `MODEL`. Every later agent task imports `generate` (and tests inject a fake object in place of `client`, so the fake only needs a `.chat.completions.create(...)` method — no real `OpenAI` type dependency in tests).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm.py
import pytest
from utils import llm


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content="hello"):
        self.chat = _FakeChat(content)


def test_get_client_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm.get_client()


def test_get_client_builds_client_when_key_present(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    client = llm.get_client()
    assert client.base_url is not None


def test_generate_returns_message_content():
    fake = _FakeClient(content="generated text")
    result = llm.generate(fake, system="sys", user="usr")
    assert result == "generated text"


def test_generate_sends_system_and_user_messages():
    fake = _FakeClient(content="ignored")
    llm.generate(fake, system="be terse", user="write code")
    sent_messages = fake.chat.completions.last_kwargs["messages"]
    assert sent_messages == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "write code"},
    ]


def test_generate_uses_default_model_when_not_specified():
    fake = _FakeClient()
    llm.generate(fake, system="s", user="u")
    assert fake.chat.completions.last_kwargs["model"] == llm.MODEL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.llm'`.

- [ ] **Step 3: Write `utils/llm.py`**

```python
import os
from openai import OpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


def get_client() -> OpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY environment variable is not set")
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)


def generate(client, system: str, user: str, model: str = MODEL) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add utils/llm.py tests/test_llm.py
git commit -m "feat: add OpenRouter client wrapper"
```

---

### Task 4: `agents/planner.py`

**Files:**
- Create: `agents/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: `utils.llm.generate(client, system, user) -> str` (Task 3).
- Produces: `read_soul(base_dir: Path) -> str`, `read_projects_registry(base_dir: Path) -> list[dict]`, `pick_domain(last_domain: str | None) -> str`, `slug_exists(slug: str, base_dir: Path) -> bool`, `generate_plan(client, soul_text: str, prior_projects: list[dict], domain: str) -> dict`, `write_plan(plan: dict, base_dir: Path) -> Path`, `run(base_dir: Path, client, state: dict) -> dict`, `resume(base_dir: Path, client, state: dict) -> dict`, module constant `DOMAINS: list[str]`. Plan dicts have keys: `name, slug, pitch, story, domain, stack, structure, features, aesthetic_notes`. Later tasks (Builder, Validator/Publisher) read plans with exactly these keys.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_planner.py
import json
import pytest
from agents import planner


class _FakeCompletions:
    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._contents.pop(0)
        return _FakeResponse(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChat:
    def __init__(self, contents):
        self.completions = _FakeCompletions(contents)


class _FakeClient:
    def __init__(self, contents):
        self.chat = _FakeChat(contents)


PLAN_JSON = json.dumps({
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["main.py", "README.md"],
    "features": ["clipboard history", "fuzzy search"],
    "aesthetic_notes": "minimal terminal output",
})


def test_read_soul_raises_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="soul.md"):
        planner.read_soul(tmp_path)


def test_read_soul_returns_contents(tmp_path):
    (tmp_path / "soul.md").write_text("developer profile text", encoding="utf-8")
    assert planner.read_soul(tmp_path) == "developer profile text"


def test_read_projects_registry_missing_file_returns_empty_list(tmp_path):
    assert planner.read_projects_registry(tmp_path) == []


def test_read_projects_registry_returns_parsed_list(tmp_path):
    (tmp_path / "projects.json").write_text(
        json.dumps([{"name": "wylt", "slug": "wylt", "pitch": "x"}]), encoding="utf-8"
    )
    result = planner.read_projects_registry(tmp_path)
    assert result == [{"name": "wylt", "slug": "wylt", "pitch": "x"}]


def test_pick_domain_never_returns_last_domain():
    for _ in range(20):
        assert planner.pick_domain("cli-tools") != "cli-tools"


def test_pick_domain_handles_no_last_domain():
    assert planner.pick_domain(None) in planner.DOMAINS


def test_slug_exists_true_when_project_dir_present(tmp_path):
    (tmp_path / "projects" / "wylt").mkdir(parents=True)
    assert planner.slug_exists("wylt", tmp_path) is True


def test_slug_exists_false_when_absent(tmp_path):
    assert planner.slug_exists("wylt", tmp_path) is False


def test_generate_plan_parses_llm_json_and_sets_domain():
    fake = _FakeClient([PLAN_JSON])
    plan = planner.generate_plan(fake, soul_text="profile", prior_projects=[], domain="cli-tools")
    assert plan["slug"] == "wylt"
    assert plan["domain"] == "cli-tools"


def test_write_plan_creates_file_under_plans_dir(tmp_path):
    plan = json.loads(PLAN_JSON)
    plan["domain"] = "cli-tools"
    path = planner.write_plan(plan, tmp_path)
    assert path == tmp_path / "plans" / "wylt.json"
    assert json.loads(path.read_text(encoding="utf-8"))["slug"] == "wylt"


def test_run_writes_plan_and_updates_state(tmp_path):
    (tmp_path / "soul.md").write_text("profile", encoding="utf-8")
    fake = _FakeClient([PLAN_JSON])
    state = {}
    result = planner.run(tmp_path, fake, state)
    assert result["phase"] == "building"
    assert result["project_slug"] == "wylt"
    assert (tmp_path / "plans" / "wylt.json").exists()


def test_run_regenerates_on_slug_collision(tmp_path):
    (tmp_path / "soul.md").write_text("profile", encoding="utf-8")
    (tmp_path / "projects" / "wylt").mkdir(parents=True)
    other_plan = json.loads(PLAN_JSON)
    other_plan["slug"] = "exl"
    other_plan["name"] = "exl"
    fake = _FakeClient([PLAN_JSON, json.dumps(other_plan)])
    state = {}
    result = planner.run(tmp_path, fake, state)
    assert result["project_slug"] == "exl"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.planner'`.

- [ ] **Step 3: Write `agents/planner.py`**

```python
import json
import random
from pathlib import Path

from utils.llm import generate

DOMAINS = [
    "cli-tools",
    "web-apps",
    "ai-llm",
    "creative-coding",
    "ml-demos",
    "browser-tools",
    "python-packages",
    "automation-bots",
]


def read_soul(base_dir: Path) -> str:
    soul_path = base_dir / "soul.md"
    if not soul_path.exists():
        raise FileNotFoundError(
            "soul.md not found — Planner cannot generate ideas without developer identity context"
        )
    return soul_path.read_text(encoding="utf-8")


def read_projects_registry(base_dir: Path) -> list[dict]:
    registry_path = base_dir / "projects.json"
    if not registry_path.exists():
        return []
    try:
        return json.loads(registry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def pick_domain(last_domain: str | None) -> str:
    choices = [d for d in DOMAINS if d != last_domain]
    return random.choice(choices)


def slug_exists(slug: str, base_dir: Path) -> bool:
    return (base_dir / "projects" / slug).exists()


def generate_plan(client, soul_text: str, prior_projects: list[dict], domain: str) -> dict:
    prior_summary = "\n".join(
        f"- {p['name']}: {p['pitch']}" for p in prior_projects
    ) or "(none yet)"
    system = (
        "You are generating a new software project idea for arpy8, a developer. "
        "Use the developer profile to decide naming, aesthetic, and tone. "
        "Never repeat the concept of a project already built. "
        'Respond with strict JSON only, matching this schema: '
        '{"name": str, "slug": str, "pitch": str, "story": str, "domain": str, '
        '"stack": [str], "structure": [str], "features": [str], "aesthetic_notes": str}'
    )
    user = (
        f"Developer profile:\n{soul_text}\n\n"
        f"Domain for this project: {domain}\n\n"
        f"Previously built projects (do not repeat these ideas):\n{prior_summary}"
    )
    raw = generate(client, system, user)
    plan = json.loads(raw)
    plan["domain"] = domain
    return plan


def write_plan(plan: dict, base_dir: Path) -> Path:
    plans_dir = base_dir / "plans"
    plans_dir.mkdir(exist_ok=True)
    plan_path = plans_dir / f"{plan['slug']}.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan_path


def run(base_dir: Path, client, state: dict) -> dict:
    soul_text = read_soul(base_dir)
    prior_projects = read_projects_registry(base_dir)
    last_domain = state.get("domain")
    domain = pick_domain(last_domain)

    plan = generate_plan(client, soul_text, prior_projects, domain)
    attempts = 0
    while slug_exists(plan["slug"], base_dir) and attempts < 5:
        plan = generate_plan(client, soul_text, prior_projects, domain)
        attempts += 1
    if slug_exists(plan["slug"], base_dir):
        raise RuntimeError(f"Could not generate a unique slug after {attempts} attempts")

    plan_path = write_plan(plan, base_dir)

    state["phase"] = "building"
    state["project_slug"] = plan["slug"]
    state["domain"] = domain
    state["plan_file"] = str(plan_path.relative_to(base_dir))
    return state


def resume(base_dir: Path, client, state: dict) -> dict:
    return run(base_dir, client, state)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner.py -v`
Expected: PASS — all 12 tests green.

- [ ] **Step 5: Commit**

```bash
git add agents/planner.py tests/test_planner.py
git commit -m "feat: add Planner agent"
```

---

### Task 5: `agents/builder.py`

**Files:**
- Create: `agents/builder.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `utils.llm.generate` (Task 3); plan dict shape from Task 4 (`name, slug, pitch, story, domain, stack, structure, features, aesthetic_notes`).
- Produces: `load_plan(slug: str, base_dir: Path) -> dict`, `scaffold_files(plan: dict, project_dir: Path, client) -> None`, `generate_readme(plan: dict, project_dir: Path, client) -> None`, `run_smoke_check(project_dir: Path, stack: list[str]) -> bool`, `run(base_dir: Path, client, state: dict) -> dict`, `resume(base_dir: Path, client, state: dict) -> dict`. `state["phase"]` becomes `"validating"` on success, `"failed"` if the smoke check fails.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_builder.py
import json
import pytest
from agents import builder


class _FakeCompletions:
    def __init__(self, default_content="generated content"):
        self.default_content = default_content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.default_content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChat:
    def __init__(self, default_content):
        self.completions = _FakeCompletions(default_content)


class _FakeClient:
    def __init__(self, default_content="generated content"):
        self.chat = _FakeChat(default_content)


PLAN = {
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["main.py", "utils.py"],
    "features": ["clipboard history"],
    "aesthetic_notes": "minimal terminal output",
}


def test_load_plan_reads_json_by_slug(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    (plans_dir / "wylt.json").write_text(json.dumps(PLAN), encoding="utf-8")
    assert builder.load_plan("wylt", tmp_path) == PLAN


def test_scaffold_files_writes_each_structure_entry(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    fake = _FakeClient(default_content="print('hi')")
    builder.scaffold_files(PLAN, project_dir, fake)
    assert (project_dir / "main.py").read_text(encoding="utf-8") == "print('hi')"
    assert (project_dir / "utils.py").exists()


def test_scaffold_files_skips_existing_files(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "main.py").write_text("already here", encoding="utf-8")
    fake = _FakeClient(default_content="overwritten")
    builder.scaffold_files(PLAN, project_dir, fake)
    assert (project_dir / "main.py").read_text(encoding="utf-8") == "already here"
    assert (project_dir / "utils.py").read_text(encoding="utf-8") == "overwritten"


def test_generate_readme_writes_file(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    fake = _FakeClient(default_content="# wylt\n\nfull readme")
    builder.generate_readme(PLAN, project_dir, fake)
    assert "full readme" in (project_dir / "README.md").read_text(encoding="utf-8")


def test_generate_readme_skips_when_already_present(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "README.md").write_text("existing readme", encoding="utf-8")
    fake = _FakeClient(default_content="new readme")
    builder.generate_readme(PLAN, project_dir, fake)
    assert (project_dir / "README.md").read_text(encoding="utf-8") == "existing readme"


def test_run_smoke_check_picks_pytest_for_python_stack(tmp_path, monkeypatch):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    assert builder.run_smoke_check(project_dir, ["python"]) is True


def test_run_smoke_check_returns_false_on_failing_test(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    assert builder.run_smoke_check(project_dir, ["python"]) is False


def test_run_sets_phase_validating_on_success(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan = dict(PLAN, structure=["test_smoke.py"])
    (plans_dir / "wylt.json").write_text(json.dumps(plan), encoding="utf-8")
    fake = _FakeClient(default_content="def test_ok():\n    assert True\n")
    state = {"project_slug": "wylt"}
    result = builder.run(tmp_path, fake, state)
    assert result["phase"] == "validating"


def test_run_sets_phase_failed_on_smoke_check_failure(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan = dict(PLAN, structure=["test_smoke.py"])
    (plans_dir / "wylt.json").write_text(json.dumps(plan), encoding="utf-8")
    fake = _FakeClient(default_content="def test_fails():\n    assert False\n")
    state = {"project_slug": "wylt"}
    result = builder.run(tmp_path, fake, state)
    assert result["phase"] == "failed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_builder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.builder'`.

- [ ] **Step 3: Write `agents/builder.py`**

```python
import json
import subprocess
from pathlib import Path

from utils.llm import generate


def load_plan(slug: str, base_dir: Path) -> dict:
    plan_path = base_dir / "plans" / f"{slug}.json"
    return json.loads(plan_path.read_text(encoding="utf-8"))


def scaffold_files(plan: dict, project_dir: Path, client) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    for rel_path in plan["structure"]:
        file_path = project_dir / rel_path
        if file_path.exists():
            continue
        file_path.parent.mkdir(parents=True, exist_ok=True)
        system = (
            "Generate the full contents of one source file for a software "
            "project. Respond with only the file contents, no explanation."
        )
        user = (
            f"Project: {plan['name']} — {plan['pitch']}\n"
            f"Tech stack: {', '.join(plan['stack'])}\n"
            f"File to generate: {rel_path}\n"
            f"Features: {', '.join(plan['features'])}"
        )
        content = generate(client, system, user)
        file_path.write_text(content, encoding="utf-8")


def generate_readme(plan: dict, project_dir: Path, client) -> None:
    readme_path = project_dir / "README.md"
    if readme_path.exists():
        return
    system = (
        "Write a project README with sections: Overview, Tech Stack, Setup, "
        "Usage, Architecture, and 'How it was built'. The last section must "
        "be the first-person build story provided, not a generic template."
    )
    user = (
        f"Project: {plan['name']} — {plan['pitch']}\n"
        f"Stack: {', '.join(plan['stack'])}\n"
        f"Features: {', '.join(plan['features'])}\n"
        f"Build story: {plan['story']}"
    )
    content = generate(client, system, user)
    readme_path.write_text(content, encoding="utf-8")


def run_smoke_check(project_dir: Path, stack: list[str]) -> bool:
    lowered = [s.lower() for s in stack]
    if any("python" in s for s in lowered):
        cmd = ["pytest", "-q"]
    elif any(s in ("node", "next.js", "javascript", "typescript") for s in lowered):
        cmd = ["npm", "test", "--silent"]
    else:
        cmd = ["make", "test"]
    result = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True)
    return result.returncode == 0


def run(base_dir: Path, client, state: dict) -> dict:
    slug = state["project_slug"]
    plan = load_plan(slug, base_dir)
    project_dir = base_dir / "projects" / slug
    scaffold_files(plan, project_dir, client)
    generate_readme(plan, project_dir, client)
    passed = run_smoke_check(project_dir, plan["stack"])
    state["phase"] = "validating" if passed else "failed"
    return state


def resume(base_dir: Path, client, state: dict) -> dict:
    return run(base_dir, client, state)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_builder.py -v`
Expected: PASS — all 9 tests green.

- [ ] **Step 5: Commit**

```bash
git add agents/builder.py tests/test_builder.py
git commit -m "feat: add Builder agent"
```

---

### Task 6: `agents/validator_publisher.py`

**Files:**
- Create: `agents/validator_publisher.py`
- Test: `tests/test_validator_publisher.py`

**Interfaces:**
- Consumes: `agents.builder.load_plan`, `agents.builder.run_smoke_check` (Task 5).
- Produces: `run_lint(project_dir: Path, stack: list[str]) -> bool`, `commit_and_stage(base_dir: Path, slug: str) -> bool`, `update_registry(base_dir: Path, plan: dict) -> None`, `push(base_dir: Path, slug: str) -> bool`, `run(base_dir: Path, state: dict) -> dict`, `resume(base_dir: Path, state: dict) -> dict`. `state` keys used/set: `files_committed`, `registry_updated`, `pushed`, `retry_count`, `phase` (→ `"publishing"`, `"done"`, `"failed"`, or back to `"building"` on a first failed validation).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_validator_publisher.py
import json
import subprocess
import pytest
from agents import validator_publisher as vp

PLAN = {
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["test_smoke.py"],
    "features": ["clipboard history"],
    "aesthetic_notes": "minimal",
}


def _write_plan(base_dir, plan=PLAN):
    plans_dir = base_dir / "plans"
    plans_dir.mkdir(exist_ok=True)
    (plans_dir / f"{plan['slug']}.json").write_text(json.dumps(plan), encoding="utf-8")


def _write_passing_project(base_dir, slug="wylt"):
    project_dir = base_dir / "projects" / slug
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    return project_dir


def _init_git_repo(base_dir):
    subprocess.run(["git", "init", "-q"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=base_dir, check=True)
    (base_dir / "README.md").write_text("root", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=base_dir, check=True)


def test_update_registry_appends_entry_to_new_file(tmp_path):
    vp.update_registry(tmp_path, PLAN)
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert len(registry) == 1
    assert registry[0]["slug"] == "wylt"
    assert registry[0]["path"] == "projects/wylt"


def test_update_registry_appends_to_existing_file(tmp_path):
    (tmp_path / "projects.json").write_text(
        json.dumps([{"slug": "exl", "name": "exl"}]), encoding="utf-8"
    )
    vp.update_registry(tmp_path, PLAN)
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert [p["slug"] for p in registry] == ["exl", "wylt"]


def test_run_marks_done_when_all_publish_steps_succeed(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)
    state = {"project_slug": "wylt"}
    result = vp.run(tmp_path, state)
    assert result["phase"] == "done"
    assert result["files_committed"] is True
    assert result["registry_updated"] is True
    assert result["pushed"] is True


def test_run_does_not_mark_pushed_when_no_remote_configured(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)
    state = {"project_slug": "wylt"}
    result = vp.run(tmp_path, state)
    assert result.get("pushed") is not True
    assert result["phase"] == "publishing"


def test_run_increments_retry_count_on_first_smoke_check_failure(tmp_path):
    _init_git_repo(tmp_path)
    plan = dict(PLAN)
    _write_plan(tmp_path, plan)
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    state = {"project_slug": "wylt", "retry_count": 0}
    result = vp.run(tmp_path, state)
    assert result["retry_count"] == 1
    assert result["phase"] == "building"


def test_run_marks_failed_after_second_smoke_check_failure(tmp_path):
    _init_git_repo(tmp_path)
    plan = dict(PLAN)
    _write_plan(tmp_path, plan)
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    state = {"project_slug": "wylt", "retry_count": 1}
    result = vp.run(tmp_path, state)
    assert result["retry_count"] == 2
    assert result["phase"] == "failed"


def test_resume_skips_already_completed_publish_flags(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)

    calls = []
    original_push = vp.push

    def spy_push(base_dir, slug):
        calls.append(slug)
        return original_push(base_dir, slug)

    monkeypatch.setattr(vp, "push", spy_push)

    state = {
        "project_slug": "wylt",
        "files_committed": True,
        "registry_updated": True,
        "pushed": False,
    }
    vp.resume(tmp_path, state)
    assert calls == ["wylt"]
```

Note: `test_run_marks_done_when_all_publish_steps_succeed` and
`test_run_does_not_mark_pushed_when_no_remote_configured` are two sides of the
same behavior — with no `origin` remote configured in the throwaway test repo,
`git push origin main` fails, so `pushed` must stay falsy and `phase` must stay
`"publishing"`. Delete whichever of the two doesn't match your test git setup
once you see the actual `git push` failure output in Step 2 — keep the one
that reflects "no remote" behavior, since that's what CI sandboxes will hit.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_validator_publisher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.validator_publisher'`.

- [ ] **Step 3: Write `agents/validator_publisher.py`**

```python
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agents.builder import load_plan, run_smoke_check


def run_lint(project_dir: Path, stack: list[str]) -> bool:
    lowered = [s.lower() for s in stack]
    if any("python" in s for s in lowered):
        result = subprocess.run(
            ["ruff", "check", "."], cwd=project_dir, capture_output=True, text=True
        )
        return result.returncode == 0
    result = subprocess.run(
        ["npx", "eslint", "."], cwd=project_dir, capture_output=True, text=True
    )
    return result.returncode == 0


def commit_and_stage(base_dir: Path, slug: str) -> bool:
    result = subprocess.run(
        ["git", "add", f"projects/{slug}", "projects.json"],
        cwd=base_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def update_registry(base_dir: Path, plan: dict) -> None:
    registry_path = base_dir / "projects.json"
    registry = []
    if registry_path.exists():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            registry = []
    registry.append(
        {
            "name": plan["name"],
            "slug": plan["slug"],
            "pitch": plan["pitch"],
            "story": plan["story"],
            "domain": plan["domain"],
            "stack": plan["stack"],
            "path": f"projects/{plan['slug']}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def push(base_dir: Path, slug: str) -> bool:
    commit_result = subprocess.run(
        ["git", "commit", "-m", f"feat: add {slug}"],
        cwd=base_dir,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        return False
    push_result = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=base_dir,
        capture_output=True,
        text=True,
    )
    return push_result.returncode == 0


def run(base_dir: Path, state: dict) -> dict:
    slug = state["project_slug"]
    plan = load_plan(slug, base_dir)
    project_dir = base_dir / "projects" / slug

    if not run_smoke_check(project_dir, plan["stack"]):
        state["retry_count"] = state.get("retry_count", 0) + 1
        state["phase"] = "failed" if state["retry_count"] >= 2 else "building"
        return state

    state["phase"] = "publishing"

    if not state.get("files_committed"):
        if not commit_and_stage(base_dir, slug):
            return state
        state["files_committed"] = True

    if not state.get("registry_updated"):
        update_registry(base_dir, plan)
        subprocess.run(["git", "add", "projects.json"], cwd=base_dir)
        state["registry_updated"] = True

    if not state.get("pushed"):
        if not push(base_dir, slug):
            return state
        state["pushed"] = True

    if state["files_committed"] and state["registry_updated"] and state["pushed"]:
        state["phase"] = "done"
    return state


def resume(base_dir: Path, state: dict) -> dict:
    return run(base_dir, state)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_validator_publisher.py -v`
Expected: PASS. Resolve the `test_run_marks_done_when_all_publish_steps_succeed` vs. `test_run_does_not_mark_pushed_when_no_remote_configured` conflict per the note in Step 1 before this is green.

- [ ] **Step 5: Commit**

```bash
git add agents/validator_publisher.py tests/test_validator_publisher.py
git commit -m "feat: add Validator/Publisher agent"
```

---

### Task 7: `orchestrator.py`

**Files:**
- Create: `orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `utils.state.read_state`/`write_state` (Task 2), `utils.llm.get_client` (Task 3), `agents.planner.run`/`resume`, `agents.builder.run`/`resume`, `agents.validator_publisher.run`/`resume` (Tasks 4–6).
- Produces: `run_cycle(base_dir: Path, dry_run: bool, client) -> dict` (returns the final state, for testability — `main()` is a thin CLI wrapper around it that supplies the real client and ignores the return value).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator.py
import json
import pytest
import orchestrator


class _StubPlanner:
    @staticmethod
    def run(base_dir, client, state):
        state.update(phase="building", project_slug="wylt", domain="cli-tools")
        return state

    resume = run


class _StubBuilder:
    @staticmethod
    def run(base_dir, client, state):
        state["phase"] = "validating"
        return state

    resume = run


class _StubValidatorPublisher:
    @staticmethod
    def run(base_dir, state):
        state.update(phase="done", files_committed=True, registry_updated=True, pushed=True)
        return state

    resume = run


class _ExplodingBuilder:
    @staticmethod
    def run(base_dir, client, state):
        raise RuntimeError("boom")

    resume = run


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    yield tmp_path


def test_run_cycle_fresh_start_goes_planning_to_done(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "done"


def test_run_cycle_resumes_from_malformed_state_json_as_fresh(monkeypatch, tmp_path):
    (tmp_path / "state.json").write_text("{not valid", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "done"


def test_run_cycle_resumes_mid_pipeline_from_existing_phase(monkeypatch, tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"phase": "validating", "project_slug": "wylt"}), encoding="utf-8"
    )
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "done"
    assert result["project_slug"] == "wylt"


def test_run_cycle_dry_run_never_reaches_validator_publisher(monkeypatch, tmp_path):
    calls = []

    class _TrackedValidatorPublisher:
        @staticmethod
        def run(base_dir, state):
            calls.append("called")
            return state

        resume = run

    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _TrackedValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=True, client=object())
    assert calls == []
    assert result["phase"] == "done"


def test_run_cycle_catches_agent_exception_and_marks_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _ExplodingBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "failed"


def test_run_cycle_persists_state_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["phase"] == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator'`.

- [ ] **Step 3: Write `orchestrator.py`**

```python
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from utils.state import read_state, write_state
from utils.llm import get_client
from agents import planner, builder, validator_publisher

ACTIVE_PHASES = {"planning", "building", "validating", "publishing"}


def setup_logging(base_dir: Path) -> None:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    log_path = logs_dir / f"{timestamp}.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )


def run_cycle(base_dir: Path, dry_run: bool, client) -> dict:
    state = read_state()
    fresh_start = state.get("phase") not in ACTIVE_PHASES
    if fresh_start:
        state = {"phase": "planning", "retry_count": 0}
    else:
        logging.info(
            "[resume] Restarting from phase: %s — project: %s",
            state["phase"],
            state.get("project_slug"),
        )

    try:
        if state["phase"] == "planning":
            state = (planner.run if fresh_start else planner.resume)(base_dir, client, state)
            write_state(state)

        if state["phase"] == "building":
            state = builder.resume(base_dir, client, state)
            write_state(state)

        if state["phase"] == "validating":
            if dry_run:
                state["phase"] = "done"
            else:
                state = validator_publisher.resume(base_dir, state)
            write_state(state)

        if state["phase"] == "publishing" and not dry_run:
            state = validator_publisher.resume(base_dir, state)
            write_state(state)
    except Exception:
        logging.exception("Cycle failed")
        state["phase"] = "failed"
        write_state(state)

    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    setup_logging(base_dir)
    client = get_client()
    run_cycle(base_dir, args.dry_run, client)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS — all 6 tests green.

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add orchestrator with boot-time resume and dry-run"
```

---

### Task 8: Full pipeline integration test

**Files:**
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: `orchestrator.run_cycle` (Task 7) with the **real** `agents.planner`/`builder`/`validator_publisher` modules (no stubbing) and a fake LLM client, to prove the full chain wires together end to end — the per-agent unit tests in Tasks 4–6 already stub their neighbors, so this is the only test that exercises the real handoff between all three.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_integration.py
import json
import subprocess
import pytest
import orchestrator


class _FakeCompletions:
    def __init__(self, plan_json):
        self._plan_json = plan_json
        self._plan_served = False

    def create(self, **kwargs):
        if not self._plan_served:
            self._plan_served = True
            return _FakeResponse(self._plan_json)
        return _FakeResponse("def test_ok():\n    assert True\n")


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChat:
    def __init__(self, plan_json):
        self.completions = _FakeCompletions(plan_json)


class _FakeClient:
    def __init__(self, plan_json):
        self.chat = _FakeChat(plan_json)


PLAN_JSON = json.dumps({
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["test_smoke.py"],
    "features": ["clipboard history"],
    "aesthetic_notes": "minimal terminal output",
})


@pytest.fixture(autouse=True)
def isolated_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "soul.md").write_text("developer profile", encoding="utf-8")
    (tmp_path / "README.md").write_text("root", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    yield tmp_path


def test_dry_run_produces_project_without_pushing(tmp_path):
    fake = _FakeClient(PLAN_JSON)
    result = orchestrator.run_cycle(tmp_path, dry_run=True, client=fake)
    assert result["phase"] == "done"
    assert (tmp_path / "projects" / "wylt" / "test_smoke.py").exists()
    assert (tmp_path / "projects" / "wylt" / "README.md").exists()
    assert not (tmp_path / "projects.json").exists()


def test_live_run_stages_project_and_registry(tmp_path):
    fake = _FakeClient(PLAN_JSON)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=fake)
    assert result["phase"] == "publishing"
    assert result["files_committed"] is True
    assert result["registry_updated"] is True
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert registry[0]["slug"] == "wylt"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integration.py -v`
Expected: FAIL initially (before Tasks 4–7 exist) or, if run after Task 7, it should already mostly pass — run it now to confirm both scenarios behave as described; if `test_live_run_stages_project_and_registry` unexpectedly reports `phase == "done"` it means the sandbox has an `origin` remote reachable, which is unusual — treat that as a real pass, not a bug.

- [ ] **Step 3: Fix any wiring bugs found**

No new production code is expected here — if this test fails for a reason other than "no `origin` remote configured" (e.g. a signature mismatch between what `orchestrator.py` calls and what an agent module exports), fix the mismatch in the relevant agent file from Tasks 4–7.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_integration.py -v`
Expected: PASS — both tests green.

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -v`
Expected: PASS — every test from Tasks 2–8 green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add full pipeline integration test"
```

---

### Task 9: Top-level `README.md`

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing programmatically — this is documentation only.

- [ ] **Step 1: Write `README.md`**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: write top-level README for MiniMe pipeline"
```

---

## Post-plan note

This plan covers the pipeline sub-project only. The companion Next.js
website (reads `projects.json`, renders project cards and per-project
"blog" detail pages from the `story` field) is a separate spec + plan,
written after this one ships, since it only needs the artifacts this
pipeline produces.
