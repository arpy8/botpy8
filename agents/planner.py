import json
import random
import re
from pathlib import Path

from utils.llm import generate

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")

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


def is_valid_slug(slug: str) -> bool:
    return bool(SLUG_PATTERN.match(slug))


def _strip_json_fences(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped


def _ensure_smoke_test_file(plan: dict) -> dict:
    structure = plan.get("structure", [])
    if any("test" in entry.lower() for entry in structure):
        return plan
    stack = [s.lower() for s in plan.get("stack", [])]
    if any("python" in s for s in stack):
        structure.append("test_smoke.py")
    else:
        structure.append("smoke.test.js")
    plan["structure"] = structure
    return plan


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
    plan = json.loads(_strip_json_fences(raw))
    plan["domain"] = domain
    plan = _ensure_smoke_test_file(plan)
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

    def _needs_regeneration(candidate: dict) -> bool:
        return not is_valid_slug(candidate["slug"]) or slug_exists(candidate["slug"], base_dir)

    plan = generate_plan(client, soul_text, prior_projects, domain)
    attempts = 0
    while _needs_regeneration(plan) and attempts < 5:
        plan = generate_plan(client, soul_text, prior_projects, domain)
        attempts += 1
    if _needs_regeneration(plan):
        raise RuntimeError(f"Could not generate a valid, unique slug after {attempts} attempts")

    plan_path = write_plan(plan, base_dir)

    state["phase"] = "building"
    state["project_slug"] = plan["slug"]
    state["domain"] = domain
    state["plan_file"] = str(plan_path.relative_to(base_dir))
    return state


def resume(base_dir: Path, client, state: dict) -> dict:
    return run(base_dir, client, state)
